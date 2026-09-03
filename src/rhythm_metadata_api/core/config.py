from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RHYTHM_", env_file=".env", extra="ignore")

    environment: str = "development"
    bootstrap_token: str = "change-me-in-development"
    database_path: str = ":memory:"
    v2_database_path: str = "./var/rhythm-v2.sqlite3"
    local_object_root: str = "./var/objects"
    upload_session_ttl_seconds: int = 24 * 60 * 60
    idempotency_ttl_days: int = 30
    max_lyrics_bytes: int = 2 * 1024 * 1024
    max_artwork_bytes: int = 10 * 1024 * 1024
    max_audio_bytes: int = 1024 * 1024 * 1024
    max_musicxml_bytes: int = 50 * 1024 * 1024
    max_midi_bytes: int = 100 * 1024 * 1024

    @model_validator(mode="after")
    def reject_default_production_token(self) -> "Settings":
        if self.environment == "production" and self.bootstrap_token == "change-me-in-development":
            raise ValueError("RHYTHM_BOOTSTRAP_TOKEN must be set in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
