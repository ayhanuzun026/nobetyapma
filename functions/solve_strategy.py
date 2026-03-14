"""
Cozum Stratejisi � Akilli teshis tabanli retry + relaxation dongusu.
Faz 1: Orijinal parametrelerle cozum
Faz 2: INFEASIBLE ise akilli teshis ve otomatik gevsetme
"""

import time as _time
import logging

from solver_models import SolverGorev, SolverSonuc
from ortools_solver import NobetSolver
from utils import find_matching_id

logger = logging.getLogger(__name__)


def _sirala_birlikte_kurallari(kurallar, personeller, hedefler):
    personel_map = {p.id: p for p in personeller}
    birlikte_kurallari = []

    for kural in kurallar:
        if getattr(kural, 'tur', None) != 'birlikte':
            continue

        valid_ids = []
        for raw_pid in getattr(kural, 'kisiler', []) or []:
            matched_id = find_matching_id(raw_pid, personel_map.keys())
            if matched_id is not None and matched_id not in valid_ids:
                valid_ids.append(matched_id)

        if len(valid_ids) < 2:
            continue

        ortak_gunler = None
        for pid in valid_ids:
            musait_gunler = set(getattr(personel_map[pid], 'musait_gunler', set()) or set())
            ortak_gunler = musait_gunler if ortak_gunler is None else (ortak_gunler & musait_gunler)

        min_hedef = min(
            int((hedefler or {}).get(pid, {}).get('hedef_toplam', 0) or 0)
            for pid in valid_ids
        )

        birlikte_kurallari.append({
            'kural': kural,
            'valid_ids': valid_ids,
            'ortak_gun_sayisi': len(ortak_gunler or set()),
            'min_hedef': min_hedef,
            'grup_boyutu': len(valid_ids),
        })

    birlikte_kurallari.sort(
        key=lambda item: (
            item['ortak_gun_sayisi'],
            -item['grup_boyutu'],
            item['min_hedef'],
        )
    )
    return birlikte_kurallari


