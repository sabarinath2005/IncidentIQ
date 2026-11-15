"""Project-scoped historical retrieval with persisted real-embedding provenance."""
import re
from hashlib import sha256

from sqlalchemy import func, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import (DocumentChunk, Incident, InvestigationRunContextReference,
                        KnowledgeDocument)
from app.services.embeddings import EmbeddingUnavailable, embed_texts


def tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_.-]{3,}", value.lower()))


def retrieve_context(db: Session, run, incident, evidence, limit: int = 5):
    limit = max(1, min(int(limit), 20))
    query_text = " ".join([incident.title, incident.description] + [item.summary for item in evidence])[:20000]
    query_terms = tokens(query_text)
    candidates = db.query(Incident).filter(
        Incident.workspace_id == run.workspace_id,
        Incident.project_id == run.project_id,
        Incident.id != run.incident_id,
        Incident.confirmed_root_cause != "",
    ).all()
    ranked_incidents = []
    for row in candidates:
        source_text = " ".join([row.title, row.description, row.confirmed_root_cause])
        source_terms = tokens(source_text)
        score = len(query_terms & source_terms) / max(1, len(query_terms | source_terms))
        if score > 0:
            ranked_incidents.append((score, row, source_text))
    ranked_incidents.sort(key=lambda result: result[0], reverse=True)
    references, result = [], []
    for rank, (score, old, source_text) in enumerate(ranked_incidents[:limit], 1):
        ref = db.query(InvestigationRunContextReference).filter_by(
            run_id=run.id, source_kind="incident", source_incident_id=old.id).one_or_none()
        if ref is None:
            ref = InvestigationRunContextReference(
                workspace_id=run.workspace_id, project_id=run.project_id, incident_id=run.incident_id,
                run_id=run.id, origin_workspace_id=old.workspace_id, origin_project_id=old.project_id,
                source_kind="incident", source_incident_id=old.id, retrieval_rank=rank,
                retrieval_score=score, retrieval_method="project_keyword_v1",
                source_content_hash=sha256(source_text.encode()).hexdigest(),
                source_outcome_snapshot=old.confirmed_root_cause, source_timestamp=old.detected_at,
                source_excerpt=source_text[:6000],
            )
            db.add(ref)
            db.flush()
        references.append(ref)
        result.append({"id": str(ref.id), "source_incident_id": str(old.id), "title": old.title,
                       "outcome": old.confirmed_root_cause, "kind": "historical_incident",
                       "retrieval_method": "project_keyword_v1", "retrieval_score": score})

    document_status = {"status": "unavailable", "reason": "No indexed project knowledge documents were found."}
    if settings.embedding_provider.strip().lower() == "openai":
        try:
            indexed_count = db.query(func.count(DocumentChunk.id)).join(
                KnowledgeDocument, KnowledgeDocument.id == DocumentChunk.document_id,
            ).filter(DocumentChunk.workspace_id == run.workspace_id, DocumentChunk.project_id == run.project_id,
                KnowledgeDocument.workspace_id == run.workspace_id, KnowledgeDocument.project_id == run.project_id,
                KnowledgeDocument.status == "indexed", DocumentChunk.embedding_1536.is_not(None),
                DocumentChunk.embedding_1536_dimensions == settings.embedding_dimensions,
                DocumentChunk.embedding_1536_model == settings.openai_embedding_model).scalar() or 0
            if not indexed_count:
                return references, result, {"status": "completed", "reason": "No indexed project knowledge chunks were found.", "result_count": 0}
            vectors, model = embed_texts([query_text])
            # Bound work performed by the vector search, even for a broad project.
            db.execute(text("SET LOCAL statement_timeout = '3000ms'"))
            chunks = db.query(DocumentChunk, KnowledgeDocument).join(
                KnowledgeDocument, KnowledgeDocument.id == DocumentChunk.document_id,
            ).add_columns(
                DocumentChunk.embedding_1536.cosine_distance(vectors[0]).label("distance"),
            ).filter(
                DocumentChunk.workspace_id == run.workspace_id,
                DocumentChunk.project_id == run.project_id,
                KnowledgeDocument.workspace_id == run.workspace_id,
                KnowledgeDocument.project_id == run.project_id,
                KnowledgeDocument.status == "indexed",
                DocumentChunk.embedding_1536.is_not(None),
                DocumentChunk.embedding_1536_dimensions == settings.embedding_dimensions,
                DocumentChunk.embedding_1536_model == model,
            ).order_by(DocumentChunk.embedding_1536.cosine_distance(vectors[0])).limit(limit).all()
            document_status = {"status": "completed", "reason": "Semantic search completed.",
                               "embedding_model": model, "result_count": len(chunks)}
            for doc_rank, (chunk, doc, distance) in enumerate(chunks, len(result) + 1):
                ref = db.query(InvestigationRunContextReference).filter_by(
                    run_id=run.id, source_kind="document", source_chunk_id=chunk.id).one_or_none()
                if ref is None:
                    ref = InvestigationRunContextReference(
                        workspace_id=run.workspace_id, project_id=run.project_id, incident_id=run.incident_id,
                        run_id=run.id, origin_workspace_id=doc.workspace_id, origin_project_id=doc.project_id,
                        source_kind="document", source_document_id=doc.id, source_chunk_id=chunk.id,
                        source_document_version=doc.version, source_chunk_index=chunk.chunk_index,
                        source_timestamp=doc.updated_at,
                        retrieval_rank=doc_rank, retrieval_score=max(0.0, 1.0 - float(distance)),
                        retrieval_method=f"pgvector_cosine:{model}"[:40], source_content_hash=doc.content_hash,
                        source_outcome_snapshot=doc.outcome, source_excerpt=chunk.text[:6000],
                    )
                    db.add(ref)
                    db.flush()
                references.append(ref)
                result.append({"id": str(ref.id), "source_document_id": str(doc.id),
                    "source_chunk_id": str(chunk.id), "chunk_index": chunk.chunk_index,
                    "title": doc.title, "outcome": doc.outcome, "text": chunk.text,
                    "kind": "historical_document", "source_kind": doc.source_kind,
                    "version": doc.version, "created_at": doc.created_at.isoformat(),
                    "retrieval_method": ref.retrieval_method, "retrieval_score": ref.retrieval_score})
        except EmbeddingUnavailable as exc:
            document_status = {"status": "unavailable", "reason": str(exc)[:300]}
    else:
        document_status = {"status": "unavailable", "reason": "A real embedding provider is not configured."}
    return references, result, document_status
