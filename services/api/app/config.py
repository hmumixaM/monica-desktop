from datetime import date
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Monica Next CRM"
    app_env: str = "development"
    desktop_origin: str = "http://127.0.0.1:3000"
    migrations_root: Path | None = None
    database_url: str = "postgresql+psycopg://crm:crm@localhost:5432/crm"
    wechat_database_url: str = ""
    instagram_database_url: str = ""
    linkedin_database_url: str = ""
    linkedin_collection_token: str = ""
    cdc_enabled: bool = False
    async_projection_enabled: bool = False
    raw_root: Path = Path("./runtime/raw")
    media_root: Path = Path("./runtime/media")
    session_secret: str = "development-only-change-me"
    import_hmac_secret: str = "development-only-change-me"
    import_hmac_secrets: str = ""
    session_ttl_seconds: int = 60 * 60 * 24 * 14
    import_signature_ttl_seconds: int = 300
    cors_origins: str = "http://localhost:3000"
    api_statement_timeout_ms: int = 15000
    crm_timezone: str = "America/Chicago"
    # Live WeChat history stays in Chatlog Alpha. This proxy is optional and
    # never writes those messages into Monica.
    chatlog_alpha_url: str = "http://host.docker.internal:5030"
    chatlog_alpha_timeout_seconds: int = 120
    # Model features go through the NAS LiteLLM proxy. Off unless AI_ENABLED is set;
    # when off, no model is called and their blocks are not shown.
    ai_enabled: bool = False
    litellm_base_url: str = "http://host.docker.internal:4000/v1"
    litellm_api_key: str = ""
    ai_batch_model: str = "gemini-3.8-flash"
    # Claude through LiteLLM emulates the response schema with a tool call and returns nested arrays as
    # (sometimes malformed) JSON text; Gemini enforces the schema natively.
    ai_writer_model: str = "gemini-3.8-flash"
    ai_person_model: str = "gemini-3.8-flash"
    # Reads text and content off post images; registered in the NAS LiteLLM config.
    ai_vision_model: str = "gemini-3.5-flash-lite"
    # Posts from this day on are read automatically, including ones imported later.
    ai_post_media_since: date = date(2026, 6, 1)

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @model_validator(mode="after")
    def validate_desktop(self):
        if self.app_env == "desktop":
            origin = urlsplit(self.desktop_origin)
            if (origin.scheme != "http" or origin.hostname != "127.0.0.1"
                    or origin.username or origin.password or origin.path or origin.query or origin.fragment):
                raise ValueError("DESKTOP_ORIGIN must be an HTTP 127.0.0.1 origin without a path")
            _ = origin.port  # Reject malformed ports before starting any service.
            for name in ("session_secret", "import_hmac_secret"):
                value = getattr(self, name)
                if len(value) < 32 or "change-me" in value.lower() or "development-only" in value.lower():
                    raise ValueError(f"Desktop {name} must be a generated secret of at least 32 characters")
        return self

    @property
    def secure_cookies(self) -> bool:
        return self.app_env not in ("development", "desktop")

    @property
    def cors_origin_list(self) -> list[str]:
        if self.app_env == "desktop":
            return [self.desktop_origin]
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.raw_root.mkdir(parents=True, exist_ok=True)
    settings.media_root.mkdir(parents=True, exist_ok=True)
    return settings
