"""Cross-layer regression checks for scheduling, IDs, logging and retry strategy."""

import json
import sys
from copy import deepcopy
from unittest.mock import patch

from firestore_logger import _chunk_json, _redact
from gun_iskelet_planlayici import GunIskeletPlanlayici
from hedef_hesaplayici import HedefHesaplayici
from kapasite import kapasite_hesapla
from ortools_solver import NobetSolver
from parsers import (
    parse_gorev_havuzlari,
    parse_kurum_profili,
    parse_manuel_atamalar,
    parse_solver_gorevler,
    parse_solver_gorevler_nobet_coz,
    parse_solver_personeller_hedef,
)
from planlayici import frontend_kilitli_hedefleri_topla, plan_kontrati_hash_yenile
from preflight_analyzer import analyze_preflight
from solve_strategy import solve_with_diagnostics
from solver_models import SolverAtama, SolverGorev, SolverKural, SolverPersonel, SolverSonuc
from utils import JS_MAX_SAFE_INTEGER, normalize_id


def _minimal_veri():
    gun_tipleri = {1: "hici", 2: "hici"}
    personeller = [SolverPersonel(id=1, ad="A")]
    gorevler = [SolverGorev(id=1, ad="Acil", slot_idx=0, base_name="Acil")]
    hedefler = {1: {"hedef_toplam": 1, "hedef_tipler": {"hici": 1}}}
    return gun_tipleri, personeller, gorevler, hedefler


def test_js_safe_ids():
    text_id = normalize_id("abc")
    large_int = normalize_id(10**30)
    assert 0 <= text_id <= JS_MAX_SAFE_INTEGER
    assert 0 <= large_int <= JS_MAX_SAFE_INTEGER
    assert normalize_id("1000000000000000000000000000000") == large_int
    assert normalize_id("abc") == text_id


def test_ara_gun_semantigi():
    gun_tipleri = {g: "hici" for g in range(1, 6)}
    personel = SolverPersonel(id=1, ad="A")
    gorev = SolverGorev(id=1, ad="Acil", slot_idx=0, base_name="Acil")
    solver = NobetSolver(
        gun_sayisi=5,
        gun_tipleri=gun_tipleri,
        personeller=[personel],
        gorevler=[gorev],
        hedefler={1: {"hedef_toplam": 2, "hedef_tipler": {"hici": 2}}},
        ara_gun=2,
        max_sure_saniye=1,
    )
    assert solver._max_assignable_with_ara_gun([1, 2, 3, 4, 5]) == 2

    planlayici = object.__new__(GunIskeletPlanlayici)
    planlayici.ara_gun = 2
    planlayici.planlanan_gunler = {1: {1}}
    assert planlayici._ara_gun_ihlali_var_mi(1, 3) is True
    assert planlayici._ara_gun_ihlali_var_mi(1, 4) is False


def test_contract_v2_fields_and_explicit_target_locks():
    personel = parse_solver_personeller_hedef({
        "personeller": [{
            "id": "personel-a",
            "ad": "A",
            "isYukuKatsayisi": "0.5",
            "minNobet": "1",
            "maxNobet": "4",
            "esitlemedenMuaf": "false",
            "adaletGrubu": "sorumlu",
            "gecmisVeriDurumu": "tam",
            "yetkiliGorevler": ["AMATEM"],
        }]
    })[0]
    gorev = parse_solver_gorevler({
        "gorevler": [{
            "id": "amatem-1",
            "ad": "AMATEM",
            "baseName": "AMATEM",
            "ayriBina": True,
            "kritik": True,
        }]
    })[0]

    assert personel.is_yuku_katsayisi == 0.5
    assert personel.min_nobet == 1
    assert personel.max_nobet == 4
    assert personel.esitlemeden_muaf is False
    assert personel.adalet_grubu == "sorumlu"
    assert personel.yetkili_gorevler == {"AMATEM"}
    assert gorev.bina_id == "AYRI_BINA:AMATEM"
    assert gorev.kritik is True and gorev.exclusive is True
    assert gorev.istisna_politikasi == "asla"

    coz_gorev = parse_solver_gorevler_nobet_coz({
        "gorevler": [{
            "id": "amatem-1",
            "ad": "AMATEM",
            "baseName": "AMATEM",
            "ayriBina": True,
            "binaId": "BINA-B",
        }]
    }, 1)[0]
    assert coz_gorev.ayri_bina is True
    assert coz_gorev.bina_id == "BINA-B"

    personel.hedef_tipler = {"hici": 4}
    assert frontend_kilitli_hedefleri_topla([personel]) == {}
    kilitler = frontend_kilitli_hedefleri_topla(
        [personel], {str(personel.id): {"hici": 2}}
    )
    assert kilitler[personel.id]["hici"] == 2


def test_security_boolean_and_authoritative_pool_parsing():
    personeller = [SolverPersonel(id=1, ad="A")]
    gorevler = parse_solver_gorevler({
        "gorevler": [{
            "id": "amatem",
            "ad": "AMATEM",
            "baseName": "",
            "exclusive": "false",
            "kritik": "false",
        }]
    })
    assert gorevler[0].base_name == "AMATEM"
    assert gorevler[0].exclusive is False
    assert gorevler[0].kritik is False

    manuel = parse_manuel_atamalar({
        "manuelAtamalar": [{
            "personelId": 1,
            "gun": 1,
            "slotIdx": 0,
            "mazeretOnayli": "false",
        }]
    }, personeller, gorevler, 1)
    assert len(manuel) == 1
    assert manuel[0].mazeret_onayli is False

    havuzlar = parse_gorev_havuzlari({
        "gorevHavuzlari": {"AMATEM": []},
        "gorevKisitlamalari": [{
            "personelId": 1,
            "gorevAdi": "AMATEM",
            "tasmaGorevi": "AMATEM",
        }],
    }, gorevler, personeller)
    assert havuzlar == {"AMATEM": set()}

    try:
        parse_gorev_havuzlari(
            {"gorevHavuzlari": "gecersiz"}, gorevler, personeller
        )
    except ValueError:
        pass
    else:
        raise AssertionError("Malformed explicit task pool must fail closed")


def test_historical_saturday_debt_changes_target():
    gun_tipleri = {1: "cmt", 2: "cmt"}
    personeller = [
        SolverPersonel(
            id=1,
            ad="Fazla",
            yillik_gerceklesen={"cmt": 8},
            gecmis_veri_durumu="tam",
        ),
        SolverPersonel(
            id=2,
            ad="Eksik",
            yillik_gerceklesen={"cmt": 3},
            gecmis_veri_durumu="tam",
        ),
    ]
    sonuc = HedefHesaplayici(
        gun_sayisi=2,
        gun_tipleri=gun_tipleri,
        personeller=personeller,
        gorevler=[SolverGorev(id=1, ad="A", slot_idx=0, base_name="A")],
        ara_gun=0,
    ).hesapla()
    assert sonuc.basarili is True, sonuc.mesaj
    hedefler = {h["id"]: h for h in sonuc.hedefler}
    assert hedefler[2]["hedef_cmt"] > hedefler[1]["hedef_cmt"], hedefler
    assert sonuc.istatistikler["adalet"]["tarihsel_karsilastirma_personel_sayisi"] == 2


def test_workload_coefficient_and_min_max_targets():
    gun_tipleri = {gun: "hici" for gun in range(1, 7)}
    personeller = [
        SolverPersonel(id=1, ad="Sorumlu", is_yuku_katsayisi=0.5),
        SolverPersonel(id=2, ad="Normal", is_yuku_katsayisi=1.0),
    ]
    sonuc = HedefHesaplayici(
        gun_sayisi=6,
        gun_tipleri=gun_tipleri,
        personeller=personeller,
        gorevler=[SolverGorev(id=1, ad="A", slot_idx=0, base_name="A")],
        ara_gun=0,
    ).hesapla()
    assert sonuc.basarili is True, sonuc.mesaj
    hedefler = {h["id"]: h["hedef_toplam"] for h in sonuc.hedefler}
    assert hedefler == {1: 2, 2: 4}, hedefler

    sinirli = [
        SolverPersonel(id=1, ad="Min", min_nobet=3, max_nobet=3),
        SolverPersonel(id=2, ad="Kalan"),
    ]
    sinir_sonucu = HedefHesaplayici(
        gun_sayisi=4,
        gun_tipleri={gun: "hici" for gun in range(1, 5)},
        personeller=sinirli,
        gorevler=[SolverGorev(id=1, ad="A", slot_idx=0, base_name="A")],
        ara_gun=0,
    ).hesapla()
    assert sinir_sonucu.basarili is True, sinir_sonucu.mesaj
    sinir_hedefleri = {h["id"]: h["hedef_toplam"] for h in sinir_sonucu.hedefler}
    assert sinir_hedefleri[1] == 3, sinir_hedefleri


def test_fairness_redistributes_unavailable_and_exempt_capacity():
    gun_tipleri = {gun: "hici" for gun in range(1, 7)}
    gorevler = [SolverGorev(id=1, ad="A", slot_idx=0, base_name="A")]
    kapasite_sonucu = HedefHesaplayici(
        gun_sayisi=6,
        gun_tipleri=gun_tipleri,
        personeller=[
            SolverPersonel(id=1, ad="Yok", mazeret_gunleri=set(gun_tipleri)),
            SolverPersonel(id=2, ad="Normal", is_yuku_katsayisi=1.0),
            SolverPersonel(id=3, ad="Cift", is_yuku_katsayisi=2.0),
        ],
        gorevler=gorevler,
        ara_gun=0,
    ).hesapla()
    assert kapasite_sonucu.basarili is True, kapasite_sonucu.mesaj
    kapasite_hedefleri = {
        h["id"]: h["hedef_toplam"] for h in kapasite_sonucu.hedefler
    }
    assert kapasite_hedefleri == {1: 0, 2: 2, 3: 4}, kapasite_hedefleri

    muaf_sonucu = HedefHesaplayici(
        gun_sayisi=6,
        gun_tipleri=gun_tipleri,
        personeller=[
            SolverPersonel(id=1, ad="Muaf", esitlemeden_muaf=True, min_nobet=2),
            SolverPersonel(id=2, ad="Normal", is_yuku_katsayisi=1.0),
            SolverPersonel(id=3, ad="Cift", is_yuku_katsayisi=2.0),
        ],
        gorevler=gorevler,
        ara_gun=0,
    ).hesapla()
    assert muaf_sonucu.basarili is True, muaf_sonucu.mesaj
    muaf_hedefleri = {h["id"]: h["hedef_toplam"] for h in muaf_sonucu.hedefler}
    assert muaf_hedefleri == {1: 2, 2: 1, 3: 3}, muaf_hedefleri


