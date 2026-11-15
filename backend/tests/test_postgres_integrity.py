"""Optional PostgreSQL integration tests. Set TEST_DATABASE_URL to a disposable DB name ending in _test."""
import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import Base
from tests.pg_test_safety import validated_test_url
from app.models import (
    EvidenceItem, Finding, FindingEvidenceLink, Incident, InvestigationRun,
    InvestigationRunContextReference, InvestigationRunEvidence,
    InvestigationRunSourceStatus, Project, ProjectMember, User, Workspace,
    WorkspaceMember,
)

TEST_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not TEST_URL, reason="Set TEST_DATABASE_URL to a disposable PostgreSQL/pgvector database")


def test_postgres_enforces_scoped_evidence_and_historical_relationships():
    if TEST_URL is None:
        pytest.skip("No TEST_DATABASE_URL")
    try:
        parsed_url = validated_test_url(TEST_URL)
    except ValueError as exc:
        pytest.fail(str(exc), pytrace=False)

    engine = create_engine(parsed_url)
    schema = "it_" + uuid4().hex[:12]
    with engine.connect() as connection:
        outer = connection.begin()
        try:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}", public'))
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            Base.metadata.create_all(connection)
            session = Session(bind=connection, join_transaction_mode="create_savepoint")
            now = datetime.now(timezone.utc)
            user = User(email="test@example.test", full_name="Test User", password_hash="test-hash")
            workspace = Workspace(name="test workspace")
            other_workspace = Workspace(name="other workspace")
            session.add_all([user, workspace, other_workspace])
            session.flush()
            session.add_all([
                WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role="owner"),
                WorkspaceMember(workspace_id=other_workspace.id, user_id=user.id, role="owner"),
            ])
            project = Project(workspace_id=workspace.id, name="test project")
            other_project = Project(workspace_id=other_workspace.id, name="other project")
            session.add_all([project, other_project])
            session.flush()
            session.add_all([
                ProjectMember(workspace_id=workspace.id, project_id=project.id, user_id=user.id, role="admin"),
                ProjectMember(workspace_id=other_workspace.id, project_id=other_project.id, user_id=user.id, role="admin"),
            ])
            session.flush()
            incident = Incident(workspace_id=workspace.id, project_id=project.id, title="current",
                                reporter_id=user.id, started_at=now, detected_at=now)
            other_incident = Incident(workspace_id=workspace.id, project_id=project.id, title="historical",
                                      reporter_id=user.id, started_at=now, detected_at=now,
                                      confirmed_root_cause="known cause")
            session.add_all([incident, other_incident])
            session.flush()
            evidence = EvidenceItem(workspace_id=workspace.id, project_id=project.id, incident_id=incident.id,
                                    source_type="log", summary="observed error", content_hash="a" * 64)
            historical_evidence = EvidenceItem(workspace_id=workspace.id, project_id=project.id,
                incident_id=other_incident.id, source_type="log", summary="old error", content_hash="b" * 64)
            session.add_all([evidence, historical_evidence])
            session.flush()
            run = InvestigationRun(workspace_id=workspace.id, project_id=project.id, incident_id=incident.id,
                requested_by=user.id, idempotency_key="run-current", thread_id="thread-current", status="running")
            other_run = InvestigationRun(workspace_id=workspace.id, project_id=project.id, incident_id=other_incident.id,
                requested_by=user.id, idempotency_key="run-history", thread_id="thread-history", status="running")
            session.add_all([run, other_run])
            session.flush()
            current_link = InvestigationRunEvidence(workspace_id=workspace.id, project_id=project.id,
                incident_id=incident.id, run_id=run.id, evidence_id=evidence.id, usage_type="preexisting_attached")
            historical_link = InvestigationRunEvidence(workspace_id=workspace.id, project_id=project.id,
                incident_id=other_incident.id, run_id=other_run.id, evidence_id=historical_evidence.id,
                usage_type="preexisting_attached")
            session.add_all([current_link, historical_link])
            finding = Finding(workspace_id=workspace.id, project_id=project.id, incident_id=incident.id,
                run_id=run.id, rank=1, hypothesis="current incident cause", confidence_score=0.5, rationale="uncertain")
            session.add(finding)
            session.flush()
            session.add(FindingEvidenceLink(workspace_id=workspace.id, project_id=project.id,
                incident_id=incident.id, run_id=run.id, finding_id=finding.id, evidence_id=evidence.id,
                stance="supports"))
            gap = InvestigationRunSourceStatus(workspace_id=workspace.id, project_id=project.id,
                incident_id=incident.id, run_id=run.id, source_type="traces", source_instance_ref="none",
                query_fingerprint="trace-gap", status="unavailable", reason="connector is not configured")
            session.add(gap)
            session.flush()
            assert session.query(EvidenceItem).filter_by(incident_id=incident.id).count() == 1

            with pytest.raises(IntegrityError):
                session.add(FindingEvidenceLink(workspace_id=workspace.id, project_id=project.id,
                    incident_id=incident.id, run_id=run.id, finding_id=finding.id,
                    evidence_id=historical_evidence.id, stance="context"))
                session.flush()
            session.rollback()

            with pytest.raises(IntegrityError):
                session.add(InvestigationRunContextReference(
                    workspace_id=workspace.id, project_id=project.id, incident_id=incident.id, run_id=run.id,
                    origin_workspace_id=other_workspace.id, origin_project_id=other_project.id,
                    source_kind="document", source_document_id=uuid4(), retrieved_at=now,
                    retrieval_rank=1, retrieval_score=0.4, retrieval_method="test",
                    source_content_hash="c" * 64))
                session.flush()
            session.rollback()

            with pytest.raises(IntegrityError):
                session.add(InvestigationRun(workspace_id=workspace.id, project_id=project.id,
                    incident_id=incident.id, requested_by=user.id, idempotency_key="second-active",
                    thread_id="thread-current-2", status="queued"))
                session.flush()
            session.rollback()
            session.close()
        finally:
            outer.rollback()
    engine.dispose()
