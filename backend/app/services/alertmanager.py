"""Authenticated Alertmanager event ingestion and idempotent incident linking."""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import re
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import (
    AlertmanagerAlert, AuditEvent, EvidenceItem, Incident, IncidentActivity,
    IncidentService, Integration, IntegrationSyncRun, OutboxEvent,
    Service,
)
from app.schemas import AlertmanagerWebhookPayload


SENSITIVE_KEY_RE = re.compile(r"password|token|secret|authorization|api[_-]?key|email", re.IGNORECASE)
SENSITIVE_TEXT_RE = re.compile(
    r"(?i)(password|token|secret|api[_-]?key|authorization)\s*[:=]\s*[^\s,;]+|"
    r"Bearer\s+[A-Za-z0-9._~+/-]+=*|"
    r"https?://[^/@\s]+:[^/@\s]+@|"
    r"([?&](?:token|key|secret|password|api_key)=)[^&\s]+"
)


def _redact(value: str, limit: int) -> str:
    return SENSITIVE_TEXT_RE.sub(lambda match: "[REDACTED]" if not match.group(1) else match.group(1) + "=[REDACTED]", value)[:limit]


def safe_alert_fields(labels: dict[str, str], annotations: dict[str, str]) -> tuple[dict, dict]:
    clean_labels = {
        str(key)[:80]: _redact(str(value), 300)
        for key, value in labels.items()
        if isinstance(key, str) and not SENSITIVE_KEY_RE.search(key)
    }
    clean_annotations = {
        key: _redact(str(annotations[key]), 2000)
        for key in ("summary", "description")
        if key in annotations and isinstance(annotations[key], str)
    }
    return clean_labels, clean_annotations


