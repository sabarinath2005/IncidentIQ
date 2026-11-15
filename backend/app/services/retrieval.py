"""Project-scoped demo retrieval. Feature hashing is intentionally labeled, not a semantic model."""
import re
from hashlib import sha256
from math import sqrt
from sqlalchemy.orm import Session
from app.models import DocumentChunk, Incident, InvestigationRunContextReference, KnowledgeDocument


def feature_vector(text: str, dimensions: int = 64) -> list[float]:
    values = [0.0] * dimensions
    for token in re.findall(r"[a-z0-9_.-]+", text.lower()):
        digest = sha256(token.encode()).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        values[index] += 1.0 if digest[4] & 1 else -1.0
    norm = sqrt(sum(value * value for value in values)) or 1.0
    return [value / norm for value in values]


def tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_.-]{3,}", text.lower()))


def retrieve_context(db: Session, run, incident, evidence, limit: int = 5):
    query_text = " ".join([incident.title, incident.description] + [item.summary for item in evidence])
    query_terms = tokens(query_text)
    candidates = db.query(Incident).filter(
        Incident.project_id == run.project_id, Incident.id != run.incident_id,
        Incident.confirmed_root_cause != "",
    ).all()
    ranked_incidents = []
    for row in candidates:
        text = " ".join([row.title, row.description, row.confirmed_root_cause])
        other = tokens(text)
        score = len(query_terms & other) / max(1, len(query_terms | other))
        if score > 0:
            ranked_incidents.append((score, row, text))
    ranked_incidents.sort(key=lambda result: result[0], reverse=True)
    references = []
    result = []
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
                source_outcome_snapshot=old.confirmed_root_cause,
            )
            db.add(ref)
            db.flush()
        references.append(ref)
        result.append({"id": str(ref.id), "source_incident_id": str(old.id), "title": old.title,
                       "outcome": old.confirmed_root_cause, "kind": "historical_incident"})

    # pgvector cosine-distance query is always constrained to the current project first.
    query_vector = feature_vector(query_text)
    chunks = db.query(DocumentChunk, KnowledgeDocument).join(
        KnowledgeDocument, KnowledgeDocument.id == DocumentChunk.document_id,
    ).filter(
        DocumentChunk.project_id == run.project_id,
        DocumentChunk.embedding.is_not(None),
    ).order_by(DocumentChunk.embedding.cosine_distance(query_vector)).limit(limit).all()
    doc_rank = len(result) + 1
    for chunk, doc in chunks:
        ref = db.query(InvestigationRunContextReference).filter_by(
            run_id=run.id, source_kind="document", source_document_id=doc.id).one_or_none()
        if ref is None:
            ref = InvestigationRunContextReference(
                workspace_id=run.workspace_id, project_id=run.project_id, incident_id=run.incident_id,
                run_id=run.id, origin_workspace_id=doc.workspace_id, origin_project_id=doc.project_id,
                source_kind="document", source_document_id=doc.id, retrieval_rank=doc_rank,
                retrieval_score=0.0, retrieval_method="pgvector_cosine_demo_hash_v1",
                source_content_hash=doc.content_hash, source_outcome_snapshot=doc.outcome,
            )
            db.add(ref)
            db.flush()
        references.append(ref)
        result.append({"id": str(ref.id), "source_document_id": str(doc.id), "title": doc.title,
                       "outcome": doc.outcome, "text": chunk.text, "kind": "historical_document"})
        doc_rank += 1
    return references, result
