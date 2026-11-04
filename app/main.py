from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.core.config import settings
from app.mcp.server import router as mcp_router
from app.middleware import AdminActionLogMiddleware, RateLimitMiddleware
from app.routes import (
    admin_router,
    agents_doc_router,
    home_router,
    keys_router,
    rooms_router,
)

app = FastAPI(
    title="Playground — комнаты для торга AI-агентов",
    version="0.1.0",
    docs_url="/docs",
)

# CORS: домены площадки задаются в CORS_ORIGINS (через запятую)
_cors_origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Custom middlewares
app.add_middleware(
    RateLimitMiddleware,
    max_requests=settings.rate_limit_per_minute,
    window_seconds=60,
)
app.add_middleware(AdminActionLogMiddleware)

# API routers
app.include_router(mcp_router)
app.include_router(admin_router)
app.include_router(keys_router)
app.include_router(rooms_router)
app.include_router(agents_doc_router)
app.include_router(home_router)  # GET / (room list), GET /r/{uuid}

# Static files
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/health")
async def health():
    return {"status": "ok", "version": "0.1.0"}