def test_havuz_arzi_confined_kisilerin_toplam_hedefini_sinirlar():
    # Kişi-gün-görev birleşik kapasite: yalnız TEK role çalışabilen (confined)
    # kişilerin toplam hedefi, o rolün fiziksel slot arzını (slot × gün) aşamaz.
    # C,D'ye büyük geçmiş borç verilir → adalet onları azaltıp tüm yükü
    # AMATEM'e hapsedilmiş A,B,E'ye yığmak ister; havuz arzı buna sınır koyar.
    gun_tipleri = {gun: "hici" for gun in range(1, 7)}  # 6 gün
    gorevler = [
        SolverGorev(id=1, ad="AMATEM", slot_idx=0, base_name="AMATEM", exclusive=True),
        SolverGorev(id=2, ad="MAVI", slot_idx=1, base_name="MAVI"),
    ]
    personeller = [
        SolverPersonel(id=1, ad="A", gecmis_veri_durumu="tam", yillik_gerceklesen={"hici": 0}),
        SolverPersonel(id=2, ad="B", gecmis_veri_durumu="tam", yillik_gerceklesen={"hici": 0}),
        SolverPersonel(id=3, ad="E", gecmis_veri_durumu="tam", yillik_gerceklesen={"hici": 0}),
        SolverPersonel(id=4, ad="C", gecmis_veri_durumu="tam", yillik_gerceklesen={"hici": 50}),
        SolverPersonel(id=5, ad="D", gecmis_veri_durumu="tam", yillik_gerceklesen={"hici": 50}),
    ]
    sonuc = HedefHesaplayici(
        gun_sayisi=6,
        gun_tipleri=gun_tipleri,
        personeller=personeller,
        gorevler=gorevler,
        gorev_havuzlari={"AMATEM": {1, 2, 3}, "MAVI": {4, 5}},
        ara_gun=0,
    ).hesapla()
    assert sonuc.basarili is True, sonuc.mesaj
    hedefler = {h["id"]: h["hedef_toplam"] for h in sonuc.hedefler}
    # AMATEM arzı = 1 slot × 6 gün = 6 → hapsedilmiş {1,2,3} toplamı ≤ 6
    assert hedefler[1] + hedefler[2] + hedefler[3] <= 6, hedefler
    # MAVI arzı = 6 → hapsedilmiş {4,5} toplamı ≤ 6
    assert hedefler[4] + hedefler[5] <= 6, hedefler
    # Toplam slot korunur (2 slot × 6 gün = 12)
    assert sum(hedefler.values()) == 12, hedefler


def test_havuz_arzi_gun_tipi_bazinda_sinirlar():
    # Gün tipi granülerliği: toplam kapasite bol olsa bile, confined kişilerin
    # tek bir gün tipindeki hedefi o rolün o tipteki slot arzını aşamaz.
    # 4 gün hici + 2 gün cmt. AMATEM cmt arzı = 1 slot × 2 gün = 2.
    # C,D'ye büyük cmt borcu → adalet tüm cmt yükünü AMATEM'e hapsedilmiş
    # A,B'ye yığmak ister (toplam kapasite buna izin verir) → gün tipi sınırı keser.
    gun_tipleri = {1: "hici", 2: "hici", 3: "hici", 4: "hici", 5: "cmt", 6: "cmt"}
    gorevler = [
        SolverGorev(id=1, ad="AMATEM", slot_idx=0, base_name="AMATEM", exclusive=True),
        SolverGorev(id=2, ad="MAVI", slot_idx=1, base_name="MAVI"),
    ]
    personeller = [
        SolverPersonel(id=1, ad="A", gecmis_veri_durumu="tam", yillik_gerceklesen={"cmt": 0}),
        SolverPersonel(id=2, ad="B", gecmis_veri_durumu="tam", yillik_gerceklesen={"cmt": 0}),
        SolverPersonel(id=3, ad="C", gecmis_veri_durumu="tam", yillik_gerceklesen={"cmt": 50}),
        SolverPersonel(id=4, ad="D", gecmis_veri_durumu="tam", yillik_gerceklesen={"cmt": 50}),
    ]
    sonuc = HedefHesaplayici(
        gun_sayisi=6,
        gun_tipleri=gun_tipleri,
        personeller=personeller,
        gorevler=gorevler,
        gorev_havuzlari={"AMATEM": {1, 2}, "MAVI": {3, 4}},
        ara_gun=0,
    ).hesapla()
    assert sonuc.basarili is True, sonuc.mesaj
    tipler = {h["id"]: h["hedef_tipler"] for h in sonuc.hedefler}
    # AMATEM cmt arzı = 2 → hapsedilmiş {1,2}'nin cmt toplamı ≤ 2
    assert tipler[1]["cmt"] + tipler[2]["cmt"] <= 2, tipler
    # AMATEM hici arzı = 1 × 4 = 4 → {1,2} hici toplamı ≤ 4
    assert tipler[1]["hici"] + tipler[2]["hici"] <= 4, tipler
    # Tüm cmt slotları (2 rol × 2 gün = 4) dağıtılmalı → toplam korunur
    toplam = {h["id"]: h["hedef_toplam"] for h in sonuc.hedefler}
    assert sum(toplam.values()) == 12, toplam


def test_rol_transport_hall_alt_kume_ihlalini_yakalar():
    # Kişi-gün-görev BİRLEŞİK model (transport fizibilitesi): confined-tekil üst
    # sınırın KAÇIRDIĞI Hall-tipi çapraz uygunluğu yakalar. A,B rolleri herkese
    # açık; C rolünü yalnız P1,P2,P3 yapabilir ama her biri tek bir güne müsait
    # → C talebi (1 slot × 4 gün = 4) yalnız 3 kişi-gün ile karşılanabilir ve
    # gün4'te hiç C-yapabilen müsait değil → count seviyesinde infeasible.
    # Kimse TEK role hapsedilmediği için eski confined sınır bunu göremez;
    # transport fizibilitesi hedef modelini INFEASIBLE yapar (çizelge modelinin
    # dolduramayacağı hedef üretmek yerine boş slotu kökten engeller).
    gun_tipleri = {1: "hici", 2: "hici", 3: "hici", 4: "hici"}
    gorevler = [
        SolverGorev(id=1, ad="A", slot_idx=0, base_name="A"),
        SolverGorev(id=2, ad="B", slot_idx=1, base_name="B"),
        SolverGorev(id=3, ad="C", slot_idx=2, base_name="C"),
    ]
    personeller = [
        SolverPersonel(id=1, ad="P1", mazeret_gunleri={2, 3, 4}),  # C yapabilir, yalnız gün1
        SolverPersonel(id=2, ad="P2", mazeret_gunleri={1, 3, 4}),  # C yapabilir, yalnız gün2
        SolverPersonel(id=3, ad="P3", mazeret_gunleri={1, 2, 4}),  # C yapabilir, yalnız gün3
        SolverPersonel(id=4, ad="P4"),
        SolverPersonel(id=5, ad="P5"),
        SolverPersonel(id=6, ad="P6"),
        SolverPersonel(id=7, ad="P7"),
    ]
    sonuc = HedefHesaplayici(
        gun_sayisi=4,
        gun_tipleri=gun_tipleri,
        personeller=personeller,
        gorevler=gorevler,
        gorev_havuzlari={
            "A": {1, 2, 3, 4, 5, 6, 7},
            "B": {1, 2, 3, 4, 5, 6, 7},
            "C": {1, 2, 3},
        },
        ara_gun=0,
    ).hesapla()
    assert sonuc.basarili is False, (
        "Rol-transport karşılanamayan C talebini yakalamalı; üretilen hedefler: "
        f"{[(h['id'], h['hedef_toplam']) for h in sonuc.hedefler]}"
    )


def test_rol_transport_gecerli_kismi_rol_dagilimini_elemez():
    # Transport güvenli üst sınırdır: gerçekten fizibil kısmi-rol dağılımını
    # ASLA elemez. C'yi 3 kişi yapabilir ve 4 günün her birinde en az biri
    # müsait → C talebi (4) karşılanabilir; model feasible kalmalı.
    gun_tipleri = {1: "hici", 2: "hici", 3: "hici", 4: "hici"}
    gorevler = [
        SolverGorev(id=1, ad="A", slot_idx=0, base_name="A"),
        SolverGorev(id=2, ad="B", slot_idx=1, base_name="B"),
        SolverGorev(id=3, ad="C", slot_idx=2, base_name="C"),
    ]
    personeller = [
        SolverPersonel(id=1, ad="P1"),  # C yapabilir, tüm günler müsait
        SolverPersonel(id=2, ad="P2"),  # C yapabilir, tüm günler müsait
        SolverPersonel(id=3, ad="P3"),  # C yapabilir, tüm günler müsait
        SolverPersonel(id=4, ad="P4"),
        SolverPersonel(id=5, ad="P5"),
        SolverPersonel(id=6, ad="P6"),
        SolverPersonel(id=7, ad="P7"),
    ]
    sonuc = HedefHesaplayici(
        gun_sayisi=4,
        gun_tipleri=gun_tipleri,
        personeller=personeller,
        gorevler=gorevler,
        gorev_havuzlari={
            "A": {1, 2, 3, 4, 5, 6, 7},
            "B": {1, 2, 3, 4, 5, 6, 7},
            "C": {1, 2, 3},
        },
        ara_gun=0,
    ).hesapla()
    assert sonuc.basarili is True, sonuc.mesaj
    hedefler = {h["id"]: h["hedef_toplam"] for h in sonuc.hedefler}
    # C talebi 4; yalnız {1,2,3} yapabilir → toplam hedefleri ≥ 4 olmalı.
    assert hedefler[1] + hedefler[2] + hedefler[3] >= 4, hedefler
    assert sum(hedefler.values()) == 12, hedefler


def test_unknown_history_is_not_treated_as_zero_debt():
    sonuc = HedefHesaplayici(
        gun_sayisi=4,
        gun_tipleri={gun: "hici" for gun in range(1, 5)},
        personeller=[
            SolverPersonel(id=1, ad="Bilinmiyor", gecmis_veri_durumu="bilinmiyor"),
            SolverPersonel(
                id=2,
                ad="Tek Tam",
                gecmis_veri_durumu="tam",
                yillik_gerceklesen={"hici": 8},
            ),
        ],
        gorevler=[SolverGorev(id=1, ad="A", slot_idx=0, base_name="A")],
        ara_gun=0,
    ).hesapla()
    assert sonuc.basarili is True, sonuc.mesaj
    hedefler = {h["id"]: h["hedef_toplam"] for h in sonuc.hedefler}
    assert hedefler == {1: 2, 2: 2}, hedefler


