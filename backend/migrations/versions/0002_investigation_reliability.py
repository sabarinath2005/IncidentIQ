"""Durable run leases, execution-attempt history, step integrity, and run-scoped recommendations.

The original baseline creates its tables from current SQLAlchemy metadata. This
revision therefore uses PostgreSQL IF NOT EXISTS guards so it upgrades both
databases created by that baseline before these model changes and fresh databases
where the baseline already sees the expanded metadata.
"""
from alembic import op

revision = "0002_investigation_reliability"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE investigation_runs ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE investigation_runs ADD COLUMN IF NOT EXISTS lease_owner VARCHAR(160)")
    op.execute("ALTER TABLE investigation_runs ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ")

    op.execute("""
        CREATE TABLE IF NOT EXISTS investigation_run_attempts (
            id UUID PRIMARY KEY,
            workspace_id UUID NOT NULL,
            project_id UUID NOT NULL,
            incident_id UUID NOT NULL,
            run_id UUID NOT NULL,
            attempt_no INTEGER NOT NULL,
            worker_id VARCHAR(160) NOT NULL,
            celery_task_id VARCHAR(160) NOT NULL DEFAULT '',
            status VARCHAR(24) NOT NULL DEFAULT 'running',
            started_at TIMESTAMPTZ NOT NULL,
            completed_at TIMESTAMPTZ,
            error_type VARCHAR(200) NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '',
            CONSTRAINT uq_run_attempt_no UNIQUE (run_id, attempt_no),
            CONSTRAINT fk_run_attempt_run FOREIGN KEY (workspace_id, project_id, incident_id, run_id)
                REFERENCES investigation_runs (workspace_id, project_id, incident_id, id) ON DELETE CASCADE,
            CONSTRAINT ck_run_attempt_no_positive CHECK (attempt_no > 0),
            CONSTRAINT ck_run_attempt_status CHECK (status IN ('running','retrying','succeeded','failed','lease_expired','cancelled'))
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_run_attempts_run_started ON investigation_run_attempts (run_id, started_at)")

    op.execute("ALTER TABLE investigation_steps ALTER COLUMN latest_attempt_no DROP NOT NULL")
    op.execute("UPDATE investigation_steps SET latest_attempt_no = NULL WHERE latest_attempt_no = 0")
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_investigation_step_status' AND conrelid = 'investigation_steps'::regclass) THEN
                ALTER TABLE investigation_steps ADD CONSTRAINT ck_investigation_step_status
                CHECK (status IN ('pending','running','retrying','succeeded','failed','cancelled'));
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_step_latest_attempt_positive' AND conrelid = 'investigation_steps'::regclass) THEN
                ALTER TABLE investigation_steps ADD CONSTRAINT ck_step_latest_attempt_positive
                CHECK (latest_attempt_no IS NULL OR latest_attempt_no > 0);
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_step_latest_success_positive' AND conrelid = 'investigation_steps'::regclass) THEN
                ALTER TABLE investigation_steps ADD CONSTRAINT ck_step_latest_success_positive
                CHECK (latest_successful_attempt_no IS NULL OR latest_successful_attempt_no > 0);
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_step_attempt_step' AND conrelid = 'investigation_step_attempts'::regclass) THEN
                ALTER TABLE investigation_step_attempts ADD CONSTRAINT fk_step_attempt_step
                FOREIGN KEY (run_id, step_key) REFERENCES investigation_steps (run_id, step_key) ON DELETE CASCADE;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_step_latest_attempt' AND conrelid = 'investigation_steps'::regclass) THEN
                ALTER TABLE investigation_steps ADD CONSTRAINT fk_step_latest_attempt
                FOREIGN KEY (run_id, step_key, latest_attempt_no)
                REFERENCES investigation_step_attempts (run_id, step_key, attempt_no);
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_step_latest_successful_attempt' AND conrelid = 'investigation_steps'::regclass) THEN
                ALTER TABLE investigation_steps ADD CONSTRAINT fk_step_latest_successful_attempt
                FOREIGN KEY (run_id, step_key, latest_successful_attempt_no)
                REFERENCES investigation_step_attempts (run_id, step_key, attempt_no);
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_step_attempt_no_positive' AND conrelid = 'investigation_step_attempts'::regclass) THEN
                ALTER TABLE investigation_step_attempts ADD CONSTRAINT ck_step_attempt_no_positive CHECK (attempt_no > 0);
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_step_attempt_status' AND conrelid = 'investigation_step_attempts'::regclass) THEN
                ALTER TABLE investigation_step_attempts ADD CONSTRAINT ck_step_attempt_status
                CHECK (status IN ('running','retrying','succeeded','failed','cancelled'));
            END IF;
        END; $$
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_step_attempt_history ON investigation_step_attempts (run_id, step_key, attempt_no)")

    op.execute("ALTER TABLE recommendations ADD COLUMN IF NOT EXISTS run_id UUID")
    op.execute("ALTER TABLE recommendations ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(64)")
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_finding_review_status' AND conrelid = 'findings'::regclass) THEN
                ALTER TABLE findings ADD CONSTRAINT ck_finding_review_status
                CHECK (review_status IN ('pending','confirmed','rejected','needs_more_evidence'));
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_recommendation_run_key' AND conrelid = 'recommendations'::regclass) THEN
                ALTER TABLE recommendations ADD CONSTRAINT uq_recommendation_run_key UNIQUE (run_id, idempotency_key);
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_recommendation_run' AND conrelid = 'recommendations'::regclass) THEN
                ALTER TABLE recommendations ADD CONSTRAINT fk_recommendation_run
                FOREIGN KEY (workspace_id, project_id, incident_id, run_id)
                REFERENCES investigation_runs (workspace_id, project_id, incident_id, id);
            END IF;
        END; $$
    """)


