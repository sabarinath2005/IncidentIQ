"""Persist AI provider mode and model execution metadata."""
from alembic import op

revision = "0007_ai_provider_metadata"
down_revision = "0006_system_identity"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE investigation_runs ADD COLUMN IF NOT EXISTS provider_name VARCHAR(40) NOT NULL DEFAULT 'unknown'")
    op.execute("ALTER TABLE investigation_runs ADD COLUMN IF NOT EXISTS provider_mode VARCHAR(24) NOT NULL DEFAULT 'unknown'")
    op.execute("ALTER TABLE investigation_runs ADD COLUMN IF NOT EXISTS model_version VARCHAR(160) NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE investigation_runs ADD COLUMN IF NOT EXISTS model_duration_ms INTEGER")
    op.execute("ALTER TABLE investigation_runs ADD COLUMN IF NOT EXISTS input_tokens INTEGER")
    op.execute("ALTER TABLE investigation_runs ADD COLUMN IF NOT EXISTS output_tokens INTEGER")
    op.execute("ALTER TABLE investigation_runs ADD COLUMN IF NOT EXISTS estimated_cost_usd DOUBLE PRECISION")
    op.execute("ALTER TABLE findings ADD COLUMN IF NOT EXISTS details_json JSON NOT NULL DEFAULT '{}'::json")
    op.execute("ALTER TABLE recommendations ADD COLUMN IF NOT EXISTS expected_benefit TEXT NOT NULL DEFAULT ''")
    op.execute("ALTER TABLE recommendations ADD COLUMN IF NOT EXISTS evidence_refs JSON NOT NULL DEFAULT '[]'::json")
    op.execute("ALTER TABLE recommendations ADD COLUMN IF NOT EXISTS required_permissions JSON NOT NULL DEFAULT '[]'::json")
    op.execute("ALTER TABLE recommendations ADD COLUMN IF NOT EXISTS verification_steps JSON NOT NULL DEFAULT '[]'::json")


def downgrade():
    op.execute("ALTER TABLE recommendations DROP COLUMN IF EXISTS verification_steps")
    op.execute("ALTER TABLE recommendations DROP COLUMN IF EXISTS required_permissions")
    op.execute("ALTER TABLE recommendations DROP COLUMN IF EXISTS evidence_refs")
    op.execute("ALTER TABLE recommendations DROP COLUMN IF EXISTS expected_benefit")
    op.execute("ALTER TABLE findings DROP COLUMN IF EXISTS details_json")
    op.execute("ALTER TABLE investigation_runs DROP COLUMN IF EXISTS estimated_cost_usd")
    op.execute("ALTER TABLE investigation_runs DROP COLUMN IF EXISTS output_tokens")
    op.execute("ALTER TABLE investigation_runs DROP COLUMN IF EXISTS input_tokens")
    op.execute("ALTER TABLE investigation_runs DROP COLUMN IF EXISTS model_duration_ms")
    op.execute("ALTER TABLE investigation_runs DROP COLUMN IF EXISTS model_version")
    op.execute("ALTER TABLE investigation_runs DROP COLUMN IF EXISTS provider_mode")
    op.execute("ALTER TABLE investigation_runs DROP COLUMN IF EXISTS provider_name")
