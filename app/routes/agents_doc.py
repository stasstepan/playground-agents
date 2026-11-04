from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from app.core.config import settings

router = APIRouter()


@router.get("/agents.md", response_class=PlainTextResponse)
async def agents_doc():
    with open("app/static/agents.md", "r", encoding="utf-8") as f:
        text = f.read()
    # Подставляем реальный публичный адрес площадки (задаётся в PUBLIC_BASE_URL)
    return text.replace("{{BASE_URL}}", settings.public_base_url)