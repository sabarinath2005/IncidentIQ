"""Bounded HTTP clients for local or remote observability providers."""
from __future__ import annotations

import ipaddress
import json
import re
import socket
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

import httpx


MAX_RESPONSE_BYTES = 1_000_000
MAX_PROMETHEUS_RESULTS = 500
MAX_LOG_ENTRIES = 200
MAX_TRACE_RESULTS = 100
MAX_QUERY_RANGE = timedelta(hours=24)
TRACE_ID_RE = re.compile(r"^[0-9a-fA-F]{1,32}$")
SENSITIVE_ATTRIBUTE_RE = re.compile(r"password|token|secret|authorization|api[_-]?key|email", re.IGNORECASE)
SENSITIVE_VALUE_RE = re.compile(r"(?i)(password|token|secret|api[_-]?key|authorization)\s*[:=]\s*[^\s,;]+|Bearer\s+[A-Za-z0-9._~+/-]+=*")


class ProviderError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


def validate_provider_url(value: str, allowed_private_hosts: set[str] | None = None,
                          allowed_public_hosts: set[str] | None = None) -> str:
    """Reject unsafe provider endpoints before making an outbound request.

    Plain HTTP is limited to exact local Compose service names. Other hosts must
    use HTTPS and resolve exclusively to globally routable addresses.
    """
    if not isinstance(value, str) or len(value) > 500:
        raise ValueError("Provider URL must be a URL no longer than 500 characters")
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Provider URL must use http or https and include a host")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Provider URL must not contain user information, query parameters, or fragments")
    host = parsed.hostname.rstrip(".").lower()
    allowed = {item.lower().rstrip(".") for item in (allowed_private_hosts or set())}
    if host in allowed:
        return value.rstrip("/")
    public_allowed = {item.lower().rstrip(".") for item in (allowed_public_hosts or set())}
    if host not in public_allowed:
        raise ValueError("Provider hostname must be in the configured external-host allowlist")
    if parsed.scheme != "https":
        raise ValueError("HTTP is allowed only for explicitly allowlisted local observability services")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("Local and internal provider hosts must be explicitly allowlisted")
    try:
        address = ipaddress.ip_address(host)
        if not address.is_global:
            raise ValueError("Private, loopback, and link-local provider addresses are not allowed")
    except ValueError as exc:
        if "not allowed" in str(exc):
            raise
        try:
            resolved = {ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)}
        except OSError as dns_error:
            raise ValueError("Provider hostname could not be safely resolved") from dns_error
        if not resolved or any(not address.is_global for address in resolved):
            raise ValueError("Provider host resolves to a private or reserved network")
    return value.rstrip("/")


def auth_headers(credentials: dict | None) -> dict[str, str]:
    credentials = credentials or {}
    token = credentials.get("token") or credentials.get("api_key") or credentials.get("access_token")
    if isinstance(token, str) and token:
        return {"Authorization": f"Bearer {token}"}
    username, password = credentials.get("username"), credentials.get("password")
    if isinstance(username, str) and isinstance(password, str):
        import base64
        encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
        return {"Authorization": f"Basic {encoded}"}
    return {}


def _safe_http_error(exc: Exception) -> ProviderError:
    if isinstance(exc, httpx.TimeoutException):
        return ProviderError("provider_timeout", "Provider request timed out", retryable=True)
    if isinstance(exc, httpx.ConnectError):
        return ProviderError("provider_unavailable", "Provider could not be reached", retryable=True)
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return ProviderError("provider_http_error", f"Provider returned HTTP {code}", retryable=code == 429 or code >= 500)
    if isinstance(exc, (httpx.InvalidURL, httpx.UnsupportedProtocol)):
        return ProviderError("provider_invalid_url", "Provider URL is invalid")
    return ProviderError("provider_request_failed", "Provider request failed")


