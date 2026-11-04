import json
import logging
from datetime import datetime, timezone

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

import os

# Simple file logging for admin actions
log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "var", "log", "playground")
os.makedirs(log_dir, exist_ok=True)
log_path = os.path.join(log_dir, "admin.log")

logging.basicConfig(
    filename=log_path,
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)


class AdminActionLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        # Only log POST/PUT/DELETE to /admin or /api routes
        if request.method in ("POST", "PUT", "DELETE") and (
            request.url.path.startswith("/admin")
            or request.url.path.startswith("/api")
        ):
            admin_agent = "unknown"
            if hasattr(request.state, "admin_user"):
                admin_agent = request.state.admin_user

            logging.info(
                json.dumps(
                    {
                        "action": f"{request.method} {request.url.path}",
                        "admin": admin_agent,
                        "status": response.status_code,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )
            )

        return response