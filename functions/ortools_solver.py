"""
OR-Tools CP-SAT Nobet Cozucu v4.2
Gorev kotalari + Gun tipi kotalari dahil
"""

from typing import List, Dict, Set
import time
import math

from utils import (
    GUN_TIPLERI, SAAT_DEGERLERI,
    find_matching_id,
)
from solver_models import (
    SolverPersonel, SolverGorev, SolverKural, SolverAtama,
    SolverSonuc,
    WEIGHT_GOREV_KOTA, WEIGHT_GUN_TIPI, WEIGHT_YILLIK,
    WEIGHT_HOMOJEN, WEIGHT_PANIK, WEIGHT_TOPLAM, WEIGHT_BIRLIKTE,
)

# Lazy import for ortools (Firebase deploy timeout fix) — thread-safe
import threading

_cp_model_lock = threading.Lock()
_cp_model_module = None

def _get_cp_model():
    global _cp_model_module
    if _cp_model_module is None:
        with _cp_model_lock:
            if _cp_model_module is None:
                from ortools.sat.python import cp_model as _cm
                _cp_model_module = _cm
    return _cp_model_module


class NobetSolver:
    def __init__(self, gun_sayisi: int, gun_tipleri: Dict[int, str],
                 personeller: List[SolverPersonel], gorevler: List[SolverGorev],
                 kurallar: List[SolverKural] = None,
                 gorev_havuzlari: Dict[str, Set[int]] = None,
                 kisitlama_istisnalari: List[Dict] = None,
                 birlikte_istisnalari: List[Dict] = None,
                 aragun_istisnalari: List[Dict] = None,
                 manuel_atamalar: List[SolverAtama] = None,
                 hedefler: Dict[int, Dict] = None,
                 ara_gun: int = 2, max_sure_saniye: int = 300):
        self.gun_sayisi = gun_sayisi
        self.gun_tipleri = gun_tipleri
        self.personeller = {p.id: p for p in personeller}
        self.personel_listesi = personeller
        self.gorevler = gorevler
        self.kurallar = kurallar or []
        self.gorev_havuzlari = gorev_havuzlari or {}
        self.kisitlama_istisnalari = kisitlama_istisnalari or []
        self.manuel_atamalar = manuel_atamalar or []
        self.hedefler = hedefler or {}
        self.ara_gun = ara_gun
        self.max_sure = max_sure_saniye
        self.slot_sayisi = len(gorevler)
        
        self.gunler_by_tip = {t: [] for t in GUN_TIPLERI}
        for g, tip in gun_tipleri.items():
            if tip in self.gunler_by_tip:
                self.gunler_by_tip[tip].append(g)
        
        self.role_slots = {}
        for s, gorev in enumerate(gorevler):
            base = gorev.base_name if gorev.base_name else gorev.ad
            if base not in self.role_slots:
                self.role_slots[base] = []
            self.role_slots[base].append(s)

        # Role bazli havuz ID'lerini mevcut personel ID'lerine normalize et
        normalized_havuzlar = {}
        for role, raw_ids in self.gorev_havuzlari.items():
            matched_ids = set()
            for pid in raw_ids or []:
                matched_id = find_matching_id(pid, self.personeller.keys())
                if matched_id is not None:
                    matched_ids.add(matched_id)
            if matched_ids:
                normalized_havuzlar[role] = matched_ids
        self.gorev_havuzlari = normalized_havuzlar

        # Kisitlama istisnalari: (personel_id, gun) -> {gorev_adi1, gorev_adi2}
        self.kisitlama_istisna_map = {}
        self.kisitlama_istisna_debug = {"ham_sayi": len(self.kisitlama_istisnalari), "gecerli_sayi": 0}
        for raw in self.kisitlama_istisnalari:
            raw_pid = raw.get("personel_id")
            gun = int(raw.get("gun", 0) or 0)
            istisna_gorev = raw.get("istisna_gorev")
            matched_id = find_matching_id(raw_pid, self.personeller.keys())
            if matched_id is None or gun < 1 or gun > self.gun_sayisi or not istisna_gorev:
                continue
            key = (matched_id, gun)
            if key not in self.kisitlama_istisna_map:
                self.kisitlama_istisna_map[key] = set()
            self.kisitlama_istisna_map[key].add(istisna_gorev)
        self.kisitlama_istisna_debug["gecerli_sayi"] = sum(
            len(v) for v in self.kisitlama_istisna_map.values()
        )

        # Birlikte istisnalari: (personel_id, gun) set
        self.birlikte_istisna_set = set()
        for raw in (birlikte_istisnalari or []):
            raw_pid = raw.get("personel_id")
            gun = int(raw.get("gun", 0) or 0)
            matched_id = find_matching_id(raw_pid, self.personeller.keys())
            if matched_id is not None and 1 <= gun <= self.gun_sayisi:
                self.birlikte_istisna_set.add((matched_id, gun))

        # Ara gun istisnalari: (personel_id, gun1, gun2) set
        self.aragun_istisna_set = set()
        for raw in (aragun_istisnalari or []):
            raw_pid = raw.get("personel_id")
            gun1 = int(raw.get("gun1", 0) or 0)
            gun2 = int(raw.get("gun2", 0) or 0)
            matched_id = find_matching_id(raw_pid, self.personeller.keys())
            if matched_id is not None and gun1 >= 1 and gun2 >= 1:
                g1, g2 = min(gun1, gun2), max(gun1, gun2)
                self.aragun_istisna_set.add((matched_id, g1, g2))
        
        # Slot kıtlık ağırlığı: Az slotlu görevler daha önemli
        # max_slot / slot_sayisi formülü ile hesapla
        max_slot = max(len(slots) for slots in self.role_slots.values()) if self.role_slots else 1
        self.slot_agirliklari = {}
        for base_name, slots in self.role_slots.items():
            # Örn: max=3, KVC=1 slot → ağırlık=3, AMELİYATHANE=3 slot → ağırlık=1
            self.slot_agirliklari[base_name] = max(1, max_slot // len(slots))
        
        for p in personeller:
            p.musait_tipler = {t: 0 for t in GUN_TIPLERI}
            p.musait_gunler = set()
            for g, tip in gun_tipleri.items():
                if g not in p.mazeret_gunleri:
                    p.musait_tipler[tip] += 1
                    p.musait_gunler.add(g)

    def _role_name_by_slot(self, slot_idx: int) -> str:
        if slot_idx < 0 or slot_idx >= len(self.gorevler):
            return ""
        gorev = self.gorevler[slot_idx]
        return gorev.base_name if gorev.base_name else gorev.ad

    def _manual_hard_conflict_diagnostics(self) -> List[Dict]:
        """Model kurulmadan önce hard çakışmaları yakala."""
        conflicts = []

        ayri_pairs = []
        for kural in self.kurallar:
            if kural.tur != 'ayri':
                continue
            valid_ids = []
            for pid in kural.kisiler:
                matched = find_matching_id(pid, self.personeller.keys())
                if matched is not None:
                    valid_ids.append(matched)
            if len(valid_ids) >= 2:
                for i, p1 in enumerate(valid_ids):
                    for p2 in valid_ids[i + 1:]:
                        ayri_pairs.append((p1, p2))

        birlikte_uye_ids = set()
        for kural in self.kurallar:
            if kural.tur != 'birlikte':
                continue
            for pid in kural.kisiler:
                matched = find_matching_id(pid, self.personeller.keys())
                if matched is not None:
                    birlikte_uye_ids.add(matched)

        ayri_bina_slotlar = set(
            s for s, gorev in enumerate(self.gorevler)
            if getattr(gorev, 'ayri_bina', False)
        )

        exclusive_gorevler = set()
        for gorev in self.gorevler:
            if gorev.exclusive:
                base = gorev.base_name if gorev.base_name else gorev.ad
                if base not in self.gorev_havuzlari:
                    exclusive_gorevler.add(base)

        per_person_day = {}
        per_slot_day = {}
        manual_days = {}

        for m in self.manuel_atamalar:
            pid = find_matching_id(m.personel_id, self.personeller.keys())
            if pid is None:
                conflicts.append({
                    "code": "MANUEL_KISI_YOK",
                    "mesaj": f"Manuel atama personeli bulunamadi: {m.personel_id}",
                    "personel_id": m.personel_id,
                    "gun": m.gun,
                    "slot_idx": m.slot_idx
                })
                continue

            if not (1 <= m.gun <= self.gun_sayisi):
                conflicts.append({
                    "code": "MANUEL_GUN_HATALI",
                    "mesaj": f"Manuel atama gun aralik disi: {m.gun}",
                    "personel_id": pid,
                    "gun": m.gun,
                    "slot_idx": m.slot_idx
                })
                continue

            if not (0 <= m.slot_idx < self.slot_sayisi):
                conflicts.append({
                    "code": "MANUEL_SLOT_HATALI",
                    "mesaj": f"Manuel atama slot aralik disi: {m.slot_idx}",
                    "personel_id": pid,
                    "gun": m.gun,
                    "slot_idx": m.slot_idx
                })
                continue

            p = self.personeller[pid]
            role = self._role_name_by_slot(m.slot_idx)

            per_person_day[(pid, m.gun)] = per_person_day.get((pid, m.gun), 0) + 1
            per_slot_day[(m.gun, m.slot_idx)] = per_slot_day.get((m.gun, m.slot_idx), 0) + 1
            manual_days.setdefault(pid, []).append(m.gun)

            if m.gun in p.mazeret_gunleri:
                conflicts.append({
                    "code": "MAZERET_GUNU",
                    "mesaj": f"{p.ad} mazeretli oldugu gun manuel atama almis",
                    "personel_id": pid,
                    "personel_ad": p.ad,
                    "gun": m.gun,
                    "gorev": role
                })

            allowed_exception_roles = self.kisitlama_istisna_map.get((pid, m.gun), set())
            tasma_ok = p.tasma_gorevi and role == p.tasma_gorevi
            if p.kisitli_gorev and role != p.kisitli_gorev and not tasma_ok and role not in allowed_exception_roles:
                conflicts.append({
                    "code": "KISITLAMA_IHLALI",
                    "mesaj": f"{p.ad} kisitli gorevi disinda manuel atama almis",
                    "personel_id": pid,
                    "personel_ad": p.ad,
                    "gun": m.gun,
                    "kisitli_gorev": p.kisitli_gorev,
                    "gorev": role
                })

            if role in exclusive_gorevler and p.kisitli_gorev != role:
                # YENI KURAL: Eger bu kisiye bu gorev icin hedef kota verilmisse exclusive bloklamasini gec
                hedef = self.hedefler.get(pid, {})
                gorev_kotalari = hedef.get('gorev_kotalari', {})
                if gorev_kotalari.get(role, 0) == 0:
                    conflicts.append({
                        "code": "EXCLUSIVE_IHLALI",
                        "mesaj": f"{p.ad} exclusive goreve manuel atanmis",
                        "personel_id": pid,
                        "personel_ad": p.ad,
                        "gun": m.gun,
                        "gorev": role
                    })

            if role in self.gorev_havuzlari and pid not in self.gorev_havuzlari[role]:
                # Bug fix: kısıtlı kişiler veya taşma görevi olan kişiler havuz dışı sayılmaz
                if p.kisitli_gorev != role and (not p.tasma_gorevi or p.tasma_gorevi != role):
                    # YENI KURAL: Eger bu kisiye bu gorev icin hedef kota verilmisse havuz bloklamasini gec
                    hedef = self.hedefler.get(pid, {})
                    gorev_kotalari = hedef.get('gorev_kotalari', {})
                    if gorev_kotalari.get(role, 0) == 0:
                        conflicts.append({
                            "code": "HAVUZ_IHLALI",
                            "mesaj": f"{p.ad} gorev havuzu disinda manuel atanmis",
                            "personel_id": pid,
                            "personel_ad": p.ad,
                            "gun": m.gun,
                            "gorev": role
                        })

            if m.slot_idx in ayri_bina_slotlar and pid in birlikte_uye_ids:
                if (pid, m.gun) not in self.birlikte_istisna_set:
                    # YENI KURAL: Manuel atamalarda birlikte uyesi ayri binaya atanirsa otomatik istisna kabul et
                    self.birlikte_istisna_set.add((pid, m.gun))

        for (pid, gun), cnt in per_person_day.items():
            if cnt > 1:
                p = self.personeller.get(pid)
                conflicts.append({
                    "code": "AYNI_GUN_CIFT_ATAMA",
                    "mesaj": f"{p.ad if p else pid} ayni gun birden fazla manuel atama almis",
                    "personel_id": pid,
                    "personel_ad": p.ad if p else "",
                    "gun": gun,
                    "adet": cnt
                })

        for (gun, slot_idx), cnt in per_slot_day.items():
            if cnt > 1:
                conflicts.append({
                    "code": "AYNI_SLOT_CIFT_ATAMA",
                    "mesaj": f"{gun}. gun {slot_idx}. slot birden fazla manuel atama iceriyor",
                    "gun": gun,
                    "slot_idx": slot_idx,
                    "adet": cnt
                })

        for pid, gunler in manual_days.items():
            gunler = sorted(gunler)
            for i in range(len(gunler) - 1):
                g1, g2 = gunler[i], gunler[i + 1]
                if g2 - g1 <= self.ara_gun:
                    if (pid, g1, g2) not in self.aragun_istisna_set:
                        # YENI KURAL: Manuel atamalarda ara gun ihlali otomatik olarak kullanicinin onayi sayilsin
                        self.aragun_istisna_set.add((pid, g1, g2))

        # Ayrı kuralı: aynı gün iki kişi de manuel atanmış mı?
        daily_manual_people = {}
        for (pid, gun), cnt in per_person_day.items():
            if cnt > 0:
                if gun not in daily_manual_people:
                    daily_manual_people[gun] = set()
                daily_manual_people[gun].add(pid)

        for gun, pid_set in daily_manual_people.items():
            for p1, p2 in ayri_pairs:
                if p1 in pid_set and p2 in pid_set:
                    n1 = self.personeller[p1].ad if p1 in self.personeller else str(p1)
                    n2 = self.personeller[p2].ad if p2 in self.personeller else str(p2)
                    conflicts.append({
                        "code": "AYRI_KURALI_IHLALI",
                        "mesaj": f"{n1} ve {n2} ayni gun manuel atanmis (ayri kurali)",
                        "gun": gun,
                        "personel1_id": p1,
                        "personel2_id": p2
                    })

        return conflicts

    def _exclusive_roles_without_pool(self) -> Set[str]:
        roles = set()
        for gorev in self.gorevler:
            if gorev.exclusive:
                base = gorev.base_name if gorev.base_name else gorev.ad
                if base not in self.gorev_havuzlari:
                    roles.add(base)
        return roles

    def _birlikte_uye_ids(self) -> Set[int]:
        ids = set()
        for kural in self.kurallar:
            if kural.tur != 'birlikte':
                continue
            for raw_pid in kural.kisiler:
                matched_pid = find_matching_id(raw_pid, self.personeller.keys())
                if matched_pid is not None:
                    ids.add(matched_pid)
        return ids

    def _person_can_take_slot_on_day(self, pid: int, slot_idx: int, gun: int,
                                     exclusive_roles: Set[str],
                                     birlikte_uye_ids: Set[int]) -> bool:
        p = self.personeller.get(pid)
        if p is None:
            return False
        if gun in p.mazeret_gunleri:
            return False
        if slot_idx < 0 or slot_idx >= self.slot_sayisi:
            return False

        role = self._role_name_by_slot(slot_idx)
        allowed_exception_roles = self.kisitlama_istisna_map.get((pid, gun), set())

        # H7: Kısıtlı görev kuralı (taşma görevi de izinli)
        if p.kisitli_gorev and role != p.kisitli_gorev and role not in allowed_exception_roles:
            if not (p.tasma_gorevi and role == p.tasma_gorevi):
                return False

        # H8: Exclusive görevler (havuzsuz) - taşma görevi olan kişi de girebilir
        if role in exclusive_roles and p.kisitli_gorev != role and p.tasma_gorevi != role:
            return False

        # H10: Görev havuzu
        allowed_ids = self.gorev_havuzlari.get(role)
        if allowed_ids is not None and pid not in allowed_ids:
            # Bug fix: kısıtlı veya taşma görevi olan kişiler havuz dışı sayılmaz
            if not (p.kisitli_gorev and p.kisitli_gorev == role):
                if not (p.tasma_gorevi and p.tasma_gorevi == role):
                    return False

        # H9: Ayrı bina + birlikte üyesi → eliminasyon KALDIRILDI
        # Birlikte üyeleri artık ayrı bina slotları için aday olabilir.
        # Limit kontrolü H9 hard constraint'inde yapılır:
        # ayri_bina_max = hedef - ceil(hedef/2)

        return True

    def _max_assignable_with_ara_gun(self, gunler: List[int]) -> int:
        if not gunler:
            return 0
        secilen = 0
        son_gun = -10_000
        for g in sorted(gunler):
            if g - son_gun > self.ara_gun:
                secilen += 1
                son_gun = g
        return secilen

    def _build_feasibility_diagnostics(self, limit_preview: int = 60) -> Dict:
        """Hard kısıtlara göre hızlı feasibility ipuçları üret. Sonucu cache'ler."""
        cache_key = f"_feasibility_cache_{limit_preview}"
        if hasattr(self, cache_key):
            return getattr(self, cache_key)

        exclusive_roles = self._exclusive_roles_without_pool()
        birlikte_uye_ids = self._birlikte_uye_ids()

        zero_slot_days = []
        role_summaries = []

        # slot/day bazlı adaylar
        slot_day_candidates = {}
        for s in range(self.slot_sayisi):
            for g in range(1, self.gun_sayisi + 1):
                cands = [
                    p.id for p in self.personel_listesi
                    if self._person_can_take_slot_on_day(p.id, s, g, exclusive_roles, birlikte_uye_ids)
                ]
                slot_day_candidates[(s, g)] = cands
                if len(cands) == 0 and len(zero_slot_days) < limit_preview:
                    zero_slot_days.append({
                        "gun": g,
                        "slot_idx": s,
                        "gorev": self._role_name_by_slot(s)
                    })

        # role bazlı özet
        for role, slot_list in self.role_slots.items():
            demand = self.gun_sayisi * len(slot_list)
            role_daily_union = {}
            role_daily_short = []

            for g in range(1, self.gun_sayisi + 1):
                union_ids = set()
                for s in slot_list:
                    union_ids.update(slot_day_candidates.get((s, g), []))
                role_daily_union[g] = union_ids
                if len(union_ids) < len(slot_list) and len(role_daily_short) < limit_preview:
                    role_daily_short.append({
                        "gun": g,
                        "gerekli_kisi": len(slot_list),
                        "aday_kisi": len(union_ids)
                    })

            # Ara-gün etkili üst kapasite (kişi bazlı üst sınır toplamı)
            ara_gun_upper_capacity = 0
            for p in self.personel_listesi:
                uygun_gunler = [g for g in range(1, self.gun_sayisi + 1) if p.id in role_daily_union[g]]
                ara_gun_upper_capacity += self._max_assignable_with_ara_gun(uygun_gunler)

            if demand > ara_gun_upper_capacity:
                role_summaries.append({
                    "gorev": role,
                    "slot_sayisi": len(slot_list),
                    "talep": demand,
                    "ara_gun_ust_kapasite": ara_gun_upper_capacity,
                    "eksik": demand - ara_gun_upper_capacity,
                    "gunluk_aday_yetersiz_preview": role_daily_short[:10]
                })

        result = {
            "slot_day_zero_candidate_count": sum(
                1 for (_, _), cands in slot_day_candidates.items() if len(cands) == 0
            ),
            "slot_day_zero_candidate_preview": zero_slot_days,
            "role_ara_gun_capacity_issues": role_summaries[:limit_preview]
        }
        setattr(self, f"_feasibility_cache_{limit_preview}", result)
        return result

    def _diagnose_infeasible(self, diagnostics: Dict) -> 'List[Dict]':
        """INFEASIBLE nedenini analiz et, akıllı gevşetme aksiyonları öner.

        Mevcut diagnostics verisine bakarak kök nedeni tespit eder ve
        en etkili gevşetme sırasını döndürür.

        Returns: Sıralı aksiyon listesi, ör:
        [
            {'aksiyon': 'ara_gun_azalt', 'oncelik': 1, 'neden': '...', 'puan': 90},
            {'aksiyon': 'exclusive_gevset', 'oncelik': 2, 'neden': '...', 'puan': 70},
        ]
        """
        aksiyonlar = []
        zero_count = diagnostics.get('slot_day_zero_candidate_count', 0)
        zero_preview = diagnostics.get('slot_day_zero_candidate_preview', [])
        capacity_issues = diagnostics.get('role_ara_gun_capacity_issues', [])
        toplam_slot_gun = self.gun_sayisi * self.slot_sayisi

        # --- KURAL 1: Ara gün kapasite sorunu ---
        # role_ara_gun_capacity_issues varsa, ara gün azaltmak en etkili çözüm
        if capacity_issues:
            toplam_eksik = sum(r.get('eksik', 0) for r in capacity_issues)
            etkilenen_gorevler = [r['gorev'] for r in capacity_issues]
            aksiyonlar.append({
                'aksiyon': 'ara_gun_azalt',
                'puan': 95,  # Çok yüksek öncelik
                'neden': (
                    f"Ara gun kapasite sorunu: {len(capacity_issues)} gorevde "
                    f"toplam {toplam_eksik} atama eksik. "
                    f"Etkilenen gorevler: {', '.join(etkilenen_gorevler[:5])}"
                ),
                'detay': {
                    'etkilenen_gorevler': etkilenen_gorevler,
                    'toplam_eksik': toplam_eksik
                }
            })

        # --- KURAL 2: Exclusive darboğaz ---
        # zero_candidate slotların çoğu exclusive görevlerdeyse
        exclusive_roles = self._exclusive_roles_without_pool()
        if zero_preview and exclusive_roles:
            exclusive_zero = sum(
                1 for z in zero_preview
                if z.get('gorev', '') in exclusive_roles
            )
            exclusive_orani = exclusive_zero / max(len(zero_preview), 1)

            if exclusive_orani > 0.3 or exclusive_zero > 5:
                # Exclusive görevler için kapasite analizi
                exclusive_kapasite = {}
                for role in exclusive_roles:
                    kisitli_kisiler = [
                        p for p in self.personel_listesi
                        if p.kisitli_gorev == role or p.tasma_gorevi == role
                    ]
                    role_slot_count = len(self.role_slots.get(role, []))
                    talep = self.gun_sayisi * role_slot_count
                    musait_gunler = sum(
                        len(p.musait_gunler) for p in kisitli_kisiler
                    )
                    exclusive_kapasite[role] = {
                        'kisitli_kisi': len(kisitli_kisiler),
                        'slot_sayisi': role_slot_count,
                        'talep': talep,
                        'toplam_musait_gun': musait_gunler
                    }

                aksiyonlar.append({
                    'aksiyon': 'exclusive_gevset',
                    'puan': 85 if exclusive_orani > 0.5 else 70,
                    'neden': (
                        f"Exclusive darbogaz: {exclusive_zero}/{len(zero_preview)} "
                        f"bos slot exclusive gorevlerde. "
                        f"Exclusive roller: {', '.join(list(exclusive_roles)[:5])}"
                    ),
                    'detay': {
                        'exclusive_zero': exclusive_zero,
                        'toplam_zero': len(zero_preview),
                        'oran': round(exclusive_orani, 2),
                        'kapasite': exclusive_kapasite
                    }
                })

        # --- KURAL 3: Ayrı tutma kuralları çakışması ---
        # Çok sayıda ayrı kuralı + yüksek mazeret → kullanılabilir gün azalır
        ayri_kurallari = [k for k in self.kurallar if k.tur == 'ayri']
        if ayri_kurallari:
            # Ayrı kurallarının etki alanını hesapla
            ayri_kisi_ids = set()
            for k in ayri_kurallari:
                for pid in k.kisiler:
                    matched = find_matching_id(pid, self.personeller.keys())
                    if matched is not None:
                        ayri_kisi_ids.add(matched)

            # Etkilenen kişilerin ortalama müsait gün sayısı
            if ayri_kisi_ids:
                ort_musait = sum(
                    len(self.personeller[pid].musait_gunler)
                    for pid in ayri_kisi_ids
                    if pid in self.personeller
                ) / max(len(ayri_kisi_ids), 1)

                # Çok fazla kişi ayrı tutuluyorsa ve müsait gün azsa
                etki_skoru = len(ayri_kisi_ids) * (self.gun_sayisi - ort_musait)
                if etki_skoru > self.gun_sayisi * 2 or len(ayri_kurallari) > 3:
                    aksiyonlar.append({
                        'aksiyon': 'ayri_gevset',
                        'puan': 65,
                        'neden': (
                            f"Ayri tutma cakismasi: {len(ayri_kurallari)} ayri kurali "
                            f"{len(ayri_kisi_ids)} kisiyi etkiliyor, "
                            f"ort musait gun: {ort_musait:.0f}/{self.gun_sayisi}"
                        ),
                        'detay': {
                            'kural_sayisi': len(ayri_kurallari),
                            'etkilenen_kisi': len(ayri_kisi_ids),
                            'ort_musait_gun': round(ort_musait, 1)
                        }
                    })

        # --- KURAL 4: Birlikte kuralları (genellikle sorun değil ama bazen) ---
        birlikte_kurallari = [k for k in self.kurallar if k.tur == 'birlikte']
        if birlikte_kurallari:
            aksiyonlar.append({
                'aksiyon': 'birlikte_kaldir',
                'puan': 50,
                'neden': (
                    f"{len(birlikte_kurallari)} birlikte kurali var, "
                    "bunlar model karmasikligini artirabilir"
                ),
                'detay': {'kural_sayisi': len(birlikte_kurallari)}
            })

        # --- KURAL 5: Genel kapasite krizi ---
        # Çok fazla zero-candidate varsa durumu çok kötü
        if zero_count > toplam_slot_gun * 0.3:
            aksiyonlar.append({
                'aksiyon': 'tum_soft_kaldir',
                'puan': 40,
                'neden': (
                    f"Genel kapasite krizi: {zero_count}/{toplam_slot_gun} "
                    f"slot/gun ciftinde hic aday yok (%{round(100*zero_count/max(toplam_slot_gun,1))})"
                ),
                'detay': {'zero_count': zero_count, 'toplam': toplam_slot_gun}
            })

        # --- Her zaman en sonda: Greedy fallback ---
        aksiyonlar.append({
            'aksiyon': 'greedy',
            'puan': 10,
            'neden': 'Son care: Greedy algoritma ile cozum uret',
            'detay': {}
        })

        # Ara gün azalt yoksa ekle (her zaman denenebilir)
        if not any(a['aksiyon'] == 'ara_gun_azalt' for a in aksiyonlar):
            aksiyonlar.insert(0, {
                'aksiyon': 'ara_gun_azalt',
                'puan': 60,
                'neden': 'Ara gun azaltma her zaman denenebilir',
                'detay': {}
            })

        # tum_soft_kaldir yoksa ekle (greedy'den önce)
        if not any(a['aksiyon'] == 'tum_soft_kaldir' for a in aksiyonlar):
            aksiyonlar.insert(-1, {
                'aksiyon': 'tum_soft_kaldir',
                'puan': 30,
                'neden': 'Tum soft kisitlari kaldirarak dene',
                'detay': {}
            })

        # Puana göre sırala (yüksek puan = önce dene)
        aksiyonlar.sort(key=lambda a: a['puan'], reverse=True)

        # Öncelik numarası ekle
        for i, a in enumerate(aksiyonlar):
            a['oncelik'] = i + 1

        return aksiyonlar

    def _hesapla_kalite_skoru(self, kisi_sayac: Dict, atamalar: List[Dict],
                              toplam_atama: int, toplam_slot: int) -> Dict:
        """Çözüm kalitesi metrikleri hesapla"""
        nobet_sayilari = [k['toplam'] for k in kisi_sayac.values()]
        ortalama = sum(nobet_sayilari) / len(nobet_sayilari) if nobet_sayilari else 0
        max_nobet = max(nobet_sayilari) if nobet_sayilari else 0
        min_nobet = min(nobet_sayilari) if nobet_sayilari else 0

        # 1. Denge puanı: max-min farkının ortalamaya oranı (düşük = iyi)
        denge_puani = round(
            (max_nobet - min_nobet) / ortalama * 100, 1
        ) if ortalama > 0 else 0

        # 2. Saat adaleti: saat dağılımının standart sapması
        saat_listesi = []
        for pid, sayac in kisi_sayac.items():
            toplam_saat = sum(
                sayac['tipler'].get(tip, 0) * SAAT_DEGERLERI.get(tip, 8)
                for tip in GUN_TIPLERI
            )
            saat_listesi.append(toplam_saat)
        ortalama_saat = sum(saat_listesi) / len(saat_listesi) if saat_listesi else 0
        saat_varyans = sum((s - ortalama_saat) ** 2 for s in saat_listesi) / len(saat_listesi) if saat_listesi else 0
        saat_std = math.sqrt(saat_varyans)
        saat_adaleti = round(
            saat_std / ortalama_saat * 100, 1
        ) if ortalama_saat > 0 else 0

        # 3. Homojenlik: nöbet aralıklarının standart sapması
        aralik_listesi = []
        for pid, sayac in kisi_sayac.items():
            kisi_gunleri = sorted(
                a['gun'] for a in atamalar if a['personel_id'] == pid
            )
            if len(kisi_gunleri) >= 2:
                araliklar = [kisi_gunleri[i+1] - kisi_gunleri[i]
                             for i in range(len(kisi_gunleri) - 1)]
                aralik_listesi.extend(araliklar)
        if aralik_listesi:
            aralik_ort = sum(aralik_listesi) / len(aralik_listesi)
            aralik_var = sum((a - aralik_ort) ** 2 for a in aralik_listesi) / len(aralik_listesi)
            homojenlik = round(math.sqrt(aralik_var), 2)
        else:
            homojenlik = 0

        # 4. Doluluk yüzdesi
        doluluk = round(100 * toplam_atama / toplam_slot, 1) if toplam_slot > 0 else 0

        # 5. Hedef uyumu: hedeften sapma yüzdesi
        hedef_sapmalar = []
        for p in self.personel_listesi:
            hedef = self.hedefler.get(p.id, {})
            hedef_toplam = hedef.get('hedef_toplam', 0)
            gerceklesen = kisi_sayac.get(p.id, {}).get('toplam', 0)
            if hedef_toplam > 0:
                sapma = abs(gerceklesen - hedef_toplam) / hedef_toplam
                hedef_sapmalar.append(sapma)
        kural_uyumu = round(
            (1 - sum(hedef_sapmalar) / len(hedef_sapmalar)) * 100, 1
        ) if hedef_sapmalar else 100

        return {
            'denge_puani': denge_puani,
            'saat_adaleti': saat_adaleti,
            'homojenlik': homojenlik,
            'doluluk': doluluk,
            'kural_uyumu': kural_uyumu
        }

    def coz(self) -> SolverSonuc:
        baslangic = time.time()
        cp = _get_cp_model()
        model = cp.CpModel()

        manual_conflicts = self._manual_hard_conflict_diagnostics()
        if manual_conflicts:
            sure_ms = int((time.time() - baslangic) * 1000)
            preview = manual_conflicts[:50]
            return SolverSonuc(
                basarili=False,
                atamalar=[],
                istatistikler={
                    'status': 'MANUAL_CONFLICT',
                    'manual_conflict_count': len(manual_conflicts),
                    'manual_conflicts': preview,
                    'ara_gun': self.ara_gun,
                    'ara_gun_1_dene': False,
                    'kisitlama_istisna_debug': self.kisitlama_istisna_debug,
                    'feasibility_debug': self._build_feasibility_diagnostics(limit_preview=40)
                },
                sure_ms=sure_ms,
                mesaj=f"Manuel atamalarda hard kisit cakismasi var ({len(manual_conflicts)} adet)"
            )
        
        # Pre-compute impossible slot assignments for each person
        exclusive_roles = self._exclusive_roles_without_pool()
        birlikte_uye_ids = self._birlikte_uye_ids()

        # Pre-compute: hedef toplam 0 olan kişiler hiçbir yere atanamaz
        sifir_hedef_ids = set()
        for p in self.personel_listesi:
            hedef = self.hedefler.get(p.id, {})
            if hedef.get('hedef_toplam', 0) == 0:
                sifir_hedef_ids.add(p.id)

        x = {}
        eliminated_vars = 0
        for p in self.personel_listesi:
            for g in range(1, self.gun_sayisi + 1):
                if g in p.mazeret_gunleri or p.id in sifir_hedef_ids:
                    # Mazeret günlerinde veya hedefi 0 ise değişken oluşturma - sabit 0
                    for s in range(self.slot_sayisi):
                        x[p.id, g, s] = model.NewConstant(0)
                        eliminated_vars += 1
                else:
                    for s in range(self.slot_sayisi):
                        # Role-based elimination: impossible by role constraints
                        if not self._person_can_take_slot_on_day(p.id, s, g, exclusive_roles, birlikte_uye_ids):
                            x[p.id, g, s] = model.NewConstant(0)
                            eliminated_vars += 1
                        else:
                            x[p.id, g, s] = model.NewBoolVar(f'x_{p.id}_{g}_{s}')
        
        # H1. Her slot EN FAZLA 1 kişi olsun, boş kalırsa ceza (SOFT)
        bos_slotlar = []
        for g in range(1, self.gun_sayisi + 1):
            for s in range(self.slot_sayisi):
                atama_toplami = sum(x[p.id, g, s] for p in self.personel_listesi)
                model.Add(atama_toplami <= 1)  # 1'den fazla olamaz
                
                # Boş kalırsa ceza
                bos_mu = model.NewBoolVar(f'bos_{g}_{s}')
                model.Add(atama_toplami == 0).OnlyEnforceIf(bos_mu)
                model.Add(atama_toplami == 1).OnlyEnforceIf(bos_mu.Not())
                bos_slotlar.append(bos_mu)
        
        # H2. Mazeret — eliminasyonda zaten 0'a sabitlendi, ek constraint gereksiz
        # (Değişken eliminasyonu aşamasında mazeret günleri NewConstant(0) yapıldı)
        
        # H3. Ayni gun tek slot
        for p in self.personel_listesi:
            for g in range(1, self.gun_sayisi + 1):
                model.Add(sum(x[p.id, g, s] for s in range(self.slot_sayisi)) <= 1)
        
        # H4. Ara gun - Herkes için minimum ara gün (HARD)
        # Temel kural: En az 1 gün ara (aynı gün veya ardışık gün olmaz)
        for p in self.personel_listesi:
            if p.id in sifir_hedef_ids:
                continue  # Hedefi 0 olan kişiler zaten eliminate edildi
            for g1 in range(1, self.gun_sayisi + 1):
                if g1 in p.mazeret_gunleri:
                    continue  # Mazeret günü zaten 0, constraint gereksiz
                for g2 in range(g1 + 1, min(g1 + self.ara_gun + 1, self.gun_sayisi + 1)):
                    if g2 in p.mazeret_gunleri:
                        continue  # Mazeret günü zaten 0, constraint gereksiz
                    if (p.id, g1, g2) not in self.aragun_istisna_set:
                        model.Add(
                            sum(x[p.id, g1, s] for s in range(self.slot_sayisi)) +
                            sum(x[p.id, g2, s] for s in range(self.slot_sayisi)) <= 1
                        )
        
        # H5. Ayri tutma
        for kural in self.kurallar:
            if kural.tur == 'ayri':
                # Normalize edilmiş ID eşleştirme
                valid_ids = []
                for pid in kural.kisiler:
                    matched_id = find_matching_id(pid, self.personeller.keys())
                    if matched_id is not None:
                        valid_ids.append(matched_id)
                
                if len(valid_ids) >= 2:
                    for g in range(1, self.gun_sayisi + 1):
                        for i, p1_id in enumerate(valid_ids):
                            for p2_id in valid_ids[i+1:]:
                                # Kural esnetme: Eger iki kisi de bu gune manuel atanmissa kurali ekleme (kullanici onayi)
                                m_p1 = any(m.gun == g and find_matching_id(m.personel_id, self.personeller.keys()) == p1_id for m in self.manuel_atamalar)
                                m_p2 = any(m.gun == g and find_matching_id(m.personel_id, self.personeller.keys()) == p2_id for m in self.manuel_atamalar)
                                if m_p1 and m_p2:
                                    continue
                                # H5: Ayni gun AYNI GOREV TIPI (base_name) icinde birlikte olamazlar
                                # Farkli gorev tiplerine (orn: Mavi Kod vs Ameliyathane) atanabilirler
                                for base_name, slot_list in self.role_slots.items():
                                    model.Add(
                                        sum(x[p1_id, g, s] for s in slot_list) +
                                        sum(x[p2_id, g, s] for s in slot_list) <= 1
                                    )
        
        # H6. Manuel atamalar
        for m in self.manuel_atamalar:
            matched_pid = find_matching_id(m.personel_id, self.personeller.keys())
            if matched_pid is not None and 0 <= m.slot_idx < self.slot_sayisi:
                if 1 <= m.gun <= self.gun_sayisi:
                    model.Add(x[matched_pid, m.gun, m.slot_idx] == 1)
        
        # H7. Kisitli gorev - kısıtlı kişi sadece kendi görevine (+ taşma görevine) gidebilir
        for p in self.personel_listesi:
            if p.kisitli_gorev:
                # Önce base_name ile dene, sonra ad ile dene (frontend her iki formatı gönderebilir)
                izinli_slotlar = list(self.role_slots.get(p.kisitli_gorev, []))
                if not izinli_slotlar:
                    # Slot adıyla da dene: "AMELIYATHANE #1" -> slot index'i bul
                    for s, gorev in enumerate(self.gorevler):
                        if gorev.ad == p.kisitli_gorev or gorev.base_name == p.kisitli_gorev:
                            izinli_slotlar.append(s)
                # Taşma görevi varsa onun slotlarını da izinli yap
                if p.tasma_gorevi:
                    tasma_slotlar = list(self.role_slots.get(p.tasma_gorevi, []))
                    if not tasma_slotlar:
                        for s, gorev in enumerate(self.gorevler):
                            if gorev.ad == p.tasma_gorevi or gorev.base_name == p.tasma_gorevi:
                                tasma_slotlar.append(s)
                    izinli_slotlar = list(set(izinli_slotlar + tasma_slotlar))
                for g in range(1, self.gun_sayisi + 1):
                    allowed_exception_roles = self.kisitlama_istisna_map.get((p.id, g), set())
                    for s in range(self.slot_sayisi):
                        role = self._role_name_by_slot(s)
                        if s not in izinli_slotlar and role not in allowed_exception_roles:
                            model.Add(x[p.id, g, s] == 0)
        
        # H8. Exclusive görevler - kısıtlı OLMAYAN kişi exclusive slotlara gidemez
        # Hangi görevlerin exclusive olduğunu bul (görevin exclusive flag'i true ise)
        # AMA: Havuzu olan görevleri hariç tut - H10 zaten havuz kontrolü yapıyor
        exclusive_gorevler = set()
        for gorev in self.gorevler:
            if gorev.exclusive:
                base = gorev.base_name if gorev.base_name else gorev.ad
                if base not in self.gorev_havuzlari:
                    exclusive_gorevler.add(base)
        
        # Kısıtlı olmayan kişiler exclusive slotlara gidemez
        # Veya farklı bir göreve kısıtlı kişiler de exclusive slotlara gidemez
        # Taşma görevi olarak bu göreve atanmış kişiler de girebilir
        for p in self.personel_listesi:
            for exclusive_gorev in exclusive_gorevler:
                # Bu kişi bu exclusive göreve kısıtlı mı veya taşma görevi mi?
                if p.kisitli_gorev != exclusive_gorev and p.tasma_gorevi != exclusive_gorev:
                    # Hayır - bu exclusive göreve gidemez
                    exclusive_slotlar = self.role_slots.get(exclusive_gorev, [])
                    for g in range(1, self.gun_sayisi + 1):
                        for s in exclusive_slotlar:
                            model.Add(x[p.id, g, s] == 0)

        # H9. Ayrı bina slotları + birlikte kuralı üyeleri
        #     YENİ FORMÜL: ceil(hedef/2) nöbet birlikte, geri kalanı serbest (ayrı binaya gidebilir)
        #     3 nöbet → 2 birlikte, max 1 ayrı bina
        #     4 nöbet → 2 birlikte, max 2 ayrı bina
        #     5 nöbet → 3 birlikte, max 2 ayrı bina
        ayri_bina_slotlar = [
            s for s, gorev in enumerate(self.gorevler)
            if getattr(gorev, 'ayri_bina', False)
        ]
        if ayri_bina_slotlar:
            birlikte_uye_ids = set()
            for kural in self.kurallar:
                if kural.tur != 'birlikte':
                    continue
                for raw_pid in kural.kisiler:
                    matched_pid = find_matching_id(raw_pid, self.personeller.keys())
                    if matched_pid is not None:
                        birlikte_uye_ids.add(matched_pid)

            for pid in birlikte_uye_ids:
                # Kişinin hedef nöbet sayısını al
                hedef = self.hedefler.get(pid, {})
                hedef_toplam = hedef.get('hedef_toplam', 3)

                # ceil(hedef/2) birlikte minimum, geri kalanı ayrı binaya gidebilir
                birlikte_minimum = math.ceil(hedef_toplam / 2)
                ayri_bina_max = hedef_toplam - birlikte_minimum
                # En az 1 izin ver (tamamen sıfır olmasın)
                ayri_bina_max = max(ayri_bina_max, 1)

                toplam_ayri_bina_atamasi = []
                for g in range(1, self.gun_sayisi + 1):
                    if (pid, g) in self.birlikte_istisna_set:
                        continue  # İstisna olan gün hesaplamadan hariç tutulur
                    for s in ayri_bina_slotlar:
                        toplam_ayri_bina_atamasi.append(x[pid, g, s])

                if toplam_ayri_bina_atamasi:
                    model.Add(sum(toplam_ayri_bina_atamasi) <= ayri_bina_max)

        # H10. Non-exclusive görev havuzu varsa sadece o havuzdan seçim yap
        for role, allowed_ids in self.gorev_havuzlari.items():
            role_slotlari = self.role_slots.get(role, [])
            if not role_slotlari or not allowed_ids:
                continue
            for p in self.personel_listesi:
                if p.id in allowed_ids:
                    continue
                # Bug fix: kısıtlı veya taşma görevi olan kişiler havuz dışı sayılmaz
                if p.kisitli_gorev == role or p.tasma_gorevi == role:
                    continue
                for g in range(1, self.gun_sayisi + 1):
                    for s in role_slotlari:
                        model.Add(x[p.id, g, s] == 0)
        
        # SOFT CONSTRAINTS
        penalties = []
        
        # S0. Boş slot cezası (çok büyük - boş bırakmamaya çalışsın)
        WEIGHT_BOS_SLOT = 100000
        for bos_mu in bos_slotlar:
            penalties.append(bos_mu * WEIGHT_BOS_SLOT)
        
        # S1. Gorev kotalari — HARD üst sınır + SOFT eksik cezası
        for p in self.personel_listesi:
            hedef = self.hedefler.get(p.id, {})
            gorev_kotalari = hedef.get('gorev_kotalari', {})
            for role, slot_list in self.role_slots.items():
                kota = gorev_kotalari.get(role, 0)
                role_atama = sum(x[p.id, g, s] for g in range(1, self.gun_sayisi + 1) for s in slot_list)
                if kota > 0:
                    # HARD: Kotayı aşamaz
                    model.Add(role_atama <= kota)
                # SOFT: Eksik kalırsa ceza
                eksik = model.NewIntVar(0, self.gun_sayisi * len(slot_list), f'role_eksik_{p.id}_{role}')
                model.Add(kota - role_atama <= eksik)
                slot_agirlik = self.slot_agirliklari.get(role, 1)
                penalties.append(eksik * WEIGHT_GOREV_KOTA * slot_agirlik)
        
        # S2. Gun tipi kotalari
        for p in self.personel_listesi:
            hedef = self.hedefler.get(p.id, {})
            hedef_tipler = hedef.get('hedef_tipler', {})
            for tip in GUN_TIPLERI:
                tip_hedef = hedef_tipler.get(tip, 0)
                tip_gunleri = self.gunler_by_tip.get(tip, [])
                if tip_gunleri:
                    tip_atama = sum(x[p.id, g, s] for g in tip_gunleri for s in range(self.slot_sayisi))
                    fazla = model.NewIntVar(0, len(tip_gunleri) * self.slot_sayisi, f'tip_fazla_{p.id}_{tip}')
                    eksik = model.NewIntVar(0, len(tip_gunleri) * self.slot_sayisi, f'tip_eksik_{p.id}_{tip}')
                    model.Add(tip_atama - tip_hedef == fazla - eksik)
                    penalties.append(fazla * WEIGHT_GUN_TIPI)
                    penalties.append(eksik * WEIGHT_GUN_TIPI)
        
        # S3. Toplam hedef — HARD üst sınır + SOFT eksik cezası
        for p in self.personel_listesi:
            hedef = self.hedefler.get(p.id, {})
            hedef_toplam = hedef.get('hedef_toplam', 3)
            toplam_atama = sum(x[p.id, g, s] for g in range(1, self.gun_sayisi + 1) for s in range(self.slot_sayisi))
            # HARD: Hedefi aşamaz
            model.Add(toplam_atama <= hedef_toplam)
            # SOFT: Eksik kalırsa ceza
            eksik = model.NewIntVar(0, self.gun_sayisi, f'toplam_eksik_{p.id}')
            model.Add(hedef_toplam - toplam_atama <= eksik)
            penalties.append(eksik * WEIGHT_TOPLAM)
        
        # S4. Birlikte tutma (SOFT CONSTRAINT - Aynı gün ikisi de atanmalı tercih edilir)
        WEIGHT_BIRLIKTE = 500  # Yüksek ağırlık ama hard değil
        for kural in self.kurallar:
            if kural.tur == 'birlikte':
                # Normalize edilmiş ID eşleştirme
                valid_ids = []
                for pid in kural.kisiler:
                    matched_id = find_matching_id(pid, self.personeller.keys())
                    if matched_id is not None:
                        valid_ids.append(matched_id)
                
                if len(valid_ids) >= 2:
                    # SOFT: Birlikte çalışma tercihi - all-pairs karşılaştırma
                    for i in range(len(valid_ids)):
                        for j in range(i + 1, len(valid_ids)):
                            p1_id = valid_ids[i]
                            p2_id = valid_ids[j]
                            p1_obj = self.personeller[p1_id]
                            p2_obj = self.personeller[p2_id]
                            # Her iki kişinin de müsait olduğu günleri bul
                            ortak_gunler = p1_obj.musait_gunler & p2_obj.musait_gunler

                            for g in range(1, self.gun_sayisi + 1):
                                p1_atama = sum(x[p1_id, g, s] for s in range(self.slot_sayisi))
                                p2_atama = sum(x[p2_id, g, s] for s in range(self.slot_sayisi))

                                if g in ortak_gunler:
                                    # SOFT: Ortak günlerde birlikte atama tercih et
                                    fark = model.NewIntVar(0, 2, f'birlikte_fark_{p1_id}_{p2_id}_{g}')
                                    model.Add(p1_atama - p2_atama <= fark)
                                    model.Add(p2_atama - p1_atama <= fark)
                                    penalties.append(fark * WEIGHT_BIRLIKTE)
        
        # S5. Homojen dağılım - Nöbetleri ay geneline yay (haftada ~1 nöbet hedefi)
        # Mazeretler izin veriyorsa yay, vermiyorsa sıkışık tutulabilir
        for p in self.personel_listesi:
            hedef = self.hedefler.get(p.id, {})
            hedef_toplam = hedef.get('hedef_toplam', 3)

            if hedef_toplam >= 2:
                # İdeal aralık hesapla: ay_gunu / hedef_nobet
                # Örn: 31 gün, 4 nöbet → ideal 7-8 gün arayla
                ideal_aralik = self.gun_sayisi // hedef_toplam

                # Ayı haftalara böl ve her haftada max 1 nöbet tercih et
                hafta_sayisi = (self.gun_sayisi + 6) // 7  # Yukarı yuvarla

                for hafta in range(hafta_sayisi):
                    hafta_baslangic = hafta * 7 + 1
                    hafta_bitis = min((hafta + 1) * 7, self.gun_sayisi)

                    if hafta_bitis >= hafta_baslangic:
                        hafta_gunleri = list(range(hafta_baslangic, hafta_bitis + 1))
                        # Bu haftadaki toplam nöbet sayısı
                        hafta_nobet = sum(
                            x[p.id, g, s]
                            for g in hafta_gunleri if g <= self.gun_sayisi
                            for s in range(self.slot_sayisi)
                        )
                        # Haftada 1'den fazla nöbet varsa ceza
                        fazla = model.NewIntVar(0, 7, f'hafta_fazla_{p.id}_{hafta}')
                        model.Add(hafta_nobet - 1 <= fazla)
                        model.Add(fazla >= 0)
                        penalties.append(fazla * WEIGHT_HOMOJEN)

                # Max aralık penceresi (SOFT): nöbetler arasında çok uzun boşluk olmasın
                # max_aralik = ideal_aralik + tolerans
                tolerans = max(2, ideal_aralik // 2)
                max_aralik = ideal_aralik + tolerans
                # Sert üst sınır: ideal_aralik * 2'den büyük boşluklar için 5x ceza
                sert_ust_sinir = ideal_aralik * 2
                if max_aralik < self.gun_sayisi:
                    for baslangic in range(1, self.gun_sayisi - max_aralik + 1):
                        pencere_gunleri = list(range(baslangic, baslangic + max_aralik + 1))
                        pencere_nobet = sum(
                            x[p.id, g, s]
                            for g in pencere_gunleri if 1 <= g <= self.gun_sayisi
                            for s in range(self.slot_sayisi)
                        )
                        # Pencere içinde en az 1 nöbet olsun (SOFT)
                        bos_pencere = model.NewBoolVar(f'bos_pencere_{p.id}_{baslangic}')
                        model.Add(pencere_nobet == 0).OnlyEnforceIf(bos_pencere)
                        model.Add(pencere_nobet >= 1).OnlyEnforceIf(bos_pencere.Not())
                        penalties.append(bos_pencere * WEIGHT_HOMOJEN)

                # Kademeli ceza: sert_ust_sinir penceresi (büyük boşluklar için 5x)
                if sert_ust_sinir < self.gun_sayisi:
                    for baslangic in range(1, self.gun_sayisi - sert_ust_sinir + 1):
                        pencere_gunleri = list(range(baslangic, baslangic + sert_ust_sinir + 1))
                        pencere_nobet = sum(
                            x[p.id, g, s]
                            for g in pencere_gunleri if 1 <= g <= self.gun_sayisi
                            for s in range(self.slot_sayisi)
                        )
                        buyuk_bosluk = model.NewBoolVar(f'buyuk_bosluk_{p.id}_{baslangic}')
                        model.Add(pencere_nobet == 0).OnlyEnforceIf(buyuk_bosluk)
                        model.Add(pencere_nobet >= 1).OnlyEnforceIf(buyuk_bosluk.Not())
                        penalties.append(buyuk_bosluk * WEIGHT_HOMOJEN * 5)
        
        # S6. Yıllık dengeleme - Geçmiş ay eksiklerini bu ay tamamla
        # yillik_gerceklesen: {'hici': 10, 'cmt': 5, ...} şeklinde geçmiş ayların toplamı
        for p in self.personel_listesi:
            if hasattr(p, 'yillik_gerceklesen') and p.yillik_gerceklesen:
                # Yıllık ortalamayı hesapla
                yillik_toplam = sum(p.yillik_gerceklesen.values())
                
                # Tüm personelin yıllık ortalaması
                tum_yillik = [sum(pp.yillik_gerceklesen.values()) 
                              for pp in self.personel_listesi 
                              if hasattr(pp, 'yillik_gerceklesen') and pp.yillik_gerceklesen]
                
                if tum_yillik:
                    ortalama = sum(tum_yillik) / len(tum_yillik)
                    fark = yillik_toplam - ortalama
                    
                    # Ortalamanın altındaysa daha fazla nöbet alsın
                    # Ortalamanın üstündeyse daha az nöbet alsın
                    if fark < -1:  # Ortalamadan 1+ eksik
                        # Bu kişiye daha fazla nöbet ver (eksik sayısı kadar bonus)
                        eksik_bonus = int(abs(fark))
                        toplam_atama = sum(x[p.id, g, s] for g in range(1, self.gun_sayisi + 1) for s in range(self.slot_sayisi))
                        hedef = self.hedefler.get(p.id, {})
                        hedef_toplam = hedef.get('hedef_toplam', 3)
                        # Hedefin altında kalırsa ceza (eksik olanı doldur)
                        eksik = model.NewIntVar(0, self.gun_sayisi, f'yillik_eksik_{p.id}')
                        model.Add(hedef_toplam - toplam_atama <= eksik)
                        penalties.append(eksik * WEIGHT_YILLIK * min(eksik_bonus, 3))
                    elif fark > 1:  # Ortalamadan 1+ fazla
                        # Bu kişiye daha az nöbet ver
                        fazla_ceza = int(fark)
                        toplam_atama = sum(x[p.id, g, s] for g in range(1, self.gun_sayisi + 1) for s in range(self.slot_sayisi))
                        hedef = self.hedefler.get(p.id, {})
                        hedef_toplam = hedef.get('hedef_toplam', 3)
                        # Hedefin üstüne çıkarsa ceza (fazla tutanı azalt)
                        fazla = model.NewIntVar(0, self.gun_sayisi, f'yillik_fazla_{p.id}')
                        model.Add(toplam_atama - hedef_toplam <= fazla)
                        penalties.append(fazla * WEIGHT_YILLIK * min(fazla_ceza, 3))
        
        # S7. Panik faktörü - Sıkışık kişilere öncelik
        # Mazereti çok olan ve hedefi yüksek olan kişilere öncelik ver
        for p in self.personel_listesi:
            mazeret_sayisi = len(p.mazeret_gunleri)
            musait_gun = self.gun_sayisi - mazeret_sayisi
            hedef = self.hedefler.get(p.id, {})
            hedef_toplam = hedef.get('hedef_toplam', 3)
            
            if musait_gun > 0 and hedef_toplam > 0:
                # Panik oranı = hedef / müsait gün
                # Oran yüksekse (sıkışıksa) hedefin altına düşmemeli
                panik_orani = hedef_toplam / musait_gun
                
                if panik_orani > 0.3:  # %30'dan fazla sıkışıksa
                    toplam_atama = sum(x[p.id, g, s] for g in range(1, self.gun_sayisi + 1) for s in range(self.slot_sayisi))
                    # Hedefin altına düşerse ağır ceza
                    eksik = model.NewIntVar(0, self.gun_sayisi, f'panik_eksik_{p.id}')
                    model.Add(hedef_toplam - toplam_atama <= eksik)
                    # Panik oranına göre ceza çarpanı
                    carpan = min(int(panik_orani * 10), 5)
                    penalties.append(eksik * WEIGHT_PANIK * carpan)
        
        if penalties:
            model.Minimize(sum(penalties))
        
        # COZUM
        solver = cp.CpSolver()
        solver.parameters.max_time_in_seconds = self.max_sure
        solver.parameters.num_search_workers = 4
        
        status = solver.Solve(model)
        sure_ms = int((time.time() - baslangic) * 1000)
        
        if status in [cp.OPTIMAL, cp.FEASIBLE]:
            atamalar = []
            kisi_sayac = {p.id: {'toplam': 0, 'tipler': {t: 0 for t in GUN_TIPLERI}, 'gorevler': {}} for p in self.personel_listesi}
            bos_slot_sayisi = sum(1 for bos_mu in bos_slotlar if solver.Value(bos_mu) == 1)
            
            for g in range(1, self.gun_sayisi + 1):
                gun_tipi = self.gun_tipleri.get(g, 'hici')
                for s in range(self.slot_sayisi):
                    for p in self.personel_listesi:
                        if solver.Value(x[p.id, g, s]) == 1:
                            gorev = self.gorevler[s] if s < len(self.gorevler) else None
                            gorev_ad = gorev.ad if gorev else f'Slot {s}'
                            base_name = gorev.base_name if gorev and gorev.base_name else gorev_ad
                            atamalar.append({
                                'gun': g, 'slot_idx': s, 'gorev_ad': gorev_ad,
                                'gorev_base': base_name, 'personel_id': p.id,
                                'personel_ad': p.ad, 'gun_tipi': gun_tipi
                            })
                            kisi_sayac[p.id]['toplam'] += 1
                            kisi_sayac[p.id]['tipler'][gun_tipi] += 1
                            kisi_sayac[p.id]['gorevler'][base_name] = kisi_sayac[p.id]['gorevler'].get(base_name, 0) + 1
            
            toplam_atama = len(atamalar)
            toplam_slot = self.gun_sayisi * self.slot_sayisi
            min_nobet = min(k['toplam'] for k in kisi_sayac.values()) if kisi_sayac else 0
            max_nobet = max(k['toplam'] for k in kisi_sayac.values()) if kisi_sayac else 0
            
            # DEBUG: Kısıtlamalı personel bilgileri
            kisitli_debug = []
            for p in self.personel_listesi:
                if p.kisitli_gorev:
                    izinli = list(self.role_slots.get(p.kisitli_gorev, []))
                    if p.tasma_gorevi:
                        tasma_slotlar = list(self.role_slots.get(p.tasma_gorevi, []))
                        izinli = list(set(izinli + tasma_slotlar))
                    kisitli_debug.append({
                        'personel_id': p.id,
                        'personel_ad': p.ad,
                        'kisitli_gorev': p.kisitli_gorev,
                        'tasma_gorevi': p.tasma_gorevi,
                        'izinli_slotlar': izinli,
                        'gerceklesen_gorevler': kisi_sayac[p.id]['gorevler']
                    })
            
            istatistikler = {
                'status': 'OPTIMAL' if status == cp.OPTIMAL else 'FEASIBLE',
                'objective': solver.ObjectiveValue() if penalties else 0,
                'toplam_atama': toplam_atama, 'toplam_slot': toplam_slot,
                'bos_slot_sayisi': bos_slot_sayisi,
                'ara_gun': self.ara_gun,
                'solver_status_name': solver.StatusName(status),
                'doluluk_yuzde': round(100 * toplam_atama / toplam_slot, 1) if toplam_slot > 0 else 0,
                'min_nobet': min_nobet, 'max_nobet': max_nobet,
                'denge_farki': max_nobet - min_nobet,
                'solver_num_conflicts': solver.NumConflicts(),
                'solver_num_branches': solver.NumBranches(),
                'solver_wall_time_s': round(solver.WallTime(), 3),
                'eliminated_vars': eliminated_vars,
                'kalite_skoru': self._hesapla_kalite_skoru(kisi_sayac, atamalar, toplam_atama, toplam_slot),
                'kisi_detay': [
                    {'personel_id': str(p.id), 'personel_ad': p.ad, 'toplam': kisi_sayac[p.id]['toplam'],
                     'tipler': kisi_sayac[p.id]['tipler'], 'gorevler': kisi_sayac[p.id]['gorevler']}
                    for p in self.personel_listesi
                ],
                'role_slots': {k: v for k, v in self.role_slots.items()},
                'kisitli_debug': kisitli_debug,
                'kisitlama_istisna_debug': self.kisitlama_istisna_debug,
                'feasibility_debug': self._build_feasibility_diagnostics(limit_preview=30) if bos_slot_sayisi > 0 else {},
                'gorev_listesi': [{'idx': i, 'ad': g.ad, 'base_name': g.base_name} for i, g in enumerate(self.gorevler)]
            }
            return SolverSonuc(basarili=True, atamalar=atamalar, istatistikler=istatistikler,
                              sure_ms=sure_ms, mesaj='OPTIMAL' if status == cp.OPTIMAL else 'FEASIBLE')
        else:
            # Çözüm bulunamadı - gerçek solver status bilgisini dön
            status_name = solver.StatusName(status)
            if status == cp.INFEASIBLE:
                normalized_status = 'INFEASIBLE'
            elif status == cp.MODEL_INVALID:
                normalized_status = 'MODEL_INVALID'
            elif status == cp.UNKNOWN:
                normalized_status = 'UNKNOWN'
            else:
                normalized_status = f'STATUS_{status}'

            ara_gun_1_dene = (normalized_status == 'INFEASIBLE' and self.ara_gun > 1)
            timeout_olasi = (
                normalized_status == 'UNKNOWN' and
                sure_ms >= max(int(self.max_sure * 1000) - 500, 0)
            )
            reason_hint = (
                "Muhtemel timeout veya model cok zor."
                if timeout_olasi else
                "Model cozulmedi, ayrintiları kontrol edin."
            )
            feasibility_debug = self._build_feasibility_diagnostics(limit_preview=40)
            return SolverSonuc(basarili=False, atamalar=[], 
                              istatistikler={
                                  'status': normalized_status,
                                  'solver_status_name': status_name,
                                  'ara_gun': self.ara_gun,
                                  'ara_gun_1_dene': ara_gun_1_dene,
                                  'solver_num_conflicts': solver.NumConflicts(),
                                  'solver_num_branches': solver.NumBranches(),
                                  'solver_wall_time_s': round(solver.WallTime(), 3),
                                  'max_sure_saniye': self.max_sure,
                                  'timeout_olasi': timeout_olasi,
                                  'reason_hint': reason_hint,
                                  'kisitlama_istisna_debug': self.kisitlama_istisna_debug,
                                  'feasibility_debug': feasibility_debug
                              },
                              sure_ms=sure_ms, 
                              mesaj=f"Cozum bulunamadi: {normalized_status} (ara_gun={self.ara_gun})")
