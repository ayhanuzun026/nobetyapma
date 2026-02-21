"""
Request verisi parse fonksiyonları — endpoint'ler arası tekrarı kaldırır.
"""

from typing import List, Dict, Set

from utils import (
    _safe_int, get_days_in_month, gun_adi_bul, gun_tipi_hesapla,
    _extract_mazeret_gunleri, _resolve_personel_id, _find_duplicate_personel_ids,
    normalize_id, ids_match,
)
from models import GorevTanim, Personel
from ortools_solver import (
    SolverPersonel, SolverGorev, SolverKural, SolverAtama,
)


# ============================================
# TAKVİM VE GÜN TİPLERİ
# ============================================

def build_takvim(yil: int, ay: int, resmi_tatiller: list) -> Dict[int, str]:
    """Ay takvimi sözlüğü oluştur {gün_no: gün_adı}"""
    days_in_month = get_days_in_month(yil, ay)
    return {d: gun_adi_bul(yil, ay, d, resmi_tatiller) for d in range(1, days_in_month + 1)}


def build_gun_tipleri(yil: int, ay: int, gun_sayisi: int, resmi_tatiller: list) -> Dict[int, str]:
    """Gün tipi haritası oluştur {gün_no: 'hici'|'prs'|'cum'|'cmt'|'pzr'}"""
    return {g: gun_tipi_hesapla(yil, ay, g, resmi_tatiller) for g in range(1, gun_sayisi + 1)}


# ============================================
# GREEDY ÇÖZÜCÜ PARSERLERİ
# ============================================

def parse_gorev_tanimlari(data: Dict, gunluk_sayi: int) -> List[GorevTanim]:
    """Greedy çözücü için görev tanımlarını parse et"""
    raw_gorevler = data.get("gorevTanimlari", [])
    gorev_objs = []

    if raw_gorevler and isinstance(raw_gorevler[0], dict):
        for idx, g in enumerate(raw_gorevler):
            gorev_id = g.get("id", idx)
            gorev_objs.append(GorevTanim(
                id=gorev_id,
                ad=g.get("ad", f"Nöbetçi {idx + 1}"),
                slot_index=idx,
                base_name=g.get("baseName", g.get("ad", "")),
                ayri_bina=bool(g.get("ayriBina", False))
            ))
    else:
        for idx, g_ad in enumerate(raw_gorevler):
            gorev_objs.append(GorevTanim(
                id=idx, ad=str(g_ad), slot_index=idx, base_name=str(g_ad), ayri_bina=False
            ))

    while len(gorev_objs) < gunluk_sayi:
        idx = len(gorev_objs)
        gorev_objs.append(GorevTanim(
            id=idx, ad=f"Nöbetçi {idx + 1}", slot_index=idx, ayri_bina=False
        ))

    return gorev_objs


def parse_greedy_personeller(data: Dict) -> List[Personel]:
    """Greedy çözücü için personelleri parse et"""
    personeller = []

    for idx, p_data in enumerate(data.get("personeller", [])):
        ad = p_data.get("ad")
        if not ad:
            continue

        hici = _safe_int(p_data.get("hici", 0), 0)
        prs = _safe_int(p_data.get("prs", 0), 0)
        cum = _safe_int(p_data.get("cum", 0), 0)
        cmt = _safe_int(p_data.get("cmt", 0), 0)
        pzr = _safe_int(p_data.get("pzr", 0), 0)
        toplam = hici + prs + cum + cmt + pzr

        devir = p_data.get("devir", {})
        yillik_toplam = _safe_int(p_data.get("yillikToplam", 0), 0)

        rol_kotalari = {}
        gorev_kotalari_raw = p_data.get("gorevKotalari", {})
        if gorev_kotalari_raw and isinstance(gorev_kotalari_raw, dict):
            for gorev_adi, kota in gorev_kotalari_raw.items():
                try:
                    v = int(kota)
                    if v > 0:
                        rol_kotalari[gorev_adi] = v
                except (ValueError, TypeError):
                    pass

        mazeretler = _extract_mazeret_gunleri(p_data)

        personel = Personel(
            id=normalize_id(p_data.get("id", idx)),
            ad=ad,
            hedef_toplam=toplam,
            hedef_hici=hici,
            hedef_prs=prs,
            hedef_cum=cum,
            hedef_cmt=cmt,
            hedef_pzr=pzr,
            hedef_roller=rol_kotalari,
            mazeret_gunleri=mazeretler
        )
        personel.devir = devir
        personel.yillik_toplam = yillik_toplam
        personeller.append(personel)

    return personeller


