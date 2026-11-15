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
    llm_timeout_seconds: float = Field(default=35, gt=0, le=90)
    llm_max_retries: int = Field(default=2, ge=0, le=3)
    embedding_provider: str = "disabled"
    openai_embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = Field(default=1536, ge=1536, le=1536)
    embedding_chunk_words: int = Field(default=900, ge=100, le=2000)
    embedding_chunk_overlap_words: int = Field(default=120, ge=0, le=400)
    knowledge_max_file_bytes: int = Field(default=2_000_000, ge=1024, le=10_000_000)
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
