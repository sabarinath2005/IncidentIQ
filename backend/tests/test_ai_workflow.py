import json
import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from app.ai import build_graph
from app.core.config import settings


@pytest.fixture(autouse=True)
def default_mock_provider(monkeypatch):
    """Ensure demo workflow tests run deterministically regardless of root .env."""
    monkeypatch.setattr(settings, "llm_provider", "mock")


def test_demo_graph_returns_hypothesis_with_run_evidence_citation():
    evidence = [{
        "id": "00000000-0000-0000-0000-000000000001",
        "source_type": "deployment_event",
        "summary": "Deployment version 2.4.0 completed shortly before ERROR 503.",
        "observed_at": "2026-11-13T10:00:00+00:00",
    }]
    graph = build_graph(checkpointer=InMemorySaver())
    result = graph.invoke(
        {"incident": {"title": "checkout errors"}, "evidence": evidence,
         "source_gaps": [{"source": "traces", "status": "unavailable"}], "historical": []},
        {"configurable": {"thread_id": "test-run-1"}},
    )
    finding = result["result"]["hypotheses"][0]
    assert finding["support"][0]["id"] == evidence[0]["id"]
    assert finding["confidence_method"] == "transparent_evidence_coverage_heuristic"
    assert "not a calibrated probability" in finding["uncertainty"]
    assert result["result"]["source_gaps"][0]["source"] == "traces"


def test_demo_graph_reports_insufficient_evidence_without_citations():
    graph = build_graph(checkpointer=InMemorySaver())
    result = graph.invoke(
        {"incident": {"title": "unknown issue"}, "evidence": [], "source_gaps": [], "historical": []},
        {"configurable": {"thread_id": "test-run-2"}},
    )
    assert result["result"]["insufficient_evidence"] is True
    assert result["result"]["hypotheses"][0]["support"] == []


def test_real_provider_graph_execution_with_mock_transport(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(settings, "openai_model", "test-model")
    monkeypatch.setattr(settings, "openai_base_url", "https://api.openai.com/v1")

    evidence = [{
        "id": "00000000-0000-0000-0000-000000000001",
        "source_type": "deployment_event",
        "summary": "Release 2.4.0 completed shortly before 503 errors.",
        "observed_at": "2026-11-13T10:00:00+00:00",
    }]

    def respond(request):
        payload = {
            "hypotheses": [{
                "hypothesis": "Release 2.4.0 regression",
                "explanation": "Errors began directly following the deployment.",
                "suspected_root_cause": "Release 2.4.0 regression",
                "affected_service": "checkout-api",
                "support_ids": ["00000000-0000-0000-0000-000000000001"],
                "contradict_ids": [],
                "historical_context_ids": [],
                "missing_evidence": ["Post-release trace logs"],
                "verification_steps": ["Review diff between v2.3.9 and v2.4.0."],
                "risk_assessment": "Rollback may impact newer checkout features.",
            }]
        }
        return httpx.Response(200, json={"model": "test-model-v1", "choices": [{
            "message": {"content": json.dumps(payload)}}],
            "usage": {"prompt_tokens": 150, "completion_tokens": 60}}, request=request)

    import app.ai_providers as ai_providers
    original_client = ai_providers.httpx.Client
    monkeypatch.setattr(ai_providers.httpx, "Client",
                        lambda *args, **kwargs: original_client(*args, **{**kwargs, "transport": httpx.MockTransport(respond)}))

    graph = build_graph(checkpointer=InMemorySaver())
    result = graph.invoke(
        {"incident": {"title": "checkout errors"}, "evidence": evidence,
         "source_gaps": [], "historical": []},
        {"configurable": {"thread_id": "test-run-real"}},
    )
    finding = result["result"]["hypotheses"][0]
    assert finding["hypothesis"] == "Release 2.4.0 regression"
    assert finding["support"][0]["id"] == evidence[0]["id"]
    assert result["provider_metadata"]["provider"] == "openai"
    assert result["provider_metadata"]["input_tokens"] == 150
