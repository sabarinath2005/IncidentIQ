"""Provider-neutral connector lifecycle and explicit adapter availability registry."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
import re
from urllib.parse import urlparse
import httpx

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


class GitHubConnector(Connector):
    provider = "github"
    implemented = True
    max_repositories_per_test = 20

    _repository_pattern = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")

    def validate_config(self, config: dict) -> list[str]:
        repositories = config.get("repositories") if isinstance(config, dict) else None
        if not isinstance(repositories, list) or not repositories:
            return ["Map at least one GitHub repository to this project before testing the connection."]
        if len(repositories) > self.max_repositories_per_test:
            return [f"A connection test can check at most {self.max_repositories_per_test} mapped repositories at once."]
        for name in repositories:
            if not isinstance(name, str) or not self._repository_pattern.fullmatch(name):
                return ["The mapped GitHub repository configuration is invalid."]
            if any(segment in {".", ".."} for segment in name.split("/")):
                return ["The mapped GitHub repository configuration is invalid."]
        return []

    def test_connection(self, config: dict, credentials: dict | None = None) -> ConnectorTest:
        token = (credentials or {}).get("token")
        if not isinstance(token, str) or not token:
            return ConnectorTest(ConnectionResult.INVALID_CONFIGURATION, "missing_credentials", "A GitHub read token is required.")
        errors = self.validate_config(config)
        if errors:
            return ConnectorTest(ConnectionResult.INVALID_CONFIGURATION, "missing_repository_configuration", "; ".join(errors))

        headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        for full_name in config["repositories"]:
            # The strict owner/name allowlist prevents caller-controlled URL paths.
            owner, repository = full_name.split("/", 1)
            try:
                response = httpx.get(f"https://api.github.com/repos/{owner}/{repository}",
                                     headers=headers, timeout=httpx.Timeout(8.0))
            except httpx.TimeoutException:
                return ConnectorTest(ConnectionResult.UNAVAILABLE, "provider_timeout", "GitHub repository access check timed out.")
            except httpx.HTTPError:
                return ConnectorTest(ConnectionResult.UNAVAILABLE, "provider_unavailable", "GitHub could not be reached.")

            if response.status_code == 200:
                try:
                    returned_name = response.json().get("full_name", "")
                except (ValueError, AttributeError):
                    return ConnectorTest(ConnectionResult.UNAVAILABLE, "invalid_provider_response", "GitHub returned an invalid repository response.")
                if not isinstance(returned_name, str) or returned_name.casefold() != full_name.casefold():
                    return ConnectorTest(ConnectionResult.UNAVAILABLE, "repository_mismatch", "GitHub returned a different repository than the project mapping.")
                continue
            if response.status_code == 401:
                return ConnectorTest(ConnectionResult.INVALID_CONFIGURATION, "credential_rejected", "GitHub rejected the configured token.")
            if response.status_code == 404:
                return ConnectorTest(ConnectionResult.INVALID_CONFIGURATION, "repository_inaccessible", "The mapped repository was not found or is not accessible to this token.")
            if response.status_code == 429 or (response.status_code == 403 and
                    (response.headers.get("retry-after") or response.headers.get("x-ratelimit-remaining") == "0")):
                return ConnectorTest(ConnectionResult.UNAVAILABLE, "rate_limited", "GitHub rate-limited the repository access check. Retry later.")
            if response.status_code == 403:
                return ConnectorTest(ConnectionResult.INVALID_CONFIGURATION, "insufficient_permissions", "The token lacks read access to the mapped repository; grant repository metadata read access.")
            if response.status_code >= 500:
                return ConnectorTest(ConnectionResult.UNAVAILABLE, "provider_unavailable", "GitHub is temporarily unavailable.")
            if response.is_error:
                return ConnectorTest(ConnectionResult.UNAVAILABLE, "provider_error", f"GitHub repository access check failed (HTTP {response.status_code}).")

        return ConnectorTest(ConnectionResult.CONNECTED, "connected",
            "The token can read repository metadata for every repository mapped to this project.")


def validate_jira_base_url(value: object) -> str:
    if not isinstance(value, str) or len(value) > 300:
        raise ValueError("Jira base_url must be a URL")
    parsed = urlparse(value.strip())
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host.endswith(".atlassian.net") or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Jira Cloud base_url must be HTTPS on an *.atlassian.net host")
    from app.services.observability_http import validate_provider_url
    validate_provider_url(value.rstrip("/"), allowed_public_hosts={host})
    return value.rstrip("/")


class JiraConnector(Connector):
    provider = "jira"
    implemented = True

    def validate_config(self, config: dict) -> list[str]:
        try:
            validate_jira_base_url(config.get("base_url"))
        except ValueError as exc:
            return [str(exc)]
        return []

    def test_connection(self, config: dict, credentials: dict | None = None) -> ConnectorTest:
        errors = self.validate_config(config)
        if errors:
            return ConnectorTest(ConnectionResult.INVALID_CONFIGURATION, "invalid_configuration", "; ".join(errors))
        credential = credentials or {}
        email, token = credential.get("email"), credential.get("api_token")
        if not isinstance(email, str) or not isinstance(token, str) or not email or not token:
            return ConnectorTest(ConnectionResult.INVALID_CONFIGURATION, "missing_credentials", "Jira email and API token are required.")
        try:
            response = httpx.get(config["base_url"].rstrip("/") + "/rest/api/3/myself",
                auth=(email, token), headers={"Accept": "application/json"}, timeout=httpx.Timeout(8.0))
        except httpx.HTTPError:
            return ConnectorTest(ConnectionResult.UNAVAILABLE, "provider_unavailable", "Jira could not be reached.")
        if response.status_code == 200:
            return ConnectorTest(ConnectionResult.CONNECTED, "connected", "Jira authenticated successfully.")
        if response.status_code in {401, 403}:
            return ConnectorTest(ConnectionResult.INVALID_CONFIGURATION, "credential_rejected", "Jira rejected the configured credentials or permissions.")
        return ConnectorTest(ConnectionResult.UNAVAILABLE, "provider_error", f"Jira returned HTTP {response.status_code}.")


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
            GitHubConnector(),
            JiraConnector(),
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
