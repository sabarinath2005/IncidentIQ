"""Phase 4 production hardening and demo verification tests."""
from datetime import datetime, timezone
from hashlib import sha256
from uuid import UUID, uuid4
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.core.security import create_token
from app.db import Base, get_db
from app.main import app
from app.models import (
    ApprovalRequest, EvidenceItem, ExternalIssueLink, Finding, GitHubDelivery,
    GitHubRepositoryMapping, Incident, Integration, InvestigationRun,
    InvestigationStep, JiraProjectMapping, KnowledgeDocument, Project,
    ProjectMember, Recommendation, Service, User, Workspace, WorkspaceMember,
)
from app.schemas import KnowledgeSearchRequest, GitHubSimulatePayload


@pytest.fixture
def test_db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client(test_db):
    def override_get_db():
        try:
            yield test_db
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def seed_context(test_db):
    user = User(email="operator@example.test", full_name="Ops Operator", password_hash="dummy")
    test_db.add(user)
    test_db.flush()

    workspace = Workspace(name="Prod Ops Workspace")
    test_db.add(workspace)
    test_db.flush()

    test_db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role="owner"))

    project = Project(workspace_id=workspace.id, name="Checkout Payments")
    test_db.add(project)
    test_db.flush()

    test_db.add(ProjectMember(workspace_id=workspace.id, project_id=project.id, user_id=user.id, role="admin"))
    test_db.commit()

    token = create_token(str(user.id))
    headers = {"Authorization": f"Bearer {token}"}
    return {"user": user, "workspace": workspace, "project": project, "headers": headers}


def test_seed_demo_scenario_and_reset(client, seed_context, test_db):
    project_id = str(seed_context["project"].id)
    headers = seed_context["headers"]

    # 1. First seeding
    res1 = client.post(f"/api/v1/projects/{project_id}/demo-scenario", headers=headers)
    assert res1.status_code == 201
    data1 = res1.json()
    assert data1["severity"] == "SEV-1"
    assert "Checkout 503" in data1["title"]
    assert data1["demo_scenario_key"] == "checkout-release-regression-v1"

    # Verify associated records were seeded
    incident_id = UUID(data1["id"])
    evidence = test_db.query(EvidenceItem).filter_by(incident_id=incident_id).all()
    assert len(evidence) == 4
    assert any(e.source_type == "application_log" for e in evidence)
    assert any(e.source_type == "deployment_event" for e in evidence)

    # Verify GitHub & Jira integrations were seeded
    github_int = test_db.query(Integration).filter_by(project_id=seed_context["project"].id, provider="github").first()
    assert github_int is not None
    assert github_int.enabled is True

    jira_int = test_db.query(Integration).filter_by(project_id=seed_context["project"].id, provider="jira").first()
    assert jira_int is not None
    assert jira_int.config.get("demo_mode") is True

    # 2. Re-seeding without reset returns current
    res2 = client.post(f"/api/v1/projects/{project_id}/demo-scenario", headers=headers)
    assert res2.status_code == 201
    assert res2.json()["id"] == data1["id"]

    # 3. Reset re-creates scenario cleanly
    res3 = client.post(f"/api/v1/projects/{project_id}/demo-scenario?reset=true", headers=headers)
    assert res3.status_code == 201
    assert res3.json()["id"] != data1["id"]


def test_knowledge_search_keyword_fallback(client, seed_context, test_db, monkeypatch):
    monkeypatch.setattr(settings, "embedding_provider", "disabled")
    project_id = str(seed_context["project"].id)
    headers = seed_context["headers"]

    # Seed a document
    doc = KnowledgeDocument(
        workspace_id=seed_context["workspace"].id,
        project_id=seed_context["project"].id,
        title="PostgreSQL connection pool runbook",
        filename="pg-runbook.md",
        mime_type="text/markdown",
        file_size=120,
        source_ref="manual",
        source_kind="runbook",
        content="Increase connection pool size to 100 when checkout transactions fail with 503 errors.",
        outcome="Resolved connection timeouts",
        content_hash="abc123hash",
        status="indexed",
    )
    test_db.add(doc)
    test_db.commit()

    # Search knowledge
    payload = {"query": "checkout connection pool 503", "limit": 5}
    res = client.post(f"/api/v1/projects/{project_id}/knowledge/search", json=payload, headers=headers)
    assert res.status_code == 200
    hits = res.json()
    assert len(hits) >= 1
    assert hits[0]["document_title"] == "PostgreSQL connection pool runbook"
    assert hits[0]["retrieval_method"] == "keyword_fallback"
    assert hits[0]["similarity_score"] > 0


