"""End-to-end authorization and audit checks on disposable PostgreSQL schemas."""
import os
from uuid import UUID, uuid4
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.security import current_user
from app.db import get_db
from app.main import app
from app.models import (
    ApprovalRequest, AuditEvent, Incident, Integration, Issue, Project, ProjectMember,
    Recommendation, User, Workspace, WorkspaceMember, Service, AlertmanagerAlert, EvidenceItem, OutboxEvent,
    IntegrationSyncRun,
)
from app.schemas import AlertmanagerWebhookPayload
from app.services.alertmanager import process_alertmanager_webhook
from app.services import telemetry_collection
from app.services.observability_http import ProviderError


pytestmark = pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"),
    reason="Set TEST_DATABASE_URL to the disposable incident_platform_test database",
)


@pytest.fixture
def access_scenario(pg_session_factory, monkeypatch):
    monkeypatch.setattr(settings, "integration_encryption_key", Fernet.generate_key().decode())
    db = pg_session_factory()
    people = {
        role: User(email=f"{role}-{uuid4().hex[:8]}@example.test", full_name=role,
                   password_hash="test-hash")
        for role in ("org_admin", "admin", "incident_manager", "engineer", "viewer", "outsider")
    }
    workspace_a, workspace_b = Workspace(name="Workspace A"), Workspace(name="Workspace B")
    db.add_all([*people.values(), workspace_a, workspace_b])
    db.flush()
    for role in ("org_admin", "admin", "incident_manager", "engineer", "viewer"):
        db.add(WorkspaceMember(workspace_id=workspace_a.id, user_id=people[role].id,
                               role="owner" if role == "org_admin" else "member"))
    db.add(WorkspaceMember(workspace_id=workspace_b.id, user_id=people["outsider"].id, role="owner"))
    project_a = Project(workspace_id=workspace_a.id, name="Project A")
    project_b = Project(workspace_id=workspace_b.id, name="Project B")
    db.add_all([project_a, project_b])
    db.flush()
    for role in ("admin", "incident_manager", "engineer", "viewer"):
        db.add(ProjectMember(workspace_id=workspace_a.id, project_id=project_a.id,
                             user_id=people[role].id, role=role))
    db.add(ProjectMember(workspace_id=workspace_b.id, project_id=project_b.id,
                         user_id=people["outsider"].id, role="admin"))
    db.flush()
    incident = Incident(workspace_id=workspace_a.id, project_id=project_a.id,
                        title="Test incident", reporter_id=people["admin"].id)
    integration = Integration(workspace_id=workspace_a.id, project_id=project_a.id,
                              provider="slack", config={})
    recommendation = Recommendation(workspace_id=workspace_a.id, project_id=project_a.id,
                                    incident_id=None, title="Test action", description="Simulation")
    # Recommendation requires an incident, so create the incident before it is flushed.
    db.add(incident)
    db.flush()
    recommendation.incident_id = incident.id
    db.add_all([integration, recommendation])
    db.flush()
    approvals = {}
    for role, person in people.items():
        if role in {"org_admin", "outsider"}:
            continue
        rec = Recommendation(workspace_id=workspace_a.id, project_id=project_a.id,
                             incident_id=incident.id, title=f"Action {role}", description="Simulation")
        db.add(rec)
        db.flush()
        approvals[role] = ApprovalRequest(
            workspace_id=workspace_a.id, project_id=project_a.id, recommendation_id=rec.id,
            requested_by=person.id, action_snapshot={"mode": "simulation"}, action_hash="a" * 64,
        )
    db.add_all(approvals.values())
    db.commit()
    yield {"db": db, "people": people, "workspace_a": workspace_a, "workspace_b": workspace_b,
           "project_a": project_a, "project_b": project_b, "incident": incident,
           "integration": integration, "recommendation": recommendation, "approvals": approvals}
    app.dependency_overrides.pop(current_user, None)
    app.dependency_overrides.pop(get_db, None)
    db.close()


