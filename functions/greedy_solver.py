"""
Greedy nöbet dağıtım çözücüsü — NobetYoneticisi sınıfı.
"""

from typing import List, Dict, Set, Optional
from models import GorevTanim, Personel
from utils import normalize_id, ids_match


class NobetYoneticisi:
    """Nöbet dağıtım yöneticisi - Gün tipi bazlı sistem"""

    def __init__(self, personeller: List[Personel], gunluk_sayi: int, takvim: Dict,
                 ara_gun: int, days_in_month: int, gorev_tanimlari: List[GorevTanim],
                 kurallar: List[Dict], gorev_kisitlamalari: List[Dict] = None):
        self.personeller = personeller
        self.gunluk_sayi = gunluk_sayi
        self.takvim = takvim
        self.ara_gun = ara_gun
        self.days_in_month = days_in_month
        self.gorevler = sorted(gorev_tanimlari, key=lambda x: x.slot_index)
        self.kurallar = kurallar
        self.gorev_kisitlamalari = gorev_kisitlamalari or []
        self.cizelge = {d: [None] * len(self.gorevler) for d in range(1, days_in_month + 1)}
        self.manuel_atamalar_set = set()  # (gun, slot_idx) ciftleri - backtracking koruması
        self.birlikte_uye_adlari = self._birlikte_uye_adlarini_hesapla()

        # Mazeret istatistikleri
        self.gun_mazeret_sayisi = {}
        self._hesapla_mazeret_istatistikleri()

    def _birlikte_uye_adlarini_hesapla(self) -> Set[str]:
        adlar = set()
        for kural in self.kurallar:
            if kural.get('tur') != 'birlikte':
                continue
            for key in ['p1', 'p2', 'p3']:
                ad = kural.get(key)
                if isinstance(ad, str) and ad.strip():
                    adlar.add(ad.strip())
            kisiler = kural.get('kisiler')
            if isinstance(kisiler, list):
                for ref in kisiler:
                    if isinstance(ref, str):
                        ref_s = ref.strip()
                        if ref_s:
                            adlar.add(ref_s)
                        continue
                    pid = normalize_id(ref)
                    p = next((x for x in self.personeller if ids_match(x.id, pid)), None)
                    if p:
                        adlar.add(p.ad)
        return adlar

    def _hesapla_mazeret_istatistikleri(self):
        """Her gün için kaç kişinin mazeretli olduğunu hesapla"""
        for gun in range(1, self.days_in_month + 1):
            self.gun_mazeret_sayisi[gun] = sum(
                1 for p in self.personeller if gun in p.mazeret_gunleri
            )

    def _get_gun_tipi(self, gun: int) -> str:
        gun_adi = self.takvim.get(gun, "")
        if gun_adi == "Pazar": return "pazar"
        if gun_adi == "Cumartesi": return "cumartesi"
        if gun_adi == "Cuma": return "cuma"
        if gun_adi == "Persembe": return "persembe"
        return "hici"

    def gunleri_sirala(self) -> List[int]:
        """
        VBA MANTIĞI: En kısıtlı (mazeretli) günden başla
        1. Mazeret sayısı çok olan gün önce
        2. Zorluk sırası: Cmt (24s) > Pzr = Cum (16s) > Prş = H.İçi (8s)
        3. Tarih sırası
        """
        gun_skorlari = []
        for gun in range(1, self.days_in_month + 1):
            gun_tipi = self._get_gun_tipi(gun)
            mazeret_sayisi = self.gun_mazeret_sayisi.get(gun, 0)

            skor = mazeret_sayisi * 1000

            if gun_tipi == "cumartesi":
                skor += 500
            elif gun_tipi == "pazar":
                skor += 400
            elif gun_tipi == "cuma":
                skor += 400
            elif gun_tipi == "persembe":
                skor += 200
            else:
                skor += 200

            gun_skorlari.append((gun, skor))

        gun_skorlari.sort(key=lambda x: (-x[1], x[0]))
        return [x[0] for x in gun_skorlari]

    def gruplari_sirala(self, birlikte_kurallar: List[Dict]) -> List[Dict]:
        """
        VBA MANTIĞI: En çok mazeretli grup önce
        """

        def grup_mazeret_skoru(kural):
            toplam = 0
            for key in ['p1', 'p2', 'p3']:
                isim = kural.get(key)
                if isim:
                    p = next((x for x in self.personeller if x.ad == isim), None)
                    if p:
                        toplam += p.mazeret_sayisi
            return toplam

        return sorted(birlikte_kurallar, key=grup_mazeret_skoru, reverse=True)

    def kisi_uygun_mu(self, p: Personel, gun: int, gun_tipi: str,
                      gorev: GorevTanim, min_ara_gun: int,
                      bugun_atananlar: List[str] = None) -> bool:
        """Kişinin belirli bir güne atanıp atanamayacağını kontrol et"""
        bugun_atananlar = bugun_atananlar or []

        if gun in p.mazeret_gunleri: return False
        if gun in p.atanan_gunler: return False
        if p.ad in bugun_atananlar: return False
        if not p.kota_kontrol(gun_tipi, gorev.ad, gorev.base_name): return False

        for atanan_gun in p.atanan_gunler:
            if abs(gun - atanan_gun) <= min_ara_gun:
                return False

        for kisit in self.gorev_kisitlamalari:
            if ids_match(kisit.get('personelId'), p.id):
                kisit_gorev = kisit.get('gorevAdi')
                if kisit_gorev != gorev.ad and kisit_gorev != gorev.base_name:
                    # Havuz üyesi ise (exclusive=false) diğer görevlere de atanabilir
                    is_exclusive = kisit.get('exclusive', True)
                    havuz_ids = kisit.get('havuzIds', [])
                    if not is_exclusive or len(havuz_ids) > 0:
                        # Havuz varsa veya exclusive değilse, kısıtlama yumuşak
                        pass
                    else:
                        return False

        if gorev.ayri_bina and p.ad in self.birlikte_uye_adlari:
            return False

        for kural in self.kurallar:
            if kural.get('tur') == 'ayri':
                p1, p2 = kural.get('p1'), kural.get('p2')
                if p.ad == p1 and p2 in bugun_atananlar: return False
                if p.ad == p2 and p1 in bugun_atananlar: return False

        return True

    def kisi_puanla(self, p: Personel, gun: int, gun_tipi: str, gorev: GorevTanim) -> float:
        """VBA MANTIĞI: Sıkışıklık ve panik faktörü ile puanlama"""
        puan = 0.0

        kontrol_adi = gorev.base_name if gorev.base_name else gorev.ad
        if kontrol_adi in p.kalan_roller and p.kalan_roller[kontrol_adi] > 0:
            puan += 5000

        # Kişi SADECE bu göreve atanabiliyorsa (diğer görevlere kotası 0) → çok yüksek öncelik
        diger_gorev_var = False
        for g in self.gorevler:
            g_adi = g.base_name if g.base_name else g.ad
            if g_adi != kontrol_adi and p.kalan_roller.get(g_adi, 0) > 0:
                diger_gorev_var = True
                break
        if not diger_gorev_var and kontrol_adi in p.kalan_roller and p.kalan_roller[kontrol_adi] > 0:
            puan += 20000  # Çok yüksek bonus - bu kişi SADECE bu göreve gidebilir

        puan += p.mazeret_sayisi * 100

        kalan_hedef = p.kalan_toplam
        kalan_bos_gun = self.days_in_month - p.mazeret_sayisi - len(p.atanan_gunler)
        if kalan_bos_gun < 1: kalan_bos_gun = 1

        panic = (kalan_hedef * 1000) / kalan_bos_gun
        puan += panic

        devir_map = {"hici": "hici", "persembe": "prs", "cuma": "cum",
                     "cumartesi": "cmt", "pazar": "pzr"}
        devir_key = devir_map.get(gun_tipi, gun_tipi)
        if p.devir.get(devir_key, 0) > 0:
            puan += 3000

        puan -= p.yillik_toplam * 10
        puan -= len(p.atanan_gunler) * 200

        if p.son_nobet_gunu > 0:
            puan += (gun - p.son_nobet_gunu) * 10
        else:
            puan += 500

        return puan

    def en_uygun_adayi_sec(self, gun: int, gun_tipi: str, gorev: GorevTanim,
                           min_ara_gun: int, bugun_atananlar: List[str] = None) -> Optional[Personel]:
        """En uygun adayı seç (VBA puanlama sistemiyle)"""
        bugun_atananlar = bugun_atananlar or []

        adaylar = []
        for p in self.personeller:
            if self.kisi_uygun_mu(p, gun, gun_tipi, gorev, min_ara_gun, bugun_atananlar):
                puan = self.kisi_puanla(p, gun, gun_tipi, gorev)
                adaylar.append((p, puan))

        if not adaylar:
            return None

        adaylar.sort(key=lambda x: -x[1])
        return adaylar[0][0]

    def grup_ortak_musait_gunler(self, grup_uyeleri: List[Personel], min_ara_gun: int) -> List[int]:
        """Grubun tüm üyelerinin müsait olduğu günleri bul"""
        ortak_gunler = []
        sirali_gunler = self.gunleri_sirala()

        for gun in sirali_gunler:
            hepsi_musait = True
            for p in grup_uyeleri:
                if gun in p.mazeret_gunleri:
                    hepsi_musait = False
                    break
                if gun in p.atanan_gunler:
                    hepsi_musait = False
                    break
                for atanan_gun in p.atanan_gunler:
                    if abs(gun - atanan_gun) <= min_ara_gun:
                        hepsi_musait = False
                        break
                if not hepsi_musait:
                    break

            if hepsi_musait:
                ortak_gunler.append(gun)

        return ortak_gunler

    def grup_dagitimi(self, min_ara_gun: int):
        """VBA MANTIĞI: Birlikte tutulacakları önce yerleştir"""
        birlikte_kurallar = [k for k in self.kurallar if k.get('tur') == 'birlikte']
        if not birlikte_kurallar:
            return

        sirali_kurallar = self.gruplari_sirala(birlikte_kurallar)

        for kural in sirali_kurallar:
            grup_uyeleri = []
            for key in ['p1', 'p2', 'p3']:
                isim = kural.get(key)
                if isim:
                    p = next((x for x in self.personeller if x.ad == isim), None)
                    if p:
                        grup_uyeleri.append(p)

            if len(grup_uyeleri) < 2:
                continue

            hedef_sayisi = min(p.hedef_toplam for p in grup_uyeleri)
            yazilan_sayisi = 0

            ortak_gunler = self.grup_ortak_musait_gunler(grup_uyeleri, min_ara_gun)

            for gun in ortak_gunler:
                if yazilan_sayisi >= hedef_sayisi:
                    break

                gun_tipi = self._get_gun_tipi(gun)

                bos_slotlar = []
                for s_idx, g_obj in enumerate(self.gorevler):
                    if self.cizelge[gun][s_idx] is None and not g_obj.ayri_bina:
                        bos_slotlar.append((s_idx, g_obj))

                if len(bos_slotlar) < len(grup_uyeleri):
                    continue

                puanli_uyeler = []
                for p in grup_uyeleri:
                    uygun_slot_sayisi = sum(
                        1 for s_idx, g_obj in bos_slotlar
                        if p.kota_kontrol(gun_tipi, g_obj.ad, g_obj.base_name)
                    )
                    puanli_uyeler.append((p, uygun_slot_sayisi))

                puanli_uyeler.sort(key=lambda x: x[1])

                gecici_atama = {}
                kullanilan = set()
                basarili = True

                for p, _ in puanli_uyeler:
                    yer_buldu = False
                    for s_idx, g_obj in bos_slotlar:
                        if s_idx in kullanilan:
                            continue
                        if p.kota_kontrol(gun_tipi, g_obj.ad, g_obj.base_name):
                            gecici_atama[p.id] = (s_idx, g_obj)
                            kullanilan.add(s_idx)
                            yer_buldu = True
                            break
                    if not yer_buldu:
                        basarili = False
                        break

                en_iyi_atama = gecici_atama if basarili else None

                if en_iyi_atama:
                    for pid, (s_idx, g_obj) in en_iyi_atama.items():
                        p = next((x for x in self.personeller if x.id == pid), None)
                        if p:
                            self.cizelge[gun][s_idx] = p.ad
                            p.nobet_yaz(gun, gun_tipi, g_obj.ad, g_obj.base_name)
                    yazilan_sayisi += 1

    def tekli_dagitim(self, min_ara_gun: int):
        """Tekli atamaları yap - backtracking destekli"""
        sirali_gunler = self.gunleri_sirala()

        for gun in sirali_gunler:
            gun_tipi = self._get_gun_tipi(gun)
            bugun_atananlar = [x for x in self.cizelge[gun] if x is not None]

            for slot_idx, gorev in enumerate(self.gorevler):
                if self.cizelge[gun][slot_idx] is not None:
                    continue

                aday = self.en_uygun_adayi_sec(gun, gun_tipi, gorev, min_ara_gun, bugun_atananlar)
                if aday:
                    self.cizelge[gun][slot_idx] = aday.ad
                    aday.nobet_yaz(gun, gun_tipi, gorev.ad, gorev.base_name)
                    bugun_atananlar.append(aday.ad)
                elif min_ara_gun > 1:
                    geri_alinan = self._backtrack_komsular(gun, slot_idx, gorev, min_ara_gun)
                    if geri_alinan:
                        bugun_atananlar = [x for x in self.cizelge[gun] if x is not None]
                        aday = self.en_uygun_adayi_sec(gun, gun_tipi, gorev, min_ara_gun, bugun_atananlar)
                        if aday:
                            self.cizelge[gun][slot_idx] = aday.ad
                            aday.nobet_yaz(gun, gun_tipi, gorev.ad, gorev.base_name)
                            bugun_atananlar.append(aday.ad)

    def _backtrack_komsular(self, gun: int, slot_idx: int, gorev: GorevTanim, min_ara_gun: int) -> bool:
        """Komşu günlerdeki atamayı geri alarak yeni aday alanı aç.
        Hem önceki hem sonraki günleri tarar. max_backtrack_depth ile korunur."""
        max_backtrack_depth = 3  # Sonsuz döngü koruması

        # Hem önceki hem sonraki günleri tara
        aday_gunler = []
        for geri_gun in range(max(1, gun - min_ara_gun), gun):
            aday_gunler.append(geri_gun)
        for ileri_gun in range(gun + 1, min(gun + min_ara_gun + 1, self.days_in_month + 1)):
            aday_gunler.append(ileri_gun)

        denemeler = 0
        for geri_gun in aday_gunler:
            if denemeler >= max_backtrack_depth:
                break
            for geri_slot in range(len(self.gorevler) - 1, -1, -1):
                if denemeler >= max_backtrack_depth:
                    break
                if (geri_gun, geri_slot) in self.manuel_atamalar_set:
                    continue
                atanan_ad = self.cizelge[geri_gun][geri_slot]
                if atanan_ad is None:
                    continue
                kisi = None
                for p in self.personeller:
                    if p.ad == atanan_ad:
                        kisi = p
                        break
                if kisi is None:
                    continue
                denemeler += 1

                # Geri almayı dene, sonra aday var mı kontrol et
                geri_gun_tipi = self._get_gun_tipi(geri_gun)
                geri_gorev = self.gorevler[geri_slot]
                self._atama_geri_al(kisi, geri_gun, geri_gun_tipi, geri_gorev)
                self.cizelge[geri_gun][geri_slot] = None

                # Yeni aday var mı kontrol et
                gun_tipi = self._get_gun_tipi(gun)
                bugun_atananlar = [x for x in self.cizelge[gun] if x is not None]
                yeni_aday = self.en_uygun_adayi_sec(gun, gun_tipi, gorev, min_ara_gun, bugun_atananlar)
                if yeni_aday:
                    return True  # Başarılı, geri alma kalıcı

                # Başarısız - geri almayı undo yap
                self.cizelge[geri_gun][geri_slot] = kisi.ad
                kisi.nobet_yaz(geri_gun, geri_gun_tipi, geri_gorev.ad, geri_gorev.base_name)

        return False

    def _atama_geri_al(self, p: Personel, gun: int, gun_tipi: str, gorev: GorevTanim):
        """Bir atamayı geri al - personelin kotalarını güncelle"""
        p.atanan_gunler.discard(gun)
        p.kalan_toplam += 1

        if gun_tipi == "hici":
            p.kalan_hici += 1
        elif gun_tipi == "persembe":
            p.kalan_prs += 1
        elif gun_tipi == "cuma":
            p.kalan_cum += 1
        elif gun_tipi == "cumartesi":
            p.kalan_cmt += 1
        elif gun_tipi == "pazar":
            p.kalan_pzr += 1

        kontrol_adi = gorev.base_name if gorev.base_name else gorev.ad
        if kontrol_adi in p.kalan_roller:
            p.kalan_roller[kontrol_adi] += 1

        if p.atanan_gunler:
            p.son_nobet_gunu = max(p.atanan_gunler)
        else:
            p.son_nobet_gunu = -999

    def son_vurus(self):
        """Kalan boşlukları en esnek kuralla doldur (ara gün = 1)"""
        sirali_gunler = self.gunleri_sirala()

        for gun in sirali_gunler:
            gun_tipi = self._get_gun_tipi(gun)
            bugun_atananlar = [x for x in self.cizelge[gun] if x is not None]

            for slot_idx, gorev in enumerate(self.gorevler):
                if self.cizelge[gun][slot_idx] is not None:
                    continue

                aday = self.en_uygun_adayi_sec(gun, gun_tipi, gorev, 1, bugun_atananlar)
                if aday:
                    self.cizelge[gun][slot_idx] = aday.ad
                    aday.nobet_yaz(gun, gun_tipi, gorev.ad, gorev.base_name)
                    bugun_atananlar.append(aday.ad)

    def dagit(self):
        """
        Sadeleştirilmiş dağıtım - Gün tipi bazlı
        1. Grup dağıtımı (birlikte tutulacaklar)
        2. Tekli dağıtım (gün tipi kotalarına göre)
        3. Son vuruş (kalan boşluklar)
        """
        self.grup_dagitimi(self.ara_gun)
        self.tekli_dagitim(self.ara_gun)

        if self.ara_gun > 1:
            self.tekli_dagitim(self.ara_gun - 1)

        self.son_vurus()
