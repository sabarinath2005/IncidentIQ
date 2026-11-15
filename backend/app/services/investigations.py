"""Claimed, leased, resumable investigation execution and retry persistence."""
from datetime import datetime, timedelta, timezone
from contextlib import nullcontext
from hashlib import sha256
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select, update, func
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from app.ai import build_graph
from app.core.config import settings
from app.db import SessionLocal
from app.models import (
    AuditEvent, EvidenceItem, Finding, FindingContextLink, FindingEvidenceLink,
    Incident, InvestigationRun, InvestigationRunAttempt, InvestigationRunContextReference,
    InvestigationRunEvidence, InvestigationRunSourceStatus, InvestigationStep,
    InvestigationStepAttempt, OutboxEvent, Recommendation, Integration, IntegrationSyncRun,
)
from app.run_lifecycle import InvalidRunTransition, RUN_ACTIVE_STATUSES, transition_run

GRAPH_STEP_KEYS = ("normalize", "retrieve", "generate", "verify", "rank", "recommend")


def is_retryable_error(exc: Exception) -> bool:
    """Retry network transport/temporary provider errors and transient DB failures only."""
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {408, 425, 429} or exc.response.status_code >= 500
    if isinstance(exc, OperationalError):
        original = getattr(exc, "orig", None)
        sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
        transient_states = {"40001", "40P01", "55P03", "57P01", "57P02", "57P03", "53300"}
        if exc.connection_invalidated or (sqlstate and (sqlstate.startswith("08") or sqlstate in transient_states)):
            return True
        message = str(original or exc).lower()
        return not sqlstate and any(term in message for term in (
            "connection refused", "could not connect", "connection timed out", "server closed the connection",
            "network is unreachable", "temporary failure in name resolution",
        ))
    if isinstance(exc, DBAPIError):
        original = getattr(exc, "orig", None)
        sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
        transient_states = {"40001", "40P01", "55P03", "57P01", "57P02", "57P03", "53300"}
        return bool(exc.connection_invalidated or (sqlstate and (sqlstate.startswith("08") or sqlstate in transient_states)))
    return False


def _attempt_for_owner(db: Session, run: InvestigationRun, owner: str):
    return db.query(InvestigationRunAttempt).filter_by(run_id=run.id, worker_id=owner).order_by(
        InvestigationRunAttempt.attempt_no.desc()).first()


def _close_open_steps(db: Session, run: InvestigationRun, reason: str, final_status: str):
    steps = db.query(InvestigationStep).filter_by(run_id=run.id).all()
    for step in steps:
        if step.status == "running" or (step.status == "failed" and final_status == "retrying"):
            attempt = db.query(InvestigationStepAttempt).filter_by(
                run_id=run.id, step_key=step.step_key, attempt_no=step.latest_attempt_no).one_or_none()
            if attempt and attempt.status == "running":
                attempt.status = "failed" if final_status in {"retrying", "failed"} else "cancelled"
                attempt.error = reason[:2000]
                attempt.completed_at = datetime.now(timezone.utc)
            step.status = "retrying" if final_status == "retrying" else final_status
        elif step.status == "pending" and final_status in {"failed", "cancelled"}:
            step.status = "cancelled"


def _enqueue_run(db: Session, run: InvestigationRun, *, retry_no: int, delay_seconds: int = 0):
    now = datetime.now(timezone.utc)
    event = OutboxEvent(
        workspace_id=run.workspace_id, project_id=run.project_id,
        aggregate_type="investigation_run", aggregate_id=run.id,
        event_type="investigation.retry" if retry_no else "investigation.requested",
        payload={"run_id": str(run.id), "retry_no": retry_no},
        idempotency_key=f"investigation:{run.id}:attempt:{retry_no}",
        status="pending", available_at=now + timedelta(seconds=delay_seconds),
    )
    db.add(event)


def _expire_previous_claim(db: Session, run: InvestigationRun, now: datetime):
    reason = "Worker lease expired; this attempt will be replayed from its checkpoint."
    previous = db.query(InvestigationRunAttempt).filter_by(run_id=run.id, worker_id=run.lease_owner).order_by(
        InvestigationRunAttempt.attempt_no.desc()).first()
    if previous and previous.status == "running":
        previous.status = "lease_expired"
        previous.error_type = "WorkerLeaseExpired"
        previous.error_message = reason
        previous.completed_at = now
    _close_open_steps(db, run, reason, "retrying")
    run.error_summary = reason
    db.add(AuditEvent(workspace_id=run.workspace_id, project_id=run.project_id,
        actor_id=None, action="investigation.lease_expired", resource_type="investigation_run",
        resource_id=run.id, detail={"previous_worker_id": run.lease_owner}))


