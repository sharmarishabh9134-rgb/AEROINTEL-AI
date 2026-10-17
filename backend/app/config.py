from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore")
    app_env: str = "development"
    demo_mode: bool = True
    auto_seed_demo_data: bool = False
    database_url: str = "sqlite:///./aerointel_demo.db"
    supabase_url: str | None = None
    supabase_publishable_key: str | None = None
    supabase_secret_key: str | None = None
    jwt_secret_key: str = ""
    jwt_expire_minutes: int = Field(default=30, ge=5, le=1440)
    frontend_url: str = "http://127.0.0.1:8000"
    public_api_base_url: str = ""
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"
    open_meteo_latitude: float = Field(default=28.6139, ge=-90, le=90)
    open_meteo_longitude: float = Field(default=77.2090, ge=-180, le=180)
    open_meteo_location: str = "Demo region"
    weather_cache_seconds: int = Field(default=600, ge=30, le=86400)

    @field_validator("frontend_url")
    @classmethod
    def trim_url(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("public_api_base_url")
    @classmethod
    def validate_public_api_url(cls, value: str) -> str:
        value = value.rstrip("/")
        if value:
            parts = urlsplit(value)
            if parts.scheme not in {"http", "https"} or not parts.netloc or parts.username or parts.password:
                raise ValueError("PUBLIC_API_BASE_URL must be an http(s) origin/base URL without credentials")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
