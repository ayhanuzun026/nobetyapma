"""
Nöbet Yapma — Firebase Cloud Functions giriş noktası.
4 endpoint: nobet_dagit, nobet_kapasite, nobet_hedef_hesapla, nobet_coz
"""

from firebase_functions import https_fn
from firebase_admin import initialize_app, storage
from datetime import datetime, timedelta
import json
import logging

from utils import (
    _safe_int, get_days_in_month,
    normalize_id, ids_match,
    _find_duplicate_personel_ids, _resolve_personel_id,
)
from models import GorevTanim, Personel
from greedy_solver import NobetYoneticisi
from excel_export import create_excel
from ortools_solver import (
    SolverPersonel, SolverGorev, SolverKural, SolverAtama,
    NobetSolver, HedefHesaplayici, kapasite_hesapla,
    SolverSonuc,
)
from parsers import (
    build_takvim, build_gun_tipleri,
    parse_gorev_tanimlari, parse_greedy_personeller, parse_greedy_manuel_atamalar,
    parse_kapasite_personeller,
    parse_solver_gorevler, parse_solver_gorevler_nobet_coz,
    parse_solver_personeller_hedef, parse_solver_personeller_coz,
    parse_kurallar, parse_birlikte_kurallar,
    parse_gorev_kisitlamalari, parse_manuel_atamalar, parse_gorev_havuzlari,
)

initialize_app()
logger = logging.getLogger(__name__)


# ============================================
# CORS VE HATA YARDIMCILARI
# ============================================

CORS_HEADERS = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type'
}


def _cors_preflight():
    return https_fn.Response("", status=204, headers=CORS_HEADERS)


def _json_response(payload: dict, status: int = 200):
    return https_fn.Response(json.dumps(payload), status=status, headers=CORS_HEADERS)


def _error_response(e: Exception, context: str = ""):
    logger.exception("Sunucu hatası [%s]", context)
    return _json_response({"error": "Sunucu hatası oluştu. Lütfen tekrar deneyin."}, status=500)


# ============================================
# ENDPOINT: nobet_dagit (Greedy)
# ============================================

