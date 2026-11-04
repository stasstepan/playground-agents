import asyncio
import hashlib
import time
from collections import defaultdict

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware


class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, max_requests: int = 60, window_seconds: int = 60):
        super().__init__(app)
        self.max_requests = max_requests
        self.window = window_seconds
        self._lock = asyncio.Lock()
        self.requests: dict[str, list[float]] = defaultdict(list)

    def _resolve_client_id(self, request: Request) -> str:
        """Return a stable client identifier: SHA-256 of Bearer token, or IP."""
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            raw = auth.removeprefix("Bearer ").strip()
            if raw:
                return f"key:{hashlib.sha256(raw.encode()).hexdigest()}"
        host = request.client.host if request.client else "unknown"
        return f"ip:{host}"

    async def dispatch(self, request: Request, call_next):
        # Skip rate limiting for certain paths
        if request.url.path in ("/health", "/admin/login", "/static"):
            return await call_next(request)

        client_id = self._resolve_client_id(request)
        now = time.time()
        window_start = now - self.window

        async with self._lock:
            self.requests[client_id] = [
                t for t in self.requests[client_id] if t > window_start
            ]
            if len(self.requests[client_id]) >= self.max_requests:
                # Важно: из BaseHTTPMiddleware нельзя «кидать» HTTPException —
                # FastAPI не перехватывает его здесь и отдаёт 500. Возвращаем
                # обычный JSON-ответ 429.
                return JSONResponse(
                    status_code=429,
                    content={"detail": "Too many requests", "code": "rate_limited"},
                    headers={"Retry-After": str(self.window)},
                )
            self.requests[client_id].append(now)

        return await call_next(request)