def parse_greedy_manuel_atamalar(data: Dict, personeller: List[Personel],
                                  gorev_objs: List[GorevTanim], days_in_month: int,
                                  yonetici) -> None:
    """Greedy çözücü için manuel atamaları parse et ve yonetici'ye uygula"""
    manuel_atamalar = data.get("manuelAtamalar", [])
    for m in manuel_atamalar:
        p_ad = m.get("personel") or m.get("personelAd")
        p_raw_id = m.get("personelId")
        gun = _safe_int(m.get("gun", 0), 0)

        gorev_id = m.get("gorevId")
        gorev_adi = m.get("gorevAdi")
        gorev_idx = None

        if gorev_id is not None:
            for idx, g in enumerate(gorev_objs):
                if ids_match(g.id, gorev_id):
                    gorev_idx = idx
                    break

        if gorev_idx is None and gorev_adi:
            for idx, g in enumerate(gorev_objs):
                if g.ad == gorev_adi:
                    gorev_idx = idx
                    break

        if gorev_idx is None:
            gorev_idx = _safe_int(m.get("slotIdx"), None)

        if gorev_idx is None:
            gorev_idx = _safe_int(m.get("gorevIdx"), None)

        kisi_id = _resolve_personel_id(p_raw_id, personeller, require_existing=True)
        if kisi_id is None:
            kisi_id = _resolve_personel_id(p_ad, personeller, require_existing=True)
        kisi = next((p for p in personeller if ids_match(p.id, kisi_id)), None)
        if kisi and 1 <= gun <= days_in_month and gorev_idx is not None and 0 <= gorev_idx < len(gorev_objs):
            if yonetici.cizelge[gun][gorev_idx] is None:
                yonetici.cizelge[gun][gorev_idx] = kisi.ad
                yonetici.manuel_atamalar_set.add((gun, gorev_idx))
                g_obj = gorev_objs[gorev_idx]
                gun_tipi = yonetici._get_gun_tipi(gun)
                kisi.nobet_yaz(gun, gun_tipi, g_obj.ad, g_obj.base_name)


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

        personeller.append(SolverPersonel(
            id=normalize_id(p_data.get("id", len(personeller))),
            ad=p_data.get("ad"),
            mazeret_gunleri=mazeretler,
            kisitli_gorev=p_data.get("kisitliGorev"),
            yillik_gerceklesen=yillik_gerceklesen
        ))

    return personeller


# ============================================
# OR-TOOLS SOLVER PARSERLERİ
# ============================================

def parse_solver_gorevler(data: Dict, key: str = "gorevler") -> List[SolverGorev]:
    """OR-Tools çözücü için görevleri parse et"""
    gorevler = []
    for idx, g_data in enumerate(data.get(key, [])):
        gorevler.append(SolverGorev(
            id=g_data.get("id", idx),
            ad=g_data.get("ad", f"Görev {idx}"),
            slot_idx=idx,
            base_name=g_data.get("baseName", ""),
            exclusive=g_data.get("exclusive", False),
            ayri_bina=bool(g_data.get("ayriBina", False))
        ))
    return gorevler


