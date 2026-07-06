"""Gecici smoke test — NobetSolver'in Faz 1 duzeltmeleri sonrasi calistigini dogrular.
Firebase gerektirmez; dogrudan NobetSolver.coz() cagirir. Calistir: venv/Scripts/python _smoke_test.py
"""
from solver_models import SolverPersonel, SolverGorev
from ortools_solver import NobetSolver


def senaryo_temel():
    """4 personel, 1 gorev, 7 hafta ici gun, hedef 2+2+2+1=7. Feasible olmali."""
    gun_sayisi = 7
    gun_tipleri = {g: 'hici' for g in range(1, gun_sayisi + 1)}
    gorevler = [SolverGorev(id=0, ad='Acil', slot_idx=0, base_name='Acil',
                            exclusive=False, ayri_bina=False)]
    personeller = [
        SolverPersonel(id=i + 1, ad=f'Kisi{i + 1}', mazeret_gunleri=set())
        for i in range(4)
    ]
    hedef_toplamlar = [2, 2, 2, 1]
    hedefler = {}
    for i, p in enumerate(personeller):
        t = hedef_toplamlar[i]
        hedefler[p.id] = {
            'hedef_toplam': t,
            'hedef_tipler': {'hici': t, 'prs': 0, 'cum': 0, 'cmt': 0, 'pzr': 0},
        }
    return gun_sayisi, gun_tipleri, personeller, gorevler, hedefler


def senaryo_eksik_hedef():
    """hedef_toplam=0 varsayilan tutarsizligi testi (Faz 1 Fix 1.2).
    Bir kisi hedefler sozlugunde YOK — eskiden S3 onu 3 sayip INFEASIBLE uretebiliyordu.
    """
    gun_sayisi = 7
    gun_tipleri = {g: 'hici' for g in range(1, gun_sayisi + 1)}
    gorevler = [SolverGorev(id=0, ad='Acil', slot_idx=0, base_name='Acil',
                            exclusive=False, ayri_bina=False)]
    personeller = [
        SolverPersonel(id=i + 1, ad=f'Kisi{i + 1}', mazeret_gunleri=set())
        for i in range(4)
    ]
    # 5. kisi hedefler'de YOK (eksik hedef) — eleme onu 0 saymali, S3 de 0 saymali
    personeller.append(SolverPersonel(id=99, ad='HedefiYok', mazeret_gunleri=set()))
    hedefler = {}
    for i in range(4):
        hedefler[i + 1] = {'hedef_toplam': 2 if i < 3 else 1,
                           'hedef_tipler': {'hici': 2 if i < 3 else 1}}
    # id=99 icin hedef YOK — bilincli
    return gun_sayisi, gun_tipleri, personeller, gorevler, hedefler


def coz(baslik, veri):
    gun_sayisi, gun_tipleri, personeller, gorevler, hedefler = veri
    solver = NobetSolver(
        gun_sayisi=gun_sayisi, gun_tipleri=gun_tipleri,
        personeller=personeller, gorevler=gorevler,
        kurallar=[], gorev_havuzlari={},
        kisitlama_istisnalari=[], birlikte_istisnalari=[],
        aragun_istisnalari=[], manuel_atamalar=[], hedefler=hedefler,
        ara_gun=2, max_sure_saniye=10,
        ignore_manual_conflicts=False, plan_kontrati=None,
    )
    sonuc = solver.coz()
    print(f"\n=== {baslik} ===")
    print(f"  basarili={sonuc.basarili}  atama={len(sonuc.atamalar)}  "
          f"status={(sonuc.istatistikler or {}).get('status')}")
    for a in sorted(sonuc.atamalar, key=lambda a: a['gun']):
        print(f"    gun {a['gun']} -> {a.get('personel_ad')}")
    return sonuc


if __name__ == '__main__':
    s1 = coz("Senaryo 1: temel feasible", senaryo_temel())
    assert s1.basarili, "SENARYO 1 BASARISIZ — solver bozulmus olabilir!"
    assert len(s1.atamalar) >= 6, f"Senaryo 1 cok az atama: {len(s1.atamalar)}"

    s2 = coz("Senaryo 2: eksik hedefli kisi (Fix 1.2)", senaryo_eksik_hedef())
    assert s2.basarili, "SENARYO 2 BASARISIZ — hedef_toplam varsayilan tutarsizligi!"
    # HedefiYok kisisine atama YAPILMAMALI (hedef 0)
    yok_atama = [a for a in s2.atamalar if a.get('personel_ad') == 'HedefiYok']
    assert len(yok_atama) == 0, f"HedefiYok kisisine atama yapildi: {yok_atama}"

    print("\n*** TUM SMOKE TESTLER GECTI ***")
