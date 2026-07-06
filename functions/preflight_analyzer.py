from __future__ import annotations

from typing import Dict, List, Set, Tuple, Optional
from dataclasses import dataclass

from utils import GUN_TIPLERI, canonicalize_role_name, normalize_id
from solver_models import SolverPersonel, SolverGorev, SolverKural, SolverAtama


@dataclass
class PreflightOptions:
    ara_gun: int = 2
    max_preview: int = 30


def _role_slots_and_exclusive(gorevler: List[SolverGorev]):
    role_slots: Dict[str, List[int]] = {}
    exclusive_roles: Set[str] = set()
    for idx, g in enumerate(gorevler or []):
        base = g.base_name if getattr(g, 'base_name', None) else g.ad
        role_slots.setdefault(base, []).append(idx)
        if getattr(g, 'exclusive', False):
            exclusive_roles.add(base)
    return role_slots, exclusive_roles


def _manual_role_fill_counts(manuel_atamalar: List[SolverAtama], gorevler: List[SolverGorev]):
    role_slots, _ = _role_slots_and_exclusive(gorevler)
    slot_to_role = {}
    for idx, g in enumerate(gorevler or []):
        slot_to_role[idx] = g.base_name if getattr(g, 'base_name', None) else g.ad
    fill = {}
    for m in manuel_atamalar or []:
        role = slot_to_role.get(getattr(m, 'slot_idx', -1))
        gun = int(getattr(m, 'gun', 0) or 0)
        if not role or gun < 1:
            continue
        key = (gun, role)
        fill[key] = fill.get(key, 0) + 1
    def role_full_on_day(role: str, gun: int) -> bool:
        need = len(role_slots.get(role, []))
        return fill.get((gun, role), 0) >= max(1, need)
    return role_full_on_day, role_slots


def _build_kisitlama_istisna_map(kisitlama_istisnalari: List[Dict], personeller: List[SolverPersonel], gorevler: List[SolverGorev]):
    ids = {normalize_id(p.id) for p in personeller or []}
    roles = set()
    for g in gorevler or []:
        roles.add(g.base_name if getattr(g, 'base_name', None) else g.ad)
    m: Dict[Tuple[int, int], Set[str]] = {}
    for raw in kisitlama_istisnalari or []:
        try:
            pid = normalize_id(raw.get('personel_id'))
            gun = int(raw.get('gun', 0) or 0)
            ist = raw.get('istisna_gorev') or raw.get('gorevAdi')
            if ist:
                ist = canonicalize_role_name(ist)
            if pid in ids and gun >= 1 and ist in roles:
                m.setdefault((pid, gun), set()).add(ist)
        except Exception:
            continue
    return m


def _person_can_take_role_day(
    p: SolverPersonel,
    role: str,
    gun: int,
    exclusive_roles: Set[str],
    gorev_havuzlari: Dict[str, Set[int]],
    kisitlama_istisna_map: Dict[Tuple[int, int], Set[str]],
) -> bool:
    if p is None:
        return False
    if gun in getattr(p, 'mazeret_gunleri', set()):
        return False
    allowed_exception_roles = kisitlama_istisna_map.get((normalize_id(p.id), gun), set())
    # H7: kisitli_gorev / tasma_gorevi
    if getattr(p, 'kisitli_gorev', None) and role != p.kisitli_gorev and role not in allowed_exception_roles:
        if not (getattr(p, 'tasma_gorevi', None) and role == p.tasma_gorevi):
            return False
    # H8: exclusive + havuz
    if role in exclusive_roles and getattr(p, 'kisitli_gorev', None) != role and getattr(p, 'tasma_gorevi', None) != role:
        hav = gorev_havuzlari.get(role)
        if hav is None or normalize_id(p.id) not in hav:
            return False
    # H10: genel havuz
    allowed = gorev_havuzlari.get(role)
    if allowed is not None and normalize_id(p.id) not in allowed:
        if not (getattr(p, 'kisitli_gorev', None) == role or getattr(p, 'tasma_gorevi', None) == role or role in allowed_exception_roles):
            return False
    return True


