"""
Kapasite hesaplama — Personel müsaitlik ve slot kapasitesi analizi.
"""

import time
from copy import deepcopy
from typing import Any, Dict, List, Optional, Set, Tuple

from utils import GUN_TIPLERI, find_matching_id
from solver_models import SolverAtama, SolverGorev, SolverKural, SolverPersonel


def _kalan_tam_saniye(deadline: Optional[float]) -> int:
    """Bir sonraki solver geçişine ayrılabilecek tam saniyeyi döndürür."""
    if deadline is None:
        return 0
    return max(0, int(deadline - time.monotonic()))


def _pencere_kisi_ust_kapasitesi(
    pid: int,
    uygun_gunler: Set[int],
    baslangic: int,
    bitis: int,
    ara_gun: int,
    aragun_istisnalari: Set[Tuple[int, int]],
) -> int:
    """Bir kişinin penceredeki gerçek maksimum nöbet sayısını hesaplar.

    Yakın gün istisnaları birbirinden bağımsız ``+1`` değildir. Örneğin
    (1,2) ve (2,3) istisna iken (1,3) yasaksa maksimum 3 değil 2'dir.
    Aşağıdaki durum DP'si, son ``ara_gun`` içindeki seçimleri birlikte
    taşıyarak bu çakışmayı tam olarak çözer.

    Çok sıra dışı, çok büyük ara-gün/istisna örneklerinde durum sayısı
    patlarsa ``len(uygun)`` döner. Bu gevşek ama güvenli bir üst sınırdır;
    hiçbir zaman yanlış bir "kapasite açığı" kanıtı üretmez.
    """
    uygun = {
        int(gun) for gun in uygun_gunler
        if baslangic <= int(gun) <= bitis
    }
    if not uygun:
        return 0
    ara_gun = max(0, int(ara_gun or 0))
    if ara_gun == 0:
        return len(uygun)

    # state -> şimdiye kadarki en yüksek toplam seçim. State yalnız halen
    # yeni günle çatışabilecek yakın seçilmiş günleri taşır.
    durumlar: Dict[Tuple[int, ...], int] = {(): 0}
    max_durum = 50_000
    for gun in range(baslangic, bitis + 1):
        yeni: Dict[Tuple[int, ...], int] = {}
        for yakin_gunler, toplam in durumlar.items():
            etkin = tuple(g for g in yakin_gunler if gun - g <= ara_gun)
            onceki = yeni.get(etkin)
            if onceki is None or toplam > onceki:
                yeni[etkin] = toplam

            if gun not in uygun:
                continue
            secilebilir = all(
                (min(onceki_gun, gun), max(onceki_gun, gun)) in aragun_istisnalari
                for onceki_gun in etkin
            )
            if secilebilir:
                secili = etkin + (gun,)
                onceki = yeni.get(secili)
                if onceki is None or toplam + 1 > onceki:
                    yeni[secili] = toplam + 1

        durumlar = yeni
        if len(durumlar) > max_durum:
            return len(uygun)

    return max(durumlar.values(), default=0)


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


def _kapasite_hedefleri(
    gun_sayisi: int,
    personeller: List[SolverPersonel],
    kilitli_hedefler: Optional[Dict[Any, Dict[str, int]]] = None,
) -> Dict[Any, Dict]:
    """Kapasite modelinin üst sınırlarını ve açık hedef kilitlerini üretir.

    Otomatik plan hedefleri bu aşamada henüz üretilmemiştir. Buna karşılık
    kullanıcının açıkça kilitlediği gün-tipi hedefleri hedef modelinde hard
    olduğundan, aynı değerler gerçek kişi×gün×görev-slotu modelinde de hard
    eşitlik olarak işaretlenir. Böylece ön kontrolün kabul ettiği bir kilit daha
    sonra hedef modelinde sessizce INFEASIBLE üretemez.
    """
    kilitli_hedefler = kilitli_hedefler or {}
    hedefler = {}
    for personel in personeller:
        ust_sinir = gun_sayisi
        if personel.max_nobet is not None:
            ust_sinir = min(ust_sinir, max(0, int(personel.max_nobet)))
        hedef = {
            'hedef_toplam': ust_sinir,
            'hedef_tipler': {},
        }
        matched_kilit = find_matching_id(personel.id, kilitli_hedefler.keys())
        if matched_kilit is not None:
            raw_tipler = kilitli_hedefler.get(matched_kilit) or {}
            tipler = {}
            for tip in GUN_TIPLERI:
                try:
                    tipler[tip] = max(0, int(raw_tipler.get(tip, 0) or 0))
                except (TypeError, ValueError):
                    tipler[tip] = 0
            hedef.update({
                'hedef_tipler': tipler,
                'kapasite_kilitli_hedef': True,
                'kilitli_hedef_toplam': sum(tipler.values()),
                'kilitli_hedef_tipler': tipler,
            })
        hedefler[personel.id] = hedef
    return hedefler


def _kapasite_solver_olustur(
    *,
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
    kilitli_hedefler: Dict[Any, Dict[str, int]],
    kurum_profili: str,
    max_sure_saniye: int,
):
    from ortools_solver import NobetSolver

    return NobetSolver(
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
        hedefler=_kapasite_hedefleri(
            gun_sayisi,
            personeller,
            kilitli_hedefler=kilitli_hedefler,
        ),
        ara_gun=ara_gun,
        max_sure_saniye=max(1, int(max_sure_saniye or 1)),
        leksikografik=False,
        kurum_profili=kurum_profili,
    )


def _kismi_cozum_payload(sonuc: Any, toplam_slot: int) -> Dict:
    """Minimum-boşluk SolverSonuc'unu kararlı API sözleşmesine çevirir."""
    istatistikler = (getattr(sonuc, 'istatistikler', None) or {}) if sonuc is not None else {}
    status = str(
        istatistikler.get('minimum_bosluk_status')
        or istatistikler.get('status')
        or 'UNKNOWN'
    ).upper()
    atamalar = list(getattr(sonuc, 'atamalar', None) or []) if sonuc is not None else []
    bulunan = istatistikler.get('bulunan_bosluk')
    if bulunan is None and getattr(sonuc, 'basarili', False):
        bulunan = istatistikler.get('bos_slot_sayisi')
    try:
        bulunan = int(bulunan) if bulunan is not None else None
    except (TypeError, ValueError):
        bulunan = None

    doldurulan = None if bulunan is None else max(0, int(toplam_slot) - bulunan)
    optimum = (
        status == 'OPTIMAL'
        and bool(istatistikler.get('optimum_kanitlandi', False))
    )
    return {
        'solver_status': status,
        'optimum_kanitlandi': optimum,
        'kesinlik': (
            'KANITLANMIS_MINIMUM' if optimum
            else 'BULUNAN_EN_IYI' if status == 'FEASIBLE'
            else 'BELIRLENEMEDI'
        ),
        'doldurulan_slot': doldurulan,
        'toplam_slot': int(toplam_slot),
        'bos_slot': bulunan,
        'minimum_bosluk_alt_siniri': istatistikler.get('minimum_bosluk_alt_siniri'),
        'objective_bound': istatistikler.get('objective_bound'),
        'atamalar': atamalar,
        'bos_slot_detaylari': list(istatistikler.get('bos_slot_aciklamalari') or []),
        'takas_onerileri': list(istatistikler.get('takas_onerileri') or []),
        'sure_ms': int(getattr(sonuc, 'sure_ms', 0) or 0) if sonuc is not None else 0,
        'mesaj': str(getattr(sonuc, 'mesaj', '') or '') if sonuc is not None else '',
    }


