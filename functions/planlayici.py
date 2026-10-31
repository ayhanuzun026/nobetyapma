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
from dataclasses import asdict
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


def _hedef_hizli_on_kontrolu(
    gun_sayisi: int,
    gun_tipleri: Dict[int, str],
    personeller: List[SolverPersonel],
    gorevler: List[SolverGorev],
    manuel_atamalar: Optional[List[SolverAtama]] = None,
) -> Optional[HedefSonuc]:
    """Hedef CP-SAT modelinden önce kesin, ucuz kapasite çelişkilerini bulur.

    Bu kontrol yalnızca gerekli koşulları kullanır; yani ``aday sayısı < slot``
    gibi bir çelişki görürse model kesinlikle çözümsüzdür. Uygun aday sayısı
    yeterliyse ``None`` döner ve tam hedef modeli çalışır. Böylece özellikle
    bütün Cuma günleri mazeretli olan senaryolar, 7--30 saniyelik adalet
    aramasına girmeden açık bir tanı ile sonlanır.
    """
    try:
        gun_sayisi = int(gun_sayisi)
    except (TypeError, ValueError):
        return None
    if gun_sayisi <= 0 or not personeller or not gorevler:
        return None

    slot_sayisi = len(gorevler)
    gun_tipleri = gun_tipleri or {}
    onayli_mazeretler = set()
    for atama in manuel_atamalar or []:
        if not getattr(atama, "mazeret_onayli", False):
            continue
        try:
            onayli_mazeretler.add((normalize_id(atama.personel_id), int(atama.gun)))
        except (TypeError, ValueError):
            continue

    def uygun_mu(personel, gun: int) -> bool:
        pid = normalize_id(personel.id)
        return gun not in (getattr(personel, "mazeret_gunleri", set()) or set()) \
            or (pid, gun) in onayli_mazeretler

    # Aynı gün her slot için farklı kişi gerekir. Bir günün aday sayısı
    # slot sayısından azsa, görev/havuz ayrıntılarına bakmadan çözüm yoktur.
    gun_aciklari = []
    for gun in range(1, gun_sayisi + 1):
        adaylar = [p for p in personeller if uygun_mu(p, gun)]
        if len(adaylar) < slot_sayisi:
            gun_aciklari.append({
                "gun": gun,
                "gun_tipi": gun_tipleri.get(gun, "hici"),
                "slot_ihtiyaci": slot_sayisi,
                "uygun_personel": len(adaylar),
                "eksik": slot_sayisi - len(adaylar),
                "personeller": [p.ad for p in adaylar],
            })

    if gun_aciklari:
        ilk = gun_aciklari[0]
        tip = ilk["gun_tipi"]
        tip_adi = {
            "hici": "hafta içi", "prs": "perşembe", "cum": "cuma",
            "cmt": "cumartesi", "pzr": "pazar",
        }.get(tip, tip)
        tani = {
            "neden": "gun_bazli_kapasite",
            "baslik": f"{ilk['gun']}. gün ({tip_adi}) için yeterli uygun personel yok",
            "detay": (
                f"Bu gün {slot_sayisi} farklı kişi gerekiyor; yalnızca "
                f"{ilk['uygun_personel']} kişi uygun. {ilk['eksik']} slot kesin olarak boş kalır."
            ),
            "oneriler": [
                "Bu günün mazeret/izin kayıtlarını gözden geçirin.",
                "Günlük görev/slot sayısını azaltın veya ek personel ekleyin.",
            ],
            "debug": {"gun_aciklari": gun_aciklari[:20], "kaynak": "hizli_on_kontrol"},
        }
        cuma_aciklari = [a for a in gun_aciklari if a.get("gun_tipi") == "cum"]
        if tip == "cum" or cuma_aciklari:
            tani["esdeger_gun_notu"] = {
                "kaynak_tip": "cum",
                "aday_tipler": ["pzr"],
                "saat_esit": True,
                "otomatik_uygulanabilir": False,
                "gunler": [a.get("gun") for a in cuma_aciklari],
                "mesaj": (
                    "Cuma günlerinde aday açığı var. Pazar da 16 saatlik eşdeğer "
                    "sınıftadır; ancak kişi, hedef ve ara-gün uygunluğu yeniden "
                    "doğrulanmadan otomatik Cuma→Pazar takası yapılmaz."
                ),
            }
            tani["oneriler"].append(tani["esdeger_gun_notu"]["mesaj"])
        stats = {
            "status": "INFEASIBLE",
            "solver_status": "PRECHECK_INFEASIBLE",
            "karar_durumu": "INFEASIBLE",
            "on_kontrol": {
                "durum": "INFEASIBLE",
                "kod": "GUN_BAZLI_MAZERET_KAPASITESI",
                "gun_aciklari": gun_aciklari[:20],
            },
            "hedef_tanisi": tani,
        }
        return HedefSonuc(
            False, [], [], {}, stats,
            tani["baslik"] + ". " + tani["detay"] + " Öneriler: " + " ".join(tani["oneriler"]),
        )

    # Gün tipi toplam aday arzı da gerekli bir koşuldur. Örneğin her Cuma
    # gününde en az slot kadar kişi bulunsa bile toplam Cuma aday sayısı talebi
    # karşılamıyorsa hedef modeli çözümsüzdür.
    tip_aciklari = []
    for tip in GUN_TIPLERI:
        tip_gunleri = [g for g in range(1, gun_sayisi + 1) if gun_tipleri.get(g) == tip]
        ihtiyac = len(tip_gunleri) * slot_sayisi
        if ihtiyac <= 0:
            continue
        arz = sum(1 for p in personeller for g in tip_gunleri if uygun_mu(p, g))
        if arz < ihtiyac:
            tip_aciklari.append({
                "gun_tipi": tip,
                "gun_sayisi": len(tip_gunleri),
                "slot_ihtiyaci": ihtiyac,
                "uygunluk_arzi": arz,
                "eksik": ihtiyac - arz,
            })

    if tip_aciklari:
        ilk = tip_aciklari[0]
        tip_adi = {
            "hici": "hafta içi", "prs": "perşembe", "cum": "cuma",
            "cmt": "cumartesi", "pzr": "pazar",
        }.get(ilk["gun_tipi"], ilk["gun_tipi"])
        tani = {
            "neden": "gun_tipi_kapasitesi",
            "baslik": f"{tip_adi.capitalize()} günlerinde yeterli aday arzı yok",
            "detay": (
                f"{ilk['gun_sayisi']} gün ve {slot_sayisi} slot için "
                f"{ilk['slot_ihtiyaci']} nöbet gerekiyor; toplam uygunluk arzı "
                f"{ilk['uygunluk_arzi']}. {ilk['eksik']} nöbetlik açık kesin."
            ),
            "oneriler": [
                f"{tip_adi.capitalize()} günlerindeki mazeret/izin kayıtlarını gözden geçirin.",
                "İlgili gün tipi için slot sayısını azaltın veya uygun personel ekleyin.",
            ],
            "debug": {"tip_aciklari": tip_aciklari, "kaynak": "hizli_on_kontrol"},
        }
        # Cuma ve Pazar aynı 16 saatlik sınıftadır; ancak bu yalnızca
        # kullanıcı onaylı bir öneridir. Takvimde Cuma'yı sessizce Pazar'a
        # çevirmek, gerçek gün ve mazeret kaydını bozacağından otomatikleşmez.
        if ilk["gun_tipi"] == "cum":
            tani["esdeger_gun_notu"] = {
                "kaynak_tip": "cum",
                "aday_tipler": ["pzr"],
                "saat_esit": True,
                "otomatik_uygulanabilir": False,
                "mesaj": (
                    "Cuma arzı yetersiz. Aynı 16 saat sınıfındaki Pazar günleri "
                    "için kişi/hedef/ara-gün uygunluğu yeniden doğrulanarak "
                    "kullanıcı onaylı bir öneri üretilebilir; otomatik takas yapılmadı."
                ),
            }
            tani["oneriler"].append(tani["esdeger_gun_notu"]["mesaj"])
        stats = {
            "status": "INFEASIBLE",
            "solver_status": "PRECHECK_INFEASIBLE",
            "karar_durumu": "INFEASIBLE",
            "on_kontrol": {
                "durum": "INFEASIBLE",
                "kod": "GUN_TIPI_MAZERET_KAPASITESI",
                "tip_aciklari": tip_aciklari,
            },
            "hedef_tanisi": tani,
        }
        return HedefSonuc(
            False, [], [], {}, stats,
            tani["baslik"] + ". " + tani["detay"] + " Öneriler: " + " ".join(tani["oneriler"]),
        )

    return None


