"""
Ortak yardımcı fonksiyonlar — proje bağımlılığı yok (yaprak modül).
"""

from datetime import date, timedelta
from typing import Dict, List, Set
import math
import hashlib
import re


# ============================================
# SABITLER
# ============================================

GUN_TIPLERI = ['hici', 'prs', 'cum', 'cmt', 'pzr']

SAAT_DEGERLERI = {
    'hici': 8, 'prs': 8, 'cum': 16, 'cmt': 24, 'pzr': 16
}

# Saat bazli esdeger gun tipi gecis haritasi.
# Ayni saat degerine sahip tipler birbirine gecebilir (soft fallback).
# hici <-> prs (8s), cum <-> pzr (16s), cmt -> cum/pzr (24s son care)
ESDEGER_TIP_GRUPLARI = {
    'hici': ['prs'],
    'prs':  ['hici'],
    'cum':  ['pzr'],
    'pzr':  ['cum'],
    'cmt':  ['cum', 'pzr'],
}

BIRLIKTE_ESDEGER_GOREV_AILESI = frozenset({
    'AMELIYATHANE',
    'MAVI KOD',
    'KVC',
})
BIRLIKTE_ESDEGER_GOREV_AILE_ADI = 'AMELIYATHANE_MAVI_KOD_KVC'

_TURKCE_ASCII_TRANSLATION = str.maketrans({
    'Ç': 'C', 'Ğ': 'G', 'İ': 'I', 'Ö': 'O', 'Ş': 'S', 'Ü': 'U',
    'ç': 'C', 'ğ': 'G', 'ı': 'I', 'i': 'I', 'ö': 'O', 'ş': 'S', 'ü': 'U',
})


# ============================================
# ID NORMALİZASYON
# ============================================

def normalize_id(pid) -> int:
    """ID'yi int'e normalize et"""
    if pid is None:
        return None
    if isinstance(pid, bool):
        return int(pid)

    if isinstance(pid, int):
        return pid

    if isinstance(pid, float):
        if math.isfinite(pid) and pid.is_integer():
            return int(pid)
        token = f"float:{pid:.15g}" if math.isfinite(pid) else f"float:{pid}"
        return int(hashlib.sha1(token.encode("utf-8")).hexdigest()[:15], 16)

    raw = str(pid).strip()
    if not raw:
        return int(hashlib.sha1(b"text:").hexdigest()[:15], 16)

    if raw.lstrip("+-").isdigit():
        return int(raw, 10)

    try:
        numeric = float(raw)
        if math.isfinite(numeric) and numeric.is_integer():
            return int(numeric)
        if math.isfinite(numeric):
            token = f"float:{numeric:.15g}"
            return int(hashlib.sha1(token.encode("utf-8")).hexdigest()[:15], 16)
    except (ValueError, TypeError):
        pass

    token = f"text:{raw}"
    return int(hashlib.sha1(token.encode("utf-8")).hexdigest()[:15], 16)


def ids_match(id1, id2) -> bool:
    """İki ID'nin eşit olup olmadığını kontrol et"""
    if id1 is None or id2 is None:
        return False
    return normalize_id(id1) == normalize_id(id2)


def find_matching_id(target_id, id_collection):
    """Bir ID'yi koleksiyonda bul"""
    target_norm = normalize_id(target_id)
    for pid in id_collection:
        if normalize_id(pid) == target_norm:
            return pid
    return None


def canonicalize_role_name(role_name) -> str:
    """Görev adını birlikte/eşdeğerlik kontrolleri için normalize et."""
    if role_name is None:
        return ""

    raw = re.sub(r"\s+#\d+$", "", str(role_name)).strip()
    if not raw:
        return ""

    upper = re.sub(r"\s+", " ", raw.upper().translate(_TURKCE_ASCII_TRANSLATION)).strip()
    if upper == 'AMELIYATHANE':
        return 'AMELIYATHANE'
    if upper == 'MAVI KOD':
        return 'MAVI KOD'
    if upper == 'KVC':
        return 'KVC'
    return upper


def birlikte_aile_anahtari(role_name) -> str:
    """Birlikte kuralları için görev ailesi anahtarını döndür."""
    canonical = canonicalize_role_name(role_name)
    if canonical in BIRLIKTE_ESDEGER_GOREV_AILESI:
        return BIRLIKTE_ESDEGER_GOREV_AILE_ADI
    return canonical


