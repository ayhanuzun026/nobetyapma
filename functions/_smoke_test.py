"""Gecici smoke test — NobetSolver'in Faz 1 duzeltmeleri sonrasi calistigini dogrular.
Firebase gerektirmez; dogrudan NobetSolver.coz() cagirir. Calistir: venv/Scripts/python _smoke_test.py
"""
from solver_models import SolverPersonel, SolverGorev
from ortools_solver import NobetSolver
from solve_strategy import solve_with_diagnostics, _doluluk_raporu_uret


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


def senaryo_unsat_core_ara_gun():
    """Faz 6A: plan hard hedefi ile ara gun kisiti cakisirsa core H4 + S3 icermeli."""
    gun_sayisi = 2
    gun_tipleri = {1: 'hici', 2: 'hici'}
    gorevler = [SolverGorev(id=0, ad='Acil', slot_idx=0, base_name='Acil',
                            exclusive=False, ayri_bina=False)]
    personeller = [SolverPersonel(id=1, ad='TekKisi', mazeret_gunleri=set())]
    hedefler = {
        1: {
            'hedef_toplam': 2,
            'hedef_tipler': {'hici': 2, 'prs': 0, 'cum': 0, 'cmt': 0, 'pzr': 0},
        }
    }
    return gun_sayisi, gun_tipleri, personeller, gorevler, hedefler


