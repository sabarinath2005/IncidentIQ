from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
import httpx
from langgraph.checkpoint.memory import InMemorySaver

import app.main as main
from app.models import (
    EvidenceItem, Finding, FindingEvidenceLink, Incident, InvestigationRun,
    InvestigationRunAttempt, InvestigationRunEvidence, InvestigationStep,
    InvestigationStepAttempt, OutboxEvent, Project, ProjectMember, User,
    Workspace, WorkspaceMember, Recommendation, KnowledgeDocument, DocumentChunk,
)
from app.services.investigations import _persist_graph_result, _step, claim_run, execute_run, record_execution_failure
from app.run_lifecycle import transition_run


def make_project(session):
    user = User(email=f"review-{uuid4()}@example.test", full_name="Reviewer", password_hash="test")
    workspace = Workspace(name=f"Workspace {uuid4()}")
    session.add_all([user, workspace])
    session.flush()
    session.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role="owner"))
    project = Project(workspace_id=workspace.id, name=f"Project {uuid4()}")
    session.add(project)
    session.flush()
    session.add(ProjectMember(workspace_id=workspace.id, project_id=project.id, user_id=user.id, role="admin"))
    session.flush()
    return workspace, project, user


def make_incident(session, workspace, project, user, title="Incident"):
    incident = Incident(workspace_id=workspace.id, project_id=project.id, title=f"{title} {uuid4()}",
        reporter_id=user.id, started_at=datetime.now(timezone.utc), detected_at=datetime.now(timezone.utc))
    session.add(incident)
    session.flush()
    return incident


def make_run(session, incident, user, status="queued", key=None):
    run = InvestigationRun(workspace_id=incident.workspace_id, project_id=incident.project_id,
        incident_id=incident.id, requested_by=user.id, idempotency_key=key or str(uuid4()),
        thread_id=f"thread-{uuid4()}", status=status)
    session.add(run)
    session.flush()
    return run


def allow_queue(monkeypatch):
    import app.workers as workers
    monkeypatch.setattr(main.settings, "task_mode", "celery")
    monkeypatch.setattr(workers.dispatch_outbox_event, "delay", lambda event_id: None)


def test_execution_waits_for_review_then_completes_and_allows_next_run(pg_session_factory, monkeypatch):
    allow_queue(monkeypatch)
    db = pg_session_factory()
    try:
        workspace, project, user = make_project(db)
        incident = make_incident(db, workspace, project, user)
        run = make_run(db, incident, user, status="queued", key="first-review-run")
        summary = "ERROR checkout service returned 503 after release 2.4.0"
        db.add(EvidenceItem(workspace_id=workspace.id, project_id=project.id, incident_id=incident.id,
            source_type="log", summary=summary, content_hash="a" * 64))
        db.commit()

        assert execute_run(str(run.id), worker_id="test-runner", session_factory=pg_session_factory,
                           checkpointer=InMemorySaver())
        db.refresh(run)
        assert run.status == "waiting_for_review"
        finding = db.query(Finding).filter_by(run_id=run.id).one()

        reviewed = main.review_finding(finding.id, main.ReviewDecision(status="confirmed"), user, db)
        assert reviewed["review_status"] == "confirmed"
        db.refresh(run)
        assert run.status == "completed"

        created = main.start_investigation(incident.id, "second-run-key", user, db)
        assert created["status"] == "queued"
        assert db.query(InvestigationRun).filter_by(incident_id=incident.id).count() == 2
    finally:
        db.close()


def test_failed_and_cancelled_runs_release_active_run_constraint(pg_session_factory, monkeypatch):
    allow_queue(monkeypatch)
    db = pg_session_factory()
    try:
        workspace, project, user = make_project(db)
        for status in ("failed", "cancelled"):
            incident = make_incident(db, workspace, project, user, status)
            make_run(db, incident, user, status=status, key=f"terminal-{status}")
            result = main.start_investigation(incident.id, f"new-{status}-run", user, db)
            assert result["status"] == "queued"
    finally:
        db.close()


def test_same_idempotency_key_replays_same_incident_and_conflicts_across_incidents(pg_session_factory, monkeypatch):
    allow_queue(monkeypatch)
    db = pg_session_factory()
    try:
        workspace, project, user = make_project(db)
        first = make_incident(db, workspace, project, user, "First")
        second = make_incident(db, workspace, project, user, "Second")
        one = main.start_investigation(first.id, "same-project-key", user, db)
        same = main.start_investigation(first.id, "same-project-key", user, db)
        assert one["id"] == same["id"] and same["deduplicated"] is True
        with pytest.raises(HTTPException) as error:
            main.start_investigation(second.id, "same-project-key", user, db)
        assert error.value.status_code == 409
        assert db.query(OutboxEvent).filter(OutboxEvent.event_type == "investigation.requested").count() == 1
    finally:
        db.close()