def test_target_day_model_enforces_spacing_and_together_intersection():
    kilitli_sonuc = HedefHesaplayici(
        gun_sayisi=4,
        gun_tipleri={1: "cmt", 2: "hici", 3: "hici", 4: "cmt"},
        personeller=[
            SolverPersonel(id=1, ad="A", mazeret_gunleri={3}),
            SolverPersonel(id=2, ad="B"),
        ],
        gorevler=[SolverGorev(id=1, ad="A", slot_idx=0, base_name="A")],
        ara_gun=2,
        kilitli_hedefler={
            1: {"hici": 1, "prs": 0, "cum": 0, "cmt": 1, "pzr": 0}
        },
    ).hesapla()
    assert kilitli_sonuc.basarili is False

    ortak_sonuc = HedefHesaplayici(
        gun_sayisi=3,
        gun_tipleri={1: "cum", 2: "cum", 3: "cum"},
        personeller=[
            SolverPersonel(id=1, ad="A", mazeret_gunleri={3}),
            SolverPersonel(id=2, ad="B", mazeret_gunleri={1}),
            SolverPersonel(id=3, ad="C"),
            SolverPersonel(id=4, ad="D"),
            SolverPersonel(id=5, ad="E"),
        ],
        gorevler=[
            SolverGorev(id=1, ad="A1", slot_idx=0, base_name="A"),
            SolverGorev(id=2, ad="A2", slot_idx=1, base_name="A"),
            SolverGorev(id=3, ad="A3", slot_idx=2, base_name="A"),
        ],
        birlikte_kurallar=[
            SolverKural(tur="birlikte", kisiler=[1, 2], politika="hard")
        ],
        ara_gun=0,
    ).hesapla()
    assert ortak_sonuc.basarili is True, ortak_sonuc.mesaj
    ortak_hedefler = {h["id"]: h["hedef_toplam"] for h in ortak_sonuc.hedefler}
    assert ortak_hedefler[1] == ortak_hedefler[2]
    assert ortak_hedefler[1] <= 1, ortak_hedefler


def test_together_capacity_uses_real_date_intersection():
    hesaplayici = HedefHesaplayici(
        gun_sayisi=3,
        gun_tipleri={1: "cum", 2: "cum", 3: "cum"},
        personeller=[
            SolverPersonel(id=1, ad="A", mazeret_gunleri={3}),
            SolverPersonel(id=2, ad="B", mazeret_gunleri={1}),
        ],
        gorevler=[SolverGorev(id=1, ad="A", slot_idx=0, base_name="A")],
        ara_gun=0,
    )
    assert hesaplayici.personeller[1].musait_tipler["cum"] == 2
    assert hesaplayici.personeller[2].musait_tipler["cum"] == 2
    assert hesaplayici._birlikte_ortak_musait_tipler([1, 2])["cum"] == 1


def _takas_solver(personeller, gorevler=None, ara_gun=2, gun_sayisi=3, kurum_profili="genel"):
    if gorevler is None:
        gorevler = [SolverGorev(id=1, ad="R", slot_idx=0, base_name="R")]
    hedefler = {
        p.id: {"hedef_toplam": 1, "hedef_tipler": {"hici": 1}} for p in personeller
    }
    return NobetSolver(
        gun_sayisi=gun_sayisi,
        gun_tipleri={g: "hici" for g in range(1, gun_sayisi + 1)},
        personeller=personeller,
        gorevler=gorevler,
        hedefler=hedefler,
        ara_gun=ara_gun,
        max_sure_saniye=2,
        kurum_profili=kurum_profili,
    )


def test_takas_onerileri_ikili_takas_dogrudan_ve_negatif():
    # "Atanamama durumunda sor" — boş slotlar için salt-okunur, doğrulanmış
    # eyleme dönük öneriler (çoklu çözüm kartı). Analiz solve'dan bağımsız
    # (elle kurulmuş çözüm üzerinden) test edilir; her öneri sert kısıtlara
    # (mazeret/ara_gün/kota/ayrı) karşı geçerlidir.

    # 1) İKİLİ TAKAS: (2,0) boş; P yalnız ara_gün nedeniyle giremiyor (gün1'de
    #    nöbeti var), Q gün2'de mazeretli ama gün1'i doldurabilir → P'yi gün2'ye
    #    taşı, P'nin gün1 yerini Q doldursun.
    solver = _takas_solver([
        SolverPersonel(id=1, ad="P"),
        SolverPersonel(id=2, ad="Q", mazeret_gunleri={2}),
        SolverPersonel(id=3, ad="F"),
    ])
    atamalar = [
        {"gun": 1, "slot_idx": 0, "personel_id": 1},
        {"gun": 3, "slot_idx": 0, "personel_id": 3},
    ]
    oneriler = solver._bos_slot_takas_onerileri(atamalar)
    ikili = [o for o in oneriler if o["gun"] == 2 and o["tur"] == "ikili_takas"]
    assert len(ikili) == 1, oneriler
    o = ikili[0]
    assert o["tasinan_personel_id"] == 1
    assert o["tasinan_kaynak_gun"] == 1
    assert o["yerine_personel_id"] == 2

    # 2) DOĞRUDAN ATAMA: (2,0) boş; Q boşta, uygun ve müsait → doğrudan atanır.
    solver2 = _takas_solver([
        SolverPersonel(id=1, ad="P"),
        SolverPersonel(id=2, ad="Q"),
        SolverPersonel(id=3, ad="F"),
    ])
    oneriler2 = solver2._bos_slot_takas_onerileri(atamalar)
    dogrudan = [o for o in oneriler2 if o["gun"] == 2 and o["tur"] == "dogrudan_atama"]
    assert len(dogrudan) == 1, oneriler2
    assert dogrudan[0]["personel_id"] == 2

    # 3) NEGATİF: tek kişi, kotası dolu, takas için ikinci kişi yok → öneri yok.
    solver3 = _takas_solver([SolverPersonel(id=1, ad="P")])
    oneriler3 = solver3._bos_slot_takas_onerileri(
        [{"gun": 1, "slot_idx": 0, "personel_id": 1}]
    )
    assert oneriler3 == [], oneriler3


def test_leksikografik_bos_slot_onceligi_ve_gozlemlenebilir():
    # Leksikografik (çok geçişli) çözüm: Tier 1 boş slotu kesin öncelikle
    # minimize eder, Tier 2 bunu sabitleyip ağırlıklı amacı çözer. Tamamen
    # doldurulabilir bir senaryoda boş slot 0 olmalı ve leksikografik yolun
    # kullanıldığı istatistikte gözlemlenebilir olmalı (yeni sözleşme).
    gun_tipleri = {1: "hici", 2: "hici", 3: "hici", 4: "hici"}
    personeller = [
        SolverPersonel(id=1, ad="A"),
        SolverPersonel(id=2, ad="B"),
        SolverPersonel(id=3, ad="C"),
        SolverPersonel(id=4, ad="D"),
    ]
    gorevler = [
        SolverGorev(id=1, ad="S1", slot_idx=0, base_name="S1"),
        SolverGorev(id=2, ad="S2", slot_idx=1, base_name="S2"),
    ]
    hedefler = {
        p.id: {"hedef_toplam": 2, "hedef_tipler": {"hici": 2}} for p in personeller
    }
    ortak = dict(
        gun_sayisi=4,
        gun_tipleri=gun_tipleri,
        gorevler=gorevler,
        hedefler=hedefler,
        ara_gun=0,
        max_sure_saniye=3,
    )

    leks = NobetSolver(personeller=deepcopy(personeller), **ortak).coz()
    assert leks.basarili is True, leks.mesaj
    assert leks.istatistikler["leksikografik_kullanildi"] is True
    assert leks.istatistikler["bos_slot_sayisi"] == 0, leks.istatistikler

    # Kontrol: leksikografik kapalıyken tek geçiş kullanılır (bayrak False) ve
    # yine geçerli çözüm üretir. Leksikografik boş slotu asla artırmaz.
    tek = NobetSolver(
        personeller=deepcopy(personeller), leksikografik=False, **ortak
    ).coz()
    assert tek.basarili is True, tek.mesaj
    assert tek.istatistikler["leksikografik_kullanildi"] is False
    assert leks.istatistikler["bos_slot_sayisi"] <= tek.istatistikler["bos_slot_sayisi"]


def test_separate_rule_is_building_based():
    gun_tipleri = {1: "hici"}
    personeller = [SolverPersonel(id=1, ad="A"), SolverPersonel(id=2, ad="B")]
    hedefler = {
        1: {"hedef_toplam": 1, "hedef_tipler": {"hici": 1}},
        2: {"hedef_toplam": 1, "hedef_tipler": {"hici": 1}},
    }
    kural = SolverKural(tur="ayri", kisiler=[1, 2], politika="hard", asla_gevsetme=True)

    ayni_bina = NobetSolver(
        gun_sayisi=1,
        gun_tipleri=gun_tipleri,
        personeller=deepcopy(personeller),
        gorevler=[
            SolverGorev(id=1, ad="A1", slot_idx=0, base_name="A1", bina_id="ANA"),
            SolverGorev(id=2, ad="A2", slot_idx=1, base_name="A2", bina_id="ANA"),
        ],
        kurallar=[kural],
        hedefler=hedefler,
        ara_gun=0,
        max_sure_saniye=2,
    ).coz()
    assert len(ayni_bina.atamalar) <= 1, ayni_bina.atamalar

    farkli_bina = NobetSolver(
        gun_sayisi=1,
        gun_tipleri=gun_tipleri,
        personeller=deepcopy(personeller),
        gorevler=[
            SolverGorev(id=1, ad="A1", slot_idx=0, base_name="A1", bina_id="ANA"),
            SolverGorev(id=2, ad="B1", slot_idx=1, base_name="B1", bina_id="EK"),
        ],
        kurallar=[kural],
        hedefler=hedefler,
        ara_gun=0,
        max_sure_saniye=2,
    ).coz()
    assert len(farkli_bina.atamalar) == 2, farkli_bina.atamalar


