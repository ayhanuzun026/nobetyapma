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
    "toplam_hard": False,
    "gun_tipi_toleransi": 1,
    "gorev_kota_toleransi": 1,
    "plan_sadakat_agirlik_carpani": 4,
    "gun_iskeleti_kullan": True,
    "gun_iskeleti_toleransi": 1,
    "gun_iskeleti_hard": False,
    "gun_iskeleti_sadakat_agirligi": 4000,
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


def frontend_kilitli_hedefleri_topla(
    personeller: List[SolverPersonel],
    kilitli_hedefler_raw: Optional[Dict] = None,
) -> Dict[int, Dict[str, int]]:
    """Yalnız kullanıcının açıkça kilitlediği hedefleri normalize eder.

    Personel nesnesindeki ``hedef_tipler`` hesaplanmış plan önerisidir; kullanıcı
    kilidi değildir. Eski davranış tüm pozitif hedefleri hard kilide çevirerek
    manuel atama ve geçmiş değişikliklerinden sonra yeniden hesaplamayı
    etkisizleştiriyordu.
    """
    kilitli_hedefler: Dict[int, Dict[str, int]] = {}
    if not isinstance(kilitli_hedefler_raw, dict):
        return kilitli_hedefler

    personel_ids = {normalize_id(p.id) for p in personeller}
    for raw_pid, raw_hedef in kilitli_hedefler_raw.items():
        if not isinstance(raw_hedef, dict):
            continue
        pid = normalize_id(raw_pid)
        if pid not in personel_ids:
            continue
        hedef_tipler = _normalize_tip_hedefleri(raw_hedef)
        kilitli_hedefler[pid] = hedef_tipler
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


def plan_kontrati_hash_yenile(plan_kontrati):
    """Plan kontratinin mevcut icerigine gore hash'ini yeniden uretir.

    Gevsetme akisi kontratin ``uygulama`` bolumunu degistirebildigi icin eski
    hash'i tasimak, raporlanan plan ile solver'in kullandigi planin farkli
    gorunmesine neden olur. Yardimci hem API katmaninda kullanilan sozlukleri
    hem de planlayicinin kendi ``PlanKontrati`` nesnesini destekler.
    """
    if plan_kontrati is None:
        return None

    if isinstance(plan_kontrati, PlanKontrati):
        plan_kontrati.plan_hash = _plan_hash_payload(
            plan_kontrati.hedefler or {},
            plan_kontrati.gun_iskeleti or {},
            plan_kontrati.kaynak,
            plan_kontrati.olusturulan_ara_gun,
            plan_kontrati.uygulama or {},
            plan_kontrati.meta or {},
        )
        return plan_kontrati

    if not isinstance(plan_kontrati, dict):
        raise TypeError("plan_kontrati dict veya PlanKontrati olmali")

    yenilenmis = deepcopy(plan_kontrati)
    yenilenmis["plan_hash"] = _plan_hash_payload(
        yenilenmis.get("hedefler", {}) or {},
        yenilenmis.get("gun_iskeleti", {}) or {},
        yenilenmis.get("kaynak", ""),
        int(yenilenmis.get("olusturulan_ara_gun", 0) or 0),
        yenilenmis.get("uygulama", {}) or {},
        yenilenmis.get("meta", {}) or {},
    )
    return yenilenmis


def _kilit_alan(kaynak, *anahtarlar, default=None):
    """İlk dolu anahtarı döndürür (camelCase/snake_case toleransı)."""
    if not isinstance(kaynak, dict):
        return default
    for anahtar in anahtarlar:
        deger = kaynak.get(anahtar)
        if deger is not None:
            return deger
    return default