def test_concurrent_run_creation_returns_one_active_run_and_one_outbox(pg_session_factory, monkeypatch):
    allow_queue(monkeypatch)
    setup = pg_session_factory()
    try:
        workspace, project, user = make_project(setup)
        incident = make_incident(setup, workspace, project, user)
        incident_id, user_id = incident.id, user.id
        setup.commit()
    finally:
        setup.close()

    def create(index):
        db = pg_session_factory()
        try:
            user = db.get(User, user_id)
            return main.start_investigation(incident_id, f"concurrent-key-{index}", user, db)["id"]
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        run_ids = list(pool.map(create, (1, 2)))
    check = pg_session_factory()
    try:
        assert run_ids[0] == run_ids[1]
        assert check.query(InvestigationRun).filter_by(incident_id=incident_id).count() == 1
        assert check.query(OutboxEvent).filter(OutboxEvent.event_type == "investigation.requested").count() == 1
    finally:
        check.close()


def test_pgvector_retrieval_is_project_scoped_and_persists_chunk_provenance(pg_session_factory, monkeypatch):
    import app.services.retrieval as retrieval
    from app.core.config import settings

    monkeypatch.setattr(settings, "embedding_provider", "openai")
    query_vector = [1.0] + [0.0] * 1535
    monkeypatch.setattr(retrieval, "embed_texts", lambda values: ([query_vector], settings.openai_embedding_model))
    session = pg_session_factory()
    try:
        workspace, project, user = make_project(session)
        incident = make_incident(session, workspace, project, user, "Checkout timeout")
        run = make_run(session, incident, user, status="running")
        doc = KnowledgeDocument(workspace_id=workspace.id, project_id=project.id,
            title="Checkout runbook", source_kind="runbook", content_hash="b" * 64,
            content="Inspect connection pool saturation.", status="indexed", version=3)
        session.add(doc)
        session.flush()
        chunks = [DocumentChunk(workspace_id=workspace.id, project_id=project.id,
            document_id=doc.id, chunk_index=index, text=text,
            embedding=[0.1] * 64, embedding_model="demo-hash-v1",
            embedding_1536=query_vector, embedding_1536_model="text-embedding-3-small",
            embedding_1536_dimensions=1536)
            for index, text in ((2, "Inspect connection pool saturation."), (3, "Compare checkout timeout thresholds."))]
        session.add_all(chunks)

        other_workspace, other_project, other_user = make_project(session)
        other_incident = make_incident(session, other_workspace, other_project, other_user, "Checkout timeout")
        other_doc = KnowledgeDocument(workspace_id=other_workspace.id, project_id=other_project.id,
            title="Other project runbook", source_kind="runbook", content_hash="c" * 64,
            content="Private source text.", status="indexed")
        session.add(other_doc)
        session.flush()
        session.add(DocumentChunk(workspace_id=other_workspace.id, project_id=other_project.id,
            document_id=other_doc.id, chunk_index=0, text="Private source text.",
            embedding=[0.1] * 64, embedding_model="demo-hash-v1",
            embedding_1536=query_vector, embedding_1536_model="text-embedding-3-small",
            embedding_1536_dimensions=1536))
        session.flush()

        _, contexts, status = retrieval.retrieve_context(session, run, incident, [])
        assert status["status"] == "completed" and status["result_count"] == 2
        assert len(contexts) == 2
        assert {item["source_document_id"] for item in contexts} == {str(doc.id)}
        assert {item["source_chunk_id"] for item in contexts} == {str(chunk.id) for chunk in chunks}
        assert {item["version"] for item in contexts} == {3}
        references = session.query(main.InvestigationRunContextReference).filter_by(run_id=run.id).all()
        assert len(references) == 2
        assert all(reference.origin_workspace_id == workspace.id for reference in references)
        assert {reference.source_document_version for reference in references} == {3}
        assert {reference.source_chunk_index for reference in references} == {2, 3}
        assert all(reference.source_excerpt and reference.source_timestamp for reference in references)
    finally:
        session.close()