def test_together_is_hard_and_manual_ara_gun_is_not_implicit():
    gun_tipleri = {1: "hici", 2: "hici"}
    gorevler = [
        SolverGorev(id=1, ad="A #1", slot_idx=0, base_name="A"),
        SolverGorev(id=2, ad="A #2", slot_idx=1, base_name="A"),
    ]
    personeller = [
        SolverPersonel(id=1, ad="A", mazeret_gunleri={2}),
        SolverPersonel(id=2, ad="B", mazeret_gunleri={1}),
    ]
    hedefler = {
        1: {"hedef_toplam": 1, "hedef_tipler": {"hici": 1}},
        2: {"hedef_toplam": 1, "hedef_tipler": {"hici": 1}},
    }
    plan = {
        "plan_hash": "birlikte-hard",
        "kaynak": "test",
        "olusturulan_ara_gun": 0,
        "hedefler": hedefler,
        "gun_iskeleti": {"aktif": False},
        "uygulama": {
            "yetkili": True,
            "toplam_hard": True,
            "gun_tipi_toleransi": 0,
            "gorev_kota_toleransi": 0,
            "gun_iskeleti_kullan": False,
        },
        "meta": {},
    }
    birlikte_sonucu = NobetSolver(
        gun_sayisi=2,
        gun_tipleri=gun_tipleri,
        personeller=personeller,
        gorevler=gorevler,
        kurallar=[SolverKural(tur="birlikte", kisiler=[1, 2])],
        hedefler=hedefler,
        plan_kontrati=plan,
        ara_gun=0,
        max_sure_saniye=2,
    ).coz()
    assert birlikte_sonucu.basarili is False
    assert birlikte_sonucu.istatistikler["status"] == "INFEASIBLE"

    manuel_solver = NobetSolver(
        gun_sayisi=2,
        gun_tipleri=gun_tipleri,
        personeller=[SolverPersonel(id=1, ad="A")],
        gorevler=[SolverGorev(id=1, ad="A", slot_idx=0, base_name="A")],
        manuel_atamalar=[
            SolverAtama(personel_id=1, gun=1, slot_idx=0),
            SolverAtama(personel_id=1, gun=2, slot_idx=0),
        ],
        hedefler={1: {"hedef_toplam": 2, "hedef_tipler": {"hici": 2}}},
        ara_gun=1,
        max_sure_saniye=2,
    )
    conflict_codes = {c["code"] for c in manuel_solver._manual_hard_conflict_diagnostics()}
    assert "ARA_GUN_IHLALI" in conflict_codes
    assert manuel_solver.aragun_istisna_set == set()


def test_critical_role_quota_is_not_authority():
    gorev = SolverGorev(
        id=1,
        ad="AMATEM",
        slot_idx=0,
        base_name="AMATEM",
        exclusive=True,
        kritik=True,
        bina_id="AMATEM_BINASI",
        istisna_politikasi="asla",
    )
    personeller = [SolverPersonel(id=1, ad="Yetkili"), SolverPersonel(id=2, ad="Yetkisiz")]
    solver = NobetSolver(
        gun_sayisi=1,
        gun_tipleri={1: "hici"},
        personeller=personeller,
        gorevler=[gorev],
        gorev_havuzlari={"AMATEM": {1}},
        manuel_atamalar=[SolverAtama(personel_id=2, gun=1, slot_idx=0)],
        hedefler={
            1: {"hedef_toplam": 0, "hedef_tipler": {}, "gorev_kotalari": {}},
            2: {
                "hedef_toplam": 1,
                "hedef_tipler": {"hici": 1},
                "gorev_kotalari": {"AMATEM": 1},
            },
        },
        ara_gun=0,
        max_sure_saniye=2,
    )
    sonuc = solver.coz()
    assert sonuc.basarili is False
    assert sonuc.istatistikler["status"] == "MANUAL_CONFLICT"
    codes = {c["code"] for c in sonuc.istatistikler["manual_conflicts"]}
    assert "EXCLUSIVE_IHLALI" in codes or "HAVUZ_IHLALI" in codes


def test_explicit_pool_and_manual_conflicts_cannot_be_bypassed():
    gorev = SolverGorev(
        id=1,
        ad="AMATEM",
        slot_idx=0,
        base_name="AMATEM",
        exclusive=True,
        kritik=True,
        istisna_politikasi="asla",
    )
    personel = SolverPersonel(
        id=1,
        ad="A",
        mazeret_gunleri={1},
        kisitli_gorev="AMATEM",
        tasma_gorevi="AMATEM",
        yetkili_gorevler={"AMATEM"},
    )
    sonuc = NobetSolver(
        gun_sayisi=1,
        gun_tipleri={1: "hici"},
        personeller=[personel],
        gorevler=[gorev],
        gorev_havuzlari={"AMATEM": set()},
        manuel_atamalar=[SolverAtama(personel_id=1, gun=1, slot_idx=0)],
        hedefler={1: {"hedef_toplam": 1, "hedef_tipler": {"hici": 1}}},
        ara_gun=0,
        max_sure_saniye=2,
        ignore_manual_conflicts=True,
    ).coz()
    assert sonuc.basarili is False
    assert sonuc.istatistikler["status"] == "MANUAL_CONFLICT"
    codes = {item["code"] for item in sonuc.istatistikler["manual_conflicts"]}
    assert "MAZERET_GUNU" in codes
    assert "HAVUZ_IHLALI" in codes


def test_hard_together_ignores_exception_and_soft_separate_stays_soft():
    hedefler = {
        1: {"hedef_toplam": 1, "hedef_tipler": {"hici": 1}},
        2: {"hedef_toplam": 1, "hedef_tipler": {"hici": 1}},
    }
    plan = {
        "plan_hash": "strict-rules",
        "kaynak": "test",
        "olusturulan_ara_gun": 0,
        "hedefler": hedefler,
        "gun_iskeleti": {"aktif": False},
        "uygulama": {
            "yetkili": True,
            "toplam_hard": True,
            "gun_tipi_toleransi": 0,
            "gorev_kota_toleransi": 0,
            "gun_iskeleti_kullan": False,
        },
        "meta": {},
    }
    gorevler = [
        SolverGorev(id=1, ad="A1", slot_idx=0, base_name="A", bina_id="ANA"),
        SolverGorev(id=2, ad="A2", slot_idx=1, base_name="A", bina_id="ANA"),
    ]
    hard_sonuc = NobetSolver(
        gun_sayisi=1,
        gun_tipleri={1: "hici"},
        personeller=[
            SolverPersonel(id=1, ad="A"),
            SolverPersonel(id=2, ad="B", mazeret_gunleri={1}),
        ],
        gorevler=gorevler,
        kurallar=[
            SolverKural(
                tur="birlikte",
                kisiler=[1, 2],
                politika="hard",
                asla_gevsetme=True,
            )
        ],
        birlikte_istisnalari=[{"personel_id": 2, "gun": 1}],
        hedefler=hedefler,
        plan_kontrati=plan,
        ara_gun=0,
        max_sure_saniye=2,
    ).coz()
    assert hard_sonuc.basarili is False
    assert hard_sonuc.istatistikler["status"] == "INFEASIBLE"

    soft_sonuc = NobetSolver(
        gun_sayisi=1,
        gun_tipleri={1: "hici"},
        personeller=[SolverPersonel(id=1, ad="A"), SolverPersonel(id=2, ad="B")],
        gorevler=gorevler,
        kurallar=[SolverKural(tur="ayri", kisiler=[1, 2], politika="soft")],
        hedefler=hedefler,
        plan_kontrati=plan,
        ara_gun=0,
        max_sure_saniye=2,
    ).coz()
    assert soft_sonuc.basarili is True, soft_sonuc.mesaj
    assert len(soft_sonuc.atamalar) == 2, soft_sonuc.atamalar

    fail_safe_sonuc = NobetSolver(
        gun_sayisi=1,
        gun_tipleri={1: "hici"},
        personeller=[SolverPersonel(id=1, ad="A"), SolverPersonel(id=2, ad="B")],
        gorevler=gorevler,
        kurallar=[
            SolverKural(
                tur="ayri",
                kisiler=[1, 2],
                politika="soft",
                asla_gevsetme=True,
            )
        ],
        manuel_atamalar=[
            SolverAtama(personel_id=1, gun=1, slot_idx=0),
            SolverAtama(personel_id=2, gun=1, slot_idx=1),
        ],
        hedefler=hedefler,
        ara_gun=0,
        max_sure_saniye=2,
    ).coz()
    assert fail_safe_sonuc.basarili is False
    assert fail_safe_sonuc.istatistikler["status"] == "MANUAL_CONFLICT"


def test_gun_iskeleti_soft_ve_hard_semantigi():
    gun_tipleri = {1: "hici", 2: "hici"}
    personeller = [
        SolverPersonel(id=1, ad="A", mazeret_gunleri={1}),
        SolverPersonel(id=2, ad="B"),
    ]
    gorevler = [SolverGorev(id=1, ad="Acil", slot_idx=0, base_name="Acil")]
    hedefler = {
        1: {"hedef_toplam": 1, "hedef_tipler": {"hici": 1}},
        2: {"hedef_toplam": 1, "hedef_tipler": {"hici": 1}},
    }
    plan = {
        "plan_hash": "test",
        "kaynak": "test",
        "olusturulan_ara_gun": 0,
        "hedefler": hedefler,
        "gun_iskeleti": {
            "aktif": True,
            "uygulanabilir_personeller": [1],
            "personel_gunleri": {1: [1]},
            "personel_rol_gunleri": {},
        },
        "uygulama": {
            "yetkili": True,
            "toplam_hard": True,
            "gun_tipi_toleransi": 0,
            "gorev_kota_toleransi": 0,
            "gun_iskeleti_kullan": True,
            "gun_iskeleti_toleransi": 0,
            "gun_iskeleti_hard": False,
            "gun_iskeleti_sadakat_agirligi": 4000,
        },
        "meta": {},
    }

    soft = NobetSolver(
        gun_sayisi=2,
        gun_tipleri=gun_tipleri,
        personeller=personeller,
        gorevler=gorevler,
        hedefler=hedefler,
        ara_gun=0,
        max_sure_saniye=2,
        plan_kontrati=plan,
    ).coz()
    assert soft.basarili is True, soft.mesaj
    assert any(a["personel_id"] == 1 and a["gun"] == 2 for a in soft.atamalar)

    hard_plan = deepcopy(plan)
    hard_plan["uygulama"]["gun_iskeleti_hard"] = True
    hard = NobetSolver(
        gun_sayisi=2,
        gun_tipleri=gun_tipleri,
        personeller=personeller,
        gorevler=gorevler,
        hedefler=hedefler,
        ara_gun=0,
        max_sure_saniye=2,
        plan_kontrati=hard_plan,
    ).coz()
    assert hard.basarili is False
    assert hard.istatistikler["status"] == "INFEASIBLE"


def test_preflight_skoru_kapasiteyi_maskelemez():
    personeller = [SolverPersonel(id=1, ad="A", mazeret_gunleri=set())]
    gorevler = [SolverGorev(id=1, ad="Acil", slot_idx=0, base_name="Acil")]
    sonuc = analyze_preflight(
        gun_sayisi=31,
        gun_tipleri={g: "hici" for g in range(1, 32)},
        personeller=personeller,
        gorevler=gorevler,
        kurallar=[],
        gorev_havuzlari={},
        manuel_atamalar=[],
        ara_gun=2,
        plan_kontrati={"hedefler": {}},
        kisitlama_istisnalari=[],
    )
    assert sonuc["skor"] < 80, sonuc
    assert sonuc["ozet"]["rol_kapasite_eksik_rol_sayisi"] == 1
    assert sonuc["metrikler"]["kapasite"]["roller"][0]["eksik"] == 20