@https_fn.on_request(min_instances=0, max_instances=10, timeout_sec=540, memory=1024)
def nobet_dagit(req: https_fn.Request) -> https_fn.Response:
    if req.method == 'OPTIONS':
        return _cors_preflight()

    try:
        data = req.get_json(silent=True)
        if not data:
            return _json_response({"error": "No data"}, status=400)

        yil = int(data.get("yil", 2025))
        ay = int(data.get("ay", 1))
        gunluk_sayi = int(data.get("gunlukSayi", 5))
        ara_gun = int(data.get("araGun", 2))
        kurallar = data.get("kurallar", [])
        gorev_kisitlamalari = data.get("gorevKisitlamalari", [])

        gorev_objs = parse_gorev_tanimlari(data, gunluk_sayi)
        days_in_month = get_days_in_month(yil, ay)
        resmi_tatiller = data.get("resmiTatiller", [])
        takvim = build_takvim(yil, ay, resmi_tatiller)
        personeller = parse_greedy_personeller(data)

        duplicate_ids = _find_duplicate_personel_ids(personeller)
        if duplicate_ids:
            return _json_response({"error": "Duplicate personel ID", "duplicateIds": duplicate_ids}, status=400)

        yonetici = NobetYoneticisi(
            personeller=personeller, gunluk_sayi=gunluk_sayi, takvim=takvim,
            ara_gun=ara_gun, days_in_month=days_in_month,
            gorev_tanimlari=gorev_objs, kurallar=kurallar,
            gorev_kisitlamalari=gorev_kisitlamalari
        )

        parse_greedy_manuel_atamalar(data, personeller, gorev_objs, days_in_month, yonetici)
        yonetici.dagit()

        sonuc_cizelge = {str(gun): atamalar for gun, atamalar in yonetici.cizelge.items()}

        kisi_ozet = []
        eksik_atamalar = []
        for p in personeller:
            gerceklesen = len(p.atanan_gunler)
            fark = p.hedef_toplam - gerceklesen
            kisi_ozet.append({
                "ad": p.ad, "hedef": p.hedef_toplam, "gerceklesen": gerceklesen, "fark": fark,
                "kalanHici": p.kalan_hici, "kalanPrs": p.kalan_prs, "kalanCum": p.kalan_cum,
                "kalanCmt": p.kalan_cmt, "kalanPzr": p.kalan_pzr
            })
            if fark > 0:
                eksik_atamalar.append({
                    "personel": p.ad, "eksik": fark,
                    "detay": {"hici": p.kalan_hici, "prs": p.kalan_prs, "cum": p.kalan_cum,
                              "cmt": p.kalan_cmt, "pzr": p.kalan_pzr}
                })

        excel_file = create_excel(yil, ay, yonetici)
        bucket = storage.bucket()
        dosya_adi = f"sonuclar/nobet_{yil}_{ay}_{int(datetime.now().timestamp())}.xlsx"
        blob = bucket.blob(dosya_adi)
        blob.upload_from_file(
            excel_file,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        signed_url = blob.generate_signed_url(version="v4", expiration=timedelta(hours=1), method="GET")

        return _json_response({
            "basari": True, "excelUrl": signed_url, "cizelge": sonuc_cizelge,
            "kisiOzet": kisi_ozet, "eksikAtamalar": eksik_atamalar,
            "gorevler": [g.ad for g in yonetici.gorevler]
        })

    except Exception as e:
        return _error_response(e, "nobet_dagit")


# ============================================
# ENDPOINT: nobet_kapasite
# ============================================

@https_fn.on_request(min_instances=0, max_instances=10, timeout_sec=60, memory=512)
def nobet_kapasite(req: https_fn.Request) -> https_fn.Response:
    if req.method == 'OPTIONS':
        return _cors_preflight()

    try:
        data = req.get_json(silent=True)
        if not data:
            return _json_response({"error": "No data"}, status=400)

        yil = _safe_int(data.get("yil", 2025), 2025)
        ay = _safe_int(data.get("ay", 1), 1)
        slot_sayisi = _safe_int(data.get("slotSayisi", 5), 5)
        resmi_tatiller = data.get("resmiTatiller", [])

        gun_sayisi = get_days_in_month(yil, ay)
        gun_tipleri = build_gun_tipleri(yil, ay, gun_sayisi, resmi_tatiller)
        personeller = parse_kapasite_personeller(data)

        duplicate_ids = _find_duplicate_personel_ids(personeller)
        if duplicate_ids:
            return _json_response({"error": "Duplicate personel ID", "duplicateIds": duplicate_ids}, status=400)

        sonuc = kapasite_hesapla(
            gun_sayisi=gun_sayisi, gun_tipleri=gun_tipleri,
            personeller=personeller, slot_sayisi=slot_sayisi
        )

        return _json_response({"basari": True, **sonuc})

    except Exception as e:
        return _error_response(e, "nobet_kapasite")


# ============================================
# ENDPOINT: nobet_hedef_hesapla
# ============================================

@https_fn.on_request(min_instances=0, max_instances=5, timeout_sec=300, memory=1024)
def nobet_hedef_hesapla(req: https_fn.Request) -> https_fn.Response:
    if req.method == 'OPTIONS':
        return _cors_preflight()

    try:
        data = req.get_json(silent=True)
        if not data:
            return _json_response({"error": "No data"}, status=400)

        gun_sayisi = _safe_int(data.get("gunSayisi", 31), 31)
        gun_tipleri_raw = data.get("gunTipleri", {})
        gun_tipleri = {int(k): v for k, v in gun_tipleri_raw.items()}
        ara_gun = _safe_int(data.get("araGun", 2), 2)
        saat_degerleri = data.get("saatDegerleri", None)

        personeller = parse_solver_personeller_hedef(data)

        duplicate_ids = _find_duplicate_personel_ids(personeller)
        if duplicate_ids:
            return _json_response({"error": "Duplicate personel ID", "duplicateIds": duplicate_ids}, status=400)

        gorevler = parse_solver_gorevler(data)
        birlikte_kurallar = parse_birlikte_kurallar(data, personeller)
        gorev_kisitlamalari = parse_gorev_kisitlamalari(data, personeller)
        manuel_atamalar = parse_manuel_atamalar(data, personeller, gorevler, gun_sayisi)

        hesaplayici = HedefHesaplayici(
            gun_sayisi=gun_sayisi, gun_tipleri=gun_tipleri,
            personeller=personeller, gorevler=gorevler,
            birlikte_kurallar=birlikte_kurallar,
            gorev_kisitlamalari=gorev_kisitlamalari,
            manuel_atamalar=manuel_atamalar,
            ara_gun=ara_gun, saat_degerleri=saat_degerleri
        )
        sonuc = hesaplayici.hesapla()

        return _json_response({
            "basari": sonuc.basarili, "hedefler": sonuc.hedefler,
            "birlikteAtamalar": sonuc.birlikte_atamalar,
            "gorevKotalari": sonuc.gorev_kotalari,
            "istatistikler": sonuc.istatistikler, "mesaj": sonuc.mesaj
        })

    except Exception as e:
        return _error_response(e, "nobet_hedef_hesapla")


# ============================================
# ENDPOINT: nobet_coz
# ============================================

@https_fn.on_request(min_instances=0, max_instances=5, timeout_sec=540, memory=2048)
def nobet_coz(req: https_fn.Request) -> https_fn.Response:
    if req.method == 'OPTIONS':
        return _cors_preflight()

    try:
        data = req.get_json(silent=True)
        if not data:
            return _json_response({"error": "No data"}, status=400)

        yil = _safe_int(data.get("yil", 2025), 2025)
        ay = _safe_int(data.get("ay", 1), 1)
        slot_sayisi = _safe_int(data.get("slotSayisi", 6), 6)
        ara_gun = _safe_int(data.get("araGun", 2), 2)
        max_sure = _safe_int(data.get("maxSure", 300), 300)
        resmi_tatiller = data.get("resmiTatiller", [])
        saat_degerleri = data.get("saatDegerleri", None)

        gun_sayisi = get_days_in_month(yil, ay)
        gun_tipleri = build_gun_tipleri(yil, ay, gun_sayisi, resmi_tatiller)
        gorevler = parse_solver_gorevler_nobet_coz(data, slot_sayisi)
        personeller = parse_solver_personeller_coz(data, gorevler)

        duplicate_ids = _find_duplicate_personel_ids(personeller)
        if duplicate_ids:
            return _json_response({"error": "Duplicate personel ID", "duplicateIds": duplicate_ids}, status=400)

        gorev_havuzlari = parse_gorev_havuzlari(data, gorevler, personeller)
        kurallar = parse_kurallar(data, personeller)
        manuel_atamalar = parse_manuel_atamalar(data, personeller, gorevler, gun_sayisi)

        # Hedefleri hazırla
        frontend_hedefleri_var = any(
            p.hedef_tipler and sum(p.hedef_tipler.values()) > 0
            for p in personeller
        )

        if frontend_hedefleri_var:
            hedefler = {}
            toplam_slot = gun_sayisi * len(gorevler)
            kisi_sayisi = len(personeller)
            kisi_basi_hedef = toplam_slot // kisi_sayisi if kisi_sayisi > 0 else 0
            kalan = toplam_slot % kisi_sayisi if kisi_sayisi > 0 else 0

            for idx, p in enumerate(personeller):
                if p.hedef_tipler and sum(p.hedef_tipler.values()) > 0:
                    hedef_toplam = sum(p.hedef_tipler.values())
                else:
                    musait_gun = gun_sayisi - len(p.mazeret_gunleri)
                    hedef_toplam = min(kisi_basi_hedef + (1 if idx < kalan else 0), musait_gun)

                hedefler[p.id] = {
                    'hedef_toplam': hedef_toplam,
                    'hedef_tipler': p.hedef_tipler or {},
                    'gorev_kotalari': p.gorev_kotalari or {}
                }
        else:
            birlikte_kurallar = [k for k in kurallar if k.tur == 'birlikte']
            gorev_kisitlamalari_dict = parse_gorev_kisitlamalari(data, personeller)

            hesaplayici = HedefHesaplayici(
                gun_sayisi=gun_sayisi, gun_tipleri=gun_tipleri,
                personeller=personeller, gorevler=gorevler,
                birlikte_kurallar=birlikte_kurallar,
                gorev_kisitlamalari=gorev_kisitlamalari_dict,
                manuel_atamalar=manuel_atamalar,
                ara_gun=ara_gun, saat_degerleri=saat_degerleri
            )
            hesap_sonuc = hesaplayici.hesapla()
            hedefler = {}
            for h in hesap_sonuc.hedefler:
                pid = h.get('id')
                hedefler[pid] = {
                    'hedef_toplam': h.get('hedef_toplam', 0),
                    'hedef_tipler': h.get('hedef_tipler', {}),
                    'gorev_kotalari': h.get('gorev_kotalari', {})
                }

        # Kademeli çözüm
        sonuc = None
        kullanilan_ara_gun = ara_gun
        for dene_ara_gun in range(ara_gun, -1, -1):
            solver = NobetSolver(
                gun_sayisi=gun_sayisi, gun_tipleri=gun_tipleri,
                personeller=personeller, gorevler=gorevler,
                kurallar=kurallar, gorev_havuzlari=gorev_havuzlari,
                manuel_atamalar=manuel_atamalar, hedefler=hedefler,
                ara_gun=dene_ara_gun, max_sure_saniye=max_sure
            )
            sonuc = solver.coz()
            kullanilan_ara_gun = dene_ara_gun
            if sonuc.basarili:
                break

        if sonuc is None:
            sonuc = SolverSonuc(
                basarili=False, atamalar=[],
                istatistikler={'status': 'NO_SOLUTION', 'ara_gun': ara_gun},
                sure_ms=0, mesaj="Çözüm üretilemedi - parametre hatası olabilir"
            )
            kullanilan_ara_gun = ara_gun

        if kullanilan_ara_gun != ara_gun and sonuc.basarili:
            sonuc = SolverSonuc(
                basarili=sonuc.basarili, atamalar=sonuc.atamalar,
                istatistikler={**sonuc.istatistikler,
                               'fallback_ara_gun': kullanilan_ara_gun,
                               'istenen_ara_gun': ara_gun},
                sure_ms=sonuc.sure_ms,
                mesaj=f"{sonuc.mesaj} (ara_gun {ara_gun}->{kullanilan_ara_gun} gevsetildi)"
            )

        # Çizelge formatına dönüştür
        cizelge = {}
        for g in range(1, gun_sayisi + 1):
            cizelge[str(g)] = [None] * len(gorevler)

        for atama in sonuc.atamalar:
            cizelge[str(atama['gun'])][atama['slot_idx']] = atama['personel_ad']

        hedef_debug = []
        for p in personeller:
            h = hedefler.get(p.id, {})
            hedef_debug.append({
                'id': p.id, 'ad': p.ad,
                'hedef_toplam': h.get('hedef_toplam', 0),
                'hedef_tipler': h.get('hedef_tipler', {}),
                'mazeret_sayisi': len(p.mazeret_gunleri)
            })

        return _json_response({
            "basari": sonuc.basarili, "mesaj": sonuc.mesaj, "sureMs": sonuc.sure_ms,
            "cizelge": cizelge, "atamalar": sonuc.atamalar,
            "istatistikler": sonuc.istatistikler,
            "gorevler": [g.ad for g in gorevler], "hedefDebug": hedef_debug
        })

    except Exception as e:
        return _error_response(e, "nobet_coz")