def _what_if_adaylari(
    *,
    ara_gun: int,
    personeller: List[SolverPersonel],
    gorevler: List[SolverGorev],
    gorev_havuzlari: Dict[str, Set[int]],
    bos_slot_detaylari: List[Dict],
    manuel_atamalar: Optional[List[SolverAtama]] = None,
    manuel_cakismalar: Optional[List[Dict]] = None,
    manuel_cakisma: bool = False,
    limit: int = 2,
) -> List[Dict]:
    """Boş slotlara temas eden az sayıda, salt-okunur karşı-olgusal seçer."""
    adaylar: List[Dict] = []
    if ara_gun > 0 and not manuel_cakisma:
        adaylar.append({
            'id': f'aragun-{ara_gun}-{ara_gun - 1}',
            'tur': 'ara_gun_azalt',
            'baslik': f'Ara günü {ara_gun} → {ara_gun - 1} yap',
            'aciklama': 'Yalnız analiz kopyasında ara gün bir azaltılır.',
            'degisiklik': {'eski_ara_gun': ara_gun, 'yeni_ara_gun': ara_gun - 1},
            'degisiklik_sayisi': 1,
            'puan': 10_000,
        })

    gorev_map = {}
    kritik_roller = set()
    for gorev in gorevler:
        role = str(gorev.base_name or gorev.ad or '').strip()
        if role:
            gorev_map[role] = gorev
            if gorev.exclusive or bool(getattr(gorev, 'kritik', False)):
                kritik_roller.add(role)

    puanlar: Dict[Tuple, int] = {}
    kayitlar: Dict[Tuple, Dict] = {}
    bos_gunler = {
        int(detay.get('gun'))
        for detay in bos_slot_detaylari[:40]
        if str(detay.get('gun', '')).isdigit()
    }
    manuel_cakismalar = list(manuel_cakismalar or [])

    def manuel_kayit_cakisma_sayisi(atama: SolverAtama) -> int:
        """Kaydın taraf olduğu farklı hard çakışmaların sayısını döndürür."""
        pid = getattr(atama, 'personel_id', None)
        gun = int(getattr(atama, 'gun', 0) or 0)
        slot_idx = int(getattr(atama, 'slot_idx', -1) or 0)
        eslesen = 0
        for cakisma in manuel_cakismalar:
            kod = str(cakisma.get('code') or '').upper()
            if kod == 'AYNI_SLOT_CIFT_ATAMA':
                if gun == cakisma.get('gun') and slot_idx == cakisma.get('slot_idx'):
                    eslesen += 1
                continue
            if kod == 'ARA_GUN_IHLALI':
                if pid == cakisma.get('personel_id') and gun in {
                    cakisma.get('gun1'), cakisma.get('gun2')
                }:
                    eslesen += 1
                continue
            if kod == 'AYRI_KURALI_IHLALI':
                if gun == cakisma.get('gun') and pid in {
                    cakisma.get('personel1_id'), cakisma.get('personel2_id')
                }:
                    eslesen += 1
                continue

            cakisma_pid = cakisma.get('personel_id')
            if cakisma_pid is not None and pid != cakisma_pid:
                continue
            cakisma_gun = cakisma.get('gun')
            if cakisma_gun is not None and gun != cakisma_gun:
                continue
            cakisma_slot = cakisma.get('slot_idx')
            if cakisma_slot is not None and slot_idx != cakisma_slot:
                continue
            if cakisma_pid is not None or cakisma_gun is not None or cakisma_slot is not None:
                eslesen += 1
        return eslesen

    for index, atama in enumerate(manuel_atamalar or []):
        cakisma_kapsami = manuel_kayit_cakisma_sayisi(atama) if manuel_cakisma else 0
        if manuel_cakisma and cakisma_kapsami <= 0:
            continue
        gun = int(getattr(atama, 'gun', 0) or 0)
        yakinlik = max(
            [6 if bos_gun == gun else 3 if abs(bos_gun - gun) <= max(1, ara_gun) else 0
             for bos_gun in bos_gunler]
            or [0]
        )
        if not manuel_cakisma and yakinlik <= 0:
            continue
        pid = getattr(atama, 'personel_id', None)
        personel = next((p for p in personeller if p.id == pid), None)
        ad = personel.ad if personel is not None else str(pid)
        slot_idx = int(getattr(atama, 'slot_idx', -1))
        key = ('manuel_atama_kaldir', index)
        # En fazla iki tam-model what-if çözülebildiğinden, birden çok hard
        # çakışmanın ortak tarafı olan kayıt önce denenmelidir.
        puanlar[key] = (12 * cakisma_kapsami if manuel_cakisma else 0) + yakinlik
        kayitlar[key] = {
            'id': f'manuel-{index}-{pid}-{gun}-{slot_idx}',
            'tur': 'manuel_atama_kaldir',
            'baslik': f'{ad}: {gun}. gün manuel kilidini kaldırmayı dene',
            'aciklama': (
                'Yalnız analiz kopyasından bu tek manuel kayıt kaldırılır; '
                'kayıt otomatik taşınmaz veya silinmez.'
            ),
            'degisiklik': {
                'manuel_atama_index': index,
                'personel_id': pid,
                'gun': gun,
                'slot_idx': slot_idx,
            },
            'degisiklik_sayisi': 1,
        }

    for detay in bos_slot_detaylari[:40]:
        try:
            gun = int(detay.get('gun'))
        except (TypeError, ValueError):
            continue
        role = str(detay.get('gorev') or '').strip()
        if not role:
            continue

        for personel in personeller:
            pid = personel.id
            if gun in (personel.mazeret_gunleri or set()):
                key = ('mazeret_kaldir', pid, gun)
                puanlar[key] = puanlar.get(key, 0) + 4
                kayitlar[key] = {
                    'id': f'mazeret-{pid}-{gun}',
                    'tur': 'mazeret_kaldir',
                    'baslik': f'{gun}. günde {personel.ad} mazeretini düzelt',
                    'aciklama': (
                        f"{personel.ad} için {gun}. gün mazereti yalnız analiz "
                        'kopyasında kaldırılır.'
                    ),
                    'degisiklik': {'personel_id': pid, 'gun': gun},
                    'degisiklik_sayisi': 1,
                }

            yetkiler = set(personel.yetkili_gorevler or set())
            havuz = gorev_havuzlari.get(role)
            if havuz is not None and pid not in havuz:
                yetki_de_gerekli = bool(yetkiler and role not in yetkiler)
                key = ('havuz_ekle', pid, role, yetki_de_gerekli)
                puanlar[key] = puanlar.get(key, 0) + 3
                kayitlar[key] = {
                    'id': f'havuz-{pid}-{role}',
                    'tur': 'havuz_ekle',
                    'baslik': f'{personel.ad} kişisini {role} havuzuna ekle',
                    'aciklama': (
                        f"{personel.ad}, yalnız analiz kopyasında {role} havuzuna"
                        + (' ve yetki listesine eklenir.' if yetki_de_gerekli else ' eklenir.')
                    ),
                    'degisiklik': {
                        'personel_id': pid,
                        'gorev': role,
                        'yetki_de_ekle': yetki_de_gerekli,
                    },
                    'degisiklik_sayisi': 2 if yetki_de_gerekli else 1,
                }
            elif role not in yetkiler and (yetkiler or role in kritik_roller):
                key = ('yetki_ekle', pid, role)
                puanlar[key] = puanlar.get(key, 0) + 2
                kayitlar[key] = {
                    'id': f'yetki-{pid}-{role}',
                    'tur': 'yetki_ekle',
                    'baslik': f'{personel.ad} için {role} yetkisi ekle',
                    'aciklama': (
                        f"{role} yetkisi yalnız {personel.ad} analiz kopyasına eklenir."
                    ),
                    'degisiklik': {'personel_id': pid, 'gorev': role},
                    'degisiklik_sayisi': 1,
                }

    sirali = sorted(
        kayitlar.items(),
        key=lambda item: (
            -puanlar.get(item[0], 0),
            int(item[1].get('degisiklik_sayisi', 1)),
            str(item[0]),
        ),
    )
    for key, kayit in sirali:
        kayit['puan'] = puanlar.get(key, 0)
        adaylar.append(kayit)
        if len(adaylar) >= max(1, int(limit or 1)):
            break
    return adaylar[:max(1, int(limit or 1))]