def test_kapasite_gun_bazli_ara_gun_fizibilitesini_kesinlestirir():
    sonuc = kapasite_hesapla(
        gun_sayisi=2,
        gun_tipleri={1: "hici", 2: "hici"},
        personeller=[
            SolverPersonel(id=1, ad="A"),
            SolverPersonel(id=2, ad="B"),
        ],
        slot_sayisi=2,
        ara_gun=1,
    )

    assert sonuc["toplam_slot"] == 4
    assert sonuc["fizibilite"]["durum"] == "INFEASIBLE"
    assert sonuc["fizibilite"]["neden"]["kod"] == "ARA_GUN_PENCERE_KAPASITE_ACIGI"
    assert sonuc["fizibilite"]["oneri"]


def test_kapasite_hard_birlikte_manuel_mazeret_cakismasini_aciklar():
    sonuc = kapasite_hesapla(
        gun_sayisi=1,
        gun_tipleri={1: "hici"},
        personeller=[
            SolverPersonel(id=1, ad="A"),
            SolverPersonel(id=2, ad="B", mazeret_gunleri={1}),
            SolverPersonel(id=3, ad="C"),
        ],
        slot_sayisi=2,
        ara_gun=0,
        manuel_atamalar=[SolverAtama(personel_id=1, gun=1, slot_idx=0)],
        birlikte_kurallar=[
            SolverKural(tur="birlikte", kisiler=[1, 2], politika="hard")
        ],
    )

    fizibilite = sonuc["fizibilite"]
    assert fizibilite["durum"] == "INFEASIBLE"
    assert fizibilite["neden"]["kod"] == "BIRLIKTE_MANUEL_MAZERET_CAKISMASI"
    assert fizibilite["neden"]["detay"]["gun"] == 1


def test_kapasite_gun_bazli_fizibilite_gecerli_senaryoyu_kabul_eder():
    sonuc = kapasite_hesapla(
        gun_sayisi=2,
        gun_tipleri={1: "hici", 2: "hici"},
        personeller=[SolverPersonel(id=pid, ad=str(pid)) for pid in range(1, 5)],
        slot_sayisi=2,
        ara_gun=1,
    )

    assert sonuc["fizibilite"]["durum"] == "FEASIBLE"
    assert sonuc["fizibilite"]["neden"] is None
    assert sonuc["durum"] == "FEASIBLE"
    assert sonuc["uygulanabilir"] is True


def test_kapasite_ara_gun_pencere_acigini_tarih_araligiyla_raporlar():
    pencere = set(range(15, 22))
    uygun_gruplar = [
        {15, 16, 17},
        {18, 19, 20},
        {21},
        set(),
    ]
    personeller = []
    for grup_idx, uygun in enumerate(uygun_gruplar):
        for kisi_idx in range(3):
            pid = grup_idx * 3 + kisi_idx + 1
            personeller.append(SolverPersonel(
                id=pid,
                ad=f"P{pid}",
                mazeret_gunleri=pencere - uygun,
            ))

    sonuc = kapasite_hesapla(
        gun_sayisi=31,
        gun_tipleri={gun: "hici" for gun in range(1, 32)},
        personeller=personeller,
        slot_sayisi=3,
        ara_gun=2,
    )

    assert sonuc["durum"] == "INFEASIBLE"
    assert sonuc["uygulanabilir"] is False
    assert sonuc["mesaj"].startswith("INFEASIBLE:")
    aciklar = sonuc["neden"]["detay"]["ara_gun_pencere_aciklari"]
    hedef = next(a for a in aciklar if a["baslangic"] == 15 and a["bitis"] == 21)
    assert hedef == {
        "baslangic": 15,
        "bitis": 21,
        "gun_sayisi": 7,
        "talep": 21,
        "ust_kapasite": 9,
        "eksik": 12,
    }


def test_kapasite_aragun_ve_birlikte_istisnalarini_uygular():
    ara_sonuc = kapasite_hesapla(
        gun_sayisi=2,
        gun_tipleri={1: "hici", 2: "hici"},
        personeller=[SolverPersonel(id=1, ad="A"), SolverPersonel(id=2, ad="B")],
        slot_sayisi=2,
        ara_gun=1,
        aragun_istisnalari=[
            {"personel_id": 1, "gun1": 1, "gun2": 2},
            {"personel_id": 2, "gun1": 1, "gun2": 2},
        ],
    )
    assert ara_sonuc["durum"] == "FEASIBLE"

    birlikte_sonuc = kapasite_hesapla(
        gun_sayisi=1,
        gun_tipleri={1: "hici"},
        personeller=[
            SolverPersonel(id=1, ad="A"),
            SolverPersonel(id=2, ad="B", mazeret_gunleri={1}),
            SolverPersonel(id=3, ad="C"),
        ],
        slot_sayisi=2,
        ara_gun=0,
        manuel_atamalar=[SolverAtama(personel_id=1, gun=1, slot_idx=0)],
        birlikte_kurallar=[
            SolverKural(tur="birlikte", kisiler=[1, 2], politika="kullanici_onayli")
        ],
        birlikte_istisnalari=[{"personel_id": 2, "gun": 1}],
    )
    assert birlikte_sonuc["durum"] == "FEASIBLE"


def test_kapasite_tam_modelde_birlikte_gorev_ailesini_dogrular():
    gorevler = [
        SolverGorev(id=1, ad="A", slot_idx=0, base_name="A"),
        SolverGorev(id=2, ad="B", slot_idx=1, base_name="B"),
    ]
    sonuc = kapasite_hesapla(
        gun_sayisi=1,
        gun_tipleri={1: "hici"},
        personeller=[SolverPersonel(id=1, ad="A"), SolverPersonel(id=2, ad="B")],
        slot_sayisi=2,
        ara_gun=0,
        gorevler=gorevler,
        kurallar=[SolverKural(tur="birlikte", kisiler=[1, 2], politika="hard")],
    )

    assert sonuc["durum"] == "INFEASIBLE"
    assert sonuc["neden"]["kod"] == "TAM_CIZELGE_KISIT_CAKISMASI"


def test_kapasite_gorev_listesini_slot_sayisina_otorite_kabul_eder():
    gorevler = [
        SolverGorev(id=1, ad="A #1", slot_idx=0, base_name="A"),
        SolverGorev(id=2, ad="A #2", slot_idx=1, base_name="A"),
    ]
    sonuc = kapasite_hesapla(
        gun_sayisi=1,
        gun_tipleri={1: "hici"},
        personeller=[SolverPersonel(id=1, ad="A"), SolverPersonel(id=2, ad="B")],
        slot_sayisi=1,
        ara_gun=0,
        gorevler=gorevler,
    )

    assert sonuc["durum"] == "FEASIBLE"
    assert sonuc["toplam_slot"] == 2


def test_kapasite_soft_asla_gevsetme_birlikte_kuralini_hard_uygular():
    sonuc = kapasite_hesapla(
        gun_sayisi=1,
        gun_tipleri={1: "hici"},
        personeller=[
            SolverPersonel(id=1, ad="A"),
            SolverPersonel(id=2, ad="B", mazeret_gunleri={1}),
            SolverPersonel(id=3, ad="C"),
        ],
        slot_sayisi=2,
        ara_gun=0,
        manuel_atamalar=[SolverAtama(personel_id=1, gun=1, slot_idx=0)],
        birlikte_kurallar=[
            SolverKural(
                tur="birlikte", kisiler=[1, 2], politika="soft", asla_gevsetme=True
            )
        ],
    )

    assert sonuc["durum"] == "INFEASIBLE"
    assert sonuc["neden"]["kod"] == "BIRLIKTE_MANUEL_MAZERET_CAKISMASI"


def test_kapasite_gecersiz_slot_ve_ara_gun_degerlerini_reddeder():
    for slot_sayisi, ara_gun in [(0, 0), (1, -1)]:
        try:
            kapasite_hesapla(
                gun_sayisi=1,
                gun_tipleri={1: "hici"},
                personeller=[SolverPersonel(id=1, ad="A")],
                slot_sayisi=slot_sayisi,
                ara_gun=ara_gun,
            )
        except ValueError:
            pass
        else:
            raise AssertionError("Geçersiz kapasite parametresi kabul edildi")


def test_log_redaction_and_utf8_chunks():
    payload = {"ad": "Ayse Kaya", "mesaj": "Ayse Kaya icin çözüm", "veri": "ş" * 100}
    redacted = _redact(payload, {"Ayse Kaya": "P001"})
    assert redacted["ad"] == "P001"
    assert redacted["mesaj"] == "P001 icin çözüm"

    chunks = _chunk_json(payload, chunk_size=37)
    assert all(len(chunk.encode("utf-8")) <= 37 for chunk in chunks)
    assert json.loads("".join(chunks)) == payload


def test_plan_hash_changes_with_relaxation():
    plan = {
        "plan_hash": "old",
        "kaynak": "test",
        "olusturulan_ara_gun": 2,
        "hedefler": {1: {"hedef_toplam": 1, "hedef_tipler": {"hici": 1}}},
        "gun_iskeleti": {"aktif": False},
        "uygulama": {"toplam_hard": True},
        "meta": {},
    }
    ilk = plan_kontrati_hash_yenile(plan)
    gevsek = dict(ilk)
    gevsek["uygulama"] = {"toplam_hard": False}
    ikinci = plan_kontrati_hash_yenile(gevsek)
    assert ilk["plan_hash"] != ikinci["plan_hash"]


def test_detay_cozum_sure_adaptif():
    # Performans: detay adalet geçişi süre bütçesi örnek boyutuyla ölçeklenir.
    # Küçük örnekler tabanı kullanır (mevcut davranış korunur); büyük örnekler
    # tavana kadar daha fazla süre alır (50×31×6 timeout riski azalır).
    def _yap(n_personel, gun):
        gt = {g: "hici" for g in range(1, gun + 1)}
        pers = [SolverPersonel(id=i, ad=f"P{i}") for i in range(1, n_personel + 1)]
        gor = [SolverGorev(id=1, ad="A", slot_idx=0, base_name="A")]
        return HedefHesaplayici(
            gun_sayisi=gun, gun_tipleri=gt, personeller=pers, gorevler=gor, ara_gun=2
        )

    kucuk = _yap(3, 4)._detay_cozum_sure_saniye()
    buyuk = _yap(50, 31)._detay_cozum_sure_saniye()
    devasa = _yap(200, 31)._detay_cozum_sure_saniye()

    assert kucuk == HedefHesaplayici.DETAY_SURE_TABAN
    assert kucuk < buyuk <= HedefHesaplayici.DETAY_SURE_TAVAN
    assert devasa >= buyuk
    assert devasa == HedefHesaplayici.DETAY_SURE_TAVAN


