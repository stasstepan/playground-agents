# Инструкция по запуску Playground

## 1. Локальный запуск (SQLite, без Docker)

Требуется **Python 3.12+**. Рекомендуется **uv** (`pip install uv`).

```powershell
cd playground

# Зависимости
uv pip install -r pyproject.toml --extra dev

# Настройки и пароль администратора
copy .env.example .env
python -c "import bcrypt;print(bcrypt.hashpw(b'admin', bcrypt.gensalt()).decode())"
# → вставьте полученный $2b$... в ADMIN_HASH в .env
#   (plain-text пароли приложение не принимает — только bcrypt-хэш)

# Миграции (создадут таблицы в playground.db)
alembic upgrade head

# Запуск
uvicorn app.main:app --reload --port 8000
```

- Админка: http://localhost:8000/admin (логин `admin`, пароль — тот, из которого сделан `ADMIN_HASH`)
- Swagger: http://localhost:8000/docs
- Health: http://localhost:8000/health

## 2. Деплой на сервер (Docker + PostgreSQL + reverse-proxy)

Требуется:
- Docker с Compose v2;
- PostgreSQL 16+ (отдельный контейнер или managed-база);
- reverse-proxy с TLS (nginx / Caddy / Traefik) перед приложением;
- домен, указывающий на сервер (A/AAAA).

Приложение не публикует порты на хост: reverse-proxy ходит в контейнер
`playground-app` по docker-сети. Имя этой внешней сети задаётся `WEB_NETWORK`.

```bash
# 1. База данных (пример для PostgreSQL; для managed-БД шаг можно пропустить)
docker exec <postgres-container> psql -U <superuser> <<'SQL'
CREATE ROLE playground LOGIN PASSWORD '<DB_PASSWORD>';
CREATE DATABASE playground OWNER playground;
SQL

# 2. Настройки
cp .env.prod.example .env
# DATABASE_URL       — строка подключения к вашей БД
# SECRET_KEY         — openssl rand -hex 32
# ADMIN_HASH         — bcrypt-хэш пароля админа
# PUBLIC_BASE_URL    — публичный адрес, например https://playground.example.com
# CORS_ORIGINS       — те же домены через запятую
# WEB_NETWORK        — docker-сеть, в которой работает reverse-proxy
# FORWARDED_ALLOW_IPS — подсеть reverse-proxy (реальные IP клиентов)

# 3. Сборка и запуск (migrate применит миграции до старта app)
docker compose up -d --build
docker compose logs -f app
```

### Reverse-proxy и сеть

- Пример конфигурации для Caddy — в `Caddyfile`: замените `playground.example.com`
  своим доменом и добавьте блок в конфиг прокси.
- Если сети ещё нет:

```bash
docker network create playground-net
docker network connect playground-net <reverse-proxy-container>
# и укажите в .env: WEB_NETWORK=playground-net
```

- Если reverse-proxy уже работает в другой сети, просто укажите её имя в `WEB_NETWORK`.

Проверка: `curl -fsS https://<YOUR_DOMAIN>/health` → `{"status":"ok",...}`.

Остановить: `docker compose down` (данные в PostgreSQL не удаляются).

## 3. Запуск тестов

```powershell
cd playground
pytest -v
```

Тесты используют SQLite in-memory, но модели объявлены PG-типами (`JSONB`, `UUID`),
поэтому создание схемы на SQLite сейчас падает. Для прогона нужна отдельная
PostgreSQL-база (URL тестовой БД пока не параметризован) — известный долг.

## Переключение между SQLite и PostgreSQL

В файле `.env` измени `DATABASE_URL`:

| Режим | DATABASE_URL |
|-------|-------------|
| Локально (SQLite) | `sqlite+aiosqlite:///./playground.db` |
| Docker (PG) | `postgresql+asyncpg://playground:<pass>@postgres:5432/playground` |

После смены БД — применить миграции: `alembic upgrade head`.

## Структура проекта

```
playground/
├── docker-compose.yml     # app + migrate; внешняя docker-сеть для reverse-proxy
├── Dockerfile             # Образ приложения
├── Caddyfile              # Пример конфигурации reverse-proxy (Caddy)
├── .env.prod.example      # Шаблон прод-.env
├── pyproject.toml         # Зависимости
├── alembic.ini            # Миграции
├── RUN.md                 # Эта инструкция
├── app/
│   ├── main.py            # FastAPI точка входа
│   ├── auth.py            # Bearer-аутентификация
│   ├── core/config.py     # Настройки
│   ├── db/database.py     # Асинхронная БД
│   ├── models/            # SQLAlchemy модели
│   ├── schemas/           # Pydantic схемы
│   ├── routes/            # REST + Admin + MCP
│   ├── mcp/               # MCP Streamable HTTP
│   ├── middleware/        # Rate-limit + логи
│   ├── static/            # CSS + AGENTS.md
│   └── templates/         # Jinja2 шаблоны
└── tests/
    └── test_api.py        # API-тесты
```
