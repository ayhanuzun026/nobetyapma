"""
Request verisi parse fonksiyonları — endpoint'ler arası tekrarı kaldırır.
"""

import math
from typing import List, Dict, Set

from utils import (
    _safe_int, get_days_in_month, gun_adi_bul, gun_tipi_hesapla,
    _extract_mazeret_gunleri, _resolve_personel_id, _find_duplicate_personel_ids,
    normalize_id, ids_match, build_personel_lookup,
)
from solver_models import (
    SolverPersonel, SolverGorev, SolverKural, SolverAtama,
)


def _parse_optional_int(value, default=None):
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _parse_non_negative_float(value, default: float = 1.0) -> float:
    try:
        parsed = float(value)
    except (ValueError, TypeError):
        return default
    if not math.isfinite(parsed):
        return default
    return min(10.0, max(0.0, parsed))


def parse_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "evet", "yes"}:
            return True
        if normalized in {"false", "0", "hayir", "hayır", "no", ""}:
            return False
    return default


def parse_kurum_profili(value, default: str = "genel") -> str:
    """Kurum profili bayragini normalize eder: "genel" | "112".

    112'ye ozel domain kurallari (mesai-bazli min nobet, max ara gun,
    gece/gunduz dinlenme, izin yerlesimi, yetkinlik) yalniz "112" profilinde
    aktiflesir. Taninmayan/bos deger -> ``default`` ("genel"): boylece bayrak
    gonderilmeyen mevcut istekler eskisi gibi calisir (geriye tam uyumlu).
    """
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if not normalized:
        return default
    if "112" in normalized or "ambulans" in normalized:
        return "112"
    if "genel" in normalized or "hastane" in normalized:
        return "genel"
    return default


def _parse_string_set(value) -> Set[str]:
    if not isinstance(value, (list, tuple, set)):
        return set()
    return {
        str(item).strip()
        for item in value
        if item is not None and str(item).strip()
    }


def _parse_personel_adalet_alanlari(p_data: Dict) -> Dict:
    min_nobet = max(0, _parse_optional_int(p_data.get("minNobet"), 0) or 0)
    max_nobet = _parse_optional_int(p_data.get("maxNobet"), None)
    if max_nobet is not None:
        max_nobet = max(0, max_nobet)
    adalet_grubu = str(p_data.get("adaletGrubu") or "normal").strip() or "normal"
    gecmis_veri_durumu = str(
        p_data.get("gecmisVeriDurumu") or "bilinmiyor"
    ).strip().lower()
    if gecmis_veri_durumu not in {"tam", "kismi", "yeni", "bilinmiyor"}:
        gecmis_veri_durumu = "bilinmiyor"
    gecmis_donem_raw = p_data.get("gecmisDonem")
    return {
        "yetkili_gorevler": _parse_string_set(p_data.get("yetkiliGorevler")),
        "is_yuku_katsayisi": _parse_non_negative_float(p_data.get("isYukuKatsayisi"), 1.0),
        "min_nobet": min_nobet,
        "max_nobet": max_nobet,
        "esitlemeden_muaf": parse_bool(p_data.get("esitlemedenMuaf"), False),
        "adalet_grubu": adalet_grubu,
        "gecmis_veri_durumu": gecmis_veri_durumu,
        "gecmis_donem": dict(gecmis_donem_raw) if isinstance(gecmis_donem_raw, dict) else {},
    }


def _parse_gorev_politikasi(g_data: Dict, base_name: str) -> Dict:
    ayri_bina = parse_bool(g_data.get("ayriBina"), False)
    raw_bina_id = g_data.get("binaId")
    if raw_bina_id is not None and str(raw_bina_id).strip():
        bina_id = str(raw_bina_id).strip()
    elif ayri_bina:
        bina_id = f"AYRI_BINA:{base_name}"
    else:
        bina_id = "ANA_BINA"

    kritik = parse_bool(g_data.get("kritik"), False)
    default_politika = "asla" if kritik else "kullanici_onayli"
    istisna_politikasi = str(
        g_data.get("istisnaPolitikasi") or default_politika
    ).strip().lower()
    if istisna_politikasi not in {"asla", "kullanici_onayli", "otomatik", "soft"}:
        istisna_politikasi = default_politika

    alternatifler = g_data.get("alternatifGorevler", [])
    if not isinstance(alternatifler, list):
        alternatifler = []

    return {
        "ayri_bina": ayri_bina,
        "bina_id": bina_id,
        "kritik": kritik,
        "istisna_politikasi": istisna_politikasi,
        "alternatif_gorevler": [dict(item) for item in alternatifler if isinstance(item, dict)],
    }


