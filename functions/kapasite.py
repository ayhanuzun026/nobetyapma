"""
Kapasite hesaplama — Personel müsaitlik ve slot kapasitesi analizi.
"""

from typing import Dict, List, Optional, Set, Tuple

from utils import GUN_TIPLERI, find_matching_id
from solver_models import SolverAtama, SolverGorev, SolverKural, SolverPersonel


def _fizibilite_sonucu(
    durum: str,
    *,
    kod: Optional[str] = None,
    mesaj: Optional[str] = None,
    oneri: Optional[str] = None,
    detay: Optional[Dict] = None,
    solver: Optional[Dict] = None,
) -> Dict:
    neden = None
    if kod or mesaj:
        neden = {'kod': kod or durum, 'mesaj': mesaj or ''}
        if detay:
            neden['detay'] = detay
    return {
        'durum': durum,
        'neden': neden,
        'oneri': oneri,
        'solver': solver or {},
    }


def _tam_doluluk_fizibilite_kontrolu(
    gun_sayisi: int,
    gun_tipleri: Dict[int, str],
    personeller: List[SolverPersonel],
    gorevler: List[SolverGorev],
    kurallar: List[SolverKural],
    ara_gun: int,
    manuel_atamalar: List[SolverAtama],
    gorev_havuzlari: Dict[str, Set[int]],
    kisitlama_istisnalari: List[Dict],
    birlikte_istisnalari: List[Dict],
    aragun_istisnalari: List[Dict],
    kurum_profili: str,
    max_sure_saniye: int,
) -> Dict:
    from ortools_solver import NobetSolver

    hedefler = {}
    for personel in personeller:
        ust_sinir = gun_sayisi
        if personel.max_nobet is not None:
            ust_sinir = min(ust_sinir, max(0, int(personel.max_nobet)))
        hedefler[personel.id] = {
            'hedef_toplam': ust_sinir,
            'hedef_tipler': {},
        }

    solver = NobetSolver(
        gun_sayisi=gun_sayisi,
        gun_tipleri=gun_tipleri,
        personeller=personeller,
        gorevler=gorevler,
        kurallar=kurallar,
        gorev_havuzlari=gorev_havuzlari,
        kisitlama_istisnalari=kisitlama_istisnalari,
        birlikte_istisnalari=birlikte_istisnalari,
        aragun_istisnalari=aragun_istisnalari,
        manuel_atamalar=manuel_atamalar,
        hedefler=hedefler,
        ara_gun=ara_gun,
        max_sure_saniye=max_sure_saniye,
        leksikografik=False,
        kurum_profili=kurum_profili,
    )
    sonuc = solver.tam_doluluk_fizibilitesi(max_sure_saniye=max_sure_saniye)
    istatistikler = sonuc.istatistikler or {}
    solver_durum = str(istatistikler.get('status') or '').upper()
    solver_bilgi = {
        'status': solver_durum or ('FEASIBLE' if sonuc.basarili else 'UNKNOWN'),
        'sure_ms': sonuc.sure_ms,
        'toplam_slot': gun_sayisi * len(gorevler),
        'feasibility_debug': istatistikler.get('feasibility_debug') or {},
        'manual_conflicts': istatistikler.get('manual_conflicts') or [],
    }

    if sonuc.basarili and int(istatistikler.get('bos_slot_sayisi', 0) or 0) == 0:
        return _fizibilite_sonucu('FEASIBLE', solver=solver_bilgi)

    if solver_durum in {'UNKNOWN', 'MODEL_INVALID'}:
        return _fizibilite_sonucu(
            'UNKNOWN',
            kod='MODEL_GECERSIZ' if solver_durum == 'MODEL_INVALID' else 'FIZIBILITE_BELIRLENEMEDI',
            mesaj=(
                'Tam çizelge modeli geçersiz oluşturuldu.'
                if solver_durum == 'MODEL_INVALID'
                else 'Tam çizelge modeli süre sınırı içinde kesin karar veremedi.'
            ),
            oneri='Kontrolü yeniden çalıştırın veya sert kısıtları sadeleştirin.',
            solver=solver_bilgi,
        )

    if solver_durum == 'MANUAL_CONFLICT':
        ilk_cakisma = (solver_bilgi['manual_conflicts'] or [{}])[0]
        return _fizibilite_sonucu(
            'INFEASIBLE',
            kod='MANUEL_ATAMA_CAKISMASI',
            mesaj=ilk_cakisma.get('mesaj') or sonuc.mesaj,
            oneri='Çakışan manuel atamaları düzeltin.',
            detay={'manual_conflicts': solver_bilgi['manual_conflicts'][:20]},
            solver=solver_bilgi,
        )

    return _fizibilite_sonucu(
        'INFEASIBLE',
        kod='TAM_CIZELGE_KISIT_CAKISMASI',
        mesaj='Görev, havuz, mazeret, ara gün, manuel ve personel kurallarıyla tüm slotlar doldurulamıyor.',
        oneri='Riskli günleri, görev havuzlarını ve hard birlikte/ayrı kurallarını gözden geçirin.',
        detay={'feasibility_debug': solver_bilgi['feasibility_debug']},
        solver=solver_bilgi,
    )