def downgrade():
    op.execute("ALTER TABLE recommendations DROP CONSTRAINT IF EXISTS fk_recommendation_run")
    op.execute("ALTER TABLE recommendations DROP CONSTRAINT IF EXISTS uq_recommendation_run_key")
    op.execute("ALTER TABLE recommendations DROP COLUMN IF EXISTS idempotency_key")
    op.execute("ALTER TABLE recommendations DROP COLUMN IF EXISTS run_id")
    op.execute("ALTER TABLE findings DROP CONSTRAINT IF EXISTS ck_finding_review_status")
    op.execute("ALTER TABLE investigation_step_attempts DROP CONSTRAINT IF EXISTS fk_step_attempt_step")
    op.execute("ALTER TABLE investigation_steps DROP CONSTRAINT IF EXISTS fk_step_latest_attempt")
    op.execute("ALTER TABLE investigation_steps DROP CONSTRAINT IF EXISTS fk_step_latest_successful_attempt")
    op.execute("ALTER TABLE investigation_step_attempts DROP CONSTRAINT IF EXISTS ck_step_attempt_no_positive")
    op.execute("ALTER TABLE investigation_step_attempts DROP CONSTRAINT IF EXISTS ck_step_attempt_status")
    op.execute("ALTER TABLE investigation_steps DROP CONSTRAINT IF EXISTS ck_investigation_step_status")
    op.execute("ALTER TABLE investigation_steps DROP CONSTRAINT IF EXISTS ck_step_latest_attempt_positive")
    op.execute("ALTER TABLE investigation_steps DROP CONSTRAINT IF EXISTS ck_step_latest_success_positive")
    op.execute("UPDATE investigation_steps SET latest_attempt_no = 0 WHERE latest_attempt_no IS NULL")
    op.execute("ALTER TABLE investigation_steps ALTER COLUMN latest_attempt_no SET NOT NULL")
    op.execute("DROP INDEX IF EXISTS ix_step_attempt_history")
    op.execute("DROP INDEX IF EXISTS ix_run_attempts_run_started")
    op.execute("DROP TABLE IF EXISTS investigation_run_attempts")
    op.execute("ALTER TABLE investigation_runs DROP COLUMN IF EXISTS lease_expires_at")
    op.execute("ALTER TABLE investigation_runs DROP COLUMN IF EXISTS lease_owner")
    op.execute("ALTER TABLE investigation_runs DROP COLUMN IF EXISTS retry_count")
