import httpx
import pytest

from app.core.config import settings
from app.services.embeddings import EmbeddingUnavailable, chunk_document, embed_texts
from app.services.knowledge import parse_document


def test_safe_document_parser_validates_extension_mime_utf8_and_path():
    filename, mime, body = parse_document("..\\safe\\runbook.md", "text/markdown", b"# Recovery\nRestart only after approval.")
    assert filename == "runbook.md"
    assert mime == "text/markdown"
    assert body.startswith("# Recovery")
    with pytest.raises(ValueError, match="Supported document types"):
        parse_document("payload.exe", "text/plain", b"run this")
    with pytest.raises(ValueError, match="does not match"):
        parse_document("runbook.md", "application/pdf", b"text")
    with pytest.raises(ValueError, match="UTF-8"):
        parse_document("runbook.txt", "text/plain", b"\xff\xfe")


def test_json_is_parsed_as_data_and_malformed_json_rejected():
    _, _, text = parse_document("ops.json", "application/json", b'{"retries": 3}')
    assert '"retries": 3' in text
    with pytest.raises(ValueError, match="malformed"):
        parse_document("ops.json", "application/json", b"{bad json")


def test_chunker_uses_overlap():
    from app.services import embeddings
    old_size, old_overlap = settings.embedding_chunk_words, settings.embedding_chunk_overlap_words
    settings.embedding_chunk_words, settings.embedding_chunk_overlap_words = 5, 2
    try:
        chunks = chunk_document("one two three four five six seven eight")
        assert chunks == ["one two three four five", "four five six seven eight"]
    finally:
        settings.embedding_chunk_words, settings.embedding_chunk_overlap_words = old_size, old_overlap


def test_openai_embeddings_validate_real_vector_dimensions(monkeypatch):
    monkeypatch.setattr(settings, "embedding_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "test-only")
    monkeypatch.setattr(settings, "embedding_dimensions", 1536)

    def respond(request):
        return httpx.Response(200, json={"model": "embedding-test-v1", "data": [{
            "index": 0, "embedding": [0.25] * 1536}]}, request=request)

    vectors, model = embed_texts(["real source text"], transport=httpx.MockTransport(respond))
    assert len(vectors) == 1 and len(vectors[0]) == 1536
    assert model == "embedding-test-v1"


def test_embedding_provider_disabled_does_not_fabricate_vectors(monkeypatch):
    monkeypatch.setattr(settings, "embedding_provider", "disabled")
    with pytest.raises(EmbeddingUnavailable, match="not configured"):
        embed_texts(["source text"])


def test_invalid_embedding_dimensions_are_rejected(monkeypatch):
    monkeypatch.setattr(settings, "embedding_provider", "openai")
    monkeypatch.setattr(settings, "openai_api_key", "test-only")

    def respond(request):
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1]}]}, request=request)

    with pytest.raises(EmbeddingUnavailable, match="invalid vectors"):
        embed_texts(["source text"], transport=httpx.MockTransport(respond))
