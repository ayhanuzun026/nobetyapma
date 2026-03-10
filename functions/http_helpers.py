"""
HTTP yardımcıları — CORS, JSON response, error response.
"""

import json
import logging

from firebase_functions import https_fn

logger = logging.getLogger(__name__)

CORS_HEADERS = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type'
}


def _cors_preflight():
    return https_fn.Response("", status=204, headers=CORS_HEADERS)


def _json_response(payload: dict, status: int = 200):
    return https_fn.Response(json.dumps(payload), status=status, headers=CORS_HEADERS)


def _error_response(e: Exception, context: str = ""):
    logger.exception("Sunucu hatası [%s]", context)
    error_type = type(e).__name__
    if isinstance(e, (ValueError, TypeError)):
        detail = f"Geçersiz veri formatı: {str(e)[:200]}"
        status = 400
    elif isinstance(e, KeyError):
        detail = f"Eksik alan: {str(e)[:200]}"
        status = 400
    else:
        detail = "Beklenmeyen bir hata oluştu. Lütfen tekrar deneyin."
        status = 500
    return _json_response({
        "error": detail,
        "error_type": error_type,
        "context": context,
    }, status=status)
