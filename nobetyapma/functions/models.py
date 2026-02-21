"""
Greedy çözücüde kullanılan veri yapıları — proje bağımlılığı yok (yaprak modül).
"""

from dataclasses import dataclass, field
from typing import Dict, Set


@dataclass
class GorevTanim:
    id: int
    ad: str
    slot_index: int
    base_name: str = ""
    ayri_bina: bool = False


@dataclass
class Personel:
    """Personel veri yapısı - Sadece gün tipi bazlı (WE/WD kaldırıldı)"""
    id: int
    ad: str
    hedef_toplam: int
    hedef_hici: int
    hedef_prs: int
    hedef_cum: int
    hedef_cmt: int
    hedef_pzr: int
    hedef_roller: Dict[str, int]

    kalan_toplam: int = 0
    kalan_hici: int = 0
    kalan_prs: int = 0
    kalan_cum: int = 0
    kalan_cmt: int = 0
    kalan_pzr: int = 0
    kalan_roller: Dict[str, int] = field(default_factory=dict)

    mazeret_gunleri: Set[int] = field(default_factory=set)
    atanan_gunler: Set[int] = field(default_factory=set)
    son_nobet_gunu: int = -999
    mazeret_sayisi: int = 0
    yillik_toplam: int = 0

    devir: Dict[str, int] = field(default_factory=dict)

    def __post_init__(self):
        self.kalan_toplam = self.hedef_toplam
        self.kalan_hici = self.hedef_hici
        self.kalan_prs = self.hedef_prs
        self.kalan_cum = self.hedef_cum
        self.kalan_cmt = self.hedef_cmt
        self.kalan_pzr = self.hedef_pzr
        self.kalan_roller = self.hedef_roller.copy()
        if isinstance(self.mazeret_gunleri, list):
            self.mazeret_gunleri = set(self.mazeret_gunleri)
        self.mazeret_sayisi = len(self.mazeret_gunleri)

    def __hash__(self):
        return hash(self.id)

    def __eq__(self, other):
        if not isinstance(other, Personel): return False
        return self.id == other.id

    def kota_kontrol(self, gun_tipi: str, gorev_adi: str, gorev_base_name: str = "") -> bool:
        """Gün tipi bazlı kota kontrolü"""
        if self.kalan_toplam <= 0: return False

        if gun_tipi == "hici" and self.kalan_hici <= 0: return False
        if gun_tipi == "persembe" and self.kalan_prs <= 0: return False
        if gun_tipi == "cuma" and self.kalan_cum <= 0: return False
        if gun_tipi == "cumartesi" and self.kalan_cmt <= 0: return False
        if gun_tipi == "pazar" and self.kalan_pzr <= 0: return False

        kontrol_adi = gorev_base_name if gorev_base_name else gorev_adi
        is_generic = (kontrol_adi.startswith("Nöbetçi") or
                      kontrol_adi.startswith("Nöbet Yeri") or
                      kontrol_adi == "Genel")

        if not is_generic:
            if self.kalan_roller.get(kontrol_adi, 0) <= 0:
                return False
        return True

    def nobet_yaz(self, gun: int, gun_tipi: str, gorev_adi: str, gorev_base_name: str = ""):
        """Nöbet yaz - sadece gün tipi kotalarını güncelle"""
        self.atanan_gunler.add(gun)
        self.son_nobet_gunu = gun
        self.kalan_toplam -= 1

        if gun_tipi == "hici":
            self.kalan_hici -= 1
        elif gun_tipi == "persembe":
            self.kalan_prs -= 1
        elif gun_tipi == "cuma":
            self.kalan_cum -= 1
        elif gun_tipi == "cumartesi":
            self.kalan_cmt -= 1
        elif gun_tipi == "pazar":
            self.kalan_pzr -= 1

        kontrol_adi = gorev_base_name if gorev_base_name else gorev_adi
        if kontrol_adi in self.kalan_roller:
            self.kalan_roller[kontrol_adi] -= 1
