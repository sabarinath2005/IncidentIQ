"""Add scoped observability evidence provenance and Alertmanager deduplication."""
from alembic import op


revision = "0005_observability_alerts"
down_revision = "0004_secure_incident_connectors"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE evidence_items ADD COLUMN IF NOT EXISTS service_id UUID")
    op.execute("ALTER TABLE evidence_items ADD COLUMN IF NOT EXISTS integration_id UUID")
    op.execute("ALTER TABLE evidence_items ADD COLUMN IF NOT EXISTS provider VARCHAR(80) NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE evidence_items ADD COLUMN IF NOT EXISTS external_id VARCHAR(300) NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE evidence_items ADD COLUMN IF NOT EXISTS collection_metadata JSON NOT NULL DEFAULT '{}'::json")
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='uq_evidence_external_source' AND conrelid='evidence_items'::regclass) THEN
                ALTER TABLE evidence_items ADD CONSTRAINT uq_evidence_external_source
                    UNIQUE (integration_id, incident_id, source_type, external_id);
            END IF;
        END $$
    """)
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_evidence_service_scope' AND conrelid='evidence_items'::regclass) THEN
                ALTER TABLE evidence_items ADD CONSTRAINT fk_evidence_service_scope
                    FOREIGN KEY (workspace_id, project_id, service_id)
                    REFERENCES services(workspace_id, project_id, id);
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_evidence_integration_scope' AND conrelid='evidence_items'::regclass) THEN
                ALTER TABLE evidence_items ADD CONSTRAINT fk_evidence_integration_scope
                    FOREIGN KEY (workspace_id, project_id, integration_id)
                    REFERENCES integrations(workspace_id, project_id, id);
            END IF;
        END $$
    """)

    op.execute("ALTER TABLE integration_sync_runs ADD COLUMN IF NOT EXISTS incident_id UUID")
    op.execute("ALTER TABLE integration_sync_runs ADD COLUMN IF NOT EXISTS source_type VARCHAR(50) NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE integration_sync_runs ADD COLUMN IF NOT EXISTS result_count INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE integration_sync_runs ADD COLUMN IF NOT EXISTS details JSON NOT NULL DEFAULT '{}'::json")
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_sync_run_incident_scope' AND conrelid='integration_sync_runs'::regclass) THEN
                ALTER TABLE integration_sync_runs ADD CONSTRAINT fk_sync_run_incident_scope
                    FOREIGN KEY (workspace_id, project_id, incident_id)
                    REFERENCES incidents(workspace_id, project_id, id) ON DELETE CASCADE;
            END IF;
        END $$
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS alertmanager_alerts (
            id UUID PRIMARY KEY,
            workspace_id UUID NOT NULL,
            project_id UUID NOT NULL,
            integration_id UUID NOT NULL,
            service_id UUID NOT NULL,
            incident_id UUID,
            fingerprint VARCHAR(128) NOT NULL,
            alertname VARCHAR(240) NOT NULL,
            status VARCHAR(20) NOT NULL,
            severity VARCHAR(20) NOT NULL,
            starts_at TIMESTAMPTZ NOT NULL,
            ends_at TIMESTAMPTZ,
            labels JSON NOT NULL DEFAULT '{}'::json,
            annotations JSON NOT NULL DEFAULT '{}'::json,
            last_received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_alertmanager_integration_fingerprint UNIQUE (integration_id, fingerprint),
            CONSTRAINT fk_alertmanager_integration_scope FOREIGN KEY (workspace_id, project_id, integration_id)
                REFERENCES integrations(workspace_id, project_id, id) ON DELETE CASCADE,
            CONSTRAINT fk_alertmanager_service_scope FOREIGN KEY (workspace_id, project_id, service_id)
                REFERENCES services(workspace_id, project_id, id),
            CONSTRAINT fk_alertmanager_incident_scope FOREIGN KEY (workspace_id, project_id, incident_id)
                REFERENCES incidents(workspace_id, project_id, id),
            CONSTRAINT ck_alertmanager_alert_status CHECK (status IN ('firing','resolved')),
            CONSTRAINT ck_alertmanager_alert_severity CHECK (severity IN ('SEV-1','SEV-2','SEV-3','SEV-4'))
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_alertmanager_project_status ON alertmanager_alerts(project_id, status, last_received_at)")


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_alertmanager_project_status")
    op.execute("DROP TABLE IF EXISTS alertmanager_alerts")
    op.execute("ALTER TABLE integration_sync_runs DROP CONSTRAINT IF EXISTS fk_sync_run_incident_scope")
    op.execute("ALTER TABLE integration_sync_runs DROP COLUMN IF EXISTS details")
    op.execute("ALTER TABLE integration_sync_runs DROP COLUMN IF EXISTS result_count")
    op.execute("ALTER TABLE integration_sync_runs DROP COLUMN IF EXISTS source_type")
    op.execute("ALTER TABLE integration_sync_runs DROP COLUMN IF EXISTS incident_id")
    op.execute("ALTER TABLE evidence_items DROP CONSTRAINT IF EXISTS fk_evidence_integration_scope")
    op.execute("ALTER TABLE evidence_items DROP CONSTRAINT IF EXISTS fk_evidence_service_scope")
    op.execute("ALTER TABLE evidence_items DROP CONSTRAINT IF EXISTS uq_evidence_external_source")
    op.execute("ALTER TABLE evidence_items DROP COLUMN IF EXISTS collection_metadata")
    op.execute("ALTER TABLE evidence_items DROP COLUMN IF EXISTS external_id")
    op.execute("ALTER TABLE evidence_items DROP COLUMN IF EXISTS provider")
    op.execute("ALTER TABLE evidence_items DROP COLUMN IF EXISTS integration_id")
    op.execute("ALTER TABLE evidence_items DROP COLUMN IF EXISTS service_id")
