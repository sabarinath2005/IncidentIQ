"""Migration upgrade tests use disposable PostgreSQL schemas only."""
import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import DocumentChunk
from tests.pg_test_safety import validated_test_url

RAW_URL = os.getenv("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not RAW_URL, reason="Set TEST_DATABASE_URL to a disposable PostgreSQL/pgvector database")


def _migrate(target: str):
    config = Config("alembic.ini")
    command.upgrade(config, target)


def _insert_revision_0005_user_records(engine):
    user_id, workspace_id, project_id, incident_id, activity_id = (uuid4() for _ in range(5))
    with engine.begin() as conn:
        conn.execute(text("""
            INSERT INTO users (id,email,full_name,password_hash,active,created_at)
            VALUES (:id,:email,'Migration user','hash',true,now())
        """), {"id": user_id, "email": f"identity-{user_id.hex}@example.test"})
        conn.execute(text("INSERT INTO workspaces (id,name,created_at) VALUES (:id,'Identity workspace',now())"),
                     {"id": workspace_id})
        conn.execute(text("""
            INSERT INTO workspace_members (workspace_id,user_id,role)
            VALUES (:workspace_id,:user_id,'owner')
        """), {"workspace_id": workspace_id, "user_id": user_id})
        conn.execute(text("""
            INSERT INTO projects (id,workspace_id,name,description,created_at)
            VALUES (:id,:workspace_id,'Identity project','',now())
        """), {"id": project_id, "workspace_id": workspace_id})
        conn.execute(text("""
            INSERT INTO project_members (workspace_id,project_id,user_id,role)
            VALUES (:workspace_id,:project_id,:user_id,'admin')
        """), {"workspace_id": workspace_id, "project_id": project_id, "user_id": user_id})
        conn.execute(text("""
            INSERT INTO incidents (id,workspace_id,project_id,title,description,severity,status,impact,
                                   resolution_notes,confirmed_root_cause,
                                   started_at,detected_at,reporter_id)
            VALUES (:id,:workspace_id,:project_id,'Existing user incident','', 'SEV-3','open','','','',now(),now(),:user_id)
        """), {"id": incident_id, "workspace_id": workspace_id, "project_id": project_id,
               "user_id": user_id})
        conn.execute(text("""
            INSERT INTO incident_activities (id,workspace_id,project_id,incident_id,actor_id,event_type,detail,created_at)
            VALUES (:id,:workspace_id,:project_id,:incident_id,:user_id,'created','{}'::json,now())
        """), {"id": activity_id, "workspace_id": workspace_id, "project_id": project_id,
               "incident_id": incident_id, "user_id": user_id})
    return incident_id, activity_id, user_id, workspace_id, project_id


def test_clean_and_existing_v2_upgrade_preserve_rows(monkeypatch):
    if not RAW_URL: pytest.skip("No TEST_DATABASE_URL")
    try: safe_url = validated_test_url(RAW_URL)
    except ValueError as exc: pytest.fail(str(exc), pytrace=False)
    admin = create_engine(safe_url)
    clean_schema, upgrade_schema = "mig_" + uuid4().hex[:10], "mig_" + uuid4().hex[:10]
    try:
        with admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{clean_schema}"'))
            conn.execute(text(f'CREATE SCHEMA "{upgrade_schema}"'))
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))

        for schema in (clean_schema, upgrade_schema):
            scoped = make_url(safe_url).update_query_dict({"options": f"-csearch_path={schema},public"})
            monkeypatch.setattr(settings, "database_url", scoped.render_as_string(hide_password=False))
            _migrate("head")

        scoped = make_url(safe_url).update_query_dict({"options": f"-csearch_path={upgrade_schema},public"})
        monkeypatch.setattr(settings, "database_url", scoped.render_as_string(hide_password=False))
        command.downgrade(Config("alembic.ini"), "0005_observability_alerts")
        legacy_engine = create_engine(scoped)
        identity_ids = _insert_revision_0005_user_records(legacy_engine)
        legacy_engine.dispose()
        _migrate("head")

        identity_engine = create_engine(scoped)
        try:
            with identity_engine.connect() as conn:
                reporter_type = conn.execute(text("SELECT reporter_type FROM incidents WHERE id=:id"),
                                             {"id": identity_ids[0]}).scalar_one()
                actor_type = conn.execute(text("SELECT actor_type FROM incident_activities WHERE id=:id"),
                                          {"id": identity_ids[1]}).scalar_one()
                assert reporter_type == actor_type == "user"
                assert conn.execute(text("SELECT reporter_id FROM incidents WHERE id=:id"),
                                    {"id": identity_ids[0]}).scalar_one() == identity_ids[2]
                assert conn.execute(text("SELECT actor_id FROM incident_activities WHERE id=:id"),
                                    {"id": identity_ids[1]}).scalar_one() == identity_ids[2]
        finally:
            identity_engine.dispose()

        command.downgrade(Config("alembic.ini"), "0002_investigation_reliability")

        old_engine = create_engine(scoped)
        with old_engine.begin() as conn:
            conn.execute(text("INSERT INTO users (id,email,full_name,password_hash,active,created_at) VALUES (gen_random_uuid(),'kept@example.test','Kept','hash',true,now())"))
        old_engine.dispose()

        monkeypatch.setattr(settings, "database_url", scoped.render_as_string(hide_password=False))
        _migrate("head")
        upgraded = create_engine(scoped)
        try:
            inspector = inspect(upgraded)
            assert {"credentials_ciphertext", "last_checked_at", "last_test_result"} <= {c["name"] for c in inspector.get_columns("integrations")}
            assert {"assignee_id", "closed_at", "impact"} <= {c["name"] for c in inspector.get_columns("incidents")}
            assert {"reporter_type"} <= {c["name"] for c in inspector.get_columns("incidents")}
            assert {"actor_type"} <= {c["name"] for c in inspector.get_columns("incident_activities")}
            assert {"provider_name", "provider_mode", "model_version", "input_tokens", "output_tokens"} <= {c["name"] for c in inspector.get_columns("investigation_runs")}
            assert {"status", "processing_error", "version", "chunk_count", "source_chunk_id"} <= (
                {c["name"] for c in inspector.get_columns("knowledge_documents")}
                | {c["name"] for c in inspector.get_columns("investigation_run_context_references")})
            assert inspector.has_table("github_deliveries") and inspector.has_table("external_issue_links")
            assert {"service_id", "integration_id", "provider", "external_id", "collection_metadata"} <= {c["name"] for c in inspector.get_columns("evidence_items")}
            assert {"incident_id", "source_type", "result_count", "details"} <= {c["name"] for c in inspector.get_columns("integration_sync_runs")}
            assert inspector.has_table("alertmanager_alerts")
            assert {"fingerprint", "integration_id", "incident_id", "status", "severity"} <= {c["name"] for c in inspector.get_columns("alertmanager_alerts")}
            with upgraded.connect() as conn:
                assert conn.execute(text("SELECT count(*) FROM users WHERE email='kept@example.test'")).scalar_one() == 1
                assert conn.execute(text("SELECT reporter_type FROM incidents WHERE id=:id"),
                                    {"id": identity_ids[0]}).scalar_one() == "user"
                assert conn.execute(text("""
                    SELECT count(*) FROM pg_constraint
                    WHERE conrelid IN ('incidents'::regclass, 'incident_activities'::regclass)
                      AND conname IN ('ck_incident_reporter_identity', 'ck_incident_activity_actor_identity')
                """)).scalar_one() == 2
        finally: upgraded.dispose()
    finally:
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{clean_schema}" CASCADE'))
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{upgrade_schema}" CASCADE'))
        admin.dispose()


