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
from hedef_teshis import build_hedef_infeasible_debug

# Lazy import for ortools (Firebase deploy timeout fix) — thread-safe
import threading

_cp_model_lock = threading.Lock()
_cp_model_module = None

ADALET_KATSAYI_OLCEGI = 100
ADALET_KATSAYI_UST_SINIR = 10.0
ADALET_GECMIS_SAYI_UST_SINIR = 10_000

ADALET_AYLIK_AGIRLIKLARI = {
    'toplam': 80,
    'saat': 2,
    'we': 40,
    'wd': 40,
    'hici': 35,
    'prs': 35,
    'cum': 35,
    'cmt': 35,
    'pzr': 35,
}

ADALET_TARIHSEL_AGIRLIKLARI = {
    'toplam': 800,
    'saat': 20,
    'we': 400,
    'wd': 400,
    'hici': 350,
    'prs': 350,
    'cum': 350,
    'cmt': 350,
    'pzr': 350,
}

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
                 kilitli_hedefler: Dict[int, Dict[str, int]] = None,
                 gorev_havuzlari: Dict[str, set] = None):
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
        self.gorev_havuzlari = {
            str(rol): set(ids)
            for rol, ids in (gorev_havuzlari or {}).items()
        }

        # Rol yapıları (kişi-gün-görev birleşik kapasite katmanı için).
        # Rol = slot'un base_name'i (yoksa adı); final solver ile aynı tanım.
        self.rol_slot_sayilari: Dict[str, int] = {}
        self.rol_slot_adlari: Dict[str, set] = {}
        self.exclusive_roller: set = set()
        for g in self.gorevler:
            rol = self._rol_adi(g)
            self.rol_slot_sayilari[rol] = self.rol_slot_sayilari.get(rol, 0) + 1
            self.rol_slot_adlari.setdefault(rol, set()).add(g.ad)
            if g.exclusive or bool(getattr(g, 'kritik', False)):
                self.exclusive_roller.add(rol)

        self.tip_sayilari = {t: 0 for t in GUN_TIPLERI}
        for g, tip in gun_tipleri.items():
            if tip in self.tip_sayilari:
                self.tip_sayilari[tip] += 1
        self.tip_slotlari = {t: self.tip_sayilari[t] * self.slot_sayisi for t in GUN_TIPLERI}
        self.toplam_slot = sum(self.tip_slotlari.values())
        self._hesapla_kapasiteler()

    @staticmethod
    def _rol_adi(gorev: SolverGorev) -> str:
        """Slot'un rol adı = base_name (yoksa ad). Final solver ile aynı tanım."""
        return gorev.base_name if gorev.base_name else gorev.ad

    def _havuz_coz(self) -> Dict[str, set]:
        """``gorev_havuzlari``yı gerçek (normalize) personel id kümelerine çözer.

        Yalnız tanımlı rollere (mevcut slotlara karşılık gelen) ait havuzlar
        döner; id normalizasyonuna dayanıklıdır (``find_matching_id``).
        Dönen: ``{rol: {normalize_id, ...}}``. Havuz yoksa boş sözlük.
        """
        tum_roller = set(self.rol_slot_sayilari.keys())
        havuz: Dict[str, set] = {}
        for rol, uyeler in self.gorev_havuzlari.items():
            if rol not in tum_roller:
                continue
            cozulmus = {
                find_matching_id(pid, self.personeller.keys()) for pid in uyeler
            }
            havuz[rol] = {mid for mid in cozulmus if mid is not None}
        return havuz

    def _havuz_arz_kapasiteleri(self) -> Dict[str, tuple]:
        """Kişi-gün-görev birleşik kapasite katmanı.

        Yalnız TEK role çalışabilen (confined) kişilerin GÜN TİPİ bazındaki
        toplam hedefi, o rolün ilgili gün tipindeki fiziksel slot arzını
        (slot_sayısı × o tipteki gün sayısı) aşamaz. Aksi halde hedef modeli,
        çizelge modelinin dolduramayacağı görev hedefleri üretir → boş slot.
        Gün tipi sınırları toplam sınırı da kapsar (örn. "4 uygun cuma var ama
        sadece 2'sinde AMATEM boş" durumunu kökten çözer).

        Yetki kaynağı otoriter ``gorev_havuzlari``dır: bir rol havuzda listeliyse
        yalnız üyeleri o rolü yapabilir; havuzda olmayan (açık) rol herkese açıktır.
        Açık rol varsa hiç kimse confined olmaz → kısıt üretilmez. Üretilen sınır
        güvenli bir üst sınırdır: geçerli hiçbir çözümü elemez (yalnız fiziksel
        olarak imkânsız hedef dağılımlarını keser).

        Dönen: ``{rol: (confined_personel_id_kümesi, {gün_tipi: arz_ust_siniri})}``.
        ``gorev_havuzlari`` verilmemişse boş sözlük (davranış değişmez).
        """
        if not self.gorev_havuzlari:
            return {}

        tum_roller = set(self.rol_slot_sayilari.keys())
        havuz = self._havuz_coz()

        def _calisabilir_roller(pid) -> set:
            # Havuzda olmayan rol herkese açık; havuzdaki rol yalnız üyelerine.
            return {
                rol for rol in tum_roller
                if rol not in havuz or pid in havuz[rol]
            }

        sonuc: Dict[str, tuple] = {}
        for rol in havuz:
            confined = {
                pid for pid in self.personeller
                if _calisabilir_roller(pid) == {rol}
            }
            if not confined:
                continue
            slot_sayisi = self.rol_slot_sayilari.get(rol, 0)
            arz_tipleri = {
                tip: slot_sayisi * self.tip_sayilari.get(tip, 0)
                for tip in GUN_TIPLERI
            }
            sonuc[rol] = (confined, arz_tipleri)
        return sonuc

    def _manuel_rol_kenarlari(self) -> Dict[int, set]:
        """Manuel atamaların hangi rollere yapıldığını çıkarır.

        Transport modelinde uygunluk kenarı olarak kullanılır: kullanıcı bir
        kişiyi havuz dışı bir role manuel atadıysa (bilinçli override), bu
        kenar eklenerek transport modelinin o atamayı yanlışlıkla infeasible
        yapması önlenir. Kenar eklemek yalnızca KAPASITE ekler → hiçbir geçerli
        çözümü elemez. Rol, önce ``slot_idx`` ile, olmazsa görev adıyla eşlenir.
        Dönen: ``{personel_id: {rol, ...}}``.
        """
        slot_rol = {g.slot_idx: self._rol_adi(g) for g in self.gorevler}
        kenarlar: Dict[int, set] = {}
        for atama in self.manuel_atamalar:
            pid = find_matching_id(atama.personel_id, self.personeller.keys())
            if pid is None:
                continue
            rol = slot_rol.get(getattr(atama, 'slot_idx', None))
            if rol is None and getattr(atama, 'gorev_adi', ''):
                for g in self.gorevler:
                    if atama.gorev_adi in (g.ad, g.base_name):
                        rol = self._rol_adi(g)
                        break
            if rol is not None:
                kenarlar.setdefault(pid, set()).add(rol)
        return kenarlar

    def _rol_transport_kisitlari_ekle(self, model, h) -> None:
        """Kişi-gün-görev BİRLEŞİK count-seviyesi transport fizibilitesi.

        ``h[pid,tip]`` hedeflerinin gerçek rollere (görev tiplerine)
        dağıtılabilir olmasını garanti eder. Kısmi-rol (ör. 3 rolden 2'sini
        yapabilen) kişilerde, confined-tekil üst sınırın KAÇIRDIĞI Hall-tipi
        çapraz uygunluğu yakalar: bir rolü yalnız az sayıda/kısıtlı-müsait kişi
        yapabiliyorsa ve o rolün talebi count seviyesinde karşılanamıyorsa model
        INFEASIBLE olur — çizelge modelinin dolduramayacağı hedef üretip boş
        slot bırakmak yerine kökten engeller.

        Transport değişkeni ``x[pid,tip,rol]``:
          * ``Σ_rol x[pid,tip,rol] == h[pid,tip]`` (kişinin tipteki yükü rollere dağılır)
          * ``Σ_pid x[pid,tip,rol] == rol_slot_sayısı[rol] × o_tipteki_gün_sayısı``
          * ``x[pid,tip,rol]`` yalnız pid'in yapabildiği (havuz) VEYA manuel
            atandığı roller için > 0 olabilir.

        Bu, transportasyon politopu (tamamen unimodüler) olduğundan count
        seviyesinde tam Gale–Hoffman fizibilitesidir: güvenli bir yapıdır,
        gerçekten fizibil hiçbir hedef dağılımını elemez. ``gorev_havuzlari``
        yoksa tüm roller açık → transport trivial → hiç kurulmaz (davranış
        değişmez, ek değişken üretilmez).
        """
        if not self.gorev_havuzlari:
            return

        havuz = self._havuz_coz()
        manuel_kenarlar = self._manuel_rol_kenarlari()
        tum_roller = sorted(self.rol_slot_sayilari.keys())
        pids = [p.id for p in self.personel_listesi]

        def _uygun_roller(pid) -> set:
            roller = {
                rol for rol in tum_roller
                if rol not in havuz or pid in havuz[rol]
            }
            roller |= manuel_kenarlar.get(pid, set())
            return roller

        for tip in GUN_TIPLERI:
            tipteki_gun = self.tip_sayilari.get(tip, 0)
            if tipteki_gun == 0:
                continue

            x = {}
            for pid in pids:
                uygun = _uygun_roller(pid)
                for rol in uygun:
                    x[pid, rol] = model.NewIntVar(0, tipteki_gun, f'x_{pid}_{tip}_{rol}')
                # Kişinin bu tipteki yükü yapabildiği rollere dağılmalı.
                # Uygun rolü yoksa toplam 0 → h[pid,tip] == 0 zorlanır.
                model.Add(h[pid, tip] == sum(x[pid, rol] for rol in uygun))

            # Her rolün bu tipteki talebi tam karşılanmalı.
            for rol in tum_roller:
                talep = self.rol_slot_sayilari[rol] * tipteki_gun
                katilanlar = [x[pid, rol] for pid in pids if (pid, rol) in x]
                model.Add(sum(katilanlar) == talep)

    # Detay adalet geçişi taban/tavan süre bütçesi (saniye).
    DETAY_SURE_TABAN = 7
    DETAY_SURE_TAVAN = 30

    def _detay_cozum_sure_saniye(self) -> int:
        """Detay adalet geçişi için örnek boyutuna göre ADAPTİF süre bütçesi (sn).

        Küçük örnekler tavandan çok önce OPTIMAL'e ulaşıp erken durar → tavan
        onları etkilemez (mevcut çıktılar/testler değişmez). Büyük örnekler
        (ör. 50×31×6) sabit 7 sn'de FEASIBLE'da takılıp adaleti optimuma
        taşıyamıyordu; kişi×gün ölçeğiyle büyüyen tavan onlara daha fazla süre
        tanır. Fonksiyon timeout'ları (nobet_hedef_hesapla=300s, nobet_coz=540s)
        bol headroom sağlar; üst sınır (``DETAY_SURE_TAVAN``) latency'i korur.

        Güvenli: tavanı yükseltmek yalnız ÜST sınırı büyütür — CP-SAT OPTIMAL'i
        kanıtlayınca erken durduğundan hızlı çözülen örneklerin sonucu aynı kalır.
        """
        yuk = len(self.personel_listesi) * max(1, self.gun_sayisi)
        # Taban 7 sn; ~150 kişi-gün başına +1 sn; DETAY_SURE_TAVAN ile sınırlı.
        return int(min(self.DETAY_SURE_TAVAN, max(self.DETAY_SURE_TABAN, yuk // 150)))

    def _hesapla_kapasiteler(self):
        manuel_mazeret_onayli_gunler = {}
        for atama in self.manuel_atamalar:
            if not getattr(atama, 'mazeret_onayli', False):
                continue
            matched_id = find_matching_id(atama.personel_id, self.personeller.keys())
            if matched_id is not None:
                manuel_mazeret_onayli_gunler.setdefault(matched_id, set()).add(atama.gun)

        for p in self.personel_listesi:
            p.musait_tipler = {t: 0 for t in GUN_TIPLERI}
            p.musait_gunler = set()
            onayli_gunler = manuel_mazeret_onayli_gunler.get(p.id, set())
            for g, tip in self.gun_tipleri.items():
                if g not in p.mazeret_gunleri or g in onayli_gunler:
                    p.musait_tipler[tip] += 1
                    p.musait_gunler.add(g)

    def _birlikte_ortak_musait_tipler(self, grup_ids: List) -> Dict[str, int]:
        """Birlikte grubundaki kişilerin ortak müsait gün tiplerini hesapla"""
        ortak_gunler = None
        for pid in grup_ids:
            p = self.personeller.get(pid)
            if p:
                personel_gunleri = set(p.musait_gunler)
                ortak_gunler = (
                    personel_gunleri
                    if ortak_gunler is None
                    else ortak_gunler & personel_gunleri
                )

        ortak = {t: 0 for t in GUN_TIPLERI}
        for gun in ortak_gunler or set():
            tip = self.gun_tipleri.get(gun)
            if tip in ortak:
                ortak[tip] += 1
        return ortak

    @staticmethod
    def _guvenli_int(value, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return default

    @staticmethod
    def _gecmis_kullanilabilir_mi(personel: SolverPersonel) -> bool:
        durum = str(getattr(personel, 'gecmis_veri_durumu', 'bilinmiyor') or 'bilinmiyor').strip().lower()
        if durum not in {'tam', 'kismi'}:
            return False
        if bool(getattr(personel, 'esitlemeden_muaf', False)):
            return False
        return bool(getattr(personel, 'yillik_gerceklesen', None))

    def _adalet_katsayisi(self, personel: SolverPersonel, uyarilar: List[str]) -> tuple:
        raw = getattr(personel, 'is_yuku_katsayisi', 1.0)
        try:
            katsayi = float(raw)
        except (TypeError, ValueError, OverflowError):
            katsayi = 1.0
            uyarilar.append(f"{personel.ad}: geçersiz iş yükü katsayısı 1.0 kabul edildi")

        if not math.isfinite(katsayi):
            katsayi = 1.0
            uyarilar.append(f"{personel.ad}: sonlu olmayan iş yükü katsayısı 1.0 kabul edildi")
        katsayi = max(0.0, katsayi)
        if katsayi > ADALET_KATSAYI_UST_SINIR:
            uyarilar.append(
                f"{personel.ad}: iş yükü katsayısı {katsayi:g} → {ADALET_KATSAYI_UST_SINIR:g} sınırlandı"
            )
            katsayi = ADALET_KATSAYI_UST_SINIR

        agirlik = int(round(katsayi * ADALET_KATSAYI_OLCEGI))
        if katsayi > 0 and agirlik == 0:
            agirlik = 1
        return katsayi, agirlik

    def _gecmis_metrikleri(self, personel: SolverPersonel, uyarilar: List[str]) -> Dict[str, int]:
        raw = getattr(personel, 'yillik_gerceklesen', {}) or {}
        tipler = {}
        for tip in GUN_TIPLERI:
            deger = max(0, self._guvenli_int(raw.get(tip, 0), 0))
            if deger > ADALET_GECMIS_SAYI_UST_SINIR:
                uyarilar.append(
                    f"{personel.ad}: geçmiş {tip} değeri {deger} → {ADALET_GECMIS_SAYI_UST_SINIR} sınırlandı"
                )
                deger = ADALET_GECMIS_SAYI_UST_SINIR
            tipler[tip] = deger

        toplam = sum(tipler.values())
        we = sum(tipler.get(tip, 0) for tip in ['cum', 'cmt', 'pzr'])
        wd = sum(tipler.get(tip, 0) for tip in ['hici', 'prs'])
        saat = sum(tipler[tip] * self.saat[tip] for tip in GUN_TIPLERI)
        return {
            **tipler,
            'toplam': toplam,
            'saat': saat,
            'we': we,
            'wd': wd,
        }

    def _max_assignable_with_ara_gun(self, gunler, zorunlu_gunler=None) -> int:
        uygun_gunler = sorted({int(gun) for gun in gunler})
        zorunlu = sorted({int(gun) for gun in (zorunlu_gunler or [])})
        if self.ara_gun <= 0:
            return len(uygun_gunler)

        for idx in range(1, len(zorunlu)):
            if zorunlu[idx] - zorunlu[idx - 1] <= self.ara_gun:
                return -1

        adaylar = [
            gun for gun in uygun_gunler
            if gun not in zorunlu
            and all(abs(gun - sabit_gun) > self.ara_gun for sabit_gun in zorunlu)
        ]
        secilen = list(zorunlu)
        for gun in adaylar:
            if all(abs(gun - mevcut) > self.ara_gun for mevcut in secilen):
                secilen.append(gun)
        return len(secilen)

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
        HARD_CAP = self.gun_sayisi

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
        manuel_gunler = {p.id: [] for p in self.personel_listesi}
        manuel_atama_map = {p.id: [] for p in self.personel_listesi}
        for m in self.manuel_atamalar:
            if m.personel_id is None:
                continue
            tip = self.gun_tipleri.get(m.gun, 'hici')
            matched_id = find_matching_id(m.personel_id, manuel_sayac.keys())
            if matched_id is not None:
                manuel_sayac[matched_id][tip] += 1
                manuel_gunler[matched_id].append(m.gun)
                manuel_atama_map[matched_id].append(m)

        adalet_uyarilari = []
        katsayi_map = {}
        agirlik_map = {}
        gecmis_metrikleri = {}
        for p in self.personel_listesi:
            katsayi, agirlik = self._adalet_katsayisi(p, adalet_uyarilari)
            katsayi_map[p.id] = katsayi
            agirlik_map[p.id] = agirlik
            gecmis_metrikleri[p.id] = self._gecmis_metrikleri(p, adalet_uyarilari)

        # Başlangıç hedefleri + kilitli hedef uygulaması
        kilitli_ids = set()
        kilitli_toplam_slot = 0  # Kilitli kişilerin kapladığı toplam slot
        for p in self.personel_listesi:
            pid = p.id
            matched_kilitli = find_matching_id(pid, self.kilitli_hedefler.keys())
            if matched_kilitli is not None:
                # Kilitli kişi: hedefi frontend'den gelen sabit değere ayarla
                kilitli = self.kilitli_hedefler[matched_kilitli]
                p.hedef_tipler = {
                    tip: self._guvenli_int(kilitli.get(tip, 0), 0)
                    for tip in GUN_TIPLERI
                }
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

        # --- 2. OR-TOOLS MODELİ ---
        cp = _get_cp_model()
        model = cp.CpModel()

        h = {}  # h[pid, tip]: Kişinin o tipteki nöbet sayısı
        t = {}  # t[pid]: Kişinin toplam nöbet sayısı
        total_h_hours = {}  # Kişinin toplam saati
        total_h_we = {}     # Kişinin toplam WE sayısı
        total_h_wd = {}     # Kişinin toplam WD sayısı

        penalties = []
        oncelikli_penalties = []
        birlikte_debug = []
        personel_sinirlar = {}

        for p in self.personel_listesi:
            pid = p.id
            is_kilitli = pid in kilitli_ids

            # Kişinin kapasitesi
            max_kapasite = sum(p.musait_tipler.get(tip, 0) for tip in GUN_TIPLERI)
            tekil_manuel_gunler = set(manuel_gunler[pid])
            if len(tekil_manuel_gunler) != len(manuel_gunler[pid]):
                return HedefSonuc(
                    False, [], [], {},
                    {'adalet': {'uyarilar': adalet_uyarilari}},
                    f"Aynı kişiye aynı gün birden fazla manuel görev atanmış: {p.ad}"
                )
            for atama in manuel_atama_map[pid]:
                if atama.gun not in p.mazeret_gunleri:
                    continue
                if not getattr(atama, 'mazeret_onayli', False):
                    return HedefSonuc(
                        False, [], [], {},
                        {'adalet': {'uyarilar': adalet_uyarilari}},
                        f"Manuel atama mazeretli günde ve onaysız: {p.ad} / gün={atama.gun}"
                    )

            ara_gun_kapasitesi = self._max_assignable_with_ara_gun(
                p.musait_gunler,
                tekil_manuel_gunler,
            )
            if ara_gun_kapasitesi < 0:
                return HedefSonuc(
                    False, [], [], {},
                    {'adalet': {'uyarilar': adalet_uyarilari}},
                    f"Manuel atamalar ara gün kuralıyla çakışıyor: {p.ad} / ara_gün={self.ara_gun}"
                )
            max_kapasite = min(max_kapasite, ara_gun_kapasitesi)

            # Görev kısıtlaması varsa kapasiteyi sınırla
            matched_kisitli = find_matching_id(pid, kisitli_kapasite.keys())
            if matched_kisitli is not None:
                max_kapasite = min(max_kapasite, kisitli_kapasite[matched_kisitli])

            manuel_total = sum(manuel_sayac[pid].values())
            min_nobet = max(0, self._guvenli_int(getattr(p, 'min_nobet', 0), 0))
            max_nobet_raw = getattr(p, 'max_nobet', None)
            max_nobet = None if max_nobet_raw is None else max(0, self._guvenli_int(max_nobet_raw, 0))

            if max_nobet is not None and max_nobet < min_nobet:
                return HedefSonuc(
                    False, [], [], {},
                    {'adalet': {'uyarilar': adalet_uyarilari}},
                    f"Min/max nöbet sınırı geçersiz: {p.ad} / min={min_nobet} max={max_nobet}"
                )

            alt_sinir = max(manuel_total, min_nobet)
            ust_sinir = max_kapasite
            if max_nobet is not None:
                ust_sinir = min(ust_sinir, max_nobet)

            personel_sinirlar[pid] = {
                'manuel_alt_sinir': manuel_total,
                'min_nobet': min_nobet,
                'max_nobet': max_nobet,
                'kapasite': max_kapasite,
                'ara_gun_kapasitesi': ara_gun_kapasitesi,
                'uygulanan_alt_sinir': alt_sinir,
                'uygulanan_ust_sinir': ust_sinir,
            }

            if alt_sinir > ust_sinir:
                return HedefSonuc(
                    False, [], [], {},
                    {'adalet': {'sinirlar': personel_sinirlar, 'uyarilar': adalet_uyarilari}},
                    f"Personel hedef kapasitesi yetersiz: {p.ad} / alt={alt_sinir} üst={ust_sinir}"
                )

            if is_kilitli:
                # KİLİTLİ KİŞİ: Sabit değer (Hard Constraint)
                kilitli_val = p.hedef_tipler
                kilitli_total = 0
                for tip in GUN_TIPLERI:
                    val = self._guvenli_int(kilitli_val.get(tip, 0), 0)
                    if val < manuel_sayac[pid][tip]:
                        return HedefSonuc(
                            False, [], [], {},
                            {'adalet': {'sinirlar': personel_sinirlar, 'uyarilar': adalet_uyarilari}},
                            f"Kilitli hedef manuel atamanın altında: {p.ad} / {tip}"
                        )
                    if val > p.musait_tipler.get(tip, 0):
                        return HedefSonuc(
                            False, [], [], {},
                            {'adalet': {'sinirlar': personel_sinirlar, 'uyarilar': adalet_uyarilari}},
                            f"Kilitli hedef gün tipi kapasitesini aşıyor: {p.ad} / {tip}"
                        )
                    h[pid, tip] = model.NewIntVar(val, val, f'h_{pid}_{tip}_LOCKED')
                    kilitli_total += val
                if kilitli_total < alt_sinir or kilitli_total > ust_sinir:
                    return HedefSonuc(
                        False, [], [], {},
                        {'adalet': {'sinirlar': personel_sinirlar, 'uyarilar': adalet_uyarilari}},
                        f"Kilitli hedef min/max veya kapasite sınırı dışında: {p.ad} / hedef={kilitli_total}"
                    )
                t[pid] = model.NewIntVar(kilitli_total, kilitli_total, f't_{pid}_LOCKED')
                model.Add(sum(h[pid, tip] for tip in GUN_TIPLERI) == t[pid])
                total_h_hours[pid] = sum(h[pid, tip] * self.saat[tip] for tip in GUN_TIPLERI)
                total_h_we[pid] = sum(h[pid, tip] for tip in we_tipleri)
                total_h_wd[pid] = sum(h[pid, tip] for tip in wd_tipleri)
                continue

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

            t[pid] = model.NewIntVar(alt_sinir, ust_sinir, f't_{pid}')

            # Toplam nöbet sayısı eşitliği
            model.Add(sum(h[pid, tip] for tip in GUN_TIPLERI) == t[pid])

            # Saat ve WE toplamları
            total_h_hours[pid] = sum(h[pid, tip] * self.saat[tip] for tip in GUN_TIPLERI)
            total_h_we[pid] = sum(h[pid, tip] for tip in we_tipleri)
            total_h_wd[pid] = sum(h[pid, tip] for tip in wd_tipleri)

        # --- 4. ZORUNLU KISITLAR ---
        pids = [p.id for p in self.personel_listesi]
        gunler = sorted(self.gun_tipleri.keys())
        kisi_gun = {}

        for p in self.personel_listesi:
            pid = p.id
            manuel_gun_set = set(manuel_gunler[pid])
            for gun in gunler:
                var = model.NewBoolVar(f'kisi_gun_{pid}_{gun}')
                kisi_gun[pid, gun] = var
                if gun not in p.musait_gunler:
                    model.Add(var == 0)
                if gun in manuel_gun_set:
                    model.Add(var == 1)

            model.Add(t[pid] == sum(kisi_gun[pid, gun] for gun in gunler))
            for tip in GUN_TIPLERI:
                tip_gunleri = [gun for gun in gunler if self.gun_tipleri.get(gun) == tip]
                model.Add(h[pid, tip] == sum(kisi_gun[pid, gun] for gun in tip_gunleri))

            if self.ara_gun > 0:
                for idx, gun1 in enumerate(gunler):
                    for gun2 in gunler[idx + 1:]:
                        if gun2 - gun1 > self.ara_gun:
                            break
                        model.Add(kisi_gun[pid, gun1] + kisi_gun[pid, gun2] <= 1)

        # Her gün tanımlı tüm görev slotları kadar farklı personel seçilmeli.
        for gun in gunler:
            model.Add(sum(kisi_gun[pid, gun] for pid in pids) == self.slot_sayisi)

        # Toplam slot tutmalı
        model.Add(sum(t[pid] for pid in pids) == self.toplam_slot)

        # Gün tipi toplamları tutmalı
        for tip in GUN_TIPLERI:
            model.Add(sum(h[pid, tip] for pid in pids) == self.tip_slotlari[tip])

        # Kişi-gün-görev birleşik kapasite: role hapsedilmiş kişilerin gün tipi
        # bazındaki hedefi, rolün o tipteki fiziksel slot arzını aşamaz
        # (güvenli üst sınır kesidi; gün tipi sınırları toplamı da kapsar).
        for rol, (uyeler, arz_tipleri) in self._havuz_arz_kapasiteleri().items():
            uygun_uyeler = [pid for pid in uyeler if pid in t]
            if not uygun_uyeler:
                continue
            for tip, arz_tip in arz_tipleri.items():
                model.Add(sum(h[pid, tip] for pid in uygun_uyeler) <= arz_tip)

        # Kişi-gün-görev BİRLEŞİK transport fizibilitesi: hedeflerin gerçek
        # rollere dağıtılabilirliğini count seviyesinde garanti eder (confined
        # sınırın kaçırdığı kısmi-rol Hall ihlallerini yakalar).
        self._rol_transport_kisitlari_ekle(model, h)

        # --- 5. AYLIK KATSAYI VE TARİHSEL BORÇ ADALETİ ---
        current_dimensions = {
            'toplam': {pid: t[pid] for pid in pids},
            'saat': {pid: total_h_hours[pid] for pid in pids},
            'we': {pid: total_h_we[pid] for pid in pids},
            'wd': {pid: total_h_wd[pid] for pid in pids},
        }
        for tip in GUN_TIPLERI:
            current_dimensions[tip] = {pid: h[pid, tip] for pid in pids}

        max_saat_degeri = max(self.saat.values()) if self.saat else 0
        person_dimension_bounds = {}
        person_dimension_lower = {}
        for p in self.personel_listesi:
            pid = p.id
            kisi_ust = personel_sinirlar[pid]['uygulanan_ust_sinir']
            manuel_tipler = manuel_sayac[pid]
            if pid in kilitli_ids:
                kilitli_tipler = {
                    tip: int(p.hedef_tipler.get(tip, 0))
                    for tip in GUN_TIPLERI
                }
                kilitli_toplam = sum(kilitli_tipler.values())
                kilitli_saat = sum(kilitli_tipler[tip] * self.saat[tip] for tip in GUN_TIPLERI)
                kilitli_we = sum(kilitli_tipler[tip] for tip in we_tipleri)
                kilitli_wd = sum(kilitli_tipler[tip] for tip in wd_tipleri)
                person_dimension_lower[pid] = {
                    **kilitli_tipler,
                    'toplam': kilitli_toplam,
                    'saat': kilitli_saat,
                    'we': kilitli_we,
                    'wd': kilitli_wd,
                }
                person_dimension_bounds[pid] = dict(person_dimension_lower[pid])
                continue

            person_dimension_lower[pid] = {
                **manuel_tipler,
                'toplam': personel_sinirlar[pid]['uygulanan_alt_sinir'],
                'saat': sum(manuel_tipler[tip] * self.saat[tip] for tip in GUN_TIPLERI),
                'we': sum(manuel_tipler[tip] for tip in we_tipleri),
                'wd': sum(manuel_tipler[tip] for tip in wd_tipleri),
            }
            person_dimension_bounds[pid] = {
                'toplam': kisi_ust,
                'saat': kisi_ust * max_saat_degeri,
                'we': min(kisi_ust, sum(p.musait_tipler.get(tip, 0) for tip in we_tipleri)),
                'wd': min(kisi_ust, sum(p.musait_tipler.get(tip, 0) for tip in wd_tipleri)),
                **{
                    tip: min(kisi_ust, p.musait_tipler.get(tip, 0))
                    for tip in GUN_TIPLERI
                },
            }

        def add_abs_penalty(name, expression, upper_bound, multiplier):
            if upper_bound <= 0 or multiplier <= 0:
                return None
            fark = model.NewIntVar(0, int(upper_bound), name)
            model.Add(fark >= expression)
            model.Add(fark >= -expression)
            ceza = fark * multiplier
            penalties.append(ceza)
            return ceza

        tarihsel_gruplar = {}
        for p in self.personel_listesi:
            if (
                self._gecmis_kullanilabilir_mi(p)
                and agirlik_map[p.id] > 0
                and person_dimension_bounds[p.id]['toplam'] > 0
            ):
                grup = str(getattr(p, 'adalet_grubu', 'normal') or 'normal')
                tarihsel_gruplar.setdefault(grup, []).append(p.id)
                if str(getattr(p, 'gecmis_veri_durumu', '')).strip().lower() == 'kismi':
                    adalet_uyarilari.append(
                        f"{p.ad}: kısmi geçmiş veri ölçeklenmeden tarihsel kıyaslamaya dahil edildi"
                    )

        tarihsel_karsilastirilan_ids = set()
        for grup, grup_ids in sorted(tarihsel_gruplar.items()):
            if len(grup_ids) < 2:
                adalet_uyarilari.append(
                    f"{grup}: tarihsel kıyaslama için en az iki karşılaştırılabilir personel gerekli"
                )
                continue
            tarihsel_karsilastirilan_ids.update(grup_ids)

        aylik_aktif_ids = [
            p.id for p in self.personel_listesi
            if (
                not bool(getattr(p, 'esitlemeden_muaf', False))
                and agirlik_map[p.id] > 0
                and person_dimension_bounds[p.id]['toplam'] > 0
            )
        ]
        aylik_aktif_toplam_agirlik = sum(agirlik_map[pid] for pid in aylik_aktif_ids)

        # Zorunlu manuel/min yükü çıktıktan sonra kalan gerçek değişken toplamı,
        # kalan kapasitesi olan aktif personellere katsayı oranında dağıtılır.
        aylik_esnek_ids = [
            pid for pid in aylik_aktif_ids
            if person_dimension_bounds[pid]['toplam'] > person_dimension_lower[pid]['toplam']
        ]
        aylik_esnek_agirlik = sum(agirlik_map[pid] for pid in aylik_esnek_ids)
        if aylik_esnek_ids and aylik_esnek_agirlik > 0:
            kalan_toplam = sum(
                t[pid] - person_dimension_lower[pid]['toplam']
                for pid in aylik_esnek_ids
            )
            kalan_ust_toplam = sum(
                person_dimension_bounds[pid]['toplam'] - person_dimension_lower[pid]['toplam']
                for pid in aylik_esnek_ids
            )
            for pid in aylik_esnek_ids:
                kisi_kalan = t[pid] - person_dimension_lower[pid]['toplam']
                kisi_kalan_ust = (
                    person_dimension_bounds[pid]['toplam']
                    - person_dimension_lower[pid]['toplam']
                )
                ifade = kisi_kalan * aylik_esnek_agirlik - kalan_toplam * agirlik_map[pid]
                fark_ust_sinir = max(
                    kisi_kalan_ust * aylik_esnek_agirlik,
                    kalan_ust_toplam * agirlik_map[pid],
                )
                ceza = add_abs_penalty(
                    f'adalet_aylik_kalan_toplam_{pid}',
                    ifade,
                    fark_ust_sinir,
                    ADALET_AYLIK_AGIRLIKLARI['toplam'],
                )
                if ceza is not None:
                    oncelikli_penalties.append(ceza)

        # Geçmişi güvenle kıyaslanabilen kişilerde birleşik tarihsel objective
        # aylık objective'in yerini alır. Bilinmeyen/yeni kişiler aylık katsayıyla
        # dengelenmeye devam eder.
        aylik_ids = [pid for pid in aylik_aktif_ids if pid not in tarihsel_karsilastirilan_ids]
        if aylik_ids:
            for boyut, ifadeler in current_dimensions.items():
                if boyut == 'toplam':
                    continue
                esnek_boyut_ids = [
                    pid for pid in aylik_ids
                    if person_dimension_bounds[pid][boyut] > person_dimension_lower[pid][boyut]
                ]
                if not esnek_boyut_ids:
                    continue
                esnek_boyut_agirlik = sum(agirlik_map[pid] for pid in esnek_boyut_ids)
                kalan_boyut_toplam = sum(
                    ifadeler[pid] - person_dimension_lower[pid][boyut]
                    for pid in esnek_boyut_ids
                )
                kalan_boyut_ust = sum(
                    person_dimension_bounds[pid][boyut] - person_dimension_lower[pid][boyut]
                    for pid in esnek_boyut_ids
                )
                for pid in esnek_boyut_ids:
                    kisi_kalan = ifadeler[pid] - person_dimension_lower[pid][boyut]
                    kisi_kalan_ust = (
                        person_dimension_bounds[pid][boyut]
                        - person_dimension_lower[pid][boyut]
                    )
                    ifade = kisi_kalan * esnek_boyut_agirlik - kalan_boyut_toplam * agirlik_map[pid]
                    fark_ust_sinir = max(
                        kisi_kalan_ust * esnek_boyut_agirlik,
                        kalan_boyut_ust * agirlik_map[pid],
                    )
                    add_abs_penalty(
                        f'adalet_aylik_{boyut}_{pid}',
                        ifade,
                        fark_ust_sinir,
                        ADALET_AYLIK_AGIRLIKLARI[boyut],
                    )

        # Eşitlemeden muaf veya katsayısı sıfır kişiler, ancak kapasite zorunlu
        # kılıyorsa alt sınırlarının üzerine çıkar.
        for p in self.personel_listesi:
            pid = p.id
            if bool(getattr(p, 'esitlemeden_muaf', False)) or agirlik_map[pid] == 0:
                alt_sinir = personel_sinirlar[pid]['uygulanan_alt_sinir']
                ust_sinir = personel_sinirlar[pid]['uygulanan_ust_sinir']
                if ust_sinir > alt_sinir:
                    muaf_fazla = model.NewIntVar(0, ust_sinir - alt_sinir, f'adalet_muaf_fazla_{pid}')
                    model.Add(muaf_fazla == t[pid] - alt_sinir)
                    ceza = muaf_fazla * 200_000
                    penalties.append(ceza)
                    oncelikli_penalties.append(ceza)

        for grup_idx, (grup, grup_ids) in enumerate(sorted(tarihsel_gruplar.items())):
            if len(grup_ids) < 2:
                continue
            for boyut, ifadeler in current_dimensions.items():
                boyut_ids = [
                    pid for pid in grup_ids
                    if person_dimension_bounds[pid][boyut] > 0
                ]
                if len(boyut_ids) < 2:
                    continue
                grup_agirlik = sum(agirlik_map[pid] for pid in boyut_ids)
                birlesik_toplam = sum(
                    ifadeler[pid] + gecmis_metrikleri[pid][boyut]
                    for pid in boyut_ids
                )
                birlesik_ust_toplam = sum(
                    person_dimension_bounds[pid][boyut] + gecmis_metrikleri[pid][boyut]
                    for pid in boyut_ids
                )
                for pid in boyut_ids:
                    ifade = (
                        (ifadeler[pid] + gecmis_metrikleri[pid][boyut]) * grup_agirlik
                        - birlesik_toplam * agirlik_map[pid]
                    )
                    kisi_birlesik_ust = (
                        gecmis_metrikleri[pid][boyut]
                        + person_dimension_bounds[pid][boyut]
                    )
                    fark_ust_sinir = max(
                        kisi_birlesik_ust * grup_agirlik,
                        birlesik_ust_toplam * agirlik_map[pid],
                    )
                    ceza = add_abs_penalty(
                        f'adalet_tarih_{grup_idx}_{boyut}_{pid}',
                        ifade,
                        fark_ust_sinir,
                        ADALET_TARIHSEL_AGIRLIKLARI[boyut],
                    )
                    if boyut == 'toplam' and ceza is not None:
                        oncelikli_penalties.append(ceza)

        tip_esdeger_gruplari = []
        saat_gruplari = {}
        for tip in GUN_TIPLERI:
            saat_gruplari.setdefault(self.saat.get(tip, 0), []).append(tip)
        for tipler in saat_gruplari.values():
            if len(tipler) >= 2:
                tip_esdeger_gruplari.append(sorted(tipler))

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

            politika = str(getattr(kural, 'politika', 'kullanici_onayli') or 'kullanici_onayli').strip().lower()
            birlikte_debug.append(f"Grup: {grup_adlar} / politika={politika}")

            if politika != 'soft':
                referans_id = grup[0]
                for diger_id in grup[1:]:
                    for gun in gunler:
                        model.Add(kisi_gun[referans_id, gun] == kisi_gun[diger_id, gun])
                continue

            # All-pairs: tüm çiftleri karşılaştır — SOFT constraint
            for i in range(len(grup)):
                for j in range(i + 1, len(grup)):
                    p1_id, p2_id = grup[i], grup[j]
                    diff = model.NewIntVar(-HARD_CAP, HARD_CAP, f'birlikte_diff_{p1_id}_{p2_id}')
                    model.Add(t[p1_id] - t[p2_id] == diff)
                    abs_diff = model.NewIntVar(0, HARD_CAP, f'abs_birlikte_{p1_id}_{p2_id}')
                    model.AddAbsEquality(abs_diff, diff)
                    penalties.append(abs_diff * 500)

                    for tip in GUN_TIPLERI:
                        tip_diff = model.NewIntVar(-HARD_CAP, HARD_CAP, f'birlikte_tip_diff_{p1_id}_{p2_id}_{tip}')
                        model.Add(h[p1_id, tip] - h[p2_id, tip] == tip_diff)
                        abs_tip_diff = model.NewIntVar(0, HARD_CAP, f'abs_birlikte_tip_{p1_id}_{p2_id}_{tip}')
                        model.AddAbsEquality(abs_tip_diff, tip_diff)
                        penalties.append(abs_tip_diff * 250)

                    for grup_idx, tipler in enumerate(tip_esdeger_gruplari):
                        grup_diff = model.NewIntVar(-HARD_CAP, HARD_CAP, f'birlikte_esdeger_diff_{p1_id}_{p2_id}_{grup_idx}')
                        model.Add(
                            sum(h[p1_id, tip] for tip in tipler) -
                            sum(h[p2_id, tip] for tip in tipler) == grup_diff
                        )
                        abs_grup_diff = model.NewIntVar(0, HARD_CAP, f'abs_birlikte_esdeger_{p1_id}_{p2_id}_{grup_idx}')
                        model.AddAbsEquality(abs_grup_diff, grup_diff)
                        penalties.append(abs_grup_diff * 175)

        # --- 6. ÇÖZÜM ---
        oncelikli_objective = sum(oncelikli_penalties)
        adalet_objective = sum(penalties)
        model.Minimize(adalet_objective)

        # MODEL VALIDATE — hangi kısıt geçersiz?
        validation_err = model.Validate()
        if validation_err:
            return HedefSonuc(False, [], [], {}, {},
                f"MODEL_INVALID validate: {validation_err}")

        projection_model = cp.CpModel()
        projection_t = {
            pid: projection_model.NewIntVar(
                person_dimension_lower[pid]['toplam'],
                person_dimension_bounds[pid]['toplam'],
                f'projection_t_{pid}',
            )
            for pid in pids
        }
        projection_model.Add(sum(projection_t.values()) == self.toplam_slot)
        projection_penalties = []

        def projection_add_abs(name, expression, upper_bound, multiplier):
            if upper_bound <= 0 or multiplier <= 0:
                return
            fark = projection_model.NewIntVar(0, int(upper_bound), name)
            projection_model.Add(fark >= expression)
            projection_model.Add(fark >= -expression)
            projection_penalties.append(fark * multiplier)

        if aylik_esnek_ids and aylik_esnek_agirlik > 0:
            projection_kalan_toplam = sum(
                projection_t[pid] - person_dimension_lower[pid]['toplam']
                for pid in aylik_esnek_ids
            )
            projection_kalan_ust = sum(
                person_dimension_bounds[pid]['toplam']
                - person_dimension_lower[pid]['toplam']
                for pid in aylik_esnek_ids
            )
            for pid in aylik_esnek_ids:
                kisi_kalan = projection_t[pid] - person_dimension_lower[pid]['toplam']
                kisi_kalan_ust = (
                    person_dimension_bounds[pid]['toplam']
                    - person_dimension_lower[pid]['toplam']
                )
                ifade = (
                    kisi_kalan * aylik_esnek_agirlik
                    - projection_kalan_toplam * agirlik_map[pid]
                )
                fark_ust_sinir = max(
                    kisi_kalan_ust * aylik_esnek_agirlik,
                    projection_kalan_ust * agirlik_map[pid],
                )
                projection_add_abs(
                    f'projection_aylik_toplam_{pid}',
                    ifade,
                    fark_ust_sinir,
                    ADALET_AYLIK_AGIRLIKLARI['toplam'],
                )

        for p in self.personel_listesi:
            pid = p.id
            if bool(getattr(p, 'esitlemeden_muaf', False)) or agirlik_map[pid] == 0:
                alt_sinir = person_dimension_lower[pid]['toplam']
                ust_sinir = person_dimension_bounds[pid]['toplam']
                if ust_sinir > alt_sinir:
                    projection_penalties.append(
                        (projection_t[pid] - alt_sinir) * 200_000
                    )

        for grup_idx, (grup, grup_ids) in enumerate(sorted(tarihsel_gruplar.items())):
            boyut_ids = [
                pid for pid in grup_ids
                if person_dimension_bounds[pid]['toplam'] > 0
            ]
            if len(boyut_ids) < 2:
                continue
            grup_agirlik = sum(agirlik_map[pid] for pid in boyut_ids)
            birlesik_toplam = sum(
                projection_t[pid] + gecmis_metrikleri[pid]['toplam']
                for pid in boyut_ids
            )
            birlesik_ust_toplam = sum(
                person_dimension_bounds[pid]['toplam']
                + gecmis_metrikleri[pid]['toplam']
                for pid in boyut_ids
            )
            for pid in boyut_ids:
                ifade = (
                    (projection_t[pid] + gecmis_metrikleri[pid]['toplam']) * grup_agirlik
                    - birlesik_toplam * agirlik_map[pid]
                )
                kisi_birlesik_ust = (
                    gecmis_metrikleri[pid]['toplam']
                    + person_dimension_bounds[pid]['toplam']
                )
                fark_ust_sinir = max(
                    kisi_birlesik_ust * grup_agirlik,
                    birlesik_ust_toplam * agirlik_map[pid],
                )
                projection_add_abs(
                    f'projection_tarih_{grup_idx}_{pid}',
                    ifade,
                    fark_ust_sinir,
                    ADALET_TARIHSEL_AGIRLIKLARI['toplam'],
                )

        for kural in self.birlikte_kurallar:
            if kural.tur != 'birlikte':
                continue
            politika = str(
                getattr(kural, 'politika', 'kullanici_onayli') or 'kullanici_onayli'
            ).strip().lower()
            if politika == 'soft':
                continue
            grup = []
            for pid in kural.kisiler:
                matched_id = find_matching_id(pid, self.personeller.keys())
                if matched_id is not None:
                    grup.append(matched_id)
            if len(grup) < 2:
                continue
            for diger_id in grup[1:]:
                projection_model.Add(projection_t[grup[0]] == projection_t[diger_id])

        projection_model.Minimize(sum(projection_penalties))
        projection_solver = cp.CpSolver()
        projection_solver.parameters.max_time_in_seconds = 1
        projection_solver.parameters.num_search_workers = 4
        projection_status = projection_solver.Solve(projection_model)
        projection_uygulanabilir = projection_status in [cp.OPTIMAL, cp.FEASIBLE]
        projection_objective_degeri = (
            int(round(projection_solver.ObjectiveValue()))
            if projection_uygulanabilir
            else None
        )
        projection_wall_time = round(projection_solver.WallTime(), 3)

        solver = cp.CpSolver()
        status = cp.UNKNOWN
        optimizasyon_status = cp.UNKNOWN
        optimizasyon_objective_degeri = None
        optimizasyon_wall_time = 0.0
        projection_siniri_kullanildi = False
        if projection_uygulanabilir:
            projection_sinirli_model = model.Clone()
            if oncelikli_penalties:
                projection_sinirli_model.Add(
                    oncelikli_objective <= projection_objective_degeri
                )
            for pid in pids:
                projection_sinirli_model.AddHint(
                    t[pid], int(projection_solver.Value(projection_t[pid]))
                )
            projection_sinirli_model.Minimize(adalet_objective)
            solver.parameters.max_time_in_seconds = self._detay_cozum_sure_saniye()
            solver.parameters.num_search_workers = 4
            status = solver.Solve(projection_sinirli_model)
            optimizasyon_status = status
            optimizasyon_objective_degeri = (
                round(solver.ObjectiveValue(), 3)
                if status in [cp.OPTIMAL, cp.FEASIBLE]
                else None
            )
            optimizasyon_wall_time = round(solver.WallTime(), 3)
            projection_siniri_kullanildi = status in [cp.OPTIMAL, cp.FEASIBLE]

        ilk_solver = cp.CpSolver()
        ilk_status = cp.UNKNOWN
        ilk_uygulanabilir = False
        ilk_objective_degeri = None
        ilk_wall_time = 0.0
        feasibility_fallback_kullanildi = False
        if status not in [cp.OPTIMAL, cp.FEASIBLE]:
            model.Minimize(oncelikli_objective)
            ilk_solver.parameters.max_time_in_seconds = 3
            ilk_solver.parameters.num_search_workers = 4
            ilk_status = ilk_solver.Solve(model)
            ilk_uygulanabilir = ilk_status in [cp.OPTIMAL, cp.FEASIBLE]
            ilk_objective_degeri = (
                int(round(ilk_solver.ObjectiveValue()))
                if ilk_uygulanabilir
                else None
            )
            ilk_wall_time = round(ilk_solver.WallTime(), 3)
            if ilk_uygulanabilir:
                for degisken in list(h.values()) + list(t.values()) + list(kisi_gun.values()):
                    model.AddHint(degisken, int(ilk_solver.Value(degisken)))
                if oncelikli_penalties:
                    model.Add(oncelikli_objective <= ilk_objective_degeri)

            model.Minimize(adalet_objective)
            solver = cp.CpSolver()
            solver.parameters.max_time_in_seconds = self._detay_cozum_sure_saniye()
            solver.parameters.num_search_workers = 4
            status = solver.Solve(model)
            optimizasyon_status = status
            optimizasyon_objective_degeri = (
                round(solver.ObjectiveValue(), 3)
                if status in [cp.OPTIMAL, cp.FEASIBLE]
                else None
            )
            optimizasyon_wall_time = round(solver.WallTime(), 3)
            if status not in [cp.OPTIMAL, cp.FEASIBLE] and ilk_uygulanabilir:
                solver = ilk_solver
                status = ilk_status
                feasibility_fallback_kullanildi = True

        solver_status_name = solver.StatusName(status)
        projection_status_name = projection_solver.StatusName(projection_status)
        ilk_status_name = ilk_solver.StatusName(ilk_status)
        optimizasyon_status_name = cp.CpSolver().StatusName(optimizasyon_status)
        adalet_optimal = (
            projection_siniri_kullanildi
            and projection_status == cp.OPTIMAL
            and optimizasyon_status == cp.OPTIMAL
        ) or (
            not projection_siniri_kullanildi
            and ilk_status == cp.OPTIMAL
            and optimizasyon_status == cp.OPTIMAL
        )

        if status not in [cp.OPTIMAL, cp.FEASIBLE]:
            debug_msg = build_hedef_infeasible_debug(
                cp=cp,
                hesaplayici=self,
                status=status,
                n=n,
                kilitli_ids=kilitli_ids,
                kalan_slot=kalan_slot,
                n_kilitsiz=n_kilitsiz,
                avg_count_float=avg_count_float,
                avg_count_floor=avg_count_floor,
                avg_hours=avg_hours,
                hard_cap=HARD_CAP,
                kisitli_kapasite=kisitli_kapasite,
                manuel_sayac=manuel_sayac,
                total_we_slots=total_we_slots,
                we_tipleri=we_tipleri,
            )
            return HedefSonuc(False, [], [], {}, {}, f"Hedef CP-SAT cozumsuz: {debug_msg}")

        # --- 7. SONUÇLARI PERSONELLERE YAZ ---
        for p in self.personel_listesi:
            pid = p.id
            for tip in GUN_TIPLERI:
                p.hedef_tipler[tip] = int(solver.Value(h[pid, tip]))

        # Kısıtlı kişiler için taşma kota dağıtımı
        # (hedef_tipler OR-Tools'tan geldikten sonra çalışmalı)
        self._hesapla_kisitli_kisi_gorev_kotalari()

        aylik_metrikler = {}
        for p in self.personel_listesi:
            tipler = {tip: int(p.hedef_tipler.get(tip, 0)) for tip in GUN_TIPLERI}
            aylik_metrikler[p.id] = {
                **tipler,
                'toplam': sum(tipler.values()),
                'saat': sum(tipler[tip] * self.saat[tip] for tip in GUN_TIPLERI),
                'we': sum(tipler[tip] for tip in we_tipleri),
                'wd': sum(tipler[tip] for tip in wd_tipleri),
            }

        adalet_boyutlari = ['toplam', 'saat', 'we', 'wd', *GUN_TIPLERI]
        aylik_beklenen = {pid: {} for pid in pids}
        aylik_sapma = {pid: {} for pid in pids}
        if aylik_aktif_ids and aylik_aktif_toplam_agirlik > 0:
            for boyut in adalet_boyutlari:
                aktif_toplam = sum(aylik_metrikler[pid][boyut] for pid in aylik_aktif_ids)
                for pid in aylik_aktif_ids:
                    beklenen = aktif_toplam * agirlik_map[pid] / aylik_aktif_toplam_agirlik
                    aylik_beklenen[pid][boyut] = round(beklenen, 3)
                    aylik_sapma[pid][boyut] = round(aylik_metrikler[pid][boyut] - beklenen, 3)

        aylik_kalan_gerceklesen = {pid: {} for pid in pids}
        aylik_kalan_beklenen = {pid: {} for pid in pids}
        aylik_kalan_sapma = {pid: {} for pid in pids}
        if aylik_esnek_ids and aylik_esnek_agirlik > 0:
            toplam_kalan = sum(
                aylik_metrikler[pid]['toplam'] - person_dimension_lower[pid]['toplam']
                for pid in aylik_esnek_ids
            )
            for pid in aylik_esnek_ids:
                gerceklesen = (
                    aylik_metrikler[pid]['toplam']
                    - person_dimension_lower[pid]['toplam']
                )
                beklenen = toplam_kalan * agirlik_map[pid] / aylik_esnek_agirlik
                aylik_kalan_gerceklesen[pid]['toplam'] = gerceklesen
                aylik_kalan_beklenen[pid]['toplam'] = round(beklenen, 3)
                aylik_kalan_sapma[pid]['toplam'] = round(gerceklesen - beklenen, 3)

        for boyut in (boyut for boyut in adalet_boyutlari if boyut != 'toplam'):
            esnek_boyut_ids = [
                pid for pid in aylik_ids
                if person_dimension_bounds[pid][boyut] > person_dimension_lower[pid][boyut]
            ]
            if not esnek_boyut_ids:
                continue
            esnek_boyut_agirlik = sum(agirlik_map[pid] for pid in esnek_boyut_ids)
            if esnek_boyut_agirlik <= 0:
                continue
            toplam_kalan = sum(
                aylik_metrikler[pid][boyut] - person_dimension_lower[pid][boyut]
                for pid in esnek_boyut_ids
            )
            for pid in esnek_boyut_ids:
                gerceklesen = (
                    aylik_metrikler[pid][boyut]
                    - person_dimension_lower[pid][boyut]
                )
                beklenen = toplam_kalan * agirlik_map[pid] / esnek_boyut_agirlik
                aylik_kalan_gerceklesen[pid][boyut] = gerceklesen
                aylik_kalan_beklenen[pid][boyut] = round(beklenen, 3)
                aylik_kalan_sapma[pid][boyut] = round(gerceklesen - beklenen, 3)

        devir_once = {pid: {} for pid in pids}
        devir_sonra = {pid: {} for pid in pids}
        tarihsel_beklenen_once = {pid: {} for pid in pids}
        tarihsel_beklenen_sonra = {pid: {} for pid in pids}
        for grup, grup_ids in tarihsel_gruplar.items():
            if len(grup_ids) < 2:
                continue
            for boyut in adalet_boyutlari:
                boyut_ids = [
                    pid for pid in grup_ids
                    if person_dimension_bounds[pid][boyut] > 0
                ]
                if len(boyut_ids) < 2:
                    continue
                grup_agirlik = sum(agirlik_map[pid] for pid in boyut_ids)
                gecmis_toplam = sum(gecmis_metrikleri[pid][boyut] for pid in boyut_ids)
                aylik_toplam = sum(aylik_metrikler[pid][boyut] for pid in boyut_ids)
                birlesik_toplam = gecmis_toplam + aylik_toplam
                for pid in boyut_ids:
                    once_beklenen = gecmis_toplam * agirlik_map[pid] / grup_agirlik
                    sonra_beklenen = birlesik_toplam * agirlik_map[pid] / grup_agirlik
                    tarihsel_beklenen_once[pid][boyut] = round(once_beklenen, 3)
                    tarihsel_beklenen_sonra[pid][boyut] = round(sonra_beklenen, 3)
                    devir_once[pid][boyut] = round(
                        gecmis_metrikleri[pid][boyut] - once_beklenen, 3
                    )
                    devir_sonra[pid][boyut] = round(
                        gecmis_metrikleri[pid][boyut]
                        + aylik_metrikler[pid][boyut]
                        - sonra_beklenen,
                        3,
                    )

        adalet_detaylari = {}
        for p in self.personel_listesi:
            pid = p.id
            durum = str(getattr(p, 'gecmis_veri_durumu', 'bilinmiyor') or 'bilinmiyor').strip().lower()
            muaf = bool(getattr(p, 'esitlemeden_muaf', False))
            gecmis_kullanildi = pid in tarihsel_karsilastirilan_ids
            if muaf:
                aciklama = "Eşitlemeden muaf; yalnız manuel/min/max ve kapasite sınırları uygulandı."
            elif agirlik_map[pid] == 0:
                aciklama = "İş yükü katsayısı 0; otomatik hedef yalnız kapasite zorunluysa alt sınırı aşabilir."
            elif durum in {'bilinmiyor', 'yeni'}:
                aciklama = "Geçmiş kıyaslanmadı; aylık iş yükü katsayısı adaleti uygulandı."
            elif not gecmis_kullanildi:
                aciklama = "Geçmiş mevcut ancak aynı adalet grubunda karşılaştırılabilir ikinci kişi yok."
            else:
                aciklama = "Pozitif devir fazla yükü, negatif devir nöbet alacağını gösterir."

            adalet_detaylari[pid] = {
                'model_versiyonu': 2,
                'is_yuku_katsayisi': katsayi_map[pid],
                'katsayi_agirligi': agirlik_map[pid],
                'adalet_grubu': str(getattr(p, 'adalet_grubu', 'normal') or 'normal'),
                'esitlemeden_muaf': muaf,
                'gecmis_veri_durumu': durum,
                'gecmis_kullanildi': gecmis_kullanildi,
                'gecmis': (
                    gecmis_metrikleri[pid]
                    if bool(getattr(p, 'yillik_gerceklesen', None))
                    else {}
                ),
                'aylik_gerceklesen_hedef': aylik_metrikler[pid],
                'aylik_beklenen': aylik_beklenen[pid],
                'aylik_sapma': aylik_sapma[pid],
                'aylik_kalan_gerceklesen': aylik_kalan_gerceklesen[pid],
                'aylik_kalan_beklenen': aylik_kalan_beklenen[pid],
                'aylik_kalan_sapma': aylik_kalan_sapma[pid],
                'hedef_gunler': [
                    gun for gun in gunler
                    if int(solver.Value(kisi_gun[pid, gun])) == 1
                ],
                'tarihsel_beklenen_once': tarihsel_beklenen_once[pid],
                'tarihsel_beklenen_sonra': tarihsel_beklenen_sonra[pid],
                'devir_once': devir_once[pid],
                'devir_sonra': devir_sonra[pid],
                'sinirlar': personel_sinirlar[pid],
                'aciklama': aciklama,
            }

        devir_ozeti = {}
        for boyut in adalet_boyutlari:
            once_mutlak = sum(
                abs(devir_once[pid].get(boyut, 0))
                for pid in tarihsel_karsilastirilan_ids
            )
            sonra_mutlak = sum(
                abs(devir_sonra[pid].get(boyut, 0))
                for pid in tarihsel_karsilastirilan_ids
            )
            devir_ozeti[boyut] = {
                'once_mutlak_toplam': round(once_mutlak, 3),
                'sonra_mutlak_toplam': round(sonra_mutlak, 3),
                'iyilesme': round(once_mutlak - sonra_mutlak, 3),
            }

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
                'adalet': adalet_detaylari[p.id],
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
            'kisitli_kapasite': {str(k): v for k, v in kisitli_kapasite.items()},
            'adalet': {
                'model_versiyonu': 2,
                'katsayi_olcegi': ADALET_KATSAYI_OLCEGI,
                'cozucu_durumu': solver_status_name,
                'oncelikli_gecis_durumu': projection_status_name,
                'oncelikli_gecis_objective_degeri': projection_objective_degeri,
                'oncelikli_gecis_suresi_saniye': projection_wall_time,
                'projeksiyon_siniri_kullanildi': projection_siniri_kullanildi,
                'exact_fallback_gecis_durumu': ilk_status_name,
                'exact_fallback_objective_degeri': ilk_objective_degeri,
                'exact_fallback_suresi_saniye': ilk_wall_time,
                'optimizasyon_durumu': optimizasyon_status_name,
                'adalet_optimal': adalet_optimal,
                'uygulanabilirlik_yedegi_kullanildi': feasibility_fallback_kullanildi,
                'optimizasyon_objective_degeri': optimizasyon_objective_degeri,
                'optimizasyon_suresi_saniye': optimizasyon_wall_time,
                'kisi_gun_degisken_sayisi': len(kisi_gun),
                'aylik_aktif_personel_sayisi': len(aylik_aktif_ids),
                'aylik_esnek_personel_sayisi': len(aylik_esnek_ids),
                'aylik_detay_personel_sayisi': len(aylik_ids),
                'aylik_katsayi_personel_sayisi': len(aylik_aktif_ids),
                'tarihsel_karsilastirma_personel_sayisi': len(tarihsel_karsilastirilan_ids),
                'tarihsel_gruplar': {
                    grup: len(grup_ids) for grup, grup_ids in tarihsel_gruplar.items()
                },
                'bilinmeyen_ve_yeni_personel_sayisi': sum(
                    1 for p in self.personel_listesi
                    if str(getattr(p, 'gecmis_veri_durumu', 'bilinmiyor')).strip().lower()
                    in {'bilinmiyor', 'yeni'}
                ),
                'esitlemeden_muaf_personel_sayisi': sum(
                    1 for p in self.personel_listesi
                    if bool(getattr(p, 'esitlemeden_muaf', False))
                ),
                'devir_ozeti': devir_ozeti,
                'uyarilar': adalet_uyarilari,
                'devir_aciklamasi': (
                    "Pozitif değer beklenenden fazla geçmiş+aylık yükü, negatif değer nöbet alacağını gösterir."
                ),
            },
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
