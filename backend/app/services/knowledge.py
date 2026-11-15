"""Safe document parsing, chunking and real-provider indexing lifecycle."""
from __future__ import annotations

import json
from hashlib import sha256
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import DocumentChunk, KnowledgeDocument
from app.services.embeddings import EmbeddingUnavailable, chunk_document, embed_texts


SUPPORTED_DOCUMENTS = {
    ".txt": {"text/plain", "application/octet-stream"},
    ".md": {"text/markdown", "text/plain", "application/octet-stream"},
    ".csv": {"text/csv", "text/plain", "application/octet-stream"},
    ".json": {"application/json", "text/json", "application/octet-stream"},
}


def parse_document(filename: str, mime_type: str, content: bytes) -> tuple[str, str, str]:
    normalized = filename.replace("\\", "/").split("/")[-1]
    extension = "." + normalized.rsplit(".", 1)[-1].lower() if "." in normalized else ""
    if extension not in SUPPORTED_DOCUMENTS:
        raise ValueError("Supported document types are .txt, .md, .csv and .json")
    declared = (mime_type or "application/octet-stream").split(";", 1)[0].strip().lower()
    if declared not in SUPPORTED_DOCUMENTS[extension]:
        raise ValueError("File content type does not match its supported document extension")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("Document must contain valid UTF-8 text") from exc
    if "\x00" in text:
        raise ValueError("Binary content is not accepted")
    if extension == ".json":
        try:
            text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
        except (ValueError, TypeError) as exc:
            raise ValueError("JSON document is malformed") from exc
    text = text.strip()
    if not text:
        raise ValueError("Document contains no text")
    if len(text.encode("utf-8")) > settings.knowledge_max_file_bytes:
        raise ValueError("Document exceeds the configured size limit")
    return normalized[:240], declared, text


def process_document(db: Session, document: KnowledgeDocument) -> KnowledgeDocument:
    document.status = "processing"
    document.processing_error = ""
    db.commit()
    try:
        chunks = chunk_document(document.content)
        if not chunks or len(chunks) > 64:
            raise EmbeddingUnavailable("Document exceeds the current indexing chunk limit")
        embeddings, model = [], ""
        for start in range(0, len(chunks), 64):
            vectors, model = embed_texts(chunks[start:start + 64])
            embeddings.extend(vectors)
        db.query(DocumentChunk).filter_by(workspace_id=document.workspace_id,
            project_id=document.project_id, document_id=document.id).delete(synchronize_session=False)
        for index, (chunk, vector) in enumerate(zip(chunks, embeddings, strict=True)):
            db.add(DocumentChunk(workspace_id=document.workspace_id, project_id=document.project_id,
                document_id=document.id, chunk_index=index, text=chunk,
                embedding_1536=vector, embedding_1536_model=model,
                embedding_1536_dimensions=len(vector)))
        from datetime import datetime, timezone
        document.status = "indexed"
        document.chunk_count = len(chunks)
        document.indexed_at = datetime.now(timezone.utc)
        document.processing_error = ""
        db.commit()
    except Exception as exc:
        db.rollback()
        document = db.get(KnowledgeDocument, document.id)
        document.status = "failed"
        document.processing_error = (str(exc) if isinstance(exc, EmbeddingUnavailable)
                                     else "Document indexing failed due to an internal processing error.")[:500]
        db.commit()
    return document


def content_digest(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()
