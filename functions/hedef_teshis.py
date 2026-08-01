"""Hedef hesaplama CP-SAT basarisizlik teshis yardimcilari."""

from typing import Any, Dict, List, Optional, Set

from utils import GUN_TIPLERI, find_matching_id

# Gun tipi -> kullaniciya gosterilecek ad.
_TIP_ADLARI = {
    'hici': 'hafta içi',
    'prs': 'perşembe',
    'cum': 'cuma',
    'cmt': 'cumartesi',
    'pzr': 'pazar',
}


def _ara_gun_tavani(gun_sayisi: int, ara_gun: int) -> int:
    """``ara_gun`` kuralıyla bir kişinin ayda tutabileceği azami nöbet sayısı."""
    if ara_gun <= 0:
        return gun_sayisi
    return -(-gun_sayisi // (ara_gun + 1))   # ceil


def _kisi_ust_siniri(p, sinir: Optional[Dict], gun_sayisi: int, ara_gun: int) -> int:
    """Verilen ara gün değerinde kişinin tahmini nöbet tavanı."""
    musait = len(getattr(p, 'musait_gunler', ()) or ())
    tavan = min(musait, _ara_gun_tavani(gun_sayisi, ara_gun))
    max_nobet = (sinir or {}).get('max_nobet')
    if max_nobet is not None:
        tavan = min(tavan, int(max_nobet))
    return max(0, tavan)


def hedef_infeasible_insan_dili(
    hesaplayici: Any,
    personel_sinirlar: Dict,
    kilitli_ids: Set[int],
    debug_msg: str = "",
) -> Dict[str, Any]:
    """Çözülemeyen hedef modelini kullanıcı diline çevirir.

    Ham CP-SAT çıktısı (``STATUS=3 | IZOLASYON: ...``) kullanıcıya gösterilmez;
    burada kök neden aranıp somut aksiyon önerilir. Dönen sözlük:
    ``{neden, baslik, detay, oneriler[], debug}``. Neden kesin saptanamazsa
    ``neden='belirsiz'`` döner ve genel öneriler verilir.
    """
    self = hesaplayici
    toplam_slot = int(self.toplam_slot)
    personeller = list(self.personel_listesi)
    n = len(personeller)
    oneriler: List[str] = []

    # --- 1. Toplam arz: herkesin tavanı toplansa slotlar dolar mı? ---
    toplam_ust = 0
    for p in personeller:
        if p.id in kilitli_ids:
            toplam_ust += sum(int(v) for v in (p.hedef_tipler or {}).values())
            continue
        sinir = personel_sinirlar.get(p.id)
        if sinir is not None:
            toplam_ust += int(sinir.get('uygulanan_ust_sinir', 0))
        else:
            toplam_ust += _kisi_ust_siniri(p, None, self.gun_sayisi, self.ara_gun)

    if toplam_ust < toplam_slot:
        eksik = toplam_slot - toplam_ust
        # Ara gün kuralı bağlıyorsa: hangi değer yeterdi?
        if self.ara_gun > 0:
            for aday in range(self.ara_gun - 1, -1, -1):
                tahmin = sum(
                    _kisi_ust_siniri(p, personel_sinirlar.get(p.id), self.gun_sayisi, aday)
                    for p in personeller if p.id not in kilitli_ids
                )
                if tahmin >= toplam_slot:
                    oneriler.append(
                        f"Ara günü {self.ara_gun} yerine {aday} yapın "
                        f"(kapasite ~{tahmin} nöbete çıkar, {toplam_slot} yeri doldurur)."
                    )
                    break
        kisi_basi = max(1, toplam_ust // n) if n else 1
        oneriler.append(
            f"Ya da en az {-(-eksik // kisi_basi)} personel daha ekleyin."
        )
        oneriler.append(
            "Ya da günlük ekip/slot sayısını azaltın "
            f"({toplam_slot} nöbet yerine {toplam_ust} kişi-nöbet kapasitesi var)."
        )
        return {
            'neden': 'kapasite_yetersiz',
            'baslik': 'Nöbet yerlerini dolduracak kadar müsait personel yok',
            'detay': (
                f"Ay boyunca {toplam_slot} nöbet yeri var, ancak {n} personelin "
                f"mazeret ve ara gün kuralı sonrası toplam kapasitesi {toplam_ust} nöbet. "
                f"{eksik} nöbet yeri açıkta kalıyor."
            ),
            'oneriler': oneriler,
            'debug': debug_msg,
        }

    # --- 2. Gün tipi bazlı yetersizlik (ör. cumartesi müsait kimse yok) ---
    for tip in GUN_TIPLERI:
        ihtiyac = int(self.tip_slotlari.get(tip, 0))
        if ihtiyac <= 0:
            continue
        musait = sum(int(p.musait_tipler.get(tip, 0)) for p in personeller)
        if musait < ihtiyac:
            tip_adi = _TIP_ADLARI.get(tip, tip)
            return {
                'neden': 'gun_tipi_yetersiz',
                'baslik': f"{tip_adi.capitalize()} günlerinde yeterli müsait personel yok",
                'detay': (
                    f"{tip_adi.capitalize()} günleri {ihtiyac} nöbet yeri istiyor, "
                    f"toplam müsaitlik {musait}."
                ),
                'oneriler': [
                    f"{tip_adi.capitalize()} günlerindeki mazeret/izinlerden bazılarını kaldırın.",
                    f"{tip_adi.capitalize()} günleri için ekip sayısını azaltın.",
                ],
                'debug': debug_msg,
            }

    # --- 3. Kapasite var ama kurallar çelişiyor ---
    return {
        'neden': 'kurallar_celisiyor',
        'baslik': 'Kurallar birbiriyle çelişiyor',
        'detay': (
            f"{toplam_slot} nöbet yeri için toplam kapasite ({toplam_ust}) yeterli, "
            "ancak kurallar birlikte sağlanamıyor. En sık sebep: manuel atamalar, "
            "kilitli hedefler, 'birlikte' kuralları veya görev havuzu kısıtları."
        ),
        'oneriler': [
            "Manuel (elle) atamaları ve kilitli hedefleri gözden geçirin.",
            "'Birlikte' ve 'ayrı' kurallarından bazılarını geçici olarak kaldırın.",
            "Görev havuzlarını kontrol edin: bir role yalnız birkaç kişi yetkiliyse "
            "o rolün slotları dolmayabilir.",
        ],
        'debug': debug_msg,
    }


def build_hedef_infeasible_debug(
    cp: Any,
    hesaplayici: Any,
    status: int,
    n: int,
    kilitli_ids: Set[int],
    kalan_slot: int,
    n_kilitsiz: int,
    avg_count_float: float,
    avg_count_floor: int,
    avg_hours: int,
    hard_cap: int,
    kisitli_kapasite: Dict,
    manuel_sayac: Dict,
    total_we_slots: int,
    we_tipleri: List[str],
) -> str:
    """Basarisiz hedef modelini izole eden detayli debug mesajini uretir."""
    self = hesaplayici
    HARD_CAP = hard_cap
    # === DETAYLI İZOLASYON DEBUG ===
    debug_info = []
    debug_info.append(f"STATUS={status}")
    debug_info.append(f"toplam_slot={self.toplam_slot}, personel={n}, kilitli={len(kilitli_ids)}")
    debug_info.append(f"kalan_slot={kalan_slot}, kilitsiz={n_kilitsiz}, avg={avg_count_float:.2f}, HARD_CAP={HARD_CAP}")

    # Gün tipi kapasite analizi
    for tip in GUN_TIPLERI:
        ihtiyac = self.tip_slotlari[tip]
        toplam_musait = sum(p.musait_tipler.get(tip, 0) for p in self.personel_listesi if p.id not in kilitli_ids)
        kilitli_tip = sum(self.kilitli_hedefler.get(find_matching_id(pid, self.kilitli_hedefler.keys()) or -1, {}).get(tip, 0) for pid in kilitli_ids)
        kalan_ihtiyac = ihtiyac - kilitli_tip
        debug_info.append(f"  {tip}: ihtiyac={ihtiyac}, kilitli={kilitli_tip}, kalan={kalan_ihtiyac}, musait={toplam_musait}")

    # Kişi bazlı darboğaz analizi
    toplam_upper = 0
    darbogazlar = []
    for p in self.personel_listesi:
        pid = p.id
        if pid in kilitli_ids:
            toplam_upper += sum(p.hedef_tipler.values())
            continue
        mk = sum(p.musait_tipler.get(tip, 0) for tip in GUN_TIPLERI)
        matched_k = find_matching_id(pid, kisitli_kapasite.keys())
        if matched_k is not None:
            mk = min(mk, kisitli_kapasite[matched_k])
        ub = min(mk, HARD_CAP)
        toplam_upper += ub
        if ub < 2:
            darbogazlar.append(f"{p.ad}: musait={mk}, ub={ub}, mazeret={len(p.mazeret_gunleri)}")

    debug_info.append(f"toplam_upper_bound={toplam_upper} vs toplam_slot={self.toplam_slot}")
    if toplam_upper < self.toplam_slot:
        debug_info.append(f"*** KAPASITE YETERSIZ: {self.toplam_slot - toplam_upper} slot eksik ***")
    if darbogazlar:
        debug_info.append(f"darbogazlar ({len(darbogazlar)}): " + "; ".join(darbogazlar[:10]))

    # === İZOLASYON TESTLERİ ===
    # Her kısıt grubunu tek tek kaldırıp hangisi olmadan çözüm bulunduğunu test et
    izolasyon = []

    # TEST 1: Sadece değişkenler + toplam slot (gün tipi kısıtı YOK)
    m1 = cp.CpModel()
    t1 = {}
    for p in self.personel_listesi:
        pid = p.id
        if pid in kilitli_ids:
            kv = sum(p.hedef_tipler.values())
            t1[pid] = m1.NewIntVar(kv, kv, f't1_{pid}')
        else:
            mk = sum(p.musait_tipler.get(tp, 0) for tp in GUN_TIPLERI)
            matched_k = find_matching_id(pid, kisitli_kapasite.keys())
            if matched_k is not None:
                mk = min(mk, kisitli_kapasite[matched_k])
            ub = min(mk, HARD_CAP)
            t1[pid] = m1.NewIntVar(0, ub, f't1_{pid}')
    m1.Add(sum(t1[p.id] for p in self.personel_listesi) == self.toplam_slot)
    s1 = cp.CpSolver()
    s1.parameters.max_time_in_seconds = 5
    st1 = s1.Solve(m1)
    izolasyon.append(f"TEST1_sadece_toplam={'OK' if st1 in [cp.OPTIMAL, cp.FEASIBLE] else 'FAIL'}")

    # TEST 2: Değişkenler + gün tipi kısıtları (toplam slot kısıtı YOK, birlikte YOK)
    m2 = cp.CpModel()
    h2 = {}
    for p in self.personel_listesi:
        pid = p.id
        for tip in GUN_TIPLERI:
            if pid in kilitli_ids:
                val = p.hedef_tipler.get(tip, 0)
                h2[pid, tip] = m2.NewIntVar(val, val, f'h2_{pid}_{tip}')
            else:
                h2[pid, tip] = m2.NewIntVar(0, p.musait_tipler.get(tip, 0), f'h2_{pid}_{tip}')
    for tip in GUN_TIPLERI:
        m2.Add(sum(h2[p.id, tip] for p in self.personel_listesi) == self.tip_slotlari[tip])
    s2 = cp.CpSolver()
    s2.parameters.max_time_in_seconds = 5
    st2 = s2.Solve(m2)
    izolasyon.append(f"TEST2_sadece_guntipi={'OK' if st2 in [cp.OPTIMAL, cp.FEASIBLE] else 'FAIL'}")

    # TEST 3: Değişkenler + gün tipi + HARD_CAP (birlikte ve ceza YOK)
    m3 = cp.CpModel()
    h3 = {}
    t3 = {}
    for p in self.personel_listesi:
        pid = p.id
        if pid in kilitli_ids:
            kv = sum(p.hedef_tipler.values())
            t3[pid] = m3.NewIntVar(kv, kv, f't3_{pid}')
            for tip in GUN_TIPLERI:
                val = p.hedef_tipler.get(tip, 0)
                h3[pid, tip] = m3.NewIntVar(val, val, f'h3_{pid}_{tip}')
        else:
            for tip in GUN_TIPLERI:
                h3[pid, tip] = m3.NewIntVar(0, p.musait_tipler.get(tip, 0), f'h3_{pid}_{tip}')
            mk = sum(p.musait_tipler.get(tp, 0) for tp in GUN_TIPLERI)
            matched_k = find_matching_id(pid, kisitli_kapasite.keys())
            if matched_k is not None:
                mk = min(mk, kisitli_kapasite[matched_k])
            ub = min(mk, HARD_CAP)
            t3[pid] = m3.NewIntVar(0, ub, f't3_{pid}')
            m3.Add(sum(h3[pid, tip] for tip in GUN_TIPLERI) == t3[pid])
    m3.Add(sum(t3[p.id] for p in self.personel_listesi) == self.toplam_slot)
    for tip in GUN_TIPLERI:
        m3.Add(sum(h3[p.id, tip] for p in self.personel_listesi) == self.tip_slotlari[tip])
    s3 = cp.CpSolver()
    s3.parameters.max_time_in_seconds = 5
    st3 = s3.Solve(m3)
    izolasyon.append(f"TEST3_guntipi+hardcap={'OK' if st3 in [cp.OPTIMAL, cp.FEASIBLE] else 'FAIL'}")

    # TEST 4: TEST3 + excess/missing SOFT kısıtları
    m4 = cp.CpModel()
    h4 = {}
    t4 = {}
    pen4 = []
    for p in self.personel_listesi:
        pid = p.id
        if pid in kilitli_ids:
            kv = sum(p.hedef_tipler.values())
            t4[pid] = m4.NewIntVar(kv, kv, f't4_{pid}')
            for tip in GUN_TIPLERI:
                val = p.hedef_tipler.get(tip, 0)
                h4[pid, tip] = m4.NewIntVar(val, val, f'h4_{pid}_{tip}')
        else:
            for tip in GUN_TIPLERI:
                h4[pid, tip] = m4.NewIntVar(0, p.musait_tipler.get(tip, 0), f'h4_{pid}_{tip}')
            mk = sum(p.musait_tipler.get(tp, 0) for tp in GUN_TIPLERI)
            matched_k = find_matching_id(pid, kisitli_kapasite.keys())
            if matched_k is not None:
                mk = min(mk, kisitli_kapasite[matched_k])
            ub = min(mk, HARD_CAP)
            t4[pid] = m4.NewIntVar(0, ub, f't4_{pid}')
            m4.Add(sum(h4[pid, tip] for tip in GUN_TIPLERI) == t4[pid])
            # Soft excess/missing
            exc4 = m4.NewIntVar(0, HARD_CAP, f'exc4_{pid}')
            m4.Add(exc4 >= t4[pid] - (avg_count_floor + 1))
            exc4sq = m4.NewIntVar(0, HARD_CAP*HARD_CAP, f'exc4sq_{pid}')
            m4.AddMultiplicationEquality(exc4sq, [exc4, exc4])
            pen4.append(exc4sq)
    m4.Add(sum(t4[p.id] for p in self.personel_listesi) == self.toplam_slot)
    for tip in GUN_TIPLERI:
        m4.Add(sum(h4[p.id, tip] for p in self.personel_listesi) == self.tip_slotlari[tip])
    m4.Minimize(sum(pen4))
    s4 = cp.CpSolver()
    s4.parameters.max_time_in_seconds = 5
    st4 = s4.Solve(m4)
    izolasyon.append(f"TEST4_+soft_excess={'OK' if st4 in [cp.OPTIMAL, cp.FEASIBLE] else 'FAIL'}")

    # TEST 5: TEST4 + saat dengesi (AddAbsEquality)
    m5 = cp.CpModel()
    h5 = {}
    t5 = {}
    pen5 = []
    for p in self.personel_listesi:
        pid = p.id
        if pid in kilitli_ids:
            kv = sum(p.hedef_tipler.values())
            t5[pid] = m5.NewIntVar(kv, kv, f't5_{pid}')
            for tip in GUN_TIPLERI:
                val = p.hedef_tipler.get(tip, 0)
                h5[pid, tip] = m5.NewIntVar(val, val, f'h5_{pid}_{tip}')
        else:
            for tip in GUN_TIPLERI:
                h5[pid, tip] = m5.NewIntVar(0, p.musait_tipler.get(tip, 0), f'h5_{pid}_{tip}')
            mk = sum(p.musait_tipler.get(tp, 0) for tp in GUN_TIPLERI)
            matched_k = find_matching_id(pid, kisitli_kapasite.keys())
            if matched_k is not None:
                mk = min(mk, kisitli_kapasite[matched_k])
            ub = min(mk, HARD_CAP)
            t5[pid] = m5.NewIntVar(0, ub, f't5_{pid}')
            m5.Add(sum(h5[pid, tip] for tip in GUN_TIPLERI) == t5[pid])
            # Saat dengesi
            th5 = sum(h5[pid, tip] * self.saat[tip] for tip in GUN_TIPLERI)
            hd5 = m5.NewIntVar(0, 200, f'hd5_{pid}')
            m5.AddAbsEquality(hd5, th5 - avg_hours)
            pen5.append(hd5)
    m5.Add(sum(t5[p.id] for p in self.personel_listesi) == self.toplam_slot)
    for tip in GUN_TIPLERI:
        m5.Add(sum(h5[p.id, tip] for p in self.personel_listesi) == self.tip_slotlari[tip])
    m5.Minimize(sum(pen5))
    s5 = cp.CpSolver()
    s5.parameters.max_time_in_seconds = 5
    st5 = s5.Solve(m5)
    izolasyon.append(f"TEST5_+saat_dengesi={'OK' if st5 in [cp.OPTIMAL, cp.FEASIBLE] else 'FAIL'}")

    # TEST 6: TEST5 + WE dengesi (AddAbsEquality)
    m6 = cp.CpModel()
    h6 = {}
    t6 = {}
    pen6 = []
    for p in self.personel_listesi:
        pid = p.id
        if pid in kilitli_ids:
            kv = sum(p.hedef_tipler.values())
            t6[pid] = m6.NewIntVar(kv, kv, f't6_{pid}')
            for tip in GUN_TIPLERI:
                val = p.hedef_tipler.get(tip, 0)
                h6[pid, tip] = m6.NewIntVar(val, val, f'h6_{pid}_{tip}')
        else:
            for tip in GUN_TIPLERI:
                h6[pid, tip] = m6.NewIntVar(0, p.musait_tipler.get(tip, 0), f'h6_{pid}_{tip}')
            mk = sum(p.musait_tipler.get(tp, 0) for tp in GUN_TIPLERI)
            matched_k = find_matching_id(pid, kisitli_kapasite.keys())
            if matched_k is not None:
                mk = min(mk, kisitli_kapasite[matched_k])
            ub = min(mk, HARD_CAP)
            t6[pid] = m6.NewIntVar(0, ub, f't6_{pid}')
            m6.Add(sum(h6[pid, tip] for tip in GUN_TIPLERI) == t6[pid])
            th6 = sum(h6[pid, tip] * self.saat[tip] for tip in GUN_TIPLERI)
            hd6 = m6.NewIntVar(0, 200, f'hd6_{pid}')
            m6.AddAbsEquality(hd6, th6 - avg_hours)
            pen6.append(hd6)
            # WE dengesi
            we6 = sum(h6[pid, tip] for tip in we_tipleri)
            wed6 = m6.NewIntVar(0, 5000, f'wed6_{pid}')
            m6.AddAbsEquality(wed6, we6 * self.toplam_slot - t6[pid] * total_we_slots)
            pen6.append(wed6)
    m6.Add(sum(t6[p.id] for p in self.personel_listesi) == self.toplam_slot)
    for tip in GUN_TIPLERI:
        m6.Add(sum(h6[p.id, tip] for p in self.personel_listesi) == self.tip_slotlari[tip])
    m6.Minimize(sum(pen6))
    s6 = cp.CpSolver()
    s6.parameters.max_time_in_seconds = 5
    st6 = s6.Solve(m6)
    izolasyon.append(f"TEST6_+we_dengesi={'OK' if st6 in [cp.OPTIMAL, cp.FEASIBLE] else 'FAIL'}")

    # Kişi bazlı h domain analizi (her kişinin h üst sınırları toplamı vs HARD_CAP)
    kisi_debug = []
    for p in self.personel_listesi:
        pid = p.id
        if pid in kilitli_ids:
            continue
        h_sum_ub = sum(p.musait_tipler.get(tip, 0) for tip in GUN_TIPLERI)
        mk = h_sum_ub
        matched_k = find_matching_id(pid, kisitli_kapasite.keys())
        if matched_k is not None:
            mk = min(mk, kisitli_kapasite[matched_k])
        ub = min(mk, HARD_CAP)
        # h değişkenlerinin toplam alt sınırı vs üst sınırı
        h_lb_total = sum(manuel_sayac[pid].get(tip, 0) for tip in GUN_TIPLERI)
        kisi_debug.append(f"{p.ad}:ub={ub},h_ub={h_sum_ub},lb={h_lb_total},mzrt={len(p.mazeret_gunleri)},musait={dict(p.musait_tipler)}")

    debug_info.append(f"IZOLASYON: {' | '.join(izolasyon)}")
    # İlk 15 kişiyi göster
    debug_info.append(f"KISI_DETAY({len(kisi_debug)}): " + " ; ".join(kisi_debug[:15]))

    if darbogazlar:
        debug_info.append(f"darbogazlar ({len(darbogazlar)}): " + "; ".join(darbogazlar[:10]))

    debug_msg = " | ".join(debug_info)
    return debug_msg