def _what_if_uygula(
    aday: Dict,
    personeller: List[SolverPersonel],
    gorev_havuzlari: Dict[str, Set[int]],
    manuel_atamalar: List[SolverAtama],
    ara_gun: int,
    kilitli_hedefler: Dict[Any, Dict[str, int]],
) -> Tuple[
    List[SolverPersonel], Dict[str, Set[int]], List[SolverAtama], int,
    Dict[Any, Dict[str, int]],
]:
    """Senaryoyu yalnız derin kopyalara uygular; çağıranın verisi değişmez."""
    yeni_personeller = deepcopy(personeller)
    yeni_havuzlar = {str(role): set(ids or set()) for role, ids in gorev_havuzlari.items()}
    yeni_manuel_atamalar = deepcopy(manuel_atamalar)
    yeni_ara_gun = int(ara_gun)
    yeni_kilitli_hedefler = deepcopy(kilitli_hedefler or {})
    degisiklik = aday.get('degisiklik') or {}
    tur = aday.get('tur')

    if tur == 'ara_gun_azalt':
        yeni_ara_gun = max(0, int(degisiklik.get('yeni_ara_gun', ara_gun)))
        return (
            yeni_personeller, yeni_havuzlar, yeni_manuel_atamalar,
            yeni_ara_gun, yeni_kilitli_hedefler,
        )

    if tur == 'manuel_atama_kaldir':
        try:
            index = int(degisiklik.get('manuel_atama_index'))
        except (TypeError, ValueError):
            index = -1
        if 0 <= index < len(yeni_manuel_atamalar):
            yeni_manuel_atamalar.pop(index)
        return (
            yeni_personeller, yeni_havuzlar, yeni_manuel_atamalar,
            yeni_ara_gun, yeni_kilitli_hedefler,
        )

    pid = degisiklik.get('personel_id')
    personel = next((p for p in yeni_personeller if p.id == pid), None)
    if personel is None:
        return (
            yeni_personeller, yeni_havuzlar, yeni_manuel_atamalar,
            yeni_ara_gun, yeni_kilitli_hedefler,
        )

    if tur == 'mazeret_kaldir':
        gun = int(degisiklik.get('gun', 0) or 0)
        personel.mazeret_gunleri.discard(gun)
        if isinstance(personel.izin_turleri, dict):
            personel.izin_turleri.pop(gun, None)
    elif tur == 'havuz_ekle':
        role = str(degisiklik.get('gorev') or '')
        yeni_havuzlar.setdefault(role, set()).add(pid)
        if degisiklik.get('yetki_de_ekle'):
            personel.yetkili_gorevler.add(role)
    elif tur == 'yetki_ekle':
        role = str(degisiklik.get('gorev') or '')
        personel.yetkili_gorevler.add(role)
    return (
        yeni_personeller, yeni_havuzlar, yeni_manuel_atamalar,
        yeni_ara_gun, yeni_kilitli_hedefler,
    )