def client_as(db, user):
    app.dependency_overrides[current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def test_workspace_and_project_lists_are_membership_scoped(access_scenario):
    s = access_scenario
    with client_as(s["db"], s["people"]["org_admin"]) as client:
        workspaces = client.get("/api/v1/workspaces").json()
        assert [row["id"] for row in workspaces] == [str(s["workspace_a"].id)]
        # Workspace admins manage workspaces but do not bypass explicit project membership.
        assert client.get("/api/v1/projects").json() == []
        assert client.get(f"/api/v1/projects/{s['project_a'].id}/services").status_code == 403
        assert client.post(f"/api/v1/workspaces/{s['workspace_a'].id}/teams",
                           json={"name": "Org Admin Team"}).status_code == 201
    with client_as(s["db"], s["people"]["outsider"]) as client:
        assert [row["id"] for row in client.get("/api/v1/projects").json()] == [str(s["project_b"].id)]
        assert client.get(f"/api/v1/projects/{s['project_a'].id}/services").status_code == 403
        assert client.get(f"/api/v1/projects/{s['project_a'].id}/integrations").status_code == 403
    with client_as(s["db"], s["people"]["viewer"]) as client:
        assert client.post(f"/api/v1/workspaces/{s['workspace_a'].id}/teams",
                           json={"name": "Unauthorized Team"}).status_code == 403


@pytest.mark.parametrize("role,write_status", [
    ("admin", 201), ("incident_manager", 201), ("engineer", 201), ("viewer", 403),
])
def test_project_roles_cover_services_incidents_and_approval_requests(access_scenario, role, write_status):
    s = access_scenario
    with client_as(s["db"], s["people"][role]) as client:
        project_id = s["project_a"].id
        assert client.get(f"/api/v1/projects/{project_id}/services").status_code == 200
        assert client.get(f"/api/v1/projects/{project_id}/incidents").status_code == 200
        assert client.get(f"/api/v1/projects/{project_id}/integrations").status_code == 200
        assert client.post(f"/api/v1/projects/{project_id}/services",
                           json={"name": f"service-{role}"}).status_code == write_status
        approval = client.post(
            f"/api/v1/recommendations/{s['recommendation'].id}/approvals"
        )
        assert approval.status_code == write_status
        assert client.post(f"/api/v1/approvals/{s['approvals'][role].id}/decision",
                           json={"status": "approved"}).status_code == (200 if role == "admin" else 403)


@pytest.mark.parametrize("role", ["org_admin", "incident_manager", "engineer", "viewer"])
def test_credentials_are_admin_only_and_api_response_uses_constant_mask(access_scenario, role):
    s = access_scenario
    with client_as(s["db"], s["people"][role]) as client:
        assert client.put(f"/api/v1/integrations/{s['integration'].id}/credentials",
                          json={"credentials": {"token": "secret-value"}}).status_code == 403
    with client_as(s["db"], s["people"]["admin"]) as client:
        response = client.put(f"/api/v1/integrations/{s['integration'].id}/credentials",
                              json={"credentials": {"token": "a-secret-with-variable-length"}})
        assert response.status_code == 200
        assert response.json()["credentials"] == "********"
        assert "a-secret-with-variable-length" not in response.text
        assert s["integration"].credentials_ciphertext not in response.text


def test_member_approval_link_and_unlink_actions_are_audited(access_scenario):
    s = access_scenario
    db, actor = s["db"], s["people"]["admin"]
    newcomer = User(email=f"new-{uuid4().hex[:8]}@example.test", full_name="New member", password_hash="hash")
    historical_member = User(email=f"history-{uuid4().hex[:8]}@example.test",
                             full_name="Historical member", password_hash="hash")
    db.add(newcomer)
    db.add(historical_member)
    db.flush()
    db.add(WorkspaceMember(workspace_id=s["workspace_a"].id, user_id=newcomer.id, role="member"))
    db.add(WorkspaceMember(workspace_id=s["workspace_a"].id, user_id=historical_member.id, role="member"))
    issue = Issue(workspace_id=s["workspace_a"].id, project_id=s["project_a"].id, key="AUD-1",
                  title="Audit target", reporter_id=actor.id)
    db.add(issue)
    db.commit()
    with client_as(db, actor) as client:
        base = f"/api/v1/projects/{s['project_a'].id}/members"
        assert client.post(base, json={"email": newcomer.email, "role": "engineer"}).status_code == 201
        assert client.post(base, json={"email": newcomer.email, "role": "viewer"}).status_code == 201
        assert client.post(base, json={"email": historical_member.email, "role": "engineer"}).status_code == 201
        approval = client.post(f"/api/v1/recommendations/{s['recommendation'].id}/approvals")
        assert approval.status_code == 201
        link_url = f"/api/v1/incidents/{s['incident'].id}/issues/{issue.id}"
        assert client.post(link_url).status_code == 201
        assert client.delete(link_url).status_code == 204
        assert client.delete(f"{base}/{newcomer.id}").status_code == 204
        historical_issue = Issue(workspace_id=s["workspace_a"].id, project_id=s["project_a"].id,
                                 key="AUD-2", title="Keep history", reporter_id=historical_member.id)
        db.add(historical_issue)
        db.commit()
        assert client.delete(f"{base}/{historical_member.id}").status_code == 409
        assert db.get(Issue, historical_issue.id) is not None
        assert db.get(ProjectMember, (s["project_a"].id, historical_member.id)) is not None
    events = db.query(AuditEvent).filter(AuditEvent.project_id == s["project_a"].id).all()
    actions = {event.action for event in events}
    assert {
        "project.member_added", "project.member_role_changed", "project.member_removed",
        "approval.requested", "incident.issue_linked", "incident.issue_unlinked",
    } <= actions
    for event in events:
        assert event.actor_id == actor.id
        assert event.workspace_id == s["workspace_a"].id
        assert event.created_at is not None
        assert "secret" not in str(event.detail).lower()


def test_manual_and_uploaded_evidence_audits_reference_created_resources(access_scenario, monkeypatch, tmp_path):
    s = access_scenario
    monkeypatch.setattr(settings, "artifact_dir", str(tmp_path))
    with client_as(s["db"], s["people"]["admin"]) as client:
        manual = client.post(
            f"/api/v1/incidents/{s['incident'].id}/evidence",
            json={"source_type": "diagnostic", "summary": "Synthetic manual evidence"},
        )
        assert manual.status_code == 201
        uploaded = client.post(
            f"/api/v1/incidents/{s['incident'].id}/evidence/upload",
            files={"file": ("synthetic.log", b"synthetic diagnostic", "text/plain")},
        )
        assert uploaded.status_code == 201

    events = s["db"].query(AuditEvent).filter(
        AuditEvent.action.in_(["incident.evidence_added", "incident.evidence_uploaded"])
    ).all()
    by_action = {event.action: event for event in events}
    assert by_action["incident.evidence_added"].resource_id == UUID(manual.json()["id"])
    assert by_action["incident.evidence_uploaded"].resource_id == UUID(uploaded.json()["id"])
    assert all(event.actor_id == s["people"]["admin"].id for event in events)


def test_alertmanager_firing_resolved_dedup_and_evidence_provenance(access_scenario):
    s, db = access_scenario, access_scenario["db"]
    service = Service(workspace_id=s["workspace_a"].id, project_id=s["project_a"].id,
                      name="payment-api", environment="test")
    db.add(service)
    db.flush()
    integration = Integration(workspace_id=s["workspace_a"].id, project_id=s["project_a"].id,
        provider="alertmanager", enabled=True, config={"service_id": str(service.id)})
    db.add(integration)
    db.commit()
    base = {"receiver": "incidentiq", "status": "firing", "alerts": [{
        "status": "firing", "labels": {"alertname": "PaymentApiHighErrorRate", "severity": "critical",
            "service_name": "payment-api", "api_token": "must-not-persist"},
        "annotations": {"summary": "token=must-not-persist error rate high"},
        "startsAt": "2026-11-14T10:00:00Z", "endsAt": "0001-01-01T00:00:00Z",
        "generatorURL": "https://monitor.example/?token=must-not-persist", "fingerprint": "a" * 64,
    }]}
    first = process_alertmanager_webhook(db, integration, AlertmanagerWebhookPayload.model_validate(base))
    assert first["created"] == 1
    incident = db.query(Incident).filter(Incident.title.like("[SEV-1] PaymentApiHighErrorRate%")).one()
    assert db.query(OutboxEvent).filter_by(event_type="alert.telemetry.requested").count() == 1
    duplicate = process_alertmanager_webhook(db, integration, AlertmanagerWebhookPayload.model_validate(base))
    assert duplicate["duplicate"] == 1
    assert db.query(Incident).filter(Incident.title.like("[SEV-1] PaymentApiHighErrorRate%")).count() == 1

    resolved = {**base, "status": "resolved", "alerts": [{**base["alerts"][0],
        "status": "resolved", "endsAt": "2026-11-14T10:20:00Z"}]}
    result = process_alertmanager_webhook(db, integration, AlertmanagerWebhookPayload.model_validate(resolved))
    assert result["resolved"] == 1
    row = db.query(AlertmanagerAlert).filter_by(integration_id=integration.id).one()
    assert row.status == "resolved" and row.incident_id == incident.id
    evidence = db.query(EvidenceItem).filter_by(incident_id=incident.id, provider="alertmanager").all()
    assert len(evidence) == 2
    assert all("must-not-persist" not in str(item.metadata_json) for item in evidence)
    assert all("generator_url" not in item.metadata_json for item in evidence)
    audit_rows = db.query(AuditEvent).filter(AuditEvent.action.like("alertmanager.%")).all()
    assert len(audit_rows) == 2
    assert all(row.workspace_id == s["workspace_a"].id and row.project_id == s["project_a"].id for row in audit_rows)


def test_alertmanager_rejects_service_claim_outside_configured_project_service(access_scenario):
    s, db = access_scenario, access_scenario["db"]
    service = Service(workspace_id=s["workspace_a"].id, project_id=s["project_a"].id,
                      name="payment-api", environment="test")
    db.add(service)
    db.flush()
    integration = Integration(workspace_id=s["workspace_a"].id, project_id=s["project_a"].id,
        provider="alertmanager", enabled=True, config={"service_id": str(service.id)})
    db.add(integration)
    db.commit()
    payload = AlertmanagerWebhookPayload.model_validate({"status": "firing", "alerts": [{
        "status": "firing", "labels": {"alertname": "Nope", "service_id": str(s["project_b"].id)},
        "startsAt": "2026-11-14T10:00:00Z", "fingerprint": "b" * 64,
    }]})
    with pytest.raises(Exception) as exc:
        process_alertmanager_webhook(db, integration, payload)
    assert getattr(exc.value, "status_code", None) == 403


def test_prometheus_collection_persists_idempotent_evidence_and_unavailable_status(access_scenario, monkeypatch):
    s, db = access_scenario, access_scenario["db"]
    service = Service(workspace_id=s["workspace_a"].id, project_id=s["project_a"].id,
                      name="payment-api", environment="test")
    db.add(service)
    db.flush()
    integration = Integration(workspace_id=s["workspace_a"].id, project_id=s["project_a"].id,
        provider="prometheus", enabled=True, config={"base_url": "http://prometheus:9090",
        "service_id": str(service.id)})
    db.add(integration)
    db.commit()

    class FakePrometheus:
        def __init__(self, *args, **kwargs): pass
        def query_range(self, expression, start, end, step_seconds=60):
            return {"resultType": "matrix", "result": [{"metric": {"__name__": expression[:24]},
                    "values": [[end.timestamp(), "1"]]}]}

    monkeypatch.setattr(telemetry_collection, "PrometheusClient", FakePrometheus)
    start, end = datetime.now(timezone.utc)-timedelta(minutes=5), datetime.now(timezone.utc)
    first = telemetry_collection.collect_incident_telemetry(db, s["incident"], start=start, end=end)
    assert first["outcomes"] == [{"provider": "prometheus", "status": "completed", "count": 10}]
    second = telemetry_collection.collect_incident_telemetry(db, s["incident"], start=start, end=end)
    assert second["outcomes"] == [{"provider": "prometheus", "status": "completed", "count": 0}]
    evidence = db.query(EvidenceItem).filter_by(incident_id=s["incident"].id, provider="prometheus").all()
    assert len(evidence) == 10
    assert all(item.collection_metadata["query_window_start"] == start.isoformat() for item in evidence)

    class UnavailablePrometheus:
        def __init__(self, *args, **kwargs): pass
        def query_range(self, *args, **kwargs):
            raise ProviderError("provider_unavailable", "Provider could not be reached", retryable=True)

    monkeypatch.setattr(telemetry_collection, "PrometheusClient", UnavailablePrometheus)
    unavailable = telemetry_collection.collect_incident_telemetry(db, s["incident"],
        start=start-timedelta(minutes=1), end=end)
    assert unavailable["outcomes"][0]["status"] == "unavailable"
    sync = db.query(IntegrationSyncRun).filter_by(incident_id=s["incident"].id,
        integration_id=integration.id).order_by(IntegrationSyncRun.started_at.desc()).first()
    assert sync.status == "unavailable" and sync.error_code == "provider_unavailable"


def test_loki_default_collection_keeps_structured_severity_logs(access_scenario, monkeypatch):
    s, db = access_scenario, access_scenario["db"]
    service = Service(workspace_id=s["workspace_a"].id, project_id=s["project_a"].id,
                      name="payment-api", environment="test")
    db.add(service)
    db.flush()
    integration = Integration(workspace_id=s["workspace_a"].id, project_id=s["project_a"].id,
        provider="loki", enabled=True, config={"base_url": "http://loki:3100",
        "service_id": str(service.id)})
    db.add(integration)
    db.commit()

    class FakeLoki:
        query = None
        def __init__(self, *args, **kwargs): pass
        def query_range(self, query, start, end, limit=100):
            self.query = query
            return {"entries": [{"timestamp": end.isoformat(), "timestamp_ns": "1700000000000000000",
                    "message": "controlled payment failure", "level": "ERROR",
                    "service": "payment-api", "labels": {"service_name": "payment-api"}}]}

    monkeypatch.setattr(telemetry_collection, "LokiClient", FakeLoki)
    start, end = datetime.now(timezone.utc)-timedelta(minutes=5), datetime.now(timezone.utc)
    result = telemetry_collection.collect_incident_telemetry(db, s["incident"], start=start, end=end)
    assert result["outcomes"] == [{"provider": "loki", "status": "completed", "count": 1}]
    evidence = db.query(EvidenceItem).filter_by(incident_id=s["incident"].id, provider="loki").one()
    assert evidence.summary.endswith("controlled payment failure")
    assert evidence.collection_metadata["query"] == '{service_name="payment-api"}'
