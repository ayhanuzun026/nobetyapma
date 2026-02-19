"""
Ortak yardımcı fonksiyonlar — proje bağımlılığı yok (yaprak modül).
"""

from datetime import date, timedelta
from typing import Dict, List, Set
import math
import hashlib


# ============================================
# SABITLER
# ============================================

GUN_TIPLERI = ['hici', 'prs', 'cum', 'cmt', 'pzr']

SAAT_DEGERLERI = {
    'hici': 8, 'prs': 8, 'cum': 16, 'cmt': 24, 'pzr': 16
}


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


def _resolve_personel_id(raw_ref, personeller, require_existing=True):
    if raw_ref is None:
        return None

    if isinstance(raw_ref, str):
        ref = raw_ref.strip()
        if not ref:
            return None
        for p in personeller:
            if getattr(p, "ad", None) == ref:
                return getattr(p, "id", None)
        raw_ref = ref

    normalized = normalize_id(raw_ref)
    if not require_existing:
        return normalized

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