def test_simulate_github_event(client, seed_context, test_db):
    project_id = str(seed_context["project"].id)
    headers = seed_context["headers"]

    # Setup GitHub integration and repository mapping
    integration = Integration(
        workspace_id=seed_context["workspace"].id,
        project_id=seed_context["project"].id,
        provider="github",
        config={},
        enabled=True,
    )
    test_db.add(integration)
    test_db.flush()

    repo_map = GitHubRepositoryMapping(
        workspace_id=seed_context["workspace"].id,
        project_id=seed_context["project"].id,
        integration_id=integration.id,
        repository_id="998877",
        repository_full_name="incidentiq/checkout-api",
    )
    test_db.add(repo_map)
    test_db.commit()

    # Trigger simulation
    payload = {
        "event_type": "deployment_status",
        "repository_full_name": "incidentiq/checkout-api",
        "summary": "Deployment v2.4.0 succeeded on prod",
    }
    res = client.post(f"/api/v1/projects/{project_id}/github/simulate-event", json=payload, headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "correlated"
    assert data["event_type"] == "deployment_status"
    assert "v2.4.0" in data["summary"]


def test_jira_issue_creation_and_sync_demo_simulation(client, seed_context, test_db):
    project = seed_context["project"]
    headers = seed_context["headers"]

    # Setup incident, recommendation, approval, and demo jira integration
    incident = Incident(
        workspace_id=project.workspace_id,
        project_id=project.id,
        title="High error rate",
        severity="SEV-1",
        status="open",
        reporter_id=seed_context["user"].id,
    )
    test_db.add(incident)
    test_db.flush()

    rec = Recommendation(
        workspace_id=project.workspace_id,
        project_id=project.id,
        incident_id=incident.id,
        title="Rollback release v2.4.0",
        description="Revert container image to v2.3.9",
        risk_level="high",
    )
    test_db.add(rec)
    test_db.flush()

    approval = ApprovalRequest(
        workspace_id=project.workspace_id,
        project_id=project.id,
        recommendation_id=rec.id,
        requested_by=seed_context["user"].id,
        status="approved",
        action_snapshot={"action": "rollback"},
        action_hash="hash123",
    )
    test_db.add(approval)
    test_db.flush()

    integration = Integration(
        workspace_id=project.workspace_id,
        project_id=project.id,
        provider="jira",
        config={"base_url": "https://demo.atlassian.net", "demo_mode": True},
        enabled=True,
    )
    test_db.add(integration)
    test_db.flush()

    mapping = JiraProjectMapping(
        workspace_id=project.workspace_id,
        project_id=project.id,
        integration_id=integration.id,
        jira_project_key="DEMO",
        jira_project_name="Demo Jira",
    )
    test_db.add(mapping)
    test_db.commit()

    # 1. Create Jira Issue
    res_create = client.post(f"/api/v1/recommendations/{rec.id}/jira-issue", headers=headers)
    assert res_create.status_code == 200
    issue_data = res_create.json()
    assert issue_data["status"] == "created"
    assert issue_data["external_issue_key"].startswith("DEMO-")
    assert issue_data["external_status"] == "To Do"
    assert "https://demo.atlassian.net/browse/" in issue_data["external_issue_url"]

    # 2. Sync Jira Status
    link_id = issue_data["id"]
    res_sync = client.post(f"/api/v1/external-issue-links/{link_id}/sync", headers=headers)
    assert res_sync.status_code == 200
    sync_data = res_sync.json()
    assert sync_data["external_status"] == "In Progress"


def test_analytics_summary_phase4_fields(client, seed_context, test_db):
    project_id = str(seed_context["project"].id)
    headers = seed_context["headers"]

    res = client.get(f"/api/v1/analytics/summary?project_id={project_id}", headers=headers)
    assert res.status_code == 200
    data = res.json()
    assert "issues_by_status" in data
    assert "incidents_by_severity" in data
    assert "investigations_by_status" in data
    assert "investigation_total" in data
    assert "approvals_by_status" in data
    assert "approval_total" in data
    assert "knowledge_docs_total" in data
    assert "ai_provider_mode" in data
