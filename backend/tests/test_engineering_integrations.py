import hashlib
import hmac
import json

import httpx
import pytest

from app.services.github import _event_data, valid_signature
import app.services.jira as jira_module
import app.services.connectors as connector_module
from app.services.jira import (JiraAdapter, JiraProviderError, build_incident_description,
                               incident_url_for_origin, jira_priority_for_risk)


def test_github_webhook_signature_is_exact_and_constant_time_compatible():
    body, secret = b'{"action":"published"}', "test-webhook-secret"
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert valid_signature(body, signature, secret)
    assert not valid_signature(body + b" ", signature, secret)
    assert not valid_signature(body, signature.upper(), secret)
    assert not valid_signature(body, signature, "")


def test_github_push_normalization_preserves_event_time_and_neutral_summary():
    timestamp = "2026-11-14T10:00:00Z"
    event = {"repository": {"id": 42, "full_name": "acme/checkout"}, "ref": "refs/heads/main",
        "after": "abcdef0123456789", "commits": [{"id": "abcdef0123456789", "message": "password=supersecret",
        "timestamp": timestamp}], "head_commit": {"id": "abcdef0123456789", "timestamp": timestamp}}
    repo_id, repo_name, external_id, summary, occurred_at, state = _event_data("push", event)
    assert (repo_id, repo_name, external_id, state) == ("42", "acme/checkout", "abcdef0123456789", "push")
    assert occurred_at.isoformat() == "2026-11-14T10:00:00+00:00"
    assert "supersecret" not in summary
    assert "caus" not in summary.lower()


def test_jira_issue_create_reconciles_using_idempotency_marker(monkeypatch):
    monkeypatch.setattr(jira_module, "validate_jira_base_url", lambda value: value.rstrip("/"))
    calls = {"post": 0, "search": 0}
    create_payloads = []

    def respond(request):
        if request.method == "GET":
            assert request.url.path == "/rest/api/3/search/jql"
            calls["search"] += 1
            issue = {"id": "1001", "key": "OPS-9", "fields": {"status": {"name": "To Do"}}}
            return httpx.Response(200, json={"issues": [issue] if calls["search"] > 1 else []}, request=request)
        calls["post"] += 1
        payload = json.loads(request.content)
        create_payloads.append(payload)
        text_value = json.dumps(payload)
        assert "IIQ" + "A" * 32 in text_value
        return httpx.Response(201, json={"id": "1001", "key": "OPS-9"}, request=request)

    adapter = JiraAdapter("https://example.atlassian.net", "user@example.test", "test-token",
                          transport=httpx.MockTransport(respond))
    kwargs = {"project_key": "OPS", "summary": "Investigate checkout timeout",
              "description": "IncidentIQ incident reference\\nIncident ID: test-id\\nReview verified evidence.",
              "marker": "IIQ" + "A" * 32, "priority": "High"}
    first = adapter.create_or_reconcile(**kwargs)
    second = adapter.create_or_reconcile(**kwargs)
    assert first["key"] == second["key"] == "OPS-9"
    assert calls == {"post": 1, "search": 2}
    fields = create_payloads[0]["fields"]
    assert fields["priority"] == {"name": "High"}
    description_text = "\n".join(paragraph["content"][0]["text"]
        for paragraph in fields["description"]["content"])
    assert "Incident ID: test-id" in description_text


@pytest.mark.parametrize("risk_level,expected", [
    ("low", "Low"), ("medium", "Medium"), ("high", "High"), ("critical", "Highest"),
    ("unsupported", None), (None, None), ("", None),
])
def test_recommendation_risk_maps_to_supported_jira_priority(risk_level, expected):
    assert jira_priority_for_risk(risk_level) == expected


