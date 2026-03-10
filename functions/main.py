"""
Nöbet Yapma — Firebase Cloud Functions giriş noktası.
5 endpoint: nobet_dagit, nobet_kapasite, nobet_hedef_hesapla, nobet_coz, debug_event_log
"""

from firebase_functions import https_fn
from firebase_admin import initialize_app, storage
from datetime import datetime, timedelta
import logging
import time

from utils import (
    _safe_int, get_days_in_month,
    normalize_id,
    _find_duplicate_personel_ids,
)
from greedy_solver import NobetYoneticisi
from excel_export import create_excel
from solver_models import SolverSonuc
from ortools_solver import NobetSolver
from hedef_hesaplayici import HedefHesaplayici
from kapasite import kapasite_hesapla
from http_helpers import _cors_preflight, _json_response, _error_response
from solve_strategy import solve_with_diagnostics
from firestore_logger import log_session
from parsers import (
    build_takvim, build_gun_tipleri,
    parse_gorev_tanimlari, parse_greedy_personeller, parse_greedy_manuel_atamalar,
    parse_kapasite_personeller,
    parse_solver_gorevler, parse_solver_gorevler_nobet_coz,
    parse_solver_personeller_hedef, parse_solver_personeller_coz,
    parse_kurallar, parse_birlikte_kurallar,
    parse_gorev_kisitlamalari, parse_manuel_atamalar, parse_gorev_havuzlari,
    parse_kisitlama_istisnalari,
    parse_birlikte_istisnalari, parse_aragun_istisnalari,
)

initialize_app()
logger = logging.getLogger(__name__)


# ============================================
# ENDPOINT: nobet_dagit (Greedy)
# ============================================

@https_fn.on_request(min_instances=0, max_instances=10, timeout_sec=540, memory=1024)
def nobet_dagit(req: https_fn.Request) -> https_fn.Response:
    if req.method == 'OPTIONS':
        return _cors_preflight()

    t0 = time.time()
    data = None
    try:
        data = req.get_json(silent=True)
        if not data:
            return _json_response({"error": "Veri gönderilmedi"}, status=400)

        try:
            yil = int(data.get("yil", 2025))
            ay = int(data.get("ay", 1))
            gunluk_sayi = int(data.get("gunlukSayi", 5))
            ara_gun = int(data.get("araGun", 2))
        except (ValueError, TypeError) as ve:
            return _json_response({"error": f"Geçersiz parametre değeri: {ve}", "error_type": "ValueError"}, status=400)

        if not (1 <= ay <= 12):
            return _json_response({"error": f"Geçersiz ay değeri: {ay}"}, status=400)
        if not (2000 <= yil <= 2100):
            return _json_response({"error": f"Geçersiz yıl değeri: {yil}"}, status=400)

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

        cikti = {
            "basari": True, "excelUrl": signed_url, "cizelge": sonuc_cizelge,
            "kisiOzet": kisi_ozet, "eksikAtamalar": eksik_atamalar,
            "gorevler": [g.ad for g in yonetici.gorevler]
        }
        sure_ms = int((time.time() - t0) * 1000)
        log_session("nobet_dagit", data, cikti, sure_ms,
                    frontend_loglar=data.get("frontendLoglar"))
        return _json_response(cikti)

    except Exception as e:
        sure_ms = int((time.time() - t0) * 1000)
        log_session("nobet_dagit", data or {}, None, sure_ms, hata=e,
                    frontend_loglar=(data or {}).get("frontendLoglar"))
        return _error_response(e, "nobet_dagit")


# ============================================
# ENDPOINT: nobet_kapasite
# ============================================

@https_fn.on_request(min_instances=0, max_instances=10, timeout_sec=60, memory=512)
def nobet_kapasite(req: https_fn.Request) -> https_fn.Response:
    if req.method == 'OPTIONS':
        return _cors_preflight()

    t0 = time.time()
    data = None
    try:
        data = req.get_json(silent=True)
        if not data:
            return _json_response({"error": "Veri gönderilmedi"}, status=400)

        try:
            yil = _safe_int(data.get("yil", 2025), 2025)
            ay = _safe_int(data.get("ay", 1), 1)
            slot_sayisi = _safe_int(data.get("slotSayisi", 5), 5)
        except (ValueError, TypeError) as ve:
            return _json_response({"error": f"Geçersiz parametre değeri: {ve}", "error_type": "ValueError"}, status=400)

        if not (1 <= ay <= 12):
            return _json_response({"error": f"Geçersiz ay değeri: {ay}"}, status=400)

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

        cikti = {"basari": True, **sonuc}
        sure_ms = int((time.time() - t0) * 1000)
        log_session("nobet_kapasite", data, cikti, sure_ms,
                    frontend_loglar=data.get("frontendLoglar"))
        return _json_response(cikti)

    except Exception as e:
        sure_ms = int((time.time() - t0) * 1000)
        log_session("nobet_kapasite", data or {}, None, sure_ms, hata=e,
                    frontend_loglar=(data or {}).get("frontendLoglar"))
        return _error_response(e, "nobet_kapasite")


