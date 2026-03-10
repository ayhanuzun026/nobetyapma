"""
Greedy Fallback — SolverPersonel -> Personel dönüşümü yapıp greedy solver çalıştırır.
OR-Tools çözüm bulamadığında son çare olarak kullanılır.
"""

import time as _time

from models import GorevTanim, Personel
from greedy_solver import NobetYoneticisi
from solver_models import SolverSonuc
from parsers import build_takvim


def greedy_fallback(personeller, gorevler, gun_sayisi, gun_tipleri,
                    kurallar, hedefler, ara_gun, yil, ay, resmi_tatiller,
                    gorev_kisitlamalari=None):
    """SolverPersonel -> Personel dönüşümü yapıp greedy solver çalıştırır,
    sonucu SolverSonuc formatına çevirir."""
    baslangic = _time.time()

    # SolverGorev -> GorevTanim dönüşümü
    greedy_gorevler = []
    for g in gorevler:
        greedy_gorevler.append(GorevTanim(
            id=g.id, ad=g.ad, slot_index=g.slot_idx,
            base_name=g.base_name if g.base_name else g.ad,
            ayri_bina=g.ayri_bina
        ))

    # SolverPersonel -> Personel dönüşümü
    greedy_personeller = []
    for sp in personeller:
        h = hedefler.get(sp.id, {})
        hedef_tipler = h.get('hedef_tipler', {})
        hedef_toplam = h.get('hedef_toplam', 3)

        greedy_p = Personel(
            id=sp.id, ad=sp.ad,
            hedef_toplam=hedef_toplam,
            hedef_hici=hedef_tipler.get('hici', 0),
            hedef_prs=hedef_tipler.get('prs', 0),
            hedef_cum=hedef_tipler.get('cum', 0),
            hedef_cmt=hedef_tipler.get('cmt', 0),
            hedef_pzr=hedef_tipler.get('pzr', 0),
            hedef_roller=h.get('gorev_kotalari', {}),
            mazeret_gunleri=sp.mazeret_gunleri.copy()
        )
        greedy_personeller.append(greedy_p)

    # Greedy kurallar: SolverKural -> dict format
    greedy_kurallar = []
    for k in kurallar:
        kural_dict = {'tur': k.tur}
        for i, pid in enumerate(k.kisiler):
            # Kişi adını bul
            p = next((pp for pp in greedy_personeller if pp.id == pid), None)
            if p:
                kural_dict[f'p{i+1}'] = p.ad
        greedy_kurallar.append(kural_dict)

    takvim = build_takvim(yil, ay, resmi_tatiller)
    yonetici = NobetYoneticisi(
        personeller=greedy_personeller,
        gunluk_sayi=len(greedy_gorevler),
        takvim=takvim,
        ara_gun=ara_gun,
        days_in_month=gun_sayisi,
        gorev_tanimlari=greedy_gorevler,
        kurallar=greedy_kurallar,
        gorev_kisitlamalari=gorev_kisitlamalari
    )
    yonetici.dagit()

    # Greedy sonucunu SolverSonuc formatına dönüştür
    atamalar = []
    for gun, slotlar in yonetici.cizelge.items():
        for slot_idx, personel_ad in enumerate(slotlar):
            if personel_ad is not None:
                gorev = greedy_gorevler[slot_idx] if slot_idx < len(greedy_gorevler) else None
                gorev_ad = gorev.ad if gorev else f'Slot {slot_idx}'
                base_name = gorev.base_name if gorev and gorev.base_name else gorev_ad
                gun_tipi = gun_tipleri.get(gun, 'hici')
                p = next((pp for pp in greedy_personeller if pp.ad == personel_ad), None)
                pid = p.id if p else 0
                atamalar.append({
                    'gun': gun, 'slot_idx': slot_idx, 'gorev_ad': gorev_ad,
                    'gorev_base': base_name, 'personel_id': pid,
                    'personel_ad': personel_ad, 'gun_tipi': gun_tipi
                })

    toplam_atama = len(atamalar)
    toplam_slot = gun_sayisi * len(greedy_gorevler)
    sure_ms = int((_time.time() - baslangic) * 1000)

    return SolverSonuc(
        basarili=toplam_atama > 0,
        atamalar=atamalar,
        istatistikler={
            'status': 'GREEDY_FALLBACK',
            'toplam_atama': toplam_atama,
            'toplam_slot': toplam_slot,
            'doluluk_yuzde': round(100 * toplam_atama / toplam_slot, 1) if toplam_slot > 0 else 0,
            'bos_slot_sayisi': toplam_slot - toplam_atama,
            'ara_gun': ara_gun,
        },
        sure_ms=sure_ms,
        mesaj='GREEDY_FALLBACK'
    )
