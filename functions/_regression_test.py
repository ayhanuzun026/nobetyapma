"""Cross-layer regression checks for scheduling, IDs, logging and retry strategy."""

import json
import sys
from copy import deepcopy
from unittest.mock import patch

from firestore_logger import _chunk_json, _redact
from gun_iskelet_planlayici import GunIskeletPlanlayici
from ortools_solver import NobetSolver
from planlayici import plan_kontrati_hash_yenile
from preflight_analyzer import analyze_preflight
from solve_strategy import solve_with_diagnostics
from solver_models import SolverGorev, SolverKural, SolverPersonel, SolverSonuc
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
