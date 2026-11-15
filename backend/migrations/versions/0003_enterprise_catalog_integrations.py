"""Expand service catalog and scoped integration health metadata."""
from alembic import op

revision = "0003_enterprise_integrations"
down_revision = "0002_investigation_reliability"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE services ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE services ADD COLUMN IF NOT EXISTS criticality VARCHAR(20) NOT NULL DEFAULT 'medium'")
    op.execute("ALTER TABLE services ADD COLUMN IF NOT EXISTS technology JSON NOT NULL DEFAULT '{}'::json")
    op.execute("ALTER TABLE integrations ADD COLUMN IF NOT EXISTS enabled BOOLEAN NOT NULL DEFAULT FALSE")
    op.execute("ALTER TABLE integrations ADD COLUMN IF NOT EXISTS permission_scope JSON NOT NULL DEFAULT '[]'::json")
    op.execute("ALTER TABLE integrations ADD COLUMN IF NOT EXISTS health_status VARCHAR(24) NOT NULL DEFAULT 'unknown'")
    op.execute("ALTER TABLE integrations ADD COLUMN IF NOT EXISTS last_successful_sync_at TIMESTAMPTZ")
    op.execute("ALTER TABLE integrations ADD COLUMN IF NOT EXISTS last_error TEXT NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE integrations ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()")
    op.execute("UPDATE integrations SET enabled = (status = 'enabled') WHERE status IN ('enabled','disabled')")
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_integration_project_provider' AND conrelid = 'integrations'::regclass) THEN
                ALTER TABLE integrations ADD CONSTRAINT uq_integration_project_provider UNIQUE (project_id, provider);
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_service_criticality' AND conrelid = 'services'::regclass) THEN
                ALTER TABLE services ADD CONSTRAINT ck_service_criticality CHECK (criticality IN ('critical','high','medium','low'));
            END IF;
        END $$
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_integrations_workspace_project ON integrations (workspace_id, project_id)")


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_integrations_workspace_project")
    op.execute("ALTER TABLE integrations DROP CONSTRAINT IF EXISTS uq_integration_project_provider")
    op.execute("ALTER TABLE services DROP CONSTRAINT IF EXISTS ck_service_criticality")
    for column in ("updated_at", "last_error", "last_successful_sync_at", "health_status", "permission_scope", "enabled"):
        op.execute(f"ALTER TABLE integrations DROP COLUMN IF EXISTS {column}")
    for column in ("technology", "criticality", "description"):
        op.execute(f"ALTER TABLE services DROP COLUMN IF EXISTS {column}")
