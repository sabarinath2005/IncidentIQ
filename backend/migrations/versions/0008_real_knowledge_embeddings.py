"""Add document processing lifecycle and real embedding vector storage."""
from alembic import op

revision = "0008_real_knowledge_embeddings"
down_revision = "0007_ai_provider_metadata"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS filename VARCHAR(240) NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS mime_type VARCHAR(120) NOT NULL DEFAULT 'text/plain'")
    op.execute("ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS file_size INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS status VARCHAR(24) NOT NULL DEFAULT 'pending'")
    op.execute("ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS processing_error TEXT NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1")
    op.execute("ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS chunk_count INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now()")
    op.execute("ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()")
    op.execute("ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS indexed_at TIMESTAMPTZ")
    op.execute("""DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_knowledge_document_status' AND conrelid='knowledge_documents'::regclass) THEN
            ALTER TABLE knowledge_documents ADD CONSTRAINT ck_knowledge_document_status
            CHECK (status IN ('pending','processing','indexed','failed','removed'));
        END IF;
    END $$""")
    op.execute("ALTER TABLE investigation_run_context_references ADD COLUMN IF NOT EXISTS source_chunk_id UUID")
    op.execute("ALTER TABLE investigation_run_context_references ADD COLUMN IF NOT EXISTS source_document_version INTEGER")
    op.execute("ALTER TABLE investigation_run_context_references ADD COLUMN IF NOT EXISTS source_chunk_index INTEGER")
    op.execute("ALTER TABLE investigation_run_context_references ADD COLUMN IF NOT EXISTS source_timestamp TIMESTAMPTZ")
    op.execute("ALTER TABLE investigation_run_context_references ADD COLUMN IF NOT EXISTS source_excerpt TEXT NOT NULL DEFAULT ''")
    op.execute("""DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_document_chunk_scope_id' AND conrelid='document_chunks'::regclass) THEN
            ALTER TABLE document_chunks ADD CONSTRAINT uq_document_chunk_scope_id UNIQUE (workspace_id, project_id, document_id, id);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_context_document_chunk' AND conrelid='investigation_run_context_references'::regclass) THEN
            ALTER TABLE investigation_run_context_references ADD CONSTRAINT fk_context_document_chunk
            FOREIGN KEY (workspace_id, project_id, source_document_id, source_chunk_id)
            REFERENCES document_chunks(workspace_id, project_id, document_id, id);
        END IF;
    END $$""")
    op.execute("DROP INDEX IF EXISTS uq_context_document_source")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_context_document_chunk_source ON investigation_run_context_references (run_id, source_chunk_id) WHERE source_kind='document' AND source_chunk_id IS NOT NULL")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_context_document_legacy_source ON investigation_run_context_references (run_id, source_document_id) WHERE source_kind='document' AND source_chunk_id IS NULL")
    # Expand in place: legacy 64-dimensional embeddings remain available while
    # real 1536-dimensional embeddings are stored and indexed separately.
    op.execute("ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS embedding_1536 vector(1536)")
    op.execute("ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS embedding_1536_model VARCHAR(120) NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE document_chunks ADD COLUMN IF NOT EXISTS embedding_1536_dimensions INTEGER")
    op.execute("""DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_document_chunk_embedding_1536_dimensions' AND conrelid='document_chunks'::regclass) THEN
            ALTER TABLE document_chunks ADD CONSTRAINT ck_document_chunk_embedding_1536_dimensions
            CHECK ((embedding_1536 IS NULL AND embedding_1536_dimensions IS NULL)
                OR (embedding_1536 IS NOT NULL AND embedding_1536_dimensions = 1536 AND embedding_1536_model <> ''));
        END IF;
    END $$""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_doc_chunks_embedding_1536 ON document_chunks USING hnsw (embedding_1536 vector_cosine_ops)")


def downgrade():
    # Chunk-level references and real vectors cannot be represented by the
    # original one-document-reference/64-dimensional schema. Fail before any
    # DDL so operators can retain data and perform an explicit archival/export.
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM document_chunks WHERE embedding_1536 IS NOT NULL) THEN
            RAISE EXCEPTION 'Cannot downgrade 0008: 1536-dimensional embeddings exist; archive or explicitly remove them first';
        END IF;
        IF EXISTS (SELECT 1 FROM investigation_run_context_references WHERE source_chunk_id IS NOT NULL) THEN
            RAISE EXCEPTION 'Cannot downgrade 0008: chunk-level historical references exist; archive or explicitly remove them first';
        END IF;
        IF EXISTS (
            SELECT 1 FROM investigation_run_context_references
            WHERE source_kind='document'
            GROUP BY run_id, source_document_id HAVING count(*) > 1
        ) THEN
            RAISE EXCEPTION 'Cannot downgrade 0008: multiple document references exist for a run';
        END IF;
    END $$""")
    op.execute("DROP INDEX IF EXISTS ix_doc_chunks_embedding")
    op.execute("DROP INDEX IF EXISTS uq_context_document_chunk_source")
    op.execute("DROP INDEX IF EXISTS uq_context_document_legacy_source")
    op.execute("ALTER TABLE investigation_run_context_references DROP CONSTRAINT IF EXISTS fk_context_document_chunk")
    op.execute("ALTER TABLE knowledge_documents DROP CONSTRAINT IF EXISTS ck_knowledge_document_status")
    op.execute("ALTER TABLE document_chunks DROP CONSTRAINT IF EXISTS uq_document_chunk_scope_id")
    op.execute("ALTER TABLE investigation_run_context_references DROP COLUMN IF EXISTS source_chunk_id")
    op.execute("ALTER TABLE investigation_run_context_references DROP COLUMN IF EXISTS source_document_version")
    op.execute("ALTER TABLE investigation_run_context_references DROP COLUMN IF EXISTS source_chunk_index")
    op.execute("ALTER TABLE investigation_run_context_references DROP COLUMN IF EXISTS source_timestamp")
    op.execute("ALTER TABLE investigation_run_context_references DROP COLUMN IF EXISTS source_excerpt")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_context_document_source ON investigation_run_context_references (run_id, source_document_id) WHERE source_kind='document'")
    op.execute("ALTER TABLE document_chunks DROP CONSTRAINT IF EXISTS ck_document_chunk_embedding_1536_dimensions")
    op.execute("DROP INDEX IF EXISTS ix_doc_chunks_embedding_1536")
    op.execute("ALTER TABLE document_chunks DROP COLUMN IF EXISTS embedding_1536_dimensions")
    op.execute("ALTER TABLE document_chunks DROP COLUMN IF EXISTS embedding_1536_model")
    op.execute("ALTER TABLE document_chunks DROP COLUMN IF EXISTS embedding_1536")
    op.execute("CREATE INDEX IF NOT EXISTS ix_doc_chunks_embedding ON document_chunks USING hnsw (embedding vector_cosine_ops)")
    for column in ("indexed_at", "updated_at", "created_at", "chunk_count", "version", "processing_error",
                   "status", "file_size", "mime_type", "filename"):
        op.execute(f"ALTER TABLE knowledge_documents DROP COLUMN IF EXISTS {column}")