# ============================================
# TAKVİM VE GÜN TİPLERİ
# ============================================

def build_gun_tipleri(yil: int, ay: int, gun_sayisi: int, resmi_tatiller: list) -> Dict[int, str]:
    """Gün tipi haritası oluştur {gün_no: 'hici'|'prs'|'cum'|'cmt'|'pzr'}"""
    return {g: gun_tipi_hesapla(yil, ay, g, resmi_tatiller) for g in range(1, gun_sayisi + 1)}


# ============================================
# KAPASİTE (nobet_kapasite) PARSERLERİ
# ============================================

def parse_kapasite_personeller(data: Dict) -> List[SolverPersonel]:
    """Kapasite hesabı için personelleri parse et"""
    personeller = []
    for p_data in data.get("personeller", []):
        if not p_data.get("ad"):
            continue

        mazeretler = _extract_mazeret_gunleri(p_data)

        yillik_gerceklesen = _parse_yillik_gerceklesen(p_data)
        adalet_alanlari = _parse_personel_adalet_alanlari(p_data)

        personeller.append(SolverPersonel(
            id=normalize_id(p_data.get("id", len(personeller))),
            ad=p_data.get("ad"),
            mazeret_gunleri=mazeretler,
            kisitli_gorev=p_data.get("kisitliGorev"),
            yillik_gerceklesen=yillik_gerceklesen,
            **adalet_alanlari,
        ))

    return personeller


# ============================================
# OR-TOOLS SOLVER PARSERLERİ
# ============================================

def parse_solver_gorevler(data: Dict, key: str = "gorevler") -> List[SolverGorev]:
    """OR-Tools çözücü için görevleri parse et"""
    gorevler = []
    for idx, g_data in enumerate(data.get(key, [])):
        gorev_ad = g_data.get("ad") or f"Görev {idx}"
        base_name = str(g_data.get("baseName") or "").strip() or gorev_ad
        politika = _parse_gorev_politikasi(g_data, base_name)
        gorevler.append(SolverGorev(
            id=g_data.get("id", idx),
            ad=gorev_ad,
            slot_idx=idx,
            base_name=base_name,
            exclusive=parse_bool(g_data.get("exclusive"), False) or politika["kritik"],
            **politika,
        ))
    return gorevler


def parse_solver_gorevler_nobet_coz(data: Dict, slot_sayisi: int) -> List[SolverGorev]:
    """nobet_coz için görevleri parse et (exclusive + ayrı bina desteği)"""
    gorev_kisitlamalari_raw = data.get("gorevKisitlamalari", [])
    exclusive_gorevler = set()
    for k in gorev_kisitlamalari_raw:
        if not isinstance(k, dict):
            continue
        gorev_adi = k.get("gorevAdi")
        if parse_bool(k.get("exclusive"), False) and gorev_adi:
            exclusive_gorevler.add(gorev_adi)

    gorevler = []
    for idx, g_data in enumerate(data.get("gorevler", [])):
        if isinstance(g_data, dict):
            gorev_id = g_data.get("id", idx)
            gorev_ad = g_data.get("ad") or f"Nöbetçi {idx + 1}"
            fallback_base_name = gorev_ad.split(" #")[0] if " #" in gorev_ad else gorev_ad
            base_name = str(g_data.get("baseName") or "").strip() or fallback_base_name
            politika = _parse_gorev_politikasi(g_data, base_name)
            exclusive = (
                parse_bool(g_data.get("exclusive"), False)
                or politika["kritik"]
                or gorev_ad in exclusive_gorevler
                or base_name in exclusive_gorevler
            )
        else:
            gorev_id = idx
            gorev_ad = str(g_data)
            base_name = gorev_ad.split(" #")[0] if " #" in gorev_ad else gorev_ad
            exclusive = gorev_ad in exclusive_gorevler or base_name in exclusive_gorevler
            politika = _parse_gorev_politikasi({}, base_name)

        gorevler.append(SolverGorev(
            id=gorev_id,
            ad=gorev_ad,
            slot_idx=idx,
            base_name=base_name,
            exclusive=exclusive,
            **politika,
        ))

    # Defansif üst sınır (OOM backstop): main.py slot_sayisi'ni zaten doğruluyor,
    # ama bu parser başka bir çağırandan devasa slot_sayisi alırsa bellek patlamasın.
    _MAX_GENERATED_SLOTS = 200
    while len(gorevler) < slot_sayisi and len(gorevler) < _MAX_GENERATED_SLOTS:
        idx = len(gorevler)
        gorevler.append(SolverGorev(
            id=idx,
            ad=f"Nöbetçi {idx + 1}",
            slot_idx=idx,
            base_name=f"Nöbetçi {idx + 1}",
            **_parse_gorev_politikasi({}, f"Nöbetçi {idx + 1}"),
        ))

    return gorevler