# ============================================
# ENDPOINT: nobet_hedef_hesapla
# ============================================

@https_fn.on_request(min_instances=0, max_instances=5, timeout_sec=300, memory=1024)
def nobet_hedef_hesapla(req: https_fn.Request) -> https_fn.Response:
    if req.method == 'OPTIONS':
        return _cors_preflight()

    t0 = time.time()
    data = None
    try:
        data = req.get_json(silent=True)
        if not data:
            return _json_response({"error": "Veri gönderilmedi"}, status=400)

        try:
            gun_sayisi = _safe_int(data.get("gunSayisi", 31), 31)
            gun_tipleri_raw = data.get("gunTipleri", {})
            gun_tipleri = {int(k): v for k, v in gun_tipleri_raw.items()}
            ara_gun = _safe_int(data.get("araGun", 2), 2)
        except (ValueError, TypeError) as ve:
            return _json_response({"error": f"Geçersiz parametre değeri: {ve}", "error_type": "ValueError"}, status=400)

        if gun_sayisi < 1 or gun_sayisi > 31:
            return _json_response({"error": f"Geçersiz gün sayısı: {gun_sayisi}"}, status=400)

        saat_degerleri = data.get("saatDegerleri", None)

        # Kilitli hedefler: {personelId: {hici: N, prs: N, cum: N, cmt: N, pzr: N}}
        kilitli_hedefler_raw = data.get("kilitliHedefler", {})
        kilitli_hedefler = {}
        for k, v in kilitli_hedefler_raw.items():
            kilitli_hedefler[normalize_id(k)] = {
                tip: int(v.get(tip, 0)) for tip in ["hici", "prs", "cum", "cmt", "pzr"]
            }

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
            ara_gun=ara_gun, saat_degerleri=saat_degerleri,
            kilitli_hedefler=kilitli_hedefler
        )
        sonuc = hesaplayici.hesapla()

        cikti = {
            "basari": sonuc.basarili, "hedefler": sonuc.hedefler,
            "birlikteAtamalar": sonuc.birlikte_atamalar,
            "gorevKotalari": sonuc.gorev_kotalari,
            "istatistikler": sonuc.istatistikler, "mesaj": sonuc.mesaj
        }
        sure_ms = int((time.time() - t0) * 1000)
        log_session("nobet_hedef_hesapla", data, cikti, sure_ms,
                    frontend_loglar=data.get("frontendLoglar"))
        return _json_response(cikti)

    except Exception as e:
        sure_ms = int((time.time() - t0) * 1000)
        log_session("nobet_hedef_hesapla", data or {}, None, sure_ms, hata=e,
                    frontend_loglar=(data or {}).get("frontendLoglar"))
        return _error_response(e, "nobet_hedef_hesapla")


# ============================================
# ENDPOINT: nobet_coz
# ============================================

