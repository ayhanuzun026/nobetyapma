"""
Hedef Hesaplayıcı — OR-Tools CP-SAT ile nöbet hedeflerini dengeli dağıtır.
Üçlü dengeleme: Sayı dengesi, Saat dengesi, WE/WD dengesi.
"""

import math
from typing import List, Dict

from utils import (
    GUN_TIPLERI, SAAT_DEGERLERI,
    find_matching_id,
    birlikte_aile_anahtari,
    BIRLIKTE_ESDEGER_GOREV_AILE_ADI,
)
from solver_models import (
    SolverPersonel, SolverGorev, SolverKural, SolverAtama,
    HedefSonuc,
)

# Lazy import for ortools (Firebase deploy timeout fix) — thread-safe
import threading

_cp_model_lock = threading.Lock()
_cp_model_module = None

def _get_cp_model():
    global _cp_model_module
    if _cp_model_module is None:
        with _cp_model_lock:
            if _cp_model_module is None:
                from ortools.sat.python import cp_model as _cm
                _cp_model_module = _cm
    return _cp_model_module


class HedefHesaplayici:
    def __init__(self, gun_sayisi: int, gun_tipleri: Dict[int, str],
                 personeller: List[SolverPersonel], gorevler: List[SolverGorev],
                 birlikte_kurallar: List[SolverKural] = None,
                 gorev_kisitlamalari: Dict[int, str] = None,
                 manuel_atamalar: List[SolverAtama] = None,
                 ara_gun: int = 2, saat_degerleri: Dict[str, int] = None,
                 kilitli_hedefler: Dict[int, Dict[str, int]] = None):
        self.gun_sayisi = gun_sayisi
        self.gun_tipleri = gun_tipleri
        self.personeller = {p.id: p for p in personeller}
        self.personel_listesi = personeller
        self.gorevler = gorevler
        self.birlikte_kurallar = birlikte_kurallar or []
        self.gorev_kisitlamalari = gorev_kisitlamalari or {}
        self.manuel_atamalar = manuel_atamalar or []
        self.ara_gun = ara_gun
        self.saat = saat_degerleri or SAAT_DEGERLERI
        self.slot_sayisi = len(gorevler) if gorevler else 6
        self.kilitli_hedefler = kilitli_hedefler or {}

        self.tip_sayilari = {t: 0 for t in GUN_TIPLERI}
        for g, tip in gun_tipleri.items():
            if tip in self.tip_sayilari:
                self.tip_sayilari[tip] += 1
        self.tip_slotlari = {t: self.tip_sayilari[t] * self.slot_sayisi for t in GUN_TIPLERI}
        self.toplam_slot = sum(self.tip_slotlari.values())
        self._hesapla_kapasiteler()

    def _hesapla_kapasiteler(self):
        for p in self.personel_listesi:
            p.musait_tipler = {t: 0 for t in GUN_TIPLERI}
            p.musait_gunler = set()
            for g, tip in self.gun_tipleri.items():
                if g not in p.mazeret_gunleri:
                    p.musait_tipler[tip] += 1
                    p.musait_gunler.add(g)

    def _birlikte_ortak_musait_tipler(self, grup_ids: List) -> Dict[str, int]:
        """Birlikte grubundaki kişilerin ortak müsait gün tiplerini hesapla"""
        ortak = {t: float('inf') for t in GUN_TIPLERI}
        for pid in grup_ids:
            p = self.personeller.get(pid)
            if p:
                for t in GUN_TIPLERI:
                    ortak[t] = min(ortak[t], p.musait_tipler.get(t, 0))
        # inf -> 0
        return {t: (v if v != float('inf') else 0) for t, v in ortak.items()}

    def _birlikte_gruplari_dengele(self):
        """Birlikte tutulacak kişilere aynı gün tipi ve görev kotası hedefleri ata"""
        if not self.birlikte_kurallar:
            return

        for kural in self.birlikte_kurallar:
            if kural.tur != 'birlikte':
                continue

            grup_ids = kural.kisiler
            if len(grup_ids) < 2:
                continue

            # Grubun ortak müsait gün tiplerini bul
            ortak_musait = self._birlikte_ortak_musait_tipler(grup_ids)

            # Grubun mevcut hedeflerinin minimumunu al
            grup_hedefler = []
            grup_gorev_kotalari = []
            for pid in grup_ids:
                p = self.personeller.get(pid)
                if p and hasattr(p, 'hedef_tipler'):
                    grup_hedefler.append(p.hedef_tipler.copy())
                    # Görev kotalarını da topla
                    if hasattr(p, 'gorev_kotalari') and p.gorev_kotalari:
                        grup_gorev_kotalari.append(p.gorev_kotalari.copy())

            if not grup_hedefler:
                continue

            # Her gün tipi için minimum hedefi bul (ortak kapasiteyi aşmayacak şekilde)
            min_hedef = {}
            for t in GUN_TIPLERI:
                tip_hedefler = [h.get(t, 0) for h in grup_hedefler]
                # Ortalama hedef = grubun ort. hedefi ama ortak kapasiteyi aşmasın
                min_hedef[t] = min(max(tip_hedefler), ortak_musait[t])

            # Görev kotalarını da dengele (ortak görevler için minimum al)
            ortak_gorev_kota = {}
            if grup_gorev_kotalari:
                # Tüm görev isimlerini topla
                tum_gorevler = set()
                for gk in grup_gorev_kotalari:
                    tum_gorevler.update(gk.keys())

                # Her görev için minimum kotayı bul
                for gorev in tum_gorevler:
                    kotalar = [gk.get(gorev, 0) for gk in grup_gorev_kotalari]
                    # Sadece hepsinde varsa (0'dan büyükse) dengele
                    if all(k > 0 for k in kotalar):
                        ortak_gorev_kota[gorev] = min(kotalar)

            # Tüm grup üyelerine aynı hedefi ata
            for pid in grup_ids:
                p = self.personeller.get(pid)
                if p:
                    p.hedef_tipler = min_hedef.copy()
                    # Görev kotalarını da güncelle (varsa)
                    if ortak_gorev_kota:
                        if not hasattr(p, 'gorev_kotalari') or not p.gorev_kotalari:
                            p.gorev_kotalari = {}
                        for gorev, kota in ortak_gorev_kota.items():
                            p.gorev_kotalari[gorev] = kota

    def _sirala_mazerete_gore(self):
        """Personelleri mazeret sayısına göre sırala (en mazeretli önce)"""
        self.personel_listesi.sort(key=lambda p: len(p.mazeret_gunleri), reverse=True)

    def _sirala_birlikte_gruplari(self):
        """Birlikte gruplarını toplam mazeret sayısına göre sırala (en mazeretli grup önce)"""
        if not self.birlikte_kurallar:
            return

        def grup_mazeret_skoru(kural):
            toplam = 0
            for pid in kural.kisiler:
                p = self.personeller.get(pid)
                if p:
                    toplam += len(p.mazeret_gunleri)
            return toplam

        self.birlikte_kurallar.sort(key=grup_mazeret_skoru, reverse=True)

    def _yillik_dengeleme_hedef_ayarla(self):
        """Yıllık gerçekleşene göre hedefleri ayarla (eksik olan daha fazla alsın)
        NOT: Toplam slot sayısı değişmez - birinden alıp diğerine ver mantığı
        """
        # Yıllık verisi olan personelleri bul
        yillik_verileri = []
        for p in self.personel_listesi:
            if hasattr(p, 'yillik_gerceklesen') and p.yillik_gerceklesen:
                yillik_toplam = sum(p.yillik_gerceklesen.values())
                yillik_verileri.append((p, yillik_toplam))

        if not yillik_verileri or len(yillik_verileri) < 2:
            return

        # Ortalamayı hesapla
        ortalama = sum(v[1] for v in yillik_verileri) / len(yillik_verileri)

        # Eksik ve fazla olanları ayır
        eksik_olanlar = []  # (personel, eksik_miktar)
        fazla_olanlar = []  # (personel, fazla_miktar)

        for p, yillik_toplam in yillik_verileri:
            fark = yillik_toplam - ortalama
            if fark < -2:  # Ortalamadan 2+ eksik
                eksik_olanlar.append((p, int(abs(fark) / 2)))
            elif fark > 2:  # Ortalamadan 2+ fazla
                fazla_olanlar.append((p, int(fark / 2)))

        # Fazla olanlardan al, eksik olanlara ver (toplam değişmez)
        transfer_havuzu = {t: 0 for t in GUN_TIPLERI}

        # Önce fazla olanlardan al
        for p, azalt in fazla_olanlar:
            azalt = min(azalt, 2)  # Max 2 azalt
            for tip in GUN_TIPLERI:
                if azalt <= 0:
                    break
                if p.hedef_tipler.get(tip, 0) > 0:
                    p.hedef_tipler[tip] = p.hedef_tipler.get(tip, 0) - 1
                    transfer_havuzu[tip] += 1
                    azalt -= 1

        # Sonra eksik olanlara ver (havuzdan)
        for p, ekstra in eksik_olanlar:
            ekstra = min(ekstra, 2)  # Max 2 ekstra
            for tip in GUN_TIPLERI:
                if ekstra <= 0:
                    break
                if transfer_havuzu[tip] > 0 and p.musait_tipler.get(tip, 0) > p.hedef_tipler.get(tip, 0):
                    p.hedef_tipler[tip] = p.hedef_tipler.get(tip, 0) + 1
                    transfer_havuzu[tip] -= 1
                    ekstra -= 1

        # Özel görev dengeleme: gecmis_gorevler verisiyle görev kotalarını ayarla
        self._yillik_gorev_dengeleme()

    def _yillik_gorev_dengeleme(self):
        """Özel görev geçmişine göre görev kotalarını dengeleyerek yıl sonu eşitliği sağla.
        Geçmişte az yapanın kotasını artır, çok yapanınkini azalt.
        """
        # Geçmiş görev verisi olan personelleri bul
        gecmis_olan = [p for p in self.personel_listesi
                       if hasattr(p, 'gecmis_gorevler') and p.gecmis_gorevler]
        if len(gecmis_olan) < 2:
            return

        # Tüm özel görev isimlerini topla
        tum_gorevler = set()
        for p in gecmis_olan:
            tum_gorevler.update(p.gecmis_gorevler.keys())

        if not tum_gorevler:
            return

        # Her görev için dengeleme yap
        for gorev_adi in tum_gorevler:
            # Bu göreve atanabilecek personelleri bul (kotası olan veya geçmişi olan)
            ilgili = []
            for p in gecmis_olan:
                gecmis = p.gecmis_gorevler.get(gorev_adi, 0)
                kota = p.gorev_kotalari.get(gorev_adi, 0)
                if gecmis > 0 or kota > 0:
                    ilgili.append((p, gecmis))

            if len(ilgili) < 2:
                continue

            # Ortalama geçmiş
            ort = sum(g for _, g in ilgili) / len(ilgili)

            transfer = 0
            eksik_kisiler = []
            fazla_kisiler = []

            for p, gecmis in ilgili:
                fark = gecmis - ort
                if fark > 1:  # Ortalamadan 1+ fazla yapmış
                    azalt = min(int(fark / 2), 1)  # Max 1 azalt
                    mevcut = p.gorev_kotalari.get(gorev_adi, 0)
                    if mevcut > 0 and azalt > 0:
                        p.gorev_kotalari[gorev_adi] = mevcut - azalt
                        transfer += azalt
                elif fark < -1:  # Ortalamadan 1+ eksik yapmış
                    eksik_kisiler.append((p, min(int(abs(fark) / 2), 1)))

            # Eksik olanlara ver
            for p, ekstra in eksik_kisiler:
                if transfer <= 0:
                    break
                ver = min(ekstra, transfer)
                p.gorev_kotalari[gorev_adi] = p.gorev_kotalari.get(gorev_adi, 0) + ver
                transfer -= ver

    def hesapla(self) -> HedefSonuc:
        """
        ÜÇLÜ DENGELEME SİSTEMİ
        1. Sayı Dengesi (Kelepçe) - Makas açılmasın
        2. Saat Dengesi - Yorgunluk eşitlensin
        3. WE/WD Dengesi - Hafta sonu adil dağılsın
        """
        n = len(self.personel_listesi)
        if n == 0:
            return HedefSonuc(False, [], [], {}, {}, "Personel yok")

        self._sirala_birlikte_gruplari()

        # --- 1. HEDEF VE ORTALAMA ANALİZİ ---

        # A) SAYI ORTALAMASI
        avg_count_float = self.toplam_slot / n
        avg_count_floor = int(avg_count_float)
        HARD_CAP = math.ceil(avg_count_float) + 1  # Kesin üst sınır

        # B) SAAT ORTALAMASI
        total_hours_needed = sum(self.tip_slotlari[tip] * self.saat[tip] for tip in GUN_TIPLERI)
        avg_hours = int(total_hours_needed / n)

        # C) HAFTA SONU ORANI
        we_tipleri = ['cum', 'cmt', 'pzr']
        wd_tipleri = ['hici', 'prs']
        total_we_slots = sum(self.tip_slotlari[tip] for tip in we_tipleri)
        total_wd_slots = sum(self.tip_slotlari[tip] for tip in wd_tipleri)

        # Görev kısıtlamalı kişilerin kapasite sınırları (taşma görevi dahil)
        kisitli_kapasite = {}
        for pid, kisit_bilgi in self.gorev_kisitlamalari.items():
            # Yeni format: dict, eski format: str (geriye uyumluluk)
            if isinstance(kisit_bilgi, dict):
                ana_gorev = kisit_bilgi.get("gorevAdi", "")
                tasma = kisit_bilgi.get("tasmaGorevi")
            else:
                ana_gorev = kisit_bilgi
                tasma = None
            slot_sayisi = sum(1 for g in self.gorevler if g.base_name == ana_gorev or g.ad == ana_gorev)
            if tasma:
                slot_sayisi += sum(1 for g in self.gorevler if g.base_name == tasma or g.ad == tasma)
            if slot_sayisi > 0:
                kisitli_kapasite[pid] = slot_sayisi * self.gun_sayisi

        # Manuel atama sayacı
        manuel_sayac = {p.id: {tip: 0 for tip in GUN_TIPLERI} for p in self.personel_listesi}
        for m in self.manuel_atamalar:
            if m.personel_id is None:
                continue
            tip = self.gun_tipleri.get(m.gun, 'hici')
            matched_id = find_matching_id(m.personel_id, manuel_sayac.keys())
            if matched_id is not None:
                manuel_sayac[matched_id][tip] += 1

        # Başlangıç hedefleri + kilitli hedef uygulaması
        kilitli_ids = set()
        kilitli_toplam_slot = 0  # Kilitli kişilerin kapladığı toplam slot
        for p in self.personel_listesi:
            pid = p.id
            matched_kilitli = find_matching_id(pid, self.kilitli_hedefler.keys())
            if matched_kilitli is not None:
                # Kilitli kişi: hedefi frontend'den gelen sabit değere ayarla
                kilitli = self.kilitli_hedefler[matched_kilitli]
                p.hedef_tipler = {tip: kilitli.get(tip, 0) for tip in GUN_TIPLERI}
                kilitli_ids.add(pid)
                kilitli_toplam_slot += sum(p.hedef_tipler.values())
            else:
                p.hedef_tipler = {tip: manuel_sayac[pid][tip] for tip in GUN_TIPLERI}

        # Kilitli kişiler çıkarıldıktan sonra kalan slotlar üzerinden ortalamayı yeniden hesapla
        kilitsiz_personel = [p for p in self.personel_listesi if p.id not in kilitli_ids]
        kalan_slot = self.toplam_slot - kilitli_toplam_slot
        n_kilitsiz = len(kilitsiz_personel)
        if n_kilitsiz > 0:
            avg_count_float = kalan_slot / n_kilitsiz
            avg_count_floor = int(avg_count_float)
            HARD_CAP = avg_count_floor + 2

        # --- 2. OR-TOOLS MODELİ ---
        cp = _get_cp_model()
        model = cp.CpModel()

        h = {}  # h[pid, tip]: Kişinin o tipteki nöbet sayısı
        t = {}  # t[pid]: Kişinin toplam nöbet sayısı
        total_h_hours = {}  # Kişinin toplam saati
        total_h_we = {}     # Kişinin toplam WE sayısı

        penalties = []
        birlikte_debug = []

        for p in self.personel_listesi:
            pid = p.id
            is_kilitli = pid in kilitli_ids

            # Kişinin kapasitesi
            max_kapasite = sum(p.musait_tipler.get(tip, 0) for tip in GUN_TIPLERI)

            # Görev kısıtlaması varsa kapasiteyi sınırla
            matched_kisitli = find_matching_id(pid, kisitli_kapasite.keys())
            if matched_kisitli is not None:
                max_kapasite = min(max_kapasite, kisitli_kapasite[matched_kisitli])

            if is_kilitli:
                # KİLİTLİ KİŞİ: Sabit değer (Hard Constraint)
                kilitli_val = p.hedef_tipler
                kilitli_total = sum(kilitli_val.values())
                for tip in GUN_TIPLERI:
                    val = kilitli_val.get(tip, 0)
                    h[pid, tip] = model.NewIntVar(val, val, f'h_{pid}_{tip}_LOCKED')
                t[pid] = model.NewIntVar(kilitli_total, kilitli_total, f't_{pid}_LOCKED')
                model.Add(sum(h[pid, tip] for tip in GUN_TIPLERI) == t[pid])
                total_h_hours[pid] = sum(h[pid, tip] * self.saat[tip] for tip in GUN_TIPLERI)
                total_h_we[pid] = sum(h[pid, tip] for tip in we_tipleri)
                # Kilitli kişiye ceza uygulanmaz - continue
                continue

            # Manuel atama sayısı
            manuel_total = sum(manuel_sayac[pid].values())

            # Gün tipi değişkenleri
            for tip in GUN_TIPLERI:
                manuel_count = manuel_sayac[pid][tip]
                musait_sayisi = p.musait_tipler.get(tip, 0)

                if manuel_count > musait_sayisi:
                    return HedefSonuc(False, [], [], {}, {}, f"Manuel atama kapasiteyi aşıyor: {p.ad} / {tip}")

                h[pid, tip] = model.NewIntVar(manuel_count, musait_sayisi, f'h_{pid}_{tip}')

            # Manuel atamalar kullanıcının bilinçli tercihi olabilir; bu yüzden
            # adalet cap'i, zaten yapılmış manuel nöbetlerin altına inmemeli.
            if manuel_total > max_kapasite:
                return HedefSonuc(
                    False, [], [], {}, {},
                    f"Manuel atama toplam kapasiteyi aşıyor: {p.ad} / manuel={manuel_total} kapasite={max_kapasite}"
                )

            # Toplam hedef (HARD_CAP sınırlı), ancak manuel atama sayısı kadar genişletilir.
            upper_bound = max(manuel_total, min(max_kapasite, HARD_CAP))
            t[pid] = model.NewIntVar(manuel_total, upper_bound, f't_{pid}')

            # Toplam nöbet sayısı eşitliği
            model.Add(sum(h[pid, tip] for tip in GUN_TIPLERI) == t[pid])

            # Saat ve WE toplamları
            total_h_hours[pid] = sum(h[pid, tip] * self.saat[tip] for tip in GUN_TIPLERI)
            total_h_we[pid] = sum(h[pid, tip] for tip in we_tipleri)

            # --- 3. CEZA MEKANİZMALARI ---

            # A) SAYI DENGESİ (KELEPÇE) - ÖNCELİK 1
            mazeret_orani = len(p.mazeret_gunleri) / self.gun_sayisi if self.gun_sayisi > 0 else 0
            cok_mazeretli = mazeret_orani > 0.4

            if cok_mazeretli:
                target_limit = avg_count_floor  # Mazeretli: taban hedef
            else:
                target_limit = avg_count_floor + 1  # Normal: tavan hedef

            # Fazlalık (Slack) değişkeni
            excess = model.NewIntVar(0, 10, f'excess_{pid}')
            model.Add(t[pid] <= target_limit + excess)

            # Karesel ceza (makas kontrolü) - ÇOK YÜKSEK
            excess_sq = model.NewIntVar(0, 100, f'excess_sq_{pid}')
            model.AddMultiplicationEquality(excess_sq, [excess, excess])
            penalties.append(excess_sq * 100000)

            # Alt sınır kontrolü (aşağı makas açılmasın)
            if not cok_mazeretli:
                missing = model.NewIntVar(0, 10, f'missing_{pid}')
                min_hedef = max(0, avg_count_floor - 1)
                model.Add(t[pid] >= min_hedef - missing)
                missing_sq = model.NewIntVar(0, 100, f'missing_sq_{pid}')
                model.AddMultiplicationEquality(missing_sq, [missing, missing])
                penalties.append(missing_sq * 100000)

            # B) SAAT DENGESİ - ÖNCELİK 2
            hour_diff = model.NewIntVar(0, 200, f'h_diff_{pid}')
            model.AddAbsEquality(hour_diff, total_h_hours[pid] - avg_hours)
            penalties.append(hour_diff * 50)

            # C) HAFTA SONU DENGESİ - ÖNCELİK 3
            # (KisiWE * ToplamSlot) vs (KisiToplam * ToplamWE)
            we_balance_diff = model.NewIntVar(0, 5000, f'we_diff_{pid}')
            val1 = total_h_we[pid] * self.toplam_slot
            val2 = t[pid] * total_we_slots
            model.AddAbsEquality(we_balance_diff, val1 - val2)
            penalties.append(we_balance_diff * 100)

        # --- 4. ZORUNLU KISITLAR ---
        pids = [p.id for p in self.personel_listesi]

        # Toplam slot tutmalı
        model.Add(sum(t[pid] for pid in pids) == self.toplam_slot)

        # Gün tipi toplamları tutmalı
        for tip in GUN_TIPLERI:
            model.Add(sum(h[pid, tip] for pid in pids) == self.tip_slotlari[tip])

        # --- 5. BİRLİKTE KURALLARI ---
        for kural in self.birlikte_kurallar:
            if kural.tur != 'birlikte':
                continue

            grup = []
            grup_adlar = []
            for pid in kural.kisiler:
                matched_id = find_matching_id(pid, self.personeller.keys())
                if matched_id is not None:
                    grup.append(matched_id)
                    grup_adlar.append(self.personeller[matched_id].ad)

            if len(grup) < 2:
                birlikte_debug.append(f"Grup yetersiz: {grup_adlar}")
                continue

            birlikte_debug.append(f"Grup: {grup_adlar}")

            # All-pairs: tüm çiftleri karşılaştır — SOFT constraint
            for i in range(len(grup)):
                for j in range(i + 1, len(grup)):
                    p1_id, p2_id = grup[i], grup[j]
                    diff = model.NewIntVar(-HARD_CAP, HARD_CAP, f'birlikte_diff_{p1_id}_{p2_id}')
                    model.Add(t[p1_id] - t[p2_id] == diff)
                    abs_diff = model.NewIntVar(0, HARD_CAP, f'abs_birlikte_{p1_id}_{p2_id}')
                    model.AddAbsEquality(abs_diff, diff)
                    penalties.append(abs_diff * 500)

        # --- 6. ÇÖZÜM ---
        model.Minimize(sum(penalties))

        # MODEL VALIDATE — hangi kısıt geçersiz?
        validation_err = model.Validate()
        if validation_err:
            return HedefSonuc(False, [], [], {}, {},
                f"MODEL_INVALID validate: {validation_err}")

        solver = cp.CpSolver()
        solver.parameters.max_time_in_seconds = 10
        solver.parameters.num_search_workers = 4
        status = solver.Solve(model)

        if status not in [cp.OPTIMAL, cp.FEASIBLE]:
            # === DETAYLI İZOLASYON DEBUG ===
            debug_info = []
            debug_info.append(f"STATUS={status}")
            debug_info.append(f"toplam_slot={self.toplam_slot}, personel={n}, kilitli={len(kilitli_ids)}")
            debug_info.append(f"kalan_slot={kalan_slot}, kilitsiz={n_kilitsiz}, avg={avg_count_float:.2f}, HARD_CAP={HARD_CAP}")

            # Gün tipi kapasite analizi
            for tip in GUN_TIPLERI:
                ihtiyac = self.tip_slotlari[tip]
                toplam_musait = sum(p.musait_tipler.get(tip, 0) for p in self.personel_listesi if p.id not in kilitli_ids)
                kilitli_tip = sum(self.kilitli_hedefler.get(find_matching_id(pid, self.kilitli_hedefler.keys()) or -1, {}).get(tip, 0) for pid in kilitli_ids)
                kalan_ihtiyac = ihtiyac - kilitli_tip
                debug_info.append(f"  {tip}: ihtiyac={ihtiyac}, kilitli={kilitli_tip}, kalan={kalan_ihtiyac}, musait={toplam_musait}")

            # Kişi bazlı darboğaz analizi
            toplam_upper = 0
            darbogazlar = []
            for p in self.personel_listesi:
                pid = p.id
                if pid in kilitli_ids:
                    toplam_upper += sum(p.hedef_tipler.values())
                    continue
                mk = sum(p.musait_tipler.get(tip, 0) for tip in GUN_TIPLERI)
                matched_k = find_matching_id(pid, kisitli_kapasite.keys())
                if matched_k is not None:
                    mk = min(mk, kisitli_kapasite[matched_k])
                ub = min(mk, HARD_CAP)
                toplam_upper += ub
                if ub < 2:
                    darbogazlar.append(f"{p.ad}: musait={mk}, ub={ub}, mazeret={len(p.mazeret_gunleri)}")

            debug_info.append(f"toplam_upper_bound={toplam_upper} vs toplam_slot={self.toplam_slot}")
            if toplam_upper < self.toplam_slot:
                debug_info.append(f"*** KAPASITE YETERSIZ: {self.toplam_slot - toplam_upper} slot eksik ***")
            if darbogazlar:
                debug_info.append(f"darbogazlar ({len(darbogazlar)}): " + "; ".join(darbogazlar[:10]))

            # === İZOLASYON TESTLERİ ===
            # Her kısıt grubunu tek tek kaldırıp hangisi olmadan çözüm bulunduğunu test et
            izolasyon = []

            # TEST 1: Sadece değişkenler + toplam slot (gün tipi kısıtı YOK)
            m1 = cp.CpModel()
            t1 = {}
            for p in self.personel_listesi:
                pid = p.id
                if pid in kilitli_ids:
                    kv = sum(p.hedef_tipler.values())
                    t1[pid] = m1.NewIntVar(kv, kv, f't1_{pid}')
                else:
                    mk = sum(p.musait_tipler.get(tp, 0) for tp in GUN_TIPLERI)
                    matched_k = find_matching_id(pid, kisitli_kapasite.keys())
                    if matched_k is not None:
                        mk = min(mk, kisitli_kapasite[matched_k])
                    ub = min(mk, HARD_CAP)
                    t1[pid] = m1.NewIntVar(0, ub, f't1_{pid}')
            m1.Add(sum(t1[p.id] for p in self.personel_listesi) == self.toplam_slot)
            s1 = cp.CpSolver()
            s1.parameters.max_time_in_seconds = 5
            st1 = s1.Solve(m1)
            izolasyon.append(f"TEST1_sadece_toplam={'OK' if st1 in [cp.OPTIMAL, cp.FEASIBLE] else 'FAIL'}")

            # TEST 2: Değişkenler + gün tipi kısıtları (toplam slot kısıtı YOK, birlikte YOK)
            m2 = cp.CpModel()
            h2 = {}
            for p in self.personel_listesi:
                pid = p.id
                for tip in GUN_TIPLERI:
                    if pid in kilitli_ids:
                        val = p.hedef_tipler.get(tip, 0)
                        h2[pid, tip] = m2.NewIntVar(val, val, f'h2_{pid}_{tip}')
                    else:
                        h2[pid, tip] = m2.NewIntVar(0, p.musait_tipler.get(tip, 0), f'h2_{pid}_{tip}')
            for tip in GUN_TIPLERI:
                m2.Add(sum(h2[p.id, tip] for p in self.personel_listesi) == self.tip_slotlari[tip])
            s2 = cp.CpSolver()
            s2.parameters.max_time_in_seconds = 5
            st2 = s2.Solve(m2)
            izolasyon.append(f"TEST2_sadece_guntipi={'OK' if st2 in [cp.OPTIMAL, cp.FEASIBLE] else 'FAIL'}")

            # TEST 3: Değişkenler + gün tipi + HARD_CAP (birlikte ve ceza YOK)
            m3 = cp.CpModel()
            h3 = {}
            t3 = {}
            for p in self.personel_listesi:
                pid = p.id
                if pid in kilitli_ids:
                    kv = sum(p.hedef_tipler.values())
                    t3[pid] = m3.NewIntVar(kv, kv, f't3_{pid}')
                    for tip in GUN_TIPLERI:
                        val = p.hedef_tipler.get(tip, 0)
                        h3[pid, tip] = m3.NewIntVar(val, val, f'h3_{pid}_{tip}')
                else:
                    for tip in GUN_TIPLERI:
                        h3[pid, tip] = m3.NewIntVar(0, p.musait_tipler.get(tip, 0), f'h3_{pid}_{tip}')
                    mk = sum(p.musait_tipler.get(tp, 0) for tp in GUN_TIPLERI)
                    matched_k = find_matching_id(pid, kisitli_kapasite.keys())
                    if matched_k is not None:
                        mk = min(mk, kisitli_kapasite[matched_k])
                    ub = min(mk, HARD_CAP)
                    t3[pid] = m3.NewIntVar(0, ub, f't3_{pid}')
                    m3.Add(sum(h3[pid, tip] for tip in GUN_TIPLERI) == t3[pid])
            m3.Add(sum(t3[p.id] for p in self.personel_listesi) == self.toplam_slot)
            for tip in GUN_TIPLERI:
                m3.Add(sum(h3[p.id, tip] for p in self.personel_listesi) == self.tip_slotlari[tip])
            s3 = cp.CpSolver()
            s3.parameters.max_time_in_seconds = 5
            st3 = s3.Solve(m3)
            izolasyon.append(f"TEST3_guntipi+hardcap={'OK' if st3 in [cp.OPTIMAL, cp.FEASIBLE] else 'FAIL'}")

            # TEST 4: TEST3 + excess/missing SOFT kısıtları
            m4 = cp.CpModel()
            h4 = {}
            t4 = {}
            pen4 = []
            for p in self.personel_listesi:
                pid = p.id
                if pid in kilitli_ids:
                    kv = sum(p.hedef_tipler.values())
                    t4[pid] = m4.NewIntVar(kv, kv, f't4_{pid}')
                    for tip in GUN_TIPLERI:
                        val = p.hedef_tipler.get(tip, 0)
                        h4[pid, tip] = m4.NewIntVar(val, val, f'h4_{pid}_{tip}')
                else:
                    for tip in GUN_TIPLERI:
                        h4[pid, tip] = m4.NewIntVar(0, p.musait_tipler.get(tip, 0), f'h4_{pid}_{tip}')
                    mk = sum(p.musait_tipler.get(tp, 0) for tp in GUN_TIPLERI)
                    matched_k = find_matching_id(pid, kisitli_kapasite.keys())
                    if matched_k is not None:
                        mk = min(mk, kisitli_kapasite[matched_k])
                    ub = min(mk, HARD_CAP)
                    t4[pid] = m4.NewIntVar(0, ub, f't4_{pid}')
                    m4.Add(sum(h4[pid, tip] for tip in GUN_TIPLERI) == t4[pid])
                    # Soft excess/missing
                    exc4 = m4.NewIntVar(0, HARD_CAP, f'exc4_{pid}')
                    m4.Add(exc4 >= t4[pid] - (avg_count_floor + 1))
                    exc4sq = m4.NewIntVar(0, HARD_CAP*HARD_CAP, f'exc4sq_{pid}')
                    m4.AddMultiplicationEquality(exc4sq, [exc4, exc4])
                    pen4.append(exc4sq)
            m4.Add(sum(t4[p.id] for p in self.personel_listesi) == self.toplam_slot)
            for tip in GUN_TIPLERI:
                m4.Add(sum(h4[p.id, tip] for p in self.personel_listesi) == self.tip_slotlari[tip])
            m4.Minimize(sum(pen4))
            s4 = cp.CpSolver()
            s4.parameters.max_time_in_seconds = 5
            st4 = s4.Solve(m4)
            izolasyon.append(f"TEST4_+soft_excess={'OK' if st4 in [cp.OPTIMAL, cp.FEASIBLE] else 'FAIL'}")

            # TEST 5: TEST4 + saat dengesi (AddAbsEquality)
            m5 = cp.CpModel()
            h5 = {}
            t5 = {}
            pen5 = []
            for p in self.personel_listesi:
                pid = p.id
                if pid in kilitli_ids:
                    kv = sum(p.hedef_tipler.values())
                    t5[pid] = m5.NewIntVar(kv, kv, f't5_{pid}')
                    for tip in GUN_TIPLERI:
                        val = p.hedef_tipler.get(tip, 0)
                        h5[pid, tip] = m5.NewIntVar(val, val, f'h5_{pid}_{tip}')
                else:
                    for tip in GUN_TIPLERI:
                        h5[pid, tip] = m5.NewIntVar(0, p.musait_tipler.get(tip, 0), f'h5_{pid}_{tip}')
                    mk = sum(p.musait_tipler.get(tp, 0) for tp in GUN_TIPLERI)
                    matched_k = find_matching_id(pid, kisitli_kapasite.keys())
                    if matched_k is not None:
                        mk = min(mk, kisitli_kapasite[matched_k])
                    ub = min(mk, HARD_CAP)
                    t5[pid] = m5.NewIntVar(0, ub, f't5_{pid}')
                    m5.Add(sum(h5[pid, tip] for tip in GUN_TIPLERI) == t5[pid])
                    # Saat dengesi
                    th5 = sum(h5[pid, tip] * self.saat[tip] for tip in GUN_TIPLERI)
                    hd5 = m5.NewIntVar(0, 200, f'hd5_{pid}')
                    m5.AddAbsEquality(hd5, th5 - avg_hours)
                    pen5.append(hd5)
            m5.Add(sum(t5[p.id] for p in self.personel_listesi) == self.toplam_slot)
            for tip in GUN_TIPLERI:
                m5.Add(sum(h5[p.id, tip] for p in self.personel_listesi) == self.tip_slotlari[tip])
            m5.Minimize(sum(pen5))
            s5 = cp.CpSolver()
            s5.parameters.max_time_in_seconds = 5
            st5 = s5.Solve(m5)
            izolasyon.append(f"TEST5_+saat_dengesi={'OK' if st5 in [cp.OPTIMAL, cp.FEASIBLE] else 'FAIL'}")

            # TEST 6: TEST5 + WE dengesi (AddAbsEquality)
            m6 = cp.CpModel()
            h6 = {}
            t6 = {}
            pen6 = []
            for p in self.personel_listesi:
                pid = p.id
                if pid in kilitli_ids:
                    kv = sum(p.hedef_tipler.values())
                    t6[pid] = m6.NewIntVar(kv, kv, f't6_{pid}')
                    for tip in GUN_TIPLERI:
                        val = p.hedef_tipler.get(tip, 0)
                        h6[pid, tip] = m6.NewIntVar(val, val, f'h6_{pid}_{tip}')
                else:
                    for tip in GUN_TIPLERI:
                        h6[pid, tip] = m6.NewIntVar(0, p.musait_tipler.get(tip, 0), f'h6_{pid}_{tip}')
                    mk = sum(p.musait_tipler.get(tp, 0) for tp in GUN_TIPLERI)
                    matched_k = find_matching_id(pid, kisitli_kapasite.keys())
                    if matched_k is not None:
                        mk = min(mk, kisitli_kapasite[matched_k])
                    ub = min(mk, HARD_CAP)
                    t6[pid] = m6.NewIntVar(0, ub, f't6_{pid}')
                    m6.Add(sum(h6[pid, tip] for tip in GUN_TIPLERI) == t6[pid])
                    th6 = sum(h6[pid, tip] * self.saat[tip] for tip in GUN_TIPLERI)
                    hd6 = m6.NewIntVar(0, 200, f'hd6_{pid}')
                    m6.AddAbsEquality(hd6, th6 - avg_hours)
                    pen6.append(hd6)
                    # WE dengesi
                    we6 = sum(h6[pid, tip] for tip in we_tipleri)
                    wed6 = m6.NewIntVar(0, 5000, f'wed6_{pid}')
                    m6.AddAbsEquality(wed6, we6 * self.toplam_slot - t6[pid] * total_we_slots)
                    pen6.append(wed6)
            m6.Add(sum(t6[p.id] for p in self.personel_listesi) == self.toplam_slot)
            for tip in GUN_TIPLERI:
                m6.Add(sum(h6[p.id, tip] for p in self.personel_listesi) == self.tip_slotlari[tip])
            m6.Minimize(sum(pen6))
            s6 = cp.CpSolver()
            s6.parameters.max_time_in_seconds = 5
            st6 = s6.Solve(m6)
            izolasyon.append(f"TEST6_+we_dengesi={'OK' if st6 in [cp.OPTIMAL, cp.FEASIBLE] else 'FAIL'}")

            # Kişi bazlı h domain analizi (her kişinin h üst sınırları toplamı vs HARD_CAP)
            kisi_debug = []
            for p in self.personel_listesi:
                pid = p.id
                if pid in kilitli_ids:
                    continue
                h_sum_ub = sum(p.musait_tipler.get(tip, 0) for tip in GUN_TIPLERI)
                mk = h_sum_ub
                matched_k = find_matching_id(pid, kisitli_kapasite.keys())
                if matched_k is not None:
                    mk = min(mk, kisitli_kapasite[matched_k])
                ub = min(mk, HARD_CAP)
                # h değişkenlerinin toplam alt sınırı vs üst sınırı
                h_lb_total = sum(manuel_sayac[pid].get(tip, 0) for tip in GUN_TIPLERI)
                kisi_debug.append(f"{p.ad}:ub={ub},h_ub={h_sum_ub},lb={h_lb_total},mzrt={len(p.mazeret_gunleri)},musait={dict(p.musait_tipler)}")

            debug_info.append(f"IZOLASYON: {' | '.join(izolasyon)}")
            # İlk 15 kişiyi göster
            debug_info.append(f"KISI_DETAY({len(kisi_debug)}): " + " ; ".join(kisi_debug[:15]))

            if darbogazlar:
                debug_info.append(f"darbogazlar ({len(darbogazlar)}): " + "; ".join(darbogazlar[:10]))

            debug_msg = " | ".join(debug_info)
            return HedefSonuc(False, [], [], {}, {}, f"Hedef CP-SAT çözümsüz: {debug_msg}")

        # --- 7. SONUÇLARI PERSONELLERE YAZ ---
        for p in self.personel_listesi:
            pid = p.id
            for tip in GUN_TIPLERI:
                p.hedef_tipler[tip] = int(solver.Value(h[pid, tip]))

        # Kısıtlı kişiler için taşma kota dağıtımı
        # (hedef_tipler OR-Tools'tan geldikten sonra çalışmalı)
        self._hesapla_kisitli_kisi_gorev_kotalari()

        hedefler = []
        birlikte_bilgi = []  # Birlikte grupları hakkında bilgi

        we_tipleri = ['cum', 'cmt', 'pzr']
        wd_tipleri = ['hici', 'prs']

        for p in self.personel_listesi:
            toplam = sum(p.hedef_tipler.values())
            saat = sum(p.hedef_tipler[tip] * self.saat[tip] for tip in GUN_TIPLERI)
            we_val = sum(p.hedef_tipler.get(tip, 0) for tip in we_tipleri)
            wd_val = sum(p.hedef_tipler.get(tip, 0) for tip in wd_tipleri)
            hedefler.append({
                'id': p.id, 'ad': p.ad,
                'hedef_hici': p.hedef_tipler.get('hici', 0),
                'hedef_prs': p.hedef_tipler.get('prs', 0),
                'hedef_cum': p.hedef_tipler.get('cum', 0),
                'hedef_cmt': p.hedef_tipler.get('cmt', 0),
                'hedef_pzr': p.hedef_tipler.get('pzr', 0),
                'hedef_toplam': toplam, 'saat': saat,
                'hedef_we': we_val, 'hedef_wd': wd_val,
                'gorev_kotalari': p.gorev_kotalari.copy() if p.gorev_kotalari else {},
                'hedef_tipler': p.hedef_tipler.copy(),
            })

        # Birlikte grupları bilgisi
        for kural in self.birlikte_kurallar:
            if kural.tur == 'birlikte':
                grup_adlar = []
                gecerli_pids = []
                for pid in kural.kisiler:
                    matched_id = find_matching_id(pid, self.personeller.keys())
                    if matched_id is not None:
                        p = self.personeller[matched_id]
                        grup_adlar.append(p.ad)
                        gecerli_pids.append(p.id)
                if len(grup_adlar) >= 2:
                    ortak = self._birlikte_ortak_musait_tipler(gecerli_pids)
                    esdeger_aile_toplamlari = {}
                    for pid in gecerli_pids:
                        p = self.personeller.get(pid)
                        if not p:
                            continue
                        esdeger_aile_toplamlari[p.ad] = sum(
                            kota for gorev, kota in (p.gorev_kotalari or {}).items()
                            if birlikte_aile_anahtari(gorev) == BIRLIKTE_ESDEGER_GOREV_AILE_ADI
                        )
                    birlikte_bilgi.append({
                        'kisiler': grup_adlar,
                        'ortak_kapasite': ortak,
                        'esdeger_gorevler': ['AMELİYATHANE', 'MAVİ KOD', 'KVC'],
                        'esdeger_aile_toplamlari': esdeger_aile_toplamlari,
                    })

        gorev_kotalari = self._hesapla_gorev_kotalari()

        # Görev kısıtlama bilgilerini hazırla
        kisitlama_bilgi = []
        for pid, kisit_bilgi in self.gorev_kisitlamalari.items():
            if isinstance(kisit_bilgi, dict):
                ana_gorev = kisit_bilgi.get("gorevAdi", "")
                tasma = kisit_bilgi.get("tasmaGorevi")
            else:
                ana_gorev = kisit_bilgi
                tasma = None
            matched_id = find_matching_id(pid, self.personeller.keys())
            if matched_id is not None:
                p = self.personeller[matched_id]
                kisitlama_bilgi.append({
                    'personel_id': pid,
                    'personel_ad': p.ad,
                    'gorev_adi': ana_gorev,
                    'tasma_gorevi': tasma
                })

        istatistikler = {
            'toplam_slot': self.toplam_slot,
            'toplam_hedef': sum(h['hedef_toplam'] for h in hedefler),
            'tip_slotlari': self.tip_slotlari,
            'personel_sayisi': n,
            'birlikte_gruplar': birlikte_bilgi,
            'birlikte_debug': birlikte_debug,
            'birlikte_kural_sayisi': len(self.birlikte_kurallar),
            'gorev_kisitlamalari': kisitlama_bilgi,
            'kisitli_kapasite': {str(k): v for k, v in kisitli_kapasite.items()}
        }
        return HedefSonuc(True, hedefler, [], gorev_kotalari, istatistikler, "Hedefler hesaplandi")

    def _hesapla_gorev_kotalari(self) -> Dict:
        kotalari = {}
        for g in self.gorevler:
            gorev_adi = g.base_name if g.base_name else g.ad
            if gorev_adi not in kotalari:
                kotalari[gorev_adi] = {
                    'toplam': self.gun_sayisi,
                    'tip_dagilimi': {t: self.tip_sayilari[t] for t in GUN_TIPLERI}
                }
        return kotalari

    def _hesapla_kisitli_kisi_gorev_kotalari(self) -> None:
        """
        Kısıtlı kişiler için kişi bazında gorev_kotalari üret.

        Senaryo: KVC'ye 9 kişi kısıtlı, KVC kapasitesi 30 slot.
        9 × hedef = 36 talep → 6 nöbet taşar → taşma görevine (MAVİ KOD) yaz.

        Her kişi için:
          ana_gorev_kota  = floor(ana_gorev_kapasitesi / kisitli_kisi_sayisi)
          tasma_kota      = hedef_toplam - ana_gorev_kota  (>0 ise taşma görevine)
        """
        # gorev → kısıtlı kişi listesi ve taşma görevi
        gorev_kisitli: Dict[str, list] = {}   # gorev_adi → [pid, ...]
        gorev_tasma: Dict[str, str] = {}       # gorev_adi → tasma_gorev_adi

        for pid, kisit_bilgi in self.gorev_kisitlamalari.items():
            if isinstance(kisit_bilgi, dict):
                ana_gorev = kisit_bilgi.get("gorevAdi", "")
                tasma = kisit_bilgi.get("tasmaGorevi")
            else:
                ana_gorev = str(kisit_bilgi)
                tasma = None
            if not ana_gorev:
                continue
            matched_pid = find_matching_id(pid, {p.id: p for p in self.personel_listesi})
            if matched_pid is None:
                continue
            gorev_kisitli.setdefault(ana_gorev, []).append(matched_pid)
            if tasma:
                gorev_tasma[ana_gorev] = tasma

        for ana_gorev, pids in gorev_kisitli.items():
            # Ana görevin slot sayısı (bir günde kaç slot)
            ana_slot_gunluk = sum(
                1 for g in self.gorevler
                if g.base_name == ana_gorev or g.ad == ana_gorev
            )
            if ana_slot_gunluk == 0:
                continue

            # 30 günlük toplam kapasite
            ana_kapasite = ana_slot_gunluk * self.gun_sayisi

            # Taşma görevi slot sayısı
            tasma_gorev = gorev_tasma.get(ana_gorev)
            tasma_slot_gunluk = 0
            if tasma_gorev:
                tasma_slot_gunluk = sum(
                    1 for g in self.gorevler
                    if g.base_name == tasma_gorev or g.ad == tasma_gorev
                )

            # Her kısıtlı kişinin hedef toplamını topla
            toplam_talep = 0
            kisi_hedefler: Dict[int, int] = {}
            for pid in pids:
                p = self.personeller.get(pid)
                if p is None:
                    continue
                hedef = sum(p.hedef_tipler.values()) if p.hedef_tipler else 0
                kisi_hedefler[pid] = hedef
                toplam_talep += hedef

            if toplam_talep == 0:
                continue

            # Kapasite aşımı var mı?
            tasma_toplam = max(0, toplam_talep - ana_kapasite)

            for pid in pids:
                p = self.personeller.get(pid)
                if p is None:
                    continue
                hedef = kisi_hedefler.get(pid, 0)
                if hedef == 0:
                    continue

                if tasma_toplam > 0 and tasma_gorev:
                    # Bu kişinin taşma payı: hedefine orantılı
                    kisi_tasma = round(tasma_toplam * hedef / toplam_talep)
                    kisi_tasma = max(0, min(kisi_tasma, hedef - 1))  # En az 1 ana görev kalır
                    ana_kota = hedef - kisi_tasma
                else:
                    kisi_tasma = 0
                    ana_kota = hedef

                # Taşma görevine de kapasite kontrolü: günlük slot × gün sayısı
                if tasma_gorev and tasma_slot_gunluk > 0:
                    maks_tasma_kapasite = tasma_slot_gunluk * self.gun_sayisi
                    kisi_tasma = min(kisi_tasma, maks_tasma_kapasite)
                    ana_kota = hedef - kisi_tasma

                if not hasattr(p, 'gorev_kotalari') or p.gorev_kotalari is None:
                    p.gorev_kotalari = {}

                # Sadece gerçek taşma varsa kotaları yaz
                # Taşma yoksa (tasma_toplam==0) mevcut kotaları bozma
                if tasma_toplam > 0 and tasma_gorev:
                    p.gorev_kotalari[ana_gorev] = ana_kota
                    if kisi_tasma > 0:
                        p.gorev_kotalari[tasma_gorev] = (
                            p.gorev_kotalari.get(tasma_gorev, 0) + kisi_tasma
                        )