def parse_solver_personeller_hedef(data: Dict) -> List[SolverPersonel]:
    """nobet_hedef_hesapla için personelleri parse et"""
    personeller = []
    for p_data in data.get("personeller", []):
        pid = normalize_id(p_data.get("id", len(personeller)))
        mazeret_set = _extract_mazeret_gunleri(p_data)
        yillik_gerceklesen = _parse_yillik_gerceklesen(p_data)
        gecmis_gorevler = _parse_gecmis_gorevler(p_data)
        adalet_alanlari = _parse_personel_adalet_alanlari(p_data)

        personeller.append(SolverPersonel(
            id=pid,
            ad=p_data.get("ad", ""),
            mazeret_gunleri=mazeret_set,
            kisitli_gorev=p_data.get("kisitliGorev"),
            yillik_gerceklesen=yillik_gerceklesen,
            gecmis_gorevler=gecmis_gorevler,
            **adalet_alanlari,
        ))

    return personeller


def parse_solver_personeller_coz(data: Dict, gorevler: List[SolverGorev]) -> List[SolverPersonel]:
    """nobet_coz için personelleri parse et (hedef_tipler + gorev_kotalari dahil)"""

    def _normalize_gorev_adi(raw_gorev_adi):
        if not raw_gorev_adi:
            return None
        for g in gorevler:
            if g.ad == raw_gorev_adi or g.base_name == raw_gorev_adi:
                return g.base_name if g.base_name else g.ad
        return raw_gorev_adi

    personeller = []
    for p_data in data.get("personeller", []):
        if not p_data.get("ad"):
            continue

        raw_id = p_data.get("id", len(personeller))
        pid = normalize_id(raw_id)

        mazeretler = _extract_mazeret_gunleri(p_data)

        # Görev kısıtlaması
        kisitli_gorev = None
        tasma_gorevi = None
        for k in data.get("gorevKisitlamalari", []):
            k_pid = k.get("personelId")
            k_pid_matches = ids_match(k_pid, pid)
            if isinstance(k_pid, str) and k_pid.strip() == p_data.get("ad", ""):
                k_pid_matches = True

            if k_pid_matches:
                raw_gorev_adi = k.get("gorevAdi")
                kisitli_gorev = _normalize_gorev_adi(raw_gorev_adi)
                raw_tasma = k.get("tasmaGorevi")
                if raw_tasma:
                    tasma_gorevi = _normalize_gorev_adi(raw_tasma)
                break

        # Gün tipi hedefleri
        hedef_tipler = {}
        for tip in ['hici', 'prs', 'cum', 'cmt', 'pzr']:
            val = p_data.get(tip)
            if val is not None:
                try:
                    hedef_tipler[tip] = int(val)
                except (ValueError, TypeError):
                    hedef_tipler[tip] = 0

        # Görev kotaları
        gorev_kotalari = {}
        gk_raw = p_data.get("gorevKotalari", {})
        if isinstance(gk_raw, dict):
            for gorev_adi, kota in gk_raw.items():
                try:
                    gorev_kotalari[gorev_adi] = int(kota)
                except (ValueError, TypeError):
                    pass

        yillik_gerceklesen = _parse_yillik_gerceklesen(p_data)
        gecmis_gorevler = _parse_gecmis_gorevler(p_data)
        adalet_alanlari = _parse_personel_adalet_alanlari(p_data)

        personeller.append(SolverPersonel(
            id=pid,
            ad=p_data.get("ad"),
            mazeret_gunleri=mazeretler,
            kisitli_gorev=kisitli_gorev,
            tasma_gorevi=tasma_gorevi,
            hedef_tipler=hedef_tipler,
            gorev_kotalari=gorev_kotalari,
            yillik_gerceklesen=yillik_gerceklesen,
            gecmis_gorevler=gecmis_gorevler,
            **adalet_alanlari,
        ))

    return personeller