def test_concurrent_claim_and_duplicate_delivery_are_safe(pg_session_factory):
    db = pg_session_factory()
    try:
        workspace, project, user = make_project(db)
        incident = make_incident(db, workspace, project, user)
        run = make_run(db, incident, user)
        run_id = str(run.id)
        db.commit()
    finally:
        db.close()

    def claim(owner):
        return claim_run(run_id, owner, session_factory=pg_session_factory)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(claim, ("worker-one", "worker-two")))
    assert sum(claimed) == 1

    check = pg_session_factory()
    try:
        run = check.get(InvestigationRun, run.id)
        attempt = check.query(InvestigationRunAttempt).filter_by(run_id=run.id).one()
        transition_run(run, "waiting_for_review")
        transition_run(run, "completed")
        run.lease_owner = None
        run.lease_expires_at = None
        attempt.status = "succeeded"
        attempt.completed_at = datetime.now(timezone.utc)
        check.commit()
    finally:
        check.close()
    assert not claim_run(run_id, "duplicate-after-completion", session_factory=pg_session_factory)


def test_expired_lease_is_reclaimed_and_attempt_history_is_preserved(pg_session_factory, monkeypatch):
    monkeypatch.setattr(main.settings, "investigation_lease_seconds", 30)
    db = pg_session_factory()
    try:
        workspace, project, user = make_project(db)
        incident = make_incident(db, workspace, project, user)
        run = make_run(db, incident, user)
        run_id = str(run.id)
        db.commit()
    finally:
        db.close()

    now = datetime.now(timezone.utc)
    assert claim_run(run_id, "worker-expired", session_factory=pg_session_factory, now=now)
    assert claim_run(run_id, "worker-recovered", session_factory=pg_session_factory, now=now + timedelta(seconds=31))
    check = pg_session_factory()
    try:
        attempts = check.query(InvestigationRunAttempt).filter_by(run_id=UUID(run_id)).order_by(
            InvestigationRunAttempt.attempt_no).all()
        assert [row.status for row in attempts] == ["lease_expired", "running"]
        run = check.get(InvestigationRun, UUID(run_id))
        assert run.status == "running" and run.lease_owner == "worker-recovered"
        assert run.retry_count == 1
    finally:
        check.close()


def test_transient_failures_retry_same_run_then_fail_at_bounded_limit(pg_session_factory, monkeypatch):
    monkeypatch.setattr(main.settings, "investigation_max_retries", 2)
    db = pg_session_factory()
    try:
        workspace, project, user = make_project(db)
        incident = make_incident(db, workspace, project, user)
        run = make_run(db, incident, user)
        run_id = str(run.id)
        db.commit()
    finally:
        db.close()

    request = httpx.Request("GET", "https://provider.example.test")
    error = httpx.ConnectError("temporary", request=request)
    for number in (1, 2):
        owner = f"attempt-{number}"
        assert claim_run(run_id, owner, session_factory=pg_session_factory)
        assert record_execution_failure(run_id, owner, error, retryable=True, session_factory=pg_session_factory)

    owner = "final-attempt"
    assert claim_run(run_id, owner, session_factory=pg_session_factory)
    assert not record_execution_failure(run_id, owner, error, retryable=True, session_factory=pg_session_factory)
    check = pg_session_factory()
    try:
        run = check.get(InvestigationRun, UUID(run_id))
        assert run.status == "failed" and run.retry_count == 2
        assert check.query(InvestigationRunAttempt).filter_by(run_id=run.id).count() == 3
        assert check.query(OutboxEvent).filter_by(aggregate_id=run.id, event_type="investigation.retry").count() == 2
    finally:
        check.close()


def test_permanent_failure_is_terminal_without_retry_event(pg_session_factory):
    db = pg_session_factory()
    try:
        workspace, project, user = make_project(db)
        incident = make_incident(db, workspace, project, user)
        run = make_run(db, incident, user)
        run_id = str(run.id)
        db.commit()
    finally:
        db.close()

    owner = "permanent-error-worker"
    assert claim_run(run_id, owner, session_factory=pg_session_factory)
    assert not record_execution_failure(run_id, owner, ValueError("invalid provider response"),
        retryable=False, session_factory=pg_session_factory)
    check = pg_session_factory()
    try:
        run = check.get(InvestigationRun, UUID(run_id))
        attempt = check.query(InvestigationRunAttempt).filter_by(run_id=run.id).one()
        assert run.status == "failed"
        assert attempt.status == "failed" and attempt.error_type == "ValueError"
        assert check.query(OutboxEvent).filter_by(aggregate_id=run.id, event_type="investigation.retry").count() == 0
    finally:
        check.close()