def severity_for(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {"critical", "page", "emergency", "sev-1", "p1"}:
        return "SEV-1"
    if normalized in {"high", "error", "sev-2", "p2"}:
        return "SEV-2"
    if normalized in {"warning", "warn", "medium", "sev-3", "p3"}:
        return "SEV-3"
    return "SEV-4"


def configured_service(db: Session, integration: Integration) -> Service:
    try:
        service_id = UUID(str((integration.config or {}).get("service_id", "")))
    except (ValueError, TypeError):
        raise HTTPException(422, "Alertmanager integration requires a project service")
    service = db.query(Service).filter_by(workspace_id=integration.workspace_id,
                                          project_id=integration.project_id, id=service_id).one_or_none()
    if service is None:
        raise HTTPException(404, "Integration service was not found in its project")
    return service


def process_alertmanager_webhook(db: Session, integration: Integration,
                                 payload: AlertmanagerWebhookPayload) -> dict:
    service = configured_service(db, integration)
    processed = 0
    created = 0
    updated = 0
    duplicate = 0
    resolved = 0
    for alert in payload.alerts:
        labels, annotations = safe_alert_fields(alert.labels, alert.annotations)
        alertname = labels.get("alertname")
        if not alertname:
            raise HTTPException(422, "Each alert must include the alertname label")
        configured_id = str(service.id)
        claimed_service = labels.get("service_id")
        claimed_name = labels.get("service_name") or labels.get("service")
        if claimed_service and claimed_service != configured_id:
            raise HTTPException(403, "Alert service does not match the integration project service")
        if claimed_name and claimed_name != service.name:
            raise HTTPException(403, "Alert service does not match the integration project service")

        event_status = alert.status or payload.status
        severity = severity_for(labels.get("severity", "warning"))
        now = datetime.now(timezone.utc)
        starts_at = alert.startsAt.astimezone(timezone.utc)
        ends_at = alert.endsAt.astimezone(timezone.utc) if alert.endsAt else None
        row = db.query(AlertmanagerAlert).filter_by(integration_id=integration.id,
                                                   fingerprint=alert.fingerprint).with_for_update().one_or_none()
        previous_status = row.status if row else None
        incident = db.get(Incident, row.incident_id) if row and row.incident_id else None
        is_duplicate = row is not None and previous_status == event_status and row.starts_at == starts_at
        if is_duplicate:
            row.last_received_at = now
            duplicate += 1
            processed += 1
            continue

        new_incident = False
        if event_status == "firing" and (incident is None or incident.status in {"resolved", "closed"}):
            description = annotations.get("description", annotations.get("summary", "Monitoring alert received."))
            incident = Incident(
                workspace_id=integration.workspace_id, project_id=integration.project_id,
                title=f"[{severity}] {alertname}"[:240], description=description[:10000],
                severity=severity, status="open", started_at=starts_at, detected_at=starts_at,
                reporter_id=None, reporter_type="system",
            )
            db.add(incident)
            db.flush()
            db.add(IncidentService(workspace_id=integration.workspace_id, project_id=integration.project_id,
                                   incident_id=incident.id, service_id=service.id, impact_role="affected"))
            new_incident = True
            created += 1
        elif event_status == "firing" and incident is not None:
            incident.severity = severity
            incident.description = annotations.get("description", incident.description)[:10000]
            updated += 1
        elif event_status == "resolved":
            resolved += 1

        if row is None:
            row = AlertmanagerAlert(
                workspace_id=integration.workspace_id, project_id=integration.project_id,
                integration_id=integration.id, service_id=service.id, fingerprint=alert.fingerprint,
                alertname=alertname[:240], status=event_status, severity=severity,
                starts_at=starts_at, ends_at=ends_at, labels=labels, annotations=annotations,
                incident_id=incident.id if incident else None, last_received_at=now,
            )
            db.add(row)
            db.flush()
        else:
            row.status = event_status
            row.severity = severity
            row.starts_at = starts_at
            row.ends_at = ends_at
            row.labels = labels
            row.annotations = annotations
            row.last_received_at = now
            if incident is not None:
                row.incident_id = incident.id

        if incident is not None:
            detail = {"provider": "alertmanager", "integration_id": str(integration.id),
                      "fingerprint": alert.fingerprint, "status": event_status,
                      "alertname": alertname[:240], "severity": severity,
                      "starts_at": starts_at.isoformat(), "ends_at": ends_at.isoformat() if ends_at else None}
            db.add(IncidentActivity(workspace_id=incident.workspace_id, project_id=incident.project_id,
                                    incident_id=incident.id, actor_id=None, actor_type="system",
                                    event_type="monitoring_alert_received", detail=detail))
            db.add(AuditEvent(workspace_id=incident.workspace_id, project_id=incident.project_id,
                              actor_id=None, action=f"alertmanager.{event_status}", resource_type="alertmanager_alert",
                              resource_id=row.id, detail={"integration_id": str(integration.id),
                              "incident_id": str(incident.id), "fingerprint": alert.fingerprint,
                              "alertname": alertname[:240], "severity": severity}))
            normalized = {"provider": "alertmanager", "fingerprint": alert.fingerprint,
                          "status": event_status, "alertname": alertname[:240], "severity": severity,
                          "labels": labels, "annotations": annotations,
                          "starts_at": starts_at.isoformat(),
                          "ends_at": ends_at.isoformat() if ends_at else None,
                          "generator_url_present": bool(alert.generatorURL)}
            normalized_hash = sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            evidence_exists = db.query(EvidenceItem).filter_by(
                incident_id=incident.id, integration_id=integration.id,
                source_type="alertmanager", content_hash=normalized_hash,
            ).first()
            if evidence_exists is None:
                db.add(EvidenceItem(
                    workspace_id=incident.workspace_id, project_id=incident.project_id,
                    incident_id=incident.id, service_id=service.id, integration_id=integration.id,
                    provider="alertmanager", external_id=f"{alert.fingerprint}:{event_status}:{int(starts_at.timestamp())}"[:300],
                    source_type="alertmanager", source_ref=f"alertmanager:{alert.fingerprint}",
                    observed_at=ends_at if event_status == "resolved" and ends_at else starts_at,
                    summary=f"Alertmanager {event_status}: {alertname} ({severity}). {annotations.get('summary', '')}"[:12000],
                    content_hash=normalized_hash, metadata_json=normalized,
                    collection_metadata={"integration_id": str(integration.id), "received_at": now.isoformat()},
                ))
            if event_status == "firing" and new_incident:
                start = starts_at - timedelta(minutes=5)
                outbox_key = f"alert-telemetry:{integration.id}:{sha256(alert.fingerprint.encode()).hexdigest()[:16]}:{int(starts_at.timestamp())}"
                existing_outbox = db.query(OutboxEvent).filter_by(idempotency_key=outbox_key).one_or_none()
                if existing_outbox is None:
                    db.add(OutboxEvent(
                        workspace_id=incident.workspace_id, project_id=incident.project_id,
                        aggregate_type="incident", aggregate_id=incident.id,
                        event_type="alert.telemetry.requested",
                        payload={"incident_id": str(incident.id), "start": start.isoformat(), "end": now.isoformat()},
                        idempotency_key=outbox_key,
                    ))
        processed += 1

    now = datetime.now(timezone.utc)
    db.add(IntegrationSyncRun(
        workspace_id=integration.workspace_id, project_id=integration.project_id,
        integration_id=integration.id, source_type="alertmanager", status="completed",
        error_code="", result_count=processed,
        details={"created": created, "updated": updated, "duplicate": duplicate, "resolved": resolved},
        started_at=now, completed_at=now,
    ))
    integration.health_status = "healthy"
    integration.last_checked_at = now
    integration.last_successful_sync_at = now
    integration.last_test_result = "connected"
    integration.last_error = ""
    db.commit()
    return {"processed": processed, "created": created, "updated": updated,
            "duplicate": duplicate, "resolved": resolved}
