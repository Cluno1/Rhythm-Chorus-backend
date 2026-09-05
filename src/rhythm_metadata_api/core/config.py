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

    # COS 直连（issue 11）：为 provider='cos' 的 asset 签发 presigned GET URL，
    # 让客户端绕开后端代理直接从对象存储下载音频。凭据从 env 注入（0600，不入库、不打印）。
    cos_secret_id: str = ""
    cos_secret_key: str = ""
    cos_region: str = "ap-guangzhou"
    cos_presign_expires_seconds: int = 900

    # Public device gateway (issue 14). These remain optional for the private app;
    # create_public_app validates them before exposing a public listener.
    public_token_secret: str = ""
    public_admin_username: str = "admin"
    public_admin_password_hash: str = ""
    public_access_token_ttl_seconds: int = 10 * 60
    public_admin_token_ttl_seconds: int = 5 * 60
    public_device_session_ttl_days: int = 90
    public_invite_ttl_seconds: int = 10 * 60
    public_nonce_ttl_seconds: int = 60

    @model_validator(mode="after")
    def reject_default_production_token(self) -> "Settings":
        if self.environment == "production" and self.bootstrap_token == "change-me-in-development":
            raise ValueError("RHYTHM_BOOTSTRAP_TOKEN must be set in production")
        if bool(self.cos_secret_id) != bool(self.cos_secret_key):
            raise ValueError("RHYTHM_COS_SECRET_ID and RHYTHM_COS_SECRET_KEY must be set together")
        if not 60 <= self.cos_presign_expires_seconds <= 3600:
            raise ValueError("RHYTHM_COS_PRESIGN_EXPIRES_SECONDS must be between 60 and 3600")
        if not 60 <= self.public_access_token_ttl_seconds <= 3600:
            raise ValueError("public access token TTL must be between 60 and 3600 seconds")
        if not 60 <= self.public_admin_token_ttl_seconds <= 900:
            raise ValueError("public admin token TTL must be between 60 and 900 seconds")
        if not 1 <= self.public_device_session_ttl_days <= 365:
            raise ValueError("public device session TTL must be between 1 and 365 days")
        if not 60 <= self.public_invite_ttl_seconds <= 86400:
            raise ValueError("public invite TTL must be between 60 seconds and one day")
        if not 15 <= self.public_nonce_ttl_seconds <= 300:
            raise ValueError("public nonce TTL must be between 15 and 300 seconds")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
