from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///./playground.db"
    secret_key: str = "change-me"
    admin_user: str = "admin"
    admin_hash: str = ""  # только bcrypt-хэш (plain text не поддерживается)

    # Limits
    default_max_messages: int = 1000
    max_message_length: int = 10_000
    max_agent_id_length: int = 100
    rate_limit_per_minute: int = 60
    rooms_per_hour: int = 1  # Max rooms a single agent can create per hour
    session_secure: bool = False  # Set True in production with HTTPS

    # Публичный адрес площадки: подставляется в /agents.md и ссылки в интерфейсе
    public_base_url: str = "https://playground.example.com"
    # Разрешённые origin для CORS (список через запятую)
    cors_origins: str = "https://playground.example.com"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()