def claim_run(run_id: str, worker_id: str, *, celery_task_id: str = "", session_factory: sessionmaker = SessionLocal,
              now: datetime | None = None) -> bool:
    """Atomically claim a queued run or reclaim one whose lease expired."""
    now = now or datetime.now(timezone.utc)
    db = session_factory()
    try:
        run = db.execute(select(InvestigationRun).where(
            InvestigationRun.id == UUID(run_id)
        ).with_for_update()).scalar_one_or_none()
        if run is None or run.status in {"waiting_for_review", "completed", "failed", "cancelled"}:
            return False
        if run.status == "running":
            if run.lease_expires_at is not None and run.lease_expires_at > now:
                return False
            _expire_previous_claim(db, run, now)
            run.retry_count += 1
        elif run.status != "queued":
            return False

        if run.retry_count > settings.investigation_max_retries:
            transition_run(run, "failed")
            run.error_summary = "Maximum worker lease recoveries exceeded."
            run.lease_owner = None
            run.lease_expires_at = None
            _close_open_steps(db, run, run.error_summary, "failed")
            db.commit()
            return False

        transition_run(run, "running")
        run.lease_owner = worker_id
        run.lease_expires_at = now + timedelta(seconds=settings.investigation_lease_seconds)
        run.started_at = run.started_at or now
        attempt_no = (db.query(func.max(InvestigationRunAttempt.attempt_no)).filter_by(run_id=run.id).scalar() or 0) + 1
        db.add(InvestigationRunAttempt(
            workspace_id=run.workspace_id, project_id=run.project_id, incident_id=run.incident_id,
            run_id=run.id, attempt_no=attempt_no, worker_id=worker_id, celery_task_id=celery_task_id,
            status="running", started_at=now,
        ))
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def record_execution_failure(run_id: str, worker_id: str, exc: Exception, *, retryable: bool,
                             session_factory: sessionmaker = SessionLocal) -> bool:
    """Persist a failed attempt; retry the same run through a delayed outbox event."""
    db = session_factory()
    try:
        run = db.execute(select(InvestigationRun).where(
            InvestigationRun.id == UUID(run_id)
        ).with_for_update()).scalar_one_or_none()
        if run is None:
            return False
        if run.status == "queued" and run.lease_owner is None and retryable:
            pending_retry = db.query(OutboxEvent).filter_by(
                aggregate_id=run.id, event_type="investigation.retry", status="pending").first()
            if pending_retry:
                return True
            if run.retry_count >= settings.investigation_max_retries:
                transition_run(run, "failed")
                run.error_summary = str(exc)[:3000]
                db.add(AuditEvent(workspace_id=run.workspace_id, project_id=run.project_id,
                    actor_id=None, action="investigation.failed", resource_type="investigation_run",
                    resource_id=run.id, detail={"error_type": type(exc).__name__, "retryable": True,
                                                 "retry_count": run.retry_count}))
                db.commit()
                return False
            run.retry_count += 1
            run.error_summary = str(exc)[:3000]
            delay = min(300, 2 ** min(run.retry_count, 8))
            _enqueue_run(db, run, retry_no=run.retry_count, delay_seconds=delay)
            db.add(AuditEvent(workspace_id=run.workspace_id, project_id=run.project_id,
                actor_id=None, action="investigation.retry_scheduled", resource_type="investigation_run",
                resource_id=run.id, detail={"error_type": type(exc).__name__, "retryable": True,
                                             "retry_count": run.retry_count, "claim_failed": True}))
            db.commit()
            return True
        if run.status != "running" or run.lease_owner != worker_id:
            return False
        attempt = _attempt_for_owner(db, run, worker_id)
        exhausted = run.retry_count >= settings.investigation_max_retries
        will_retry = retryable and not exhausted
        message = str(exc)[:3000]
        if attempt:
            attempt.status = "retrying" if will_retry else "failed"
            attempt.error_type = type(exc).__name__
            attempt.error_message = message
            attempt.completed_at = datetime.now(timezone.utc)
        _close_open_steps(db, run, message, "retrying" if will_retry else "failed")
        run.error_summary = message
        run.lease_owner = None
        run.lease_expires_at = None
        if will_retry:
            run.retry_count += 1
            transition_run(run, "queued")
            delay = min(300, 2 ** min(run.retry_count, 8))
            _enqueue_run(db, run, retry_no=run.retry_count, delay_seconds=delay)
            action = "investigation.retry_scheduled"
        else:
            transition_run(run, "failed")
            action = "investigation.failed"
        db.add(AuditEvent(workspace_id=run.workspace_id, project_id=run.project_id,
            actor_id=None, action=action, resource_type="investigation_run", resource_id=run.id,
            detail={"error_type": type(exc).__name__, "retryable": retryable, "retry_count": run.retry_count}))
        db.commit()
        return will_retry
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def recover_stale_runs(*, session_factory: sessionmaker = SessionLocal, now: datetime | None = None, batch_size: int = 50):
    """Lease watchdog: recover orphaned running jobs and enqueue them durably."""
    now = now or datetime.now(timezone.utc)
    recovered = 0
    db = session_factory()
    try:
        ids = [row[0] for row in db.execute(select(InvestigationRun.id).where(
            InvestigationRun.status == "running",
            (InvestigationRun.lease_expires_at.is_(None) | (InvestigationRun.lease_expires_at <= now)),
        ).order_by(InvestigationRun.lease_expires_at).limit(batch_size)).all()]
    finally:
        db.close()
    for run_id in ids:
        db = session_factory()
        try:
            run = db.execute(select(InvestigationRun).where(InvestigationRun.id == run_id).with_for_update(skip_locked=True)).scalar_one_or_none()
            if run is None or run.status != "running" or (run.lease_expires_at is not None and run.lease_expires_at > now):
                continue
            _expire_previous_claim(db, run, now)
            run.retry_count += 1
            run.lease_owner = None
            run.lease_expires_at = None
            if run.retry_count > settings.investigation_max_retries:
                transition_run(run, "failed")
                run.error_summary = "Maximum worker lease recoveries exceeded."
                _close_open_steps(db, run, run.error_summary, "failed")
                terminal = "failed"
            else:
                transition_run(run, "queued")
                run.error_summary = "Worker lease expired; recovery has been queued."
                _enqueue_run(db, run, retry_no=run.retry_count)
                terminal = "queued"
            db.add(AuditEvent(workspace_id=run.workspace_id, project_id=run.project_id,
                actor_id=None, action="investigation.lease_recovered", resource_type="investigation_run",
                resource_id=run.id, detail={"retry_count": run.retry_count, "resulting_status": terminal}))
            db.commit()
            recovered += 1
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    return recovered