def test_kilitli_hucre_atamalari_kismi_cozum():
    # Kısmi yeniden çözüm: önceki çözüm + kilit seçiminden sabitlenecek hücreler.
    from planlayici import kilitli_hucre_atamalari

    onceki = [
        {"personel_id": 1, "gun": 1, "slot_idx": 0, "gorev_base": "A"},
        {"personel_id": 2, "gun": 1, "slot_idx": 1, "gorev_base": "B"},
        {"personel_id": 3, "gun": 5, "slot_idx": 0, "gorev_base": "A"},
        {"personel_id": 4, "gun": 8, "slot_idx": 1, "gorev_base": "B"},
    ]

    # Kilit yoksa boş (davranış değişmez).
    assert kilitli_hucre_atamalari(onceki, []) == []
    assert kilitli_hucre_atamalari([], [{"tur": "hucre", "gun": 1, "slot_idx": 0}]) == []

    # Tek hücre kilidi.
    tek = kilitli_hucre_atamalari(onceki, [{"tur": "hucre", "gun": 1, "slot_idx": 0}])
    assert [(a.personel_id, a.gun, a.slot_idx) for a in tek] == [(1, 1, 0)]

    # Hafta (gün aralığı) kilidi: gün 1..5 arası tüm hücreler.
    hafta = kilitli_hucre_atamalari(
        onceki, [{"tur": "hafta", "gun_baslangic": 1, "gun_bitis": 5}]
    )
    assert {(a.gun, a.slot_idx) for a in hafta} == {(1, 0), (1, 1), (5, 0)}

    # Görev kilidi (slot_idx ile): slot 1'in tüm günleri.
    gorev_slot = kilitli_hucre_atamalari(onceki, [{"tur": "gorev", "slot_idx": 1}])
    assert {(a.gun, a.slot_idx) for a in gorev_slot} == {(1, 1), (8, 1)}

    # Görev kilidi (ad ile): base "A" olan tüm hücreler.
    gorev_ad = kilitli_hucre_atamalari(onceki, [{"tur": "gorev", "gorev": "A"}])
    assert {(a.gun, a.slot_idx) for a in gorev_ad} == {(1, 0), (5, 0)}

    # Personel kilidi: 4 numaranın tüm nöbetleri.
    kisi = kilitli_hucre_atamalari(onceki, [{"tur": "personel", "personel_id": 4}])
    assert [(a.personel_id, a.gun, a.slot_idx) for a in kisi] == [(4, 8, 1)]

    # Çakışan kilitler tekilleştirilir (hücre + hafta aynı hücreyi kapsar).
    karisik = kilitli_hucre_atamalari(
        onceki,
        [{"tur": "hucre", "gun": 1, "slot_idx": 0},
         {"tur": "hafta", "gun_baslangic": 1, "gun_bitis": 1}],
    )
    assert {(a.gun, a.slot_idx) for a in karisik} == {(1, 0), (1, 1)}
    assert len(karisik) == 2  # (1,0) iki kez eşleşse de tek kayıt

    # camelCase toleransı (frontend alan adları).
    camel = kilitli_hucre_atamalari(
        [{"personelId": 9, "gun": 2, "slotIdx": 0}],
        [{"tur": "hucre", "gun": 2, "slotIdx": 0}],
    )
    assert [(a.personel_id, a.gun, a.slot_idx) for a in camel] == [(9, 2, 0)]


def test_plan_hash_bayat_kontrolu():
    # Optimistik eşzamanlılık: gönderilen planHash güncel planla uyuşmuyorsa
    # (girdi değişmiş) bayat sayılır → nobet_coz 409 döner.
    from planlayici import plan_hash_bayat_mi

    # İlk çalıştırma / eksik hash → bayat değil (hash göndermeyen istemci korunur).
    assert plan_hash_bayat_mi(None, "H2") is False
    assert plan_hash_bayat_mi("", "H2") is False
    assert plan_hash_bayat_mi("H1", None) is False
    assert plan_hash_bayat_mi("H1", "") is False
    # Aynı hash → güncel.
    assert plan_hash_bayat_mi("H1", "H1") is False
    # Farklı dolu hash → bayat.
    assert plan_hash_bayat_mi("H1", "H2") is True
    # Tip toleransı (int/str karışık gelebilir).
    assert plan_hash_bayat_mi(123, "123") is False
    assert plan_hash_bayat_mi(123, "456") is True


def test_explicit_strict_policy_blocks_plan_relaxation():
    gun_tipleri, personeller, gorevler, hedefler = _minimal_veri()

    class InfeasibleSolver:
        calls = 0

        def __init__(self, **kwargs):
            type(self).calls += 1

        def coz(self):
            return SolverSonuc(
                False, [], {"status": "INFEASIBLE"}, 1, "infeasible"
            )

        def _build_feasibility_diagnostics(self):
            return {}

        def _diagnose_infeasible(self, diagnostics):
            return []

        def diagnose_with_unsat_core(self, max_sure_saniye):
            return {}

        def _build_feasibility_diagnostics(self):
            return {}

        def _diagnose_infeasible(self, diagnostics):
            return []

        def diagnose_with_unsat_core(self, max_sure_saniye):
            return {}

    plan = {
        "plan_hash": "strict",
        "kaynak": "test",
        "olusturulan_ara_gun": 1,
        "hedefler": hedefler,
        "gun_iskeleti": {"aktif": False},
        "uygulama": {
            "yetkili": True,
            "toplam_hard": True,
            "gun_tipi_toleransi": 0,
            "gorev_kota_toleransi": 0,
        },
        "meta": {},
    }
    with patch("solve_strategy.NobetSolver", InfeasibleSolver):
        sonuc, gevsetme, teshis, _ = solve_with_diagnostics(
            gun_sayisi=2,
            gun_tipleri=gun_tipleri,
            personeller=personeller,
            gorevler=gorevler,
            kurallar=[],
            gorev_havuzlari={},
            kisitlama_istisnalari=[],
            birlikte_istisnalari=[],
            aragun_istisnalari=[],
            manuel_atamalar=[],
            hedefler=hedefler,
            ara_gun=1,
            max_sure=2,
            yil=2026,
            ay=7,
            resmi_tatiller=[],
            data={
                "sozlesmeSurumu": 2,
                "tamirPolitikasi": {
                    "mod": "strict",
                    "otomatikGevsetme": False,
                },
            },
            plan_kontrati=plan,
        )
    assert InfeasibleSolver.calls == 1
    assert sonuc.basarili is False
    assert gevsetme.get("plan_gevsetildi") is not True
    assert teshis.get("tamir_politikasi", {}).get("plan_gevsetme_izinli") is False

    InfeasibleSolver.calls = 0
    with patch("solve_strategy.NobetSolver", InfeasibleSolver):
        _, kilit_gevsetme, _, _ = solve_with_diagnostics(
            gun_sayisi=2,
            gun_tipleri=gun_tipleri,
            personeller=personeller,
            gorevler=gorevler,
            kurallar=[],
            gorev_havuzlari={},
            kisitlama_istisnalari=[],
            birlikte_istisnalari=[],
            aragun_istisnalari=[],
            manuel_atamalar=[],
            hedefler=hedefler,
            ara_gun=1,
            max_sure=2,
            yil=2026,
            ay=7,
            resmi_tatiller=[],
            data={
                "kilitliHedefler": {"1": {"hici": 1}},
                "tamirPolitikasi": {
                    "mod": "tamir",
                    "otomatikGevsetme": True,
                },
            },
            plan_kontrati=plan,
        )
    assert InfeasibleSolver.calls == 1
    assert kilit_gevsetme.get("plan_gevsetildi") is not True


def test_explicit_strict_partial_result_is_not_success():
    gun_tipleri, personeller, gorevler, hedefler = _minimal_veri()

    class PartialSolver:
        calls = 0

        def __init__(self, **kwargs):
            type(self).calls += 1

        def coz(self):
            return SolverSonuc(
                True,
                [{"gun": 1, "slot_idx": 0, "personel_id": 1}],
                {
                    "status": "OPTIMAL",
                    "toplam_slot": 2,
                    "bos_slot_sayisi": 1,
                    "doluluk_yuzde": 50.0,
                },
                1,
                "partial",
            )

    with patch("solve_strategy.NobetSolver", PartialSolver):
        sonuc, _, teshis, _ = solve_with_diagnostics(
            gun_sayisi=2,
            gun_tipleri=gun_tipleri,
            personeller=personeller,
            gorevler=gorevler,
            kurallar=[],
            gorev_havuzlari={},
            kisitlama_istisnalari=[],
            birlikte_istisnalari=[],
            aragun_istisnalari=[],
            manuel_atamalar=[],
            hedefler=hedefler,
            ara_gun=1,
            max_sure=2,
            yil=2026,
            ay=7,
            resmi_tatiller=[],
            data={
                "sozlesmeSurumu": 2,
                "tamirPolitikasi": {
                    "mod": "strict",
                    "otomatikGevsetme": False,
                    "araGunAzaltma": "otomatik",
                },
            },
        )
    assert PartialSolver.calls == 1
    assert sonuc.basarili is False
    assert sonuc.istatistikler["status"] == "PARTIAL_REPAIR_REQUIRED"
    assert len(sonuc.atamalar) == 1
    assert teshis["onay_bekleyen_oneriler"][0]["aksiyon"] == "ara_gun_azalt"


def test_unknown_does_not_relax_rules():
    gun_tipleri, personeller, gorevler, hedefler = _minimal_veri()

    class UnknownSolver:
        calls = 0

        def __init__(self, **kwargs):
            type(self).calls += 1

        def coz(self):
            return SolverSonuc(False, [], {"status": "UNKNOWN", "timeout_olasi": True}, 1, "unknown")

    with patch("solve_strategy.NobetSolver", UnknownSolver):
        sonuc, gevsetme, teshis, _ = solve_with_diagnostics(
            gun_sayisi=2,
            gun_tipleri=gun_tipleri,
            personeller=personeller,
            gorevler=gorevler,
            kurallar=[],
            gorev_havuzlari={},
            kisitlama_istisnalari=[],
            birlikte_istisnalari=[],
            aragun_istisnalari=[],
            manuel_atamalar=[],
            hedefler=hedefler,
            ara_gun=1,
            max_sure=1,
            yil=2026,
            ay=7,
            resmi_tatiller=[],
            data={},
        )
    assert UnknownSolver.calls == 1
    assert sonuc.istatistikler["status"] == "UNKNOWN"
    assert gevsetme == {}
    assert teshis["kok_neden"] == "solver_belirsiz_veya_timeout"


