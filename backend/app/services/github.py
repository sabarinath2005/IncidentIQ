"""Signed, idempotent GitHub webhook normalization and incident correlation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import hmac
import re

from sqlalchemy.orm import Session

from app.models import (Deployment, EvidenceItem, GitHubDelivery, GitHubRepositoryMapping,
                        Incident, IncidentActivity, IncidentService)
from app.schemas import (GitHubDeploymentPayload, GitHubDeploymentStatusPayload,
                         GitHubPushPayload, GitHubReleasePayload)


SUPPORTED_EVENTS = {"push", "release", "deployment", "deployment_status"}


def valid_signature(body: bytes, signature: str | None, secret: str | None) -> bool:
    if not signature or not secret or not re.fullmatch(r"sha256=[0-9a-f]{64}", signature):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _time(value, fallback):
    if not value:
        return fallback
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return fallback


def _normalized_event(event_type: str, raw: dict):
    now = datetime.now(timezone.utc)
    if event_type == "release":
        payload = GitHubReleasePayload.model_validate(raw)
        release = payload.release
        observed = _time(release.get("published_at") or release.get("created_at"), now)
        external = str(release.get("id") or release.get("tag_name") or "")[:300]
        summary = f"Release {str(release.get('tag_name') or 'unnamed')[:100]} {payload.action} in {payload.repository.full_name}."
        return payload.repository.id, payload.repository.full_name, external, summary, observed, "release"
    if event_type == "deployment":
        payload = GitHubDeploymentPayload.model_validate(raw)
        deployment = payload.deployment
        observed = _time(deployment.get("created_at"), now)
        external = str(deployment.get("id") or "")[:300]
        version = str(deployment.get("sha") or deployment.get("ref") or "unknown")[:100]
        summary = f"Deployment pending for {payload.repository.full_name} ({version[:12]})."
        return payload.repository.id, payload.repository.full_name, external, summary, observed, "pending"
    if event_type == "deployment_status":
        payload = GitHubDeploymentStatusPayload.model_validate(raw)
        deployment = payload.deployment
        status = payload.deployment_status
        observed = _time(status.get("created_at") or deployment.get("created_at"), now)
        external = str(deployment.get("id") or "")[:300]
        state = str(status.get("state") or ("pending" if event_type == "deployment" else "unknown"))[:40]
        version = str(deployment.get("sha") or deployment.get("ref") or "unknown")[:100]
        summary = f"Deployment {state} for {payload.repository.full_name} ({version[:12]})."
        return payload.repository.id, payload.repository.full_name, external, summary, observed, state
    raise ValueError("Unsupported GitHub event")


def _event_data(event_type: str, raw: dict):
    if event_type == "push":
        from app.schemas import GitHubPushPayload
        payload = GitHubPushPayload.model_validate(raw)
        commit = payload.head_commit or (payload.commits[-1] if payload.commits else None)
        external = payload.after or (commit.id if commit else "")
        observed = _time(commit.timestamp if commit else None, datetime.now(timezone.utc))
        count = len(payload.commits)
        summary = f"Push of {count} commit(s) to {payload.ref[:200]} in {payload.repository.full_name}; head {external[:12]}."
        return str(payload.repository.id), payload.repository.full_name, external[:300], summary, observed, "push"
    repo_id, repo_name, external, summary, observed, event_state = _normalized_event(event_type, raw)
    return str(repo_id), repo_name, external, summary, observed, event_state


def _correlate(db: Session, mapping: GitHubRepositoryMapping | None, project_id, occurred_at, summary, external_id):
    if mapping is None or mapping.service_id is None:
        return "unmapped", "Repository or service mapping is missing."
    incidents = db.query(Incident).join(IncidentService, IncidentService.incident_id == Incident.id).filter(
        Incident.workspace_id == mapping.workspace_id, Incident.project_id == project_id,
        IncidentService.service_id == mapping.service_id,
        Incident.status.in_({"open", "acknowledged", "investigating", "mitigated"})).all()
    correlated = []
    for incident in incidents:
        started = incident.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if started - timedelta(hours=2) <= occurred_at <= started + timedelta(hours=2):
            delta_minutes = int((occurred_at - started).total_seconds() / 60)
            reason = (f"Repository {mapping.repository_full_name} maps to service {mapping.service_id}; "
                      f"event occurred {delta_minutes:+d} minutes from incident start (two-hour correlation window). "
                      "Timing indicates relevance, not causation.")
            detail_hash = sha256((external_id + summary).encode()).hexdigest()
            existing = db.query(EvidenceItem).filter_by(incident_id=incident.id,
                source_type="github_change", content_hash=detail_hash).one_or_none()
            if existing is None:
                evidence = EvidenceItem(workspace_id=incident.workspace_id, project_id=incident.project_id,
                    incident_id=incident.id, source_type="github_change", source_ref=mapping.repository_full_name,
                    service_id=mapping.service_id, integration_id=mapping.integration_id, provider="github",
                    external_id=external_id, summary=summary, content_hash=detail_hash,
                    metadata_json={"correlation_reason": reason, "observed_at": occurred_at.isoformat()})
                db.add(evidence)
                db.add(IncidentActivity(workspace_id=incident.workspace_id, project_id=incident.project_id,
                    incident_id=incident.id, actor_id=None, actor_type="system", event_type="github_change_correlated",
                    detail={"repository": mapping.repository_full_name, "external_id": external_id,
                            "correlation_reason": reason}))
            correlated.append(reason)
    return ("correlated", " ".join(correlated)) if correlated else (
        "received", "Repository maps to a service, but no active incident fell within the configured time window.")


def ingest_delivery(db: Session, integration, delivery_id: str, event_type: str, raw: dict):
    old = db.query(GitHubDelivery).filter_by(integration_id=integration.id, delivery_id=delivery_id).one_or_none()
    if old:
        return old, True
    if event_type not in SUPPORTED_EVENTS:
        from uuid import uuid4
        row = GitHubDelivery(workspace_id=integration.workspace_id, project_id=integration.project_id,
            integration_id=integration.id, delivery_id=delivery_id, event_type=event_type[:40], status="ignored",
            summary="Unsupported GitHub event type was safely ignored.")
        db.add(row)
        db.commit()
        return row, False
    repo_id, repo_name, external, summary, occurred_at, event_state = _event_data(event_type, raw)
    mapping = db.query(GitHubRepositoryMapping).filter_by(integration_id=integration.id,
        repository_id=repo_id).one_or_none()
    mapping = mapping or db.query(GitHubRepositoryMapping).filter_by(integration_id=integration.id,
        repository_full_name=repo_name).one_or_none()
    if mapping and event_type in {"deployment", "deployment_status"} and external and mapping.service_id:
        deployment = db.query(Deployment).filter_by(project_id=integration.project_id, provider="github",
            external_id=external, service_id=mapping.service_id).one_or_none()
        raw_deployment = raw.get("deployment", {})
        if deployment is None:
            deployment = Deployment(workspace_id=integration.workspace_id, project_id=integration.project_id,
                service_id=mapping.service_id, provider="github", external_id=external,
                repository_full_name=repo_name, version=str(raw_deployment.get("sha") or raw_deployment.get("ref") or "unknown")[:120],
                commit_ref=str(raw_deployment.get("sha") or "")[:160], environment=str(raw_deployment.get("environment") or "unknown")[:80],
                deployed_at=occurred_at, deployment_status=event_state)
            db.add(deployment)
        else:
            deployment.deployment_status = event_state
            if event_state == "success":
                deployment.deployed_at = occurred_at
        deployment.correlation_reason = "Repository-to-service mapping recorded; temporal association is not proof of causation."
    status, reason = _correlate(db, mapping, integration.project_id, occurred_at, summary, external)
    row = GitHubDelivery(workspace_id=integration.workspace_id, project_id=integration.project_id,
        integration_id=integration.id, delivery_id=delivery_id, event_type=event_type,
        repository_id=repo_id, repository_full_name=repo_name, external_id=external,
        summary=summary[:2000], occurred_at=occurred_at, correlation_reason=reason,
        status=status if status in {"unmapped", "correlated"} else "received")
    db.add(row)
    db.commit()
    return row, False
