"""Hardening de la API: rate limiting y headers de seguridad.

El rate limiting es en memoria del proceso: suficiente para una sola instancia
(como el plan free de Render en render.yaml). Si el servicio se escala a varias
instancias, esto debe moverse a un store compartido (ej. Redis), porque cada
proceso llevaría su propio conteo y el límite real sería N veces el configurado.
"""

import os
import time
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

RATE_LIMIT_WINDOW_SECONDS = 60
RATE_LIMIT_PER_MINUTE = int(os.environ.get("RATE_LIMIT_PER_MINUTE", "60"))
# Los endpoints que llaman a Claude (análisis de góndola, OCR) son los costosos:
# tienen un límite propio, más bajo, independiente del límite general por IP/key.
RATE_LIMIT_ANALYZE_PER_MINUTE = int(os.environ.get("RATE_LIMIT_ANALYZE_PER_MINUTE", "20"))
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(15 * 1024 * 1024)))  # 15 MB por imagen

_VISION_SUFFIXES = ("/analyze", "/voucher", "/client-form")


def _client_key(request: Request) -> str:
    api_key = request.headers.get("x-api-key")
    if api_key:
        return f"key:{api_key[-12:]}"
    forwarded = request.headers.get("x-forwarded-for")
    ip = forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "unknown")
    return f"ip:{ip}"


def _is_vision_call(path: str) -> bool:
    return path.startswith("/api/") and path.endswith(_VISION_SUFFIXES)


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)
        self._hits = defaultdict(deque)

    async def dispatch(self, request: Request, call_next):
        is_vision = _is_vision_call(request.url.path)
        limit = RATE_LIMIT_ANALYZE_PER_MINUTE if is_vision else RATE_LIMIT_PER_MINUTE
        key = (_client_key(request), is_vision)
        now = time.monotonic()
        bucket = self._hits[key]
        while bucket and now - bucket[0] > RATE_LIMIT_WINDOW_SECONDS:
            bucket.popleft()
        if len(bucket) >= limit:
            return JSONResponse(
                status_code=429,
                content={"detail": "Demasiadas solicitudes. Intenta nuevamente en un momento."},
                headers={"Retry-After": str(RATE_LIMIT_WINDOW_SECONDS)},
            )
        bucket.append(now)
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        return response
