from datetime import datetime, timezone
import hashlib
import hmac
import json
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.core.config import settings
from app.core.security import create_token
from app.db import get_db
from app.main import app
from app.models import (ApprovalRequest, EvidenceItem, ExternalIssueLink, GitHubDelivery,
                        GitHubRepositoryMapping, Incident, IncidentService, Integration,
                        JiraProjectMapping, Project, ProjectMember, Recommendation, Service,
                        User, Workspace, WorkspaceMember)
from app.services.credentials import encrypt_credentials
from app.services.github import ingest_delivery


def test_github_delivery_is_idempotent_and_correlates_only_mapped_service(pg_session_factory):
    db = pg_session_factory()
    try:
        user = User(email="github-test@example.test", full_name="Test", password_hash="test")
        workspace = Workspace(name="GitHub test")
        db.add_all([user, workspace]); db.flush()
        db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role="owner")); db.flush()
        project = Project(workspace_id=workspace.id, name="GitHub scoped project")
        db.add(project); db.flush()
        db.add(ProjectMember(workspace_id=workspace.id, project_id=project.id, user_id=user.id, role="admin")); db.flush()
        service = Service(workspace_id=workspace.id, project_id=project.id, name="checkout-api")
        db.add(service); db.flush()
        now = datetime.now(timezone.utc)
        incident = Incident(workspace_id=workspace.id, project_id=project.id, title="Current outage",
            reporter_id=user.id, status="investigating", started_at=now, detected_at=now)
        db.add(incident); db.flush()
        db.add(IncidentService(workspace_id=workspace.id, project_id=project.id,
            incident_id=incident.id, service_id=service.id))
        integration = Integration(workspace_id=workspace.id, project_id=project.id,
            provider="github", enabled=True, config={})
        db.add(integration); db.flush()
        db.add(GitHubRepositoryMapping(workspace_id=workspace.id, project_id=project.id,
            integration_id=integration.id, service_id=service.id, repository_id="42",
            repository_full_name="acme/checkout")); db.flush()
        payload = {"repository": {"id": 42, "full_name": "acme/checkout"}, "ref": "refs/heads/main",
            "after": "abcdef0123456789", "commits": [{"id": "abcdef0123456789", "timestamp": now.isoformat()}],
            "head_commit": {"id": "abcdef0123456789", "timestamp": now.isoformat()}}
        first, duplicate_first = ingest_delivery(db, integration, "delivery-1", "push", payload)
        second, duplicate_second = ingest_delivery(db, integration, "delivery-1", "push", payload)
        assert first.id == second.id
        assert duplicate_first is False and duplicate_second is True
        assert first.status == "correlated"
        assert "not causation" in first.correlation_reason
        assert db.query(EvidenceItem).filter_by(incident_id=incident.id, provider="github").count() == 1
    finally:
        db.close()


def _principal(db):
    user = User(email=f"connector-{uuid4().hex}@example.test", full_name="Connector Admin", password_hash="test")
    workspace = Workspace(name="Connector tests")
    db.add_all([user, workspace]); db.flush()
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role="owner")); db.flush()
    project = Project(workspace_id=workspace.id, name=f"Connector project {uuid4().hex[:6]}")
    db.add(project); db.flush()
    db.add(ProjectMember(workspace_id=workspace.id, project_id=project.id, user_id=user.id, role="admin")); db.flush()
    return user, workspace, project


def test_github_webhook_route_requires_signature_and_deduplicates(pg_session_factory, monkeypatch):
    db = pg_session_factory()
    encryption_key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "integration_encryption_key", encryption_key)
    try:
        user, workspace, project = _principal(db)
        integration = Integration(workspace_id=workspace.id, project_id=project.id, provider="github", enabled=True)
        db.add(integration); db.flush()
        integration.credentials_ciphertext = encrypt_credentials({"token": "token", "webhook_secret": "hook-test-secret"})
        db.commit()
        raw = json.dumps({"repository": {"id": 22, "full_name": "acme/api"}, "after": "abcdef0123456789",
            "ref": "refs/heads/main", "commits": [], "head_commit": None}).encode()
        signature = "sha256=" + hmac.new(b"hook-test-secret", raw, hashlib.sha256).hexdigest()
        app.dependency_overrides[get_db] = lambda: db
        client = TestClient(app)
        url = f"/api/v1/webhooks/github/{integration.id}"
        headers = {"X-GitHub-Delivery": "delivery-api-1", "X-GitHub-Event": "push"}
        assert client.post(url, content=raw, headers={**headers, "X-Hub-Signature-256": "sha256=" + "0" * 64}).status_code == 401
        valid_headers = {**headers, "X-Hub-Signature-256": signature}
        first = client.post(url, content=raw, headers=valid_headers)
        second = client.post(url, content=raw, headers=valid_headers)
        assert first.status_code == second.status_code == 202
        assert first.json()["deduplicated"] is False and second.json()["deduplicated"] is True
        assert db.query(GitHubDelivery).filter_by(integration_id=integration.id).count() == 1
    finally:
        app.dependency_overrides.pop(get_db, None)
        db.close()


