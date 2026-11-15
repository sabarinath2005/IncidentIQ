from langgraph.checkpoint.memory import InMemorySaver
from app.ai import build_graph


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