def parse_kurallar(data: Dict, personeller) -> List[SolverKural]:
    """OR-Tools çözücü için kuralları parse et (ayri + birlikte)"""
    _cache = build_personel_lookup(personeller)
    kurallar = []
    for k_data in data.get("kurallar", []):
        tur = k_data.get("tur")
        if tur not in ['ayri', 'birlikte']:
            continue

        kisiler = []
        kisiler_raw = k_data.get("kisiler", [])
        if isinstance(kisiler_raw, list):
            for v in kisiler_raw:
                pid = _resolve_personel_id(v, personeller, require_existing=True, _cache=_cache)
                if pid is not None and pid not in kisiler:
                    kisiler.append(pid)

        if len(kisiler) == 0:
            for key in ['p1', 'p2', 'p3']:
                pid = _resolve_personel_id(k_data.get(key), personeller, require_existing=True, _cache=_cache)
                if pid is not None and pid not in kisiler:
                    kisiler.append(pid)

        if len(kisiler) >= 2:
            politika = str(k_data.get("politika") or "kullanici_onayli").strip().lower()
            if politika not in {"hard", "kullanici_onayli", "soft"}:
                politika = "kullanici_onayli"
            kurallar.append(SolverKural(
                tur=tur,
                kisiler=kisiler,
                politika=politika,
                asla_gevsetme=parse_bool(k_data.get("aslaGevsetme"), False) or politika == "hard",
            ))

    return kurallar


def parse_gorev_kisitlamalari(data: Dict, personeller) -> Dict[int, dict]:
    """Görev kısıtlamalarını dict formatında parse et {personel_id: {gorevAdi, tasmaGorevi}}"""
    _cache = build_personel_lookup(personeller)
    gorev_kisitlamalari = {}
    for k_data in data.get("gorevKisitlamalari", []):
        pid = _resolve_personel_id(k_data.get("personelId"), personeller, require_existing=True, _cache=_cache)
        gorev_adi = k_data.get("gorevAdi")
        if pid is not None and gorev_adi:
            gorev_kisitlamalari[pid] = {
                "gorevAdi": gorev_adi,
                "tasmaGorevi": k_data.get("tasmaGorevi")
            }
    return gorev_kisitlamalari


def parse_manuel_atamalar(data: Dict, personeller, gorevler: List[SolverGorev],
                          gun_sayisi: int) -> List[SolverAtama]:
    """OR-Tools çözücü için manuel atamaları parse et"""
    _cache = build_personel_lookup(personeller)
    manuel_atamalar = []
    for m_data in data.get("manuelAtamalar", []):
        p_ad = m_data.get("personel") or m_data.get("personelAd")
        p_raw_id = m_data.get("personelId")
        p_id = _resolve_personel_id(p_raw_id, personeller, require_existing=True, _cache=_cache)
        if p_id is None:
            p_id = _resolve_personel_id(p_ad, personeller, require_existing=True, _cache=_cache)

        if p_id is None:
            continue

        gun = m_data.get("gun")
        if gun is None:
            continue
        try:
            gun = int(gun)
        except (ValueError, TypeError):
            continue

        if gun < 1 or gun > gun_sayisi:
            continue

        gorev_id = m_data.get("gorevId")
        gorev_adi = m_data.get("gorevAdi")
        gorev_base_adi = m_data.get("gorevBaseAdi")
        mazeret_onayli = parse_bool(m_data.get("mazeretOnayli"), False)
        slot_idx = None

        if gorev_id is not None:
            for g in gorevler:
                if ids_match(g.id, gorev_id):
                    slot_idx = g.slot_idx
                    break

        # slotIdx / gorevIdx fallback — bulunan slot'un base_name tutarlılığını doğrula
        if slot_idx is None:
            _raw_slot = _safe_int(m_data.get("slotIdx"), None)
            if _raw_slot is not None and 0 <= _raw_slot < len(gorevler):
                _found_base = gorevler[_raw_slot].base_name or gorevler[_raw_slot].ad
                if not gorev_base_adi or _found_base == gorev_base_adi:
                    slot_idx = _raw_slot
        if slot_idx is None:
            _raw_slot = _safe_int(m_data.get("gorevIdx"), None)
            if _raw_slot is not None and 0 <= _raw_slot < len(gorevler):
                _found_base = gorevler[_raw_slot].base_name or gorevler[_raw_slot].ad
                if not gorev_base_adi or _found_base == gorev_base_adi:
                    slot_idx = _raw_slot

        if slot_idx is None and gorev_adi:
            exact_matches = [g.slot_idx for g in gorevler if g.ad == gorev_adi]
            if len(exact_matches) == 1:
                slot_idx = exact_matches[0]

        if slot_idx is None and gorev_base_adi:
            base_matches = [
                g.slot_idx for g in gorevler
                if g.base_name == gorev_base_adi or g.ad == gorev_base_adi
            ]
            if len(base_matches) == 1:
                slot_idx = base_matches[0]

        if slot_idx is None and gorev_adi:
            for g in gorevler:
                if g.ad == gorev_adi:
                    slot_idx = g.slot_idx
                    break

        # Son fallback: base_name ile ilk match — SADECE gorev_base_adi yoksa kullan
        if slot_idx is None and gorev_base_adi:
            for g in gorevler:
                if g.base_name == gorev_base_adi or g.ad == gorev_base_adi:
                    slot_idx = g.slot_idx
                    break

        if slot_idx is not None and 0 <= slot_idx < len(gorevler):
            manuel_atamalar.append(SolverAtama(
                personel_id=p_id,
                gun=gun,
                slot_idx=slot_idx,
                gorev_adi=gorev_adi or "",
                mazeret_onayli=mazeret_onayli
            ))

    return manuel_atamalar