def test_step_attempt_history_and_latest_success_are_consistent(pg_session_factory):
    db = pg_session_factory()
    try:
        workspace, project, user = make_project(db)
        incident = make_incident(db, workspace, project, user)
        run = make_run(db, incident, user, status="running")
        _step(db, run, "normalize", "running")
        _step(db, run, "normalize", "failed", error="first try failed")
        _step(db, run, "normalize", "running")
        _step(db, run, "normalize", "succeeded", output={"normalized": True})
        step = db.query(InvestigationStep).filter_by(run_id=run.id, step_key="normalize").one()
        attempts = db.query(InvestigationStepAttempt).filter_by(run_id=run.id, step_key="normalize").order_by(
            InvestigationStepAttempt.attempt_no).all()
        assert step.latest_attempt_no == step.latest_successful_attempt_no == 2
        assert [item.status for item in attempts] == ["failed", "succeeded"]
    finally:
        db.close()


def test_result_persistence_is_idempotent_for_findings_citations_and_recommendations(pg_session_factory):
    db = pg_session_factory()
    try:
        workspace, project, user = make_project(db)
        incident = make_incident(db, workspace, project, user)
        run = make_run(db, incident, user, status="running")
        evidence = EvidenceItem(workspace_id=workspace.id, project_id=project.id, incident_id=incident.id,
            source_type="log", summary="observed error", content_hash="f" * 64)
        db.add(evidence)
        db.flush()
        db.add(InvestigationRunEvidence(workspace_id=workspace.id, project_id=project.id,
            incident_id=incident.id, run_id=run.id, evidence_id=evidence.id,
            usage_type="preexisting_attached", collection_status="available"))
        db.flush()
        result = {"hypotheses": [{"hypothesis": "Candidate", "confidence_score": 0.7,
            "confidence_method": "test", "uncertainty": "test", "support": [{"id": str(evidence.id)}],
            "contradict": [], "context": []}], "historical_context": [],
            "recommendations": [{"title": "Review", "description": "Compare signals", "risk_level": "low"}]}
        _persist_graph_result(db, run, result)
        db.commit()
        _persist_graph_result(db, run, result)
        db.commit()
        assert db.query(Finding).filter_by(run_id=run.id).count() == 1
        assert db.query(FindingEvidenceLink).filter_by(run_id=run.id).count() == 1
        assert db.query(Recommendation).filter_by(run_id=run.id).count() == 1
    finally:
        db.close()


def test_incident_resolution_cancels_only_its_active_runs(pg_session_factory, monkeypatch):
    import app.workers as workers
    monkeypatch.setattr(main.settings, "task_mode", "celery")
    monkeypatch.setattr(workers.dispatch_outbox_event, "delay", lambda event_id: None)
    db = pg_session_factory()
    try:
        workspace, project, user = make_project(db)
        incident = make_incident(db, workspace, project, user, "Target")
        unrelated = make_incident(db, workspace, project, user, "Unrelated")
        target_run = make_run(db, incident, user, status="waiting_for_review")
        unrelated_run = make_run(db, unrelated, user, status="waiting_for_review")
        db.commit()

        main.resolve_incident(incident.id, {"resolution_notes": "fixed"}, user, db)
        db.refresh(target_run)
        db.refresh(unrelated_run)
        assert target_run.status == "cancelled"
        assert unrelated_run.status == "waiting_for_review"
        with pytest.raises(HTTPException) as error:
            main.start_investigation(incident.id, "resolved-incident-key", user, db)
        assert error.value.status_code == 409
    finally:
        db.close()


def test_concurrent_review_actions_allow_only_one_terminal_decision(pg_session_factory):
    db = pg_session_factory()
    try:
        workspace, project, user = make_project(db)
        incident = make_incident(db, workspace, project, user)
        run = make_run(db, incident, user, status="waiting_for_review")
        finding = Finding(workspace_id=workspace.id, project_id=project.id, incident_id=incident.id,
            run_id=run.id, rank=1, hypothesis="Candidate", confidence_score=0.5, rationale="Review")
        db.add(finding)
        db.commit()
        finding_id, user_id = finding.id, user.id
    finally:
        db.close()

    def decide(status):
        session = pg_session_factory()
        try:
            user = session.get(User, user_id)
            try:
                main.review_finding(finding_id, main.ReviewDecision(status=status), user, session)
                return "accepted"
            except HTTPException as exc:
                return exc.status_code
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(decide, ("confirmed", "rejected")))
    assert results.count("accepted") == 1
    assert results.count(409) == 1
    check = pg_session_factory()
    try:
        run = check.get(InvestigationRun, run.id)
        finding = check.get(Finding, finding_id)
        assert run.status == "completed"
        assert finding.review_status in {"confirmed", "rejected"}
    finally:
        check.close()
