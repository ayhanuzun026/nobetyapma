"""
Debug oturum logger — her backend çağrısını Firestore'a kaydeder.
Hata olursa sadece uyarı yazar, orijinal isteği asla engellemez.
"""

import json
import logging
import time
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

_MAX_INLINE_BYTES = 350_000   # Girdi + çıktı aynı parent belgede 1MB altında kalsın
_CHUNK_BYTES = 700_000        # sub-collection parça boyutu
_MAX_FRONTEND_LOGS = 100      # maksimum frontend log satırı
_MAX_FRONTEND_LOG_CHARS = 500
_RETENTION_DAYS = 30          # debug kayıtları TTL ile bu süre sonunda silinir (expireAt)

# Doğrudan kişisel tanımlayıcı (isim) taşıyan alan anahtarları
_PII_NAME_KEYS = frozenset({
    "ad", "personelAd", "personel_ad", "isim", "adSoyad", "ad_soyad",
})


def _build_pseudonym_map(girdi: dict) -> dict:
    """Personel adlarını tutarlı sözde-adlara eşle (aynı ad -> aynı 'P00x')."""
    mapping: dict = {}
    if not isinstance(girdi, dict):
        return mapping
    for p in girdi.get("personeller") or []:
        if isinstance(p, dict):
            ad = p.get("ad")
            if isinstance(ad, str) and ad.strip() and ad not in mapping:
                mapping[ad] = f"P{len(mapping) + 1:03d}"
    return mapping


def _redact_text(value: str, name_map: dict) -> str:
    redacted = value
    for name in sorted(name_map, key=len, reverse=True):
        if name:
            redacted = redacted.replace(name, name_map[name])
    return redacted


def _redact(obj, name_map: dict):
    """Yapıyı KOPYALAYARAK kişi adlarını sözde-adla değiştirir (KVKK/GDPR: doğrudan
    tanımlayıcı kaldırma). id/gün/hedef gibi yapısal veri korunur — solver hata
    ayıklaması için gerekli, isim olmadan sözde-anonim. Orijinal girdi mutasyona uğramaz.
    """
    if isinstance(obj, dict):
        return {
            k: (name_map.get(v, "***")
                if (k in _PII_NAME_KEYS and isinstance(v, str))
                else _redact(v, name_map))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact(v, name_map) for v in obj]
    if isinstance(obj, str):
        # Değer olarak geçen isimler (ör. çizelgedeki personel adları) da maskelensin
        return _redact_text(obj, name_map)
    return obj


def _json_size(obj) -> int:
    try:
        return len(json.dumps(obj, ensure_ascii=False).encode("utf-8"))
    except Exception:
        return 0


def _chunk_json(obj, chunk_size: int = _CHUNK_BYTES) -> list[str]:
    """JSON verisini UTF-8 karakterlerini bozmadan byte sınırında parçalara böl."""
    full = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    chunks = []
    start = 0
    while start < len(full):
        end = min(start + chunk_size, len(full))
        while end < len(full) and end > start and (full[end] & 0xC0) == 0x80:
            end -= 1
        if end == start:
            end = min(start + chunk_size, len(full))
            while end < len(full) and (full[end] & 0xC0) == 0x80:
                end += 1
        chunks.append(full[start:end].decode("utf-8"))
        start = end
    return chunks


def _write_subcollection(session_ref, part_type: str, obj, expire_at):
    """700KB üzeri payload'ı sub-collection'a parçalı yaz."""
    chunks = _chunk_json(obj)
    for idx, chunk in enumerate(chunks):
        session_ref.collection("payload").document(f"{part_type}_{idx}").set({
            "type": part_type,
            "part_index": idx,
            "total_parts": len(chunks),
            "data": chunk,
            "expireAt": expire_at,
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

        # Frontend loglarını hem satır hem toplam belge boyutu açısından sınırla.
        logs = [str(line)[:_MAX_FRONTEND_LOG_CHARS]
                for line in (frontend_loglar or [])[:_MAX_FRONTEND_LOGS]]

        girdi_ozet = _build_girdi_ozet(girdi)
        cikti_ozet = _build_cikti_ozet(cikti, hata)

        # PII maskeleme: isimler sözde-adlarla değiştirilir (id/yapı korunur).
        _name_map = _build_pseudonym_map(girdi)
        girdi_red = _redact(girdi, _name_map)
        cikti_red = _redact(cikti, _name_map) if cikti else cikti
        logs_red = [_redact_text(line, _name_map) for line in logs]
        hata_mesaji = _redact_text(str(hata), _name_map)[:300] if hata else None
        hata_detay = None
        if hata:
            hata_detay = _redact_text(
                "".join(__import__("traceback").format_exception(type(hata), hata, hata.__traceback__)),
                _name_map,
            )[:3000]

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
                "hata_mesaji": hata_mesaji,
            },
            "girdi_ozet": girdi_ozet,
            "cikti_ozet": cikti_ozet,
            "frontend_loglar": logs_red,
            "hata_detay": hata_detay,
        }

        # TTL: expireAt alanı. Firestore TTL politikası bu alana bağlanmalı
        # (Console/gcloud: debug_sessions için TTL policy 'expireAt' alanında açılmalı).
        doc_data["expireAt"] = ts + timedelta(days=_RETENTION_DAYS)

        session_ref = db.collection("debug_sessions").document()

        # Büyük payload'ları inline veya sub-collection'a yaz (maskelenmiş halleriyle)
        girdi_boyut = _json_size(girdi_red)
        if girdi_boyut < _MAX_INLINE_BYTES:
            doc_data["girdi_tam"] = girdi_red
        else:
            doc_data["girdi_tam"] = None
            doc_data["girdi_buyuk"] = True
            _write_subcollection(session_ref, "girdi", girdi_red, doc_data["expireAt"])

        cikti_boyut = _json_size(cikti_red) if cikti_red else 0
        if cikti_boyut < _MAX_INLINE_BYTES:
            doc_data["cikti_tam"] = cikti_red
        else:
            doc_data["cikti_tam"] = None
            doc_data["cikti_buyuk"] = True
            _write_subcollection(session_ref, "cikti", cikti_red, doc_data["expireAt"])

        session_ref.set(doc_data)
        logger.info("Debug session kaydedildi: %s (%s, %dms)", session_ref.id, endpoint, sure_ms)

    except Exception as log_err:
        logger.warning("Debug session kaydedilemedi: %s", log_err)
