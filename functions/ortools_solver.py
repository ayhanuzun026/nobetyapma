"""
OR-Tools CP-SAT Nobet Cozucu v4.2
Gorev kotalari + Gun tipi kotalari dahil
"""

from dataclasses import dataclass
from typing import Any, List, Dict, Set
import time
import math

from utils import (
    GUN_TIPLERI, SAAT_DEGERLERI,
    ESDEGER_TIP_GRUPLARI,
    find_matching_id,
    birlikte_aile_anahtari,
    BIRLIKTE_ESDEGER_GOREV_AILE_ADI,
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


@dataclass
class _SolveContext:
    cp: Any
    model: Any
    x: Dict
    kisi_gun_atama: Dict
    bos_slotlar: List
    penalties: List
    eliminated_vars: int
    unsat_registry: Any = None


@dataclass
class _AssumptionInfo:
    group: str
    action: str
    label: str
    detail: Dict


class _UnsatCoreRegistry:
    """CP-SAT assumption literal'lerini insan okunur kural gruplarina baglar."""

    def __init__(self, model: Any):
        self.model = model
        self._literals = {}
        self._infos = {}
        self._order = []

    @staticmethod
    def _safe_name(value: str) -> str:
        return ''.join(ch if ch.isalnum() else '_' for ch in value)[:80]

    def guard(self, group: str, action: str, label: str, detail: Dict = None):
        if group not in self._literals:
            lit = self.model.NewBoolVar(f"assume_{len(self._order)}_{self._safe_name(group)}")
            self.model.AddAssumption(lit)
            self._literals[group] = lit
            self._infos[group] = _AssumptionInfo(
                group=group,
                action=action,
                label=label,
                detail=detail or {},
            )
            self._order.append(group)
        return self._literals[group]

    def enforce(self, constraint: Any, group: str, action: str, label: str, detail: Dict = None):
        constraint.OnlyEnforceIf(self.guard(group, action, label, detail))
        return constraint

    def describe_core(self, core_indexes: List[int]) -> Dict:
        core_set = set(core_indexes or [])
        core_items = []
        for group in self._order:
            lit = self._literals[group]
            if lit.Index() not in core_set:
                continue
            info = self._infos[group]
            core_items.append({
                'group': info.group,
                'action': info.action,
                'label': info.label,
                'detail': info.detail,
                'literal_index': lit.Index(),
            })

        actions = []
        for item in core_items:
            action = item['action']
            if action and action not in actions:
                actions.append(action)

        return {
            'core_size': len(core_items),
            'core_groups': core_items,
            'suggested_actions': actions,
            'raw_core_indexes': list(core_indexes or []),
            'tracked_assumption_count': len(self._order),
        }

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
                 plan_kontrati: Dict = None,
                 ara_gun: int = 2, max_sure_saniye: int = 300,
                 ignore_manual_conflicts: bool = False,
                 leksikografik: bool = True):
        self.gun_sayisi = gun_sayisi
        self.gun_tipleri = gun_tipleri
        self.personeller = {p.id: p for p in personeller}
        self.personel_listesi = personeller
        self.gorevler = gorevler
        self.kurallar = kurallar or []
        self.gorev_havuzlari = gorev_havuzlari if isinstance(gorev_havuzlari, dict) else {}
        self.kisitlama_istisnalari = kisitlama_istisnalari or []
        self.manuel_atamalar = manuel_atamalar or []
        self.hedefler = hedefler or {}
        self.plan_kontrati = plan_kontrati or {}
        self.plan_uygulama = self.plan_kontrati.get("uygulama", {}) if isinstance(self.plan_kontrati, dict) else {}
        self.ara_gun = ara_gun
        self.max_sure = max_sure_saniye
        self.leksikografik = leksikografik
        self._leksikografik_kullanildi = False
        self.slot_sayisi = len(gorevler)
        self.manual_mazeret_override_days = set()
        self.manual_mazeret_override_slots = set()
        self.ignore_manual_conflicts = False
        
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

        self.birlikte_family_slots = {}
        for s, gorev in enumerate(gorevler):
            role_name = gorev.base_name if gorev.base_name else gorev.ad
            family_key = birlikte_aile_anahtari(role_name)
            if family_key not in self.birlikte_family_slots:
                self.birlikte_family_slots[family_key] = []
            self.birlikte_family_slots[family_key].append(s)

        self.bina_slots = {}
        for s, gorev in enumerate(gorevler):
            bina_id = str(getattr(gorev, 'bina_id', '') or 'ANA_BINA')
            self.bina_slots.setdefault(bina_id, []).append(s)

        # Role bazli havuz ID'lerini mevcut personel ID'lerine normalize et
        normalized_havuzlar = {}
        for role, raw_ids in self.gorev_havuzlari.items():
            matched_ids = set()
            for pid in raw_ids or []:
                matched_id = find_matching_id(pid, self.personeller.keys())
                if matched_id is not None:
                    matched_ids.add(matched_id)
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

        for m in self.manuel_atamalar:
            if not getattr(m, "mazeret_onayli", False):
                continue
            matched_id = find_matching_id(m.personel_id, self.personeller.keys())
            if matched_id is None:
                continue
            if 1 <= m.gun <= self.gun_sayisi and 0 <= m.slot_idx < self.slot_sayisi:
                self.manual_mazeret_override_days.add((matched_id, m.gun))
                self.manual_mazeret_override_slots.add((matched_id, m.gun, m.slot_idx))
        
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

    def _plan_aktif_mi(self) -> bool:
        if not isinstance(self.plan_kontrati, dict) or not self.plan_kontrati:
            return False
        return bool(self.plan_uygulama.get("yetkili", True))

    def _plan_penalty_multiplier(self) -> int:
        if not self._plan_aktif_mi():
            return 1
        try:
            return max(1, int(self.plan_uygulama.get("plan_sadakat_agirlik_carpani", 1)))
        except (TypeError, ValueError):
            return 1

    def _plan_toplam_hard_mi(self) -> bool:
        return self._plan_aktif_mi() and bool(self.plan_uygulama.get("toplam_hard", True))

    def _plan_gun_tipi_toleransi(self) -> int:
        if not self._plan_aktif_mi():
            return 0
        try:
            return max(0, int(self.plan_uygulama.get("gun_tipi_toleransi", 0)))
        except (TypeError, ValueError):
            return 0

    def _plan_gorev_kota_toleransi(self) -> int:
        if not self._plan_aktif_mi():
            return 0
        try:
            return max(0, int(self.plan_uygulama.get("gorev_kota_toleransi", 0)))
        except (TypeError, ValueError):
            return 0

    def _gun_iskeleti_aktif_mi(self) -> bool:
        if not self._plan_aktif_mi():
            return False
        gun_iskeleti = self.plan_kontrati.get("gun_iskeleti", {}) if isinstance(self.plan_kontrati, dict) else {}
        return bool(self.plan_uygulama.get("gun_iskeleti_kullan", False) and gun_iskeleti.get("aktif"))

    def _gun_iskeleti_toleransi(self) -> int:
        if not self._gun_iskeleti_aktif_mi():
            return 999999
        try:
            return max(0, int(self.plan_uygulama.get("gun_iskeleti_toleransi", 0)))
        except (TypeError, ValueError):
            return 0

    def _gun_iskeleti_hard_mi(self) -> bool:
        return self._gun_iskeleti_aktif_mi() and bool(self.plan_uygulama.get("gun_iskeleti_hard", False))

    def _gun_iskeleti_agirligi(self) -> int:
        if not self._gun_iskeleti_aktif_mi():
            return 0
        try:
            return max(1, int(self.plan_uygulama.get("gun_iskeleti_sadakat_agirligi", 2500)))
        except (TypeError, ValueError):
            return 2500

    def _planlanan_gunler_map(self) -> Dict[int, Set[int]]:
        gun_iskeleti = self.plan_kontrati.get("gun_iskeleti", {}) if isinstance(self.plan_kontrati, dict) else {}
        raw = gun_iskeleti.get("personel_gunleri", {})
        normalized = {}
        for raw_pid, gunler in (raw or {}).items():
            pid = find_matching_id(raw_pid, self.personeller.keys())
            if pid is None:
                continue
            normalized[pid] = {
                int(gun) for gun in (gunler or [])
                if isinstance(gun, int) or str(gun).isdigit()
            }
        return normalized

    def _birlikte_gecerli_ids(self, kural: SolverKural) -> List[int]:
        valid_ids = []
        for raw_pid in getattr(kural, "kisiler", []) or []:
            matched_id = find_matching_id(raw_pid, self.personeller.keys())
            if matched_id is not None and matched_id not in valid_ids:
                valid_ids.append(matched_id)
        return valid_ids

    def _birlikte_tercih_hedefi(self, valid_ids: List[int]) -> int:
        if len(valid_ids) < 2:
            return 0

        hedef_toplamlar = [
            int(self.hedefler.get(pid, {}).get('hedef_toplam', 0) or 0)
            for pid in valid_ids
        ]
        hedef_toplamlar = [val for val in hedef_toplamlar if val > 0]
        if not hedef_toplamlar:
            return 0

        min_hedef = min(hedef_toplamlar)
        # Mümkün olduğunca beraber: ayrı binaya giden 1 nöbet hariç geri kalanı birlikte
        heuristik_hedef = max(1, min_hedef - 1)

        plan_hedef = 0
        planlanan_gunler_map = self._planlanan_gunler_map() if self._gun_iskeleti_aktif_mi() else {}
        if planlanan_gunler_map:
            ortak_planlar = []
            for i in range(len(valid_ids)):
                for j in range(i + 1, len(valid_ids)):
                    p1_days = planlanan_gunler_map.get(valid_ids[i], set())
                    p2_days = planlanan_gunler_map.get(valid_ids[j], set())
                    ortak_planlar.append(len(p1_days & p2_days))
            if ortak_planlar:
                plan_hedef = min(ortak_planlar)

        return min(min_hedef, max(heuristik_hedef, plan_hedef))

    def _planlanan_rol_gunleri_map(self) -> Dict[int, Dict[int, str]]:
        """Plan kontratından kişi-gün-rol mapping'ini al."""
        if not isinstance(self.plan_kontrati, dict):
            return {}
        personeller_raw = self.plan_kontrati.get("personeller", [])
        if not personeller_raw:
            return {}
        result: Dict[int, Dict[int, str]] = {}
        for pp in personeller_raw:
            if isinstance(pp, dict):
                raw_pid = pp.get("personel_id")
                rol_gunleri = pp.get("onerilen_rol_gunleri", {})
            else:
                raw_pid = getattr(pp, "personel_id", None)
                rol_gunleri = getattr(pp, "onerilen_rol_gunleri", {})
            if not rol_gunleri:
                continue
            pid = find_matching_id(raw_pid, self.personeller.keys())
            if pid is None:
                continue
            gun_rol_map: Dict[int, str] = {}
            for gun_key, rol in (rol_gunleri or {}).items():
                try:
                    gun_rol_map[int(gun_key)] = str(rol)
                except (ValueError, TypeError):
                    continue
            if gun_rol_map:
                result[pid] = gun_rol_map
        return result

    def _gun_iskeleti_uygulanabilir_ids(self) -> Set[int]:
        if not self._gun_iskeleti_aktif_mi():
            return set()
        gun_iskeleti = self.plan_kontrati.get("gun_iskeleti", {})
        ids = set()
        for raw_pid in gun_iskeleti.get("uygulanabilir_personeller", []) or []:
            pid = find_matching_id(raw_pid, self.personeller.keys())
            if pid is not None:
                ids.add(pid)
        return ids

    def _hesapla_plan_sapmalari(self, kisi_sayac: Dict[int, Dict], atamalar: List[Dict]) -> Dict:
        if not self._plan_aktif_mi():
            return {}

        detay = []
        tam_uyumlu = 0
        toplam_sapma = 0
        tip_sapma_toplami = 0
        gorev_sapma_toplami = 0
        gun_sapma_toplami = 0
        planlanan_gunler_map = self._planlanan_gunler_map()
        actual_days_map: Dict[int, Set[int]] = {p.id: set() for p in self.personel_listesi}
        for atama in atamalar or []:
            pid = atama.get('personel_id')
            gun = atama.get('gun')
            if pid in actual_days_map and gun is not None:
                actual_days_map[pid].add(int(gun))
        for p in self.personel_listesi:
            hedef = self.hedefler.get(p.id, {})
            if not hedef:
                continue
            gercek = kisi_sayac.get(p.id, {'toplam': 0, 'tipler': {}, 'gorevler': {}})
            hedef_tipler = hedef.get('hedef_tipler', {}) or {}
            hedef_gorevler = hedef.get('gorev_kotalari', {}) or {}

            tip_sapmalari = {}
            for tip in GUN_TIPLERI:
                fark = gercek['tipler'].get(tip, 0) - hedef_tipler.get(tip, 0)
                if fark != 0:
                    tip_sapmalari[tip] = fark
                    tip_sapma_toplami += abs(fark)

            gorev_sapmalari = {}
            for gorev_adi in set(hedef_gorevler.keys()):
                fark = gercek['gorevler'].get(gorev_adi, 0) - hedef_gorevler.get(gorev_adi, 0)
                if fark != 0:
                    gorev_sapmalari[gorev_adi] = fark
                    gorev_sapma_toplami += abs(fark)

            toplam_fark = gercek['toplam'] - hedef.get('hedef_toplam', 0)
            toplam_sapma += abs(toplam_fark)
            planlanan_gunler = set(planlanan_gunler_map.get(p.id, set()))
            gercek_gunler = set(actual_days_map.get(p.id, set()))
            eksik_gunler = sorted(planlanan_gunler - gercek_gunler)
            ekstra_gunler = sorted(gercek_gunler - planlanan_gunler)
            gun_sapma_toplami += len(eksik_gunler) + len(ekstra_gunler)

            if toplam_fark == 0 and not tip_sapmalari and not gorev_sapmalari and not eksik_gunler and not ekstra_gunler:
                tam_uyumlu += 1

            detay.append({
                'personel_id': p.id,
                'personel_ad': p.ad,
                'hedef_toplam': hedef.get('hedef_toplam', 0),
                'gercek_toplam': gercek['toplam'],
                'toplam_fark': toplam_fark,
                'tip_sapmalari': tip_sapmalari,
                'gorev_sapmalari': gorev_sapmalari,
                'planlanan_gunler': sorted(planlanan_gunler),
                'gercek_gunler': sorted(gercek_gunler),
                'eksik_gunler': eksik_gunler,
                'ekstra_gunler': ekstra_gunler,
            })

        return {
            'plan_hash': self.plan_kontrati.get('plan_hash'),
            'kaynak': self.plan_kontrati.get('kaynak'),
            'tam_uyumlu_personel': tam_uyumlu,
            'personel_sayisi': len(detay),
            'toplam_sapma': toplam_sapma,
            'tip_sapma_toplami': tip_sapma_toplami,
            'gorev_sapma_toplami': gorev_sapma_toplami,
            'gun_sapma_toplami': gun_sapma_toplami,
            'detay': detay,
        }

    def _role_name_by_slot(self, slot_idx: int) -> str:
        if slot_idx < 0 or slot_idx >= len(self.gorevler):
            return ""
        gorev = self.gorevler[slot_idx]
        return gorev.base_name if gorev.base_name else gorev.ad

    def _bina_id_by_slot(self, slot_idx: int) -> str:
        if slot_idx < 0 or slot_idx >= len(self.gorevler):
            return ""
        return str(getattr(self.gorevler[slot_idx], 'bina_id', '') or 'ANA_BINA')

    @staticmethod
    def _yetkili_roller(personel: SolverPersonel) -> Set[str]:
        return {
            str(role).strip()
            for role in (getattr(personel, 'yetkili_gorevler', set()) or set())
            if str(role).strip()
        }

    def _hesapla_birlikte_grup_istatistikleri(self, atamalar: List[Dict]) -> List[Dict]:
        daily_assignments = {}
        for atama in atamalar:
            gun = atama.get('gun')
            pid = atama.get('personel_id')
            role = atama.get('gorev_base') or atama.get('gorev_ad') or ''
            if gun is None or pid is None:
                continue
            daily_assignments.setdefault(gun, {})[pid] = {
                'role': role,
                'family': birlikte_aile_anahtari(role),
            }

        birlikte_gruplar = []
        for kural in self.kurallar:
            if kural.tur != 'birlikte':
                continue

            valid_ids = self._birlikte_gecerli_ids(kural)

            if len(valid_ids) < 2:
                continue

            uyumlu_gunler = []
            gun_detaylari = []
            for g in range(1, self.gun_sayisi + 1):
                gun_atamalari = daily_assignments.get(g, {})
                entries = []
                for pid in valid_ids:
                    info = gun_atamalari.get(pid)
                    if info:
                        entries.append({
                            'personel_id': pid,
                            'personel_ad': self.personeller[pid].ad,
                            'role': info['role'],
                            'family': info['family'],
                        })

                if len(entries) < 2:
                    continue

                family_groups = {}
                for entry in entries:
                    family_groups.setdefault(entry['family'], []).append(entry)

                matched_family = next((family for family, members in family_groups.items() if len(members) >= 2), None)
                if matched_family is None:
                    continue

                uyumlu_gunler.append(g)
                gun_detaylari.append({
                    'gun': g,
                    'aile': matched_family,
                    'atamalar': [
                        {
                            'personel_id': entry['personel_id'],
                            'personel_ad': entry['personel_ad'],
                            'gorev': entry['role'],
                        }
                        for entry in family_groups[matched_family]
                    ]
                })

            birlikte_gruplar.append({
                'kisiler': [self.personeller[pid].ad for pid in valid_ids],
                'personel_ids': valid_ids,
                'hedef_gun_sayisi': self._birlikte_tercih_hedefi(valid_ids),
                'gunler': uyumlu_gunler,
                'uyumlu_gun_sayisi': len(uyumlu_gunler),
                'gun_detaylari': gun_detaylari,
                'esdeger_aile': BIRLIKTE_ESDEGER_GOREV_AILE_ADI,
            })

        return birlikte_gruplar

    def _manual_hard_conflict_diagnostics(self) -> List[Dict]:
        """Model kurulmadan önce hard çakışmaları yakala."""
        conflicts = []

        ayri_pairs = []
        for kural in self.kurallar:
            if (
                kural.tur != 'ayri'
                or (
                    str(getattr(kural, 'politika', 'kullanici_onayli')).strip().lower() == 'soft'
                    and not bool(getattr(kural, 'asla_gevsetme', False))
                )
            ):
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

        exclusive_gorevler = set()
        for gorev in self.gorevler:
            if gorev.exclusive or bool(getattr(gorev, 'kritik', False)):
                base = gorev.base_name if gorev.base_name else gorev.ad
                exclusive_gorevler.add(base)

        per_person_day = {}
        per_slot_day = {}
        manual_days = {}
        manual_buildings = {}

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
            bina_id = self._bina_id_by_slot(m.slot_idx)
            yetkili_roller = self._yetkili_roller(p)

            per_person_day[(pid, m.gun)] = per_person_day.get((pid, m.gun), 0) + 1
            per_slot_day[(m.gun, m.slot_idx)] = per_slot_day.get((m.gun, m.slot_idx), 0) + 1
            manual_days.setdefault(pid, []).append(m.gun)
            manual_buildings.setdefault((pid, m.gun), set()).add(bina_id)

            if m.gun in p.mazeret_gunleri and not getattr(m, "mazeret_onayli", False):
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
            if yetkili_roller and role not in yetkili_roller:
                conflicts.append({
                    "code": "YETKI_IHLALI",
                    "mesaj": f"{p.ad} yetkili gorevleri disinda manuel atama almis",
                    "personel_id": pid,
                    "personel_ad": p.ad,
                    "gun": m.gun,
                    "yetkili_gorevler": sorted(yetkili_roller),
                    "gorev": role,
                })
            elif (
                not yetkili_roller
                and p.kisitli_gorev
                and role != p.kisitli_gorev
                and not tasma_ok
                and role not in allowed_exception_roles
            ):
                conflicts.append({
                    "code": "KISITLAMA_IHLALI",
                    "mesaj": f"{p.ad} kisitli gorevi disinda manuel atama almis",
                    "personel_id": pid,
                    "personel_ad": p.ad,
                    "gun": m.gun,
                    "kisitli_gorev": p.kisitli_gorev,
                    "gorev": role
                })

            if (
                role in exclusive_gorevler
                and role not in yetkili_roller
                and p.kisitli_gorev != role
                and p.tasma_gorevi != role
            ):
                # Havuz üyesi ise exclusive ihlali değil
                havuz_ids = self.gorev_havuzlari.get(role)
                if havuz_ids is not None and pid in havuz_ids:
                    pass  # Havuz üyesi — exclusive ihlali yok
                else:
                    conflicts.append({
                        "code": "EXCLUSIVE_IHLALI",
                        "mesaj": f"{p.ad} exclusive goreve manuel atanmis",
                        "personel_id": pid,
                        "personel_ad": p.ad,
                        "gun": m.gun,
                        "gorev": role
                    })

            if role in self.gorev_havuzlari and pid not in self.gorev_havuzlari[role]:
                conflicts.append({
                    "code": "HAVUZ_IHLALI",
                    "mesaj": f"{p.ad} gorev havuzu disinda manuel atanmis",
                    "personel_id": pid,
                    "personel_ad": p.ad,
                    "gun": m.gun,
                    "gorev": role
                })

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
                        p = self.personeller.get(pid)
                        conflicts.append({
                            "code": "ARA_GUN_IHLALI",
                            "mesaj": (
                                f"{p.ad if p else pid} manuel atamalari ara gun "
                                f"kuralini ihlal ediyor ({g1}-{g2})"
                            ),
                            "personel_id": pid,
                            "personel_ad": p.ad if p else "",
                            "gun1": g1,
                            "gun2": g2,
                            "ara_gun": self.ara_gun,
                        })

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
                    ortak_binalar = (
                        manual_buildings.get((p1, gun), set())
                        & manual_buildings.get((p2, gun), set())
                    )
                    if not ortak_binalar:
                        continue
                    n1 = self.personeller[p1].ad if p1 in self.personeller else str(p1)
                    n2 = self.personeller[p2].ad if p2 in self.personeller else str(p2)
                    conflicts.append({
                        "code": "AYRI_KURALI_IHLALI",
                        "mesaj": (
                            f"{n1} ve {n2} ayni gun ayni binaya manuel atanmis "
                            f"(ayri kurali: {', '.join(sorted(ortak_binalar))})"
                        ),
                        "gun": gun,
                        "personel1_id": p1,
                        "personel2_id": p2,
                        "bina_ids": sorted(ortak_binalar),
                    })

        return conflicts

    def _exclusive_roles_without_pool(self) -> Set[str]:
        roles = set()
        for gorev in self.gorevler:
            if gorev.exclusive or bool(getattr(gorev, 'kritik', False)):
                base = gorev.base_name if gorev.base_name else gorev.ad
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
        if gun in p.mazeret_gunleri and (pid, gun, slot_idx) not in self.manual_mazeret_override_slots:
            return False
        if slot_idx < 0 or slot_idx >= self.slot_sayisi:
            return False

        role = self._role_name_by_slot(slot_idx)
        yetkili_roller = self._yetkili_roller(p)
        if yetkili_roller and role not in yetkili_roller:
            return False
        allowed_exception_roles = self.kisitlama_istisna_map.get((pid, gun), set())
        # H7: Kısıtlı görev kuralı (taşma görevi de izinli)
        if (
            not yetkili_roller
            and p.kisitli_gorev
            and role != p.kisitli_gorev
            and role not in allowed_exception_roles
        ):
            if not (p.tasma_gorevi and role == p.tasma_gorevi):
                return False

        # H8: Exclusive görevler - taşma görevi veya havuz üyesi olan kişi de girebilir
        if (
            role in exclusive_roles
            and role not in yetkili_roller
            and p.kisitli_gorev != role
            and p.tasma_gorevi != role
        ):
            havuz_ids = self.gorev_havuzlari.get(role)
            if havuz_ids is None or pid not in havuz_ids:
                return False

        # H10: Görev havuzu
        allowed_ids = self.gorev_havuzlari.get(role)
        if allowed_ids is not None and pid not in allowed_ids:
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
            # H4, farki ara_gun veya daha az olan iki atamayi yasaklar.
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

    # Gün tipi -> insan diline uygun etiket
    _GUN_TIPI_ETIKET = {
        'hici': 'hafta içi',
        'prs': 'Perşembe',
        'cum': 'Cuma',
        'cmt': 'Cumartesi',
        'pzr': 'Pazar',
    }

    def _ayri_cift_haritasi(self) -> Dict[int, Set[int]]:
        """Ayrı kurallarından pid -> {aynı anda çizelgelenemeyeceği pid'ler} haritası."""
        harita: Dict[int, Set[int]] = {}
        for kural in self.kurallar:
            if getattr(kural, 'tur', None) != 'ayri':
                continue
            valid = []
            for raw_pid in kural.kisiler:
                matched = find_matching_id(raw_pid, self.personeller.keys())
                if matched is not None:
                    valid.append(matched)
            for i, a in enumerate(valid):
                for b in valid[i + 1:]:
                    harita.setdefault(a, set()).add(b)
                    harita.setdefault(b, set()).add(a)
        return harita

    def _bos_slot_aciklamalari(self, atamalar: List[Dict], limit: int = 40) -> List[Dict]:
        """Her boş (gün, slot) için 'neden boş kaldı'yı insan diline çevirir.

        Bir slotu dolduramayan her aday için TEK birincil engel sınıflandırılır:
          - mazeret            : kişi o gün müsait değil (izin/mazeret)
          - gorev_uygun_degil  : görev kısıtı/havuz/exclusive nedeniyle uygun değil
          - zaten_atandi       : kişi o gün başka nöbette
          - hedef_dolu         : kişi nöbet kotasını (hedef_toplam, KN3) doldurdu
          - ara_gun            : yakın günde nöbeti var, ara gün kuralına takılı
          - ayri               : 'ayrı' kuralındaki biri o gün nöbette
          - serbest            : uygun+boştu ama denge/kota nedeniyle atanmadı

        Döndürülen her kayıt: gün, gün_tipi, slot_idx, gorev, aday_sayisi,
        sebepler (kod -> [açıklama]) ve hazır 'aciklama' cümlesi.
        """
        exclusive_roles = self._exclusive_roles_without_pool()
        birlikte_uye_ids = self._birlikte_uye_ids()
        ayri_map = self._ayri_cift_haritasi()
        kisi_ad = {p.id: (p.ad or f"#{p.id}") for p in self.personel_listesi}

        # Çözümden türetilen yardımcı haritalar
        dolu = set()                       # (gun, slot_idx)
        gun_atananlar: Dict[int, Set[int]] = {}   # gun -> {pid}
        kisi_gunler: Dict[int, List[int]] = {}     # pid -> [atandığı günler]
        kisi_atama_sayisi: Dict[int, int] = {}     # pid -> toplam atama
        for a in atamalar:
            g = a.get('gun')
            s = a.get('slot_idx')
            pid = a.get('personel_id')
            if g is None or s is None or pid is None:
                continue
            dolu.add((g, s))
            gun_atananlar.setdefault(g, set()).add(pid)
            kisi_gunler.setdefault(pid, []).append(g)
            kisi_atama_sayisi[pid] = kisi_atama_sayisi.get(pid, 0) + 1

        def _kisi_engeli(pid: int, g: int, s: int) -> tuple:
            """(kod, aciklama) döndürür; aday değilse ('mazeret'/'gorev_uygun_degil')."""
            p = self.personeller.get(pid)
            if p is None:
                return ('gorev_uygun_degil', kisi_ad.get(pid, f"#{pid}"))
            ad = kisi_ad.get(pid, f"#{pid}")

            # Yapısal uygunluk (mazeret + görev/havuz/exclusive kısıtları)
            if not self._person_can_take_slot_on_day(pid, s, g, exclusive_roles, birlikte_uye_ids):
                if g in p.mazeret_gunleri:
                    return ('mazeret', f"{ad} (o gün izinli/mazeretli)")
                return ('gorev_uygun_degil', f"{ad} (bu göreve uygun değil)")

            atananlar = gun_atananlar.get(g, set())
            # 1) O gün zaten başka nöbette
            if pid in atananlar:
                return ('zaten_atandi', f"{ad} (o gün başka nöbette)")
            # 2) Kota (hedef_toplam) dolu — KN3 sert üst sınırı
            hedef_toplam = int((self.hedefler.get(pid, {}) or {}).get('hedef_toplam', 0) or 0)
            if kisi_atama_sayisi.get(pid, 0) >= hedef_toplam:
                if hedef_toplam <= 0:
                    return ('hedef_dolu', f"{ad} (nöbet kotası 0)")
                return ('hedef_dolu', f"{ad} (kotası dolu: {hedef_toplam})")
            # 3) Ara gün: yakın günde nöbeti var (gap < ara_gun)
            for gg in kisi_gunler.get(pid, []):
                if gg != g and abs(gg - g) < self.ara_gun:
                    return ('ara_gun', f"{ad} ({gg}. günde nöbeti var, ara gün={self.ara_gun})")
            # 4) Ayrı kuralı: karşı taraf o gün nöbette
            cakisan = ayri_map.get(pid, set()) & atananlar
            if cakisan:
                karsi = kisi_ad.get(next(iter(cakisan)))
                return ('ayri', f"{ad} ({karsi} ile 'ayrı' kuralında, {karsi} bugün nöbette)")
            # 5) Uygun ve boştu ama solver atamadı (denge/soft birlikte)
            return ('serbest', f"{ad} (uygun ve boştu; denge/kota nedeniyle atanmadı)")

        aciklamalar: List[Dict] = []
        for g in range(1, self.gun_sayisi + 1):
            gun_tipi = self.gun_tipleri.get(g, 'hici')
            tip_etiket = self._GUN_TIPI_ETIKET.get(gun_tipi, gun_tipi)
            for s in range(self.slot_sayisi):
                if (g, s) in dolu:
                    continue
                gorev = self._role_name_by_slot(s) or f"Slot {s}"

                # Cümle için tam metinleri topla (yalnızca yerel kullanım),
                # payload'a yalnızca kod->sayı özeti koy (isim listeleri şişirmesin).
                sebep_metinleri: Dict[str, List[str]] = {}
                sebep_sayilari: Dict[str, int] = {}
                aday_sayisi = 0
                for p in self.personel_listesi:
                    kod, metin = _kisi_engeli(p.id, g, s)
                    # 'aday' = yapısal olarak uygun olanlar
                    if kod not in ('mazeret', 'gorev_uygun_degil'):
                        aday_sayisi += 1
                    sebep_metinleri.setdefault(kod, []).append(metin)
                    sebep_sayilari[kod] = sebep_sayilari.get(kod, 0) + 1

                aciklama = self._bos_slot_cumle(
                    g, tip_etiket, gorev, aday_sayisi, sebep_metinleri
                )
                aciklamalar.append({
                    'gun': g,
                    'gun_tipi': gun_tipi,
                    'slot_idx': s,
                    'gorev': gorev,
                    'aday_sayisi': aday_sayisi,
                    'sebep_sayilari': sebep_sayilari,
                    'aciklama': aciklama,
                })
                if len(aciklamalar) >= limit:
                    return aciklamalar
        return aciklamalar

    @staticmethod
    def _bos_slot_cumle(gun: int, tip_etiket: str, gorev: str,
                        aday_sayisi: int, sebepler: Dict[str, List[str]]) -> str:
        """Bir boş slot için tek satırlık insan-dili açıklama üretir."""
        baslik = f"{gun}. gün ({tip_etiket}) '{gorev}' boş"

        if aday_sayisi == 0:
            mazeretliler = sebepler.get('mazeret', [])
            if mazeretliler:
                isimler = ", ".join(m.split(' (')[0] for m in mazeretliler[:4])
                ek = "" if len(mazeretliler) <= 4 else f" (+{len(mazeretliler) - 4})"
                return (f"{baslik} — o gün uygun personel yoktu; "
                        f"müsait olmayanlar: {isimler}{ek}.")
            return f"{baslik} — bu göreve atanabilecek uygun personel tanımlı değil."

        # Aday vardı ama hepsi bir engele takıldı — engelli adayları say/isimle
        engelli = []
        for kod in ('zaten_atandi', 'hedef_dolu', 'ara_gun', 'ayri', 'serbest'):
            engelli.extend(sebepler.get(kod, []))
        if not engelli:
            return f"{baslik} — {aday_sayisi} uygun aday vardı."
        gosterilecek = "; ".join(engelli[:4])
        ek = "" if len(engelli) <= 4 else f"; +{len(engelli) - 4} kişi daha"
        return f"{baslik} — {aday_sayisi} uygun aday vardı ama: {gosterilecek}{ek}."

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
                    bina_ids = sorted(self.bina_slots.keys())
                    aksiyonlar.append({
                        'aksiyon': 'ayri_gevset',
                        'puan': 65,
                        'neden': (
                            f"Ayni bina ayri tutma cakismasi: {len(ayri_kurallari)} ayri kurali "
                            f"{len(ayri_kisi_ids)} kisiyi etkiliyor, "
                            f"ort musait gun: {ort_musait:.0f}/{self.gun_sayisi}, "
                            f"bina sayisi: {len(bina_ids)}"
                        ),
                        'detay': {
                            'kural_sayisi': len(ayri_kurallari),
                            'etkilenen_kisi': len(ayri_kisi_ids),
                            'ort_musait_gun': round(ort_musait, 1),
                            'bina_ids': bina_ids,
                        }
                    })

        # --- KURAL 4: Birlikte kuralları (genellikle sorun değil ama bazen) ---
        birlikte_kurallari = [k for k in self.kurallar if k.tur == 'birlikte']
        if birlikte_kurallari:
            dusuk_ortaklik = []
            for kural in birlikte_kurallari:
                valid_ids = self._birlikte_gecerli_ids(kural)
                if len(valid_ids) < 2:
                    continue
                ortak_gunler = None
                for pid in valid_ids:
                    musait = set(self.personeller[pid].musait_gunler)
                    ortak_gunler = musait if ortak_gunler is None else (ortak_gunler & musait)
                hedef = self._birlikte_tercih_hedefi(valid_ids)
                if hedef > 0 and ortak_gunler is not None and len(ortak_gunler) < hedef:
                    dusuk_ortaklik.append({
                        'kisi_sayisi': len(valid_ids),
                        'ortak_gun': len(ortak_gunler),
                        'hedef': hedef,
                    })

            if dusuk_ortaklik or len(birlikte_kurallari) > 4:
                puan = 55 if dusuk_ortaklik else 35
                neden = (
                    f"{len(birlikte_kurallari)} birlikte kurali var, "
                    "ortak musaitlik bazi gruplarda hedefin altinda"
                    if dusuk_ortaklik else
                    f"{len(birlikte_kurallari)} birlikte kurali var, "
                    "son care olarak kademeli gevsetme gerekebilir"
                )
                aksiyonlar.append({
                    'aksiyon': 'birlikte_kaldir',
                    'puan': puan,
                    'neden': neden,
                    'detay': {
                        'kural_sayisi': len(birlikte_kurallari),
                        'dusuk_ortaklik_sayisi': len(dusuk_ortaklik),
                        'ornekler': dusuk_ortaklik[:5],
                    }
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

        # Ara gün azalt yoksa ekle (her zaman denenebilir)
        if not any(a['aksiyon'] == 'ara_gun_azalt' for a in aksiyonlar):
            aksiyonlar.insert(0, {
                'aksiyon': 'ara_gun_azalt',
                'puan': 60,
                'neden': 'Ara gun azaltma her zaman denenebilir',
                'detay': {}
            })

        # tum_soft_kaldir yoksa ekle
        if not any(a['aksiyon'] == 'tum_soft_kaldir' for a in aksiyonlar):
            aksiyonlar.append({
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

    def _manual_conflict_result(self, baslangic: float, manual_conflicts: List[Dict]) -> SolverSonuc:
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


    def _build_model(self, cp: Any, collect_unsat_core: bool = False) -> _SolveContext:
        model = cp.CpModel()
        unsat_registry = _UnsatCoreRegistry(model) if collect_unsat_core else None

        def hard_add(constraint, group: str, action: str, label: str, detail: Dict = None):
            if unsat_registry is not None:
                return unsat_registry.enforce(constraint, group, action, label, detail)
            return constraint

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
                for s in range(self.slot_sayisi):
                    has_manual_mazeret_override = (p.id, g, s) in self.manual_mazeret_override_slots

                    if p.id in sifir_hedef_ids:
                        x[p.id, g, s] = model.NewConstant(0)
                        eliminated_vars += 1
                        continue

                    if g in p.mazeret_gunleri and not has_manual_mazeret_override:
                        x[p.id, g, s] = model.NewConstant(0)
                        eliminated_vars += 1
                        continue

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
        kisi_gun_atama = {}
        for p in self.personel_listesi:
            for g in range(1, self.gun_sayisi + 1):
                kisi_gun_atama[p.id, g] = sum(x[p.id, g, s] for s in range(self.slot_sayisi))
                model.Add(kisi_gun_atama[p.id, g] <= 1)
        
        # H4. Ara gun - Herkes icin minimum ara gun (HARD)
        # Temel kural: En az 1 gun ara (ayni gun veya ardisik gun olmaz)
        for p in self.personel_listesi:
            if p.id in sifir_hedef_ids:
                continue  # Hedefi 0 olan kisiler zaten eliminate edildi
            for g1 in range(1, self.gun_sayisi + 1):
                if g1 in p.mazeret_gunleri and (p.id, g1) not in self.manual_mazeret_override_days:
                    continue  # Mazeret gunu zaten 0, constraint gereksiz
                for g2 in range(g1 + 1, min(g1 + self.ara_gun + 1, self.gun_sayisi + 1)):
                    if g2 in p.mazeret_gunleri and (p.id, g2) not in self.manual_mazeret_override_days:
                        continue  # Mazeret gunu zaten 0, constraint gereksiz
                    if (p.id, g1, g2) not in self.aragun_istisna_set:
                        hard_add(
                            model.Add(
                                sum(x[p.id, g1, s] for s in range(self.slot_sayisi)) +
                                sum(x[p.id, g2, s] for s in range(self.slot_sayisi)) <= 1
                            ),
                            'H4_ARA_GUN',
                            'ara_gun_azalt',
                            'Minimum ara gun kisiti',
                        )

        # H5. Ayri tutma
        soft_ayri_cakismalari = []
        for kural in self.kurallar:
            if kural.tur == 'ayri':
                politika = str(
                    getattr(kural, 'politika', 'kullanici_onayli')
                ).strip().lower()
                soft_politika = (
                    politika == 'soft'
                    and not bool(getattr(kural, 'asla_gevsetme', False))
                )
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
                                for bina_id, slot_list in self.bina_slots.items():
                                    if soft_politika:
                                        soft_ayri_cakismalari.append(
                                            (p1_id, p2_id, g, bina_id, slot_list)
                                        )
                                    else:
                                        hard_add(
                                            model.Add(
                                                sum(x[p1_id, g, s] for s in slot_list) +
                                                sum(x[p2_id, g, s] for s in slot_list) <= 1
                                            ),
                                            'H5_AYRI_TUTMA',
                                            'ayri_kuralini_incele',
                                            'Ayni binada ayri tutulacak personeller',
                                            {'bina_id': bina_id},
                                        )
        
        # H6. Manuel atamalar
        for m in self.manuel_atamalar:
            matched_pid = find_matching_id(m.personel_id, self.personeller.keys())
            if matched_pid is not None and 0 <= m.slot_idx < self.slot_sayisi:
                if 1 <= m.gun <= self.gun_sayisi:
                    hard_add(
                        model.Add(x[matched_pid, m.gun, m.slot_idx] == 1),
                        'H6_MANUEL_ATAMA',
                        'manuel_kontrol',
                        'Manuel atamalar',
                    )
        
        # H7. Kisitli gorev - kısıtlı kişi sadece kendi görevine (+ taşma görevine) gidebilir
        for p in self.personel_listesi:
            if p.kisitli_gorev and not self._yetkili_roller(p):
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
                            hard_add(
                                model.Add(x[p.id, g, s] == 0),
                                'H7_KISITLI_GOREV',
                                'gorev_kisitlamasi_kontrol',
                                'Kisitli personel sadece izinli gorevlerine atanabilir',
                            )
        
        # H8. Exclusive görevler - kısıtlı OLMAYAN kişi exclusive slotlara gidemez
        # Havuzlu görevlerde havuz üyeleri de girebilir
        exclusive_gorevler = set()
        for gorev in self.gorevler:
            if gorev.exclusive or bool(getattr(gorev, 'kritik', False)):
                base = gorev.base_name if gorev.base_name else gorev.ad
                exclusive_gorevler.add(base)

        # Kısıtlı olmayan kişiler exclusive slotlara gidemez
        # Veya farklı bir göreve kısıtlı kişiler de exclusive slotlara gidemez
        # Taşma görevi olarak bu göreve atanmış kişiler de girebilir
        # Havuz üyeleri de girebilir
        for p in self.personel_listesi:
            yetkili_roller = self._yetkili_roller(p)
            for exclusive_gorev in exclusive_gorevler:
                # Bu kişi bu exclusive göreve kısıtlı mı veya taşma görevi mi?
                if (
                    exclusive_gorev not in yetkili_roller
                    and p.kisitli_gorev != exclusive_gorev
                    and p.tasma_gorevi != exclusive_gorev
                ):
                    # Havuz üyesi mi?
                    havuz_ids = self.gorev_havuzlari.get(exclusive_gorev)
                    if havuz_ids is not None and p.id in havuz_ids:
                        continue  # Havuz üyesi — girebilir
                    # Hayır - bu exclusive göreve gidemez
                    exclusive_slotlar = self.role_slots.get(exclusive_gorev, [])
                    for g in range(1, self.gun_sayisi + 1):
                        for s in exclusive_slotlar:
                            hard_add(
                                model.Add(x[p.id, g, s] == 0),
                                'H8_EXCLUSIVE_GOREV',
                                'exclusive_gevset',
                                'Exclusive gorevlere sadece yetkili personel atanabilir',
                            )

        # H9. Birlikte kurali: soft olmayan gruplar ayni gun ve ayni gorev
        # ailesinde calisir. Yalniz kullanici_onayli kurallar acik istisna kabul eder.
        for kural_idx, kural in enumerate(self.kurallar):
            politika = str(getattr(kural, 'politika', 'kullanici_onayli')).strip().lower()
            asla_gevsetme = bool(getattr(kural, 'asla_gevsetme', False))
            if kural.tur != 'birlikte' or (politika == 'soft' and not asla_gevsetme):
                continue
            istisna_izinli = (
                politika == 'kullanici_onayli'
                and not asla_gevsetme
            )
            valid_ids = self._birlikte_gecerli_ids(kural)
            for i, p1_id in enumerate(valid_ids):
                for p2_id in valid_ids[i + 1:]:
                    for g in range(1, self.gun_sayisi + 1):
                        if istisna_izinli and (
                            (p1_id, g) in self.birlikte_istisna_set
                            or (p2_id, g) in self.birlikte_istisna_set
                        ):
                            continue
                        hard_add(
                            model.Add(kisi_gun_atama[p1_id, g] == kisi_gun_atama[p2_id, g]),
                            f'H9_BIRLIKTE_GUN_{kural_idx}',
                            'birlikte_istisnasi_oner',
                            'Birlikte grubu ayni gun calismali',
                            {'personel_ids': [p1_id, p2_id], 'gun': g},
                        )
                        for family_key, slot_list in self.birlikte_family_slots.items():
                            hard_add(
                                model.Add(
                                    sum(x[p1_id, g, s] for s in slot_list)
                                    == sum(x[p2_id, g, s] for s in slot_list)
                                ),
                                f'H9_BIRLIKTE_AILE_{kural_idx}',
                                'birlikte_istisnasi_oner',
                                'Birlikte grubu ayni gorev ailesinde calismali',
                                {
                                    'personel_ids': [p1_id, p2_id],
                                    'gun': g,
                                    'gorev_ailesi': family_key,
                                },
                            )

        # H10. Non-exclusive görev havuzu varsa sadece o havuzdan seçim yap
        for role, allowed_ids in self.gorev_havuzlari.items():
            role_slotlari = self.role_slots.get(role, [])
            if not role_slotlari:
                continue
            for p in self.personel_listesi:
                if p.id in allowed_ids:
                    continue
                for g in range(1, self.gun_sayisi + 1):
                    for s in role_slotlari:
                        hard_add(
                            model.Add(x[p.id, g, s] == 0),
                            'H10_GOREV_HAVUZU',
                            'exclusive_gevset',
                            'Gorev havuzu disindaki personel atanamaz',
                        )

        # H10b. Kişi-gün iskeleti — ön planlı günlere sadakat
        if self._gun_iskeleti_hard_mi():
            planlanan_gunler_map = self._planlanan_gunler_map()
            uygulanabilir_ids = self._gun_iskeleti_uygulanabilir_ids()
            gun_tol = self._gun_iskeleti_toleransi()
            for p in self.personel_listesi:
                if p.id not in uygulanabilir_ids:
                    continue
                planlanan_gunler = sorted(planlanan_gunler_map.get(p.id, set()))
                if not planlanan_gunler:
                    continue
                hedef = self.hedefler.get(p.id, {})
                hedef_toplam = int(hedef.get('hedef_toplam', len(planlanan_gunler)) or 0)
                planlanan_hesap = sum(kisi_gun_atama[p.id, g] for g in planlanan_gunler)
                alt_sinir = max(0, min(len(planlanan_gunler), hedef_toplam) - gun_tol)
                hard_add(
                    model.Add(planlanan_hesap >= alt_sinir),
                    'PLAN_GUN_ISKELETI',
                    'plan_gevset',
                    'Gun iskeleti hard alt siniri',
                )
        
        # SOFT CONSTRAINTS
        penalties = []

        WEIGHT_AYRI_SOFT = 2000
        for idx, (p1_id, p2_id, g, _bina_id, slot_list) in enumerate(soft_ayri_cakismalari):
            ayni_bina_toplam = (
                sum(x[p1_id, g, s] for s in slot_list)
                + sum(x[p2_id, g, s] for s in slot_list)
            )
            cakisiyor = model.NewBoolVar(f'ayri_soft_cakisma_{idx}')
            model.Add(ayni_bina_toplam == 2).OnlyEnforceIf(cakisiyor)
            model.Add(ayni_bina_toplam <= 1).OnlyEnforceIf(cakisiyor.Not())
            penalties.append(cakisiyor * WEIGHT_AYRI_SOFT)

        # S0. Boş slot cezası (çok büyük - boş bırakmamaya çalışsın)
        WEIGHT_BOS_SLOT = 100000
        for bos_mu in bos_slotlar:
            penalties.append(bos_mu * WEIGHT_BOS_SLOT)

        # S0b. Gün iskeleti sadakati
        if self._gun_iskeleti_aktif_mi():
            planlanan_gunler_map = self._planlanan_gunler_map()
            uygulanabilir_ids = self._gun_iskeleti_uygulanabilir_ids()
            gun_tol = self._gun_iskeleti_toleransi()
            gun_iskeleti_agirligi = self._gun_iskeleti_agirligi()
            for p in self.personel_listesi:
                if p.id not in uygulanabilir_ids:
                    continue
                planlanan_gunler = sorted(planlanan_gunler_map.get(p.id, set()))
                if not planlanan_gunler:
                    continue
                hedef = self.hedefler.get(p.id, {})
                hedef_toplam = int(hedef.get('hedef_toplam', len(planlanan_gunler)) or 0)
                planlanan_hesap = sum(kisi_gun_atama[p.id, g] for g in planlanan_gunler)
                planlanan_hedef = min(len(planlanan_gunler), hedef_toplam)
                eksik_plan = model.NewIntVar(0, planlanan_hedef, f'gun_iskeleti_eksik_{p.id}')
                model.Add(eksik_plan >= planlanan_hedef - planlanan_hesap)
                penalties.append(eksik_plan * gun_iskeleti_agirligi)

        # S0c. Rol iskeleti sadakati — planlanan role uygun slot'a atama tercih edilir
        WEIGHT_ROL_ISKELET = WEIGHT_GUN_TIPI // 3  # ~165, düşük soft ceza
        if self._gun_iskeleti_aktif_mi():
            rol_gunleri_map = self._planlanan_rol_gunleri_map()
            uygulanabilir_ids = self._gun_iskeleti_uygulanabilir_ids()
            for p in self.personel_listesi:
                if p.id not in uygulanabilir_ids:
                    continue
                kisi_rol_gunleri = rol_gunleri_map.get(p.id, {})
                if not kisi_rol_gunleri:
                    continue
                for gun, planlanan_rol in kisi_rol_gunleri.items():
                    if gun < 1 or gun > self.gun_sayisi:
                        continue
                    planlanan_slotlar = self.role_slots.get(planlanan_rol, [])
                    if not planlanan_slotlar:
                        continue
                    # Planlanan role uygun slot'lara atanmışsa 0 ceza,
                    # farklı slot'a atanmışsa düşük ceza
                    farkli_slot_atama = sum(
                        x[p.id, gun, s]
                        for s in range(self.slot_sayisi)
                        if s not in planlanan_slotlar
                    )
                    if isinstance(farkli_slot_atama, int) and farkli_slot_atama == 0:
                        continue
                    sapma = model.NewIntVar(0, self.slot_sayisi, f'rol_iskelet_sapma_{p.id}_{gun}')
                    model.Add(sapma >= farkli_slot_atama)
                    penalties.append(sapma * WEIGHT_ROL_ISKELET)

        plan_penalty_multiplier = self._plan_penalty_multiplier()
        plan_gun_tipi_tol = self._plan_gun_tipi_toleransi()
        plan_gorev_tol = self._plan_gorev_kota_toleransi()
        
        # S1. Gorev kotalari ? HARD ust sinir + SOFT eksik cezasi
        for p in self.personel_listesi:
            hedef = self.hedefler.get(p.id, {})
            gorev_kotalari = hedef.get('gorev_kotalari', {})
            for role, slot_list in self.role_slots.items():
                role_atama = sum(x[p.id, g, s] for g in range(1, self.gun_sayisi + 1) for s in slot_list)
                if role not in gorev_kotalari:
                    continue

                kota = gorev_kotalari.get(role, 0)
                if self._plan_aktif_mi():
                    ust_sinir = kota if kota <= 0 else kota + plan_gorev_tol
                    hard_add(
                        model.Add(role_atama <= ust_sinir),
                        'S1_GOREV_KOTA_PLAN',
                        'plan_gevset',
                        'Plan gorev kotasi ust siniri',
                    )
                    if kota > 0:
                        hard_add(
                            model.Add(role_atama >= max(0, kota - plan_gorev_tol)),
                            'S1_GOREV_KOTA_PLAN',
                            'plan_gevset',
                            'Plan gorev kotasi alt siniri',
                        )
                elif kota > 0:
                    hard_add(
                        model.Add(role_atama <= kota),
                        'S1_GOREV_KOTA',
                        'gorev_kota_kontrol',
                        'Gorev kotasi ust siniri',
                    )

                eksik = model.NewIntVar(0, self.gun_sayisi * len(slot_list), f'role_eksik_{p.id}_{role}')
                model.Add(eksik >= kota - role_atama)
                slot_agirlik = self.slot_agirliklari.get(role, 1)
                penalties.append(eksik * WEIGHT_GOREV_KOTA * slot_agirlik * plan_penalty_multiplier)
        
        # S2. Gun tipi kotalari
        for p in self.personel_listesi:
            hedef = self.hedefler.get(p.id, {})
            hedef_tipler = hedef.get('hedef_tipler', {})
            for tip in GUN_TIPLERI:
                tip_hedef = hedef_tipler.get(tip, 0)
                tip_gunleri = self.gunler_by_tip.get(tip, [])
                if tip_gunleri:
                    tip_atama = sum(x[p.id, g, s] for g in tip_gunleri for s in range(self.slot_sayisi))
                    if self._plan_aktif_mi():
                        hard_add(
                            model.Add(tip_atama <= tip_hedef + plan_gun_tipi_tol),
                            'S2_GUN_TIPI_PLAN',
                            'plan_gevset',
                            'Plan gun tipi kotasi ust siniri',
                        )
                        hard_add(
                            model.Add(tip_atama >= max(0, tip_hedef - plan_gun_tipi_tol)),
                            'S2_GUN_TIPI_PLAN',
                            'plan_gevset',
                            'Plan gun tipi kotasi alt siniri',
                        )
                    fazla = model.NewIntVar(0, len(tip_gunleri) * self.slot_sayisi, f'tip_fazla_{p.id}_{tip}')
                    eksik = model.NewIntVar(0, len(tip_gunleri) * self.slot_sayisi, f'tip_eksik_{p.id}_{tip}')
                    model.Add(tip_atama - tip_hedef == fazla - eksik)
                    penalties.append(fazla * WEIGHT_GUN_TIPI * plan_penalty_multiplier)
                    penalties.append(eksik * WEIGHT_GUN_TIPI * plan_penalty_multiplier)

        # S2b. Esdeger gun tipi gecisi — asil tip doluysa esdeger tipe kayabilir
        # Esdeger grup toplami (asil + esdeger) hedef toplamini karsilasin.
        # Ceza: asil tipte kalmak 0 ceza, esdeger tipe gecmek dusuk ceza.
        WEIGHT_ESDEGER_GECIS = WEIGHT_GUN_TIPI // 4  # Esdeger gecis cezasi dusuk
        esdeger_isle = set()
        for p in self.personel_listesi:
            hedef = self.hedefler.get(p.id, {})
            hedef_tipler = hedef.get('hedef_tipler', {})
            for tip in GUN_TIPLERI:
                tip_hedef = hedef_tipler.get(tip, 0)
                if tip_hedef <= 0:
                    continue
                esdegerler = ESDEGER_TIP_GRUPLARI.get(tip, [])
                if not esdegerler:
                    continue
                # Tekrar islemeyi onle (hici<->prs gibi ciftler)
                pair_key = (p.id, tuple(sorted([tip] + esdegerler)))
                if pair_key in esdeger_isle:
                    continue
                esdeger_isle.add(pair_key)

                # Asil tip + esdeger tiplerin toplam atamasi
                tum_gunler = list(self.gunler_by_tip.get(tip, []))
                for es_tip in esdegerler:
                    tum_gunler.extend(self.gunler_by_tip.get(es_tip, []))
                if not tum_gunler:
                    continue

                # Grup toplam hedefi
                grup_hedef = tip_hedef
                for es_tip in esdegerler:
                    grup_hedef += hedef_tipler.get(es_tip, 0)

                grup_atama = sum(x[p.id, g, s] for g in tum_gunler for s in range(self.slot_sayisi))
                grup_eksik = model.NewIntVar(0, self.gun_sayisi, f'esdeger_eksik_{p.id}_{tip}')
                model.Add(grup_eksik >= grup_hedef - grup_atama)
                # Esdeger grup toplami hedefi karsilamiyorsa ceza
                penalties.append(grup_eksik * WEIGHT_ESDEGER_GECIS * plan_penalty_multiplier)

                # Asil tipten esdeger tipe kayan miktar icin dusuk ek ceza
                asil_gunler = self.gunler_by_tip.get(tip, [])
                if asil_gunler:
                    asil_atama = sum(x[p.id, g, s] for g in asil_gunler for s in range(self.slot_sayisi))
                    kayma = model.NewIntVar(0, self.gun_sayisi, f'esdeger_kayma_{p.id}_{tip}')
                    model.Add(kayma >= tip_hedef - asil_atama)
                    # Kayma olursa cok dusuk ceza — tercih asil tipte kalmak
                    penalties.append(kayma * (WEIGHT_ESDEGER_GECIS // 2))
        
        # S3. Toplam hedef ? yetkili planda hard esitlik + SOFT eksik cezasi
        for p in self.personel_listesi:
            hedef = self.hedefler.get(p.id, {})
            # Varsayilan 0 (eleme asamasi 1066 ile ayni). Onceden burada 3 vardi;
            # hedefi eksik kisi elenip 0'a sabitlenirken S3 onu 3 sayiyor,
            # plan-hard modda model.Add(0 == 3) => dogrudan INFEASIBLE uretiyordu.
            hedef_toplam = hedef.get('hedef_toplam', 0)
            toplam_atama = sum(x[p.id, g, s] for g in range(1, self.gun_sayisi + 1) for s in range(self.slot_sayisi))
            if self._plan_toplam_hard_mi():
                hard_add(
                    model.Add(toplam_atama == hedef_toplam),
                    'S3_TOPLAM_HEDEF_PLAN',
                    'plan_gevset',
                    'Plan toplam hedef hard esitligi',
                )
            else:
                hard_add(
                    model.Add(toplam_atama <= hedef_toplam),
                    'S3_TOPLAM_HEDEF',
                    'hedef_kontrol',
                    'Toplam hedef ust siniri',
                )
            eksik = model.NewIntVar(0, self.gun_sayisi, f'toplam_eksik_{p.id}')
            model.Add(eksik >= hedef_toplam - toplam_atama)
            penalties.append(eksik * WEIGHT_TOPLAM * plan_penalty_multiplier)

        # S4. Birlikte tutma (SOFT CONSTRAINT)
        # 1) Biri atanıp diğeri boş kalmasın (eski aynı-gün tercihi korunur)
        # 2) Aynı gün çalışıyorlarsa aynı/eşdeğer görev ailesinde olsunlar.
        #    AMELİYATHANE / MAVİ KOD / KVC birlikte üyeleri için tek aile kabul edilir.
        WEIGHT_BIRLIKTE_AILE = 2000
        WEIGHT_BIRLIKTE_HEDEF = 6000
        for kural in self.kurallar:
            if (
                kural.tur == 'birlikte'
                and getattr(kural, 'politika', 'kullanici_onayli') == 'soft'
                and not bool(getattr(kural, 'asla_gevsetme', False))
            ):
                valid_ids = self._birlikte_gecerli_ids(kural)
                
                if len(valid_ids) >= 2:
                    birlikte_tercih_hedefi = self._birlikte_tercih_hedefi(valid_ids)
                    # SOFT: Birlikte çalışma tercihi - all-pairs karşılaştırma
                    for i in range(len(valid_ids)):
                        for j in range(i + 1, len(valid_ids)):
                            p1_id = valid_ids[i]
                            p2_id = valid_ids[j]
                            p1_obj = self.personeller[p1_id]
                            p2_obj = self.personeller[p2_id]
                            # Her iki kişinin de müsait olduğu günleri bul
                            ortak_gunler = p1_obj.musait_gunler & p2_obj.musait_gunler
                            uyumlu_gunler = []

                            for g in ortak_gunler:
                                if (
                                    (p1_id, g) in self.birlikte_istisna_set
                                    or (p2_id, g) in self.birlikte_istisna_set
                                ):
                                    continue
                                p1_atama = sum(x[p1_id, g, s] for s in range(self.slot_sayisi))
                                p2_atama = sum(x[p2_id, g, s] for s in range(self.slot_sayisi))

                                # Eski davranışı koru: biri atanıp diğeri boş kalıyorsa ceza
                                fark = model.NewIntVar(0, 1, f'birlikte_fark_{p1_id}_{p2_id}_{g}')
                                model.Add(p1_atama - p2_atama <= fark)
                                model.Add(p2_atama - p1_atama <= fark)
                                penalties.append(fark * WEIGHT_BIRLIKTE)

                                same_day = model.NewBoolVar(f'birlikte_same_day_{p1_id}_{p2_id}_{g}')
                                model.Add(p1_atama + p2_atama == 2).OnlyEnforceIf(same_day)
                                model.Add(p1_atama + p2_atama <= 1).OnlyEnforceIf(same_day.Not())

                                same_family_vars = []
                                for family_idx, slot_list in enumerate(self.birlikte_family_slots.values()):
                                    p1_family_atama = sum(x[p1_id, g, s] for s in slot_list)
                                    p2_family_atama = sum(x[p2_id, g, s] for s in slot_list)
                                    same_family = model.NewBoolVar(
                                        f'birlikte_aile_{p1_id}_{p2_id}_{g}_{family_idx}'
                                    )
                                    model.Add(p1_family_atama + p2_family_atama == 2).OnlyEnforceIf(same_family)
                                    model.Add(p1_family_atama + p2_family_atama <= 1).OnlyEnforceIf(same_family.Not())
                                    same_family_vars.append(same_family)

                                birlikte_uyumlu = model.NewBoolVar(f'birlikte_uyumlu_{p1_id}_{p2_id}_{g}')
                                if same_family_vars:
                                    model.Add(sum(same_family_vars) == birlikte_uyumlu)
                                else:
                                    model.Add(birlikte_uyumlu == 0)

                                # Aynı gün çalışıp farklı aileye düşerlerse ekstra ceza
                                uyumsuz_ayni_gun = model.NewBoolVar(f'birlikte_uyumsuz_{p1_id}_{p2_id}_{g}')
                                model.Add(birlikte_uyumlu <= same_day)
                                model.Add(uyumsuz_ayni_gun >= same_day - birlikte_uyumlu)
                                model.Add(uyumsuz_ayni_gun <= same_day)
                                model.Add(uyumsuz_ayni_gun + birlikte_uyumlu <= 1)
                                penalties.append(uyumsuz_ayni_gun * WEIGHT_BIRLIKTE_AILE)
                                uyumlu_gunler.append(birlikte_uyumlu)

                            if birlikte_tercih_hedefi > 0 and uyumlu_gunler:
                                birlikte_eksik = model.NewIntVar(
                                    0, birlikte_tercih_hedefi,
                                    f'birlikte_hedef_eksik_{p1_id}_{p2_id}'
                                )
                                model.Add(birlikte_eksik >= birlikte_tercih_hedefi - sum(uyumlu_gunler))
                                penalties.append(birlikte_eksik * WEIGHT_BIRLIKTE_HEDEF)
        
        # S5. Homojen dağılım - Nöbetleri ay geneline yay (haftada ~1 nöbet hedefi)
        # Mazeretler izin veriyorsa yay, vermiyorsa sıkışık tutulabilir
        for p in self.personel_listesi:
            hedef = self.hedefler.get(p.id, {})
            hedef_toplam = hedef.get('hedef_toplam', 0)

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
                        model.Add(fazla >= hafta_nobet - 1)
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
        
        if not self._plan_aktif_mi():
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
                            hedef_toplam = hedef.get('hedef_toplam', 0)
                            # Hedefin altında kalırsa ceza (eksik olanı doldur)
                            eksik = model.NewIntVar(0, self.gun_sayisi, f'yillik_eksik_{p.id}')
                            model.Add(eksik >= hedef_toplam - toplam_atama)
                            penalties.append(eksik * WEIGHT_YILLIK * min(eksik_bonus, 3))
                        elif fark > 1:  # Ortalamadan 1+ fazla
                            # Bu kişiye daha az nöbet ver
                            fazla_ceza = int(fark)
                            toplam_atama = sum(x[p.id, g, s] for g in range(1, self.gun_sayisi + 1) for s in range(self.slot_sayisi))
                            hedef = self.hedefler.get(p.id, {})
                            hedef_toplam = hedef.get('hedef_toplam', 0)
                            # Hedefin üstüne çıkarsa ceza (fazla tutanı azalt)
                            fazla = model.NewIntVar(0, self.gun_sayisi, f'yillik_fazla_{p.id}')
                            model.Add(toplam_atama - hedef_toplam <= fazla)
                            penalties.append(fazla * WEIGHT_YILLIK * min(fazla_ceza, 3))

            # S6b. Özel görev yıllık dengeleme - Geçmiş görev dağılımını eşitle
            gecmis_gorev_olan = [p for p in self.personel_listesi
                                if hasattr(p, 'gecmis_gorevler') and p.gecmis_gorevler]
            if len(gecmis_gorev_olan) >= 2:
                # Tüm özel görev isimlerini topla
                tum_gorev_isimleri = set()
                for p in gecmis_gorev_olan:
                    tum_gorev_isimleri.update(p.gecmis_gorevler.keys())

                for gorev_adi in tum_gorev_isimleri:
                    # Bu görev için geçmişi olan personelleri bul
                    gecmis_list = [(p, p.gecmis_gorevler.get(gorev_adi, 0))
                                   for p in gecmis_gorev_olan
                                   if p.gecmis_gorevler.get(gorev_adi, 0) > 0 or
                                   p.gorev_kotalari.get(gorev_adi, 0) > 0]
                    if len(gecmis_list) < 2:
                        continue

                    ort = sum(g for _, g in gecmis_list) / len(gecmis_list)

                    # Görev slotlarını bul
                    gorev_slotlari = [s for s, g in enumerate(self.gorevler)
                                      if g.base_name == gorev_adi or g.ad == gorev_adi]
                    if not gorev_slotlari:
                        continue

                    for p, gecmis in gecmis_list:
                        fark = gecmis - ort
                        if abs(fark) <= 1:
                            continue

                        # Bu kişinin bu görevdeki atama sayısı
                        gorev_atama = sum(x[p.id, g, s]
                                          for g in range(1, self.gun_sayisi + 1)
                                          for s in gorev_slotlari)

                        if fark < -1:  # Ortalamadan eksik - daha fazla ata
                            eksik_bonus = min(int(abs(fark)), 3)
                            kota = p.gorev_kotalari.get(gorev_adi, 1)
                            eksik_var = model.NewIntVar(0, self.gun_sayisi, f'gorev_yillik_eksik_{p.id}_{gorev_adi}')
                            model.Add(kota - gorev_atama <= eksik_var)
                            penalties.append(eksik_var * WEIGHT_YILLIK * eksik_bonus)
                        elif fark > 1:  # Ortalamadan fazla - daha az ata
                            fazla_ceza = min(int(fark), 3)
                            kota = p.gorev_kotalari.get(gorev_adi, 1)
                            fazla_var = model.NewIntVar(0, self.gun_sayisi, f'gorev_yillik_fazla_{p.id}_{gorev_adi}')
                            model.Add(gorev_atama - kota <= fazla_var)
                            penalties.append(fazla_var * WEIGHT_YILLIK * fazla_ceza)

        # S7. Panik faktörü - Sıkışık kişilere öncelik
        # Mazereti çok olan ve hedefi yüksek olan kişilere öncelik ver
        for p in self.personel_listesi:
            mazeret_sayisi = len(p.mazeret_gunleri)
            musait_gun = self.gun_sayisi - mazeret_sayisi
            hedef = self.hedefler.get(p.id, {})
            hedef_toplam = hedef.get('hedef_toplam', 0)
            
            if musait_gun > 0 and hedef_toplam > 0:
                # Panik oranı = hedef / müsait gün
                # Oran yüksekse (sıkışıksa) hedefin altına düşmemeli
                panik_orani = hedef_toplam / musait_gun
                
                if panik_orani > 0.3:  # %30'dan fazla sıkışıksa
                    toplam_atama = sum(x[p.id, g, s] for g in range(1, self.gun_sayisi + 1) for s in range(self.slot_sayisi))
                    # Hedefin altına düşerse ağır ceza
                    eksik = model.NewIntVar(0, self.gun_sayisi, f'panik_eksik_{p.id}')
                    model.Add(eksik >= hedef_toplam - toplam_atama)
                    # Panik oranına göre ceza çarpanı
                    carpan = min(int(panik_orani * 10), 5)
                    penalties.append(eksik * WEIGHT_PANIK * carpan)
        
        if penalties:
            model.Minimize(sum(penalties))
        
        return _SolveContext(
            cp=cp, model=model, x=x, kisi_gun_atama=kisi_gun_atama,
            bos_slotlar=bos_slotlar, penalties=penalties,
            eliminated_vars=eliminated_vars,
            unsat_registry=unsat_registry,
        )

    def _solve(self, context: _SolveContext):
        """Leksikografik (çok geçişli) çözüm.

        Tier 1 boş slot sayısını KESİN öncelikle minimize eder; Tier 2 bu
        minimumu sabitleyip (``Σ bos ≤ en_az_bos``) tam ağırlıklı amacı
        (adalet/kota/plan) minimize eder. Böylece hiçbir yumuşak ceza bir boş
        slot pahasına iyileştirilemez — bu, ağırlık büyüklüğünden (WEIGHT_BOS_SLOT)
        bağımsız YAPISAL bir garantidir. Zaman bütçesi iki geçişe paylaştırılır;
        herhangi bir geçiş çözülemezse tek geçişli ağırlıklı çözüme düşülür.
        """
        cp = context.cp
        model = context.model
        toplam_sure = max(1, int(self.max_sure))
        self._leksikografik_kullanildi = False

        # 1 saniyelik bütçe ikiye bölünemez → tek geçiş (davranış değişmez).
        leksikografik_aktif = (
            self.leksikografik
            and toplam_sure >= 2
            and bool(context.penalties)
            and bool(context.bos_slotlar)
        )

        if leksikografik_aktif:
            tier1_solver = cp.CpSolver()
            tier1_solver.parameters.num_search_workers = 4
            tier1_solver.parameters.max_time_in_seconds = max(
                1, min(toplam_sure - 1, (toplam_sure * 2) // 5)
            )
            model.Minimize(sum(context.bos_slotlar))
            tier1_status = tier1_solver.Solve(model)

            if tier1_status in (cp.OPTIMAL, cp.FEASIBLE):
                en_az_bos = int(round(tier1_solver.ObjectiveValue()))
                model.Add(sum(context.bos_slotlar) <= en_az_bos)

                tier2_solver = cp.CpSolver()
                tier2_solver.parameters.num_search_workers = 4
                tier2_solver.parameters.max_time_in_seconds = max(
                    1, toplam_sure - (int(tier1_solver.WallTime()) + 1)
                )
                model.Minimize(sum(context.penalties))
                tier2_status = tier2_solver.Solve(model)
                self._leksikografik_kullanildi = True
                if tier2_status in (cp.OPTIMAL, cp.FEASIBLE):
                    return tier2_solver, tier2_status
                # Tier 2 sonuç veremedi → boş slotu minimal olan Tier 1 çözümü.
                return tier1_solver, tier1_status
            # Tier 1 çözülemedi → tek geçişli çözüme düş.

        solver = cp.CpSolver()
        solver.parameters.max_time_in_seconds = toplam_sure
        solver.parameters.num_search_workers = 4
        if context.penalties:
            model.Minimize(sum(context.penalties))
        status = solver.Solve(model)
        return solver, status

    def diagnose_with_unsat_core(self, max_sure_saniye: int = 10) -> Dict:
        """INFEASIBLE durumda CP-SAT assumptions ile cakisabilecek kural gruplarini raporlar.

        Bu metod normal cozum davranisini degistirmez; sadece hata yolunda ikinci,
        kisa ve tek worker'li bir model kurup sufficient assumptions core'u okur.
        """
        cp = _get_cp_model()
        try:
            context = self._build_model(cp, collect_unsat_core=True)
            solver = cp.CpSolver()
            solver.parameters.max_time_in_seconds = max(1, int(max_sure_saniye or 1))
            solver.parameters.num_search_workers = 1
            status = solver.Solve(context.model)
            status_name = solver.StatusName(status)

            result = {
                'enabled': True,
                'status': 'INFEASIBLE' if status == cp.INFEASIBLE else status_name,
                'solver_status_name': status_name,
                'solver_wall_time_s': round(solver.WallTime(), 3),
            }
            registry = context.unsat_registry
            if status != cp.INFEASIBLE or registry is None:
                result.update({
                    'core_size': 0,
                    'core_groups': [],
                    'suggested_actions': [],
                    'note': 'Assumption modeli INFEASIBLE donmedi; core raporu yok.',
                })
                return result

            core_fn = getattr(solver, 'sufficient_assumptions_for_infeasibility', None)
            if core_fn is None:
                core_fn = getattr(solver, 'SufficientAssumptionsForInfeasibility', None)
            if core_fn is None:
                result.update({
                    'core_size': 0,
                    'core_groups': [],
                    'suggested_actions': [],
                    'note': 'OR-Tools assumption core API bulunamadi.',
                })
                return result

            result.update(registry.describe_core(core_fn()))
            if result.get('core_size', 0) == 0:
                result['note'] = (
                    'Model infeasible ama core bos. Neden, guard edilmeyen temel '
                    'kisitlar veya degisken eliminasyonu olabilir.'
                )
            return result
        except Exception as exc:
            return {
                'enabled': True,
                'status': 'ERROR',
                'error': str(exc)[:300],
                'core_size': 0,
                'core_groups': [],
                'suggested_actions': [],
            }

    def _extract_solution(self, context: _SolveContext, solver: Any, status: int, sure_ms: int) -> SolverSonuc:
        cp = context.cp
        atamalar = []
        kisi_sayac = {p.id: {'toplam': 0, 'tipler': {t: 0 for t in GUN_TIPLERI}, 'gorevler': {}} for p in self.personel_listesi}
        bos_slot_sayisi = sum(1 for bos_mu in context.bos_slotlar if solver.Value(bos_mu) == 1)
            
        for g in range(1, self.gun_sayisi + 1):
            gun_tipi = self.gun_tipleri.get(g, 'hici')
            for s in range(self.slot_sayisi):
                for p in self.personel_listesi:
                    if solver.Value(context.x[p.id, g, s]) == 1:
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
        birlikte_grup_istatistikleri = self._hesapla_birlikte_grup_istatistikleri(atamalar)
        plan_sapmalari = self._hesapla_plan_sapmalari(kisi_sayac, atamalar)
            
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
            'objective': solver.ObjectiveValue() if context.penalties else 0,
            'leksikografik_kullanildi': self._leksikografik_kullanildi,
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
            'eliminated_vars': context.eliminated_vars,
            'kalite_skoru': self._hesapla_kalite_skoru(kisi_sayac, atamalar, toplam_atama, toplam_slot),
            'plan': {
                'aktif': self._plan_aktif_mi(),
                'plan_hash': self.plan_kontrati.get('plan_hash'),
                'kaynak': self.plan_kontrati.get('kaynak'),
                'olusturulan_ara_gun': self.plan_kontrati.get('olusturulan_ara_gun'),
                'uygulama': self.plan_uygulama,
                'gun_iskeleti_aktif': self._gun_iskeleti_aktif_mi(),
                'gun_iskeleti_uygulanabilir_ids': sorted(self._gun_iskeleti_uygulanabilir_ids()),
            } if self.plan_kontrati else {},
            'plan_sapmalari': plan_sapmalari,
            'birlikte_gruplar': birlikte_grup_istatistikleri,
            'birlikte_esdeger_aile': BIRLIKTE_ESDEGER_GOREV_AILE_ADI,
            'kisi_detay': [
                {'personel_id': str(p.id), 'personel_ad': p.ad, 'toplam': kisi_sayac[p.id]['toplam'],
                 'tipler': kisi_sayac[p.id]['tipler'], 'gorevler': kisi_sayac[p.id]['gorevler']}
                for p in self.personel_listesi
            ],
            'role_slots': {k: v for k, v in self.role_slots.items()},
            'kisitli_debug': kisitli_debug,
            'kisitlama_istisna_debug': self.kisitlama_istisna_debug,
            'feasibility_debug': self._build_feasibility_diagnostics(limit_preview=30) if bos_slot_sayisi > 0 else {},
            'bos_slot_aciklamalari': self._bos_slot_aciklamalari(atamalar) if bos_slot_sayisi > 0 else [],
            'gorev_listesi': [{'idx': i, 'ad': g.ad, 'base_name': g.base_name} for i, g in enumerate(self.gorevler)]
        }
        return SolverSonuc(basarili=True, atamalar=atamalar, istatistikler=istatistikler,
                          sure_ms=sure_ms, mesaj='OPTIMAL' if status == cp.OPTIMAL else 'FEASIBLE')

    def _build_failure_result(self, context: _SolveContext, solver: Any, status: int, sure_ms: int) -> SolverSonuc:
        cp = context.cp
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
                              'plan': {
                                  'aktif': self._plan_aktif_mi(),
                                  'plan_hash': self.plan_kontrati.get('plan_hash'),
                                  'kaynak': self.plan_kontrati.get('kaynak'),
                                  'olusturulan_ara_gun': self.plan_kontrati.get('olusturulan_ara_gun'),
                                  'uygulama': self.plan_uygulama,
                                  'gun_iskeleti_aktif': self._gun_iskeleti_aktif_mi(),
                                  'gun_iskeleti_uygulanabilir_ids': sorted(self._gun_iskeleti_uygulanabilir_ids()),
                              } if self.plan_kontrati else {},
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

    def coz(self) -> SolverSonuc:
        baslangic = time.time()
        manual_conflicts = self._manual_hard_conflict_diagnostics()
        if manual_conflicts:
            return self._manual_conflict_result(baslangic, manual_conflicts)

        cp = _get_cp_model()
        context = self._build_model(cp)
        solver, status = self._solve(context)
        sure_ms = int((time.time() - baslangic) * 1000)

        if status in [cp.OPTIMAL, cp.FEASIBLE]:
            return self._extract_solution(context, solver, status, sure_ms)
        return self._build_failure_result(context, solver, status, sure_ms)
