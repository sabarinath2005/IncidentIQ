"""Encrypted connector credentials, truthful test health, and incident timeline fields."""
from alembic import op

revision = "0004_secure_incident_connectors"
down_revision = "0003_enterprise_integrations"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE integrations ADD COLUMN IF NOT EXISTS credentials_ciphertext TEXT")
    op.execute("ALTER TABLE integrations ADD COLUMN IF NOT EXISTS last_checked_at TIMESTAMPTZ")
    op.execute("ALTER TABLE integrations ADD COLUMN IF NOT EXISTS last_test_result VARCHAR(32) NOT NULL DEFAULT 'not_tested'")
    op.execute("ALTER TABLE services ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ")
    op.execute("ALTER TABLE incidents ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ")
    op.execute("ALTER TABLE incidents ADD COLUMN IF NOT EXISTS assignee_id UUID")
    op.execute("ALTER TABLE incidents ADD COLUMN IF NOT EXISTS impact TEXT NOT NULL DEFAULT ''")
    op.execute("""
        DO $$ DECLARE c RECORD; BEGIN
            FOR c IN SELECT conname FROM pg_constraint WHERE conrelid='incidents'::regclass AND contype='c'
                AND pg_get_constraintdef(oid) ILIKE '%status%' LOOP
                EXECUTE format('ALTER TABLE incidents DROP CONSTRAINT %I', c.conname);
            END LOOP;
            ALTER TABLE incidents ADD CONSTRAINT ck_incident_status
                CHECK (status IN ('open','acknowledged','investigating','mitigated','resolved','closed'));
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_incident_assignee_project_member' AND conrelid='incidents'::regclass) THEN
                ALTER TABLE incidents ADD CONSTRAINT fk_incident_assignee_project_member
                    FOREIGN KEY (project_id, assignee_id) REFERENCES project_members(project_id, user_id);
            END IF;
            FOR c IN SELECT conname FROM pg_constraint WHERE conrelid='project_members'::regclass AND contype='c'
                AND pg_get_constraintdef(oid) ILIKE '%role%' LOOP
                EXECUTE format('ALTER TABLE project_members DROP CONSTRAINT %I', c.conname);
            END LOOP;
            ALTER TABLE project_members ADD CONSTRAINT ck_project_member_role
                CHECK (role IN ('admin','member','engineer','incident_manager','viewer'));
        END $$
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS incident_activities (
            id UUID PRIMARY KEY,
            workspace_id UUID NOT NULL,
            project_id UUID NOT NULL,
            incident_id UUID NOT NULL,
            actor_id UUID NOT NULL REFERENCES users(id),
            event_type VARCHAR(50) NOT NULL,
            detail JSON NOT NULL DEFAULT '{}'::json,
            created_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT fk_incident_activity_incident FOREIGN KEY (workspace_id, project_id, incident_id)
                REFERENCES incidents(workspace_id, project_id, id) ON DELETE CASCADE,
            CONSTRAINT fk_incident_activity_project_actor FOREIGN KEY (project_id, actor_id)
                REFERENCES project_members(project_id, user_id)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_incident_activity_timeline ON incident_activities (incident_id, created_at)")
    op.execute("""
        CREATE TABLE IF NOT EXISTS integration_sync_runs (
            id UUID PRIMARY KEY,
            workspace_id UUID NOT NULL,
            project_id UUID NOT NULL,
            integration_id UUID NOT NULL,
            status VARCHAR(24) NOT NULL,
            error_code VARCHAR(64) NOT NULL DEFAULT '',
            started_at TIMESTAMPTZ NOT NULL,
            completed_at TIMESTAMPTZ,
            CONSTRAINT fk_integration_sync_integration FOREIGN KEY (workspace_id, project_id, integration_id)
                REFERENCES integrations(workspace_id, project_id, id) ON DELETE CASCADE
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_integration_sync_history ON integration_sync_runs (integration_id, started_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_incident_project_assignee_status ON incidents (project_id, assignee_id, status)")


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_incident_project_assignee_status")
    op.execute("DROP INDEX IF EXISTS ix_integration_sync_history")
    op.execute("DROP TABLE IF EXISTS integration_sync_runs")
    op.execute("DROP INDEX IF EXISTS ix_incident_activity_timeline")
    op.execute("DROP TABLE IF EXISTS incident_activities")
    op.execute("ALTER TABLE project_members DROP CONSTRAINT IF EXISTS ck_project_member_role")
    op.execute("ALTER TABLE incidents DROP CONSTRAINT IF EXISTS fk_incident_assignee_project_member")
    op.execute("ALTER TABLE incidents DROP CONSTRAINT IF EXISTS ck_incident_status")
    op.execute("ALTER TABLE incidents DROP COLUMN IF EXISTS impact")
    op.execute("ALTER TABLE incidents DROP COLUMN IF EXISTS assignee_id")
    op.execute("ALTER TABLE incidents DROP COLUMN IF EXISTS closed_at")
    op.execute("ALTER TABLE services DROP COLUMN IF EXISTS archived_at")
    op.execute("ALTER TABLE integrations DROP COLUMN IF EXISTS last_test_result")
    op.execute("ALTER TABLE integrations DROP COLUMN IF EXISTS last_checked_at")
    op.execute("ALTER TABLE integrations DROP COLUMN IF EXISTS credentials_ciphertext")
