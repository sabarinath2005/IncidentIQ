from datetime import datetime, timedelta, timezone

import httpx
import pytest
import socket

from app.services.observability_http import (
    LokiClient, PrometheusClient, ProviderError, TempoClient, normalize_loki_result,
    validate_provider_url,
)
from app.services.alertmanager import safe_alert_fields, severity_for


def test_provider_url_rejects_ssrf_and_accepts_exact_compose_hosts():
    assert validate_provider_url("http://prometheus:9090", {"prometheus"}) == "http://prometheus:9090"
    for url in ("http://127.0.0.1:8080", "http://169.254.169.254/latest/meta-data",
                "https://user:pass@example.com", "https://example.com/?token=x", "file:///etc/passwd"):
        with pytest.raises(ValueError):
            validate_provider_url(url, {"prometheus"})
    with pytest.raises(ValueError, match="allowlist"):
        validate_provider_url("https://metrics.example", set())


def test_allowlisted_public_provider_must_resolve_only_to_public_ip(monkeypatch):
    import app.services.observability_http as clients
    monkeypatch.setattr(clients.socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))])
    assert validate_provider_url("https://metrics.example", set(), {"metrics.example"}) == "https://metrics.example"
    monkeypatch.setattr(clients.socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(ValueError, match="private or reserved"):
        validate_provider_url("https://metrics.example", set(), {"metrics.example"})


def test_prometheus_query_result_and_time_window_are_bounded():
    def handler(request):
        assert request.url.path == "/api/v1/query_range"
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "matrix", "result": []}})
    client = PrometheusClient("http://prometheus:9090", allowed_private_hosts={"prometheus"},
                              transport=httpx.MockTransport(handler))
    end = datetime.now(timezone.utc)
    assert client.query_range("up", end - timedelta(minutes=5), end)["result"] == []
    with pytest.raises(ValueError):
        client.query_range("up", end - timedelta(hours=25), end)


def test_loki_results_drop_sensitive_attributes_and_redact_message_values():
    rows = normalize_loki_result([{"stream": {"service_name": "payment-api", "api_token": "x"},
        "values": [["1730000000000000000", '{"message":"failed token=topsecret", "password":"hidden", "trace_id":"' + "a" * 32 + '"}']]}])
    assert rows[0]["labels"] == {"service_name": "payment-api"}
    assert "topsecret" not in rows[0]["message"]
    assert "password" not in rows[0]["attributes"]


def test_loki_http_and_tempo_trace_clients_parse_real_api_shapes():
    def loki_handler(request):
        return httpx.Response(200, json={"status": "success", "data": {"resultType": "streams", "result": [
            {"stream": {"service_name": "payment-api"}, "values": [["1730000000000000000", "plain log"]]}]}})
    end = datetime.now(timezone.utc)
    loki = LokiClient("http://loki:3100", allowed_private_hosts={"loki"}, transport=httpx.MockTransport(loki_handler))
    assert loki.query_range('{service_name="payment-api"}', end-timedelta(minutes=5), end)["entries"][0]["message"] == "plain log"
    trace_id = "a" * 32
    def tempo_handler(request):
        assert request.url.path == f"/api/traces/{trace_id}"
        return httpx.Response(200, json={"batches": [{"resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "payment-api"}}]}, "scopeSpans": [{"spans": [
            {"traceId": trace_id, "spanId": "b" * 16, "name": "POST /payments", "attributes": [
                {"key": "authorization", "value": {"stringValue": "Bearer do-not-store"}}]}]}]}]})
    tempo = TempoClient("http://tempo:3200", allowed_private_hosts={"tempo"}, transport=httpx.MockTransport(tempo_handler))
    trace = tempo.get_trace(trace_id, end-timedelta(minutes=1), end)
    assert trace["spans"][0]["service_name"] == "payment-api"
    assert trace["spans"][0]["attributes"] == {}


def test_tempo_trace_id_with_omitted_leading_zero_is_padded():
    short_id = "1" + "a" * 30
    padded_id = "0" + short_id
    end = datetime.now(timezone.utc)

    def handler(request):
        assert request.url.path == f"/api/traces/{padded_id}"
        return httpx.Response(200, json={"batches": []})

    tempo = TempoClient("http://tempo:3200", allowed_private_hosts={"tempo"},
                        transport=httpx.MockTransport(handler))
    assert tempo.get_trace(short_id, end-timedelta(minutes=1), end)["trace_id"] == padded_id


def test_http_redirects_and_large_provider_responses_are_not_followed():
    def handler(_request):
        return httpx.Response(302, headers={"Location": "http://169.254.169.254"})
    client = PrometheusClient("http://prometheus:9090", allowed_private_hosts={"prometheus"},
                              transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderError) as error:
        client.query("up")
    assert error.value.code == "provider_http_error"


def test_alert_payload_sanitization_and_severity_mapping():
    labels, annotations = safe_alert_fields({"alertname": "HighErrors", "api_token": "secret",
        "service_name": "payment-api"}, {"summary": "token=private value", "description": "safe"})
    assert labels["alertname"] == "HighErrors" and "api_token" not in labels
    assert "private" not in annotations["summary"]
    assert severity_for("critical") == "SEV-1"
    assert severity_for("warning") == "SEV-3"