def test_unknown_after_infeasible_stops_before_rule_relaxation():
    gun_tipleri, personeller, gorevler, hedefler = _minimal_veri()
    gorevler[0].exclusive = True
    kurallar = [SolverKural(tur="ayri", kisiler=[1, 2])]

    class SequenceSolver:
        calls = []
        results = [
            SolverSonuc(False, [], {"status": "INFEASIBLE"}, 1, "infeasible"),
            SolverSonuc(False, [], {"status": "UNKNOWN", "timeout_olasi": True}, 1, "unknown"),
        ]

        def __init__(self, **kwargs):
            type(self).calls.append(kwargs)

        def coz(self):
            return type(self).results.pop(0)

    plan = {
        "plan_hash": "old",
        "kaynak": "test",
        "olusturulan_ara_gun": 1,
        "hedefler": hedefler,
        "gun_iskeleti": {"aktif": False},
        "uygulama": {"yetkili": True, "toplam_hard": True},
        "meta": {},
    }
    with patch("solve_strategy.NobetSolver", SequenceSolver):
        sonuc, gevsetme, teshis, _ = solve_with_diagnostics(
            gun_sayisi=2,
            gun_tipleri=gun_tipleri,
            personeller=personeller,
            gorevler=gorevler,
            kurallar=kurallar,
            gorev_havuzlari={},
            kisitlama_istisnalari=[],
            birlikte_istisnalari=[],
            aragun_istisnalari=[],
            manuel_atamalar=[],
            hedefler=hedefler,
            ara_gun=1,
            max_sure=2,
            yil=2026,
            ay=7,
            resmi_tatiller=[],
            data={},
            plan_kontrati=plan,
        )

    assert len(SequenceSolver.calls) == 2
    assert all(call["gorevler"][0].exclusive is True for call in SequenceSolver.calls)
    assert all(any(k.tur == "ayri" for k in call["kurallar"]) for call in SequenceSolver.calls)
    assert sonuc.istatistikler["status"] == "UNKNOWN"
    assert gevsetme.get("plan_gevsetme_denendi") is True
    assert "exclusive_gevsetildi" not in gevsetme
    assert "ayri_gevsetildi" not in gevsetme
    assert teshis["kok_neden"] == "solver_belirsiz_veya_timeout"


def test_plan_relaxation_uses_normal_result_pipeline():
    gun_tipleri, personeller, gorevler, hedefler = _minimal_veri()

    class SequenceSolver:
        results = [
            SolverSonuc(False, [], {"status": "INFEASIBLE"}, 1, "infeasible"),
            SolverSonuc(True, [{"gun": 1}], {"status": "FEASIBLE", "bos_slot_sayisi": 0}, 1, "ok"),
        ]

        def __init__(self, **kwargs):
            pass

        def coz(self):
            return type(self).results.pop(0)

    plan = {
        "plan_hash": "old",
        "kaynak": "test",
        "olusturulan_ara_gun": 1,
        "hedefler": hedefler,
        "gun_iskeleti": {"aktif": False},
        "uygulama": {
            "yetkili": True,
            "toplam_hard": True,
            "gun_tipi_toleransi": 0,
            "gorev_kota_toleransi": 0,
            "gun_iskeleti_toleransi": 0,
        },
        "meta": {},
    }
    with patch("solve_strategy.NobetSolver", SequenceSolver):
        sonuc, gevsetme, _, _ = solve_with_diagnostics(
            gun_sayisi=2,
            gun_tipleri=gun_tipleri,
            personeller=personeller,
            gorevler=gorevler,
            kurallar=[],
            gorev_havuzlari={},
            kisitlama_istisnalari=[],
            birlikte_istisnalari=[],
            aragun_istisnalari=[],
            manuel_atamalar=[],
            hedefler=hedefler,
            ara_gun=1,
            max_sure=2,
            yil=2026,
            ay=7,
            resmi_tatiller=[],
            data={},
            plan_kontrati=plan,
        )
    assert sonuc.basarili is True
    assert gevsetme["plan_gevsetildi"] is True
    assert "doluluk_raporu" in sonuc.istatistikler
    kontrat = sonuc.istatistikler["plan"]["kontrat"]
    assert kontrat["uygulama"]["toplam_hard"] is False
    assert kontrat["plan_hash"] != "old"


def test_zero_budget_starts_no_solver():
    gun_tipleri, personeller, gorevler, hedefler = _minimal_veri()

    class NeverSolver:
        def __init__(self, **kwargs):
            raise AssertionError("Solver must not start after the global deadline")

    with patch("solve_strategy.NobetSolver", NeverSolver):
        sonuc, _, teshis, _ = solve_with_diagnostics(
            gun_sayisi=2,
            gun_tipleri=gun_tipleri,
            personeller=personeller,
            gorevler=gorevler,
            kurallar=[],
            gorev_havuzlari={},
            kisitlama_istisnalari=[],
            birlikte_istisnalari=[],
            aragun_istisnalari=[],
            manuel_atamalar=[],
            hedefler=hedefler,
            ara_gun=1,
            max_sure=0,
            yil=2026,
            ay=7,
            resmi_tatiller=[],
            data={},
        )
    assert sonuc.istatistikler["status"] == "DEADLINE_EXCEEDED"
    assert teshis["kok_neden"] == "global_sure_butcesi_doldu"


def test_kurum_profili_bayragi_akisi():
    """Madde 0: kurumProfili bayragi parse edilir + iki solver modeline akar.

    Geriye tam uyumlu: bayrak verilmezse default 'genel' (mevcut davranis).
    Bu asamada bayrak yalnizca SAKLANIR; hicbir kisit degisikligi yapmaz.
    """
    # 1) Saf normalizer: 112 varyantlari -> "112", digerleri -> "genel".
    assert parse_kurum_profili("112") == "112"
    assert parse_kurum_profili("112 Ambulans") == "112"
    assert parse_kurum_profili("AMBULANS") == "112"
    assert parse_kurum_profili("Genel Hastane") == "genel"
    assert parse_kurum_profili("hastane") == "genel"
    assert parse_kurum_profili(None) == "genel"
    assert parse_kurum_profili("") == "genel"
    assert parse_kurum_profili("bilinmeyen") == "genel"

    gun_tipleri, personeller, gorevler, hedefler = _minimal_veri()

    # 2) Hedef modeli bayragi saklar; default geriye uyumlu "genel".
    h_default = HedefHesaplayici(
        gun_sayisi=2, gun_tipleri=gun_tipleri,
        personeller=personeller, gorevler=gorevler,
    )
    assert h_default.kurum_profili == "genel"
    h_112 = HedefHesaplayici(
        gun_sayisi=2, gun_tipleri=gun_tipleri,
        personeller=personeller, gorevler=gorevler,
        kurum_profili="112",
    )
    assert h_112.kurum_profili == "112"

    # 3) Cizelge modeli bayragi saklar; default "genel".
    s_default = NobetSolver(
        gun_sayisi=2, gun_tipleri=gun_tipleri,
        personeller=personeller, gorevler=gorevler, hedefler=hedefler,
    )
    assert s_default.kurum_profili == "genel"
    s_112 = NobetSolver(
        gun_sayisi=2, gun_tipleri=gun_tipleri,
        personeller=personeller, gorevler=gorevler, hedefler=hedefler,
        kurum_profili="112",
    )
    assert s_112.kurum_profili == "112"

    # 4) ortak_plan_uret bayragi hedef modeline gecirir (imza akis dogrulama).
    from planlayici import ortak_plan_uret
    plan = ortak_plan_uret(
        gun_sayisi=2, gun_tipleri=gun_tipleri,
        personeller=personeller, gorevler=gorevler,
        kurum_profili="112",
    )
    assert isinstance(plan, dict)


def test_izin_turu_ayrimi_veri_modeli():
    """Madde 1: izin turleri (izin/egitim/rapor/nobet_izni/mazeret) ayristirilir.

    Mevcut ``mazeret_gunleri`` (schedule bloklayan birlesim) korunur; paralel
    ``izin_turleri`` haritasi tur bilgisini SAKLAR (madde 2 mesai-borcu dususu
    ve madde 5 izin yerlesimi bunu tuketecek). Bu asamada davranis degismez.
    """
    from utils import _extract_izin_turleri, _extract_mazeret_gunleri

    p = {
        "yillikIzinler": [3, 4],
        "raporlar": [5],
        "egitimler": [6],
        "nobetIzinleri": [7],
        "mazeretler": [8],
    }
    turler = _extract_izin_turleri(p)
    assert turler[3] == "izin"
    assert turler[4] == "izin"
    assert turler[5] == "rapor"
    assert turler[6] == "egitim"
    assert turler[7] == "nobet_izni"
    assert turler[8] == "mazeret"

    # Cakisma: yuksek oncelikli kaynak (izin/rapor/egitim) plain mazeret'i yener.
    assert _extract_izin_turleri({"mazeretler": [3], "yillikIzinler": [3]})[3] == "izin"
    # Bos girdi -> bos harita (geriye tam uyumlu).
    assert _extract_izin_turleri({}) == {}

    # mazeret_gunleri artik egitim/rapor gunlerini de kapsamali (schedule bloklanir).
    mg = _extract_mazeret_gunleri({"raporlar": [5], "egitimler": [6], "yillikIzinler": [3]})
    assert {3, 5, 6} <= mg

    # SolverPersonel izin_turleri tasir; default bos (geriye uyumlu).
    assert SolverPersonel(id=1, ad="X").izin_turleri == {}

    # Parser ucu: izin_turleri SolverPersonel'e akar.
    kisiler = parse_solver_personeller_hedef({
        "personeller": [{"id": 1, "ad": "A", "yillikIzinler": [2], "raporlar": [3]}]
    })
    assert kisiler[0].izin_turleri == {2: "izin", 3: "rapor"}
    assert {2, 3} <= kisiler[0].mazeret_gunleri


