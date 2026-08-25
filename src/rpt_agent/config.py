from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"
    log_dir: Path = Path("logs")
    supabase_db_url: str = ""
    provider_mode: str = "mock"
    mock_base_url: str = "http://localhost:9000"
    api_base_url: str = "http://localhost:8000"
    vapi_base_url: str = "https://api.vapi.ai"
    vapi_api_key: str = ""
    vapi_webhook_secret: str = "local-vapi-secret"
    vapi_hmac_secret: str = ""
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = "+15550000001"
    twilio_base_url: str = "https://api.twilio.com"
    stride_base_url: str = "https://demo.stridethera.com"
    stride_api_token: str = ""
    slot_token_secret: str = Field(default="local-slot-secret", min_length=8)
    keap_handoff_url: str = "http://localhost:9000/mock/keap/events"
    keap_handoff_secret: str = "local-keap-secret"
    worker_poll_seconds: int = 30
    mock_scenario: str = "success"
    request_timeout_seconds: float = 10.0
    db_pool_timeout_seconds: float = 5.0

    def provider_url(self, provider: str) -> str:
        if self.provider_mode == "mock":
            return f"{self.mock_base_url.rstrip('/')}/mock/{provider}"
        return {
            "vapi": self.vapi_base_url,
            "twilio": self.twilio_base_url,
            "stride": self.stride_base_url,
        }[provider].rstrip("/")

    def runtime_errors(self, service: str) -> list[str]:
        errors: list[str] = []
        if service in {"api", "worker", "cli"} and not self.supabase_db_url:
            errors.append("SUPABASE_DB_URL is required")
        elif service in {"api", "worker", "cli"} and "db.example.supabase.co" in self.supabase_db_url:
            errors.append("SUPABASE_DB_URL still contains the example hostname")
        if self.provider_mode not in {"mock", "real"}:
            errors.append("PROVIDER_MODE must be mock or real")
        if self.provider_mode == "real" and service in {"api", "worker"}:
            required = {
                "VAPI_API_KEY": self.vapi_api_key,
                "TWILIO_ACCOUNT_SID": self.twilio_account_sid,
                "TWILIO_AUTH_TOKEN": self.twilio_auth_token,
                "STRIDE_API_TOKEN": self.stride_api_token,
            }
            errors.extend(f"{name} is required in real provider mode" for name, value in required.items() if not value)
        return errors


@lru_cache
def get_settings() -> Settings:
    return Settings()