class BoundedHTTPClient:
    def __init__(self, base_url: str, credentials: dict | None = None, *, timeout_seconds: float = 4.0,
                 allowed_private_hosts: set[str] | None = None, allowed_public_hosts: set[str] | None = None,
                 transport: httpx.BaseTransport | None = None):
        self.base_url = validate_provider_url(base_url, allowed_private_hosts, allowed_public_hosts)
        self.headers = auth_headers(credentials)
        self.timeout = httpx.Timeout(timeout_seconds, connect=2.0)
        self.transport = transport

    def get_json(self, path: str, params: dict | None = None) -> dict | list:
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=False, trust_env=False, transport=self.transport) as client:
                with client.stream("GET", url, params=params, headers=self.headers) as response:
                    if response.status_code >= 300:
                        response.raise_for_status()
                    declared_size = response.headers.get("content-length")
                    if declared_size and int(declared_size) > MAX_RESPONSE_BYTES:
                        raise ProviderError("provider_response_too_large", "Provider response exceeded the size limit")
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > MAX_RESPONSE_BYTES:
                            raise ProviderError("provider_response_too_large", "Provider response exceeded the size limit")
                        chunks.append(chunk)
                    response.raise_for_status()
            value = json.loads(b"".join(chunks))
            if not isinstance(value, (dict, list)):
                raise ProviderError("provider_invalid_response", "Provider returned an invalid JSON response")
            return value
        except ProviderError:
            raise
        except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
            raise _safe_http_error(exc) from None

    def get_bytes(self, path: str, params: dict | None = None, accept: str = "application/json") -> bytes:
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        try:
            with httpx.Client(timeout=self.timeout, follow_redirects=False, trust_env=False, transport=self.transport) as client:
                with client.stream("GET", url, params=params, headers={**self.headers, "Accept": accept}) as response:
                    response.raise_for_status()
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data) > MAX_RESPONSE_BYTES:
                            raise ProviderError("provider_response_too_large", "Provider response exceeded the size limit")
            return bytes(data)
        except ProviderError:
            raise
        except httpx.HTTPError as exc:
            raise _safe_http_error(exc) from None


class PrometheusClient(BoundedHTTPClient):
    def ready(self) -> bool:
        self.get_bytes("/-/ready")
        return True

    def query(self, expression: str, at: datetime | None = None) -> dict:
        if not isinstance(expression, str) or not expression.strip() or len(expression) > 2000:
            raise ValueError("PromQL expression must be between 1 and 2000 characters")
        params = {"query": expression.strip()}
        if at is not None:
            params["time"] = str(at.timestamp())
        data = self.get_json("/api/v1/query", params)
        return self._validate_result(data)

    def query_range(self, expression: str, start: datetime, end: datetime, step_seconds: int = 15) -> dict:
        _validate_range(start, end, max_range=MAX_QUERY_RANGE)
        if not isinstance(expression, str) or not expression.strip() or len(expression) > 2000:
            raise ValueError("PromQL expression must be between 1 and 2000 characters")
        if not 1 <= step_seconds <= 3600:
            raise ValueError("Prometheus step must be from 1 to 3600 seconds")
        return self._validate_result(self.get_json("/api/v1/query_range", {
            "query": expression.strip(), "start": str(start.timestamp()), "end": str(end.timestamp()),
            "step": step_seconds,
        }))

    def _validate_result(self, data: dict | list) -> dict:
        if not isinstance(data, dict) or data.get("status") != "success" or not isinstance(data.get("data"), dict):
            raise ProviderError("provider_invalid_response", "Prometheus returned an invalid query response")
        result = data["data"].get("result")
        if not isinstance(result, list) or len(result) > MAX_PROMETHEUS_RESULTS:
            raise ProviderError("provider_result_limit", "Prometheus returned too many query results")
        if data["data"].get("resultType") not in {"vector", "matrix", "scalar", "string"}:
            raise ProviderError("provider_invalid_response", "Prometheus returned an unsupported result type")
        return data["data"]