def test_jira_creation_endpoint_requires_approval_and_is_idempotent(pg_session_factory, monkeypatch):
    db = pg_session_factory()
    encryption_key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "integration_encryption_key", encryption_key)
    try:
        user, workspace, project = _principal(db)
        incident = Incident(workspace_id=workspace.id, project_id=project.id, title="API outage", reporter_id=user.id)
        db.add(incident); db.flush()
        recommendation = Recommendation(workspace_id=workspace.id, project_id=project.id,
            incident_id=incident.id, title="Review connection pool settings", description="Inspect current limits.",
            risk_level="low", expected_benefit="Reduce timeouts", evidence_refs=["evidence-id"],
            required_permissions=["incident:review"], verification_steps=["Check pool metrics"])
        integration = Integration(workspace_id=workspace.id, project_id=project.id, provider="jira", enabled=True,
            config={"base_url": "https://example.atlassian.net"})
        db.add_all([recommendation, integration]); db.flush()
        integration.credentials_ciphertext = encrypt_credentials({"email": "admin@example.test", "api_token": "test-token"})
        db.add(JiraProjectMapping(workspace_id=workspace.id, project_id=project.id,
            integration_id=integration.id, jira_project_key="OPS"))
        approval = ApprovalRequest(workspace_id=workspace.id, project_id=project.id,
            recommendation_id=recommendation.id, requested_by=user.id, action_snapshot={"title": recommendation.title},
            action_hash="a" * 64, status="pending")
        db.add(approval); db.commit()

        class FakeJiraAdapter:
            def __init__(self, *args, **kwargs): self.base_url = "https://example.atlassian.net"
            calls = []
            def create_or_reconcile(self, **kwargs):
                self.calls.append(kwargs)
                return {"id": "1001", "key": "OPS-7", "fields": {"status": {"name": "To Do"}}}

        import app.services.jira as jira_module
        monkeypatch.setattr(jira_module, "JiraAdapter", FakeJiraAdapter)
        monkeypatch.setattr(settings, "frontend_origin", "https://incidentiq.example")
        app.dependency_overrides[get_db] = lambda: db
        client = TestClient(app)
        headers = {"Authorization": "Bearer " + create_token(str(user.id))}
        endpoint = f"/api/v1/recommendations/{recommendation.id}/jira-issue"
        assert client.post(endpoint, headers=headers).status_code == 409
        approval.status = "approved"; db.commit()
        first = client.post(endpoint, headers=headers)
        second = client.post(endpoint, headers=headers)
        assert first.status_code == second.status_code == 200
        assert first.json()["external_issue_key"] == second.json()["external_issue_key"] == "OPS-7"
        assert first.json()["external_issue_id"] == "1001"
        assert first.json()["external_issue_url"] == "https://example.atlassian.net/browse/OPS-7"
        assert len(FakeJiraAdapter.calls) == 1
        create_args = FakeJiraAdapter.calls[0]
        assert create_args["priority"] == "Low"
        assert incident.title in create_args["description"]
        assert str(incident.id) in create_args["description"]
        assert f"https://incidentiq.example/incidents/{incident.id}" in create_args["description"]
        assert recommendation.description in create_args["description"]
        assert "Reduce timeouts" in create_args["description"]
        assert "Check pool metrics" in create_args["description"]
        assert "evidence-id" in create_args["description"]
        assert "test-token" not in json.dumps(first.json())
        assert db.query(ExternalIssueLink).filter_by(recommendation_id=recommendation.id).count() == 1
    finally:
        app.dependency_overrides.pop(get_db, None)
        db.close()