def parse_solver_gorevler_nobet_coz(data: Dict, slot_sayisi: int) -> List[SolverGorev]:
    """nobet_coz için görevleri parse et (exclusive + ayrı bina desteği)"""
    gorev_kisitlamalari_raw = data.get("gorevKisitlamalari", [])
    exclusive_gorevler = set()
    for k in gorev_kisitlamalari_raw:
        gorev_adi = k.get("gorevAdi")
        if k.get("exclusive", False) and gorev_adi:
            exclusive_gorevler.add(gorev_adi)

    gorevler = []
    for idx, g_data in enumerate(data.get("gorevler", [])):
        if isinstance(g_data, dict):
            gorev_ad = g_data.get("ad", f"Nöbetçi {idx + 1}")
            base_name = g_data.get("baseName", gorev_ad.split(" #")[0] if " #" in gorev_ad else gorev_ad)
            exclusive = g_data.get("exclusive", False) or gorev_ad in exclusive_gorevler or base_name in exclusive_gorevler
            ayri_bina = bool(g_data.get("ayriBina", False))
        else:
            gorev_ad = str(g_data)
            base_name = gorev_ad.split(" #")[0] if " #" in gorev_ad else gorev_ad
            exclusive = gorev_ad in exclusive_gorevler or base_name in exclusive_gorevler
            ayri_bina = False

        gorevler.append(SolverGorev(
            id=idx,
            ad=gorev_ad,
            slot_idx=idx,
            base_name=base_name,
            exclusive=exclusive,
            ayri_bina=ayri_bina
        ))

    while len(gorevler) < slot_sayisi:
        idx = len(gorevler)
        gorevler.append(SolverGorev(
            id=idx,
            ad=f"Nöbetçi {idx + 1}",
            slot_idx=idx,
            base_name=f"Nöbetçi {idx + 1}",
            ayri_bina=False
        ))

    return gorevler


