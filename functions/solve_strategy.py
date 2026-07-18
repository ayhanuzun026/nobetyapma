"""Akilli teshis tabanli cozum ve kontrollu gevsetme stratejisi."""

from copy import deepcopy
import logging
import time as _time

from ortools_solver import NobetSolver
from planlayici import plan_kontrati_hash_yenile
from solver_models import SolverGorev, SolverSonuc
from utils import find_matching_id

logger = logging.getLogger(__name__)


def _doluluk_raporu_uret(sonuc, bos_slot, gevsetme_denendi):
    """Bos slot durumunu kullaniciya net aksiyonlarla aciklar."""
    ist = (getattr(sonuc, "istatistikler", None) or {}) if sonuc else {}
    toplam_slot = int(ist.get("toplam_slot", 0) or 0)
    doluluk_yuzde = ist.get("doluluk_yuzde", 100 if bos_slot == 0 else 0)
    feasibility_debug = ist.get("feasibility_debug") or {}

    if bos_slot <= 0:
        oneri = "Takvim tam dolu."
    else:
        cozumler = [
            "1 kişi daha ekleyin",
            "bir personelin mazeretini kaldırın",
            "günlük slot sayısını azaltın",
        ]
        gevsetme_notu = (
            " Ara gün kuralı gevşetildiği hâlde açık kaldı."
            if gevsetme_denendi else ""
        )
        oneri = (
            f"{bos_slot} slot boş kaldı.{gevsetme_notu} "
            f"Çözüm: {' / '.join(cozumler)}."
        )

    return {
        "bos_slot": bos_slot,
        "toplam_slot": toplam_slot,
        "doluluk_yuzde": doluluk_yuzde,
        "gevsetme_denendi": gevsetme_denendi,
        "feasibility_debug": feasibility_debug,
        "oneri": oneri,
    }


def _sirala_birlikte_kurallari(kurallar, personeller, hedefler):
    personel_map = {p.id: p for p in personeller}
    birlikte_kurallari = []

    for kural in kurallar:
        if getattr(kural, "tur", None) != "birlikte":
            continue

        valid_ids = []
        for raw_pid in getattr(kural, "kisiler", []) or []:
            matched_id = find_matching_id(raw_pid, personel_map.keys())
            if matched_id is not None and matched_id not in valid_ids:
                valid_ids.append(matched_id)

        if len(valid_ids) < 2:
            continue

        ortak_gunler = None
        for pid in valid_ids:
            musait_gunler = set(getattr(personel_map[pid], "musait_gunler", set()) or set())
            ortak_gunler = musait_gunler if ortak_gunler is None else ortak_gunler & musait_gunler

        min_hedef = min(
            int((hedefler or {}).get(pid, {}).get("hedef_toplam", 0) or 0)
            for pid in valid_ids
        )
        birlikte_kurallari.append({
            "kural": kural,
            "valid_ids": valid_ids,
            "ortak_gun_sayisi": len(ortak_gunler or set()),
            "min_hedef": min_hedef,
            "grup_boyutu": len(valid_ids),
        })

    birlikte_kurallari.sort(key=lambda item: (
        item["ortak_gun_sayisi"],
        -item["grup_boyutu"],
        item["min_hedef"],
    ))
    return birlikte_kurallari


