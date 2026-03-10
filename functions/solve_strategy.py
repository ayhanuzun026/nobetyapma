"""
Çözüm Stratejisi — Akıllı teşhis tabanlı retry + relaxation döngüsü.
Faz 1: Orijinal parametrelerle çöz
Faz 2: INFEASIBLE ise akıllı teşhis ve otomatik gevşetme
"""

import time as _time
import logging

from solver_models import SolverGorev, SolverSonuc
from ortools_solver import NobetSolver
from greedy_fallback import greedy_fallback

logger = logging.getLogger(__name__)


def solve_with_diagnostics(
    gun_sayisi, gun_tipleri, personeller, gorevler, kurallar,
    gorev_havuzlari, kisitlama_istisnalari, birlikte_istisnalari,
    aragun_istisnalari, manuel_atamalar, hedefler,
    ara_gun, max_sure, yil, ay, resmi_tatiller, data
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
        ara_gun=ara_gun, max_sure_saniye=sure_ilk
    )
    sonuc = solver.coz()
    logger.info("Faz 1 sonuc: basarili=%s, sure=%dms",
                sonuc.basarili if sonuc else False,
                sonuc.sure_ms if sonuc else 0)

    # ---- FAZ 2: INFEASIBLE ise akıllı teşhis ve otomatik gevşetme ----
    if sonuc and not sonuc.basarili:
        tani_mesajlari.append("Ilk deneme basarisiz, teshis baslatiliyor...")
        logger.info("Faz 1 basarisiz, teshis baslatiliyor...")

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
                    solver = NobetSolver(
                        gun_sayisi=gun_sayisi, gun_tipleri=gun_tipleri,
                        personeller=personeller, gorevler=aktif_gorevler,
                        kurallar=aktif_kurallar, gorev_havuzlari=aktif_havuzlar,
                        kisitlama_istisnalari=kisitlama_istisnalari,
                        birlikte_istisnalari=birlikte_istisnalari,
                        aragun_istisnalari=aragun_istisnalari,
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
                        birlikte_istisnalari=birlikte_istisnalari,
                        aragun_istisnalari=aragun_istisnalari,
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
                        birlikte_istisnalari=birlikte_istisnalari,
                        aragun_istisnalari=aragun_istisnalari,
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
                        birlikte_istisnalari=birlikte_istisnalari,
                        aragun_istisnalari=aragun_istisnalari,
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
                        birlikte_istisnalari=birlikte_istisnalari,
                        aragun_istisnalari=aragun_istisnalari,
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
                greedy_sonuc = greedy_fallback(
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
    logger.info("nobet_coz tamamlandi: basarili=%s, sure=%dms, atama=%d, gevsetme=%s",
                sonuc.basarili, toplam_sure_ms, len(sonuc.atamalar),
                bool(gevsetme_bilgisi))
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

    return sonuc, gevsetme_bilgisi, teshis_bilgisi, kullanilan_ara_gun
