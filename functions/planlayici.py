"""
Ortak planlama katmani.

Amaç:
- nobet_hedef_hesapla ve nobet_coz ayni hedef/plani uretsin
- frontend hedefleri varsa bunu "kilitli hedef" olarak backend planlayiciya verelim
- solver tek bir plan kontrati tuketsin
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Dict, List, Optional

from gun_iskelet_planlayici import GunIskeletPlanlayici
from hedef_hesaplayici import HedefHesaplayici
from solver_models import (
    HedefSonuc,
    PlanKontrati,
    PlanPersonel,
    SolverAtama,
    SolverGorev,
    SolverKural,
    SolverPersonel,
)
from utils import GUN_TIPLERI, normalize_id


DEFAULT_PLAN_UYGULAMA = {
    "yetkili": True,
    "toplam_hard": True,
    "gun_tipi_toleransi": 1,
    "gorev_kota_toleransi": 1,
    "plan_sadakat_agirlik_carpani": 4,
    "gun_iskeleti_kullan": True,
    "gun_iskeleti_toleransi": 1,
    "gun_iskeleti_hard": False,
    "gun_iskeleti_sadakat_agirligi": 2500,
}


def _normalize_tip_hedefleri(raw: Optional[Dict]) -> Dict[str, int]:
    raw = raw or {}
    normalized = {}
    for tip in GUN_TIPLERI:
        try:
            normalized[tip] = int(raw.get(tip, 0) or 0)
        except (TypeError, ValueError):
            normalized[tip] = 0
    return normalized


def frontend_kilitli_hedefleri_topla(personeller: List[SolverPersonel]) -> Dict[int, Dict[str, int]]:
    kilitli_hedefler: Dict[int, Dict[str, int]] = {}
    for p in personeller:
        hedef_tipler = _normalize_tip_hedefleri(getattr(p, "hedef_tipler", {}) or {})
        if sum(hedef_tipler.values()) > 0:
            kilitli_hedefler[normalize_id(p.id)] = hedef_tipler
    return kilitli_hedefler


def frontend_gorev_kota_override_topla(personeller: List[SolverPersonel]) -> Dict[int, Dict[str, int]]:
    overrides: Dict[int, Dict[str, int]] = {}
    for p in personeller:
        raw = getattr(p, "gorev_kotalari", None)
        if not isinstance(raw, dict):
            continue
        normalized = {}
        for gorev_adi, kota in raw.items():
            try:
                normalized[str(gorev_adi)] = int(kota)
            except (TypeError, ValueError):
                continue
        if normalized:
            overrides[normalize_id(p.id)] = normalized
    return overrides


def _manual_day_map(manuel_atamalar: List[SolverAtama]) -> Dict[int, List[int]]:
    gunler: Dict[int, set] = {}
    for atama in manuel_atamalar or []:
        pid = normalize_id(atama.personel_id)
        gunler.setdefault(pid, set()).add(int(atama.gun))
    return {
        pid: sorted(list(gun_set))
        for pid, gun_set in gunler.items()
    }


def _hedef_listesini_dict_yap(hedefler: List[Dict]) -> Dict[int, Dict]:
    hedef_map: Dict[int, Dict] = {}
    for h in hedefler or []:
        pid = normalize_id(h.get("id"))
        hedef_tipler = h.get("hedef_tipler", {})
        if not hedef_tipler:
            hedef_tipler = {
                "hici": h.get("hedef_hici", 0),
                "prs": h.get("hedef_prs", 0),
                "cum": h.get("hedef_cum", 0),
                "cmt": h.get("hedef_cmt", 0),
                "pzr": h.get("hedef_pzr", 0),
            }
        hedef_tipler = _normalize_tip_hedefleri(hedef_tipler)
        hedef_map[pid] = {
            "hedef_toplam": int(h.get("hedef_toplam", sum(hedef_tipler.values()))),
            "hedef_tipler": hedef_tipler,
            "gorev_kotalari": dict(h.get("gorev_kotalari", {}) or {}),
            "ad": h.get("ad", ""),
        }
    return hedef_map


def _plan_hash_payload(
    hedefler_map: Dict[int, Dict],
    gun_iskeleti: Dict,
    kaynak: str,
    ara_gun: int,
    uygulama: Dict,
    meta: Dict,
) -> str:
    payload = {
        "kaynak": kaynak,
        "ara_gun": ara_gun,
        "uygulama": uygulama,
        "meta": meta,
        "gun_iskeleti": {
            "uygulanabilir_personeller": gun_iskeleti.get("uygulanabilir_personeller", []),
            "personel_gunleri": gun_iskeleti.get("personel_gunleri", {}),
        },
        "hedefler": {
            str(pid): {
                "hedef_toplam": hedef.get("hedef_toplam", 0),
                "hedef_tipler": hedef.get("hedef_tipler", {}),
                "gorev_kotalari": hedef.get("gorev_kotalari", {}),
            }
            for pid, hedef in sorted(hedefler_map.items(), key=lambda item: str(item[0]))
        },
    }
    ham = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(ham.encode("utf-8")).hexdigest()[:16]


def plan_kontrati_olustur(
    hedef_sonuc: HedefSonuc,
    personeller: List[SolverPersonel],
    ara_gun: int,
    kaynak: str,
    kilitli_hedefler: Optional[Dict[int, Dict[str, int]]] = None,
    gorev_kota_overrides: Optional[Dict[int, Dict[str, int]]] = None,
    manuel_atamalar: Optional[List[SolverAtama]] = None,
    gun_iskeleti: Optional[Dict] = None,
    uygulama_override: Optional[Dict] = None,
) -> PlanKontrati:
    kilitli_hedefler = kilitli_hedefler or {}
    gorev_kota_overrides = gorev_kota_overrides or {}
    gun_iskeleti = gun_iskeleti or {}
    hedefler_map = _hedef_listesini_dict_yap(hedef_sonuc.hedefler)
    manual_gun_map = _manual_day_map(manuel_atamalar or [])
    personel_durumlari = gun_iskeleti.get("personel_durumlari", {}) if isinstance(gun_iskeleti, dict) else {}
    personel_rol_gunleri_raw = gun_iskeleti.get("personel_rol_gunleri", {}) if isinstance(gun_iskeleti, dict) else {}

    personel_planlari: List[PlanPersonel] = []
    for p in personeller:
        pid = normalize_id(p.id)
        hedef = hedefler_map.get(pid, {
            "hedef_toplam": 0,
            "hedef_tipler": _normalize_tip_hedefleri({}),
            "gorev_kotalari": {},
            "ad": p.ad,
        })

        if pid in gorev_kota_overrides:
            hedef["gorev_kotalari"] = dict(gorev_kota_overrides[pid])

        kilitli = pid in kilitli_hedefler
        if kilitli:
            hedef["hedef_tipler"] = _normalize_tip_hedefleri(kilitli_hedefler[pid])
            hedef["hedef_toplam"] = sum(hedef["hedef_tipler"].values())

        durum = personel_durumlari.get(str(pid), {})

        # Rol iskelet bilgisini al: {gun_str: rol_adi} -> {gun_int: rol_adi}
        rol_gunleri_raw = personel_rol_gunleri_raw.get(str(pid), {})
        onerilen_rol_gunleri = {}
        for gun_str, rol in rol_gunleri_raw.items():
            try:
                onerilen_rol_gunleri[int(gun_str)] = str(rol)
            except (ValueError, TypeError):
                continue

        personel_planlari.append(PlanPersonel(
            personel_id=pid,
            ad=p.ad,
            hedef_toplam=int(hedef.get("hedef_toplam", 0)),
            hedef_tipler=dict(hedef.get("hedef_tipler", {}) or {}),
            gorev_kotalari=dict(hedef.get("gorev_kotalari", {}) or {}),
            kilitli=kilitli,
            kaynak="kilitli" if kilitli else "otomatik",
            kilitli_gunler=manual_gun_map.get(pid, []),
            onerilen_gunler=list(durum.get("planlanan_gunler", [])),
            onerilen_rol_gunleri=onerilen_rol_gunleri,
            gun_iskeleti_uygulanabilir=bool(durum.get("uygulanabilir", False)),
        ))

    hedefler_map = {
        pp.personel_id: {
            "hedef_toplam": pp.hedef_toplam,
            "hedef_tipler": pp.hedef_tipler,
            "gorev_kotalari": pp.gorev_kotalari,
            "ad": pp.ad,
        }
        for pp in personel_planlari
    }

    meta = {
        "versiyon": 1,
        "kilitli_hedef_sayisi": len(kilitli_hedefler),
        "gorev_kota_override_sayisi": len(gorev_kota_overrides),
        "manuel_gun_kilidi_sayisi": sum(len(v) for v in manual_gun_map.values()),
        "gun_iskeleti_uygulanabilir_sayisi": len(gun_iskeleti.get("uygulanabilir_personeller", [])),
    }
    uygulama = {
        **DEFAULT_PLAN_UYGULAMA,
        **({
            "gun_tipi_toleransi": 0,
        } if kilitli_hedefler else {}),
        **({
            "gorev_kota_toleransi": 0,
        } if gorev_kota_overrides else {}),
        **(uygulama_override or {}),
    }
    plan_hash = _plan_hash_payload(hedefler_map, gun_iskeleti, kaynak, ara_gun, uygulama, meta)

    return PlanKontrati(
        plan_hash=plan_hash,
        kaynak=kaynak,
        olusturulan_ara_gun=ara_gun,
        hedefler=hedefler_map,
        personeller=personel_planlari,
        meta=meta,
        uygulama=uygulama,
        istatistikler=dict(hedef_sonuc.istatistikler or {}),
        gun_iskeleti=gun_iskeleti,
    )


def ortak_plan_uret(
    gun_sayisi: int,
    gun_tipleri: Dict[int, str],
    personeller: List[SolverPersonel],
    gorevler: List[SolverGorev],
    birlikte_kurallar: Optional[List[SolverKural]] = None,
    kurallar: Optional[List[SolverKural]] = None,
    gorev_kisitlamalari: Optional[Dict[int, str]] = None,
    manuel_atamalar: Optional[List[SolverAtama]] = None,
    ara_gun: int = 2,
    saat_degerleri: Optional[Dict[str, int]] = None,
    kilitli_hedefler: Optional[Dict[int, Dict[str, int]]] = None,
    gorev_kota_overrides: Optional[Dict[int, Dict[str, int]]] = None,
    kaynak: Optional[str] = None,
    uygulama_override: Optional[Dict] = None,
) -> Dict:
    kilitli_hedefler = dict(kilitli_hedefler or {})
    gorev_kota_overrides = dict(gorev_kota_overrides or {})
    kurallar = list(kurallar or birlikte_kurallar or [])
    birlikte_kurallar = list(
        birlikte_kurallar if birlikte_kurallar is not None
        else [k for k in kurallar if k.tur == "birlikte"]
    )

    if kaynak is None:
        if kilitli_hedefler and gorev_kota_overrides:
            kaynak = "frontend_kilitli_ve_backend_plan"
        elif kilitli_hedefler:
            kaynak = "frontend_kilitli_backend_plan"
        elif gorev_kota_overrides:
            kaynak = "frontend_kota_backend_plan"
        else:
            kaynak = "backend_ortak_plan"

    plan_personeller = deepcopy(personeller)

    hesaplayici = HedefHesaplayici(
        gun_sayisi=gun_sayisi,
        gun_tipleri=gun_tipleri,
        personeller=plan_personeller,
        gorevler=gorevler,
        birlikte_kurallar=birlikte_kurallar or [],
        gorev_kisitlamalari=gorev_kisitlamalari or {},
        manuel_atamalar=manuel_atamalar or [],
        ara_gun=ara_gun,
        saat_degerleri=saat_degerleri,
        kilitli_hedefler=kilitli_hedefler,
    )
    hedef_sonuc = hesaplayici.hesapla()
    if not hedef_sonuc or not hedef_sonuc.basarili:
        return {
            "basarili": False,
            "mesaj": getattr(hedef_sonuc, "mesaj", "Plan olusturulamadi"),
            "hedef_sonuc": hedef_sonuc,
            "plan_kontrati": None,
            "hedefler_map": {},
        }

    hedefler_map = _hedef_listesini_dict_yap(hedef_sonuc.hedefler)
    gun_iskeleti = GunIskeletPlanlayici(
        gun_sayisi=gun_sayisi,
        gun_tipleri=gun_tipleri,
        personeller=plan_personeller,
        gorevler=gorevler,
        hedefler_map=hedefler_map,
        kurallar=kurallar,
        manuel_atamalar=manuel_atamalar or [],
        ara_gun=ara_gun,
        gorev_kisitlamalari=gorev_kisitlamalari or {},
    ).planla()

    plan_kontrati = plan_kontrati_olustur(
        hedef_sonuc=hedef_sonuc,
        personeller=plan_personeller,
        ara_gun=ara_gun,
        kaynak=kaynak,
        kilitli_hedefler=kilitli_hedefler,
        gorev_kota_overrides=gorev_kota_overrides,
        manuel_atamalar=manuel_atamalar,
        gun_iskeleti=gun_iskeleti,
        uygulama_override=uygulama_override,
    )

    return {
        "basarili": True,
        "mesaj": hedef_sonuc.mesaj,
        "hedef_sonuc": hedef_sonuc,
        "plan_kontrati": plan_kontrati,
        "hedefler_map": plan_kontrati.hedefler,
    }
