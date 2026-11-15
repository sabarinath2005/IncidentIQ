import json

import httpx
import pytest

import app.ai as ai
from app.ai_providers import AnalysisOutput, ProviderUnavailable, analyze_openai
from app.core.config import settings
from app.core.config import Settings
from langgraph.checkpoint.memory import InMemorySaver


def valid_payload():
    return {"hypotheses": [{
        "hypothesis": "A database connection pool exhaustion may explain the elevated failures.",
        "explanation": "The connection pool saturation and matching timeout log appeared in the current window.",
        "suspected_root_cause": "Database connection pool exhaustion",
        "affected_service": "checkout-api",
        "support_ids": ["evidence-1"], "contradict_ids": [], "historical_context_ids": [],
        "missing_evidence": ["Database connection pool metrics"],
        "verification_steps": ["Inspect connection pool saturation for the incident interval."],
        "risk_assessment": "Restarting the service could disrupt active requests.",
    }]}


def test_real_provider_validates_schema_and_records_metadata(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "test-only")
    monkeypatch.setattr(settings, "openai_model", "test-model")
    monkeypatch.setattr(settings, "openai_base_url", "https://api.openai.com/v1")
    requests = {"n": 0}

    def respond(request):
        requests["n"] += 1
        assert str(request.url) == "https://api.openai.com/v1/chat/completions"
        assert json.loads(request.content)["response_format"]["type"] == "json_schema"
        return httpx.Response(200, json={"model": "test-model-2026-01", "choices": [{
            "message": {"content": json.dumps(valid_payload())}}],
            "usage": {"prompt_tokens": 123, "completion_tokens": 45}}, request=request)

    result = analyze_openai({"title": "Checkout timeouts"}, [], [], [],
                            transport=httpx.MockTransport(respond))
    assert requests["n"] == 1
    assert result.output.hypotheses[0].hypothesis
    assert result.output.hypotheses[0].support_ids == ["evidence-1"]
    assert result.metadata["mode"] == "real"
    assert result.metadata["model_version"] == "test-model-2026-01"
    assert result.metadata["input_tokens"] == 123


def test_custom_openai_base_url_is_used_for_chat(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "test-only")
    monkeypatch.setattr(settings, "openai_model", "compatible-chat-model")
    monkeypatch.setattr(settings, "openai_base_url", "https://gemini.example/v1/")
    seen = {}

    def respond(request):
        seen["url"] = str(request.url)
        payload = json.loads(request.content)
        seen["model"] = payload["model"]
        seen["response_format"] = payload["response_format"]["type"]
        seen["schema_in_system"] = "risk_assessment" in payload["messages"][0]["content"]
        return httpx.Response(200, json={"choices": [{"message": {
            "content": json.dumps(valid_payload())}}]}, request=request)

    analyze_openai({}, [], [], [], transport=httpx.MockTransport(respond))
    assert seen == {"url": "https://gemini.example/v1/chat/completions", "model": "compatible-chat-model",
                    "response_format": "json_object", "schema_in_system": True}


def test_openai_endpoint_and_embedding_model_defaults_and_env_alias(monkeypatch):
    defaults = Settings(_env_file=None)
    assert defaults.openai_base_url == "https://api.openai.com/v1"
    assert defaults.openai_embedding_model == "text-embedding-3-small"
    monkeypatch.setenv("OPENAI_BASE_URL", "https://gemini.example/v1")
    monkeypatch.setenv("EMBEDDING_MODEL", "gemini-embedding-model")
    custom = Settings(_env_file=None)
    assert custom.openai_base_url == "https://gemini.example/v1"
    assert custom.openai_embedding_model == "gemini-embedding-model"


def test_openai_selected_without_key_fails_without_demo_fallback(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "")
    with pytest.raises(ProviderUnavailable, match="not configured"):
        ai.generate_hypotheses({"incident": {}, "evidence": [], "historical": []})


def test_rate_limit_uses_bounded_retry(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "test-only")
    monkeypatch.setattr(settings, "openai_model", "test-model")
    monkeypatch.setattr(settings, "llm_max_retries", 1)
    attempts = {"n": 0}

    def respond(request):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, request=request)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(valid_payload())}}]}, request=request)

    result = analyze_openai({}, [], [], [], transport=httpx.MockTransport(respond))
    assert attempts["n"] == 2
    assert result.metadata["retry_count"] == 1


@pytest.mark.parametrize("content", ["not json", json.dumps({"hypotheses": [{"hypothesis": "missing required fields"}]})])
def test_invalid_provider_output_is_rejected(monkeypatch, content):
    monkeypatch.setattr(settings, "openai_api_key", "test-only")

    def respond(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]}, request=request)

    with pytest.raises(ProviderUnavailable, match="malformed or schema-invalid"):
        analyze_openai({}, [], [], [], transport=httpx.MockTransport(respond))


def test_real_provider_hallucinated_citation_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "openai")
    payload = valid_payload()
    payload["hypotheses"][0]["support_ids"] = ["invented-evidence"]
    monkeypatch.setattr(ai, "analyze_openai", lambda *args: type("Result", (), {
        "output": AnalysisOutput.model_validate(payload), "metadata": {"provider": "openai", "mode": "real"}})())
    graph = ai.build_graph(checkpointer=InMemorySaver())
    with pytest.raises(ValueError, match="outside this investigation run"):
        graph.invoke({"incident": {}, "evidence": [{"id": "evidence-1", "summary": "timeout"}],
            "historical": [], "source_gaps": []}, {"configurable": {"thread_id": "bad-citation"}})
