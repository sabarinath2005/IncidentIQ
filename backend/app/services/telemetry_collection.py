"""Collect bounded provider results and persist them as provenance-rich evidence."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import AuditEvent, EvidenceItem, Incident, Integration, IntegrationSyncRun, Service
from app.services.connectors import allowed_private_hosts, allowed_external_hosts
from app.services.observability_http import LokiClient, PrometheusClient, ProviderError, TempoClient
from app.services.credentials import decrypt_credentials


PROMETHEUS_QUERIES = {
    "request_rate": 'sum(rate(payment_http_requests_total{service_name="{service}"}[5m]))',
    "error_rate": 'sum(rate(payment_http_errors_total{service_name="{service}"}[5m]))',
    "latency_p50": 'histogram_quantile(0.50, sum by (le) (rate(payment_http_request_duration_seconds_bucket{service_name="{service}"}[5m])))',
    "latency_p95": 'histogram_quantile(0.95, sum by (le) (rate(payment_http_request_duration_seconds_bucket{service_name="{service}"}[5m])))',
    "latency_p99": 'histogram_quantile(0.99, sum by (le) (rate(payment_http_request_duration_seconds_bucket{service_name="{service}"}[5m])))',
    "cpu_utilization": 'payment_process_cpu_utilization_ratio{service_name="{service}"}',
    "memory_bytes": 'payment_process_memory_rss_bytes{service_name="{service}"}',
    "db_pool_utilization": 'payment_db_pool_utilization_ratio{service_name="{service}"}',
    "active_requests": 'payment_active_requests{service_name="{service}"}',
    "failed_requests": 'sum(rate(payment_http_errors_total{service_name="{service}"}[5m]))',
}


def _integration_credentials(integration: Integration) -> dict:
    if not integration.credentials_ciphertext:
        return {}
    return decrypt_credentials(integration.credentials_ciphertext, provider=integration.provider,
                              connector_provider=integration.provider)


def _evidence_hash(provider: str, external_id: str, normalized: dict) -> str:
    payload = json.dumps({"provider": provider, "external_id": external_id, "data": normalized},
                         sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(payload.encode("utf-8")).hexdigest()


def _save_evidence(db: Session, incident: Incident, integration: Integration, service: Service,
                   provider: str, source_type: str, external_id: str, observed_at: datetime,
                   normalized: dict, collection_metadata: dict) -> bool:
    digest = _evidence_hash(provider, external_id, normalized)
    existing = db.query(EvidenceItem).filter(
        EvidenceItem.incident_id == incident.id, EvidenceItem.source_type == source_type,
        EvidenceItem.content_hash == digest,
    ).first()
    if existing is None:
        existing = db.query(EvidenceItem).filter_by(
            incident_id=incident.id, integration_id=integration.id,
            source_type=source_type, external_id=external_id[:300],
        ).first()
    if existing:
        return False
    if source_type == "metrics":
        summary = f"Prometheus metric {external_id}: {json.dumps(normalized, sort_keys=True)[:10000]}"
    elif source_type == "application_log":
        summary = f"Loki log {normalized.get('timestamp', '')}: {normalized.get('message', '')}"[:12000]
    else:
        summary = f"Tempo trace {external_id}: {len(normalized.get('spans', []))} spans collected"
    db.add(EvidenceItem(
        workspace_id=incident.workspace_id, project_id=incident.project_id, incident_id=incident.id,
        service_id=service.id, integration_id=integration.id, provider=provider, external_id=external_id[:300],
        source_type=source_type, source_ref=external_id[:240], observed_at=observed_at,
        summary=summary, content_hash=digest, metadata_json=normalized,
        collection_metadata=collection_metadata,
    ))
    return True


def collect_incident_telemetry(db: Session, incident: Incident, *, start: datetime, end: datetime) -> dict:
    if start.tzinfo is None or end.tzinfo is None or end <= start or end - start > timedelta(hours=24):
        raise ValueError("Telemetry collection range must be ordered and no longer than 24 hours")
    integrations = db.query(Integration).filter(
        Integration.workspace_id == incident.workspace_id,
        Integration.project_id == incident.project_id,
        Integration.enabled.is_(True),
        Integration.provider.in_(["prometheus", "loki", "tempo"]),
    ).order_by(Integration.provider).all()
    outcomes: list[dict] = []
    for integration in integrations:
        config = integration.config or {}
        try:
            service = db.query(Service).filter_by(workspace_id=incident.workspace_id,
                                                  project_id=incident.project_id,
                                                  id=UUID(str(config.get("service_id", "")))).one_or_none()
        except (ValueError, TypeError):
            service = None
        if service is None:
            _record_failure(db, incident, integration, "invalid_service_scope", "Integration service is not configured for this project.", start, end)
            outcomes.append({"provider": integration.provider, "status": "failed", "count": 0})
            continue
        try:
            credentials = _integration_credentials(integration)
            base = config["base_url"]
            allowed = allowed_private_hosts()
            allowed_external = allowed_external_hosts()
            collection_time = datetime.now(timezone.utc)
            collection_metadata = {
                "integration_id": str(integration.id), "provider": integration.provider,
                "service_id": str(service.id), "query_window_start": start.isoformat(),
                "query_window_end": end.isoformat(), "collected_at": collection_time.isoformat(),
            }
            if integration.provider == "prometheus":
                count = _collect_prometheus(db, incident, integration, service, PrometheusClient(
                    base, credentials, allowed_private_hosts=allowed, allowed_public_hosts=allowed_external), start, end, collection_time, collection_metadata)
            elif integration.provider == "loki":
                # OTel log exporters often put severity in structured metadata while
                # Loki's line contains only the message. Return the bounded service
                # stream by default so useful error context is not filtered out.
                query = config.get("logql") or f'{{service_name="{service.name}"}}'
                result = LokiClient(base, credentials, allowed_private_hosts=allowed, allowed_public_hosts=allowed_external).query_range(
                    query, start, end, limit=min(int(config.get("max_log_entries", 100)), 200))
                count = 0
                for entry in result["entries"]:
                    external_id = entry.get("trace_id") or f"{entry.get('timestamp_ns', '')}:{entry.get('request_id') or ''}"
                    metadata = {**collection_metadata, "query": query, "source_labels": entry.get("labels", {})}
                    count += int(_save_evidence(db, incident, integration, service, "loki", "application_log",
                                                external_id or "loki-entry", _parse_time(entry.get("timestamp")), entry, metadata))
            else:
                client = TempoClient(base, credentials, allowed_private_hosts=allowed, allowed_public_hosts=allowed_external)
                matches = client.search(str(config.get("service_name") or service.name), start, end, limit=20)
                count = 0
                seen: set[str] = set()
                for match in matches:
                    trace_id = str(match.get("traceID", match.get("traceId", ""))).lower()
                    if not trace_id or trace_id in seen:
                        continue
                    seen.add(trace_id)
                    trace = client.get_trace(trace_id, start, end)
                    metadata = {**collection_metadata, "search": {"service_name": str(config.get("service_name") or service.name)}}
                    count += int(_save_evidence(db, incident, integration, service, "tempo", "distributed_trace",
                                                trace_id, collection_time, trace, metadata))
            run = IntegrationSyncRun(
                workspace_id=incident.workspace_id, project_id=incident.project_id,
                integration_id=integration.id, incident_id=incident.id, source_type=integration.provider,
                status="completed", error_code="", result_count=count,
                details={"window_start": start.isoformat(), "window_end": end.isoformat()},
                started_at=collection_time, completed_at=datetime.now(timezone.utc),
            )
            db.add(run)
            integration.health_status = "healthy"
            integration.last_successful_sync_at = run.completed_at
            integration.last_checked_at = run.completed_at
            integration.last_test_result = "connected"
            integration.last_error = ""
            db.add(AuditEvent(workspace_id=incident.workspace_id, project_id=incident.project_id, actor_id=None,
                              action="integration.telemetry_collected", resource_type="integration",
                              resource_id=integration.id, detail={"provider": integration.provider,
                              "incident_id": str(incident.id), "result_count": count}))
            db.commit()
            outcomes.append({"provider": integration.provider, "status": "completed", "count": count})
        except ProviderError as exc:
            db.rollback()
            _record_failure(db, incident, integration, exc.code, str(exc), start, end)
            outcomes.append({"provider": integration.provider, "status": "unavailable" if exc.retryable else "failed", "count": 0, "retryable": exc.retryable})
        except HTTPException:
            db.rollback()
            _record_failure(db, incident, integration, "credential_decryption_failed",
                            "Stored provider credentials could not be authenticated.", start, end)
            outcomes.append({"provider": integration.provider, "status": "failed", "count": 0})
        except (ValueError, KeyError, TypeError) as exc:
            db.rollback()
            _record_failure(db, incident, integration, "invalid_configuration", str(exc)[:300], start, end)
            outcomes.append({"provider": integration.provider, "status": "failed", "count": 0})
    return {"incident_id": str(incident.id), "outcomes": outcomes}


def _collect_prometheus(db, incident, integration, service, client, start, end, collected_at, metadata):
    queries = (integration.config or {}).get("queries") or PROMETHEUS_QUERIES
    count = 0
    for name, template in queries.items():
        if name not in PROMETHEUS_QUERIES or not isinstance(template, str):
            continue
        expression = template.replace("{service}", service.name.replace('\\', '\\\\').replace('"', '\\"'))
        result = client.query_range(expression, start, end, step_seconds=60)
        normalized = {"result_type": result.get("resultType"), "result": result.get("result", [])}
        request_meta = {**metadata, "query": expression, "query_name": name}
        external_id = f"{name}:{start.isoformat()}:{end.isoformat()}"
        count += int(_save_evidence(db, incident, integration, service, "prometheus", "metrics", external_id,
                                    collected_at, normalized, request_meta))
    return count


def _record_failure(db, incident, integration, error_code: str, safe_message: str,
                    start: datetime, end: datetime) -> None:
    now = datetime.now(timezone.utc)
    db.add(IntegrationSyncRun(
        workspace_id=incident.workspace_id, project_id=incident.project_id,
        integration_id=integration.id, incident_id=incident.id, source_type=integration.provider,
        status="unavailable" if error_code in {"provider_timeout", "provider_unavailable"} else "failed",
        error_code=error_code[:64], result_count=0,
        details={"window_start": start.isoformat(), "window_end": end.isoformat()},
        started_at=now, completed_at=now,
    ))
    integration.health_status = "unavailable" if error_code in {"provider_timeout", "provider_unavailable"} else "degraded"
    integration.last_checked_at = now
    integration.last_test_result = "unavailable" if integration.health_status == "unavailable" else "failed"
    integration.last_error = safe_message[:300]
    db.add(AuditEvent(workspace_id=incident.workspace_id, project_id=incident.project_id, actor_id=None,
                      action="integration.telemetry_collection_failed", resource_type="integration",
                      resource_id=integration.id, detail={"provider": integration.provider,
                      "incident_id": str(incident.id), "error_code": error_code[:64]}))
    db.commit()


def _parse_time(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime.now(timezone.utc)