def _what_if_senaryolari_coz(
    *,
    adaylar: List[Dict],
    baseline: Dict,
    deadline: Optional[float],
    solver_kwargs: Dict,
) -> List[Dict]:
    """En fazla iki senaryoyu kısa tam-model geçişleriyle doğrular."""
    sonuclar = []
    for aday in adaylar[:2]:
        kalan = _kalan_tam_saniye(deadline)
        if deadline is not None and kalan < 1:
            break
        # What-if sayısı kadar süreyi büyütmeyelim: her senaryo tek saniyelik
        # bağımsız, tam-model doğrulama geçişidir.
        senaryo_suresi = 1
        (
            yeni_personeller,
            yeni_havuzlar,
            yeni_manuel_atamalar,
            yeni_ara_gun,
            yeni_kilitli_hedefler,
        ) = _what_if_uygula(
            aday,
            solver_kwargs['personeller'],
            solver_kwargs['gorev_havuzlari'],
            solver_kwargs['manuel_atamalar'],
            solver_kwargs['ara_gun'],
            solver_kwargs['kilitli_hedefler'],
        )
        yeni_kwargs = dict(solver_kwargs)
        yeni_kwargs.update({
            'personeller': yeni_personeller,
            'gorev_havuzlari': yeni_havuzlar,
            'manuel_atamalar': yeni_manuel_atamalar,
            'ara_gun': yeni_ara_gun,
            'kilitli_hedefler': yeni_kilitli_hedefler,
            'max_sure_saniye': senaryo_suresi,
        })
        solver = _kapasite_solver_olustur(**yeni_kwargs)
        minimum_fn = getattr(solver, 'minimum_bosluk_coz', None)
        if not callable(minimum_fn):
            break
        sonuc = minimum_fn(max_sure_saniye=senaryo_suresi)
        payload = _kismi_cozum_payload(
            sonuc,
            solver_kwargs['gun_sayisi'] * len(solver_kwargs['gorevler']),
        )
        onceki_bos = baseline.get('bos_slot')
        yeni_bos = payload.get('bos_slot')
        durum = str(payload.get('solver_status') or 'UNKNOWN').upper()
        gecerli_cozum = bool(
            getattr(sonuc, 'basarili', False)
            and durum in {'OPTIMAL', 'FEASIBLE'}
            and yeni_bos is not None
        )
        if not gecerli_cozum:
            continue
        delta = (
            int(onceki_bos) - int(yeni_bos)
            if onceki_bos is not None and yeni_bos is not None else None
        )
        # Bilinen bir incumbent varken yalnız gerçekten daha çok slot dolduran
        # senaryo çözüm önerisidir. Eşit/kötü ve UNKNOWN senaryolar raporlanmaz.
        if onceki_bos is not None and (delta is None or delta <= 0):
            continue
        # Baseline ölçülemiyorsa sıradan bir pozitif-boşluk incumbent'ını
        # "iyileştirme" diye sunamayız. Manuel hard çakışmayı kaldırarak ilk
        # geçerli çizelgeyi üretmek veya tam dolu bir tanık bulmak yine somut
        # ve doğrulanabilir bir karşı-olgusaldır.
        if (
            onceki_bos is None
            and aday.get('tur') != 'manuel_atama_kaldir'
            and int(yeni_bos) != 0
        ):
            continue
        delta_kanitlandi = bool(
            delta is not None
            and delta > 0
            and baseline.get('optimum_kanitlandi')
            and payload.get('optimum_kanitlandi')
        )
        kayit = {k: v for k, v in aday.items() if k != 'puan'}
        kayit.update({
            'solver_status': payload['solver_status'],
            'optimum_kanitlandi': payload['optimum_kanitlandi'],
            'kesinlik': payload['kesinlik'],
            'onceki_bos_slot': onceki_bos,
            'bos_slot': yeni_bos,
            'doldurulan_slot': payload['doldurulan_slot'],
            'toplam_slot': payload['toplam_slot'],
            'objective_bound': payload['objective_bound'],
            'minimum_bosluk_alt_siniri': payload['minimum_bosluk_alt_siniri'],
            'delta_bos_slot': delta,
            'delta_kanitlandi': delta_kanitlandi,
            'tam_doluluk_saglandi': yeni_bos == 0,
            # Tek değişiklikli bir senaryonun işe yaraması, bunun en küçük
            # düzeltme kümesi olduğunu kanıtlamaz.
            'minimum_duzeltme_kanitlandi': False,
            'otomatik_uygulandi': False,
            'kapsam': {
                'hedef_toplam': 'DEGISTIRILMEDI',
                'kilitli_hedefler': 'DAHIL',
                'otomatik_plan_hedefleri': 'HENUZ_URETILMEDI',
            },
        })
        sonuclar.append(kayit)
    return sonuclar


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
    kilitli_hedefler: Dict[Any, Dict[str, int]],
    kurum_profili: str,
    max_sure_saniye: int,
    gun_bazli_on_analiz: Optional[Dict] = None,
    deadline: Optional[float] = None,
) -> Dict:
    toplam_slot = gun_sayisi * len(gorevler)
    kalan = _kalan_tam_saniye(deadline)
    mevcut_butce = max(1, kalan) if deadline is not None else max_sure_saniye
    tam_sure = max(1, min(3, max(1, int(max_sure_saniye or 1) // 4), mevcut_butce))
    solver_kwargs = dict(
        gun_sayisi=gun_sayisi,
        gun_tipleri=gun_tipleri,
        personeller=personeller,
        gorevler=gorevler,
        kurallar=kurallar,
        gorev_havuzlari=gorev_havuzlari,
        kisitlama_istisnalari=kisitlama_istisnalari,
        birlikte_istisnalari=birlikte_istisnalari,
        aragun_istisnalari=aragun_istisnalari,
        kilitli_hedefler=kilitli_hedefler,
        manuel_atamalar=manuel_atamalar,
        ara_gun=ara_gun,
        max_sure_saniye=tam_sure,
        kurum_profili=kurum_profili,
    )
    solver = _kapasite_solver_olustur(**solver_kwargs)
    sonuc = solver.tam_doluluk_fizibilitesi(max_sure_saniye=tam_sure)
    istatistikler = sonuc.istatistikler or {}
    solver_durum = str(istatistikler.get('status') or '').upper()
    solver_bilgi = {
        'status': solver_durum or ('FEASIBLE' if sonuc.basarili else 'UNKNOWN'),
        'sure_ms': sonuc.sure_ms,
        'toplam_slot': toplam_slot,
        'feasibility_debug': istatistikler.get('feasibility_debug') or {},
        'manual_conflicts': istatistikler.get('manual_conflicts') or [],
    }

    if sonuc.basarili and int(istatistikler.get('bos_slot_sayisi', 0) or 0) == 0:
        fizibilite = _fizibilite_sonucu('FEASIBLE', solver=solver_bilgi)
        fizibilite.update({
            'tam_doluluk_mumkun': True,
            'kismi_cozum': None,
            'teshis': {
                'pencereler': [],
                'rol_sorunlari': [],
                'feasibility_debug': {},
                'unsat_core': [],
                'unsat_core_bilgisi': {},
                'karsi_olgusal_oneriler': [],
                'gun_bazli_on_analiz': gun_bazli_on_analiz or {},
            },
        })
        return fizibilite

    if solver_durum == 'MODEL_INVALID':
        fizibilite = _fizibilite_sonucu(
            'UNKNOWN',
            kod='MODEL_GECERSIZ',
            mesaj='Tam çizelge modeli geçersiz oluşturuldu.',
            oneri='Kontrolü yeniden çalıştırın veya sert kısıtları sadeleştirin.',
            solver=solver_bilgi,
        )
        fizibilite.update({
            'tam_doluluk_mumkun': None,
            'kismi_cozum': None,
            'teshis': {
                'pencereler': [],
                'rol_sorunlari': [],
                'feasibility_debug': solver_bilgi['feasibility_debug'],
                'unsat_core': [],
                'unsat_core_bilgisi': {},
                'karsi_olgusal_oneriler': [],
                'gun_bazli_on_analiz': gun_bazli_on_analiz or {},
            },
        })
        return fizibilite

    if solver_durum == 'MANUAL_CONFLICT':
        ilk_cakisma = (solver_bilgi['manual_conflicts'] or [{}])[0]
        manuel_adaylari = _what_if_adaylari(
            ara_gun=ara_gun,
            personeller=personeller,
            gorevler=gorevler,
            gorev_havuzlari=gorev_havuzlari,
            bos_slot_detaylari=[],
            manuel_atamalar=manuel_atamalar,
            manuel_cakismalar=solver_bilgi['manual_conflicts'],
            manuel_cakisma=True,
            limit=2,
        )
        manuel_senaryolari = _what_if_senaryolari_coz(
            adaylar=manuel_adaylari,
            baseline={'bos_slot': None, 'optimum_kanitlandi': False},
            deadline=deadline,
            solver_kwargs=solver_kwargs,
        )
        fizibilite = _fizibilite_sonucu(
            'INFEASIBLE',
            kod='MANUEL_ATAMA_CAKISMASI',
            mesaj=ilk_cakisma.get('mesaj') or sonuc.mesaj,
            oneri='Çakışan manuel atamaları düzeltin.',
            detay={'manual_conflicts': solver_bilgi['manual_conflicts'][:20]},
            solver=solver_bilgi,
        )
        fizibilite.update({
            'tam_doluluk_mumkun': False,
            # Çelişkili hard manuel atamalar varken geçerli bir kısmi çizelge
            # yoktur. Hiçbir manuel kural sessizce atlanmaz.
            'kismi_cozum': None,
            'teshis': {
                'pencereler': [],
                'rol_sorunlari': [],
                'feasibility_debug': solver_bilgi['feasibility_debug'],
                'unsat_core': [],
                'unsat_core_bilgisi': {},
                'karsi_olgusal_oneriler': manuel_senaryolari,
                'gun_bazli_on_analiz': gun_bazli_on_analiz or {},
                'kismi_cozum_engeli': 'MANUEL_ATAMA_CAKISMASI',
            },
        })
        return fizibilite

    # Tam-doluluk geçişi INFEASIBLE ise minimum boşluğu buluruz. İlk geçiş
    # UNKNOWN olsa da kalan bütçede aynı tam modelin min-boşluk geçişi ikinci
    # bir karar yolu sağlar: sıfır boşluk tanığı fizibiliteyi, OPTIMAL pozitif
    # boşluk ise imkânsızlığı kanıtlar.
    kalan = _kalan_tam_saniye(deadline)
    if deadline is not None and kalan < 1:
        minimum_sonuc = None
    else:
        # Core + en çok iki what-if için üç saniye rezerve edilir.
        min_tavan = max(1, int(max_sure_saniye or 1) // 3)
        minimum_sure = max(
            1,
            min(
                min_tavan,
                max(1, kalan - 3) if deadline is not None else min_tavan,
            ),
        )
        minimum_fn = getattr(solver, 'minimum_bosluk_coz', None)
        minimum_sonuc = (
            minimum_fn(max_sure_saniye=minimum_sure)
            if callable(minimum_fn) else None
        )
    kismi_cozum = _kismi_cozum_payload(minimum_sonuc, toplam_slot)
    minimum_istatistik = (
        getattr(minimum_sonuc, 'istatistikler', None) or {}
        if minimum_sonuc is not None else {}
    )
    feasibility_debug = (
        minimum_istatistik.get('feasibility_debug')
        or solver_bilgi['feasibility_debug']
        or {}
    )

    min_status = str(kismi_cozum.get('solver_status') or 'UNKNOWN').upper()
    min_bos = kismi_cozum.get('bos_slot')
    min_cozum_var = bool(
        minimum_sonuc is not None
        and getattr(minimum_sonuc, 'basarili', False)
        and min_bos is not None
    )
    tam_imkansiz_kanitlandi = solver_durum == 'INFEASIBLE'

    if solver_durum == 'UNKNOWN' and min_cozum_var and int(min_bos) == 0:
        solver_bilgi.update({
            'karar_kaynagi': 'MINIMUM_BOSLUK_SIFIR_TANIK',
            'minimum_bosluk_status': min_status,
        })
        fizibilite = _fizibilite_sonucu(
            'FEASIBLE',
            mesaj='Tam-doluluk geçişi süreye takıldı; minimum-boşluk modeli tam dolu geçerli çizelge buldu.',
            solver=solver_bilgi,
        )
        fizibilite.update({
            'tam_doluluk_mumkun': True,
            'kismi_cozum': kismi_cozum,
            'teshis': {
                'pencereler': [],
                'rol_sorunlari': [],
                'feasibility_debug': feasibility_debug,
                'unsat_core': [],
                'unsat_core_bilgisi': {},
                'karsi_olgusal_oneriler': [],
                'gun_bazli_on_analiz': gun_bazli_on_analiz or {},
            },
        })
        return fizibilite

    if solver_durum == 'UNKNOWN':
        if min_status == 'OPTIMAL' and min_bos is not None and int(min_bos) > 0:
            tam_imkansiz_kanitlandi = True
            solver_bilgi.update({
                'karar_kaynagi': 'MINIMUM_BOSLUK_OPTIMUM',
                'minimum_bosluk_status': min_status,
            })
        elif min_status in {'INFEASIBLE', 'MANUAL_CONFLICT'}:
            tam_imkansiz_kanitlandi = True
            solver_bilgi.update({
                'karar_kaynagi': 'MINIMUM_BOSLUK_TABAN_MODEL',
                'minimum_bosluk_status': min_status,
            })
        else:
            # FEASIBLE pozitif boşluk yalnız bir incumbent'tır. Tam çözümün
            # bulunamadığını gösterir, imkânsız olduğunu kanıtlamaz.
            fizibilite = _fizibilite_sonucu(
                'UNKNOWN',
                kod='FIZIBILITE_BELIRLENEMEDI',
                mesaj=(
                    'Tam doluluğun mümkün olup olmadığı süre sınırı içinde kanıtlanamadı.'
                    + (
                        f" Geçerli bir kısmi çizelgede {min_bos} boş slot bulundu."
                        if min_cozum_var else ''
                    )
                ),
                oneri='Süre sınırını artırıp analizi yeniden çalıştırın.',
                solver=solver_bilgi,
            )
            fizibilite.update({
                'tam_doluluk_mumkun': None,
                'kismi_cozum': kismi_cozum if min_cozum_var else None,
                'teshis': {
                    'pencereler': [],
                    'rol_sorunlari': list(feasibility_debug.get('role_ara_gun_capacity_issues') or []),
                    'feasibility_debug': feasibility_debug,
                    'unsat_core': [],
                    'unsat_core_bilgisi': {},
                    'karsi_olgusal_oneriler': [],
                    'gun_bazli_on_analiz': gun_bazli_on_analiz or {},
                },
            })
            return fizibilite

    if not tam_imkansiz_kanitlandi:
        # Tanınmayan solver statüsü asla kesin imkânsızlık olarak sunulmaz.
        fizibilite = _fizibilite_sonucu(
            'UNKNOWN',
            kod='FIZIBILITE_BELIRLENEMEDI',
            mesaj='Tam model kesin bir fizibilite kararı üretemedi.',
            oneri='Analizi yeniden çalıştırın.',
            solver=solver_bilgi,
        )
        fizibilite.update({
            'tam_doluluk_mumkun': None,
            'kismi_cozum': kismi_cozum if min_cozum_var else None,
            'teshis': {
                'pencereler': [],
                'rol_sorunlari': [],
                'feasibility_debug': feasibility_debug,
                'unsat_core': [],
                'unsat_core_bilgisi': {},
                'karsi_olgusal_oneriler': [],
                'gun_bazli_on_analiz': gun_bazli_on_analiz or {},
            },
        })
        return fizibilite

    core_bilgisi: Dict = {}
    kalan = _kalan_tam_saniye(deadline)
    core_fn = getattr(solver, 'diagnose_tam_doluluk_with_unsat_core', None)
    # Core raporu 1 saniyede yetmiyor ve boş dönüyor; bu modelde aynı
    # geçiş 0.9 saniyede bitiyor. 5'e çıkarılır, kalan bütçeyi aşmaz.
    # Sonraki what-if adayları kalan sürede çalışır.
    if callable(core_fn) and (deadline is None or kalan >= 1):
        core_tavan = 5 if deadline is None else max(1, min(5, kalan))
        core_bilgisi = core_fn(max_sure_saniye=core_tavan) or {}

    adaylar = _what_if_adaylari(
        ara_gun=ara_gun,
        personeller=personeller,
        gorevler=gorevler,
        gorev_havuzlari=gorev_havuzlari,
        bos_slot_detaylari=kismi_cozum['bos_slot_detaylari'],
        manuel_atamalar=manuel_atamalar,
        limit=2,
    )
    what_if_sonuclari = _what_if_senaryolari_coz(
        adaylar=adaylar,
        baseline=kismi_cozum,
        deadline=deadline,
        solver_kwargs=solver_kwargs,
    )

    on_analiz_neden = (gun_bazli_on_analiz or {}).get('neden') or {}
    on_analiz_detay = dict(on_analiz_neden.get('detay') or {})
    pencereler = list(on_analiz_detay.get('ara_gun_pencere_aciklari') or [])
    rol_sorunlari = list(feasibility_debug.get('role_ara_gun_capacity_issues') or [])
    # Görevler arası çekişme görev-bağımsız üst sınırın göremediği açıklardır;
    # görev başına kapasite "yeterli" görünürken yine de slot boş kalır.
    kesisim_aciklari = list(feasibility_debug.get('aday_kesisimi_aciklari') or [])
    ortak_butce = dict(feasibility_debug.get('ortak_butce_kapasitesi') or {})
    teshis = {
        'pencereler': pencereler,
        'rol_sorunlari': rol_sorunlari,
        'aday_kesisimi_aciklari': kesisim_aciklari,
        'ortak_butce_kapasitesi': ortak_butce,
        'feasibility_debug': feasibility_debug,
        'unsat_core': list(core_bilgisi.get('core_groups') or []),
        'unsat_core_bilgisi': core_bilgisi,
        'karsi_olgusal_oneriler': what_if_sonuclari,
        'gun_bazli_on_analiz': gun_bazli_on_analiz or {},
    }
    if not min_cozum_var:
        teshis['kismi_cozum_engeli'] = min_status

    if (gun_bazli_on_analiz or {}).get('durum') == 'INFEASIBLE':
        kod = on_analiz_neden.get('kod') or 'TAM_CIZELGE_KISIT_CAKISMASI'
        mesaj = on_analiz_neden.get('mesaj') or 'Tam doluluk mümkün değil.'
        oneri = (gun_bazli_on_analiz or {}).get('oneri')
        neden_detay = on_analiz_detay
    else:
        kod = 'TAM_CIZELGE_KISIT_CAKISMASI'
        mesaj = 'Görev, havuz, mazeret, ara gün, manuel ve personel kurallarıyla tüm slotlar doldurulamıyor.'
        oneri = 'Boş görevleri ve kanıt düzeyi belirtilen karşı-olgusal seçenekleri inceleyin.'
        neden_detay = {}
    neden_detay.update({
        'feasibility_debug': feasibility_debug,
        'minimum_bosluk_status': kismi_cozum['solver_status'],
        'bos_slot': kismi_cozum['bos_slot'],
        'optimum_kanitlandi': kismi_cozum['optimum_kanitlandi'],
    })

    fizibilite = _fizibilite_sonucu(
        'INFEASIBLE',
        kod=kod,
        mesaj=mesaj,
        oneri=oneri,
        detay=neden_detay,
        solver=solver_bilgi,
    )
    fizibilite.update({
        'tam_doluluk_mumkun': False,
        'kismi_cozum': kismi_cozum if min_cozum_var else None,
        'teshis': teshis,
    })
    return fizibilite


def gun_bazli_fizibilite_kontrolu(
    gun_sayisi: int,
    personeller: List[SolverPersonel],
    slot_sayisi: int,
    ara_gun: int = 2,
    manuel_atamalar: Optional[List[SolverAtama]] = None,
    birlikte_kurallar: Optional[List[SolverKural]] = None,
    birlikte_istisnalari: Optional[List[Dict]] = None,
    aragun_istisnalari: Optional[List[Dict]] = None,
    ayri_kurallar: Optional[List[SolverKural]] = None,
    max_sure_saniye: int = 5,
) -> Dict:
    """Temel kişi-gün kısıtları için kesin CP-SAT fizibilite denetimi."""
    manuel_atamalar = list(manuel_atamalar or [])
    birlikte_kurallar = list(birlikte_kurallar or [])
    ayri_kurallar = list(ayri_kurallar or [])
    birlikte_istisnalari = list(birlikte_istisnalari or [])
    aragun_istisnalari = list(aragun_istisnalari or [])
    ara_gun = max(0, int(ara_gun or 0))
    slot_sayisi = max(0, int(slot_sayisi or 0))
    gunler = list(range(1, int(gun_sayisi or 0) + 1))
    personel_map = {p.id: p for p in personeller}
    pids = list(personel_map.keys())
    pid_ad = {p.id: p.ad for p in personeller}

    def _ad(pid: Any) -> str:
        return str(pid_ad.get(pid) or f'ID:{pid}')

    def _adlar(pid_listesi) -> List[str]:
        return [_ad(pid) for pid in pid_listesi]

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
            detay={
                'aciklama': (
                    f'Her gün {slot_sayisi} görev doldurulması gerekiyor, '
                    f'ama kadroda toplam {len(pids)} personel var.'
                ),
                'slot_sayisi': slot_sayisi,
                'personel_sayisi': len(pids),
            },
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
            ad = _ad(pid)
            return _fizibilite_sonucu(
                'INFEASIBLE',
                kod='MANUEL_KISI_GUN_CAKISMASI',
                mesaj='Aynı personele aynı gün birden fazla manuel görev atanmış.',
                oneri='Personelin aynı gündeki fazla manuel atamasını kaldırın.',
                detay={
                    'aciklama': (
                        f'{ad} için {gun}. güne {adet} ayrı manuel görev atanmış. '
                        f'Bir kişi aynı gün yalnız bir nöbet tutabilir.'
                    ),
                    'personel_id': pid,
                    'personel_ad': ad,
                    'gun': gun,
                    'atama_sayisi': adet,
                },
            )

    for (gun, slot_idx), atanan_ids in manuel_slotlar.items():
        if len(atanan_ids) > 1:
            return _fizibilite_sonucu(
                'INFEASIBLE',
                kod='MANUEL_SLOT_CAKISMASI',
                mesaj='Aynı gün ve görev slotuna birden fazla manuel personel atanmış.',
                oneri='Çakışan manuel atamalardan yalnız birini bırakın.',
                detay={
                    'aciklama': (
                        f'{gun}. gün {slot_idx + 1}. göreve aynı anda '
                        f'{", ".join(_adlar(atanan_ids))} atanmış. Bir slota tek kişi yazılabilir.'
                    ),
                    'gun': gun,
                    'slot_idx': slot_idx,
                    'personel_ids': atanan_ids,
                    'personel_adlari': _adlar(atanan_ids),
                },
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
                    detay={
                        'aciklama': (
                            f'{_ad(pid)} için {gun}. güne manuel atama yapılmış, '
                            f'ama o gün mazeretli/izinli. Mazeret istisnası da onaylanmamış.'
                        ),
                        'personel_id': pid,
                        'personel_ad': _ad(pid),
                        'gun': gun,
                    },
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
                    detay={
                        'aciklama': (
                            f'{_ad(pid)} için {gun1}. ve {gun2}. güne manuel atama var, '
                            f'arada {gun2 - gun1 - 1} gün boşluk kalıyor; '
                            f'ara gün kuralı en az {ara_gun} gün istiyor.'
                        ),
                        'personel_id': pid,
                        'personel_ad': _ad(pid),
                        'gunler': [gun1, gun2],
                        'ara_gun': ara_gun,
                    },
                )

    def musait_mi(pid: int, gun: int) -> bool:
        return (
            gun not in personel_map[pid].mazeret_gunleri
            or (pid, gun) in onayli_mazeret_gunleri
        )

    for gun in gunler:
        musait_ids = [pid for pid in pids if musait_mi(pid, gun)]
        if len(musait_ids) < slot_sayisi:
            mazeretli_ids = [pid for pid in pids if pid not in set(musait_ids)]
            mazeretli_adlar = _adlar(mazeretli_ids)
            eksik = slot_sayisi - len(musait_ids)
            return _fizibilite_sonucu(
                'INFEASIBLE',
                kod='GUNLUK_MAZERET_KAPASITESI',
                mesaj='Bir günde görevleri dolduracak kadar müsait personel yok.',
                oneri='Bu gündeki mazeretleri gözden geçirin, personel ekleyin veya slot sayısını azaltın.',
                detay={
                    'aciklama': (
                        f'{gun}. gün {slot_sayisi} görev var, ama yalnız {len(musait_ids)} '
                        f'personel müsait ({eksik} kişi eksik). O gün mazeretli/izinli olanlar: '
                        f'{", ".join(mazeretli_adlar) if mazeretli_adlar else "yok"}.'
                    ),
                    'gun': gun,
                    'gereken': slot_sayisi,
                    'musait': len(musait_ids),
                    'eksik': eksik,
                    'mazeretli_adlar': mazeretli_adlar,
                    'musait_adlar': _adlar(musait_ids),
                },
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
                        manuel_ad = _ad(manuel_pid)
                        diger_ad = _ad(diger_pid)
                        return _fizibilite_sonucu(
                            'INFEASIBLE',
                            kod='BIRLIKTE_MANUEL_MAZERET_CAKISMASI',
                            mesaj='Hard birlikte grubunda manuel atama ile mazeret çakışıyor.',
                            oneri='Manuel günü değiştirin, ortak mazereti kaldırın veya birlikte kuralını soft yapın.',
                            detay={
                                'aciklama': f'{manuel_ad} için {gun}. gün manuel atama var, ama birlikte olması gereken {diger_ad} o gün müsait değil (mazeret/izin).',
                                'gun': gun,
                                'manuel_personel_id': manuel_pid,
                                'manuel_personel_ad': manuel_ad,
                                'musait_olmayan_id': diger_pid,
                                'musait_olmayan_ad': diger_ad
                            },
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
                    detay={
                        'aciklama': (
                            f'{gun}. gün birlikte çalışması zorunlu grup '
                            f'({", ".join(_adlar(grup))}) {len(grup)} kişi, '
                            f'ama o gün yalnız {slot_sayisi} görev slotu var.'
                        ),
                        'gun': gun,
                        'grup_boyutu': len(grup),
                        'grup_adlari': _adlar(grup),
                        'slot_sayisi': slot_sayisi,
                    },
                )

    from ortools.sat.python import cp_model as cp

    # Hard "ayrı" grupları: aynı gruptaki iki kişi aynı gün ikisine birden
    # yazılamaz. Çözücü bu kuralı gün düzeyinde ikili yasak olarak modeller
    # (ortools_solver H5_AYRI_TUTMA); ön analiz de aynı biçimi kullanır, böylece
    # ayrı kuralından doğan çakışma generic kod yerine adıyla yakalanır.
    # Soft politikalı gruplar kasıtlı olarak dışarıda: onlar tercihtir, fizibiliteyi
    # bozmazlar.
    hard_ayri_gruplar: List[List[int]] = []
    for kural in ayri_kurallar:
        if getattr(kural, 'tur', None) != 'ayri':
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
            hard_ayri_gruplar.append(grup)

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

    for grup in hard_ayri_gruplar:
        for idx, referans_id in enumerate(grup):
            for diger_id in grup[idx + 1:]:
                for gun in gunler:
                    model.Add(
                        kisi_gun[referans_id, gun] + kisi_gun[diger_id, gun] <= 1
                    ).OnlyEnforceIf(varsayim('HARD_AYRI'))

    def ara_gun_pencere_aciklari() -> List[Dict]:
        if ara_gun <= 0:
            return []

        aciklar = []
        for baslangic in gunler:
            for bitis in range(baslangic, gun_sayisi + 1):
                ust_kapasite = 0
                for pid in pids:
                    uygun = {
                        gun for gun in range(baslangic, bitis + 1)
                        if musait_mi(pid, gun)
                    }
                    ust_kapasite += _pencere_kisi_ust_kapasitesi(
                        pid=pid,
                        uygun_gunler=uygun,
                        baslangic=baslangic,
                        bitis=bitis,
                        ara_gun=ara_gun,
                        aragun_istisnalari=aragun_istisna_map.get(pid, set()),
                    )
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
        'HARD_AYRI': (
            'AYRI_KURALI_CAKISMASI',
            'Hard ayrı kuralı diğer günlük kısıtlarla çakışıyor.',
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
    if 'HARD_AYRI' in core_gruplari:
        oneriler.append('ayrı kuralındaki kişileri ve günleri gözden geçirin (kuralı soft yapın veya gruptakilerin müsait günlerini artırın)')
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
                     kilitli_hedefler: Optional[Dict[Any, Dict[str, int]]] = None,
                     kurum_profili: str = 'genel',
                     max_sure_saniye: int = 10) -> Dict:
    analiz_baslangici = time.monotonic()
    toplam_butce = max(1, int(max_sure_saniye or 10))
    deadline = analiz_baslangici + toplam_butce
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
    ayri_kurallari = [kural for kural in tum_kurallar if kural.tur == 'ayri']
    birlikte_istisnalari = list(birlikte_istisnalari or [])
    aragun_istisnalari = list(aragun_istisnalari or [])
    gorev_havuzlari = dict(gorev_havuzlari or {})
    kisitlama_istisnalari = list(kisitlama_istisnalari or [])
    kilitli_hedefler = dict(kilitli_hedefler or {})

    tip_sayilari = {t: 0 for t in GUN_TIPLERI}
    for g, tip in gun_tipleri.items():
        if tip in tip_sayilari:
            tip_sayilari[tip] += 1

    tip_slotlari = {t: tip_sayilari[t] * slot_sayisi for t in GUN_TIPLERI}
    # Otoritatif talep gerçek gün × gerçek görev-slotu sayısıdır. Gün tipi
    # kırılımında tanınmayan/eksik bir etiket toplam talebi küçültmemelidir.
    toplam_slot = gun_sayisi * slot_sayisi

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

    if toplam_butce >= 2:
        gun_bazli_fizibilite = gun_bazli_fizibilite_kontrolu(
            gun_sayisi=gun_sayisi,
            personeller=personeller,
            slot_sayisi=slot_sayisi,
            ara_gun=ara_gun,
            manuel_atamalar=manuel_atamalar,
            birlikte_kurallar=birlikte_kurallari,
            birlikte_istisnalari=birlikte_istisnalari,
            aragun_istisnalari=aragun_istisnalari,
            ayri_kurallar=ayri_kurallari,
            max_sure_saniye=1,
        )
    else:
        gun_bazli_fizibilite = _fizibilite_sonucu(
            'UNKNOWN',
            kod='ON_ANALIZ_BUTCE_YOK',
            mesaj='Hızlı danışman analizi süre bütçesini tam modele bırakmak için atlandı.',
            oneri='Daha ayrıntılı pencere analizi için süre sınırını artırın.',
            solver={'status': 'SKIPPED'},
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
        kilitli_hedefler=kilitli_hedefler,
        kurum_profili=kurum_profili,
        max_sure_saniye=max_sure_saniye,
        gun_bazli_on_analiz=gun_bazli_fizibilite,
        deadline=deadline,
    )

    # Tek otorite gerçek kişi × gün × görev-slotu modelidir. Kişi-gün modeli
    # yalnız açıklayıcı ön analiz olarak ``teshis`` altında taşınır.
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

    kapsam = {
        'otoritatif_model': 'KISI_GUN_GOREV_SLOTU',
        'hard_operasyon_kisitlari': 'DAHIL',
        'kilitli_hedefler': 'DAHIL' if kilitli_hedefler else 'YOK',
        'kilitli_hedef_sayisi': len(kilitli_hedefler),
        # Otomatik hedef/adalet planı bu endpoint'ten sonra üretildiği için
        # fiziksel fizibilite hükmünün parçası değildir; bu açıkça raporlanır.
        'otomatik_plan_hedefleri': 'HENUZ_URETILMEDI',
    }
    fizibilite['kapsam'] = kapsam

    return {
        'durum': durum,
        'uygulanabilir': uygulanabilir,
        'tam_doluluk_mumkun': fizibilite.get('tam_doluluk_mumkun'),
        'mesaj': mesaj,
        'neden': fizibilite.get('neden'),
        'oneri': fizibilite.get('oneri'),
        'gun_sayisi': gun_sayisi,
        'tip_sayilari': tip_sayilari,
        'tip_slotlari': tip_slotlari,
        'toplam_slot': toplam_slot,
        'personel_sayisi': len(personeller),
        'kapasiteler': kapasite_listesi,
        'kismi_cozum': fizibilite.get('kismi_cozum'),
        'teshis': fizibilite.get('teshis') or {},
        'kapsam': kapsam,
        'analiz_suresi_ms': int((time.monotonic() - analiz_baslangici) * 1000),
        'fizibilite': fizibilite,
    }