def senaryo_doluluk_gevsetme():
    """Aşama 1B: Tek kişi + 4 gün + ara_gün=2 -> başta boş slot kalır (H4 gap>ara_gün),
    doluluk geçişi ara_gün'ü kademeli düşürüp (0'da ardışık serbest) tümünü doldurmalı."""
    gun_sayisi = 4
    gun_tipleri = {g: 'hici' for g in range(1, gun_sayisi + 1)}
    gorevler = [SolverGorev(id=0, ad='Acil', slot_idx=0, base_name='Acil',
                            exclusive=False, ayri_bina=False)]
    personeller = [SolverPersonel(id=1, ad='TekKisi', mazeret_gunleri=set())]
    hedefler = {
        1: {
            'hedef_toplam': 4,
            'hedef_tipler': {'hici': 4, 'prs': 0, 'cum': 0, 'cmt': 0, 'pzr': 0},
        }
    }
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

    gun_sayisi, gun_tipleri, personeller, gorevler, hedefler = senaryo_unsat_core_ara_gun()
    solver = NobetSolver(
        gun_sayisi=gun_sayisi, gun_tipleri=gun_tipleri,
        personeller=personeller, gorevler=gorevler,
        kurallar=[], gorev_havuzlari={},
        kisitlama_istisnalari=[], birlikte_istisnalari=[],
        aragun_istisnalari=[], manuel_atamalar=[], hedefler=hedefler,
        ara_gun=1, max_sure_saniye=5,
        ignore_manual_conflicts=False,
        plan_kontrati={'uygulama': {'yetkili': True, 'toplam_hard': True}},
    )
    s3 = solver.coz()
    core = solver.diagnose_with_unsat_core(max_sure_saniye=5)
    core_groups = [g.get('group') for g in core.get('core_groups', [])]
    print("\n=== Senaryo 3: unsat-core ara gun ===")
    print(f"  basarili={s3.basarili} status={(s3.istatistikler or {}).get('status')} core={core_groups}")
    assert not s3.basarili, "SENARYO 3 BASARILI DONDU - test infeasible olmali!"
    assert 'H4_ARA_GUN' in core_groups, f"Unsat core H4_ARA_GUN icermiyor: {core_groups}"
    assert 'S3_TOPLAM_HEDEF_PLAN' in core_groups, f"Unsat core S3_TOPLAM_HEDEF_PLAN icermiyor: {core_groups}"

    # === Senaryo 4A: Onaysiz doluluk gevsetmesi uygulanmaz ===
    gun_sayisi, gun_tipleri, personeller, gorevler, hedefler = senaryo_doluluk_gevsetme()
    s4a, gev4a, tesh4a, kul_ara4a = solve_with_diagnostics(
        gun_sayisi=gun_sayisi, gun_tipleri=gun_tipleri,
        personeller=personeller, gorevler=gorevler,
        kurallar=[], gorev_havuzlari={},
        kisitlama_istisnalari=[], birlikte_istisnalari=[],
        aragun_istisnalari=[], manuel_atamalar=[], hedefler=hedefler,
        ara_gun=2, max_sure=20, yil=2025, ay=1, resmi_tatiller={}, data={},
    )
    ist4a = s4a.istatistikler or {}
    rapor4a = ist4a.get('doluluk_raporu') or {}
    onay_onerileri4a = (tesh4a or {}).get('onay_bekleyen_oneriler') or []
    print("\n=== Senaryo 4A: onaysiz doluluk gevsetmesi ===")
    print(f"  basarili={s4a.basarili} atama={len(s4a.atamalar)} bos_slot={rapor4a.get('bos_slot')} "
          f"kullanilan_ara_gun={kul_ara4a} gevsetme_denendi={rapor4a.get('gevsetme_denendi')}")
    print(f"  oneri: {rapor4a.get('oneri')}")
    assert s4a.basarili, "SENARYO 4A BASARISIZ — kismi feasible sonuc bekleniyordu!"
    assert kul_ara4a == 2, f"Onaysiz ara gun degismemeliydi: {kul_ara4a}"
    assert rapor4a.get('gevsetme_denendi') is False, rapor4a
    assert rapor4a.get('bos_slot', 0) > 0, rapor4a
    assert 'doluluk_ara_gun_gevsetildi' not in gev4a, gev4a
    assert any(o.get('aksiyon') == 'ara_gun_azalt' for o in onay_onerileri4a), tesh4a

    # === Senaryo 4B: Acik otomatik onay ile doluluk gevsetmesi ===
    s4b, gev4b, tesh4b, kul_ara4b = solve_with_diagnostics(
        gun_sayisi=gun_sayisi, gun_tipleri=gun_tipleri,
        personeller=personeller, gorevler=gorevler,
        kurallar=[], gorev_havuzlari={},
        kisitlama_istisnalari=[], birlikte_istisnalari=[],
        aragun_istisnalari=[], manuel_atamalar=[], hedefler=hedefler,
        ara_gun=2, max_sure=20, yil=2025, ay=1, resmi_tatiller={},
        data={'tamirPolitikasi': {'araGunAzaltma': 'otomatik'}},
    )
    ist4b = s4b.istatistikler or {}
    rapor4b = ist4b.get('doluluk_raporu') or {}
    print("\n=== Senaryo 4B: onayli doluluk gevsetmesi ===")
    print(f"  basarili={s4b.basarili} atama={len(s4b.atamalar)} bos_slot={rapor4b.get('bos_slot')} "
          f"kullanilan_ara_gun={kul_ara4b} gevsetme_denendi={rapor4b.get('gevsetme_denendi')}")
    print(f"  oneri: {rapor4b.get('oneri')}")
    assert s4b.basarili, "SENARYO 4B BASARISIZ — feasible olmaliydi!"
    assert rapor4b.get('gevsetme_denendi') is True, rapor4b
    assert rapor4b.get('bos_slot', 99) == 0, rapor4b
    assert len(s4b.atamalar) == 4, f"4 slot dolmaliydi, atama={len(s4b.atamalar)}"
    assert kul_ara4b == 0, f"Tam doluluk icin ara gun 0 bekleniyordu: {kul_ara4b}"
    assert gev4b.get('doluluk_ara_gun_gevsetildi') is True, gev4b
    assert not (tesh4b or {}).get('onay_bekleyen_oneriler'), tesh4b

    # === Birim: _doluluk_raporu_uret bos slot dalinda oneri metni ureti mi ===
    class _SahteSonuc:
        basarili = True
        istatistikler = {'toplam_slot': 10, 'bos_slot_sayisi': 3, 'doluluk_yuzde': 70.0}
    rapor_bos = _doluluk_raporu_uret(_SahteSonuc(), bos_slot=3, gevsetme_denendi=True)
    assert rapor_bos['bos_slot'] == 3, rapor_bos
    assert 'slot boş kaldı' in rapor_bos['oneri'], f"Oneri metni beklenen degil: {rapor_bos['oneri']}"
    rapor_dolu = _doluluk_raporu_uret(_SahteSonuc(), bos_slot=0, gevsetme_denendi=False)
    assert rapor_dolu['oneri'] == "Takvim tam dolu.", rapor_dolu['oneri']

    print("\n*** TUM SMOKE TESTLER GECTI ***")
