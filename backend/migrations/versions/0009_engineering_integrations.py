"""Add scoped GitHub event/repository and approved Jira issue links."""
from alembic import op

revision = "0009_engineering_integrations"
down_revision = "0008_real_knowledge_embeddings"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE deployments ADD COLUMN IF NOT EXISTS provider VARCHAR(80) NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE deployments ADD COLUMN IF NOT EXISTS external_id VARCHAR(300) NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE deployments ADD COLUMN IF NOT EXISTS repository_full_name VARCHAR(240) NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE deployments ADD COLUMN IF NOT EXISTS correlation_reason TEXT NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE deployments ADD COLUMN IF NOT EXISTS deployment_status VARCHAR(40) NOT NULL DEFAULT 'unknown'")
    op.execute("""CREATE TABLE IF NOT EXISTS github_repository_mappings (
        id UUID PRIMARY KEY, workspace_id UUID NOT NULL, project_id UUID NOT NULL,
        integration_id UUID NOT NULL, service_id UUID, repository_id VARCHAR(80) NOT NULL,
        repository_full_name VARCHAR(240) NOT NULL, created_at TIMESTAMPTZ NOT NULL,
        CONSTRAINT uq_github_integration_repository UNIQUE (integration_id, repository_id),
        CONSTRAINT fk_github_repository_integration FOREIGN KEY (workspace_id,project_id,integration_id)
            REFERENCES integrations(workspace_id,project_id,id) ON DELETE CASCADE,
        CONSTRAINT fk_github_repository_service FOREIGN KEY (workspace_id,project_id,service_id)
            REFERENCES services(workspace_id,project_id,id) ON DELETE CASCADE
    )""")
    op.execute("""CREATE TABLE IF NOT EXISTS github_deliveries (
        id UUID PRIMARY KEY, workspace_id UUID NOT NULL, project_id UUID NOT NULL, integration_id UUID NOT NULL,
        delivery_id VARCHAR(200) NOT NULL, event_type VARCHAR(40) NOT NULL,
        repository_id VARCHAR(80) NOT NULL DEFAULT '', repository_full_name VARCHAR(240) NOT NULL DEFAULT '',
        external_id VARCHAR(300) NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',
        occurred_at TIMESTAMPTZ NOT NULL, received_at TIMESTAMPTZ NOT NULL,
        correlation_reason TEXT NOT NULL DEFAULT '', status VARCHAR(24) NOT NULL DEFAULT 'received',
        CONSTRAINT uq_github_delivery UNIQUE (integration_id,delivery_id),
        CONSTRAINT fk_github_delivery_integration FOREIGN KEY (workspace_id,project_id,integration_id)
            REFERENCES integrations(workspace_id,project_id,id) ON DELETE CASCADE,
        CONSTRAINT ck_github_delivery_status CHECK (status IN ('received','unmapped','correlated','ignored'))
    )""")
    op.execute("CREATE INDEX IF NOT EXISTS ix_github_delivery_project_time ON github_deliveries (project_id,occurred_at)")
    op.execute("""CREATE TABLE IF NOT EXISTS jira_project_mappings (
        id UUID PRIMARY KEY, workspace_id UUID NOT NULL, project_id UUID NOT NULL, integration_id UUID NOT NULL,
        jira_project_key VARCHAR(40) NOT NULL, jira_project_name VARCHAR(240) NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL, CONSTRAINT uq_jira_mapping_per_integration UNIQUE (integration_id),
        CONSTRAINT fk_jira_mapping_integration FOREIGN KEY (workspace_id,project_id,integration_id)
            REFERENCES integrations(workspace_id,project_id,id) ON DELETE CASCADE
    )""")
    op.execute("""CREATE TABLE IF NOT EXISTS external_issue_links (
        id UUID PRIMARY KEY, workspace_id UUID NOT NULL, project_id UUID NOT NULL, incident_id UUID NOT NULL,
        recommendation_id UUID NOT NULL, integration_id UUID NOT NULL, idempotency_key VARCHAR(64) NOT NULL,
        jira_project_key VARCHAR(40) NOT NULL, external_issue_id VARCHAR(80) NOT NULL DEFAULT '',
        external_issue_key VARCHAR(80) NOT NULL DEFAULT '', external_issue_url VARCHAR(1000) NOT NULL DEFAULT '',
        external_status VARCHAR(80) NOT NULL DEFAULT '', last_synced_at TIMESTAMPTZ,
        status VARCHAR(24) NOT NULL DEFAULT 'pending', sync_error TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
        CONSTRAINT uq_external_issue_recommendation_integration UNIQUE (recommendation_id,integration_id),
        CONSTRAINT uq_external_issue_idempotency UNIQUE (integration_id,idempotency_key),
        CONSTRAINT fk_external_issue_incident FOREIGN KEY (workspace_id,project_id,incident_id)
            REFERENCES incidents(workspace_id,project_id,id) ON DELETE CASCADE,
        CONSTRAINT fk_external_issue_recommendation FOREIGN KEY (workspace_id,project_id,recommendation_id)
            REFERENCES recommendations(workspace_id,project_id,id) ON DELETE CASCADE,
        CONSTRAINT fk_external_issue_integration FOREIGN KEY (workspace_id,project_id,integration_id)
            REFERENCES integrations(workspace_id,project_id,id) ON DELETE CASCADE,
        CONSTRAINT ck_external_issue_link_status CHECK (status IN ('pending','created','failed'))
    )""")
    op.execute("ALTER TABLE external_issue_links ADD COLUMN IF NOT EXISTS external_status VARCHAR(80) NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE external_issue_links ADD COLUMN IF NOT EXISTS last_synced_at TIMESTAMPTZ")


def downgrade():
    op.execute("ALTER TABLE external_issue_links DROP COLUMN IF EXISTS last_synced_at")
    op.execute("ALTER TABLE external_issue_links DROP COLUMN IF EXISTS external_status")
    op.execute("DROP TABLE IF EXISTS external_issue_links")
    op.execute("DROP TABLE IF EXISTS jira_project_mappings")
    op.execute("DROP TABLE IF EXISTS github_deliveries")
    op.execute("DROP TABLE IF EXISTS github_repository_mappings")
    op.execute("ALTER TABLE deployments DROP COLUMN IF EXISTS correlation_reason")
    op.execute("ALTER TABLE deployments DROP COLUMN IF EXISTS deployment_status")
    op.execute("ALTER TABLE deployments DROP COLUMN IF EXISTS repository_full_name")
    op.execute("ALTER TABLE deployments DROP COLUMN IF EXISTS external_id")
    op.execute("ALTER TABLE deployments DROP COLUMN IF EXISTS provider")
