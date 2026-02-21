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
    parse_kisitlama_istisnalari,
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
    error_type = type(e).__name__
    if isinstance(e, (ValueError, TypeError)):
        detail = f"Geçersiz veri formatı: {str(e)[:200]}"
        status = 400
    elif isinstance(e, KeyError):
        detail = f"Eksik alan: {str(e)[:200]}"
        status = 400
    else:
        detail = "Beklenmeyen bir hata oluştu. Lütfen tekrar deneyin."
        status = 500
    return _json_response({
        "error": detail,
        "error_type": error_type,
        "context": context,
    }, status=status)


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

        return _json_response({
            "basari": sonuc.basarili, "hedefler": sonuc.hedefler,
            "birlikteAtamalar": sonuc.birlikte_atamalar,
            "gorevKotalari": sonuc.gorev_kotalari,
            "istatistikler": sonuc.istatistikler, "mesaj": sonuc.mesaj
        })

    except Exception as e:
        return _error_response(e, "nobet_hedef_hesapla")


# ============================================
# GREEDY FALLBACK YARDIMCISI
# ============================================

def _greedy_fallback(personeller, gorevler, gun_sayisi, gun_tipleri,
                     kurallar, hedefler, ara_gun, yil, ay, resmi_tatiller,
                     gorev_kisitlamalari=None):
    """SolverPersonel -> Personel dönüşümü yapıp greedy solver çalıştırır,
    sonucu SolverSonuc formatına çevirir."""
    import time as _time
    baslangic = _time.time()

    # SolverGorev -> GorevTanim dönüşümü
    greedy_gorevler = []
    for g in gorevler:
        greedy_gorevler.append(GorevTanim(
            id=g.id, ad=g.ad, slot_index=g.slot_idx,
            base_name=g.base_name if g.base_name else g.ad,
            ayri_bina=g.ayri_bina
        ))

    # SolverPersonel -> Personel dönüşümü
    greedy_personeller = []
    for sp in personeller:
        h = hedefler.get(sp.id, {})
        hedef_tipler = h.get('hedef_tipler', {})
        hedef_toplam = h.get('hedef_toplam', 3)

        greedy_p = Personel(
            id=sp.id, ad=sp.ad,
            hedef_toplam=hedef_toplam,
            hedef_hici=hedef_tipler.get('hici', 0),
            hedef_prs=hedef_tipler.get('prs', 0),
            hedef_cum=hedef_tipler.get('cum', 0),
            hedef_cmt=hedef_tipler.get('cmt', 0),
            hedef_pzr=hedef_tipler.get('pzr', 0),
            hedef_roller=h.get('gorev_kotalari', {}),
            mazeret_gunleri=sp.mazeret_gunleri.copy()
        )
        greedy_personeller.append(greedy_p)

    # Greedy kurallar: SolverKural -> dict format
    greedy_kurallar = []
    for k in kurallar:
        kural_dict = {'tur': k.tur}
        for i, pid in enumerate(k.kisiler):
            # Kişi adını bul
            p = next((pp for pp in greedy_personeller if pp.id == pid), None)
            if p:
                kural_dict[f'p{i+1}'] = p.ad
        greedy_kurallar.append(kural_dict)

    takvim = build_takvim(yil, ay, resmi_tatiller)
    yonetici = NobetYoneticisi(
        personeller=greedy_personeller,
        gunluk_sayi=len(greedy_gorevler),
        takvim=takvim,
        ara_gun=ara_gun,
        days_in_month=gun_sayisi,
        gorev_tanimlari=greedy_gorevler,
        kurallar=greedy_kurallar,
        gorev_kisitlamalari=gorev_kisitlamalari
    )
    yonetici.dagit()

    # Greedy sonucunu SolverSonuc formatına dönüştür
    atamalar = []
    for gun, slotlar in yonetici.cizelge.items():
        for slot_idx, personel_ad in enumerate(slotlar):
            if personel_ad is not None:
                gorev = greedy_gorevler[slot_idx] if slot_idx < len(greedy_gorevler) else None
                gorev_ad = gorev.ad if gorev else f'Slot {slot_idx}'
                base_name = gorev.base_name if gorev and gorev.base_name else gorev_ad
                gun_tipi = gun_tipleri.get(gun, 'hici')
                p = next((pp for pp in greedy_personeller if pp.ad == personel_ad), None)
                pid = p.id if p else 0
                atamalar.append({
                    'gun': gun, 'slot_idx': slot_idx, 'gorev_ad': gorev_ad,
                    'gorev_base': base_name, 'personel_id': pid,
                    'personel_ad': personel_ad, 'gun_tipi': gun_tipi
                })

    toplam_atama = len(atamalar)
    toplam_slot = gun_sayisi * len(greedy_gorevler)
    sure_ms = int((_time.time() - baslangic) * 1000)

    return SolverSonuc(
        basarili=toplam_atama > 0,
        atamalar=atamalar,
        istatistikler={
            'status': 'GREEDY_FALLBACK',
            'toplam_atama': toplam_atama,
            'toplam_slot': toplam_slot,
            'doluluk_yuzde': round(100 * toplam_atama / toplam_slot, 1) if toplam_slot > 0 else 0,
            'bos_slot_sayisi': toplam_slot - toplam_atama,
            'ara_gun': ara_gun,
        },
        sure_ms=sure_ms,
        mesaj='GREEDY_FALLBACK'
    )


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
        kisitlama_istisnalari = parse_kisitlama_istisnalari(data, personeller, gorevler)
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
            hesap_sonuc = hesaplayici.hesapla()
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

        # ============================================
        # AKILLI TEŞHİS TABANLI ÇÖZÜM STRATEJİSİ
        # ============================================
        import time as _time
        baslangic_toplam = _time.time()
        tani_mesajlari = []
        gevsetme_bilgisi = {}
        teshis_bilgisi = {}

        # Zaman bütçelemesi: max_sure'yi fazlara böl
        sure_ilk = int(max_sure * 0.50)   # İlk deneme: %50
        sure_gevsetme = int(max_sure * 0.40)  # Gevşetme denemeleri: %40
        # Greedy: <1s

        sonuc = None
        kullanilan_ara_gun = ara_gun

        # ---- FAZA 1: Orijinal parametrelerle çöz ----
        solver = NobetSolver(
            gun_sayisi=gun_sayisi, gun_tipleri=gun_tipleri,
            personeller=personeller, gorevler=gorevler,
            kurallar=kurallar, gorev_havuzlari=gorev_havuzlari,
            kisitlama_istisnalari=kisitlama_istisnalari,
            manuel_atamalar=manuel_atamalar, hedefler=hedefler,
            ara_gun=ara_gun, max_sure_saniye=sure_ilk
        )
        sonuc = solver.coz()

        # ---- FAZA 2: INFEASIBLE ise akıllı teşhis ve otomatik gevşetme ----
        if sonuc and not sonuc.basarili:
            tani_mesajlari.append("Ilk deneme basarisiz, teshis baslatiliyor...")

            # Teşhis: Neden INFEASIBLE olduğunu analiz et
            diagnostics = solver._build_feasibility_diagnostics()
            aksiyonlar = solver._diagnose_infeasible(diagnostics)

            # Teşhis bilgisini kaydet
            teshis_bilgisi = {
                'kok_neden': aksiyonlar[0]['aksiyon'] if aksiyonlar else 'bilinmiyor',
                'kok_neden_aciklama': aksiyonlar[0]['neden'] if aksiyonlar else '',
                'teshis_sira': [
                    {'aksiyon': a['aksiyon'], 'puan': a['puan'], 'neden': a['neden']}
                    for a in aksiyonlar
                ],
                'zero_candidate_count': diagnostics.get('slot_day_zero_candidate_count', 0),
                'kapasite_sorunlari': len(diagnostics.get('role_ara_gun_capacity_issues', []))
            }
            tani_mesajlari.append(
                f"Teshis: Kok neden = {teshis_bilgisi['kok_neden']}, "
                f"Aciklama: {teshis_bilgisi['kok_neden_aciklama']}"
            )

            # Gevşetme için kalan süreyi hesapla
            gecen_sure = _time.time() - baslangic_toplam
            kalan_sure = max(max_sure - gecen_sure, 5)
            aksiyon_sayisi = len(aksiyonlar)
            sure_per_aksiyon = max(int(kalan_sure / max(aksiyon_sayisi, 1)), 3)

            # Hazırlık: exclusive-free görev listesi (gerekirse kullanılacak)
            gorevler_noexcl = [
                SolverGorev(
                    id=g.id, ad=g.ad, slot_idx=g.slot_idx,
                    base_name=g.base_name, exclusive=False,
                    ayri_bina=g.ayri_bina
                ) for g in gorevler
            ]

            # Kümülatif gevşetme durumu
            aktif_gorevler = gorevler  # Başlangıçta orijinal görevler
            aktif_kurallar = kurallar  # Başlangıçta orijinal kurallar
            aktif_havuzlar = gorev_havuzlari
            aktif_ara_gun = ara_gun

            # Her aksiyonu sırayla dene
            for aksiyon_info in aksiyonlar:
                if sonuc.basarili:
                    break

                aksiyon = aksiyon_info['aksiyon']
                tani_mesajlari.append(
                    f"Gevsetme denemesi: {aksiyon} (puan: {aksiyon_info['puan']})"
                )

                if aksiyon == 'ara_gun_azalt':
                    # Ara günü kademeli azalt
                    for dene_ara_gun in range(aktif_ara_gun, 0, -1):
                        if dene_ara_gun == aktif_ara_gun and aktif_ara_gun == ara_gun:
                            continue  # İlk denemede zaten denendi
                        solver = NobetSolver(
                            gun_sayisi=gun_sayisi, gun_tipleri=gun_tipleri,
                            personeller=personeller, gorevler=aktif_gorevler,
                            kurallar=aktif_kurallar, gorev_havuzlari=aktif_havuzlar,
                            kisitlama_istisnalari=kisitlama_istisnalari,
                            manuel_atamalar=manuel_atamalar, hedefler=hedefler,
                            ara_gun=dene_ara_gun, max_sure_saniye=sure_per_aksiyon
                        )
                        sonuc = solver.coz()
                        if sonuc.basarili:
                            kullanilan_ara_gun = dene_ara_gun
                            gevsetme_bilgisi['ara_gun_gevsetildi'] = True
                            tani_mesajlari.append(
                                f"Ara gun {ara_gun}->{dene_ara_gun} gevsetilerek cozum bulundu"
                            )
                            break
                    aktif_ara_gun = 0  # Sonraki aksiyonlarda ara gün=0 kullan

                elif aksiyon == 'exclusive_gevset':
                    aktif_gorevler = gorevler_noexcl
                    for dene_ara_gun in range(aktif_ara_gun, 0, -1):
                        solver = NobetSolver(
                            gun_sayisi=gun_sayisi, gun_tipleri=gun_tipleri,
                            personeller=personeller, gorevler=aktif_gorevler,
                            kurallar=aktif_kurallar, gorev_havuzlari=aktif_havuzlar,
                            kisitlama_istisnalari=kisitlama_istisnalari,
                            manuel_atamalar=manuel_atamalar, hedefler=hedefler,
                            ara_gun=dene_ara_gun, max_sure_saniye=sure_per_aksiyon
                        )
                        sonuc = solver.coz()
                        if sonuc.basarili:
                            kullanilan_ara_gun = dene_ara_gun
                            gevsetme_bilgisi['exclusive_gevsetildi'] = True
                            tani_mesajlari.append(
                                "Exclusive kisitlar gevsetilerek cozum bulundu"
                            )
                            break

                elif aksiyon == 'ayri_gevset':
                    # Ayrı kurallarını kaldır (birlikte korunur)
                    aktif_kurallar = [k for k in aktif_kurallar if k.tur != 'ayri']
                    for dene_ara_gun in range(aktif_ara_gun, 0, -1):
                        solver = NobetSolver(
                            gun_sayisi=gun_sayisi, gun_tipleri=gun_tipleri,
                            personeller=personeller, gorevler=aktif_gorevler,
                            kurallar=aktif_kurallar, gorev_havuzlari=aktif_havuzlar,
                            kisitlama_istisnalari=kisitlama_istisnalari,
                            manuel_atamalar=manuel_atamalar, hedefler=hedefler,
                            ara_gun=dene_ara_gun, max_sure_saniye=sure_per_aksiyon
                        )
                        sonuc = solver.coz()
                        if sonuc.basarili:
                            kullanilan_ara_gun = dene_ara_gun
                            gevsetme_bilgisi['ayri_gevsetildi'] = True
                            tani_mesajlari.append(
                                "Ayri tutma kurallari kaldirildiktan sonra cozum bulundu"
                            )
                            break

                elif aksiyon == 'birlikte_kaldir':
                    aktif_kurallar = [k for k in aktif_kurallar if k.tur != 'birlikte']
                    for dene_ara_gun in range(aktif_ara_gun, 0, -1):
                        solver = NobetSolver(
                            gun_sayisi=gun_sayisi, gun_tipleri=gun_tipleri,
                            personeller=personeller, gorevler=aktif_gorevler,
                            kurallar=aktif_kurallar, gorev_havuzlari=aktif_havuzlar,
                            kisitlama_istisnalari=kisitlama_istisnalari,
                            manuel_atamalar=manuel_atamalar, hedefler=hedefler,
                            ara_gun=dene_ara_gun, max_sure_saniye=sure_per_aksiyon
                        )
                        sonuc = solver.coz()
                        if sonuc.basarili:
                            kullanilan_ara_gun = dene_ara_gun
                            gevsetme_bilgisi['birlikte_kaldirildi'] = True
                            tani_mesajlari.append(
                                "Birlikte kurallari kaldirildiktan sonra cozum bulundu"
                            )
                            break

                elif aksiyon == 'tum_soft_kaldir':
                    aktif_kurallar = []
                    aktif_havuzlar = {}
                    for dene_ara_gun in range(max(1, aktif_ara_gun), 0, -1):
                        solver = NobetSolver(
                            gun_sayisi=gun_sayisi, gun_tipleri=gun_tipleri,
                            personeller=personeller, gorevler=gorevler_noexcl,
                            kurallar=[], gorev_havuzlari={},
                            kisitlama_istisnalari=kisitlama_istisnalari,
                            manuel_atamalar=manuel_atamalar, hedefler=hedefler,
                            ara_gun=dene_ara_gun, max_sure_saniye=sure_per_aksiyon
                        )
                        sonuc = solver.coz()
                        if sonuc.basarili:
                            kullanilan_ara_gun = dene_ara_gun
                            gevsetme_bilgisi['tum_soft_kaldirildi'] = True
                            tani_mesajlari.append(
                                "Tum soft kisitlar kaldirildiktan sonra cozum bulundu"
                            )
                            break

                elif aksiyon == 'greedy':
                    tani_mesajlari.append("Greedy fallback baslatiliyor")
                    greedy_sonuc = _greedy_fallback(
                        personeller=personeller, gorevler=gorevler,
                        gun_sayisi=gun_sayisi, gun_tipleri=gun_tipleri,
                        kurallar=kurallar, hedefler=hedefler,
                        ara_gun=min(1, ara_gun), yil=yil, ay=ay,
                        resmi_tatiller=resmi_tatiller,
                        gorev_kisitlamalari=data.get('gorevKisitlamalari', [])
                    )
                    if greedy_sonuc and greedy_sonuc.basarili:
                        sonuc = greedy_sonuc
                        gevsetme_bilgisi['greedy_fallback'] = True
                        tani_mesajlari.append(
                            "Greedy fallback ile cozum uretildi (kalite dusuk olabilir)"
                        )
                    else:
                        tani_mesajlari.append("Greedy fallback da basarisiz oldu")

        # Sonuç yoksa varsayılan hata
        if sonuc is None:
            sonuc = SolverSonuc(
                basarili=False, atamalar=[],
                istatistikler={'status': 'NO_SOLUTION', 'ara_gun': ara_gun},
                sure_ms=0, mesaj="Cozum uretilemedi - parametre hatasi olabilir"
            )
            kullanilan_ara_gun = ara_gun

        # Toplam süreyi güncelle
        toplam_sure_ms = int((_time.time() - baslangic_toplam) * 1000)
        sonuc = SolverSonuc(
            basarili=sonuc.basarili, atamalar=sonuc.atamalar,
            istatistikler={
                **sonuc.istatistikler,
                'tani_mesajlari': tani_mesajlari,
                'gevsetme_bilgisi': gevsetme_bilgisi,
                'teshis': teshis_bilgisi,
                **(
                    {'fallback_ara_gun': kullanilan_ara_gun, 'istenen_ara_gun': ara_gun}
                    if kullanilan_ara_gun != ara_gun else {}
                ),
            },
            sure_ms=toplam_sure_ms,
            mesaj=(
                f"{sonuc.mesaj} (ara_gun {ara_gun}->{kullanilan_ara_gun} gevsetildi)"
                if kullanilan_ara_gun != ara_gun and sonuc.basarili
                else sonuc.mesaj
            )
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

        return _json_response({
            "basari": sonuc.basarili, "mesaj": sonuc.mesaj, "sureMs": sonuc.sure_ms,
            "cizelge": cizelge, "atamalar": sonuc.atamalar,
            "istatistikler": sonuc.istatistikler,
            "kaliteUyarilari": kalite_uyarilari,
            "teshis": teshis_bilgisi,
            "gorevler": [g.ad for g in gorevler], "hedefDebug": hedef_debug
        })

    except Exception as e:
        return _error_response(e, "nobet_coz")
