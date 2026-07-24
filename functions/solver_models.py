"""
Solver veri modelleri ve ağırlık sabitleri.
Tüm solver modülleri tarafından paylaşılan dataclass'lar burada tanımlıdır.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Set


# ============================================
# AĞIRLIK SABİTLERİ
# ============================================

WEIGHT_GOREV_KOTA = 1000
WEIGHT_GUN_TIPI = 500
WEIGHT_YILLIK = 400    # Yıllık dengeleme (geçmiş ay eksiklerini eşitle)
WEIGHT_HOMOJEN = 300   # Nöbetleri ay geneline yayma
WEIGHT_MAX_ARA = 1200  # 112: iki nöbet arası sabit üst sınır (soft, kör-hard değil)
WEIGHT_IZIN_YERLESIM = 400  # 112: yıllık izin öncesi boşluk / sonrası ilk iş günü tercihi
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
    # Gün -> izin türü ("izin"/"egitim"/"rapor"/"nobet_izni"/"mazeret").
    # mazeret_gunleri'nin türlü kırılımı; madde 2 (mesai-borcu) + madde 5
    # (izin yerleşimi) tüketir. Boş = tür bilgisi yok (geriye uyumlu).
    izin_turleri: Dict[int, str] = field(default_factory=dict)
    kisitli_gorev: Optional[str] = None
    tasma_gorevi: Optional[str] = None
    yetkili_gorevler: Set[str] = field(default_factory=set)
    is_yuku_katsayisi: float = 1.0
    min_nobet: int = 0
    max_nobet: Optional[int] = None
    esitlemeden_muaf: bool = False
    adalet_grubu: str = "normal"
    gecmis_veri_durumu: str = "bilinmiyor"
    hedef_tipler: Dict[str, int] = field(default_factory=dict)
    gorev_kotalari: Dict[str, int] = field(default_factory=dict)
    musait_gunler: Set[int] = field(default_factory=set)
    musait_tipler: Dict[str, int] = field(default_factory=dict)
    yillik_gerceklesen: Dict[str, int] = field(default_factory=dict)
    gecmis_gorevler: Dict[str, int] = field(default_factory=dict)
    gecmis_donem: Dict[str, Any] = field(default_factory=dict)

@dataclass
class SolverGorev:
    id: int
    ad: str
    slot_idx: int
    base_name: str = ""
    exclusive: bool = False
    ayri_bina: bool = False
    bina_id: str = "ANA_BINA"
    kritik: bool = False
    istisna_politikasi: str = "kullanici_onayli"
    alternatif_gorevler: List[Dict[str, Any]] = field(default_factory=list)

@dataclass
class SolverKural:
    tur: str
    kisiler: List[int] = field(default_factory=list)
    politika: str = "kullanici_onayli"
    asla_gevsetme: bool = False

@dataclass
class SolverAtama:
    personel_id: int
    gun: int
    slot_idx: int
    gorev_adi: str = ""
    mazeret_onayli: bool = False

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


@dataclass
class PlanPersonel:
    personel_id: int
    ad: str
    hedef_toplam: int = 0
    hedef_tipler: Dict[str, int] = field(default_factory=dict)
    gorev_kotalari: Dict[str, int] = field(default_factory=dict)
    kilitli: bool = False
    kaynak: str = "otomatik"
    kilitli_gunler: List[int] = field(default_factory=list)
    onerilen_gunler: List[int] = field(default_factory=list)
    onerilen_rol_gunleri: Dict[int, str] = field(default_factory=dict)
    gun_iskeleti_uygulanabilir: bool = False


@dataclass
class PlanKontrati:
    plan_hash: str
    kaynak: str
    olusturulan_ara_gun: int
    hedefler: Dict[int, Dict] = field(default_factory=dict)
    personeller: List[PlanPersonel] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)
    uygulama: Dict[str, Any] = field(default_factory=dict)
    istatistikler: Dict[str, Any] = field(default_factory=dict)
    gun_iskeleti: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