def kilitli_hucre_atamalari(onceki_atamalar, kilitler) -> List[SolverAtama]:
    """Önceki çözüm + kilit seçiminden kısmi yeniden çözümde SABİTLENECEK
    hücreleri (``SolverAtama``) üretir.

    Kısmi yeniden çözüm: kullanıcı önceki çizelgeden bazı hücreleri/haftaları/
    görevleri kilitler; kilitli hücreler solver'a manuel atama gibi HARD
    (``x==1``) beslenir, kilitsiz kısım yeniden optimize edilir.

    ``onceki_atamalar``: önceki çözümün atamaları; her biri
    ``personel_id``/``personelId``, ``gun``, ``slot_idx``/``slotIdx`` ve
    (opsiyonel) ``gorev_base``/``gorev_ad`` alanlarını taşır.

    ``kilitler``: kilit kapsamları (``tur`` ile ayrışır):
      * ``{'tur':'hucre','gun':g,'slot_idx':s}`` — tek hücre
      * ``{'tur':'hafta','gun_baslangic':a,'gun_bitis':b}`` — gün aralığı
      * ``{'tur':'gorev','slot_idx':s}`` veya ``{'tur':'gorev','gorev':'R'}``
      * ``{'tur':'personel','personel_id':pid}`` — bir kişinin tüm nöbetleri

    Dönen: tekilleştirilmiş ``SolverAtama`` listesi. Kilit/atama yoksa boş liste
    (davranış değişmez). Geçersiz/eksik alanlı kayıtlar sessizce atlanır.
    """
    if not onceki_atamalar or not kilitler:
        return []

    def _int(deger):
        try:
            return int(deger)
        except (TypeError, ValueError):
            return None

    def _eslesir(pid, gun, slot, gorev_base, gorev_ad, kilit) -> bool:
        tur = str(_kilit_alan(kilit, 'tur', 'type', default='')).strip().lower()
        if tur == 'hucre':
            return gun == _int(_kilit_alan(kilit, 'gun')) and \
                   slot == _int(_kilit_alan(kilit, 'slot_idx', 'slotIdx'))
        if tur == 'hafta':
            a = _int(_kilit_alan(kilit, 'gun_baslangic', 'gunBaslangic', 'baslangic'))
            b = _int(_kilit_alan(kilit, 'gun_bitis', 'gunBitis', 'bitis'))
            if a is None or b is None:
                return False
            return a <= gun <= b
        if tur == 'gorev':
            k_slot = _int(_kilit_alan(kilit, 'slot_idx', 'slotIdx'))
            if k_slot is not None:
                return slot == k_slot
            k_gorev = _kilit_alan(kilit, 'gorev', 'gorev_base', 'gorevBase', 'gorev_ad')
            if k_gorev is None:
                return False
            k_gorev = str(k_gorev)
            return k_gorev in (gorev_base, gorev_ad)
        if tur == 'personel':
            k_pid = _kilit_alan(kilit, 'personel_id', 'personelId', 'id')
            if k_pid is None:
                return False
            try:
                return normalize_id(pid) == normalize_id(k_pid)
            except (TypeError, ValueError):
                return False
        return False

    gorulen = set()
    sonuc: List[SolverAtama] = []
    for atama in onceki_atamalar:
        ham_pid = _kilit_alan(atama, 'personel_id', 'personelId', 'id')
        gun = _int(_kilit_alan(atama, 'gun'))
        slot = _int(_kilit_alan(atama, 'slot_idx', 'slotIdx'))
        if ham_pid is None or gun is None or slot is None:
            continue
        gorev_base = _kilit_alan(atama, 'gorev_base', 'gorevBase', default=None)
        gorev_ad = _kilit_alan(atama, 'gorev_ad', 'gorevAd', 'gorev', default=None)
        gorev_base = str(gorev_base) if gorev_base is not None else None
        gorev_ad = str(gorev_ad) if gorev_ad is not None else None

        if not any(_eslesir(ham_pid, gun, slot, gorev_base, gorev_ad, k) for k in kilitler):
            continue
        try:
            norm_pid = normalize_id(ham_pid)
        except (TypeError, ValueError):
            continue
        anahtar = (norm_pid, gun, slot)
        if anahtar in gorulen:
            continue
        gorulen.add(anahtar)
        sonuc.append(SolverAtama(personel_id=norm_pid, gun=gun, slot_idx=slot))
    return sonuc


def plan_hash_bayat_mi(gonderilen_hash, guncel_hash) -> bool:
    """İstemcinin gönderdiği ``planHash`` güncel plana göre bayat mı?

    Optimistik eşzamanlılık kontrolü: istemci bir önizlemeden (nobet_hedef_hesapla)
    aldığı planHash ile çözüm ister; arada girdi verisi değiştiyse backend'in
    yeniden ürettiği plan_hash farklı olur → plan bayattır (409 sinyali).

    Yalnız HER İKİ hash de doluysa ve farklıysa ``True`` döner. İlk çalıştırmada
    (istemci hash göndermedi → ``None``/boş) veya backend hash üretemediyse
    ``False`` döner; böylece hash göndermeyen mevcut istemcilerin davranışı
    değişmez. Karşılaştırma tip-toleranslıdır (int/str normalize edilir).
    """
    if not gonderilen_hash or not guncel_hash:
        return False
    return str(gonderilen_hash) != str(guncel_hash)


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
    gorev_havuzlari: Optional[Dict[str, set]] = None,
    kurum_profili: str = "genel",
    resmi_tatil_gunleri: Optional[set] = None,
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
        gorev_havuzlari=gorev_havuzlari or {},
        kurum_profili=kurum_profili,
        resmi_tatil_gunleri=resmi_tatil_gunleri,
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
        gorev_havuzlari=gorev_havuzlari or {},
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