# ============================================
# GENEL YARDIMCILAR
# ============================================

def _safe_int(value, default=0):
    try:
        return int(value)
    except (ValueError, TypeError):
        try:
            return int(float(value))
        except (ValueError, TypeError):
            return default


def get_days_in_month(yil, ay):
    if ay == 12:
        d = date(yil + 1, 1, 1)
    else:
        d = date(yil, ay + 1, 1)
    return (d - timedelta(days=1)).day


def gun_adi_bul(yil, ay, gun, resmi_tatiller):
    for rt in resmi_tatiller:
        if _safe_int(rt.get('gun', 0), 0) == gun:
            tip = rt.get('tip', '')
            if tip == "pzr": return "Pazar"
            if tip == "cmt": return "Cumartesi"
            if tip == "cum": return "Cuma"
            if tip == "prs": return "Persembe"
    dt = date(yil, ay, gun)
    gunler = ["Pazartesi", "Sali", "Carsamba", "Persembe", "Cuma", "Cumartesi", "Pazar"]
    return gunler[dt.weekday()]


def gun_tipi_hesapla(yil: int, ay: int, gun: int, resmi_tatiller: list) -> str:
    """Gün tipini hesapla (hici, prs, cum, cmt, pzr)"""
    for rt in resmi_tatiller:
        if _safe_int(rt.get('gun', 0), 0) == gun:
            tip = rt.get('tip', '')
            if tip == 'pzr': return 'pzr'
            if tip == 'cmt': return 'cmt'
            if tip == 'cum': return 'cum'
            if tip == 'prs': return 'prs'

    dt = date(yil, ay, gun)
    weekday = dt.weekday()

    if weekday == 6:
        return 'pzr'
    elif weekday == 5:
        return 'cmt'
    elif weekday == 4:
        return 'cum'
    elif weekday == 3:
        return 'prs'
    else:
        return 'hici'


def _extract_mazeret_gunleri(personel_data: Dict) -> Set[int]:
    mazeretler = set()
    for key in ['mazeretler', 'yillikIzinler', 'nobetIzinleri']:
        raw = personel_data.get(key, [])
        if isinstance(raw, list):
            for x in raw:
                try:
                    mazeretler.add(int(x))
                except (ValueError, TypeError):
                    continue
        elif isinstance(raw, dict):
            for k in raw.keys():
                try:
                    mazeretler.add(int(k))
                except (ValueError, TypeError):
                    continue
    return mazeretler


def build_personel_lookup(personeller) -> Dict:
    """Personel listesinden ad->id ve normalized_id->id lookup tablosu oluştur.
    Aynı personel listesi ile çok sayıda _resolve_personel_id çağrısı yapılacaksa
    bu fonksiyonla önceden cache oluşturup parametre olarak verin."""
    ad_map = {}
    id_map = {}
    for p in personeller:
        ad = getattr(p, "ad", None)
        pid = getattr(p, "id", None)
        if ad and ad not in ad_map:
            ad_map[ad] = pid
        if pid is not None:
            norm = normalize_id(pid)
            if norm not in id_map:
                id_map[norm] = pid
    return {"ad_map": ad_map, "id_map": id_map}


def _resolve_personel_id(raw_ref, personeller, require_existing=True, _cache=None):
    if raw_ref is None:
        return None

    if isinstance(raw_ref, str):
        ref = raw_ref.strip()
        if not ref:
            return None
        # Cache varsa O(1) ad lookup
        if _cache:
            found = _cache["ad_map"].get(ref)
            if found is not None:
                return found
        else:
            for p in personeller:
                if getattr(p, "ad", None) == ref:
                    return getattr(p, "id", None)
        raw_ref = ref

    normalized = normalize_id(raw_ref)
    if not require_existing:
        return normalized

    # Cache varsa O(1) id lookup
    if _cache:
        found = _cache["id_map"].get(normalized)
        if found is not None:
            return found
        return None

    for p in personeller:
        if ids_match(getattr(p, "id", None), normalized):
            return getattr(p, "id", None)
    return None


def _find_duplicate_personel_ids(personeller) -> List[int]:
    seen = set()
    duplicates = []
    for p in personeller:
        pid = getattr(p, "id", None)
        if pid in seen and pid not in duplicates:
            duplicates.append(pid)
        seen.add(pid)
    return duplicates