@https_fn.on_request(min_instances=0, max_instances=5, timeout_sec=540, memory=2048)
def nobet_coz(req: https_fn.Request) -> https_fn.Response:
    if req.method == 'OPTIONS':
        return _cors_preflight()

    t0 = time.time()
    data = None
    try:
        data = req.get_json(silent=True)
        if not data:
            return _json_response({"error": "Veri gönderilmedi"}, status=400)

        try:
            yil = _safe_int(data.get("yil", 2025), 2025)
            ay = _safe_int(data.get("ay", 1), 1)
            slot_sayisi = _safe_int(data.get("slotSayisi", 6), 6)
            ara_gun = _safe_int(data.get("araGun", 2), 2)
            max_sure = _safe_int(data.get("maxSure", 300), 300)
        except (ValueError, TypeError) as ve:
            return _json_response({"error": f"Geçersiz parametre değeri: {ve}", "error_type": "ValueError"}, status=400)

        if not (1 <= ay <= 12):
            return _json_response({"error": f"Geçersiz ay değeri: {ay}"}, status=400)
        if not (2000 <= yil <= 2100):
            return _json_response({"error": f"Geçersiz yıl değeri: {yil}"}, status=400)
        if slot_sayisi < 1:
            return _json_response({"error": f"Geçersiz slot sayısı: {slot_sayisi}"}, status=400)
        if ara_gun < 0:
            return _json_response({"error": f"Geçersiz ara gün değeri: {ara_gun}"}, status=400)

        resmi_tatiller = data.get("resmiTatiller", [])
        saat_degerleri = data.get("saatDegerleri", None)

        gun_sayisi = get_days_in_month(yil, ay)
        gun_tipleri = build_gun_tipleri(yil, ay, gun_sayisi, resmi_tatiller)
        gorevler = parse_solver_gorevler_nobet_coz(data, slot_sayisi)
        personeller = parse_solver_personeller_coz(data, gorevler)

        logger.info("nobet_coz baslatildi: yil=%d, ay=%d, slot=%d, ara_gun=%d, personel=%d, gorev=%d",
                     yil, ay, slot_sayisi, ara_gun, len(personeller) if personeller else 0,
                     len(gorevler) if gorevler else 0)

        if not personeller:
            return _json_response({"error": "Personel listesi boş. En az 1 personel gereklidir."}, status=400)
        if not gorevler:
            return _json_response({"error": "Görev listesi boş. En az 1 görev tanımı gereklidir."}, status=400)
        if len(personeller) < slot_sayisi:
            logger.warning("Personel sayısı (%d) slot sayısından (%d) az — boş slotlar olabilir.",
                           len(personeller), slot_sayisi)

        duplicate_ids = _find_duplicate_personel_ids(personeller)
        if duplicate_ids:
            return _json_response({"error": "Duplicate personel ID", "duplicateIds": duplicate_ids}, status=400)

        gorev_havuzlari = parse_gorev_havuzlari(data, gorevler, personeller)
        kisitlama_istisnalari = parse_kisitlama_istisnalari(data, personeller, gorevler)
        birlikte_istisnalari = parse_birlikte_istisnalari(data, personeller)
        aragun_istisnalari = parse_aragun_istisnalari(data, personeller)
        kurallar = parse_kurallar(data, personeller)
        manuel_atamalar = parse_manuel_atamalar(data, personeller, gorevler, gun_sayisi)

        # Hedefleri hazırla
        frontend_hedefleri_var = any(
            p.hedef_tipler and sum(p.hedef_tipler.values()) > 0
            for p in personeller
        )

        if frontend_hedefleri_var:
            hedefler = {}
            for p in personeller:
                hedefler[p.id] = {
                    'hedef_toplam': sum(p.hedef_tipler.values()) if p.hedef_tipler else 0,
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
            try:
                hesap_sonuc = hesaplayici.hesapla()
            except Exception as hedef_err:
                logger.exception("Hedef hesaplama basarisiz: %s", hedef_err)
                sure_ms = int((time.time() - t0) * 1000)
                log_session("nobet_coz", data, None, sure_ms, hata=hedef_err,
                            frontend_loglar=data.get("frontendLoglar"))
                return _json_response({
                    "error": f"Hedef hesaplama sırasında hata oluştu: {str(hedef_err)[:200]}",
                    "error_type": "HedefHesaplamaHatasi"
                }, status=500)
            if not hesap_sonuc or not hesap_sonuc.hedefler:
                logger.error("Hedef hesaplama sonucu boş döndü")
                return _json_response({
                    "error": "Hedef hesaplama sonucu boş. Personel ve görev verilerini kontrol edin.",
                    "error_type": "HedefBos"
                }, status=400)
            hedefler = {}
            for h in hesap_sonuc.hedefler:
                pid = normalize_id(h.get('id'))
                hedef_tipler = h.get('hedef_tipler', {})
                if not hedef_tipler:
                    hedef_tipler = {
                        'hici': h.get('hedef_hici', 0),
                        'prs': h.get('hedef_prs', 0),
                        'cum': h.get('hedef_cum', 0),
                        'cmt': h.get('hedef_cmt', 0),
                        'pzr': h.get('hedef_pzr', 0),
                    }
                hedefler[pid] = {
                    'hedef_toplam': h.get('hedef_toplam', 0),
                    'hedef_tipler': hedef_tipler,
                    'gorev_kotalari': h.get('gorev_kotalari', {})
                }

        # Çözüm stratejisi
        sonuc, gevsetme_bilgisi, teshis_bilgisi, kullanilan_ara_gun = solve_with_diagnostics(
            gun_sayisi=gun_sayisi, gun_tipleri=gun_tipleri,
            personeller=personeller, gorevler=gorevler,
            kurallar=kurallar, gorev_havuzlari=gorev_havuzlari,
            kisitlama_istisnalari=kisitlama_istisnalari,
            birlikte_istisnalari=birlikte_istisnalari,
            aragun_istisnalari=aragun_istisnalari,
            manuel_atamalar=manuel_atamalar, hedefler=hedefler,
            ara_gun=ara_gun, max_sure=max_sure,
            yil=yil, ay=ay, resmi_tatiller=resmi_tatiller, data=data
        )

        # Çizelge formatına dönüştür
        cizelge = {}
        for g in range(1, gun_sayisi + 1):
            cizelge[str(g)] = [None] * len(gorevler)

        for atama in sonuc.atamalar:
            cizelge[str(atama['gun'])][atama['slot_idx']] = atama['personel_ad']

        hedef_debug = []
        for p in personeller:
            h = hedefler.get(p.id) or hedefler.get(normalize_id(p.id)) or {}
            hedef_debug.append({
                'id': p.id, 'ad': p.ad,
                'hedef_toplam': h.get('hedef_toplam', 0),
                'hedef_tipler': h.get('hedef_tipler', {}),
                'mazeret_sayisi': len(p.mazeret_gunleri)
            })

        # Kalite uyarıları oluştur
        kalite_uyarilari = []
        kalite_skoru = sonuc.istatistikler.get('kalite_skoru', {})
        if kalite_skoru:
            if kalite_skoru.get('denge_puani', 0) > 50:
                kalite_uyarilari.append(
                    f"Denge uyarisi: Nobet sayisi farki yuksek (%{kalite_skoru['denge_puani']}). "
                    "Personeller arasi nobet sayisi dengesi bozuk."
                )
            if kalite_skoru.get('doluluk', 100) < 95:
                kalite_uyarilari.append(
                    f"Doluluk uyarisi: Slotlarin %{kalite_skoru['doluluk']}'i dolu. "
                    "Bos kalan slotlar var."
                )
            if kalite_skoru.get('kural_uyumu', 100) < 80:
                kalite_uyarilari.append(
                    f"Hedef uyumu uyarisi: Hedeflerden sapma yuksek (%{kalite_skoru['kural_uyumu']} uyum). "
                    "Personellerin hedeflerine ulasilamamis olabilir."
                )
            if kalite_skoru.get('saat_adaleti', 0) > 30:
                kalite_uyarilari.append(
                    f"Saat adaleti uyarisi: Saat dagilimi dengesiz (%{kalite_skoru['saat_adaleti']} sapma). "
                    "Bazi personeller daha fazla saat calisiyor."
                )

        cikti = {
            "basari": sonuc.basarili, "mesaj": sonuc.mesaj, "sureMs": sonuc.sure_ms,
            "cizelge": cizelge, "atamalar": sonuc.atamalar,
            "istatistikler": sonuc.istatistikler,
            "kaliteUyarilari": kalite_uyarilari,
            "teshis": teshis_bilgisi,
            "gorevler": [g.ad for g in gorevler], "hedefDebug": hedef_debug
        }
        sure_ms = int((time.time() - t0) * 1000)
        log_session("nobet_coz", data, cikti, sure_ms,
                    frontend_loglar=data.get("frontendLoglar"))
        return _json_response(cikti)

    except Exception as e:
        sure_ms = int((time.time() - t0) * 1000)
        log_session("nobet_coz", data or {}, None, sure_ms, hata=e,
                    frontend_loglar=(data or {}).get("frontendLoglar"))
        return _error_response(e, "nobet_coz")


# ============================================
# ENDPOINT: debug_event_log
# ============================================

@https_fn.on_request(min_instances=0, max_instances=5, timeout_sec=10, memory=256)
def debug_event_log(req: https_fn.Request) -> https_fn.Response:
    if req.method == 'OPTIONS':
        return _cors_preflight()

    try:
        data = req.get_json(silent=True)
        if not data:
            return _json_response({"ok": False, "error": "Veri yok"}, status=400)

        from firebase_admin import firestore as fs
        db = fs.client()
        from datetime import timezone
        ts = datetime.now(timezone.utc)

        db.collection("debug_events").add({
            "timestamp": ts,
            "tip": data.get("tip", "bilinmiyor"),
            "personelId": data.get("personelId"),
            "gun": data.get("gun"),
            "detay": data.get("detay"),
        })

        return _json_response({"ok": True})

    except Exception as e:
        logger.warning("debug_event_log hatasi: %s", e)
        return _json_response({"ok": False, "error": str(e)[:200]}, status=500)
