from datetime import datetime, timezone

import pytest

from app import db as db_module
from app import workers
from app.services import telemetry_collection


def test_telemetry_task_retries_retryable_provider_failures(monkeypatch):
    class FakeDB:
        def get(self, _model, _incident_id):
            return object()

        def close(self):
            pass

    monkeypatch.setattr(db_module, "SessionLocal", FakeDB)
    monkeypatch.setattr(telemetry_collection, "collect_incident_telemetry", lambda *args, **kwargs: {
        "outcomes": [{"provider": "loki", "status": "unavailable", "retryable": True}],
    })

    class RetryRequested(Exception):
        def __init__(self, countdown):
            self.countdown = countdown

    def retry(*, countdown):
        raise RetryRequested(countdown)

    monkeypatch.setattr(workers.collect_incident_telemetry_task, "retry", retry)
    with pytest.raises(RetryRequested) as exc:
        workers.collect_incident_telemetry_task.run({
            "incident_id": "synthetic-incident",
            "start": datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(),
            "end": datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc).isoformat(),
        })
    assert exc.value.countdown == 5
    assert workers.collect_incident_telemetry_task.max_retries == 3


def test_telemetry_task_returns_completed_provider_outcomes(monkeypatch):
    class FakeDB:
        def get(self, _model, _incident_id):
            return object()

        def close(self):
            pass

    monkeypatch.setattr(db_module, "SessionLocal", FakeDB)
    expected = {"outcomes": [{"provider": "prometheus", "status": "completed", "retryable": False}]}
    monkeypatch.setattr(telemetry_collection, "collect_incident_telemetry", lambda *args, **kwargs: expected)
    monkeypatch.setattr(workers.collect_incident_telemetry_task, "retry", lambda **kwargs: pytest.fail("unexpected retry"))
    result = workers.collect_incident_telemetry_task.run({
        "incident_id": "synthetic-incident",
        "start": datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(),
        "end": datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc).isoformat(),
    })
    assert result == expected