def test_jira_incident_description_has_id_title_link_and_investigation_context():
    incident_id = "12345678-1234-4234-8234-1234567890ab"
    link = incident_url_for_origin("https://incidentiq.example/", incident_id)
    assert link == f"https://incidentiq.example/incidents/{incident_id}"
    assert incident_url_for_origin("javascript:alert(1)", incident_id) == ""
    body = build_incident_description(incident_id=incident_id, incident_title="Checkout outage",
        incident_url=link, recommendation_description="Inspect the pool limit.",
        expected_benefit="Reduce timeout frequency", required_permissions=["incident:review"],
        verification_steps=["Compare pool usage after the change"], evidence_refs=["evidence-123"],
        investigation_context="Run ID: run-123; provider: openai; model: test-model; status: waiting_for_review")
    for value in (incident_id, "Checkout outage", link, "Inspect the pool limit.", "Reduce timeout frequency",
                  "incident:review", "Compare pool usage after the change", "evidence-123", "run-123"):
        assert value in body


def test_jira_unsupported_priority_falls_back_to_project_default(monkeypatch):
    monkeypatch.setattr(jira_module, "validate_jira_base_url", lambda value: value.rstrip("/"))
    requests = []

    def respond(request):
        if request.method == "GET":
            return httpx.Response(200, json={"issues": []}, request=request)
        payload = json.loads(request.content)
        requests.append(payload)
        if "priority" in payload["fields"]:
            return httpx.Response(400, json={"errors": {"priority": "Priority is unavailable"}}, request=request)
        return httpx.Response(201, json={"id": "1002", "key": "OPS-10"}, request=request)

    adapter = JiraAdapter("https://example.atlassian.net", "user@example.test", "test-token",
                          transport=httpx.MockTransport(respond))
    result = adapter.create_or_reconcile(project_key="OPS", summary="Review", description="IncidentIQ reference",
        marker="IIQ" + "B" * 32, priority="Low")
    assert result["id"] == "1002"
    assert len(requests) == 2
    assert requests[0]["fields"]["priority"] == {"name": "Low"}
    assert "priority" not in requests[1]["fields"]


def test_jira_missing_or_unknown_priority_uses_project_default(monkeypatch):
    monkeypatch.setattr(jira_module, "validate_jira_base_url", lambda value: value.rstrip("/"))
    payloads = []

    def respond(request):
        if request.method == "GET":
            return httpx.Response(200, json={"issues": []}, request=request)
        payloads.append(json.loads(request.content))
        return httpx.Response(201, json={"id": "1003", "key": "OPS-11"}, request=request)

    adapter = JiraAdapter("https://example.atlassian.net", "user@example.test", "test-token",
                          transport=httpx.MockTransport(respond))
    for index, priority in enumerate((None, "Not a Jira priority")):
        adapter.create_or_reconcile(project_key="OPS", summary="Review", description="IncidentIQ reference",
            marker="IIQ" + f"{index:032X}", priority=priority)
    assert all("priority" not in payload["fields"] for payload in payloads)


def test_jira_invalid_payload_error_is_safe_and_does_not_leak_credentials(monkeypatch):
    monkeypatch.setattr(jira_module, "validate_jira_base_url", lambda value: value.rstrip("/"))
    def respond(request):
        if request.method == "GET":
            return httpx.Response(200, json={"issues": []}, request=request)
        return httpx.Response(400, json={"errors": {"summary": "REDACTED_RESPONSE_BODY"}}, request=request)
    adapter = JiraAdapter("https://example.atlassian.net", "user@example.test", "test-token",
                          transport=httpx.MockTransport(respond))
    with pytest.raises(JiraProviderError) as error:
        adapter.create_or_reconcile(project_key="OPS", summary="Review", description="Details",
            marker="IIQ" + "C" * 32, priority="Medium")
    assert "400" in str(error.value)
    assert "test-token" not in str(error.value)
    assert "REDACTED_RESPONSE_BODY" not in str(error.value)


