import httpx
import pytest
from sqlalchemy.exc import OperationalError
from langgraph.checkpoint.memory import InMemorySaver

import app.ai as ai
from app.ai import build_graph, rank_hypotheses
from app.run_lifecycle import InvalidRunTransition, RUN_ACTIVE_STATUSES, RUN_TERMINAL_STATUSES, transition_run
from app.services.investigations import is_retryable_error


def test_run_lifecycle_allows_review_completion_and_rejects_terminal_reentry():
    run = type("Run", (), {"status": "queued"})()
    transition_run(run, "running")
    transition_run(run, "waiting_for_review")
    assert run.status in RUN_ACTIVE_STATUSES
    transition_run(run, "completed")
    assert run.status in RUN_TERMINAL_STATUSES
    with pytest.raises(InvalidRunTransition):
        transition_run(run, "running")


@pytest.mark.parametrize("initial,terminal", [("queued", "cancelled"), ("running", "failed")])
def test_terminal_failures_release_active_run_policy(initial, terminal):
    run = type("Run", (), {"status": initial})()
    transition_run(run, terminal)
    assert run.status not in RUN_ACTIVE_STATUSES


def test_retry_classifier_retries_transient_http_but_not_permanent_http():
    request = httpx.Request("GET", "https://provider.example.test")
    assert is_retryable_error(httpx.ConnectError("connection reset", request=request))
    transient = httpx.Response(503, request=request)
    permanent = httpx.Response(400, request=request)
    assert is_retryable_error(httpx.HTTPStatusError("server error", request=request, response=transient))
    assert not is_retryable_error(httpx.HTTPStatusError("bad request", request=request, response=permanent))
    assert not is_retryable_error(ValueError("invalid provider response"))


def test_retry_classifier_checks_database_sqlstate():
    class PgError(Exception):
        sqlstate = "40001"

    class PermanentPgError(Exception):
        sqlstate = "23505"

    transient = OperationalError("statement", {}, PgError("serialization failure"))
    permanent = OperationalError("statement", {}, PermanentPgError("duplicate key"))
    assert is_retryable_error(transient)
    assert not is_retryable_error(permanent)
    assert is_retryable_error(OperationalError("connection", {}, Exception("lost"), connection_invalidated=True))
    assert is_retryable_error(OperationalError("connection", {}, Exception("connection refused")))


def test_graph_persists_start_and_completion_for_actual_nodes():
    events = []
    graph = build_graph(on_step=lambda key, status, output=None, error="": events.append((key, status)))
    result = graph.invoke(
        {"incident": {"title": "release issue"}, "evidence": [{
            "id": "evidence-1", "source_type": "deployment_event",
            "summary": "Release version 2.4.0 preceded an error", "observed_at": "2026-01-01T00:00:00Z",
        }], "source_gaps": [], "historical": []},
        {"configurable": {"thread_id": "reliability-node-test"}},
    )
    names = [name for name, status in events if status == "running"]
    assert names == ["normalize", "retrieve", "generate", "verify", "rank", "recommend"]
    assert all((name, "succeeded") in events for name in names)
    assert result["result"]["hypotheses"]


def test_graph_reports_failed_node_attempt(monkeypatch):
    events = []
    monkeypatch.setattr(ai, "generate_hypotheses", lambda state: {"hypotheses": [{
        "hypothesis": "bad citation", "support": [{"id": "not-in-run"}], "contradict": [], "context": [],
    }]})
    graph = build_graph(on_step=lambda key, status, output=None, error="": events.append((key, status)))
    with pytest.raises(ValueError, match="outside this investigation run"):
        graph.invoke({"incident": {"title": "test"}, "evidence": [], "source_gaps": [], "historical": []},
                     {"configurable": {"thread_id": "reliability-failed-node-test"}})
    assert ("generate", "succeeded") in events
    assert ("verify", "running") in events
    assert ("verify", "failed") in events


def test_ranking_sorts_candidates_by_score():
    result = rank_hypotheses({"evidence": [{"id": "e1"}], "source_gaps": [], "verified": [
        {"hypothesis": "weak", "support": [], "contradict": [{"id": "e1"}]},
        {"hypothesis": "strong", "support": [{"id": "e1"}], "contradict": []},
    ]})
    assert [row["hypothesis"] for row in result["ranked"]] == ["strong", "weak"]


def test_graph_retries_from_checkpoint_without_repeating_completed_nodes(monkeypatch):
    calls = {"normalize": 0, "generate": 0}
    events = []
    original_normalize = ai.normalize
    original_generate = ai.generate_hypotheses

    def count_normalize(state):
        calls["normalize"] += 1
        return original_normalize(state)

    def fail_once(state):
        calls["generate"] += 1
        if calls["generate"] == 1:
            raise RuntimeError("transient model transport failure")
        return original_generate(state)

    monkeypatch.setattr(ai, "normalize", count_normalize)
    monkeypatch.setattr(ai, "generate_hypotheses", fail_once)
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "checkpoint-recovery-test"}}
    graph = build_graph(checkpointer=saver, on_step=lambda key, status, output=None, error="": events.append((key, status)))
    state = {"incident": {"title": "release issue"}, "evidence": [{
        "id": "evidence-1", "source_type": "deployment_event", "summary": "Release preceded error",
        "observed_at": "2026-01-01T00:00:00Z",
    }], "source_gaps": [], "historical": []}

    with pytest.raises(RuntimeError, match="transient model"):
        graph.invoke(state, config)
    snapshot = graph.get_state(config)
    assert "generate" in snapshot.next
    resumed = graph.invoke(None, config)
    assert calls == {"normalize": 1, "generate": 2}
    assert resumed["result"]["hypotheses"]
    assert events.count(("generate", "running")) == 2