def solve_with_diagnostics(
    gun_sayisi, gun_tipleri, personeller, gorevler, kurallar,
    gorev_havuzlari, kisitlama_istisnalari, birlikte_istisnalari,
    aragun_istisnalari, manuel_atamalar, hedefler,
    ara_gun, max_sure, yil, ay, resmi_tatiller, data,
    ignore_manual_conflicts=False, plan_kontrati=None, plan_yenileyici=None
):
    """Akıllı teşhis tabanlı çözüm stratejisi.

    Returns: (sonuc, gevsetme_bilgisi, teshis_bilgisi, kullanilan_ara_gun)
    """
    baslangic_toplam = _time.time()
    tani_mesajlari = []
    gevsetme_bilgisi = {}
    teshis_bilgisi = {}

    # Zaman bütçelemesi: max_sure'yi fazlara böl
    sure_ilk = int(max_sure * 0.50)   # İlk deneme: %50
    # sure_gevsetme = int(max_sure * 0.40)  # Gevşetme denemeleri: %40
    # Greedy: <1s

    sonuc = None
    kullanilan_ara_gun = ara_gun
    aktif_plan_kontrati = plan_kontrati

    def _plani_yenile(yeni_ara_gun):
        nonlocal hedefler, aktif_plan_kontrati
        if not plan_yenileyici:
            return
        try:
            yeni_plan = plan_yenileyici(yeni_ara_gun)
        except Exception as exc:
            logger.exception("Plan yenileme basarisiz (ara_gun=%s): %s", yeni_ara_gun, exc)
            tani_mesajlari.append(
                f"Plan kontrati yenilenemedi (ara_gun={yeni_ara_gun}): {str(exc)[:120]}"
            )
            return
        if not yeni_plan or not yeni_plan.get('basarili'):
            return
        yeni_hedefler = yeni_plan.get('hedefler_map')
        if yeni_hedefler:
            hedefler = yeni_hedefler
        pk = yeni_plan.get('plan_kontrati')
        if pk is not None:
            aktif_plan_kontrati = pk.to_dict() if hasattr(pk, 'to_dict') else pk
        tani_mesajlari.append(
            f"Plan kontrati yenilendi (ara_gun={yeni_ara_gun}, plan_hash="
            f"{(aktif_plan_kontrati or {}).get('plan_hash', 'yok')})"
        )

    # ---- FAZ 1: Orijinal parametrelerle çöz ----
    logger.info("Faz 1: Orijinal parametrelerle cozum baslatiliyor (sure=%ds)", sure_ilk)
    solver = NobetSolver(
        gun_sayisi=gun_sayisi, gun_tipleri=gun_tipleri,
        personeller=personeller, gorevler=gorevler,
        kurallar=kurallar, gorev_havuzlari=gorev_havuzlari,
        kisitlama_istisnalari=kisitlama_istisnalari,
        birlikte_istisnalari=birlikte_istisnalari,
        aragun_istisnalari=aragun_istisnalari,
        manuel_atamalar=manuel_atamalar, hedefler=hedefler,
        ara_gun=ara_gun, max_sure_saniye=sure_ilk,
        ignore_manual_conflicts=ignore_manual_conflicts,
        plan_kontrati=aktif_plan_kontrati,
    )
    sonuc = solver.coz()
    logger.info("Faz 1 sonuc: basarili=%s, sure=%dms",
                sonuc.basarili if sonuc else False,
                sonuc.sure_ms if sonuc else 0)

    # ---- FAZ 2: INFEASIBLE ise akıllı teşhis ve otomatik gevşetme ----
    if sonuc and not sonuc.basarili:
        tani_mesajlari.append("Ilk deneme basarisiz, teshis baslatiliyor...")
        logger.info("Faz 1 basarisiz, teshis baslatiliyor...")

        # --- PLAN GEVSETME (erken deneme) ---
        # Plan kontratindaki sert esitlemeler cozumu kilitliyorsa, once yumsatarak tekrar dene.
        try:
            if isinstance(aktif_plan_kontrati, dict) and aktif_plan_kontrati:
                _uyg = dict(aktif_plan_kontrati.get("uygulama", {}) or {})
                _uyg["toplam_hard"] = False
                _uyg["gun_tipi_toleransi"] = max(int(_uyg.get("gun_tipi_toleransi", 0)), 2)
                _uyg["gorev_kota_toleransi"] = max(int(_uyg.get("gorev_kota_toleransi", 0)), 2)
                _uyg["gun_iskeleti_toleransi"] = max(int(_uyg.get("gun_iskeleti_toleransi", 0)), 2)
                aktif_plan_kontrati = { **aktif_plan_kontrati, "uygulama": _uyg }
                solver = NobetSolver(
                    gun_sayisi=gun_sayisi, gun_tipleri=gun_tipleri,
                    personeller=personeller, gorevler=gorevler,
                    kurallar=kurallar, gorev_havuzlari=gorev_havuzlari,
                    kisitlama_istisnalari=kisitlama_istisnalari,
                    birlikte_istisnalari=birlikte_istisnalari,
                    aragun_istisnalari=aragun_istisnalari,
                    manuel_atamalar=manuel_atamalar, hedefler=hedefler,
                    ara_gun=ara_gun, max_sure_saniye=max(5, int(max_sure*0.2)),
                    ignore_manual_conflicts=ignore_manual_conflicts,
                    plan_kontrati=aktif_plan_kontrati,
                )
                _relaxed = solver.coz()
                if _relaxed and _relaxed.basarili:
                    tani_mesajlari.append("Plan gevsetilerek cozum bulundu (toplam_hard=False, tolerans=2)")
                    sonuc = _relaxed
                    kullanilan_ara_gun = ara_gun
                    # Bu noktada basari bulunduysa tanilari kaydederek cikilir
                    return SolverSonuc(
                        basarili=sonuc.basarili, atamalar=sonuc.atamalar,
                        istatistikler={ **sonuc.istatistikler, 'tani_mesajlari': tani_mesajlari },
                        sure_ms=sonuc.sure_ms, mesaj=sonuc.mesaj
                    ), {}, teshis_bilgisi, kullanilan_ara_gun
        except Exception as _exc:
            logger.warning("Plan gevsetme denemesi atlandi: %s", _exc)
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
            logger.info("Gevsetme denemesi: %s (puan: %s)", aksiyon, aksiyon_info['puan'])
            tani_mesajlari.append(
                f"Gevsetme denemesi: {aksiyon} (puan: {aksiyon_info['puan']})"
            )

            if aksiyon == 'ara_gun_azalt':
                # Ara günü kademeli azalt
                for dene_ara_gun in range(aktif_ara_gun, 0, -1):
                    if dene_ara_gun == aktif_ara_gun and aktif_ara_gun == ara_gun:
                        continue  # İlk denemede zaten denendi
                    _plani_yenile(dene_ara_gun)
                    solver = NobetSolver(
                        gun_sayisi=gun_sayisi, gun_tipleri=gun_tipleri,
                        personeller=personeller, gorevler=aktif_gorevler,
                        kurallar=aktif_kurallar, gorev_havuzlari=aktif_havuzlar,
                        kisitlama_istisnalari=kisitlama_istisnalari,
                        birlikte_istisnalari=birlikte_istisnalari,
                        aragun_istisnalari=aragun_istisnalari,
                        manuel_atamalar=manuel_atamalar, hedefler=hedefler,
                        ara_gun=dene_ara_gun, max_sure_saniye=sure_per_aksiyon,
                        ignore_manual_conflicts=ignore_manual_conflicts,
                        plan_kontrati=aktif_plan_kontrati,
                    )
                    sonuc = solver.coz()
                    if sonuc.basarili:
                        kullanilan_ara_gun = dene_ara_gun
                        gevsetme_bilgisi['ara_gun_gevsetildi'] = True
                        tani_mesajlari.append(
                            f"Ara gun {ara_gun}->{dene_ara_gun} gevsetilerek cozum bulundu"
                        )
                        break
                aktif_ara_gun = 1  # Sonraki aksiyonlarda ara gün=1 ile dene

            elif aksiyon == 'exclusive_gevset':
                aktif_gorevler = gorevler_noexcl
                aktif_havuzlar = {}  # H10 havuz kısıtını da gevşet
                for dene_ara_gun in range(aktif_ara_gun, 0, -1):
                    _plani_yenile(dene_ara_gun)
                    solver = NobetSolver(
                        gun_sayisi=gun_sayisi, gun_tipleri=gun_tipleri,
                        personeller=personeller, gorevler=aktif_gorevler,
                        kurallar=aktif_kurallar, gorev_havuzlari=aktif_havuzlar,
                        kisitlama_istisnalari=kisitlama_istisnalari,
                        birlikte_istisnalari=birlikte_istisnalari,
                        aragun_istisnalari=aragun_istisnalari,
                        manuel_atamalar=manuel_atamalar, hedefler=hedefler,
                        ara_gun=dene_ara_gun, max_sure_saniye=sure_per_aksiyon,
                        ignore_manual_conflicts=ignore_manual_conflicts,
                        plan_kontrati=aktif_plan_kontrati,
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
                    _plani_yenile(dene_ara_gun)
                    solver = NobetSolver(
                        gun_sayisi=gun_sayisi, gun_tipleri=gun_tipleri,
                        personeller=personeller, gorevler=aktif_gorevler,
                        kurallar=aktif_kurallar, gorev_havuzlari=aktif_havuzlar,
                        kisitlama_istisnalari=kisitlama_istisnalari,
                        birlikte_istisnalari=birlikte_istisnalari,
                        aragun_istisnalari=aragun_istisnalari,
                        manuel_atamalar=manuel_atamalar, hedefler=hedefler,
                        ara_gun=dene_ara_gun, max_sure_saniye=sure_per_aksiyon,
                        ignore_manual_conflicts=ignore_manual_conflicts,
                        plan_kontrati=aktif_plan_kontrati,
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
                base_kurallar = [k for k in aktif_kurallar if k.tur != 'birlikte']
                birlikte_sirali = _sirala_birlikte_kurallari(aktif_kurallar, personeller, hedefler)

                for kaldirilan_sayi in range(1, len(birlikte_sirali) + 1):
                    kalan_birlikte = [
                        item['kural'] for item in birlikte_sirali[kaldirilan_sayi:]
                    ]
                    aktif_kurallar = base_kurallar + kalan_birlikte

                    for dene_ara_gun in range(aktif_ara_gun, 0, -1):
                        _plani_yenile(dene_ara_gun)
                        solver = NobetSolver(
                            gun_sayisi=gun_sayisi, gun_tipleri=gun_tipleri,
                            personeller=personeller, gorevler=aktif_gorevler,
                            kurallar=aktif_kurallar, gorev_havuzlari=aktif_havuzlar,
                            kisitlama_istisnalari=kisitlama_istisnalari,
                            birlikte_istisnalari=birlikte_istisnalari,
                            aragun_istisnalari=aragun_istisnalari,
                            manuel_atamalar=manuel_atamalar, hedefler=hedefler,
                            ara_gun=dene_ara_gun, max_sure_saniye=sure_per_aksiyon,
                            ignore_manual_conflicts=ignore_manual_conflicts,
                            plan_kontrati=aktif_plan_kontrati,
                        )
                        sonuc = solver.coz()
                        if sonuc.basarili:
                            kullanilan_ara_gun = dene_ara_gun
                            gevsetme_bilgisi['birlikte_kaldirildi'] = True
                            gevsetme_bilgisi['kaldirilan_birlikte_kural_sayisi'] = kaldirilan_sayi
                            tani_mesajlari.append(
                                f"{kaldirilan_sayi} birlikte kurali kademeli kaldirilarak cozum bulundu"
                            )
                            break

                    if sonuc.basarili:
                        break

            elif aksiyon == 'tum_soft_kaldir':
                aktif_kurallar = []
                aktif_havuzlar = {}
                for dene_ara_gun in range(max(1, aktif_ara_gun), 0, -1):
                    _plani_yenile(dene_ara_gun)
                    solver = NobetSolver(
                        gun_sayisi=gun_sayisi, gun_tipleri=gun_tipleri,
                        personeller=personeller, gorevler=gorevler_noexcl,
                        kurallar=[], gorev_havuzlari={},
                        kisitlama_istisnalari=kisitlama_istisnalari,
                        birlikte_istisnalari=birlikte_istisnalari,
                        aragun_istisnalari=aragun_istisnalari,
                        manuel_atamalar=manuel_atamalar, hedefler=hedefler,
                        ara_gun=dene_ara_gun, max_sure_saniye=sure_per_aksiyon,
                        ignore_manual_conflicts=ignore_manual_conflicts,
                        plan_kontrati=aktif_plan_kontrati,
                    )
                    sonuc = solver.coz()
                    if sonuc.basarili:
                        kullanilan_ara_gun = dene_ara_gun
                        gevsetme_bilgisi['tum_soft_kaldirildi'] = True
                        tani_mesajlari.append(
                            "Tum soft kisitlar kaldirildiktan sonra cozum bulundu"
                        )
                        break

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
    logger.info("nobet_coz tamamlandi: basarili=%s, sure=%dms, atama=%d, gevsetme=%s",
                sonuc.basarili, toplam_sure_ms, len(sonuc.atamalar),
                bool(gevsetme_bilgisi))
    sonuc = SolverSonuc(
        basarili=sonuc.basarili, atamalar=sonuc.atamalar,
        istatistikler={
            **sonuc.istatistikler,
            'plan': {
                **((sonuc.istatistikler or {}).get('plan', {}) if isinstance(sonuc.istatistikler, dict) else {}),
                **({
                    'plan_hash': (aktif_plan_kontrati or {}).get('plan_hash'),
                    'kaynak': (aktif_plan_kontrati or {}).get('kaynak'),
                    'olusturulan_ara_gun': (aktif_plan_kontrati or {}).get('olusturulan_ara_gun'),
                    'kontrat': aktif_plan_kontrati,
                } if aktif_plan_kontrati else {}),
            },
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

    return sonuc, gevsetme_bilgisi, teshis_bilgisi, kullanilan_ara_gun