def parse_gorev_havuzlari(data: Dict, gorevler: List[SolverGorev],
                          personeller) -> Dict[str, Set[int]]:
    """nobet_coz için görev havuzlarını parse et.

    ``gorevHavuzlari`` alanı gönderilmişse authoritative kabul edilir; her rol
    yalnızca açıkça listelenen mevcut personelleri içerir ve boş liste deny-all
    anlamına gelir. Alan hiç gönderilmemişse eski
    ``gorevKisitlamalari[].havuzIds`` formatına geri dönülür.
    """

    def _normalize_gorev_adi(raw_gorev_adi):
        if not raw_gorev_adi:
            return None
        for g in gorevler:
            if g.ad == raw_gorev_adi or g.base_name == raw_gorev_adi:
                return g.base_name if g.base_name else g.ad
        return raw_gorev_adi

    _cache = build_personel_lookup(personeller)
    if "gorevHavuzlari" in data:
        gorev_havuzlari_raw = data.get("gorevHavuzlari")
        if not isinstance(gorev_havuzlari_raw, dict):
            raise ValueError("gorevHavuzlari nesne olmalı")

        gorev_havuzlari = {}
        for raw_role, ids in gorev_havuzlari_raw.items():
            role = _normalize_gorev_adi(raw_role)
            if not role:
                continue
            allowed_ids = set()
            raw_ids = ids if isinstance(ids, list) else []
            for raw_id in raw_ids:
                pid = _resolve_personel_id(raw_id, personeller, require_existing=True, _cache=_cache)
                if pid is not None:
                    allowed_ids.add(pid)
            gorev_havuzlari[role] = allowed_ids

        return gorev_havuzlari

    # ESKİ FORMAT: gorevKisitlamalari içindeki havuzIds (geriye uyumluluk)
    gorev_kisitlamalari_raw = data.get("gorevKisitlamalari", [])
    gorev_havuz_kayitlari = {}
    for k_data in gorev_kisitlamalari_raw:
        role = _normalize_gorev_adi(k_data.get("gorevAdi"))
        if not role:
            continue

        kayit = gorev_havuz_kayitlari.setdefault(role, {
            "kisitlilar": set(),
            "havuz": set(),
            "has_pool": False
        })

        kisit_pid = _resolve_personel_id(k_data.get("personelId"), personeller, require_existing=True, _cache=_cache)
        if kisit_pid is not None:
            kayit["kisitlilar"].add(kisit_pid)

        havuz_ids_raw = k_data.get("havuzIds", [])
        eklenen_havuz_id = False
        if isinstance(havuz_ids_raw, list):
            for raw_id in havuz_ids_raw:
                pid = _resolve_personel_id(raw_id, personeller, require_existing=True, _cache=_cache)
                if pid is not None:
                    kayit["havuz"].add(pid)
                    eklenen_havuz_id = True
        if eklenen_havuz_id:
            kayit["has_pool"] = True

    gorev_havuzlari = {}
    for role, kayit in gorev_havuz_kayitlari.items():
        if not kayit["has_pool"]:
            continue
        allowed_ids = kayit["kisitlilar"] | kayit["havuz"]
        if allowed_ids:
            gorev_havuzlari[role] = allowed_ids

    return gorev_havuzlari