def cancel_active_runs(db: Session, incident: Incident, actor_id, reason: str):
    """Cancel only active runs belonging to the incident being resolved."""
    rows = db.execute(select(InvestigationRun).where(
        InvestigationRun.incident_id == incident.id,
        InvestigationRun.status.in_(RUN_ACTIVE_STATUSES),
    ).with_for_update()).scalars().all()
    for run in rows:
        owner = run.lease_owner
        transition_run(run, "cancelled")
        run.lease_owner = None
        run.lease_expires_at = None
        _close_open_steps(db, run, reason, "cancelled")
        if owner:
            attempt = _attempt_for_owner(db, run, owner)
            if attempt and attempt.status == "running":
                attempt.status = "cancelled"
                attempt.error_type = "IncidentResolved"
                attempt.error_message = reason[:2000]
                attempt.completed_at = datetime.now(timezone.utc)
        db.add(AuditEvent(workspace_id=run.workspace_id, project_id=run.project_id,
            actor_id=actor_id, action="investigation.cancelled", resource_type="investigation_run",
            resource_id=run.id, detail={"reason": reason, "incident_id": str(incident.id)}))
    return rows


def _step(db: Session, run: InvestigationRun, key: str, status: str, output=None, error=""):
    step = db.query(InvestigationStep).filter_by(run_id=run.id, step_key=key).one_or_none()
    if step is None:
        step = InvestigationStep(run_id=run.id, step_key=key, status="pending")
        db.add(step)
        db.flush()
    if status == "running":
        if step.latest_attempt_no is not None:
            previous = db.query(InvestigationStepAttempt).filter_by(
                run_id=run.id, step_key=key, attempt_no=step.latest_attempt_no).one_or_none()
            if previous and previous.status == "running":
                previous.status = "failed"
                previous.error = "Previous worker ended before the step completed; replaying from checkpoint."
                previous.completed_at = datetime.now(timezone.utc)
        attempt_no = (step.latest_attempt_no or 0) + 1
        attempt = InvestigationStepAttempt(run_id=run.id, step_key=key, attempt_no=attempt_no,
            status="running", started_at=datetime.now(timezone.utc))
        db.add(attempt)
        # The step pointer has an immediate composite FK to this row. Insert the
        # attempt first in the same transaction; commit below keeps both atomic.
        db.flush([attempt])
        step.latest_attempt_no = attempt_no
    else:
        attempt = db.query(InvestigationStepAttempt).filter_by(
            run_id=run.id, step_key=key, attempt_no=step.latest_attempt_no).one_or_none()
        if attempt is None or attempt.status != "running":
            return
        attempt.status = status
        attempt.output_json = output or {}
        attempt.error = error[:2000]
        attempt.completed_at = datetime.now(timezone.utc)
        if status == "succeeded":
            step.latest_successful_attempt_no = attempt.attempt_no
    step.status = status
    step.updated_at = datetime.now(timezone.utc)
    db.commit()


