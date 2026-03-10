"""
Hedef Hesaplayıcı — OR-Tools CP-SAT ile nöbet hedeflerini dengeli dağıtır.
Üçlü dengeleme: Sayı dengesi, Saat dengesi, WE/WD dengesi.
"""

from typing import List, Dict

from utils import (
    GUN_TIPLERI, SAAT_DEGERLERI,
    find_matching_id,
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
                # Minimum hedef = min(mevcut hedefler) ama ortak kapasiteyi aşmasın
                min_hedef[t] = min(min(tip_hedefler), ortak_musait[t])

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

        # --- 1. HEDEF VE ORTALAMA ANALİZİ ---

        # A) SAYI ORTALAMASI
        avg_count_float = self.toplam_slot / n
        avg_count_floor = int(avg_count_float)
        HARD_CAP = avg_count_floor + 2  # Kesin üst sınır

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

            # Toplam hedef (HARD_CAP sınırlı)
            upper_bound = min(max_kapasite, HARD_CAP)
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
            excess = model.NewIntVar(0, 5, f'excess_{pid}')
            model.Add(t[pid] <= target_limit + excess)

            # Karesel ceza (makas kontrolü) - ÇOK YÜKSEK
            excess_sq = model.NewIntVar(0, 25, f'excess_sq_{pid}')
            model.AddMultiplicationEquality(excess_sq, [excess, excess])
            penalties.append(excess_sq * 100000)

            # Alt sınır kontrolü (aşağı makas açılmasın)
            if not cok_mazeretli:
                missing = model.NewIntVar(0, 5, f'missing_{pid}')
                min_hedef = max(0, avg_count_floor - 1)
                model.Add(t[pid] >= min_hedef - missing)
                missing_sq = model.NewIntVar(0, 25, f'missing_sq_{pid}')
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
            penalties.append(we_balance_diff * 10)

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

            # All-pairs: tüm çiftleri karşılaştır (yıldız yerine)
            for i in range(len(grup)):
                for j in range(i + 1, len(grup)):
                    p1_id, p2_id = grup[i], grup[j]
                    diff = model.NewIntVar(-5, 5, f'birlikte_diff_{p1_id}_{p2_id}')
                    model.Add(t[p1_id] - t[p2_id] == diff)
                    abs_diff = model.NewIntVar(0, 5, f'abs_birlikte_{p1_id}_{p2_id}')
                    model.AddAbsEquality(abs_diff, diff)
                    penalties.append(abs_diff * 500)

        # --- 6. ÇÖZÜM ---
        model.Minimize(sum(penalties))

        solver = cp.CpSolver()
        solver.parameters.max_time_in_seconds = 10
        solver.parameters.num_search_workers = 4
        status = solver.Solve(model)

        if status not in [cp.OPTIMAL, cp.FEASIBLE]:
            return HedefSonuc(False, [], [], {}, {}, "Hedef CP-SAT çözümsüz - kapasite yetersiz olabilir")

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
                    birlikte_bilgi.append({
                        'kisiler': grup_adlar,
                        'ortak_kapasite': ortak
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