def _any_role_available_on_day(p: SolverPersonel, gun: int, roles: List[str], exclusive_roles: Set[str], gorev_havuzlari: Dict[str, Set[int]], kisitlama_istisna_map, role_full_on_day) -> bool:
    for role in roles:
        if role_full_on_day(role, gun):
            continue
        if _person_can_take_role_day(p, role, gun, exclusive_roles, gorev_havuzlari, kisitlama_istisna_map):
            return True
    return False


def _max_assignable_with_ara_gun(days: List[int], ara_gun: int) -> int:
    if not days:
        return 0
    sel = 0
    last = -10_000
    for g in sorted(days):
        if g - last > ara_gun:
            sel += 1
            last = g
    return sel


def analyze_preflight(
    gun_sayisi: int,
    gun_tipleri: Dict[int, str],
    personeller: List[SolverPersonel],
    gorevler: List[SolverGorev],
    kurallar: List[SolverKural],
    gorev_havuzlari: Dict[str, Set[int]],
    manuel_atamalar: List[SolverAtama],
    ara_gun: int,
    plan_kontrati: Dict,
    kisitlama_istisnalari: Optional[List[Dict]] = None,
    max_preview: int = 30,
) -> Dict:
    try:
        role_full_on_day, role_slots = _manual_role_fill_counts(manuel_atamalar, gorevler)
        _, exclusive_roles = _role_slots_and_exclusive(gorevler)
        kisitlama_istisna_map = _build_kisitlama_istisna_map(kisitlama_istisnalari or [], personeller, gorevler)
        roles = list(role_slots.keys())

        # 1) İskelet günleri → solver aday uygunluğu
        skeleton = ((plan_kontrati or {}).get('gun_iskeleti') or {})
        pgun_map = skeleton.get('personel_gunleri') or {}
        gecersiz: List[Dict] = []
        gecersiz_count = 0
        pid_to_obj = {normalize_id(p.id): p for p in personeller}
        for raw_pid, days in pgun_map.items():
            try:
                pid = normalize_id(int(raw_pid)) if isinstance(raw_pid, str) and raw_pid.isdigit() else normalize_id(raw_pid)
            except Exception:
                pid = normalize_id(raw_pid)
            p = pid_to_obj.get(pid)
            if p is None:
                continue
            for g in days or []:
                g = int(g)
                if not _any_role_available_on_day(p, g, roles, exclusive_roles, gorev_havuzlari, kisitlama_istisna_map, role_full_on_day):
                    if len(gecersiz) < max_preview:
                        gecersiz.append({'personel_id': pid, 'personel_ad': getattr(p, 'ad', ''), 'gun': g})
                    gecersiz_count += 1
        iskelet_toplam = sum(len(v or []) for v in pgun_map.values()) or 1
        iskelet_uyum_orani = round(1 - (gecersiz_count / iskelet_toplam), 3)

        # 2) Rol kapasite önizleme (ara_gün dahil kişi-üst sınır)
        role_summaries: List[Dict] = []
        risk_preview: List[Dict] = []
        # Hazırla: gün → aday id birliği (rol bazında)
        for role, slots in role_slots.items():
            demand = gun_sayisi * max(1, len(slots))
            daily_union: Dict[int, Set[int]] = {g: set() for g in range(1, gun_sayisi + 1)}
            for g in range(1, gun_sayisi + 1):
                # Slot doluluğuna bakma — üst kapasite tahmininde aday birliğini istiyoruz
                for p in personeller:
                    if _person_can_take_role_day(p, role, g, exclusive_roles, gorev_havuzlari, kisitlama_istisna_map):
                        daily_union[g].add(normalize_id(p.id))
                if len(daily_union[g]) < len(slots) and len(risk_preview) < max_preview:
                    risk_preview.append({'rol': role, 'gun': g, 'gereken': len(slots), 'aday': len(daily_union[g])})
            # Üst kapasite (ara_gün)
            upper = 0
            for p in personeller:
                uygun = [g for g in range(1, gun_sayisi + 1) if normalize_id(p.id) in daily_union[g]]
                upper += _max_assignable_with_ara_gun(uygun, ara_gun)
            eksik = max(0, demand - upper)
            if eksik > 0:
                role_summaries.append({'rol': role, 'talep': demand, 'ust_kapasite': upper, 'eksik': eksik})
        role_summaries.sort(key=lambda x: x['eksik'], reverse=True)

        # 3) Tip fallback ihtiyacı (kişi bazında)
        # Hedefler plan kontratından
        hedefler_map = (plan_kontrati or {}).get('hedefler') or {}
        # Kişi musait_tipler — mazeretlere göre
        musait_tipler: Dict[int, Dict[str, int]] = {}
        for p in personeller:
            pid = normalize_id(p.id)
            mt = {t: 0 for t in GUN_TIPLERI}
            for g in range(1, gun_sayisi + 1):
                if g not in getattr(p, 'mazeret_gunleri', set()):
                    tip = gun_tipleri.get(g)
                    if tip in mt:
                        mt[tip] += 1
            musait_tipler[pid] = mt
        fallback: List[Dict] = []
        fallback_kisi_sayisi = 0
        for pid, hedef in hedefler_map.items():
            try:
                npid = normalize_id(int(pid)) if isinstance(pid, str) and pid.isdigit() else normalize_id(pid)
            except Exception:
                npid = normalize_id(pid)
            mt = musait_tipler.get(npid, {t: 0 for t in GUN_TIPLERI})
            hedef_tipler = (hedef or {}).get('hedef_tipler', {})
            transferler: List[Dict] = []
            # cmt -> cum -> pzr
            cmt_fazla = max(0, int(hedef_tipler.get('cmt', 0)) - mt.get('cmt', 0))
            cum_bos = max(0, mt.get('cum', 0) - int(hedef_tipler.get('cum', 0)))
            pzr_bos = max(0, mt.get('pzr', 0) - int(hedef_tipler.get('pzr', 0)))
            mov = min(cmt_fazla, cum_bos)
            if mov > 0:
                transferler.append({'from': 'cmt', 'to': 'cum', 'adet': mov})
                cmt_fazla -= mov; cum_bos -= mov
            mov = min(cmt_fazla, pzr_bos)
            if mov > 0:
                transferler.append({'from': 'cmt', 'to': 'pzr', 'adet': mov})
                cmt_fazla -= mov; pzr_bos -= mov
            # cum -> pzr
            cum_fazla = max(0, int(hedef_tipler.get('cum', 0)) - mt.get('cum', 0))
            pzr_bos = max(0, mt.get('pzr', 0) - int(hedef_tipler.get('pzr', 0)))
            mov = min(cum_fazla, pzr_bos)
            if mov > 0:
                transferler.append({'from': 'cum', 'to': 'pzr', 'adet': mov})
            # hici <-> prs
            hici_fazla = max(0, int(hedef_tipler.get('hici', 0)) - mt.get('hici', 0))
            prs_bos = max(0, mt.get('prs', 0) - int(hedef_tipler.get('prs', 0)))
            mov1 = min(hici_fazla, prs_bos)
            if mov1 > 0:
                transferler.append({'from': 'hici', 'to': 'prs', 'adet': mov1})
            prs_fazla = max(0, int(hedef_tipler.get('prs', 0)) - mt.get('prs', 0))
            hici_bos = max(0, mt.get('hici', 0) - int(hedef_tipler.get('hici', 0)))
            mov2 = min(prs_fazla, hici_bos)
            if mov2 > 0:
                transferler.append({'from': 'prs', 'to': 'hici', 'adet': mov2})
            if transferler:
                fallback_kisi_sayisi += 1
                if len(fallback) < max_preview:
                    fallback.append({'personel_id': npid, 'transferler': transferler})

        # 4) Zero-candidate preview (slot/gün)
        zero_preview: List[Dict] = []
        # Build candidate check quick: for each day and slot
        for g in range(1, gun_sayisi + 1):
            for s, gv in enumerate(gorevler or []):
                role = gv.base_name if getattr(gv, 'base_name', None) else gv.ad
                cands = 0
                for p in personeller:
                    if _person_can_take_role_day(p, role, g, exclusive_roles, gorev_havuzlari, kisitlama_istisna_map):
                        cands += 1
                        if cands > 0:
                            break
                if cands == 0 and len(zero_preview) < max_preview:
                    zero_preview.append({'gun': g, 'slot_idx': s, 'rol': role})

        # 5) Parametre kontrolleri (hafif)
        slot_sayisi = len(gorevler or [])
        plan_slot_info = slot_sayisi  # basit; ayrıntılı kıyas opsiyonel

        # Skor (basitleştirilmiş ağırlıklandırma)
        kapasite_ceza = 0
        if role_summaries:
            toplam_talep = sum(r['talep'] for r in role_summaries)
            toplam_eksik = sum(r['eksik'] for r in role_summaries)
            kapasite_ceza = min(30, 30 * toplam_eksik / max(1, toplam_talep))
        uyum_puan = max(0, 25 * iskelet_uyum_orani)
        zero_ceza = min(15, 15 * (len(zero_preview) / max(1, gun_sayisi)))
        fallback_ceza = min(20, 5 * fallback_kisi_sayisi)
        manuel_ceza = 0  # ileri çalışma: manuel çakışma analizine bağlanabilir
        skor = int(round(100 - (kapasite_ceza + zero_ceza + fallback_ceza + manuel_ceza) + uyum_puan))
        skor = max(0, min(100, skor))

        sorunlar: List[Dict] = []
        if gecersiz_count > 0:
            sorunlar.append({
                'kod': 'ISKELET_HAVUZ', 'adet': gecersiz_count,
                'oneri': 'İskelet gün seçiminde rol/havuz filtresi ekleyin veya gun_iskeleti_toleransi=2 yapın.'
            })
        for r in role_summaries[:5]:
            sorunlar.append({
                'kod': 'ROL_KAPASITE', 'rol': r['rol'], 'eksik': r['eksik'],
                'oneri': 'Havuzu genişletin veya tasma_görevi kullanın.'
            })
        if fallback:
            sorunlar.append({
                'kod': 'TIP_FALLBACK', 'adet': fallback_kisi_sayisi,
                'oneri': 'Kişi bazlı tip fallback (cum→pzr, cmt→cum→pzr) uygulayın.'
            })
        if zero_preview:
            sorunlar.append({
                'kod': 'ZERO_CANDIDATE', 'adet': len(zero_preview),
                'oneri': 'Aday olmayan slot/günler için havuz/mazeret/kısıtları gözden geçirin.'
            })

        return {
            'skor': skor,
            'ozet': {
                'iskelet_gecersiz_gun': gecersiz_count,
                'rol_kapasite_eksik_rol_sayisi': len(role_summaries),
                'zero_candidate': len(zero_preview),
                'fallback_ihtiyaci_kisi': fallback_kisi_sayisi,
            },
            'sorunlar': sorunlar,
            'metrikler': {
                'iskelet': {
                    'uyum_orani': iskelet_uyum_orani,
                    'gecersiz_gunler': gecersiz,
                },
                'kapasite': {
                    'roller': role_summaries,
                    'riskli_gunler': risk_preview,
                },
                'tip_fallback': {
                    'kisi': fallback,
                },
                'zero_candidate_preview': zero_preview,
                'parametre': {
                    'slot_sayisi': slot_sayisi,
                    'plan_slot_info': plan_slot_info,
                },
            },
        }
    except Exception as exc:
        return {
            'skor': 0,
            'ozet': {},
            'sorunlar': [{'kod': 'ANALIZ_HATASI', 'oneri': str(exc)[:200]}],
            'metrikler': {}
        }
