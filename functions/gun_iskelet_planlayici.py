"""
Kişi-gün bazlı ön nöbet iskeleti üretir.

Amaç:
- Hedef toplamları gerçek günlere dağıtmak
- Manuel atamaları, mazeretleri, ara günü ve günlük kapasiteyi dikkate almak
- Birlikte/ayrı kurallarını mümkün olduğunca ön planda çözmek
"""

from __future__ import annotations

from itertools import combinations
from typing import Dict, List, Optional, Set, Tuple

from solver_models import SolverAtama, SolverGorev, SolverKural, SolverPersonel
from utils import GUN_TIPLERI, SAAT_DEGERLERI, ESDEGER_TIP_GRUPLARI, find_matching_id, normalize_id


class GunIskeletPlanlayici:
    def __init__(
        self,
        gun_sayisi: int,
        gun_tipleri: Dict[int, str],
        personeller: List[SolverPersonel],
        gorevler: List[SolverGorev],
        hedefler_map: Dict[int, Dict],
        kurallar: Optional[List[SolverKural]] = None,
        manuel_atamalar: Optional[List[SolverAtama]] = None,
        ara_gun: int = 2,
        gorev_kisitlamalari: Optional[Dict[int, dict]] = None,
    ):
        self.gun_sayisi = gun_sayisi
        self.gun_tipleri = gun_tipleri
        self.personeller = {normalize_id(p.id): p for p in personeller}
        self.personel_listesi = personeller
        self.gorevler = gorevler
        self.hedefler_map = hedefler_map or {}
        self.kurallar = kurallar or []
        self.manuel_atamalar = manuel_atamalar or []
        self.ara_gun = ara_gun
        self.gorev_kisitlamalari = gorev_kisitlamalari or {}
        self.gunluk_kapasite = max(len(gorevler), 1)

        self.planlanan_gunler: Dict[int, Set[int]] = {normalize_id(p.id): set() for p in personeller}
        self.kilitli_gunler: Dict[int, Set[int]] = {normalize_id(p.id): set() for p in personeller}
        self.gun_yuku: Dict[int, Set[int]] = {g: set() for g in range(1, gun_sayisi + 1)}
        self.uyarilar: List[str] = []
        self.birlikte_raporu: List[Dict] = []
        self.esdeger_gecisler: List[Dict] = []

        # Rol dağıtımı veri yapıları
        self.role_slots: Dict[str, List[int]] = {}
        for idx, gorev in enumerate(gorevler):
            base = gorev.base_name if gorev.base_name else gorev.ad
            self.role_slots.setdefault(base, []).append(idx)
        self.personel_rol_gunleri: Dict[int, Dict[int, str]] = {
            normalize_id(p.id): {} for p in personeller
        }
        self.kalan_gorev_kotalari: Dict[int, Dict[str, int]] = {}

        self.ayri_ciftler = self._ayri_ciftleri_hazirla()
        self.birlikte_gruplari = self._birlikte_gruplari_hazirla()
        self.manuel_tip_sayac = {
            normalize_id(p.id): {tip: 0 for tip in GUN_TIPLERI}
            for p in personeller
        }
        self.kalan_tipler = {}
        self.kalan_toplam = {}

        self._manuel_gunleri_uygula()
        self._hedef_kalanlarini_hazirla()

    def _ayri_ciftleri_hazirla(self) -> Set[Tuple[int, int]]:
        ciftler: Set[Tuple[int, int]] = set()
        for kural in self.kurallar:
            if kural.tur != "ayri":
                continue
            valid_ids = []
            for raw_pid in kural.kisiler:
                matched = find_matching_id(raw_pid, self.personeller.keys())
                if matched is not None and matched not in valid_ids:
                    valid_ids.append(matched)
            for p1, p2 in combinations(sorted(valid_ids), 2):
                ciftler.add((p1, p2))
        return ciftler

    def _birlikte_gruplari_hazirla(self) -> List[List[int]]:
        gruplar: List[List[int]] = []
        for kural in self.kurallar:
            if kural.tur != "birlikte":
                continue
            valid_ids = []
            for raw_pid in kural.kisiler:
                matched = find_matching_id(raw_pid, self.personeller.keys())
                if matched is not None and matched not in valid_ids:
                    valid_ids.append(matched)
            if len(valid_ids) >= 2:
                gruplar.append(valid_ids)
        return gruplar

    def _manuel_gunleri_uygula(self) -> None:
        # Manuel atamaların hangi role yapıldığını takip et
        self.manuel_rol_sayac: Dict[int, Dict[str, int]] = {
            normalize_id(p.id): {} for p in self.personel_listesi
        }
        # Günlük rol sayacı: manuel atamalardan başlat
        self.gun_rol_sayac: Dict[int, Dict[str, int]] = {
            g: {} for g in range(1, self.gun_sayisi + 1)
        }
        for atama in self.manuel_atamalar:
            pid = find_matching_id(atama.personel_id, self.personeller.keys())
            gun = int(getattr(atama, "gun", 0) or 0)
            if pid is None or gun < 1 or gun > self.gun_sayisi:
                continue
            self.planlanan_gunler[pid].add(gun)
            self.kilitli_gunler[pid].add(gun)
            self.gun_yuku[gun].add(pid)
            tip = self.gun_tipleri.get(gun)
            if tip in self.manuel_tip_sayac[pid]:
                self.manuel_tip_sayac[pid][tip] += 1

            # Manuel atamanın görev adını bul ve rol sayacına ekle
            gorev_adi = getattr(atama, "gorev_adi", "") or ""
            slot_idx = getattr(atama, "slot_idx", None)
            rol_adi = None
            if slot_idx is not None and 0 <= slot_idx < len(self.gorevler):
                g = self.gorevler[slot_idx]
                rol_adi = g.base_name if g.base_name else g.ad
            elif gorev_adi:
                # gorev_adi'ndan base_name bul
                for g in self.gorevler:
                    if g.ad == gorev_adi or g.base_name == gorev_adi:
                        rol_adi = g.base_name if g.base_name else g.ad
                        break
            if rol_adi:
                self.manuel_rol_sayac[pid][rol_adi] = (
                    self.manuel_rol_sayac[pid].get(rol_adi, 0) + 1
                )
                # Manuel atamaları personel_rol_gunleri'ne de işle
                self.personel_rol_gunleri[pid][gun] = rol_adi
                # Günlük rol sayacına ekle
                self.gun_rol_sayac[gun][rol_adi] = (
                    self.gun_rol_sayac[gun].get(rol_adi, 0) + 1
                )

    def _hedef_kalanlarini_hazirla(self) -> None:
        for p in self.personel_listesi:
            pid = normalize_id(p.id)
            hedef = self.hedefler_map.get(pid, {})
            hedef_tipler = dict(hedef.get("hedef_tipler", {}) or {})
            kalan_tip = {}
            for tip in GUN_TIPLERI:
                hedef_tip = int(hedef_tipler.get(tip, 0) or 0)
                kalan_tip[tip] = max(0, hedef_tip - self.manuel_tip_sayac[pid].get(tip, 0))
            self.kalan_tipler[pid] = kalan_tip
            hedef_toplam = int(hedef.get("hedef_toplam", sum(hedef_tipler.values())) or 0)
            self.kalan_toplam[pid] = max(0, hedef_toplam - len(self.kilitli_gunler[pid]))

            manuel_toplam = len(self.kilitli_gunler[pid])
            if manuel_toplam > hedef_toplam:
                self.uyarilar.append(
                    f"{p.ad}: manuel gün sayısı hedef toplamı aşıyor "
                    f"(manuel={manuel_toplam}, hedef={hedef_toplam})."
                )

            # Görev kotalarından kalan_gorev_kotalari hazırla
            # Manuel atamalardan yapılan rol atamalarını düş
            gorev_kotalari = dict(hedef.get("gorev_kotalari", {}) or {})
            if gorev_kotalari:
                manuel_roller = self.manuel_rol_sayac.get(pid, {})
                self.kalan_gorev_kotalari[pid] = {}
                for k, v in gorev_kotalari.items():
                    hedef_kota = max(0, int(v or 0))
                    manuel_kullanim = manuel_roller.get(str(k), 0)
                    kalan = max(0, hedef_kota - manuel_kullanim)
                    if kalan > 0:
                        self.kalan_gorev_kotalari[pid][str(k)] = kalan

    def _ara_gun_ihlali_var_mi(self, pid: int, gun: int) -> bool:
        if self.ara_gun <= 1:
            return False
        for mevcut in self.planlanan_gunler[pid]:
            if mevcut != gun and abs(mevcut - gun) < self.ara_gun:
                return True
        return False

    def _ayri_ihlali_var_mi(self, pid: int, gun: int) -> bool:
        for diger in self.gun_yuku.get(gun, set()):
            pair = tuple(sorted((pid, diger)))
            if pair in self.ayri_ciftler:
                return True
        return False

    def _gun_kapasitesi_var_mi(self, gun: int, eklenecek: int = 1) -> bool:
        return len(self.gun_yuku[gun]) + eklenecek <= self.gunluk_kapasite

    def _gun_uygun_mu(self, pid: int, gun: int, ignore_ayri: bool = False) -> bool:
        if gun in self.planlanan_gunler[pid]:
            return False
        personel = self.personeller.get(pid)
        if personel is None:
            return False
        if gun in personel.mazeret_gunleri:
            return False
        if not self._gun_kapasitesi_var_mi(gun):
            return False
        if self._ara_gun_ihlali_var_mi(pid, gun):
            return False
        if not ignore_ayri and self._ayri_ihlali_var_mi(pid, gun):
            return False
        return True

    def _hafta_indexi(self, gun: int) -> int:
        return (gun - 1) // 7

    def _gun_skoru(self, pid: int, gun: int) -> Tuple[int, int, int, int]:
        mevcut_gunler = self.planlanan_gunler[pid]
        min_gap = min((abs(gun - g) for g in mevcut_gunler), default=self.gun_sayisi)
        ayni_hafta = sum(1 for g in mevcut_gunler if self._hafta_indexi(g) == self._hafta_indexi(gun))
        yuk = len(self.gun_yuku[gun])
        tip = self.gun_tipleri.get(gun)
        tip_yuku = sum(
            1 for g in self.planlanan_gunler[pid]
            if self.gun_tipleri.get(g) == tip
        )
        return (yuk, ayni_hafta, -min_gap, tip_yuku)

    def _adaya_gunler(
        self,
        pid: int,
        tip: Optional[str] = None,
        ignore_ayri: bool = False,
    ) -> List[int]:
        gunler = []
        for gun in range(1, self.gun_sayisi + 1):
            if tip and self.gun_tipleri.get(gun) != tip:
                continue
            if self._gun_uygun_mu(pid, gun, ignore_ayri=ignore_ayri):
                gunler.append(gun)
        gunler.sort(key=lambda gun: (self._gun_skoru(pid, gun), gun))
        return gunler

    def _gune_ata(self, pid: int, gun: int, kilitli: bool = False,
                  kaynak_tip: Optional[str] = None) -> bool:
        tip = self.gun_tipleri.get(gun)
        if tip not in GUN_TIPLERI:
            return False
        if self.kalan_toplam.get(pid, 0) <= 0 and gun not in self.kilitli_gunler[pid]:
            return False

        self.planlanan_gunler[pid].add(gun)
        self.gun_yuku[gun].add(pid)
        if kilitli:
            self.kilitli_gunler[pid].add(gun)

        if gun not in self.kilitli_gunler[pid]:
            # Esdeger gecis varsa kaynak tipin kotasini dus, yoksa gunun kendi tipi
            dusulen_tip = kaynak_tip if kaynak_tip and kaynak_tip != tip else tip
            if self.kalan_tipler[pid].get(dusulen_tip, 0) > 0:
                self.kalan_tipler[pid][dusulen_tip] -= 1
            elif self.kalan_tipler[pid].get(tip, 0) > 0:
                self.kalan_tipler[pid][tip] -= 1
            self.kalan_toplam[pid] = max(0, self.kalan_toplam[pid] - 1)
        return True

    def _birlikte_gunlerini_yerlestir(self) -> None:
        for grup in self.birlikte_gruplari:
            if len(grup) > self.gunluk_kapasite:
                self.birlikte_raporu.append({
                    "kisiler": grup,
                    "hedef": 0,
                    "yerlesen": [],
                    "uyari": "Grup boyutu günlük kapasiteyi aşıyor.",
                })
                continue

            hedef = min(self.kalan_toplam.get(pid, 0) for pid in grup)
            yerlesen: List[int] = []
            while len(yerlesen) < hedef:
                adaylar = []
                for gun in range(1, self.gun_sayisi + 1):
                    tip = self.gun_tipleri.get(gun)
                    if not self._gun_kapasitesi_var_mi(gun, len(grup)):
                        continue
                    if any(not self._gun_uygun_mu(pid, gun) for pid in grup):
                        continue
                    if any(self.kalan_tipler[pid].get(tip, 0) <= 0 for pid in grup):
                        continue
                    skor = (
                        len(self.gun_yuku[gun]),
                        sum(self._hafta_indexi(gun) == self._hafta_indexi(g) for pid in grup for g in self.planlanan_gunler[pid]),
                        gun,
                    )
                    adaylar.append((skor, gun))

                if not adaylar:
                    break

                _, secilen_gun = min(adaylar, key=lambda item: item[0])
                for pid in grup:
                    self._gune_ata(pid, secilen_gun)
                yerlesen.append(secilen_gun)

            self.birlikte_raporu.append({
                "kisiler": grup,
                "hedef": hedef,
                "yerlesen": yerlesen,
                "uyari": (
                    None if len(yerlesen) == hedef else
                    f"Birlikte hedefi tam karşılanamadı ({len(yerlesen)}/{hedef})."
                ),
            })

    def _tip_onceligi(self, pid: int) -> List[str]:
        return sorted(
            GUN_TIPLERI,
            key=lambda tip: (-self.kalan_tipler[pid].get(tip, 0), len(self._adaya_gunler(pid, tip=tip)))
        )

    def _bireysel_gunleri_yerlestir(self) -> None:
        while True:
            aktifler = [pid for pid, kalan in self.kalan_toplam.items() if kalan > 0]
            if not aktifler:
                break

            aktifler.sort(
                key=lambda pid: (
                    sum(len(self._adaya_gunler(pid, tip=tip)) for tip in GUN_TIPLERI),
                    -self.kalan_toplam[pid],
                    str(self.personeller[pid].ad),
                )
            )

            ilerleme = False
            for pid in aktifler:
                tipler = self._tip_onceligi(pid)
                secilen = None
                secilen_kaynak_tip = None
                secilen_hedef_tip = None

                # 1. Asil tip: kalan kotasi olan tiplerden uygun gun ara
                for tip in tipler:
                    if self.kalan_tipler[pid].get(tip, 0) <= 0:
                        continue
                    adaylar = self._adaya_gunler(pid, tip=tip)
                    if adaylar:
                        secilen = adaylar[0]
                        secilen_kaynak_tip = tip
                        secilen_hedef_tip = tip
                        break

                # 2. Esdeger tip fallback: asil tip bulunamadiysa
                #    saat bazli esdeger tipe gec
                if secilen is None:
                    for tip in tipler:
                        if self.kalan_tipler[pid].get(tip, 0) <= 0:
                            continue
                        esdegerler = ESDEGER_TIP_GRUPLARI.get(tip, [])
                        for esdeger_tip in esdegerler:
                            adaylar = self._adaya_gunler(pid, tip=esdeger_tip)
                            if adaylar:
                                secilen = adaylar[0]
                                secilen_kaynak_tip = tip
                                secilen_hedef_tip = esdeger_tip
                                break
                        if secilen is not None:
                            break

                # 3. Herhangi bir tipten (kalan kotasi olmasa bile)
                if secilen is None:
                    for tip in tipler:
                        adaylar = self._adaya_gunler(pid, tip=tip)
                        if adaylar:
                            secilen = adaylar[0]
                            secilen_kaynak_tip = tip
                            secilen_hedef_tip = tip
                            break

                # 4. Son fallback: tip filtresi olmadan
                if secilen is None:
                    adaylar = self._adaya_gunler(pid, tip=None)
                    if adaylar:
                        secilen = adaylar[0]

                if secilen is None:
                    continue

                # Esdeger gecis mi? Kaynak tip ile hedef tip farkli ise kaydet
                if (secilen_kaynak_tip and secilen_hedef_tip
                        and secilen_kaynak_tip != secilen_hedef_tip):
                    self.esdeger_gecisler.append({
                        "personel_id": pid,
                        "personel_ad": self.personeller[pid].ad,
                        "gun": secilen,
                        "kaynak_tip": secilen_kaynak_tip,
                        "hedef_tip": secilen_hedef_tip,
                        "kaynak_saat": SAAT_DEGERLERI.get(secilen_kaynak_tip, 0),
                        "hedef_saat": SAAT_DEGERLERI.get(secilen_hedef_tip, 0),
                    })

                if self._gune_ata(pid, secilen, kaynak_tip=secilen_kaynak_tip):
                    ilerleme = True

            if not ilerleme:
                break

    def _gun_rol_kapasitesi_var_mi(self, gun: int, rol: str) -> bool:
        """O gün o role daha fazla kişi önerilip önerilemeyeceğini kontrol et."""
        slot_sayisi = len(self.role_slots.get(rol, []))
        if slot_sayisi == 0:
            return False
        mevcut = self.gun_rol_sayac.get(gun, {}).get(rol, 0)
        return mevcut < slot_sayisi

    def _rol_dagitimi_yap(self) -> None:
        """Her kişi-gün pair'i için uygun görev ailesi/rol ata.

        Kurallar:
        - Manuel atamalar zaten personel_rol_gunleri'ne işlenmiş, atla
        - Günlük rol kapasitesi aşılmamalı (role_slots slot sayısı kadar)
        - Kısıtlı görev > kota dolmamış rol > taşma görevi > fallback
        """
        if not self.role_slots:
            return

        rol_isimleri = list(self.role_slots.keys())

        for p in self.personel_listesi:
            pid = normalize_id(p.id)
            gunler = sorted(self.planlanan_gunler.get(pid, set()))
            if not gunler:
                continue

            # Kişinin kısıtlı görevi ve taşma görevi
            kisit = self.gorev_kisitlamalari.get(pid, {})
            kisitli_gorev = kisit.get("gorevAdi") if kisit else (
                getattr(p, "kisitli_gorev", None)
            )
            tasma_gorevi = kisit.get("tasmaGorevi") if kisit else (
                getattr(p, "tasma_gorevi", None)
            )

            kalan_kotalar = dict(self.kalan_gorev_kotalari.get(pid, {}))

            for gun in gunler:
                # Manuel atama zaten işlendi, atla
                if gun in self.personel_rol_gunleri.get(pid, {}):
                    continue

                atanan_rol = None

                # 1. kisitli_gorev varsa, kotası dolmamış ve günlük kapasitesi varsa
                if kisitli_gorev and kisitli_gorev in self.role_slots:
                    if (kalan_kotalar.get(kisitli_gorev, 1) > 0
                            and self._gun_rol_kapasitesi_var_mi(gun, kisitli_gorev)):
                        atanan_rol = kisitli_gorev
                    elif tasma_gorevi and tasma_gorevi in self.role_slots:
                        if (kalan_kotalar.get(tasma_gorevi, 1) > 0
                                and self._gun_rol_kapasitesi_var_mi(gun, tasma_gorevi)):
                            atanan_rol = tasma_gorevi

                # 2. Kısıtlı görev yoksa, kotası dolmamış VE günlük kapasitesi olan role öncelik
                if atanan_rol is None:
                    en_uygun = None
                    en_uygun_kalan = -1
                    for rol in rol_isimleri:
                        if not self._gun_rol_kapasitesi_var_mi(gun, rol):
                            continue
                        kalan = kalan_kotalar.get(rol, 0)
                        if kalan > en_uygun_kalan:
                            en_uygun_kalan = kalan
                            en_uygun = rol
                    if en_uygun is not None:
                        atanan_rol = en_uygun

                # 3. Fallback: günlük kapasitesi olan herhangi bir rol
                if atanan_rol is None:
                    for rol in rol_isimleri:
                        if self._gun_rol_kapasitesi_var_mi(gun, rol):
                            atanan_rol = rol
                            break

                if atanan_rol:
                    self.personel_rol_gunleri[pid][gun] = atanan_rol
                    if atanan_rol in kalan_kotalar and kalan_kotalar[atanan_rol] > 0:
                        kalan_kotalar[atanan_rol] -= 1
                    # Günlük rol sayacını güncelle
                    gun_sayac = self.gun_rol_sayac.setdefault(gun, {})
                    gun_sayac[atanan_rol] = gun_sayac.get(atanan_rol, 0) + 1

    def _personel_durumlari(self) -> Dict[str, Dict]:
        durumlar = {}
        for p in self.personel_listesi:
            pid = normalize_id(p.id)
            hedef = self.hedefler_map.get(pid, {})
            planlanan = sorted(self.planlanan_gunler[pid])
            hedef_toplam = int(hedef.get("hedef_toplam", 0) or 0)
            uygulanabilir = len(planlanan) == hedef_toplam
            durumlar[str(pid)] = {
                "personel_id": pid,
                "personel_ad": p.ad,
                "hedef_toplam": hedef_toplam,
                "planlanan_toplam": len(planlanan),
                "planlanan_gunler": planlanan,
                "kilitli_gunler": sorted(self.kilitli_gunler[pid]),
                "kalan_toplam": self.kalan_toplam[pid],
                "kalan_tipler": dict(self.kalan_tipler[pid]),
                "uygulanabilir": uygulanabilir,
            }
        return durumlar

    def planla(self) -> Dict:
        self._birlikte_gunlerini_yerlestir()
        self._bireysel_gunleri_yerlestir()
        self._rol_dagitimi_yap()

        personel_durumlari = self._personel_durumlari()
        uygulanabilirler = [
            int(pid) for pid, durum in personel_durumlari.items()
            if durum["uygulanabilir"]
        ]
        eksikler = [
            durum for durum in personel_durumlari.values()
            if not durum["uygulanabilir"]
        ]

        if eksikler:
            for durum in eksikler:
                self.uyarilar.append(
                    f"{durum['personel_ad']}: günlük iskelet eksik "
                    f"({durum['planlanan_toplam']}/{durum['hedef_toplam']})."
                )

        gun_yukleri = {
            str(gun): {
                "planlanan": len(self.gun_yuku[gun]),
                "kapasite": self.gunluk_kapasite,
                "personel_ids": sorted(self.gun_yuku[gun]),
                "tip": self.gun_tipleri.get(gun),
            }
            for gun in range(1, self.gun_sayisi + 1)
        }

        return {
            "aktif": True,
            "kullanilabilir": len(eksikler) == 0,
            "uygulanabilir_personeller": sorted(uygulanabilirler),
            "personel_gunleri": {
                str(pid): sorted(gunler)
                for pid, gunler in self.planlanan_gunler.items()
            },
            "kilitli_gunler": {
                str(pid): sorted(gunler)
                for pid, gunler in self.kilitli_gunler.items()
            },
            "personel_rol_gunleri": {
                str(pid): {str(gun): rol for gun, rol in rol_gunleri.items()}
                for pid, rol_gunleri in self.personel_rol_gunleri.items()
                if rol_gunleri
            },
            "personel_durumlari": personel_durumlari,
            "gun_yukleri": gun_yukleri,
            "birlikte_raporu": self.birlikte_raporu,
            "esdeger_gecisler": self.esdeger_gecisler,
            "esdeger_gecis_sayisi": len(self.esdeger_gecisler),
            "uyarilar": self.uyarilar,
        }
