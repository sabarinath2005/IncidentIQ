from datetime import datetime, timezone, timedelta
from sqlalchemy import select
from app.db import SessionLocal
from app.models import OutboxEvent


def dispatch_event(event_id: str):
    db = SessionLocal()
    try:
        event = db.execute(select(OutboxEvent).where(OutboxEvent.id == event_id).with_for_update(skip_locked=True)).scalar_one_or_none()
        if event is None or event.status == "published":
            return
        if event.status == "dead" or event.available_at > datetime.now(timezone.utc):
            return
        if event.event_type in {"investigation.requested", "investigation.retry"}:
            from app.workers import execute_investigation
            execute_investigation.apply_async(args=[str(event.aggregate_id)], task_id=str(event.id))
        elif event.event_type in {"alert.telemetry.requested", "telemetry.collect.requested"}:
            from app.workers import collect_incident_telemetry_task
            collect_incident_telemetry_task.apply_async(args=[event.payload], task_id=str(event.id))
        event.status = "published"
        event.published_at = datetime.now(timezone.utc)
        event.attempt_count += 1
        event.last_error = ""
        db.commit()
    except Exception as exc:
        db.rollback()
        event = db.get(OutboxEvent, event_id)
        if event:
            event.status = "pending"
            event.attempt_count += 1
            event.last_error = str(exc)[:2000]
            event.available_at = datetime.now(timezone.utc) + timedelta(seconds=min(3600, 2 ** min(event.attempt_count, 10)))
            db.commit()
        raise
    finally:
        db.close()


def dispatch_pending_events(batch_size: int = 50):
    """Recover committed events after API/dispatcher crashes; safe to call repeatedly."""
    db = SessionLocal()
    try:
        event_ids = [str(row[0]) for row in db.query(OutboxEvent.id).filter(
            OutboxEvent.status == "pending", OutboxEvent.available_at <= datetime.now(timezone.utc)
        ).order_by(OutboxEvent.created_at).limit(batch_size).all()]
    finally:
        db.close()
    for event_id in event_ids:
        dispatch_event(event_id)