def solve_with_diagnostics(
    gun_sayisi, gun_tipleri, personeller, gorevler, kurallar,
    gorev_havuzlari, kisitlama_istisnalari, birlikte_istisnalari,
    aragun_istisnalari, manuel_atamalar, hedefler,
    ara_gun, max_sure, yil, ay, resmi_tatiller, data,
    ignore_manual_conflicts=False, plan_kontrati=None, plan_yenileyici=None,
):
    """Mutlak sure butcesi icinde cozer, teshis eder ve gerekirse gevsetir.

    Otomatik gevsetme sadece son solver sonucu kesin ``INFEASIBLE`` ise devam
    eder. ``UNKNOWN``, timeout, ``MODEL_INVALID`` ve diger hata durumlari
    kisitlarin kaldirilmasi icin kanit sayilmaz.
    """
    del yil, ay, resmi_tatiller, data  # Imza geriye uyumluluk icin korunuyor.

    baslangic_toplam = _time.monotonic()
    try:
        toplam_butce_s = max(float(max_sure), 0.0)
    except (TypeError, ValueError):
        toplam_butce_s = 0.0
    deadline = baslangic_toplam + toplam_butce_s
    min_deneme_suresi = 0.05

    tani_mesajlari = []
    gevsetme_bilgisi = {}
    teshis_bilgisi = {}
    deadline_notu_eklendi = False
    otomatik_gevsetme_durduruldu = False
    solver = None

    sonuc = None
    kullanilan_ara_gun = ara_gun
    aktif_plan_kontrati = (
        plan_kontrati.to_dict() if hasattr(plan_kontrati, "to_dict") else deepcopy(plan_kontrati)
    )
    if aktif_plan_kontrati:
        aktif_plan_kontrati = plan_kontrati_hash_yenile(aktif_plan_kontrati)

    aktif_gorevler = gorevler
    aktif_kurallar = kurallar
    aktif_havuzlar = gorev_havuzlari
    aktif_ara_gun = ara_gun

    def _kalan_sure():
        return max(deadline - _time.monotonic(), 0.0)

    def _butce_var():
        return _kalan_sure() >= min_deneme_suresi

    def _butce_notu(asama):
        nonlocal deadline_notu_eklendi
        if not deadline_notu_eklendi:
            tani_mesajlari.append(
                f"Global sure butcesi doldu; yeni islem baslatilmadi (asama={asama})."
            )
            deadline_notu_eklendi = True

    def _sonuc_status(deger):
        ist = getattr(deger, "istatistikler", None) or {}
        return str(ist.get("status", "NO_STATUS") or "NO_STATUS").upper()

    def _en_az_tolerans(uygulama, alan, alt_sinir):
        try:
            mevcut = int(uygulama.get(alan, 0) or 0)
        except (TypeError, ValueError):
            mevcut = 0
        return max(mevcut, alt_sinir)

    def _infeasible(deger):
        return bool(deger and not deger.basarili and _sonuc_status(deger) == "INFEASIBLE")

    def _deadline_sonucu(asama):
        return SolverSonuc(
            basarili=False,
            atamalar=[],
            istatistikler={
                "status": "DEADLINE_EXCEEDED",
                "ara_gun": ara_gun,
                "timeout_olasi": True,
                "reason_hint": f"Global sure butcesi {asama} oncesinde doldu.",
            },
            sure_ms=int((_time.monotonic() - baslangic_toplam) * 1000),
            mesaj="Cozum bulunamadi: DEADLINE_EXCEEDED",
        )

    def _gevsetilemez_durumu_kaydet(deger, asama):
        nonlocal teshis_bilgisi, otomatik_gevsetme_durduruldu
        status = _sonuc_status(deger)
        ist = getattr(deger, "istatistikler", None) or {}
        if status == "UNKNOWN":
            kok_neden = "solver_belirsiz_veya_timeout"
            aciklama = (
                "Solver kesin bir sonuc uretemedi. Sureyi veya model boyutunu "
                "iyilestirmeden kisitlar otomatik kaldirilmayacak."
            )
        elif status == "MODEL_INVALID":
            kok_neden = "model_gecersiz"
            aciklama = "CP-SAT modeli gecersiz; gevsetme yerine model hatasi duzeltilmeli."
        elif status == "MANUAL_CONFLICT":
            kok_neden = "manuel_atama_cakismasi"
            aciklama = "Manuel atama cakismasi var; otomatik gevsetme uygulanmadi."
        elif status == "DEADLINE_EXCEEDED":
            kok_neden = "global_sure_butcesi_doldu"
            aciklama = "Global sure butcesi doldu; yeni gevsetme denemesi baslatilmadi."
        else:
            kok_neden = "solver_hatasi_veya_belirsiz_durum"
            aciklama = f"Solver durumu {status}; kesin INFEASIBLE olmadigi icin gevsetme uygulanmadi."

        teshis_bilgisi = {
            "kok_neden": kok_neden,
            "kok_neden_aciklama": aciklama,
            "solver_status": status,
            "solver_status_name": ist.get("solver_status_name"),
            "timeout_olasi": bool(ist.get("timeout_olasi") or status in {"UNKNOWN", "DEADLINE_EXCEEDED"}),
            "otomatik_gevsetme_uygulandi": False,
            "gevsetmenin_durduruldugu_asama": asama,
        }
        tani_mesajlari.append(
            f"Otomatik gevsetme durduruldu: status={status}, asama={asama}. {aciklama}"
        )
        otomatik_gevsetme_durduruldu = True

    def _cozum_dene(
        deneme_adi, deneme_gorevler, deneme_kurallar, deneme_havuzlar,
        deneme_ara_gun, istenen_sure,
    ):
        nonlocal solver
        kalan = _kalan_sure()
        if kalan < min_deneme_suresi:
            _butce_notu(deneme_adi)
            return None, False

        try:
            istenen = float(istenen_sure)
        except (TypeError, ValueError):
            istenen = kalan
        ayrilan_sure = min(kalan, max(istenen, min_deneme_suresi))

        solver = NobetSolver(
            gun_sayisi=gun_sayisi,
            gun_tipleri=gun_tipleri,
            personeller=personeller,
            gorevler=deneme_gorevler,
            kurallar=deneme_kurallar,
            gorev_havuzlari=deneme_havuzlar,
            kisitlama_istisnalari=kisitlama_istisnalari,
            birlikte_istisnalari=birlikte_istisnalari,
            aragun_istisnalari=aragun_istisnalari,
            manuel_atamalar=manuel_atamalar,
            hedefler=hedefler,
            ara_gun=deneme_ara_gun,
            max_sure_saniye=ayrilan_sure,
            ignore_manual_conflicts=ignore_manual_conflicts,
            plan_kontrati=aktif_plan_kontrati,
        )
        aday = solver.coz()
        logger.info(
            "%s sonuc: basarili=%s status=%s sure=%dms ayrilan=%.3fs",
            deneme_adi,
            aday.basarili if aday else False,
            _sonuc_status(aday) if aday else "NO_RESULT",
            aday.sure_ms if aday else 0,
            ayrilan_sure,
        )
        if _kalan_sure() <= 0:
            _butce_notu(f"{deneme_adi}_sonrasi")
        return aday, True

    def _plani_yenile(yeni_ara_gun, asama):
        nonlocal hedefler, aktif_plan_kontrati
        if not plan_yenileyici:
            return True
        if not _butce_var():
            _butce_notu(f"plan_yenileme:{asama}")
            return False

        try:
            yeni_plan = plan_yenileyici(yeni_ara_gun)
        except Exception as exc:
            logger.exception("Plan yenileme basarisiz (ara_gun=%s): %s", yeni_ara_gun, exc)
            tani_mesajlari.append(
                f"Plan kontrati yenilenemedi (ara_gun={yeni_ara_gun}): {str(exc)[:120]}"
            )
            return False

        if not yeni_plan or not yeni_plan.get("basarili"):
            tani_mesajlari.append(
                f"Plan kontrati yenilenemedi (ara_gun={yeni_ara_gun}, asama={asama})."
            )
            return False

        yeni_hedefler = yeni_plan.get("hedefler_map")
        if yeni_hedefler:
            hedefler = yeni_hedefler
        pk = yeni_plan.get("plan_kontrati")
        if pk is not None:
            pk = pk.to_dict() if hasattr(pk, "to_dict") else pk
            aktif_plan_kontrati = plan_kontrati_hash_yenile(pk)
        tani_mesajlari.append(
            f"Plan kontrati yenilendi (ara_gun={yeni_ara_gun}, plan_hash="
            f"{(aktif_plan_kontrati or {}).get('plan_hash', 'yok')})"
        )

        if not _butce_var():
            _butce_notu(f"plan_yenileme:{asama}:sonrasi")
            return False
        return True

    def _ara_gun_denemeleri(
        deneme_adi, deneme_gorevler, deneme_kurallar, deneme_havuzlar,
        ara_gunler, deneme_suresi, basari_callback,
    ):
        nonlocal sonuc, aktif_ara_gun, aktif_plan_kontrati, hedefler
        nonlocal otomatik_gevsetme_durduruldu
        deneme_sayisi = 0

        for dene_ara_gun in ara_gunler:
            if sonuc and sonuc.basarili:
                break
            if not _infeasible(sonuc):
                if sonuc and not sonuc.basarili:
                    _gevsetilemez_durumu_kaydet(sonuc, deneme_adi)
                break
            if not _butce_var():
                _butce_notu(deneme_adi)
                otomatik_gevsetme_durduruldu = True
                break

            onceki_plan = deepcopy(aktif_plan_kontrati)
            onceki_hedefler = deepcopy(hedefler)
            onceki_ara_gun = aktif_ara_gun
            if not _plani_yenile(dene_ara_gun, deneme_adi):
                aktif_plan_kontrati = onceki_plan
                hedefler = onceki_hedefler
                aktif_ara_gun = onceki_ara_gun
                otomatik_gevsetme_durduruldu = True
                break

            aday, denendi = _cozum_dene(
                deneme_adi,
                deneme_gorevler,
                deneme_kurallar,
                deneme_havuzlar,
                dene_ara_gun,
                deneme_suresi,
            )
            if not denendi:
                aktif_plan_kontrati = onceki_plan
                hedefler = onceki_hedefler
                aktif_ara_gun = onceki_ara_gun
                otomatik_gevsetme_durduruldu = True
                break

            deneme_sayisi += 1
            sonuc = aday
            aktif_ara_gun = dene_ara_gun
            if sonuc and sonuc.basarili:
                basari_callback(dene_ara_gun)
                return True, deneme_sayisi
            if not _infeasible(sonuc):
                _gevsetilemez_durumu_kaydet(sonuc, deneme_adi)
                break

        return False, deneme_sayisi

    # Faz 1: Orijinal parametrelerle cozum.
    sure_ilk = min(toplam_butce_s * 0.50, _kalan_sure())
    sonuc, ilk_denendi = _cozum_dene(
        "Faz 1", gorevler, kurallar, gorev_havuzlari, ara_gun, sure_ilk
    )
    if not ilk_denendi:
        sonuc = _deadline_sonucu("ilk cozum")

    # Faz 2: Yalniz kesin INFEASIBLE sonucunda teshis ve gevsetme.
    if sonuc and not sonuc.basarili and not _infeasible(sonuc):
        _gevsetilemez_durumu_kaydet(sonuc, "Faz 1")

    if _infeasible(sonuc):
        tani_mesajlari.append("Ilk deneme INFEASIBLE; teshis baslatiliyor.")

        # Once yalniz planin hard/tolerans uygulamasini yumusat.
        if isinstance(aktif_plan_kontrati, dict) and aktif_plan_kontrati and _butce_var():
            onceki_plan = deepcopy(aktif_plan_kontrati)
            gevsek_plan = deepcopy(aktif_plan_kontrati)
            uygulama = dict(gevsek_plan.get("uygulama", {}) or {})
            uygulama["toplam_hard"] = False
            uygulama["gun_tipi_toleransi"] = _en_az_tolerans(
                uygulama, "gun_tipi_toleransi", 2
            )
            uygulama["gorev_kota_toleransi"] = _en_az_tolerans(
                uygulama, "gorev_kota_toleransi", 2
            )
            uygulama["gun_iskeleti_toleransi"] = _en_az_tolerans(
                uygulama, "gun_iskeleti_toleransi", 2
            )
            gevsek_plan["uygulama"] = uygulama
            aktif_plan_kontrati = plan_kontrati_hash_yenile(gevsek_plan)
            gevsetme_bilgisi["plan_gevsetme_denendi"] = True

            rahat_sure = min(toplam_butce_s * 0.20, _kalan_sure())
            rahat_sonuc, rahat_denendi = _cozum_dene(
                "Plan gevsetme",
                aktif_gorevler,
                aktif_kurallar,
                aktif_havuzlar,
                aktif_ara_gun,
                rahat_sure,
            )
            if not rahat_denendi:
                aktif_plan_kontrati = onceki_plan
            else:
                sonuc = rahat_sonuc
                if sonuc and sonuc.basarili:
                    gevsetme_bilgisi["plan_gevsetildi"] = True
                    gevsetme_bilgisi["plan_hash"] = aktif_plan_kontrati.get("plan_hash")
                    tani_mesajlari.append(
                        "Plan gevsetilerek cozum bulundu (toplam_hard=False, tolerans>=2)."
                    )
                elif not _infeasible(sonuc):
                    _gevsetilemez_durumu_kaydet(sonuc, "Plan gevsetme")

        if _infeasible(sonuc) and not otomatik_gevsetme_durduruldu:
            try:
                diagnostics = solver._build_feasibility_diagnostics()
                aksiyonlar = solver._diagnose_infeasible(diagnostics)
            except Exception as exc:
                logger.exception("Feasibility teshisi basarisiz: %s", exc)
                diagnostics = {}
                aksiyonlar = []
                tani_mesajlari.append(f"Feasibility teshisi basarisiz: {str(exc)[:120]}")

            unsat_core_bilgisi = {}
            if _butce_var() and _kalan_sure() >= 1.0:
                core_sure = min(max(toplam_butce_s * 0.10, 1.0), 15.0, _kalan_sure())
                unsat_core_bilgisi = solver.diagnose_with_unsat_core(
                    max_sure_saniye=core_sure
                )
                if not _butce_var():
                    _butce_notu("unsat_core_sonrasi")

            teshis_bilgisi = {
                "kok_neden": aksiyonlar[0]["aksiyon"] if aksiyonlar else "bilinmiyor",
                "kok_neden_aciklama": aksiyonlar[0]["neden"] if aksiyonlar else "",
                "solver_status": "INFEASIBLE",
                "teshis_sira": [
                    {"aksiyon": a["aksiyon"], "puan": a["puan"], "neden": a["neden"]}
                    for a in aksiyonlar
                ],
                "zero_candidate_count": diagnostics.get("slot_day_zero_candidate_count", 0),
                "kapasite_sorunlari": len(diagnostics.get("role_ara_gun_capacity_issues", [])),
                "unsat_core": unsat_core_bilgisi,
            }
            tani_mesajlari.append(
                f"Teshis: Kok neden={teshis_bilgisi['kok_neden']}, "
                f"aciklama={teshis_bilgisi['kok_neden_aciklama']}"
            )
            if unsat_core_bilgisi.get("core_groups"):
                core_ozet = ", ".join(
                    item.get("group", "?")
                    for item in unsat_core_bilgisi.get("core_groups", [])[:5]
                )
                tani_mesajlari.append(f"Unsat-core gozlem: {core_ozet}")

            sure_per_aksiyon = (
                _kalan_sure() / max(len(aksiyonlar), 1)
                if _butce_var() else 0.0
            )
            gorevler_noexcl = [
                SolverGorev(
                    id=g.id,
                    ad=g.ad,
                    slot_idx=g.slot_idx,
                    base_name=g.base_name,
                    exclusive=False,
                    ayri_bina=g.ayri_bina,
                )
                for g in gorevler
            ]

            for aksiyon_info in aksiyonlar:
                if sonuc and sonuc.basarili:
                    break
                if otomatik_gevsetme_durduruldu or not _infeasible(sonuc):
                    break
                if not _butce_var():
                    _butce_notu("gevsetme_dongusu")
                    break

                aksiyon = aksiyon_info["aksiyon"]
                tani_mesajlari.append(
                    f"Gevsetme denemesi: {aksiyon} (puan: {aksiyon_info['puan']})"
                )

                if aksiyon == "ara_gun_azalt":
                    def _ara_basari(dene_ara_gun):
                        nonlocal kullanilan_ara_gun
                        kullanilan_ara_gun = dene_ara_gun
                        gevsetme_bilgisi["ara_gun_gevsetildi"] = True
                        tani_mesajlari.append(
                            f"Ara gun {ara_gun}->{dene_ara_gun} gevsetilerek cozum bulundu."
                        )

                    _ara_gun_denemeleri(
                        "ara_gun_azalt",
                        aktif_gorevler,
                        aktif_kurallar,
                        aktif_havuzlar,
                        range(aktif_ara_gun - 1, -1, -1),
                        sure_per_aksiyon,
                        _ara_basari,
                    )

                elif aksiyon == "exclusive_gevset":
                    onceki_gorevler = aktif_gorevler
                    onceki_havuzlar = aktif_havuzlar
                    aktif_gorevler = gorevler_noexcl
                    aktif_havuzlar = {}

                    def _exclusive_basari(dene_ara_gun):
                        nonlocal kullanilan_ara_gun
                        kullanilan_ara_gun = dene_ara_gun
                        gevsetme_bilgisi["exclusive_gevsetildi"] = True
                        tani_mesajlari.append("Exclusive kisitlar gevsetilerek cozum bulundu.")

                    _, deneme_sayisi = _ara_gun_denemeleri(
                        "exclusive_gevset",
                        aktif_gorevler,
                        aktif_kurallar,
                        aktif_havuzlar,
                        range(aktif_ara_gun, -1, -1),
                        sure_per_aksiyon,
                        _exclusive_basari,
                    )
                    if deneme_sayisi == 0:
                        aktif_gorevler = onceki_gorevler
                        aktif_havuzlar = onceki_havuzlar

                elif aksiyon == "ayri_gevset":
                    onceki_kurallar = aktif_kurallar
                    aktif_kurallar = [k for k in aktif_kurallar if k.tur != "ayri"]

                    def _ayri_basari(dene_ara_gun):
                        nonlocal kullanilan_ara_gun
                        kullanilan_ara_gun = dene_ara_gun
                        gevsetme_bilgisi["ayri_gevsetildi"] = True
                        tani_mesajlari.append(
                            "Ayri tutma kurallari kaldirildiktan sonra cozum bulundu."
                        )

                    _, deneme_sayisi = _ara_gun_denemeleri(
                        "ayri_gevset",
                        aktif_gorevler,
                        aktif_kurallar,
                        aktif_havuzlar,
                        range(aktif_ara_gun, -1, -1),
                        sure_per_aksiyon,
                        _ayri_basari,
                    )
                    if deneme_sayisi == 0:
                        aktif_kurallar = onceki_kurallar

                elif aksiyon == "birlikte_kaldir":
                    base_kurallar = [k for k in aktif_kurallar if k.tur != "birlikte"]
                    birlikte_sirali = _sirala_birlikte_kurallari(
                        aktif_kurallar, personeller, hedefler
                    )
                    for kaldirilan_sayi in range(1, len(birlikte_sirali) + 1):
                        if otomatik_gevsetme_durduruldu or not _infeasible(sonuc):
                            break
                        onceki_kurallar = aktif_kurallar
                        aktif_kurallar = base_kurallar + [
                            item["kural"] for item in birlikte_sirali[kaldirilan_sayi:]
                        ]

                        def _birlikte_basari(dene_ara_gun, sayi=kaldirilan_sayi):
                            nonlocal kullanilan_ara_gun
                            kullanilan_ara_gun = dene_ara_gun
                            gevsetme_bilgisi["birlikte_kaldirildi"] = True
                            gevsetme_bilgisi["kaldirilan_birlikte_kural_sayisi"] = sayi
                            tani_mesajlari.append(
                                f"{sayi} birlikte kurali kademeli kaldirilarak cozum bulundu."
                            )

                        _, deneme_sayisi = _ara_gun_denemeleri(
                            "birlikte_kaldir",
                            aktif_gorevler,
                            aktif_kurallar,
                            aktif_havuzlar,
                            range(aktif_ara_gun, -1, -1),
                            sure_per_aksiyon,
                            _birlikte_basari,
                        )
                        if deneme_sayisi == 0:
                            aktif_kurallar = onceki_kurallar
                            break
                        if sonuc and sonuc.basarili:
                            break

                elif aksiyon == "tum_soft_kaldir":
                    onceki_gorevler = aktif_gorevler
                    onceki_kurallar = aktif_kurallar
                    onceki_havuzlar = aktif_havuzlar
                    aktif_gorevler = gorevler_noexcl
                    aktif_kurallar = []
                    aktif_havuzlar = {}

                    def _tum_soft_basari(dene_ara_gun):
                        nonlocal kullanilan_ara_gun
                        kullanilan_ara_gun = dene_ara_gun
                        gevsetme_bilgisi["tum_soft_kaldirildi"] = True
                        tani_mesajlari.append(
                            "Tum soft kisitlar kaldirildiktan sonra cozum bulundu."
                        )

                    _, deneme_sayisi = _ara_gun_denemeleri(
                        "tum_soft_kaldir",
                        aktif_gorevler,
                        aktif_kurallar,
                        aktif_havuzlar,
                        range(aktif_ara_gun, -1, -1),
                        sure_per_aksiyon,
                        _tum_soft_basari,
                    )
                    if deneme_sayisi == 0:
                        aktif_gorevler = onceki_gorevler
                        aktif_kurallar = onceki_kurallar
                        aktif_havuzlar = onceki_havuzlar

    if sonuc is None:
        sonuc = SolverSonuc(
            basarili=False,
            atamalar=[],
            istatistikler={"status": "NO_SOLUTION", "ara_gun": ara_gun},
            sure_ms=0,
            mesaj="Cozum uretilemedi - parametre hatasi olabilir",
        )

    # Faz 3: Basarili fakat eksik sonucu, kalan butce icinde daha dolu yap.
    DOLULUK_TOLERANS = 0

    def _bos_slot_say(deger):
        if not deger or not getattr(deger, "istatistikler", None):
            return 0
        return int((deger.istatistikler or {}).get("bos_slot_sayisi", 0) or 0)

    bos_ilk = _bos_slot_say(sonuc)
    doluluk_gevsetme_denendi = False
    if (
        sonuc
        and sonuc.basarili
        and bos_ilk > DOLULUK_TOLERANS
        and kullanilan_ara_gun > 0
        and _butce_var()
    ):
        en_iyi_sonuc = sonuc
        en_iyi_bos = bos_ilk
        en_iyi_ara_gun = kullanilan_ara_gun
        en_iyi_plan = deepcopy(aktif_plan_kontrati)
        en_iyi_hedefler = deepcopy(hedefler)
        denenecek_ara = list(range(kullanilan_ara_gun - 1, -1, -1))
        sure_adim = _kalan_sure() / max(len(denenecek_ara), 1)

        for dene_ara_gun in denenecek_ara:
            if en_iyi_bos <= DOLULUK_TOLERANS or not _butce_var():
                break
            if not _plani_yenile(dene_ara_gun, "doluluk"):
                break
            aday, denendi = _cozum_dene(
                "doluluk",
                aktif_gorevler,
                aktif_kurallar,
                aktif_havuzlar,
                dene_ara_gun,
                sure_adim,
            )
            if not denendi:
                break
            doluluk_gevsetme_denendi = True
            if aday and aday.basarili:
                aday_bos = _bos_slot_say(aday)
                if aday_bos < en_iyi_bos:
                    en_iyi_sonuc = aday
                    en_iyi_bos = aday_bos
                    en_iyi_ara_gun = dene_ara_gun
                    en_iyi_plan = deepcopy(aktif_plan_kontrati)
                    en_iyi_hedefler = deepcopy(hedefler)
                    tani_mesajlari.append(
                        f"Doluluk gevsetme: ara_gun {kullanilan_ara_gun}->{dene_ara_gun}, "
                        f"bos slot {bos_ilk}->{aday_bos}."
                    )
            elif aday and not _infeasible(aday):
                tani_mesajlari.append(
                    f"Doluluk denemesi status={_sonuc_status(aday)} ile durdu; "
                    "mevcut basarili sonuc korundu."
                )
                break

        sonuc = en_iyi_sonuc
        aktif_plan_kontrati = en_iyi_plan
        hedefler = en_iyi_hedefler
        aktif_ara_gun = en_iyi_ara_gun
        if en_iyi_ara_gun != kullanilan_ara_gun:
            kullanilan_ara_gun = en_iyi_ara_gun
            gevsetme_bilgisi["doluluk_ara_gun_gevsetildi"] = True

    doluluk_raporu = _doluluk_raporu_uret(
        sonuc, _bos_slot_say(sonuc), doluluk_gevsetme_denendi
    )
    if doluluk_raporu.get("bos_slot", 0) > 0:
        tani_mesajlari.append(f"Doluluk: {doluluk_raporu.get('oneri', '')}")

    if aktif_plan_kontrati:
        aktif_plan_kontrati = plan_kontrati_hash_yenile(aktif_plan_kontrati)

    toplam_sure_ms = int((_time.monotonic() - baslangic_toplam) * 1000)
    sonuc_istatistikleri = getattr(sonuc, "istatistikler", None) or {}
    mevcut_plan_istatistikleri = (
        sonuc_istatistikleri.get("plan", {})
        if isinstance(sonuc_istatistikleri, dict) else {}
    ) or {}
    logger.info(
        "nobet_coz tamamlandi: basarili=%s status=%s sure=%dms atama=%d gevsetme=%s",
        sonuc.basarili,
        _sonuc_status(sonuc),
        toplam_sure_ms,
        len(sonuc.atamalar),
        bool(gevsetme_bilgisi),
    )

    sonuc = SolverSonuc(
        basarili=sonuc.basarili,
        atamalar=sonuc.atamalar,
        istatistikler={
            **sonuc_istatistikleri,
            "plan": {
                **mevcut_plan_istatistikleri,
                **({
                    "plan_hash": aktif_plan_kontrati.get("plan_hash"),
                    "kaynak": aktif_plan_kontrati.get("kaynak"),
                    "olusturulan_ara_gun": aktif_plan_kontrati.get("olusturulan_ara_gun"),
                    "kontrat": aktif_plan_kontrati,
                } if aktif_plan_kontrati else {}),
            },
            "tani_mesajlari": tani_mesajlari,
            "gevsetme_bilgisi": gevsetme_bilgisi,
            "teshis": teshis_bilgisi,
            "doluluk_raporu": doluluk_raporu,
            "zaman_butcesi": {
                "max_sure_saniye": toplam_butce_s,
                "toplam_sure_ms": toplam_sure_ms,
                "deadline_doldu": _time.monotonic() >= deadline,
                "yeni_islem_engellendi": deadline_notu_eklendi,
            },
            **({
                "fallback_ara_gun": kullanilan_ara_gun,
                "istenen_ara_gun": ara_gun,
            } if kullanilan_ara_gun != ara_gun else {}),
        },
        sure_ms=toplam_sure_ms,
        mesaj=(
            f"{sonuc.mesaj} (ara_gun {ara_gun}->{kullanilan_ara_gun} gevsetildi)"
            if kullanilan_ara_gun != ara_gun and sonuc.basarili
            else sonuc.mesaj
        ),
    )

    return sonuc, gevsetme_bilgisi, teshis_bilgisi, kullanilan_ara_gun
