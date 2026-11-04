# FastAPI + Uvicorn
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# uv — быстрый установщик зависимостей.
# psycopg2-binary нужен миграциям: alembic/env.py переводит
# postgresql+asyncpg:// в синхронный postgresql+psycopg2://.
COPY pyproject.toml .
RUN pip install --no-cache-dir uv && uv pip install --system -r pyproject.toml

# Сам пакет app/ в site-packages не ставится (ставим только зависимости),
# поэтому console-script alembic не видит его без явного PYTHONPATH.
ENV PYTHONPATH=/app

COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
