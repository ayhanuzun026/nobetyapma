"""Approved exceptions must reach target planning before the final solver."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "functions"))

from hedef_hesaplayici import HedefHesaplayici
from planlayici import ortak_plan_uret
from solver_models import SolverAtama, SolverGorev, SolverKural, SolverPersonel
from ortools_solver import NobetSolver


def test_approved_rest_exception_reaches_target_and_manual_capacity():
    args = dict(
        gun_sayisi=2, gun_tipleri={1: "hici", 2: "hici"},
        personeller=[SolverPersonel(id=1, ad="A")],
        gorevler=[SolverGorev(id=1, ad="Acil", slot_idx=0, base_name="Acil")],
        ara_gun=2,
        manuel_atamalar=[SolverAtama(personel_id=1, gun=1, slot_idx=0),
                         SolverAtama(personel_id=1, gun=2, slot_idx=0)],
    )
    assert not ortak_plan_uret(**args)["basarili"]
    result = ortak_plan_uret(
        **args, aragun_istisnalari=[{"personel_id": 1, "gun1": 1, "gun2": 2}]
    )
    assert result["basarili"], result["mesaj"]
    assert result["hedefler_map"][1]["hedef_toplam"] == 2


def test_approved_together_exception_reaches_target():
    rule = SolverKural(tur="birlikte", kisiler=[1, 2], politika="kullanici_onayli")
    args = dict(
        gun_sayisi=1, gun_tipleri={1: "hici"},
        personeller=[SolverPersonel(id=1, ad="A"),
                     SolverPersonel(id=2, ad="B", mazeret_gunleri={1})],
        gorevler=[SolverGorev(id=1, ad="Acil", slot_idx=0, base_name="Acil")],
        kurallar=[rule], ara_gun=0,
        manuel_atamalar=[SolverAtama(personel_id=1, gun=1, slot_idx=0)],
    )
    assert not ortak_plan_uret(**args)["basarili"]
    result = ortak_plan_uret(
        **args, birlikte_istisnalari=[{"personel_id": 2, "gun": 1}]
    )
    assert result["basarili"], result["mesaj"]
    assert result["hedefler_map"][1]["hedef_toplam"] == 1
    assert result["hedefler_map"][2]["hedef_toplam"] == 0
    rule.asla_gevsetme = True
    assert not ortak_plan_uret(
        **args, birlikte_istisnalari=[{"personel_id": 2, "gun": 1}]
    )["basarili"]


def test_role_exception_does_not_bypass_authoritative_pool():
    """A day-scoped task exception cannot bypass an authoritative pool."""
    args = dict(
        gun_sayisi=1, gun_tipleri={1: "hici"},
        personeller=[SolverPersonel(id=1, ad="A")],
        gorevler=[SolverGorev(id=1, ad="Acil", slot_idx=0, base_name="Acil")],
        gorev_havuzlari={"Acil": set()}, ara_gun=0,
    )
    result = ortak_plan_uret(
        **args, kisitlama_istisnalari=[{"personel_id": 1, "gun": 1, "istisna_gorev": "Acil"}]
    )
    assert not result["basarili"]


def test_role_exception_keeps_existing_pool_member_usable():
    """A day-scoped exception opens H7 while H10 still honors the pool."""
    person = SolverPersonel(id=1, ad="A", kisitli_gorev="Other")
    task = SolverGorev(id=1, ad="Acil", slot_idx=0, base_name="Acil")
    exception = [{"personel_id": 1, "gun": 1, "istisna_gorev": "Acil"}]
    result = ortak_plan_uret(
        gun_sayisi=1, gun_tipleri={1: "hici"},
        personeller=[person], gorevler=[task],
        gorev_havuzlari={"Acil": {1}}, ara_gun=0,
        kisitlama_istisnalari=exception,
    )
    assert result["basarili"], result["mesaj"]
    solved = NobetSolver(
        gun_sayisi=1, gun_tipleri={1: "hici"}, personeller=[person],
        gorevler=[task], gorev_havuzlari={"Acil": {1}},
        kisitlama_istisnalari=exception, ara_gun=0,
        hedefler=result["hedefler_map"],
        plan_kontrati=result["plan_kontrati"].to_dict(), max_sure_saniye=3,
    ).coz()
    assert solved.basarili, solved.mesaj


def test_hard_separate_allows_different_buildings():
    """Separate rules must not reject people assigned in distinct buildings."""
    people = [SolverPersonel(id=1, ad="A"), SolverPersonel(id=2, ad="B")]
    tasks = [
        SolverGorev(id=1, ad="G1", slot_idx=0, base_name="G1", bina_id="A"),
        SolverGorev(id=2, ad="G2", slot_idx=1, base_name="G2", bina_id="B"),
    ]
    rules = [SolverKural(tur="ayri", kisiler=[1, 2], politika="hard")]
    plan = ortak_plan_uret(
        gun_sayisi=1, gun_tipleri={1: "hici"}, personeller=people,
        gorevler=tasks, kurallar=rules, ara_gun=0,
    )
    assert plan["basarili"], plan["mesaj"]
    solved = NobetSolver(
        gun_sayisi=1, gun_tipleri={1: "hici"}, personeller=people,
        gorevler=tasks, kurallar=rules, ara_gun=0,
        hedefler=plan["hedefler_map"],
        plan_kontrati=plan["plan_kontrati"].to_dict(), max_sure_saniye=3,
    ).coz()
    assert solved.basarili, solved.mesaj


def test_rest_exception_chain_does_not_ignore_an_unapproved_pair():
    calculator = HedefHesaplayici(
        gun_sayisi=3, gun_tipleri={1: "hici", 2: "hici", 3: "hici"},
        personeller=[SolverPersonel(id=1, ad="A")],
        gorevler=[SolverGorev(id=1, ad="Acil", slot_idx=0, base_name="Acil")],
        ara_gun=2, aragun_istisnalari=[
            {"personel_id": 1, "gun1": 1, "gun2": 2},
            {"personel_id": 1, "gun1": 2, "gun2": 3},
        ],
    )
    assert calculator._max_assignable_with_ara_gun([1, 2, 3], personel_id=1) == 2
    assert calculator._max_assignable_with_ara_gun(
        [1, 2, 3], zorunlu_gunler=[1, 2, 3], personel_id=1
    ) == -1


def test_hard_separate_targets_are_feasible_in_final_solver():
    """Target balancing must not create a hard-ayri dead end."""
    people = [SolverPersonel(id=i, ad=chr(64 + i)) for i in range(1, 5)]
    tasks = [
        SolverGorev(id=1, ad="G1", slot_idx=0, base_name="G", bina_id="ANA"),
        SolverGorev(id=2, ad="G2", slot_idx=1, base_name="G", bina_id="ANA"),
    ]
    rules = [SolverKural(tur="ayri", kisiler=[1, 2], politika="hard")]
    kwargs = dict(
        gun_sayisi=3, gun_tipleri={1: "hici", 2: "hici", 3: "hici"},
        personeller=people, gorevler=tasks, kurallar=rules, ara_gun=0,
        kilitli_hedefler={4: {"hici": 0}},
    )
    plan = ortak_plan_uret(**kwargs)
    assert plan["basarili"], plan["mesaj"]
    assert plan["hedefler_map"][4]["hedef_toplam"] == 0

    result = NobetSolver(
        **{k: v for k, v in kwargs.items() if k not in {"kilitli_hedefler"}},
        hedefler=plan["hedefler_map"],
        plan_kontrati=plan["plan_kontrati"].to_dict(),
        max_sure_saniye=10,
    ).coz()
    assert result.basarili, result.mesaj
    assert len(result.atamalar) == 6


if __name__ == "__main__":
    tests = [value for name, value in globals().copy().items() if name.startswith("test_")]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"{len(tests)}/{len(tests)} passed")
