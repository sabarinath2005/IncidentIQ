"""Migration upgrade tests use disposable PostgreSQL schemas only."""
import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from app.core.config import settings
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
    return incident_id, activity_id, user_id


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
