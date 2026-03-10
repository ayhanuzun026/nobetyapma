"""
Debug oturum logger — her backend çağrısını Firestore'a kaydeder.
Hata olursa sadece uyarı yazar, orijinal isteği asla engellemez.
"""

import json
import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_MAX_INLINE_BYTES = 700_000   # 700KB altı inline yaz
_CHUNK_BYTES = 800_000        # sub-collection parça boyutu
_MAX_FRONTEND_LOGS = 500      # maksimum frontend log satırı


def _json_size(obj) -> int:
    try:
        return len(json.dumps(obj, ensure_ascii=False).encode("utf-8"))
    except Exception:
        return 0


def _chunk_json(obj, chunk_size: int = _CHUNK_BYTES) -> list[str]:
    """Büyük dict'i JSON string parçalarına böl."""
    full = json.dumps(obj, ensure_ascii=False)
    return [full[i:i + chunk_size] for i in range(0, len(full), chunk_size)]


def _write_subcollection(session_ref, part_type: str, obj):
    """700KB üzeri payload'ı sub-collection'a parçalı yaz."""
    chunks = _chunk_json(obj)
    for idx, chunk in enumerate(chunks):
        session_ref.collection("payload").document(f"{part_type}_{idx}").set({
            "type": part_type,
            "part_index": idx,
            "total_parts": len(chunks),
            "data": chunk,
        })


def _build_girdi_ozet(girdi: dict) -> dict:
    personeller = girdi.get("personeller", [])
    gorevler = girdi.get("gorevler", [])
    ara_gun = girdi.get("araGun", girdi.get("aragun", None))
    slot_sayisi = girdi.get("slotSayisi", None)
    return {
        "personel_sayisi": len(personeller),
        "gorev_sayisi": len(gorevler),
        "ara_gun": ara_gun,
        "slot_sayisi": slot_sayisi,
    }


def _build_cikti_ozet(cikti: dict | None, hata) -> dict:
    if hata or not cikti:
        return {
            "basarili": False,
            "atama_sayisi": 0,
            "kalite_skoru": None,
            "teshis_bilgisi": str(hata)[:300] if hata else None,
        }
    atamalar = cikti.get("atamalar", [])
    istatistikler = cikti.get("istatistikler", {})
    kalite = istatistikler.get("kalite_skoru") if istatistikler else None
    teshis = cikti.get("teshis", None)
    teshis_str = None
    if teshis:
        try:
            teshis_str = json.dumps(teshis, ensure_ascii=False)[:500]
        except Exception:
            teshis_str = str(teshis)[:500]
    return {
        "basarili": bool(cikti.get("basari", False)),
        "atama_sayisi": len(atamalar) if isinstance(atamalar, list) else 0,
        "kalite_skoru": kalite,
        "teshis_bilgisi": teshis_str,
    }


def log_session(
    endpoint: str,
    girdi: dict,
    cikti: dict | None,
    sure_ms: int,
    hata: Exception | None = None,
    frontend_loglar: list[str] | None = None,
):
    """
    Bir backend oturumunu Firestore debug_sessions koleksiyonuna kaydeder.
    Hata olursa sadece logger.warning yazar — orijinal isteği engellemez.
    """
    try:
        from firebase_admin import firestore as fs
        db = fs.client()

        ts = datetime.now(timezone.utc)
        durum = "hata" if hata else ("basarili" if (cikti or {}).get("basari") else "bitti")

        # Frontend logları kırp
        logs = (frontend_loglar or [])[:_MAX_FRONTEND_LOGS]

        girdi_ozet = _build_girdi_ozet(girdi)
        cikti_ozet = _build_cikti_ozet(cikti, hata)

        personel_sayisi = girdi_ozet.get("personel_sayisi", 0)
        atama_sayisi = cikti_ozet.get("atama_sayisi", 0)

        doc_data = {
            "endpoint": endpoint,
            "timestamp": ts,
            "durum": durum,
            "sure_ms": sure_ms,
            "ozet": {
                "yil": girdi.get("yil"),
                "ay": girdi.get("ay"),
                "personel_sayisi": personel_sayisi,
                "atama_sayisi": atama_sayisi,
                "hata_mesaji": str(hata)[:300] if hata else None,
            },
            "girdi_ozet": girdi_ozet,
            "cikti_ozet": cikti_ozet,
            "frontend_loglar": logs,
            "hata_detay": "".join(
                __import__("traceback").format_exception(type(hata), hata, hata.__traceback__)
            )[:3000] if hata else None,
        }

        session_ref = db.collection("debug_sessions").document()

        # Büyük payload'ları inline veya sub-collection'a yaz
        girdi_boyut = _json_size(girdi)
        if girdi_boyut < _MAX_INLINE_BYTES:
            doc_data["girdi_tam"] = girdi
        else:
            doc_data["girdi_tam"] = None
            doc_data["girdi_buyuk"] = True
            _write_subcollection(session_ref, "girdi", girdi)

        cikti_boyut = _json_size(cikti) if cikti else 0
        if cikti_boyut < _MAX_INLINE_BYTES:
            doc_data["cikti_tam"] = cikti
        else:
            doc_data["cikti_tam"] = None
            doc_data["cikti_buyuk"] = True
            _write_subcollection(session_ref, "cikti", cikti)

        session_ref.set(doc_data)
        logger.info("Debug session kaydedildi: %s (%s, %dms)", session_ref.id, endpoint, sure_ms)

    except Exception as log_err:
        logger.warning("Debug session kaydedilemedi: %s", log_err)
