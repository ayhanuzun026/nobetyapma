"""
Kapasite hesaplama — Personel müsaitlik ve slot kapasitesi analizi.
"""

from typing import List, Dict

from utils import GUN_TIPLERI
from solver_models import SolverPersonel


def kapasite_hesapla(gun_sayisi: int, gun_tipleri: Dict[int, str],
                     personeller: List[SolverPersonel], slot_sayisi: int) -> Dict:
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

    return {
        'gun_sayisi': gun_sayisi,
        'tip_sayilari': tip_sayilari,
        'tip_slotlari': tip_slotlari,
        'toplam_slot': toplam_slot,
        'personel_sayisi': len(personeller),
        'kapasiteler': kapasite_listesi
    }
