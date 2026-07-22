"""Cross-layer regression checks for scheduling, IDs, logging and retry strategy."""

import json
import sys
from copy import deepcopy
from unittest.mock import patch

from firestore_logger import _chunk_json, _redact
from gun_iskelet_planlayici import GunIskeletPlanlayici
from hedef_hesaplayici import HedefHesaplayici
from ortools_solver import NobetSolver
from parsers import (
    parse_gorev_havuzlari,
    parse_manuel_atamalar,
    parse_solver_gorevler,
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


if __name__ == "__main__":
    tests = [name for name in globals() if name.startswith("test_")]
    for name in sorted(tests):
        globals()[name]()
        print(f"PASS {name}")
    print(f"\n*** {len(tests)} REGRESSION TEST PASSED ***")