def _owns_lease(db: Session, run_id, worker_id: str) -> bool:
    run = db.execute(select(InvestigationRun.status, InvestigationRun.lease_owner).where(
        InvestigationRun.id == run_id
    )).one_or_none()
    return bool(run and run.status == "running" and run.lease_owner == worker_id)


def _persist_graph_result(db: Session, run: InvestigationRun, result: dict):
    provider_metadata = result.get("provider_metadata") or {}
    if provider_metadata:
        run.provider_name = provider_metadata.get("provider", "unknown")[:40]
        run.provider_mode = provider_metadata.get("mode", "unknown")[:24]
        run.model_id = provider_metadata.get("model", run.model_id)[:120]
        run.model_version = provider_metadata.get("model_version", "")[:160]
        run.prompt_version = provider_metadata.get("prompt_version", run.prompt_version)[:40]
        run.model_duration_ms = provider_metadata.get("duration_ms")
        run.input_tokens = provider_metadata.get("input_tokens")
        run.output_tokens = provider_metadata.get("output_tokens")
        run.estimated_cost_usd = provider_metadata.get("estimated_cost_usd")
    for rank, item in enumerate(result.get("hypotheses", []), 1):
        finding = db.query(Finding).filter_by(run_id=run.id, rank=rank).one_or_none()
        if finding is None:
            finding = Finding(workspace_id=run.workspace_id, project_id=run.project_id,
                incident_id=run.incident_id, run_id=run.id, rank=rank,
                hypothesis=item["hypothesis"], confidence_score=item["confidence_score"],
                confidence_method=item["confidence_method"], rationale=item["uncertainty"],
                details_json={key: item.get(key) for key in ("explanation", "suspected_root_cause", "affected_service",
                    "missing_evidence", "verification_steps", "risk_assessment")}, review_status="pending")
            db.add(finding)
            db.flush()
        else:
            finding.hypothesis = item["hypothesis"]
            finding.confidence_score = item["confidence_score"]
            finding.confidence_method = item["confidence_method"]
            finding.rationale = item["uncertainty"]
            finding.details_json = {key: item.get(key) for key in ("explanation", "suspected_root_cause", "affected_service",
                "missing_evidence", "verification_steps", "risk_assessment")}
        for stance, citations in (("supports", item.get("support", [])), ("contradicts", item.get("contradict", []))):
            for citation in citations:
                evidence_id = UUID(citation["id"])
                exists = db.query(FindingEvidenceLink).filter_by(finding_id=finding.id, evidence_id=evidence_id, stance=stance).first()
                if exists is None and db.query(InvestigationRunEvidence).filter_by(
                    run_id=run.id, evidence_id=evidence_id, collection_status="available").one_or_none():
                    db.add(FindingEvidenceLink(workspace_id=run.workspace_id, project_id=run.project_id,
                        incident_id=run.incident_id, run_id=run.id, finding_id=finding.id,
                        evidence_id=evidence_id, stance=stance, explanation=citation.get("summary", "")))
        for context_item in item.get("context", []):
            context = db.get(InvestigationRunContextReference, UUID(context_item["id"]))
            if context and db.query(FindingContextLink).filter_by(
                finding_id=finding.id, context_reference_id=context.id).one_or_none() is None:
                db.add(FindingContextLink(workspace_id=run.workspace_id, project_id=run.project_id,
                    incident_id=run.incident_id, run_id=run.id, finding_id=finding.id,
                    context_reference_id=context.id, explanation="Historical context; not current-incident evidence."))

    for rec in result.get("recommendations", []):
        key = sha256((rec["title"] + "\0" + rec["description"]).encode("utf-8")).hexdigest()
        recommendation = db.query(Recommendation).filter_by(run_id=run.id, idempotency_key=key).one_or_none()
        if recommendation is None:
            db.add(Recommendation(workspace_id=run.workspace_id, project_id=run.project_id,
                incident_id=run.incident_id, run_id=run.id, idempotency_key=key,
                title=rec["title"], description=rec["description"], risk_level=rec["risk_level"],
                expected_benefit=rec.get("benefit", ""), evidence_refs=rec.get("evidence_refs", []),
                required_permissions=rec.get("required_permissions", []),
                verification_steps=rec.get("verification_steps", []), status="proposed"))