def parse_solver_personeller_hedef(data: Dict) -> List[SolverPersonel]:
    """nobet_hedef_hesapla için personelleri parse et"""
    personeller = []
    for p_data in data.get("personeller", []):
        pid = normalize_id(p_data.get("id", len(personeller)))
        mazeret_set = _extract_mazeret_gunleri(p_data)
        yillik_gerceklesen = _parse_yillik_gerceklesen(p_data)

        personeller.append(SolverPersonel(
            id=pid,
            ad=p_data.get("ad", ""),
            mazeret_gunleri=mazeret_set,
            kisitli_gorev=p_data.get("kisitliGorev"),
            yillik_gerceklesen=yillik_gerceklesen
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

        personeller.append(SolverPersonel(
            id=pid,
            ad=p_data.get("ad"),
            mazeret_gunleri=mazeretler,
            kisitli_gorev=kisitli_gorev,
            tasma_gorevi=tasma_gorevi,
            hedef_tipler=hedef_tipler,
            gorev_kotalari=gorev_kotalari,
            yillik_gerceklesen=yillik_gerceklesen
        ))

    return personeller


def parse_kurallar(data: Dict, personeller) -> List[SolverKural]:
    """OR-Tools çözücü için kuralları parse et (ayri + birlikte)"""
    kurallar = []
    for k_data in data.get("kurallar", []):
        tur = k_data.get("tur")
        if tur not in ['ayri', 'birlikte']:
            continue

        kisiler = []
        kisiler_raw = k_data.get("kisiler", [])
        if isinstance(kisiler_raw, list):
            for v in kisiler_raw:
                pid = _resolve_personel_id(v, personeller, require_existing=True)
                if pid is not None and pid not in kisiler:
                    kisiler.append(pid)

        if len(kisiler) == 0:
            for key in ['p1', 'p2', 'p3']:
                pid = _resolve_personel_id(k_data.get(key), personeller, require_existing=True)
                if pid is not None and pid not in kisiler:
                    kisiler.append(pid)

        if len(kisiler) >= 2:
            kurallar.append(SolverKural(tur=tur, kisiler=kisiler))

    return kurallar


def parse_birlikte_kurallar(data: Dict, personeller) -> List[SolverKural]:
    """Sadece birlikte kurallarını parse et (nobet_hedef_hesapla için)"""
    birlikte_kurallar = []
    for k_data in data.get("kurallar", []):
        if k_data.get("tur") != "birlikte":
            continue

        kisiler = []
        for key in ['p1', 'p2', 'p3', 'kisiler']:
            val = k_data.get(key)
            if val is None:
                continue

            refs = val if (key == 'kisiler' and isinstance(val, list)) else [val]
            for ref in refs:
                pid = _resolve_personel_id(ref, personeller, require_existing=True)
                if pid is not None and pid not in kisiler:
                    kisiler.append(pid)

        if len(kisiler) >= 2:
            birlikte_kurallar.append(SolverKural(
                tur="birlikte",
                kisiler=kisiler
            ))

    return birlikte_kurallar


def parse_gorev_kisitlamalari(data: Dict, personeller) -> Dict[int, dict]:
    """Görev kısıtlamalarını dict formatında parse et {personel_id: {gorevAdi, tasmaGorevi}}"""
    gorev_kisitlamalari = {}
    for k_data in data.get("gorevKisitlamalari", []):
        pid = _resolve_personel_id(k_data.get("personelId"), personeller, require_existing=True)
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
    manuel_atamalar = []
    for m_data in data.get("manuelAtamalar", []):
        p_ad = m_data.get("personel") or m_data.get("personelAd")
        p_raw_id = m_data.get("personelId")
        p_id = _resolve_personel_id(p_raw_id, personeller, require_existing=True)
        if p_id is None:
            p_id = _resolve_personel_id(p_ad, personeller, require_existing=True)

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
        slot_idx = None
        if gorev_id is not None:
            for g in gorevler:
                if ids_match(g.id, gorev_id):
                    slot_idx = g.slot_idx
                    break

        if slot_idx is None and gorev_adi:
            for g in gorevler:
                if g.ad == gorev_adi:
                    slot_idx = g.slot_idx
                    break

        if slot_idx is None:
            slot_idx = _safe_int(m_data.get("slotIdx"), None)
        if slot_idx is None:
            slot_idx = _safe_int(m_data.get("gorevIdx"), None)

        if slot_idx is not None and 0 <= slot_idx < len(gorevler):
            manuel_atamalar.append(SolverAtama(
                personel_id=p_id,
                gun=gun,
                slot_idx=slot_idx,
                gorev_adi=gorev_adi or ""
            ))

    return manuel_atamalar


def parse_gorev_havuzlari(data: Dict, gorevler: List[SolverGorev],
                          personeller) -> Dict[str, Set[int]]:
    """nobet_coz için non-exclusive görev havuzlarını parse et"""
    gorev_kisitlamalari_raw = data.get("gorevKisitlamalari", [])

    def _normalize_gorev_adi(raw_gorev_adi):
        if not raw_gorev_adi:
            return None
        for g in gorevler:
            if g.ad == raw_gorev_adi or g.base_name == raw_gorev_adi:
                return g.base_name if g.base_name else g.ad
        return raw_gorev_adi

    exclusive_role_adlari = {
        g.base_name if g.base_name else g.ad
        for g in gorevler if g.exclusive
    }

    gorev_havuz_kayitlari = {}
    for k_data in gorev_kisitlamalari_raw:
        if k_data.get("exclusive", False):
            continue

        role = _normalize_gorev_adi(k_data.get("gorevAdi"))
        if not role or role in exclusive_role_adlari:
            continue

        kayit = gorev_havuz_kayitlari.setdefault(role, {
            "kisitlilar": set(),
            "havuz": set(),
            "has_pool": False
        })

        kisit_pid = _resolve_personel_id(k_data.get("personelId"), personeller, require_existing=True)
        if kisit_pid is not None:
            kayit["kisitlilar"].add(kisit_pid)

        havuz_ids_raw = k_data.get("havuzIds", [])
        eklenen_havuz_id = False
        if isinstance(havuz_ids_raw, list):
            for raw_id in havuz_ids_raw:
                pid = _resolve_personel_id(raw_id, personeller, require_existing=True)
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
    for raw in data.get("kisitlamaIstisnalari", []):
        pid = _resolve_personel_id(raw.get("personelId"), personeller, require_existing=True)
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