def gun_bazli_fizibilite_kontrolu(
    gun_sayisi: int,
    personeller: List[SolverPersonel],
    slot_sayisi: int,
    ara_gun: int = 2,
    manuel_atamalar: Optional[List[SolverAtama]] = None,
    birlikte_kurallar: Optional[List[SolverKural]] = None,
    birlikte_istisnalari: Optional[List[Dict]] = None,
    aragun_istisnalari: Optional[List[Dict]] = None,
    max_sure_saniye: int = 5,
) -> Dict:
    """Temel kişi-gün kısıtları için kesin CP-SAT fizibilite denetimi."""
    manuel_atamalar = list(manuel_atamalar or [])
    birlikte_kurallar = list(birlikte_kurallar or [])
    birlikte_istisnalari = list(birlikte_istisnalari or [])
    aragun_istisnalari = list(aragun_istisnalari or [])
    ara_gun = max(0, int(ara_gun or 0))
    slot_sayisi = max(0, int(slot_sayisi or 0))
    gunler = list(range(1, int(gun_sayisi or 0) + 1))
    personel_map = {p.id: p for p in personeller}
    pids = list(personel_map.keys())

    birlikte_istisna_set = set()
    for raw in birlikte_istisnalari:
        pid = find_matching_id(raw.get('personel_id'), pids)
        gun = int(raw.get('gun', 0) or 0)
        if pid is not None and gun in gunler:
            birlikte_istisna_set.add((pid, gun))

    aragun_istisna_set = set()
    aragun_istisna_map: Dict[int, Set[Tuple[int, int]]] = {}
    for raw in aragun_istisnalari:
        pid = find_matching_id(raw.get('personel_id'), pids)
        gun1 = int(raw.get('gun1', 0) or 0)
        gun2 = int(raw.get('gun2', 0) or 0)
        if pid is not None and gun1 in gunler and gun2 in gunler:
            pair = (min(gun1, gun2), max(gun1, gun2))
            aragun_istisna_set.add((pid, *pair))
            aragun_istisna_map.setdefault(pid, set()).add(pair)

    if slot_sayisi > len(pids):
        return _fizibilite_sonucu(
            'INFEASIBLE',
            kod='PERSONEL_YETERSIZ',
            mesaj='Günlük görev sayısı mevcut personel sayısını aşıyor.',
            oneri='Slot sayısını azaltın veya personel ekleyin.',
            detay={'slot_sayisi': slot_sayisi, 'personel_sayisi': len(pids)},
        )

    manuel_gunler: Dict[int, Set[int]] = {pid: set() for pid in pids}
    manuel_sayilari: Dict[Tuple[int, int], int] = {}
    manuel_slotlar: Dict[Tuple[int, int], List[int]] = {}
    onayli_mazeret_gunleri: Set[Tuple[int, int]] = set()
    for atama in manuel_atamalar:
        pid = find_matching_id(getattr(atama, 'personel_id', None), pids)
        gun = int(getattr(atama, 'gun', 0) or 0)
        if pid is None or gun not in gunler:
            continue
        kisi_gun = (pid, gun)
        manuel_sayilari[kisi_gun] = manuel_sayilari.get(kisi_gun, 0) + 1
        manuel_gunler[pid].add(gun)
        slot_idx = int(getattr(atama, 'slot_idx', -1) or 0)
        manuel_slotlar.setdefault((gun, slot_idx), []).append(pid)
        if bool(getattr(atama, 'mazeret_onayli', False)):
            onayli_mazeret_gunleri.add(kisi_gun)

    for (pid, gun), adet in manuel_sayilari.items():
        if adet > 1:
            return _fizibilite_sonucu(
                'INFEASIBLE',
                kod='MANUEL_KISI_GUN_CAKISMASI',
                mesaj='Aynı personele aynı gün birden fazla manuel görev atanmış.',
                oneri='Personelin aynı gündeki fazla manuel atamasını kaldırın.',
                detay={'personel_id': pid, 'gun': gun, 'atama_sayisi': adet},
            )

    for (gun, slot_idx), atanan_ids in manuel_slotlar.items():
        if len(atanan_ids) > 1:
            return _fizibilite_sonucu(
                'INFEASIBLE',
                kod='MANUEL_SLOT_CAKISMASI',
                mesaj='Aynı gün ve görev slotuna birden fazla manuel personel atanmış.',
                oneri='Çakışan manuel atamalardan yalnız birini bırakın.',
                detay={'gun': gun, 'slot_idx': slot_idx, 'personel_ids': atanan_ids},
            )

    for pid, manuel_set in manuel_gunler.items():
        personel = personel_map[pid]
        for gun in sorted(manuel_set):
            if gun in personel.mazeret_gunleri and (pid, gun) not in onayli_mazeret_gunleri:
                return _fizibilite_sonucu(
                    'INFEASIBLE',
                    kod='MANUEL_MAZERET_CAKISMASI',
                    mesaj='Manuel atama, onaylanmamış mazeret günüyle çakışıyor.',
                    oneri='Manuel atamayı kaldırın veya mazeret istisnasını onaylayın.',
                    detay={'personel_id': pid, 'gun': gun},
                )
        sirali = sorted(manuel_set)
        for idx, gun1 in enumerate(sirali):
            for gun2 in sirali[idx + 1:]:
                if gun2 - gun1 > ara_gun:
                    break
                if (pid, gun1, gun2) in aragun_istisna_set:
                    continue
                return _fizibilite_sonucu(
                    'INFEASIBLE',
                    kod='MANUEL_ARA_GUN_CAKISMASI',
                    mesaj='Aynı personelin manuel atamaları ara gün kuralını ihlal ediyor.',
                    oneri='Manuel atamalardan birini taşıyın veya ara gün değerini azaltın.',
                    detay={'personel_id': pid, 'gunler': [gun1, gun2], 'ara_gun': ara_gun},
                )

    def musait_mi(pid: int, gun: int) -> bool:
        return (
            gun not in personel_map[pid].mazeret_gunleri
            or (pid, gun) in onayli_mazeret_gunleri
        )

    for gun in gunler:
        musait_ids = [pid for pid in pids if musait_mi(pid, gun)]
        if len(musait_ids) < slot_sayisi:
            return _fizibilite_sonucu(
                'INFEASIBLE',
                kod='GUNLUK_MAZERET_KAPASITESI',
                mesaj='Bir günde görevleri dolduracak kadar müsait personel yok.',
                oneri='Bu gündeki mazeretleri gözden geçirin, personel ekleyin veya slot sayısını azaltın.',
                detay={'gun': gun, 'gereken': slot_sayisi, 'musait': len(musait_ids)},
            )

    hard_gruplar: List[Dict] = []
    for kural in birlikte_kurallar:
        if getattr(kural, 'tur', None) != 'birlikte':
            continue
        politika = str(getattr(kural, 'politika', 'kullanici_onayli') or 'kullanici_onayli').strip().lower()
        if politika == 'soft' and not bool(getattr(kural, 'asla_gevsetme', False)):
            continue
        grup = []
        for raw_pid in getattr(kural, 'kisiler', []) or []:
            pid = find_matching_id(raw_pid, pids)
            if pid is not None and pid not in grup:
                grup.append(pid)
        if len(grup) >= 2:
            hard_gruplar.append({
                'kisiler': grup,
                'istisna_izinli': (
                    politika == 'kullanici_onayli'
                    and not bool(getattr(kural, 'asla_gevsetme', False))
                ),
            })

    for grup_bilgi in hard_gruplar:
        grup = grup_bilgi['kisiler']
        for gun in gunler:
            for idx, pid1 in enumerate(grup):
                for pid2 in grup[idx + 1:]:
                    if grup_bilgi['istisna_izinli'] and (
                        (pid1, gun) in birlikte_istisna_set
                        or (pid2, gun) in birlikte_istisna_set
                    ):
                        continue
                    manuel_pid = pid1 if gun in manuel_gunler[pid1] else (
                        pid2 if gun in manuel_gunler[pid2] else None
                    )
                    diger_pid = pid2 if manuel_pid == pid1 else pid1
                    if manuel_pid is not None and not musait_mi(diger_pid, gun):
                        return _fizibilite_sonucu(
                            'INFEASIBLE',
                            kod='BIRLIKTE_MANUEL_MAZERET_CAKISMASI',
                            mesaj='Hard birlikte grubunda manuel atama ile mazeret çakışıyor.',
                            oneri='Manuel günü değiştirin, ortak mazereti kaldırın veya birlikte kuralını soft yapın.',
                            detay={'gun': gun, 'manuel_personel_id': manuel_pid, 'musait_olmayan': diger_pid},
                        )
            if (
                not grup_bilgi['istisna_izinli']
                and len(grup) > slot_sayisi
                and any(gun in manuel_gunler[pid] for pid in grup)
            ):
                return _fizibilite_sonucu(
                    'INFEASIBLE',
                    kod='BIRLIKTE_GRUP_SLOT_CAKISMASI',
                    mesaj='Manuel atamalı hard birlikte grubu günlük slot sayısından büyük.',
                    oneri='Birlikte grubunu küçültün, kuralı soft yapın veya slot sayısını artırın.',
                    detay={'gun': gun, 'grup_boyutu': len(grup), 'slot_sayisi': slot_sayisi},
                )

    from ortools.sat.python import cp_model as cp

    model = cp.CpModel()
    kisi_gun = {
        (pid, gun): model.NewBoolVar(f'kap_kisi_gun_{pid}_{gun}')
        for pid in pids
        for gun in gunler
    }
    assumptions = {}

    def varsayim(grup: str):
        if grup not in assumptions:
            literal = model.NewBoolVar(f'kap_assume_{grup.lower()}')
            model.AddAssumption(literal)
            assumptions[grup] = literal
        return assumptions[grup]

    for pid in pids:
        for gun in gunler:
            if not musait_mi(pid, gun):
                model.Add(kisi_gun[pid, gun] == 0).OnlyEnforceIf(varsayim('MAZERET'))
            if gun in manuel_gunler[pid]:
                model.Add(kisi_gun[pid, gun] == 1).OnlyEnforceIf(varsayim('MANUEL_ATAMA'))

        if ara_gun > 0:
            for idx, gun1 in enumerate(gunler):
                for gun2 in gunler[idx + 1:]:
                    if gun2 - gun1 > ara_gun:
                        break
                    if (pid, gun1, gun2) in aragun_istisna_set:
                        continue
                    model.Add(
                        kisi_gun[pid, gun1] + kisi_gun[pid, gun2] <= 1
                    ).OnlyEnforceIf(varsayim('ARA_GUN'))

    for gun in gunler:
        model.Add(
            sum(kisi_gun[pid, gun] for pid in pids) == slot_sayisi
        ).OnlyEnforceIf(varsayim('GUNLUK_DOLULUK'))

    for grup_bilgi in hard_gruplar:
        grup = grup_bilgi['kisiler']
        for idx, referans_id in enumerate(grup):
            for diger_id in grup[idx + 1:]:
                for gun in gunler:
                    if grup_bilgi['istisna_izinli'] and (
                        (referans_id, gun) in birlikte_istisna_set
                        or (diger_id, gun) in birlikte_istisna_set
                    ):
                        continue
                    model.Add(
                        kisi_gun[referans_id, gun] == kisi_gun[diger_id, gun]
                    ).OnlyEnforceIf(varsayim('HARD_BIRLIKTE'))

    def ara_gun_pencere_aciklari() -> List[Dict]:
        if ara_gun <= 0:
            return []

        aciklar = []
        for baslangic in gunler:
            for bitis in range(baslangic, gun_sayisi + 1):
                ust_kapasite = 0
                for pid in pids:
                    uygun = [
                        gun for gun in range(baslangic, bitis + 1)
                        if musait_mi(pid, gun)
                    ]
                    secilen = []
                    for gun in uygun:
                        if not secilen or gun - secilen[-1] > ara_gun:
                            secilen.append(gun)
                    istisna_sayisi = sum(
                        1 for gun1, gun2 in aragun_istisna_map.get(pid, set())
                        if gun1 in uygun and gun2 in uygun
                    )
                    ust_kapasite += min(len(uygun), len(secilen) + istisna_sayisi)
                pencere_gun = bitis - baslangic + 1
                talep = pencere_gun * slot_sayisi
                if ust_kapasite < talep:
                    aciklar.append({
                        'baslangic': baslangic,
                        'bitis': bitis,
                        'gun_sayisi': pencere_gun,
                        'talep': talep,
                        'ust_kapasite': ust_kapasite,
                        'eksik': talep - ust_kapasite,
                    })
        aciklar.sort(key=lambda item: (-item['eksik'], -item['gun_sayisi'], item['baslangic']))
        return aciklar[:10]

    solver = cp.CpSolver()
    solver.parameters.max_time_in_seconds = max(1, int(max_sure_saniye or 1))
    solver.parameters.num_search_workers = 1
    status = solver.Solve(model)
    solver_bilgi = {
        'status': solver.StatusName(status),
        'sure_saniye': round(solver.WallTime(), 3),
        'degisken_sayisi': len(kisi_gun),
    }
    if status in (cp.OPTIMAL, cp.FEASIBLE):
        return _fizibilite_sonucu('FEASIBLE', solver=solver_bilgi)

    if status != cp.INFEASIBLE:
        return _fizibilite_sonucu(
            'UNKNOWN',
            kod='FIZIBILITE_BELIRLENEMEDI',
            mesaj='CP-SAT süre sınırı içinde kesin fizibilite kararı veremedi.',
            oneri='Kontrol süresini artırın veya kısıtları sadeleştirip yeniden deneyin.',
            solver=solver_bilgi,
        )

    core_fn = getattr(solver, 'sufficient_assumptions_for_infeasibility', None)
    if core_fn is None:
        core_fn = getattr(solver, 'SufficientAssumptionsForInfeasibility', None)
    core_indexes = set(core_fn() if core_fn is not None else [])
    core_gruplari = [
        grup for grup, literal in assumptions.items()
        if literal.Index() in core_indexes
    ]
    etkin_gruplar = [g for g in core_gruplari if g != 'GUNLUK_DOLULUK']
    pencere_aciklari = ara_gun_pencere_aciklari()

    neden_map = {
        'MAZERET': (
            'MAZERET_KAPASITE_CAKISMASI',
            'Mazeret dağılımı günlük görevlerin tamamını doldurmaya izin vermiyor.',
        ),
        'MANUEL_ATAMA': (
            'MANUEL_ATAMA_CAKISMASI',
            'Manuel atamalar diğer zorunlu günlük kısıtlarla çakışıyor.',
        ),
        'ARA_GUN': (
            'ARA_GUN_KAPASITE_CAKISMASI',
            'Ara gün kuralı günlük görevlerin tamamını doldurmaya izin vermiyor.',
        ),
        'HARD_BIRLIKTE': (
            'HARD_BIRLIKTE_CAKISMASI',
            'Hard birlikte kuralı diğer günlük kısıtlarla çakışıyor.',
        ),
    }
    if pencere_aciklari:
        ilk_acik = pencere_aciklari[0]
        kod = 'ARA_GUN_PENCERE_KAPASITE_ACIGI'
        mesaj = (
            f"{ilk_acik['baslangic']}-{ilk_acik['bitis']} gün aralığında "
            f"talep {ilk_acik['talep']}, üst kapasite {ilk_acik['ust_kapasite']}; "
            f"{ilk_acik['eksik']} görev açıkta kalıyor."
        )
    elif len(etkin_gruplar) == 1:
        kod, mesaj = neden_map.get(etkin_gruplar[0], (
            'GUNLUK_FIZIBILITE_CAKISMASI',
            'Gün bazlı zorunlu kısıtlar birlikte çözüm bırakmıyor.',
        ))
    else:
        kod = 'GUNLUK_FIZIBILITE_CAKISMASI'
        mesaj = 'Gün bazlı zorunlu kısıtların birleşimi çözüm bırakmıyor.'

    oneriler = []
    if 'MAZERET' in core_gruplari:
        oneriler.append('riskli günlerdeki mazeretleri gözden geçirin')
    if 'MANUEL_ATAMA' in core_gruplari:
        oneriler.append('manuel atamaları farklı günlere taşıyın')
    if 'ARA_GUN' in core_gruplari:
        oneriler.append('ara gün değerini azaltın')
    if 'HARD_BIRLIKTE' in core_gruplari:
        oneriler.append('birlikte kuralını veya grup üyelerinin ortak müsaitliğini gözden geçirin')
    if not oneriler:
        oneriler.append('günlük slot sayısını ve personel uygunluğunu gözden geçirin')

    return _fizibilite_sonucu(
        'INFEASIBLE',
        kod=kod,
        mesaj=mesaj,
        oneri='; '.join(oneriler).capitalize() + '.',
        detay={
            'cekirdek_kisitlar': core_gruplari,
            'ara_gun_pencere_aciklari': pencere_aciklari,
        },
        solver=solver_bilgi,
    )


