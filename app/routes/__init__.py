from app.routes.admin import router as admin_router
from app.routes.agents_doc import router as agents_doc_router
from app.routes.home import router as home_router
from app.routes.keys import router as keys_router
from app.routes.rooms import router as rooms_router

__all__ = [
    "admin_router",
    "agents_doc_router",
    "home_router",
    "keys_router",
    "rooms_router",
]