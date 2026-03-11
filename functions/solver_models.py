"""
Solver veri modelleri ve ağırlık sabitleri.
Tüm solver modülleri tarafından paylaşılan dataclass'lar burada tanımlıdır.
"""

from dataclasses import dataclass, field
from typing import List, Dict, Set, Optional


# ============================================
# AĞIRLIK SABİTLERİ
# ============================================

WEIGHT_GOREV_KOTA = 1000
WEIGHT_GUN_TIPI = 500
WEIGHT_YILLIK = 400    # Yıllık dengeleme (geçmiş ay eksiklerini eşitle)
WEIGHT_HOMOJEN = 300   # Nöbetleri ay geneline yayma
WEIGHT_PANIK = 250     # Sıkışık kişilere öncelik
WEIGHT_TOPLAM = 100
WEIGHT_BIRLIKTE = 50


# ============================================
# DATACLASS'LAR
# ============================================

@dataclass
class SolverPersonel:
    id: int
    ad: str
    mazeret_gunleri: Set[int] = field(default_factory=set)
    kisitli_gorev: Optional[str] = None
    tasma_gorevi: Optional[str] = None
    hedef_tipler: Dict[str, int] = field(default_factory=dict)
    gorev_kotalari: Dict[str, int] = field(default_factory=dict)
    musait_gunler: Set[int] = field(default_factory=set)
    musait_tipler: Dict[str, int] = field(default_factory=dict)
    yillik_gerceklesen: Dict[str, int] = field(default_factory=dict)
    gecmis_gorevler: Dict[str, int] = field(default_factory=dict)

@dataclass
class SolverGorev:
    id: int
    ad: str
    slot_idx: int
    base_name: str = ""
    exclusive: bool = False
    ayri_bina: bool = False

@dataclass
class SolverKural:
    tur: str
    kisiler: List[int] = field(default_factory=list)

@dataclass
class SolverAtama:
    personel_id: int
    gun: int
    slot_idx: int
    gorev_adi: str = ""

@dataclass
class HedefSonuc:
    basarili: bool
    hedefler: List[Dict]
    birlikte_atamalar: List[Dict]
    gorev_kotalari: Dict
    istatistikler: Dict
    mesaj: str

@dataclass
class SolverSonuc:
    basarili: bool
    atamalar: List[Dict]
    istatistikler: Dict
    sure_ms: int
    mesaj: str