def kapasite_hesapla(gun_sayisi: int, gun_tipleri: Dict[int, str],
                     personeller: List[SolverPersonel], slot_sayisi: int,
                     ara_gun: int = 2,
                     manuel_atamalar: Optional[List[SolverAtama]] = None,
                     birlikte_kurallar: Optional[List[SolverKural]] = None,
                     birlikte_istisnalari: Optional[List[Dict]] = None,
                     aragun_istisnalari: Optional[List[Dict]] = None,
                     gorevler: Optional[List[SolverGorev]] = None,
                     kurallar: Optional[List[SolverKural]] = None,
                     gorev_havuzlari: Optional[Dict[str, Set[int]]] = None,
                     kisitlama_istisnalari: Optional[List[Dict]] = None,
                     kurum_profili: str = 'genel',
                     max_sure_saniye: int = 10) -> Dict:
    slot_sayisi = int(slot_sayisi or 0)
    ara_gun = int(ara_gun or 0)
    if slot_sayisi < 1:
        raise ValueError('slotSayisi en az 1 olmalı')
    if ara_gun < 0:
        raise ValueError('araGun negatif olamaz')

    if gorevler is None:
        gorevler = [
            SolverGorev(
                id=idx,
                ad=f'Nöbetçi {idx + 1}',
                slot_idx=idx,
                base_name='Nöbetçi',
            )
            for idx in range(slot_sayisi)
        ]
    else:
        gorevler = list(gorevler)
        slot_sayisi = len(gorevler)
    if slot_sayisi < 1:
        raise ValueError('En az bir görev tanımlanmalı')

    manuel_atamalar = list(manuel_atamalar or [])
    tum_kurallar = list(kurallar if kurallar is not None else (birlikte_kurallar or []))
    birlikte_kurallari = [kural for kural in tum_kurallar if kural.tur == 'birlikte']
    birlikte_istisnalari = list(birlikte_istisnalari or [])
    aragun_istisnalari = list(aragun_istisnalari or [])
    gorev_havuzlari = dict(gorev_havuzlari or {})
    kisitlama_istisnalari = list(kisitlama_istisnalari or [])

    tip_sayilari = {t: 0 for t in GUN_TIPLERI}
    for g, tip in gun_tipleri.items():
        if tip in tip_sayilari:
            tip_sayilari[tip] += 1

    tip_slotlari = {t: tip_sayilari[t] * slot_sayisi for t in GUN_TIPLERI}
    toplam_slot = sum(tip_slotlari.values())

    kapasite_listesi = []
    for p in personeller:
        musait = {t: 0 for t in GUN_TIPLERI}
        for g, tip in gun_tipleri.items():
            if g not in p.mazeret_gunleri:
                musait[tip] += 1
        p.musait_tipler = musait
        p.musait_gunler = {g for g in gun_tipleri.keys() if g not in p.mazeret_gunleri}
        kapasite_listesi.append({
            'id': p.id, 'ad': p.ad,
            'mazeret_sayisi': len(p.mazeret_gunleri),
            'musait_gunler': len(p.musait_gunler),
            'musait_tipler': musait
        })

    gun_bazli_fizibilite = gun_bazli_fizibilite_kontrolu(
        gun_sayisi=gun_sayisi,
        personeller=personeller,
        slot_sayisi=slot_sayisi,
        ara_gun=ara_gun,
        manuel_atamalar=manuel_atamalar,
        birlikte_kurallar=birlikte_kurallari,
        birlikte_istisnalari=birlikte_istisnalari,
        aragun_istisnalari=aragun_istisnalari,
    )

    tam_fizibilite = _tam_doluluk_fizibilite_kontrolu(
        gun_sayisi=gun_sayisi,
        gun_tipleri=gun_tipleri,
        personeller=personeller,
        gorevler=gorevler,
        kurallar=tum_kurallar,
        ara_gun=ara_gun,
        manuel_atamalar=manuel_atamalar,
        gorev_havuzlari=gorev_havuzlari,
        kisitlama_istisnalari=kisitlama_istisnalari,
        birlikte_istisnalari=birlikte_istisnalari,
        aragun_istisnalari=aragun_istisnalari,
        kurum_profili=kurum_profili,
        max_sure_saniye=max_sure_saniye,
    )

    if tam_fizibilite['durum'] == 'INFEASIBLE' and gun_bazli_fizibilite['durum'] == 'INFEASIBLE':
        gun_bazli_fizibilite['solver'] = tam_fizibilite.get('solver') or {}
        fizibilite = gun_bazli_fizibilite
    else:
        fizibilite = tam_fizibilite

    durum = fizibilite['durum']
    uygulanabilir = True if durum == 'FEASIBLE' else False if durum == 'INFEASIBLE' else None
    neden_mesaji = ((fizibilite.get('neden') or {}).get('mesaj') or '').strip()
    if durum == 'FEASIBLE':
        mesaj = 'Gün bazlı kapasite uygulanabilir.'
    elif durum == 'INFEASIBLE':
        mesaj = f"INFEASIBLE: {neden_mesaji or 'Gün bazlı zorunlu kısıtlar çözüm bırakmıyor.'}"
    else:
        mesaj = f"{durum}: {neden_mesaji or 'Fizibilite kesinleştirilemedi.'}"

    return {
        'durum': durum,
        'uygulanabilir': uygulanabilir,
        'mesaj': mesaj,
        'neden': fizibilite.get('neden'),
        'oneri': fizibilite.get('oneri'),
        'gun_sayisi': gun_sayisi,
        'tip_sayilari': tip_sayilari,
        'tip_slotlari': tip_slotlari,
        'toplam_slot': toplam_slot,
        'personel_sayisi': len(personeller),
        'kapasiteler': kapasite_listesi,
        'fizibilite': fizibilite,
    }