class LokiClient(BoundedHTTPClient):
    def ready(self) -> bool:
        self.get_bytes("/ready")
        return True

    def query_range(self, query: str, start: datetime, end: datetime, limit: int = 100) -> dict:
        _validate_range(start, end, max_range=MAX_QUERY_RANGE)
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise ValueError("LogQL expression must be between 1 and 2000 characters")
        if not 1 <= limit <= MAX_LOG_ENTRIES:
            raise ValueError(f"Loki result limit must be between 1 and {MAX_LOG_ENTRIES}")
        data = self.get_json("/loki/api/v1/query_range", {
            "query": query, "start": str(int(start.timestamp() * 1_000_000_000)),
            "end": str(int(end.timestamp() * 1_000_000_000)), "limit": str(limit), "direction": "backward",
        })
        if not isinstance(data, dict) or data.get("status") != "success" or not isinstance(data.get("data"), dict):
            raise ProviderError("provider_invalid_response", "Loki returned an invalid query response")
        result = data["data"].get("result")
        if not isinstance(result, list):
            raise ProviderError("provider_invalid_response", "Loki returned an invalid result list")
        normalized = normalize_loki_result(result, limit)
        return {"result_type": data["data"].get("resultType", "streams"), "entries": normalized}


class TempoClient(BoundedHTTPClient):
    def ready(self) -> bool:
        self.get_bytes("/ready")
        return True

    def get_trace(self, trace_id: str, start: datetime | None = None, end: datetime | None = None) -> dict:
        if not isinstance(trace_id, str) or not TRACE_ID_RE.fullmatch(trace_id):
            raise ValueError("Trace ID must be 32 hexadecimal characters")
        trace_id = trace_id.lower().zfill(32)
        params = {}
        if start is not None or end is not None:
            if start is None or end is None:
                raise ValueError("Trace lookup time range requires both start and end")
            _validate_range(start, end, max_range=MAX_QUERY_RANGE)
            params = {"start": str(int(start.timestamp())), "end": str(int(end.timestamp()))}
        data = self.get_json(f"/api/traces/{trace_id}", params)
        spans = normalize_tempo_trace(data, trace_id)
        return {"trace_id": trace_id.lower(), "spans": spans}

    def search(self, service_name: str, start: datetime, end: datetime, limit: int = 20) -> list[dict]:
        _validate_range(start, end, max_range=MAX_QUERY_RANGE)
        if not service_name or len(service_name) > 120 or not 1 <= limit <= MAX_TRACE_RESULTS:
            raise ValueError("Tempo search service name or result limit is invalid")
        escaped = service_name.replace("\\", "\\\\").replace('"', '\\"')
        data = self.get_json("/api/search", {
            "q": f'{{ resource.service.name = "{escaped}" }}',
            "start": str(int(start.timestamp())), "end": str(int(end.timestamp())), "limit": str(limit),
        })
        traces = data.get("traces") if isinstance(data, dict) else None
        if not isinstance(traces, list):
            raise ProviderError("provider_invalid_response", "Tempo returned an invalid search response")
        return traces[:limit]


def _validate_range(start: datetime, end: datetime, *, max_range: timedelta) -> None:
    if start.tzinfo is None or end.tzinfo is None or end <= start or end - start > max_range:
        raise ValueError("Time range must be timezone-aware, ordered, and bounded to 24 hours")