def parse_kisitlama_istisnalari(data: Dict, personeller,
                                gorevler: List[SolverGorev]) -> List[Dict]:
    """Kisitlama istisnalarini parse et (manuel atama bazli gun/gorev izinleri)."""

    def _normalize_gorev_adi(raw_gorev_adi):
        if not raw_gorev_adi:
            return None
        for g in gorevler:
            if g.ad == raw_gorev_adi or g.base_name == raw_gorev_adi:
                return g.base_name if g.base_name else g.ad
        return str(raw_gorev_adi).strip() or None

    istisnalar = []
    seen = set()
    _cache = build_personel_lookup(personeller)
    for raw in data.get("kisitlamaIstisnalari", []):
        pid = _resolve_personel_id(raw.get("personelId"), personeller, require_existing=True, _cache=_cache)
        gun = _safe_int(raw.get("gun"), 0)
        istisna_gorev = _normalize_gorev_adi(raw.get("istisnaGorev") or raw.get("gorevAdi"))
        kisitli_gorev = _normalize_gorev_adi(raw.get("kisitliGorev"))
        if pid is None or gun < 1 or not istisna_gorev:
            continue

        key = (pid, gun, istisna_gorev)
        if key in seen:
            continue
        seen.add(key)

        istisnalar.append({
            "personel_id": pid,
            "gun": gun,
            "istisna_gorev": istisna_gorev,
            "kisitli_gorev": kisitli_gorev,
            "onay_tarihi": raw.get("onayTarihi")
        })

    return istisnalar


def parse_birlikte_istisnalari(data: Dict, personeller) -> List[Dict]:
    """Birlikte kurali + ayri bina istisnalarini parse et."""
    istisnalar = []
    seen = set()
    _cache = build_personel_lookup(personeller)
    for raw in data.get("birlikteIstisnalari", []):
        pid = _resolve_personel_id(raw.get("personelId"), personeller, require_existing=True, _cache=_cache)
        gun = _safe_int(raw.get("gun"), 0)
        if pid is None or gun < 1:
            continue
        key = (pid, gun)
        if key in seen:
            continue
        seen.add(key)
        istisnalar.append({
            "personel_id": pid,
            "gun": gun,
        })
    return istisnalar


def parse_aragun_istisnalari(data: Dict, personeller) -> List[Dict]:
    """Ara gun istisnalarini parse et."""
    istisnalar = []
    seen = set()
    _cache = build_personel_lookup(personeller)
    for raw in data.get("araGunIstisnalari", []):
        pid = _resolve_personel_id(raw.get("personelId"), personeller, require_existing=True, _cache=_cache)
        gun1 = _safe_int(raw.get("gun1"), 0)
        gun2 = _safe_int(raw.get("gun2"), 0)
        if pid is None or gun1 < 1 or gun2 < 1:
            continue
        g1, g2 = min(gun1, gun2), max(gun1, gun2)
        key = (pid, g1, g2)
        if key in seen:
            continue
        seen.add(key)
        istisnalar.append({
            "personel_id": pid,
            "gun1": g1,
            "gun2": g2,
        })
    return istisnalar


# ============================================
# YARDIMCI (İÇ)
# ============================================

def _parse_yillik_gerceklesen(p_data: Dict) -> Dict[str, int]:
    """Yıllık gerçekleşen verisini parse et"""
    yillik_gerceklesen = {}
    yg_raw = p_data.get("yillikGerceklesen", {})
    if isinstance(yg_raw, dict):
        for key, val in yg_raw.items():
            try:
                yillik_gerceklesen[key] = int(val)
            except (ValueError, TypeError):
                yillik_gerceklesen[key] = 0
    return yillik_gerceklesen


def _parse_gecmis_gorevler(p_data: Dict) -> Dict[str, int]:
    """Geçmiş özel görev verilerini parse et"""
    gecmis_gorevler = {}
    gg_raw = p_data.get("gecmisGorevler", {})
    if isinstance(gg_raw, dict):
        for key, val in gg_raw.items():
            try:
                gecmis_gorevler[key] = int(val)
            except (ValueError, TypeError):
                gecmis_gorevler[key] = 0
    return gecmis_gorevler
