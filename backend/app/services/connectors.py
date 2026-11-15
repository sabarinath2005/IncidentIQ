"""Provider-neutral connector lifecycle and explicit adapter availability registry."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlparse

from app.core.config import settings
from app.services.observability_http import (
    BoundedHTTPClient, LokiClient, PrometheusClient, ProviderError, TempoClient,
    validate_provider_url,
)


class ConnectionResult(StrEnum):
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
    UNAVAILABLE = "UNAVAILABLE"
    CONNECTED = "CONNECTED"
    INVALID_CONFIGURATION = "INVALID_CONFIGURATION"


@dataclass(frozen=True)
class ConnectorTest:
    result: ConnectionResult
    code: str
    message: str


class Connector(ABC):
    provider: str
    implemented = False

    def validate_config(self, config: dict) -> list[str]:
        """Return field-level errors without including secret values."""
        return []

    @abstractmethod
    def test_connection(self, config: dict, credentials: dict | None = None) -> ConnectorTest:
        raise NotImplementedError


class PlannedConnector(Connector):
    def __init__(self, provider: str):
        self.provider = provider

    def test_connection(self, config: dict, credentials: dict | None = None) -> ConnectorTest:
        issues = self.validate_config(config)
        if issues:
            return ConnectorTest(ConnectionResult.INVALID_CONFIGURATION, "invalid_configuration", "; ".join(issues))
        return ConnectorTest(ConnectionResult.NOT_IMPLEMENTED, "adapter_not_implemented",
                             f"{self.provider} connectivity is planned; this adapter is not implemented.")

    def validate_config(self, config: dict) -> list[str]:
        endpoint_key = "endpoint" if self.provider == "opentelemetry" else "base_url"
        endpoint = config.get(endpoint_key)
        if endpoint is None:
            return []
        if not isinstance(endpoint, str):
            return [f"{endpoint_key} must be a URL string"]
        try:
            validate_provider_url(endpoint, allowed_private_hosts(), allowed_external_hosts())
        except ValueError as exc:
            return [str(exc)]
        return []


def allowed_private_hosts() -> set[str]:
    return {host.strip().lower() for host in settings.integration_allowed_private_hosts.split(",") if host.strip()}


def allowed_external_hosts() -> set[str]:
    return {host.strip().lower() for host in settings.integration_allowed_external_hosts.split(",") if host.strip()}


class HTTPObservabilityConnector(Connector):
    implemented = True

    def __init__(self, provider: str, probe_path: str):
        self.provider = provider
        self.probe_path = probe_path

    def validate_config(self, config: dict) -> list[str]:
        key = "endpoint" if self.provider == "opentelemetry" else "base_url"
        value = config.get(key)
        if not isinstance(value, str) or not value:
            return [f"{key} is required"]
        try:
            validate_provider_url(value, allowed_private_hosts(), allowed_external_hosts())
        except ValueError as exc:
            return [str(exc)]
        if self.provider in {"prometheus", "loki", "tempo", "alertmanager", "grafana"}:
            if config.get("service_id") is not None:
                try:
                    from uuid import UUID
                    UUID(str(config["service_id"]))
                except (ValueError, TypeError):
                    return ["service_id must be a valid project service UUID"]
        return []

    def test_connection(self, config: dict, credentials: dict | None = None) -> ConnectorTest:
        issues = self.validate_config(config)
        if issues:
            return ConnectorTest(ConnectionResult.INVALID_CONFIGURATION, "invalid_configuration", "; ".join(issues))
        try:
            base_url = config.get("endpoint") if self.provider == "opentelemetry" else config["base_url"]
            client = BoundedHTTPClient(base_url, credentials, allowed_private_hosts=allowed_private_hosts(),
                                      allowed_public_hosts=allowed_external_hosts())
            client.get_bytes(self.probe_path)
            return ConnectorTest(ConnectionResult.CONNECTED, "connected", "Provider health endpoint responded successfully.")
        except ProviderError as exc:
            return ConnectorTest(ConnectionResult.UNAVAILABLE, exc.code, str(exc))
        except ValueError as exc:
            return ConnectorTest(ConnectionResult.INVALID_CONFIGURATION, "invalid_configuration", str(exc))


class PrometheusConnector(HTTPObservabilityConnector):
    def __init__(self):
        super().__init__("prometheus", "/-/ready")

    def test_connection(self, config: dict, credentials: dict | None = None) -> ConnectorTest:
        issues = self.validate_config(config)
        if issues:
            return ConnectorTest(ConnectionResult.INVALID_CONFIGURATION, "invalid_configuration", "; ".join(issues))
        try:
            PrometheusClient(config["base_url"], credentials, allowed_private_hosts=allowed_private_hosts(), allowed_public_hosts=allowed_external_hosts()).ready()
            return ConnectorTest(ConnectionResult.CONNECTED, "connected", "Prometheus readiness endpoint responded successfully.")
        except ProviderError as exc:
            return ConnectorTest(ConnectionResult.UNAVAILABLE, exc.code, str(exc))


class LokiConnector(HTTPObservabilityConnector):
    def __init__(self):
        super().__init__("loki", "/ready")

    def test_connection(self, config: dict, credentials: dict | None = None) -> ConnectorTest:
        issues = self.validate_config(config)
        if issues:
            return ConnectorTest(ConnectionResult.INVALID_CONFIGURATION, "invalid_configuration", "; ".join(issues))
        try:
            LokiClient(config["base_url"], credentials, allowed_private_hosts=allowed_private_hosts(), allowed_public_hosts=allowed_external_hosts()).ready()
            return ConnectorTest(ConnectionResult.CONNECTED, "connected", "Loki readiness endpoint responded successfully.")
        except ProviderError as exc:
            return ConnectorTest(ConnectionResult.UNAVAILABLE, exc.code, str(exc))


class TempoConnector(HTTPObservabilityConnector):
    def __init__(self):
        super().__init__("tempo", "/ready")

    def test_connection(self, config: dict, credentials: dict | None = None) -> ConnectorTest:
        issues = self.validate_config(config)
        if issues:
            return ConnectorTest(ConnectionResult.INVALID_CONFIGURATION, "invalid_configuration", "; ".join(issues))
        try:
            TempoClient(config["base_url"], credentials, allowed_private_hosts=allowed_private_hosts(), allowed_public_hosts=allowed_external_hosts()).ready()
            return ConnectorTest(ConnectionResult.CONNECTED, "connected", "Tempo readiness endpoint responded successfully.")
        except ProviderError as exc:
            return ConnectorTest(ConnectionResult.UNAVAILABLE, exc.code, str(exc))


PROVIDERS = (
    "prometheus", "alertmanager", "loki", "opentelemetry", "tempo", "grafana",
    "github", "jira", "slack", "pagerduty",
)


class ConnectorRegistry:
    def __init__(self):
        self._connectors = {provider: PlannedConnector(provider) for provider in PROVIDERS}
        for connector in (
            PrometheusConnector(),
            HTTPObservabilityConnector("alertmanager", "/-/ready"),
            LokiConnector(),
            HTTPObservabilityConnector("opentelemetry", "/"),
            TempoConnector(),
            HTTPObservabilityConnector("grafana", "/api/health"),
        ):
            self._connectors[connector.provider] = connector

    def register(self, connector: Connector) -> None:
        if connector.provider not in PROVIDERS:
            raise ValueError("Provider is not part of the supported connector catalog")
        self._connectors[connector.provider] = connector

    def get(self, provider: str) -> Connector | None:
        return self._connectors.get(provider.lower())

    def catalog(self) -> list[dict]:
        return [{"provider": provider, "implemented": connector.implemented}
                for provider, connector in sorted(self._connectors.items())]

    def credentials_for(self, integration) -> dict:
        """Backend adapter-only accessor; provider must match the registered adapter."""
        connector = self.get(integration.provider)
        if not connector:
            raise PermissionError("No registered connector for integration")
        if not integration.credentials_ciphertext:
            return {}
        from app.services.credentials import decrypt_credentials
        return decrypt_credentials(integration.credentials_ciphertext, provider=integration.provider,
                                  connector_provider=connector.provider)


registry = ConnectorRegistry()