def normalize_loki_result(streams: list, limit: int = MAX_LOG_ENTRIES) -> list[dict]:
    rows: list[dict] = []
    for stream in streams:
        if not isinstance(stream, dict) or not isinstance(stream.get("stream"), dict) or not isinstance(stream.get("values"), list):
            raise ProviderError("provider_invalid_response", "Loki returned an invalid stream")
        labels = {str(k): _redact_text(str(v), 300) for k, v in stream["stream"].items()
                  if isinstance(k, str) and not SENSITIVE_ATTRIBUTE_RE.search(k)}
        for pair in stream["values"]:
            if not isinstance(pair, list) or len(pair) < 2:
                continue
            timestamp, line = str(pair[0]), str(pair[1])[:4000]
            try:
                structured = json.loads(line)
            except (ValueError, TypeError):
                structured = {}
            if not isinstance(structured, dict):
                structured = {}
            rows.append({
                "timestamp_ns": timestamp, "timestamp": _ns_to_iso(timestamp), "labels": labels,
                "level": str(structured.get("level", labels.get("level", labels.get("severity", "unknown"))))[:32],
                "service": str(structured.get("service.name", labels.get("service_name", "unknown")))[:120],
                "message": _redact_text(str(structured.get("message", structured.get("msg", line))), 4000),
                "trace_id": _bounded_optional(structured.get("trace_id"), 64),
                "request_id": _bounded_optional(structured.get("request_id"), 128),
                "attributes": {str(k)[:100]: _redact_text(str(v), 500) for k, v in structured.items()
                               if k not in {"message", "msg", "trace_id", "request_id"} and isinstance(k, str)
                               and not SENSITIVE_ATTRIBUTE_RE.search(k)}
                               if len(rows) < limit else {},
            })
            if len(rows) >= limit:
                return rows
    return rows


def normalize_tempo_trace(data: dict | list, trace_id: str) -> list[dict]:
    batches = data if isinstance(data, list) else data.get("batches") if isinstance(data, dict) else None
    if not isinstance(batches, list):
        raise ProviderError("provider_invalid_response", "Tempo returned an invalid trace")
    spans: list[dict] = []
    for batch in batches:
        if not isinstance(batch, dict):
            continue
        resource = batch.get("resource", {})
        resource_attrs = _kv_attributes(resource.get("attributes", [])) if isinstance(resource, dict) else {}
        scopes = batch.get("instrumentationLibrarySpans", batch.get("scopeSpans", []))
        if not isinstance(scopes, list):
            continue
        for scope in scopes:
            if not isinstance(scope, dict) or not isinstance(scope.get("spans"), list):
                continue
            for span in scope["spans"]:
                if not isinstance(span, dict):
                    continue
                attrs = _kv_attributes(span.get("attributes", []))
                spans.append({
                    "trace_id": str(span.get("traceId", trace_id)).lower(),
                    "span_id": str(span.get("spanId", "")),
                    "parent_span_id": str(span.get("parentSpanId", "")) or None,
                    "service_name": str(resource_attrs.get("service.name", "unknown"))[:120],
                    "operation_name": str(span.get("name", "unknown"))[:300],
                    "start_time_unix_nano": str(span.get("startTimeUnixNano", "")),
                    "duration_nano": _span_duration(span), "status": _span_status(span),
                    "attributes": {k: v for k, v in attrs.items() if not _sensitive_name(k)},
                })
                if len(spans) >= 500:
                    return spans
    return spans


def _kv_attributes(items) -> dict:
    output = {}
    if not isinstance(items, list):
        return output
    for item in items[:300]:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str):
            continue
        value = item.get("value", {})
        if not isinstance(value, dict):
            continue
        atom = next((value[key] for key in ("stringValue", "intValue", "boolValue", "doubleValue") if key in value), "")
        output[item["key"][:100]] = str(atom)[:500]
    return output


def _span_duration(span: dict) -> int | None:
    try:
        return max(0, int(span.get("endTimeUnixNano", 0)) - int(span.get("startTimeUnixNano", 0)))
    except (TypeError, ValueError):
        return None


def _span_status(span: dict) -> str:
    status = span.get("status", {})
    return str(status.get("code", "UNSET")) if isinstance(status, dict) else "UNSET"


def _ns_to_iso(value: str) -> str:
    try:
        return datetime.fromtimestamp(int(value) / 1_000_000_000, timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        return ""


def _bounded_optional(value, length: int) -> str | None:
    return str(value)[:length] if value is not None else None


def _redact_text(value: str, limit: int) -> str:
    return SENSITIVE_VALUE_RE.sub("[REDACTED]", value)[:limit]


def _sensitive_name(name: str) -> bool:
    lowered = name.lower()
    return any(word in lowered for word in ("password", "token", "secret", "authorization", "api_key", "email"))
