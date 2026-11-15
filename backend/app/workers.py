from celery import Celery
from uuid import uuid4
from app.core.config import settings
from app.services.investigations import is_retryable_error, record_execution_failure
from sqlalchemy.exc import SQLAlchemyError

celery_app = Celery("incident_platform", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_serializer="json", result_serializer="json", accept_content=["json"],
    task_acks_late=True, task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1, task_track_started=True,
    beat_schedule={
        "recover-pending-outbox-events": {
            "task": "outbox.dispatch_pending",
            "schedule": 10.0,
        },
        "recover-expired-investigation-leases": {
            "task": "investigations.recover_stale",
            "schedule": 10.0,
        },
    },
)


@celery_app.task(bind=True, name="investigations.execute", max_retries=3)
def execute_investigation(self, run_id: str):
    from app.services.investigations import execute_run
    worker_id = f"{self.request.id or 'celery'}:{uuid4()}"
    try:
        return execute_run(run_id, worker_id=worker_id, celery_task_id=str(self.request.id or ""))
    except Exception as exc:
        try:
            scheduled = record_execution_failure(run_id, worker_id, exc, retryable=is_retryable_error(exc))
        except Exception as persistence_error:
            # If PostgreSQL is unavailable, retain the original run and let lease
            # recovery handle it; Celery retries this task after the lease window.
            if is_retryable_error(persistence_error) and self.request.retries < self.max_retries:
                raise self.retry(exc=persistence_error, countdown=settings.investigation_lease_seconds + 5)
            raise
        if scheduled:
            return {"retry_scheduled": True}
        raise


@celery_app.task(name="outbox.dispatch", autoretry_for=(ConnectionError, TimeoutError), retry_backoff=True, retry_kwargs={"max_retries": 8})
def dispatch_outbox_event(event_id: str):
    from app.services.outbox import dispatch_event
    dispatch_event(event_id)


@celery_app.task(name="outbox.dispatch_pending")
def dispatch_pending_outbox():
    from app.services.outbox import dispatch_pending_events
    dispatch_pending_events()


@celery_app.task(name="investigations.recover_stale")
def recover_stale_investigations():
    from app.services.investigations import recover_stale_runs
    return recover_stale_runs()


@celery_app.task(bind=True, name="telemetry.collect_incident", max_retries=3,
                autoretry_for=(ConnectionError, TimeoutError, SQLAlchemyError),
                retry_backoff=True, retry_kwargs={"max_retries": 3})
def collect_incident_telemetry_task(self, payload: dict):
    from datetime import datetime
    from app.db import SessionLocal
    from app.models import Incident
    from app.services.telemetry_collection import collect_incident_telemetry

    db = SessionLocal()
    try:
        incident = db.get(Incident, payload.get("incident_id"))
        if incident is None:
            return {"status": "skipped", "reason": "incident_not_found"}
        start = datetime.fromisoformat(payload["start"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(payload["end"].replace("Z", "+00:00"))
        result = collect_incident_telemetry(db, incident, start=start, end=end)
        if any(outcome.get("retryable") is True for outcome in result.get("outcomes", [])):
            countdown = min(60, 5 * (2 ** self.request.retries))
            raise self.retry(countdown=countdown)
        return result
    finally:
        db.close()