def test_0008_expand_preserves_legacy_vectors_and_downgrade_fails_safely(monkeypatch):
    """PostgreSQL coverage for old/new vector coexistence and chunk provenance."""
    if not RAW_URL: pytest.skip("No TEST_DATABASE_URL")
    try: safe_url = validated_test_url(RAW_URL)
    except ValueError as exc: pytest.fail(str(exc), pytrace=False)
    admin = create_engine(safe_url)
    schema = "embed_" + uuid4().hex[:10]
    try:
        with admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        scoped = make_url(safe_url).update_query_dict({"options": f"-csearch_path={schema},public"})
        monkeypatch.setattr(settings, "database_url", scoped.render_as_string(hide_password=False))
        _migrate("0007_ai_provider_metadata")
        engine = create_engine(scoped)

        # Recreate the schema that existed before migration 0008. The baseline
        # imports current metadata, so later columns must be removed explicitly.
        with engine.begin() as conn:
            conn.execute(text("DROP INDEX IF EXISTS ix_doc_chunks_embedding_1536"))
            conn.execute(text("ALTER TABLE document_chunks DROP CONSTRAINT IF EXISTS ck_document_chunk_embedding_1536_dimensions"))
            conn.execute(text("ALTER TABLE document_chunks DROP COLUMN IF EXISTS embedding_1536_dimensions"))
            conn.execute(text("ALTER TABLE document_chunks DROP COLUMN IF EXISTS embedding_1536_model"))
            conn.execute(text("ALTER TABLE document_chunks DROP COLUMN IF EXISTS embedding_1536"))
            conn.execute(text("DROP INDEX IF EXISTS uq_context_document_chunk_source"))
            conn.execute(text("DROP INDEX IF EXISTS uq_context_document_legacy_source"))
            conn.execute(text("ALTER TABLE investigation_run_context_references DROP CONSTRAINT IF EXISTS fk_context_document_chunk"))
            for column in ("source_chunk_id", "source_document_version", "source_chunk_index", "source_timestamp", "source_excerpt"):
                conn.execute(text(f"ALTER TABLE investigation_run_context_references DROP COLUMN IF EXISTS {column}"))
            for column in ("indexed_at", "updated_at", "created_at", "chunk_count", "version", "processing_error",
                           "status", "file_size", "mime_type", "filename"):
                conn.execute(text(f"ALTER TABLE knowledge_documents DROP COLUMN IF EXISTS {column}"))
        incident_id, _, user_id, workspace_id, project_id = _insert_revision_0005_user_records(engine)
        document_id, run_id = uuid4(), uuid4()
        with engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO knowledge_documents (id,workspace_id,project_id,title,source_ref,source_kind,content_hash,content,outcome)
                VALUES (:id,:workspace,:project,'Migration runbook','test','runbook',:hash,'Source text','')
            """), {"id": document_id, "workspace": workspace_id, "project": project_id, "hash": "d" * 64})
            conn.execute(text("""
                INSERT INTO investigation_runs (id,workspace_id,project_id,incident_id,idempotency_key,status,requested_by,
                                                thread_id,workflow_version,prompt_version,model_id,provider_name,provider_mode,model_version,
                                                error_summary,created_at)
                VALUES (:id,:workspace,:project,:incident,:key,'queued',:user,:thread,'migration-test','migration-test',
                        'mock-demo','mock','demo','mock-demo-v1','',now())
            """), {"id": run_id, "workspace": workspace_id, "project": project_id,
                   "incident": incident_id, "key": f"migration-{run_id.hex}", "user": user_id,
                   "thread": f"thread-{run_id.hex}"})
            chunk_ids = [uuid4(), uuid4()]
            for index, chunk_id in enumerate(chunk_ids):
                conn.execute(text("""
                    INSERT INTO document_chunks (id,workspace_id,project_id,document_id,chunk_index,text,embedding,embedding_model)
                    VALUES (:id,:workspace,:project,:document,:index,:chunk,CAST(:vector AS vector(64)),'demo-hash-v1')
                """), {"id": chunk_id, "workspace": workspace_id, "project": project_id,
                       "document": document_id, "index": index, "chunk": f"Old chunk {index}",
                       "vector": "[" + ",".join(["0.125"] * 64) + "]"})
            old_vectors = conn.execute(text("SELECT array_agg(embedding::text ORDER BY chunk_index) FROM document_chunks WHERE document_id=:doc"),
                                       {"doc": document_id}).scalar_one()

        _migrate("0008_real_knowledge_embeddings")
        session = Session(engine)
        try:
            chunks = session.query(DocumentChunk).filter_by(document_id=document_id).order_by(DocumentChunk.chunk_index).all()
            assert len(chunks) == 2
            assert all(len(chunk.embedding) == 64 and chunk.embedding_1536 is None for chunk in chunks)
            assert all(chunk.embedding_model == "demo-hash-v1" for chunk in chunks)
            vectors = [[1.0] + [0.0] * 1535, [0.0, 1.0] + [0.0] * 1534]
            for chunk, vector in zip(chunks, vectors, strict=True):
                chunk.embedding_1536 = vector
                chunk.embedding_1536_model = "text-embedding-3-small"
                chunk.embedding_1536_dimensions = 1536
            session.commit()
            query = [1.0] + [0.0] * 1535
            nearest = session.query(DocumentChunk).filter(
                DocumentChunk.workspace_id == workspace_id,
                DocumentChunk.project_id == project_id,
                DocumentChunk.embedding_1536_dimensions == 1536,
                DocumentChunk.embedding_1536_model == "text-embedding-3-small",
            ).order_by(DocumentChunk.embedding_1536.cosine_distance(query)).first()
            assert nearest is not None and nearest.id == chunk_ids[0]
        finally:
            session.close()

        # Two chunk references from one document/run are valid in the expanded schema.
        with engine.begin() as conn:
            for rank, chunk_id in enumerate(chunk_ids, 1):
                conn.execute(text("""
                    INSERT INTO investigation_run_context_references
                    (id,workspace_id,project_id,incident_id,run_id,origin_workspace_id,origin_project_id,
                     source_kind,source_document_id,source_chunk_id,source_document_version,source_chunk_index,
                     source_excerpt,retrieval_rank,retrieval_score,retrieval_method,source_content_hash,
                     source_outcome_snapshot,retrieved_at)
                    VALUES (:id,:workspace,:project,:incident,:run,:workspace,:project,'document',:document,:chunk,
                            1,:rank,:excerpt,:rank,0.9,'pgvector_cosine:test',:hash,'',now())
                """), {"id": uuid4(), "workspace": workspace_id, "project": project_id,
                       "incident": incident_id, "run": run_id, "document": document_id,
                       "chunk": chunk_id, "rank": rank, "excerpt": f"Old chunk {rank-1}", "hash": "d" * 64})
            conn.execute(text("UPDATE document_chunks SET embedding_1536=NULL, embedding_1536_model='', embedding_1536_dimensions=NULL WHERE document_id=:doc"),
                         {"doc": document_id})
        with pytest.raises(Exception, match="chunk-level historical references exist"):
            command.downgrade(Config("alembic.ini"), "0007_ai_provider_metadata")
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM investigation_run_context_references WHERE run_id=:run"),
                                {"run": run_id}).scalar_one() == 2
            assert conn.execute(text("SELECT array_agg(embedding::text ORDER BY chunk_index) FROM document_chunks WHERE document_id=:doc"),
                                {"doc": document_id}).scalar_one() == old_vectors

        # Once the new, non-representable references are explicitly removed,
        # the schema downgrade succeeds and the original 64d values remain.
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM investigation_run_context_references WHERE run_id=:run"), {"run": run_id})
        command.downgrade(Config("alembic.ini"), "0007_ai_provider_metadata")
        with engine.connect() as conn:
            assert "embedding_1536" not in {column["name"] for column in inspect(engine).get_columns("document_chunks")}
            assert conn.execute(text("SELECT array_agg(embedding::text ORDER BY chunk_index) FROM document_chunks WHERE document_id=:doc"),
                                {"doc": document_id}).scalar_one() == old_vectors
        engine.dispose()
    finally:
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()
