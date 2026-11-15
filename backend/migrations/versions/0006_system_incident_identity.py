"""Represent system-generated incident reporters and timeline actors explicitly."""
from alembic import op


revision = "0006_system_identity"
down_revision = "0005_observability_alerts"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE incidents ALTER COLUMN reporter_id DROP NOT NULL")
    # Revision 0001 builds its baseline from current model metadata. These
    # columns/checks may already exist on a clean install, while older 0004
    # databases need them added here.
    op.execute("ALTER TABLE incidents ADD COLUMN IF NOT EXISTS reporter_type VARCHAR(20) NOT NULL DEFAULT 'user'")
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_incident_reporter_identity'
                           AND conrelid='incidents'::regclass) THEN
                ALTER TABLE incidents ADD CONSTRAINT ck_incident_reporter_identity
                CHECK ((reporter_type = 'user' AND reporter_id IS NOT NULL)
                    OR (reporter_type = 'system' AND reporter_id IS NULL));
            END IF;
        END $$
    """)

    op.execute("ALTER TABLE incident_activities ALTER COLUMN actor_id DROP NOT NULL")
    op.execute("ALTER TABLE incident_activities ADD COLUMN IF NOT EXISTS actor_type VARCHAR(20) NOT NULL DEFAULT 'user'")
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_incident_activity_actor_identity'
                           AND conrelid='incident_activities'::regclass) THEN
                ALTER TABLE incident_activities ADD CONSTRAINT ck_incident_activity_actor_identity
                CHECK ((actor_type = 'user' AND actor_id IS NOT NULL)
                    OR (actor_type = 'system' AND actor_id IS NULL));
            END IF;
        END $$
    """)


def downgrade():
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM incidents WHERE reporter_type = 'system') THEN
                RAISE EXCEPTION 'Cannot downgrade while system-reported incidents exist';
            END IF;
            IF EXISTS (SELECT 1 FROM incident_activities WHERE actor_type = 'system') THEN
                RAISE EXCEPTION 'Cannot downgrade while system-authored incident activity exists';
            END IF;
        END $$
    """)
    op.execute("ALTER TABLE incident_activities DROP CONSTRAINT ck_incident_activity_actor_identity")
    op.execute("ALTER TABLE incident_activities DROP COLUMN actor_type")
    op.execute("ALTER TABLE incident_activities ALTER COLUMN actor_id SET NOT NULL")
    op.execute("ALTER TABLE incidents DROP CONSTRAINT ck_incident_reporter_identity")
    op.execute("ALTER TABLE incidents DROP COLUMN reporter_type")
    op.execute("ALTER TABLE incidents ALTER COLUMN reporter_id SET NOT NULL")