def test_mesai_bazli_min_nobet_112_soft():
    """Madde 2: 112 profilinde min nobet mesai saatinden turetilir (SOFT).

    is_gunu = hafta ici (hici/prs/cum), resmi tatil haric. izin/egitim/rapor
    IS GUNUNE denk gelince borctan duser. min = ceil(net_is_gunu / 3).
    Kapasite yetmezse INFEASIBLE OLMAZ: alt sinir kapasiteye kirpilir, acik
    ('min_nobet_acigi') raporlanir. Genel profilde mesai hesabi UYGULANMAZ.
    """
    gorev = [SolverGorev(id=1, ad="X", slot_idx=0, base_name="X")]
    is_gunu9 = {g: "hici" for g in range(1, 10)}          # 9 is gunu
    is_gunu9[10] = "cmt"; is_gunu9[11] = "pzr"            # hafta sonu (sayilmaz)

    # (a) izinsiz: ceil(9/3) = 3.
    h = HedefHesaplayici(gun_sayisi=11, gun_tipleri=is_gunu9,
                         personeller=[SolverPersonel(id=1, ad="A")], gorevler=gorev,
                         ara_gun=0, kurum_profili="112")
    assert h._mesai_min_nobet(SolverPersonel(id=1, ad="A")) == 3

    # (b) izin/rapor IS GUNUNDE borctan duser; hafta sonu izni (gun10) sayilmaz.
    p_izin = SolverPersonel(id=1, ad="A",
                            izin_turleri={1: "izin", 2: "izin", 3: "rapor", 10: "izin"})
    assert h._mesai_min_nobet(p_izin) == 2  # net 6 -> ceil(6/3)=2

    # (c) resmi tatil (cum tipli) IS GUNU sayilmaz.
    gt = {g: "hici" for g in range(1, 10)}
    gt[10] = "cum"                                        # tip'e gore is gunu gibi
    h_tatilsiz = HedefHesaplayici(gun_sayisi=10, gun_tipleri=gt,
                                  personeller=[SolverPersonel(id=1, ad="A")], gorevler=gorev,
                                  ara_gun=0, kurum_profili="112")
    assert h_tatilsiz._mesai_min_nobet(SolverPersonel(id=1, ad="A")) == 4  # 10 -> ceil(10/3)=4
    h_tatil = HedefHesaplayici(gun_sayisi=10, gun_tipleri=gt,
                               personeller=[SolverPersonel(id=1, ad="A")], gorevler=gorev,
                               ara_gun=0, kurum_profili="112", resmi_tatil_gunleri={10})
    assert h_tatil._mesai_min_nobet(SolverPersonel(id=1, ad="A")) == 3  # gun10 haric -> 9 -> 3

    # (d) genel profilde mesai hesabi 0 (mevcut min_nobet mekanizmasi gecerli).
    h_gen = HedefHesaplayici(gun_sayisi=11, gun_tipleri=is_gunu9,
                             personeller=[SolverPersonel(id=1, ad="A")], gorevler=gorev,
                             ara_gun=0)
    assert h_gen._mesai_min_nobet(SolverPersonel(id=1, ad="A")) == 0

    # (e) SOFT: kapasite (max_nobet) mesai min'in altinda -> INFEASIBLE DEGIL, acik raporlanir.
    #     Kisi1 max_nobet=1 ama mesai min=3; Kisi2 talebi emer.
    kisiler = [SolverPersonel(id=1, ad="Kisitli", max_nobet=1),
               SolverPersonel(id=2, ad="Serbest")]
    sonuc = HedefHesaplayici(gun_sayisi=11, gun_tipleri=is_gunu9, personeller=kisiler,
                             gorevler=gorev, ara_gun=0, kurum_profili="112").hesapla()
    assert sonuc.basarili is True, sonuc.mesaj
    h1 = next(h for h in sonuc.hedefler if h["id"] == 1)
    assert h1["adalet"]["sinirlar"]["min_nobet_acigi"] == 2   # 3 hedef - 1 kapasite
    assert h1["hedef_toplam"] <= 1

    # (f) Acik ozeti istatistiklerde: kim/ne kadar eksik + neden + oneri.
    aciklar = sonuc.istatistikler.get("min_nobet_aciklari", [])
    kisitli = next(a for a in aciklar if a["personel_id"] == 1)
    assert kisitli["acik"] == 2
    assert kisitli["hedef_min_nobet"] == 3 and kisitli["ulasilabilen"] == 1
    assert kisitli["neden"] == "max_nobet"       # max_nobet=1 baglayici
    assert kisitli["oneri"]                        # somut oneri metni dolu

    # (g) Genel profilde acik ozeti bos (mesai hesabi pasif).
    genel_sonuc = HedefHesaplayici(gun_sayisi=11, gun_tipleri=is_gunu9, personeller=kisiler,
                                   gorevler=gorev, ara_gun=0).hesapla()
    assert genel_sonuc.istatistikler.get("min_nobet_aciklari", []) == []


def test_max_ara_gun_112_soft():
    """Madde 3: 112 profilinde max ara gün SOFT ceza (kör-hard DEĞİL).

    H4 (min ara gün, hard) ile makas kurmamak + gevşetme döngüsünü şaşırtmamak
    için max ara gün penceresi SOFT cezayla uygulanır: sağlanamazsa INFEASIBLE
    olmaz. Ulaşılabilir senaryoda ceza nöbetleri ≤ max_ara_gun aralıkla yayar.
    Genel profilde tamamen pasif (davranış değişmez).
    """
    gorevler = [SolverGorev(id=0, ad="Acil", slot_idx=0, base_name="Acil")]

    def _coz(gun_sayisi, hedef_toplam, kurum_profili="genel", max_ara_gun=0):
        gun_tipleri = {g: "hici" for g in range(1, gun_sayisi + 1)}
        hedefler = {1: {"hedef_toplam": hedef_toplam,
                        "hedef_tipler": {"hici": hedef_toplam, "prs": 0,
                                         "cum": 0, "cmt": 0, "pzr": 0}}}
        return NobetSolver(
            gun_sayisi=gun_sayisi, gun_tipleri=gun_tipleri,
            personeller=[SolverPersonel(id=1, ad="A", mazeret_gunleri=set())],
            gorevler=gorevler, kurallar=[], gorev_havuzlari={},
            kisitlama_istisnalari=[], birlikte_istisnalari=[], aragun_istisnalari=[],
            manuel_atamalar=[], hedefler=hedefler, ara_gun=0, max_sure_saniye=10,
            kurum_profili=kurum_profili, max_ara_gun=max_ara_gun,
        ).coz()

    def _max_gap(sonuc):
        gunler = sorted(a["gun"] for a in sonuc.atamalar if a["personel_id"] == 1)
        farklar = [gunler[i + 1] - gunler[i] for i in range(len(gunler) - 1)]
        return max(farklar) if farklar else 0

    # (a) Stat bayrağı: genel -> 0, 112 -> 5 (yalnız 112'de aktif).
    s_genel = _coz(15, 3, "genel", 0)
    assert s_genel.istatistikler.get("max_ara_gun", 0) == 0
    s_112 = _coz(15, 3, "112", 5)
    assert s_112.istatistikler.get("max_ara_gun") == 5

    # (b) 112, ulaşılabilir: 3 nöbet/15 gün -> ardışık max aralık <= 5.
    assert s_112.basarili is True, s_112.mesaj
    assert _max_gap(s_112) <= 5

    # (c) SOFT: max-gap sağlanamayan senaryo (2 nöbet/20 gün) INFEASIBLE OLMAZ.
    s_soft = _coz(20, 2, "112", 5)
    assert s_soft.basarili is True, s_soft.mesaj


def test_yari_vardiya_onerileri_112():
    """Madde 4: 12/12 son çare doldurma (yalnız 112 profili, kullanıcı onaylı).

    Boş slot normal/takasla dolamayınca, yapısal uygun kişiler gündüz+gece
    12'şer saatle böler. Gece adayı g-1'de nöbette OLMAMALI (sabah çıkan o
    akşam yazılamaz) ve g+1'de olmamalı. Salt-okunur; Madde 2 (24s) değişmez.
    """
    personeller = [
        SolverPersonel(id=1, ad="A"),
        SolverPersonel(id=2, ad="B"),
        SolverPersonel(id=3, ad="C"),
    ]
    # A@gün1, B@gün5 (kotaları dolu ama gündüz/gece uygun); C@gün2 = (3. günün
    # g-1'i) → C gece adayı OLAMAZ (dinlenme). 3. gün boş → 12/12 önerisi.
    atamalar = [
        {"gun": 1, "slot_idx": 0, "personel_id": 1},
        {"gun": 5, "slot_idx": 0, "personel_id": 2},
        {"gun": 2, "slot_idx": 0, "personel_id": 3},
    ]
    solver = _takas_solver(personeller, ara_gun=0, gun_sayisi=5, kurum_profili="112")
    oneriler = solver._bos_slot_takas_onerileri(atamalar)
    yari = [o for o in oneriler if o["gun"] == 3 and o["tur"] == "yari_vardiya"]
    assert len(yari) >= 1, oneriler
    o = yari[0]
    assert o["gece_personel_id"] != 3          # C (g-1'de nöbette) gece olamaz
    assert o["gunduz_personel_id"] != o["gece_personel_id"]  # iki farklı kişi
    assert {o["gunduz_personel_id"], o["gece_personel_id"]} <= {1, 2}

    # Genel profilde 12/12 önerisi YOK (yalnız 112'ye özel).
    genel = _takas_solver(personeller, ara_gun=0, gun_sayisi=5, kurum_profili="genel")
    oneriler_g = genel._bos_slot_takas_onerileri(atamalar)
    assert not [o for o in oneriler_g if o["tur"] == "yari_vardiya"], oneriler_g


def test_izin_yerlesim_112():
    """Madde 5: 112, yıllık izin öncesi 2 gün boşluk + sonrası ilk iş günü (SOFT).

    Yalnız yıllık izin (tur=='izin'); rapor/eğitim hariç. Öncesi: izin başından
    2 gün önce nöbet yazılmaması tercih edilir. Sonrası: izin bitiminden sonraki
    ilk iş gününe (hici/prs/cum) nöbet tercih edilir (hafta sonu araya girse de).
    """
    gun_tipleri = {1: "hici", 2: "hici", 3: "hici", 4: "hici", 5: "hici",
                   6: "hici", 7: "hici", 8: "cmt", 9: "pzr", 10: "hici", 11: "hici"}
    p = SolverPersonel(id=1, ad="A",
                       izin_turleri={2: "rapor", 5: "izin", 6: "izin", 7: "izin"},
                       mazeret_gunleri={2, 5, 6, 7})
    solver = NobetSolver(
        gun_sayisi=11, gun_tipleri=gun_tipleri, personeller=[p],
        gorevler=[SolverGorev(id=1, ad="R", slot_idx=0, base_name="R")],
        kurallar=[], gorev_havuzlari={}, kisitlama_istisnalari=[],
        birlikte_istisnalari=[], aragun_istisnalari=[], manuel_atamalar=[],
        hedefler={1: {"hedef_toplam": 1, "hedef_tipler": {"hici": 1}}},
        ara_gun=0, max_sure_saniye=5, kurum_profili="112",
    )
    # Yalnız yıllık izin blokları (rapor gün2 hariç), ardışık gruplanır.
    assert solver._yillik_izin_bloklari(p) == [[5, 6, 7]]
    # izin bitişi gün7; 8=cmt,9=pzr hafta sonu -> ilk iş günü gün10.
    assert solver._ilk_is_gunu_sonrasi(7) == 10
    assert solver._ilk_is_gunu_sonrasi(3) == 4       # ertesi gün iş günüyse onu döner
    assert solver._ilk_is_gunu_sonrasi(11) is None   # sonrası iş günü yoksa None

    # Davranışsal (SOFT): tek nöbet izin sonrası ilk iş gününe (gün10) yerleşir.
    sonuc = solver.coz()
    assert sonuc.basarili is True, sonuc.mesaj
    gunler = sorted(a["gun"] for a in sonuc.atamalar if a["personel_id"] == 1)
    assert gunler == [10], gunler


if __name__ == "__main__":
    tests = [name for name in globals() if name.startswith("test_")]
    for name in sorted(tests):
        globals()[name]()
        print(f"PASS {name}")
    print(f"\n*** {len(tests)} REGRESSION TEST PASSED ***")