def execute_run(run_id: str, *, worker_id: str | None = None, celery_task_id: str = "",
                session_factory: sessionmaker = SessionLocal, checkpointer=None) -> bool:
    """Execute only after an atomic claim; graph state and product writes are run-scoped."""
    worker_id = worker_id or str(uuid4())
    if not claim_run(run_id, worker_id, celery_task_id=celery_task_id, session_factory=session_factory):
        return False
    db = session_factory()
    run = None
    try:
        run = db.get(InvestigationRun, UUID(run_id))
        if run is None or not _owns_lease(db, run.id, worker_id):
            return False
        incident = db.get(Incident, run.incident_id)
        if incident is None:
            raise ValueError("Investigation incident no longer exists")

        collect_step = db.query(InvestigationStep).filter_by(run_id=run.id, step_key="collect").one_or_none()
        if collect_step is None or collect_step.status != "succeeded":
            _step(db, run, "collect", "running")
            evidence = db.query(EvidenceItem).filter_by(incident_id=run.incident_id).all()
            for item in evidence:
                association = db.query(InvestigationRunEvidence).filter_by(run_id=run.id, evidence_id=item.id).one_or_none()
                if association is None:
                    db.add(InvestigationRunEvidence(workspace_id=run.workspace_id, project_id=run.project_id,
                        incident_id=run.incident_id, run_id=run.id, evidence_id=item.id,
                        usage_type="preexisting_attached", collection_status="available"))
            source_definitions = [
                ("incident_evidence", "project-evidence", "completed", len(evidence), "Existing incident evidence was attached to this run."),
                ("live_deployments", "not-configured", "unavailable", 0, "Deployment evidence is not connected."),
            ]
            provider_sources = {"prometheus": "metrics", "loki": "application_log", "tempo": "traces"}
            latest_sync = {}
            for sync in db.query(IntegrationSyncRun).filter_by(incident_id=incident.id).order_by(
                    IntegrationSyncRun.started_at.desc()).all():
                latest_sync.setdefault(sync.source_type, sync)
            integrations = db.query(Integration).filter_by(workspace_id=incident.workspace_id,
                project_id=incident.project_id, enabled=True).filter(Integration.provider.in_(provider_sources)).all()
            for provider, source_type in provider_sources.items():
                source_ref = next((str(item.id) for item in integrations if item.provider == provider), "not-configured")
                sync = latest_sync.get(provider)
                if sync:
                    status = sync.status if sync.status in {"completed", "unavailable", "failed", "skipped"} else "failed"
                    count = sync.result_count
                    reason = ("Provider collection completed." if status == "completed" else
                              f"Provider collection {status}: {sync.error_code or 'no additional details'}.")
                elif source_ref == "not-configured":
                    status, count, reason = "unavailable", 0, "No enabled provider integration is configured."
                else:
                    status, count, reason = "unavailable", 0, "No collection has been recorded for this incident."
                source_definitions.append((source_type, source_ref, status, count, reason))
            for source_type, source_ref, source_status, count, reason in source_definitions:
                fingerprint = f"run-{run.id}-{source_type}"
                source_attempt = (db.query(func.max(InvestigationRunSourceStatus.attempt_no)).filter_by(
                    run_id=run.id, source_type=source_type, source_instance_ref=source_ref,
                    query_fingerprint=fingerprint).scalar() or 0) + 1
                db.add(InvestigationRunSourceStatus(workspace_id=run.workspace_id, project_id=run.project_id,
                    incident_id=run.incident_id, run_id=run.id, source_type=source_type,
                    source_instance_ref=source_ref, query_fingerprint=fingerprint, attempt_no=source_attempt,
                    status=source_status, result_count=count, reason=reason))
            db.commit()
            _step(db, run, "collect", "succeeded", {"evidence_count": len(evidence)})
        else:
            evidence = [item for _, item in db.query(InvestigationRunEvidence, EvidenceItem).join(
                EvidenceItem, EvidenceItem.id == InvestigationRunEvidence.evidence_id).filter(
                    InvestigationRunEvidence.run_id == run.id,
                    InvestigationRunEvidence.collection_status == "available").all()]

        source_gaps = [{"source": row.source_type, "status": row.status, "reason": row.reason}
            for row in db.query(InvestigationRunSourceStatus).filter_by(run_id=run.id).all()
            if row.status in ("unavailable", "failed", "skipped")]
        db.commit()
        state = {
            "incident": {"id": str(incident.id), "title": incident.title, "description": incident.description},
            "evidence": [{"id": str(x.id), "summary": x.summary, "source_type": x.source_type,
                          "observed_at": x.observed_at.isoformat()} for x in evidence],
            "source_gaps": source_gaps, "historical": [],
        }

        def retrieve_for_run(_state):
            from app.services.retrieval import retrieve_context
            _, context_rows, document_status = retrieve_context(db, run, incident, evidence)
            source_type = "knowledge_retrieval"
            source_ref = settings.embedding_provider if document_status["status"] == "completed" else "embedding-provider"
            fingerprint = sha256(f"{run.id}:knowledge:{run.project_id}".encode()).hexdigest()
            attempt_no = (db.query(func.max(InvestigationRunSourceStatus.attempt_no)).filter_by(
                run_id=run.id, source_type=source_type, source_instance_ref=source_ref,
                query_fingerprint=fingerprint).scalar() or 0) + 1
            db.add(InvestigationRunSourceStatus(workspace_id=run.workspace_id, project_id=run.project_id,
                incident_id=run.incident_id, run_id=run.id, source_type=source_type,
                source_instance_ref=source_ref, query_fingerprint=fingerprint, attempt_no=attempt_no,
                status=document_status["status"], result_count=document_status.get("result_count", 0),
                reason=document_status["reason"]))
            db.commit()
            gaps = [{"source": row.source_type, "status": row.status, "reason": row.reason}
                for row in db.query(InvestigationRunSourceStatus).filter_by(run_id=run.id).all()
                if row.status in ("unavailable", "failed", "skipped")]
            return {"historical": context_rows, "source_gaps": gaps}

        def progress(key, status, output=None, error=""):
            if not _owns_lease(db, run.id, worker_id):
                return
            current = db.execute(select(InvestigationRun).where(InvestigationRun.id == run.id)).scalar_one()
            _step(db, current, key, status, output, error)
            db.execute(update(InvestigationRun).where(
                InvestigationRun.id == run.id, InvestigationRun.status == "running",
                InvestigationRun.lease_owner == worker_id,
            ).values(lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=settings.investigation_lease_seconds)))
            db.commit()

        config = {"configurable": {"thread_id": str(run.thread_id)}}
        if checkpointer is None:
            from langgraph.checkpoint.postgres import PostgresSaver
            checkpointer_url = settings.database_url.replace("postgresql+psycopg://", "postgresql://")
            checkpoint_context = PostgresSaver.from_conn_string(checkpointer_url)
        else:
            checkpoint_context = nullcontext(checkpointer)
        with checkpoint_context as active_checkpointer:
            if hasattr(active_checkpointer, "setup"):
                active_checkpointer.setup()
            graph = build_graph(checkpointer=active_checkpointer, on_step=progress,
                                retrieve_fn=retrieve_for_run)
            snapshot = graph.get_state(config)
            if snapshot.values and snapshot.next:
                graph_result = graph.invoke(None, config)
            elif snapshot.values and "result" in snapshot.values:
                graph_result = snapshot.values
            else:
                graph_result = graph.invoke(state, config)
        result = graph_result["result"]

        locked_run = db.execute(select(InvestigationRun).where(
            InvestigationRun.id == run.id
        ).with_for_update().execution_options(populate_existing=True)).scalar_one()
        if locked_run.status != "running" or locked_run.lease_owner != worker_id:
            return False
        _persist_graph_result(db, locked_run, result)
        transition_run(locked_run, "waiting_for_review")
        locked_run.completed_at = datetime.now(timezone.utc)
        locked_run.error_summary = ""
        locked_run.lease_owner = None
        locked_run.lease_expires_at = None
        execution_attempt = _attempt_for_owner(db, locked_run, worker_id)
        if execution_attempt:
            execution_attempt.status = "succeeded"
            execution_attempt.completed_at = datetime.now(timezone.utc)
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
