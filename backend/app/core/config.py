from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
import secrets


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "postgresql+psycopg://incident_app:change-me@localhost:5432/incident_platform"
    redis_url: str = "redis://localhost:6379/0"
    jwt_secret: str = Field(default_factory=lambda: secrets.token_urlsafe(48))
    integration_encryption_key: str = ""
    access_token_minutes: int = 60
    llm_provider: str = "mock"
    openai_api_key: str = ""
    openai_model: str = "gpt-4.1-mini"
    artifact_dir: str = "../var/artifacts"
    seed_demo: bool = True
    frontend_origin: str = "http://localhost:5173"
    task_mode: str = "inline"
    otlp_endpoint: str = ""
    integration_allowed_private_hosts: str = "prometheus,alertmanager,loki,tempo,grafana,otel-collector,payment-api"
    integration_allowed_external_hosts: str = ""
    investigation_lease_seconds: int = Field(default=300, gt=0)
    investigation_max_retries: int = Field(default=4, ge=0)


settings = Settings()