def plan_girdi_hash(data: Dict) -> str:
    """Önizleme ve çözümün ortak girdilerini özetler; solver çıktısını değil.

    Üretilen hedef/kota, kaynak etiketi ve süreye bağlı solver kararı bayatlık
    nedeni değildir. Plan içeriği ayrıca PlanKontrati.plan_hash ile izlenir.
    """
    from parsers import (
        build_gun_tipleri, parse_solver_gorevler_nobet_coz,
        parse_solver_personeller_coz, parse_kurallar, parse_manuel_atamalar,
        parse_gorev_havuzlari, parse_kisitlama_istisnalari,
        parse_birlikte_istisnalari, parse_aragun_istisnalari, parse_kurum_profili,
    )
    from utils import get_days_in_month

    yil, ay = int(data.get('yil', 2025)), int(data.get('ay', 1))
    gun_sayisi = int(data.get('gunSayisi') or get_days_in_month(yil, ay))
    gun_tipleri = data.get('gunTipleri') or build_gun_tipleri(
        yil, ay, gun_sayisi, data.get('resmiTatiller', []))
    gorevler = parse_solver_gorevler_nobet_coz(data, int(data.get('slotSayisi', 5)))
    personeller = parse_solver_personeller_coz(data, gorevler)
    personel_girdileri = []
    for p in sorted(personeller, key=lambda p: p.id):
        kayit = asdict(p)
        # Hesaplanan görev kotaları da önizleme sonrasında istemciye yazılır.
        # Kullanıcının kota düzenlemesi frontend'de planı geçersizleştirir.
        for alan in ('hedef_tipler', 'gorev_kotalari', 'musait_gunler', 'musait_tipler'):
            kayit.pop(alan, None)
        personel_girdileri.append(kayit)

    payload = {
        'yil': yil, 'ay': ay, 'gun_sayisi': gun_sayisi,
        'gun_tipleri': gun_tipleri, 'gorevler': [asdict(g) for g in gorevler],
        'personeller': personel_girdileri,
        'ara_gun': int(data.get('araGun', 2)),
        'max_ara_gun': int(data.get('maxAraGun') or 0),
        'kurum_profili': parse_kurum_profili(data.get('kurumProfili')),
        'saatler': {tip: int((data.get('saatDegerleri') or {}).get(tip, deger))
                   for tip, deger in {'hici': 8, 'prs': 8, 'cum': 16, 'cmt': 24, 'pzr': 16}.items()},
        'kurallar': [asdict(k) for k in parse_kurallar(data, personeller)],
        'manuel': [asdict(m) for m in parse_manuel_atamalar(data, personeller, gorevler, gun_sayisi)],
        'havuzlar': parse_gorev_havuzlari(data, gorevler, personeller),
        'kisitlama_istisnalari': parse_kisitlama_istisnalari(data, personeller, gorevler),
        'birlikte_istisnalari': parse_birlikte_istisnalari(data, personeller),
        'aragun_istisnalari': parse_aragun_istisnalari(data, personeller),
        'kilitli_hedefler': frontend_kilitli_hedefleri_topla(personeller, data.get('kilitliHedefler')),
        # Otomatik personel kotaları hash'e girmez; yalnız explicit override'lar
        # önizleme/final planın girdisi sayılır.
        'gorev_kota_overrides': data.get('gorevKotaOverrides') or {},
        'tamir_politikasi': data.get('tamirPolitikasi') or {},
        'resmi_tatiller': data.get('resmiTatiller') or [],
        'gecmis_politikasi': data.get('gecmisDonemPolitikasi'),
        'yeniden_cozum_kilitleri': data.get('kilitler') or [],
    }

    def canonical(value):
        if isinstance(value, dict):
            return {str(k): canonical(v) for k, v in value.items()}
        if isinstance(value, set):
            return sorted(value)
        if isinstance(value, (list, tuple)):
            return [canonical(v) for v in value]
        return value

    raw = json.dumps(canonical(payload), sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return 'girdi-v1:' + hashlib.sha256(raw.encode('utf-8')).hexdigest()


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


def frontend_gorev_kota_override_topla(
    personeller: List[SolverPersonel],
    raw_overrides: Optional[Dict] = None,
) -> Dict[int, Dict[str, int]]:
    """Açıkça kullanıcı tarafından gönderilen kişi-görev kotalarını al.

    ``SolverPersonel.gorev_kotalari`` hedef hesaplamasının otomatik çıktısını
    de taşıyabilir. Bu alanı varsayılan olarak override kabul etmek, otomatik
    kotayı hard plan kısıtına çevirip final çizelgesini gereksiz yere bozuyordu.
    Override kanalı artık payload'da ayrı olan ``gorevKotaOverrides`` alanıdır.
    Eski çağrılar için yalnızca açıkça verilen sözlüğü işleriz.
    """
    overrides: Dict[int, Dict[str, int]] = {}
    if not isinstance(raw_overrides, dict):
        return overrides

    personel_ids = {normalize_id(p.id) for p in personeller}
    for raw_pid, raw in raw_overrides.items():
        try:
            pid = normalize_id(raw_pid)
        except (TypeError, ValueError):
            continue
        if pid not in personel_ids or not isinstance(raw, dict):
            continue
        normalized = {}
        for gorev_adi, kota in raw.items():
            try:
                normalized[str(gorev_adi)] = max(0, int(kota))
            except (TypeError, ValueError):
                continue
        if normalized:
            overrides[pid] = normalized
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


def plan_kontrati_snapshot_hazirla(
    snapshot: Optional[Dict],
    personeller: List[SolverPersonel],
    ara_gun: int,
) -> Optional[Dict]:
    """İstemciden gelen hedef/plan snapshot'ını final solver için normalize eder.

    Hedef önizlemesi ile final çizelgesi arasında hedeflerin yeniden seçilmesini
    önlemek için yalnızca yapısal olarak tam bir kontrat kabul edilir. Snapshot
    girdi hash'iyle (``planHash``) çağıran katmanda doğrulanır; burada ise kişi
    kimlikleri, hedef tipleri ve planın ``ara_gun`` değeri kontrol edilir.
    Geçersiz veya eksik snapshot ``None`` döner; çağıran katman eski yeniden
    hesaplama yolunu koruyabilir ya da kullanıcıya açık hata verebilir.
    """
    if not isinstance(snapshot, dict):
        return None
    raw_hedefler = snapshot.get("hedefler")
    if not isinstance(raw_hedefler, dict) or not raw_hedefler:
        return None
    try:
        plan_ara_gun = int(snapshot.get("olusturulan_ara_gun"))
    except (TypeError, ValueError):
        return None
    if plan_ara_gun != int(ara_gun):
        return None

    personel_ids = {normalize_id(p.id) for p in (personeller or [])}
    normalized = {}
    for raw_pid, raw_hedef in raw_hedefler.items():
        pid = normalize_id(raw_pid)
        if pid is None or pid not in personel_ids or not isinstance(raw_hedef, dict):
            return None
        tipler = _normalize_tip_hedefleri(raw_hedef.get("hedef_tipler"))
        # Hedef tipleri negatif olamaz. Sadece toplamı kontrol etmek, örneğin
        # hici=-1 ve prs=2 gibi birbirini dengeleyen bozuk snapshot'ları kabul
        # edip solver'a anlamsız sınırlar taşırdı.
        if any(value < 0 for value in tipler.values()):
            return None
        try:
            toplam = int(raw_hedef.get("hedef_toplam", sum(tipler.values())) or 0)
        except (TypeError, ValueError):
            return None
        if toplam < 0 or toplam != sum(tipler.values()):
            return None
        raw_kotalar = raw_hedef.get("gorev_kotalari", {}) or {}
        if not isinstance(raw_kotalar, dict):
            return None
        kotalar = {}
        for gorev, kota in raw_kotalar.items():
            try:
                kota_int = int(kota)
            except (TypeError, ValueError):
                return None
            if kota_int < 0:
                return None
            kotalar[str(gorev)] = kota_int
        normalized[pid] = {
            "hedef_toplam": toplam,
            "hedef_tipler": tipler,
            "gorev_kotalari": kotalar,
            "ad": raw_hedef.get("ad", ""),
        }

    # Eksik personel hedefleri final modelinde sessizce sıfıra düşer; bu,
    # snapshot'ın yanlış ayla/ekiple kullanılmasını gizler.
    if set(normalized) != personel_ids:
        return None

    prepared = deepcopy(snapshot)
    prepared["hedefler"] = normalized
    # Kontratın içerik hash'i backend tarafından üretilir. İstemcinin hedef
    # map'ini değiştirmesi, girdi planHash'i aynı kaldığı için bayatlık
    # kontrolünden kaçabilir; burada kontrat hash'ini de doğrula.
    gelen_kontrat_hash = snapshot.get("plan_hash") or snapshot.get("planHash")
    if gelen_kontrat_hash:
        try:
            beklenen_hash = plan_kontrati_hash_yenile(prepared).get("plan_hash")
        except Exception:
            return None
        if str(gelen_kontrat_hash) != str(beklenen_hash):
            return None
    return prepared


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
            # Override map is sparse (the UI sends only edited cells). Merge it
            # over the generated quotas instead of deleting the other roles.
            hedef["gorev_kotalari"] = {
                **dict(hedef.get("gorev_kotalari", {}) or {}),
                **dict(gorev_kota_overrides[pid]),
            }

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
    kisitlama_istisnalari: Optional[List[Dict]] = None,
    birlikte_istisnalari: Optional[List[Dict]] = None,
    aragun_istisnalari: Optional[List[Dict]] = None,
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
    # Incoming ``gorev_kotalari`` may be a previous automatic plan snapshot.
    # It is presentation/state, not a hard input; explicit edits arrive via
    # ``gorev_kota_overrides`` and are merged into the fresh plan below.
    for p in plan_personeller:
        p.gorev_kotalari = {}

    # CP-SAT hedef/adalet modeline girmeden önce gerekli kapasite koşullarını
    # ucuzca denetle. Bu özellikle tüm Cuma günlerinin mazeretli olduğu
    # senaryoda uzun arama yerine açık bir ``INFEASIBLE`` tanısı üretir.
    hizli_on_kontrol = _hedef_hizli_on_kontrolu(
        gun_sayisi=gun_sayisi,
        gun_tipleri=gun_tipleri,
        personeller=plan_personeller,
        gorevler=gorevler,
        manuel_atamalar=manuel_atamalar,
    )
    if hizli_on_kontrol is not None:
        return {
            "basarili": False,
            "mesaj": hizli_on_kontrol.mesaj,
            "hedef_sonuc": hizli_on_kontrol,
            "plan_kontrati": None,
            "hedefler_map": {},
        }

    hesaplayici = HedefHesaplayici(
        gun_sayisi=gun_sayisi,
        gun_tipleri=gun_tipleri,
        personeller=plan_personeller,
        gorevler=gorevler,
        birlikte_kurallar=birlikte_kurallar or [],
        kurallar=kurallar,
        gorev_kisitlamalari=gorev_kisitlamalari or {},
        manuel_atamalar=manuel_atamalar or [],
        ara_gun=ara_gun,
        saat_degerleri=saat_degerleri,
        kilitli_hedefler=kilitli_hedefler,
        gorev_havuzlari=gorev_havuzlari or {},
        kisitlama_istisnalari=kisitlama_istisnalari or [],
        birlikte_istisnalari=birlikte_istisnalari or [],
        aragun_istisnalari=aragun_istisnalari or [],
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
        kisitlama_istisnalari=kisitlama_istisnalari or [],
        birlikte_istisnalari=birlikte_istisnalari or [],
        aragun_istisnalari=aragun_istisnalari or [],
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