def test_jira_issue_status_sync_and_rate_limit_error(monkeypatch):
    monkeypatch.setattr(jira_module, "validate_jira_base_url", lambda value: value.rstrip("/"))
    adapter = JiraAdapter("https://example.atlassian.net", "user@example.test", "test-token",
        transport=httpx.MockTransport(lambda request: httpx.Response(200,
            json={"fields": {"status": {"name": "In Progress"}}}, request=request)))
    assert adapter.issue_status("OPS-9") == "In Progress"
    limited = JiraAdapter("https://example.atlassian.net", "user@example.test", "test-token",
        transport=httpx.MockTransport(lambda request: httpx.Response(429, request=request)))
    with pytest.raises(JiraProviderError, match="temporarily unavailable"):
        limited.find_by_marker("OPS", "IIQ" + "A" * 32)


def test_github_connection_requires_mapped_repository_and_checks_authenticated_repo_access(monkeypatch):
    seen = []
    def accepted(url, headers, timeout):
        seen.append((url, headers["Authorization"]))
        return httpx.Response(200, json={"full_name": "Acme/checkout"})
    monkeypatch.setattr(connector_module.httpx, "get", accepted)
    connector = connector_module.GitHubConnector()
    missing = connector.test_connection({}, {"token": "secret-token"})
    assert missing.result.value == "INVALID_CONFIGURATION"
    assert missing.code == "missing_repository_configuration"
    assert not seen

    config = {"repositories": ["acme/checkout"]}
    result = connector.test_connection(config, {"token": "secret-token"})
    assert result.result.value == "CONNECTED"
    assert seen == [("https://api.github.com/repos/acme/checkout", "Bearer secret-token")]
    assert "secret-token" not in result.message


def test_github_connection_checks_every_repository_mapping(monkeypatch):
    requested = []
    def respond(url, **kwargs):
        requested.append(url)
        full_name = url.rsplit("/", 2)[-2] + "/" + url.rsplit("/", 1)[-1]
        if full_name == "acme/private":
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(200, json={"full_name": full_name})
    monkeypatch.setattr(connector_module.httpx, "get", respond)
    result = connector_module.GitHubConnector().test_connection(
        {"repositories": ["acme/public", "acme/private"]}, {"token": "test-token"})
    assert requested == ["https://api.github.com/repos/acme/public", "https://api.github.com/repos/acme/private"]
    assert result.code == "repository_inaccessible"


def test_github_connection_provider_transport_outage_is_safe(monkeypatch):
    token = "secret-token-that-must-not-escape"
    def offline(url, **kwargs):
        raise httpx.ConnectError("transport contained " + token, request=httpx.Request("GET", url))
    monkeypatch.setattr(connector_module.httpx, "get", offline)
    result = connector_module.GitHubConnector().test_connection(
        {"repositories": ["acme/checkout"]}, {"token": token})
    assert result.result.value == "UNAVAILABLE"
    assert result.code == "provider_unavailable"
    assert token not in result.message


@pytest.mark.parametrize("status,headers,expected_result,expected_code", [
    (401, {}, "INVALID_CONFIGURATION", "credential_rejected"),
    (404, {}, "INVALID_CONFIGURATION", "repository_inaccessible"),
    (403, {}, "INVALID_CONFIGURATION", "insufficient_permissions"),
    (403, {"X-RateLimit-Remaining": "0"}, "UNAVAILABLE", "rate_limited"),
    (429, {}, "UNAVAILABLE", "rate_limited"),
    (503, {}, "UNAVAILABLE", "provider_unavailable"),
])
def test_github_repository_access_failures_are_distinguished_and_redacted(
        monkeypatch, status, headers, expected_result, expected_code):
    token = "secret-token-that-must-not-escape"
    monkeypatch.setattr(connector_module.httpx, "get", lambda *args, **kwargs:
        httpx.Response(status, headers=headers, json={"message": token}, request=httpx.Request("GET", args[0])))
    result = connector_module.GitHubConnector().test_connection(
        {"repositories": ["acme/checkout"]}, {"token": token})
    assert result.result.value == expected_result
    assert result.code == expected_code
    assert token not in result.message
