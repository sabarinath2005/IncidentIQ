from app.db import Base
import app.models as models
from sqlalchemy import UniqueConstraint


def test_all_domain_tables_are_registered():
    expected = {
        "workspaces", "users", "workspace_members", "teams", "team_members", "projects",
        "project_members", "project_teams", "services", "service_dependencies", "issues",
        "issue_comments", "issue_activities", "incidents", "incident_services", "evidence_items",
        "incident_activities", "integration_sync_runs",
        "investigation_runs", "investigation_run_attempts", "investigation_run_source_statuses", "investigation_run_evidence",
        "investigation_run_context_references", "investigation_steps", "investigation_step_attempts",
        "findings", "finding_evidence_links", "finding_context_links", "recommendations",
        "approval_requests", "remediation_executions", "incident_issue_links", "integrations", "alertmanager_alerts",
        "knowledge_documents", "document_chunks", "outbox_events", "audit_events",
        "github_repository_mappings", "github_deliveries", "jira_project_mappings", "external_issue_links",
    }
    assert expected <= set(Base.metadata.tables)


def test_enterprise_service_and_integration_scope_contracts():
    service = models.Service.__table__
    assert {"description", "criticality", "technology", "owner_team_id"} <= set(service.columns.keys())
    assert any(tuple(c.name for c in constraint.columns) == ("workspace_id", "project_id", "id")
               for constraint in service.constraints if isinstance(constraint, UniqueConstraint))
    integration = models.Integration.__table__
    assert {"enabled", "permission_scope", "health_status", "last_successful_sync_at", "last_error", "credentials_ciphertext", "last_checked_at"} <= set(integration.columns.keys())
    assert any(tuple(c.name for c in constraint.columns) == ("project_id", "provider")
               for constraint in integration.constraints if isinstance(constraint, UniqueConstraint))


def test_run_specific_evidence_and_single_active_run_constraints_exist():
    run_evidence = models.InvestigationRunEvidence.__table__
    fks = [tuple(element.parent.name for element in fk.elements) for fk in run_evidence.foreign_key_constraints]
    assert ("workspace_id", "project_id", "incident_id", "run_id") in fks
    assert ("workspace_id", "project_id", "incident_id", "evidence_id") in fks
    active_indexes = [idx for idx in models.InvestigationRun.__table__.indexes if idx.name == "uq_one_active_run_per_incident"]
    assert len(active_indexes) == 1 and active_indexes[0].unique


def test_source_gaps_are_not_evidence_items_and_historical_context_is_project_scoped():
    assert models.InvestigationRunSourceStatus.__tablename__ != models.EvidenceItem.__tablename__
    context = models.InvestigationRunContextReference.__table__
    assert "ck_context_same_project" in {constraint.name for constraint in context.constraints}
    assert "ck_context_source_kind" in {constraint.name for constraint in context.constraints}
    link_columns = set(models.FindingEvidenceLink.__table__.columns.keys())
    assert {"run_id", "evidence_id", "finding_id"} <= link_columns


def test_incident_identity_models_distinguish_users_from_systems():
    incident = models.Incident.__table__
    activity = models.IncidentActivity.__table__
    assert incident.c.reporter_id.nullable
    assert incident.c.reporter_type.server_default.arg == "user"
    assert "ck_incident_reporter_identity" in {constraint.name for constraint in incident.constraints}
    assert activity.c.actor_id.nullable
    assert activity.c.actor_type.server_default.arg == "user"
    assert "ck_incident_activity_actor_identity" in {constraint.name for constraint in activity.constraints}


def test_every_composite_foreign_key_targets_a_unique_key():
    for table in Base.metadata.tables.values():
        for foreign_key in table.foreign_key_constraints:
            target_columns = tuple(element.column.name for element in foreign_key.elements)
            target_table = foreign_key.elements[0].column.table
            unique_keys = [tuple(column.name for column in target_table.primary_key.columns)]
            unique_keys += [
                tuple(column.name for column in constraint.columns)
                for constraint in target_table.constraints
                if isinstance(constraint, UniqueConstraint)
            ]
            assert target_columns in unique_keys, (table.name, target_table.name, target_columns)


def test_ai_and_knowledge_models_record_real_provider_and_source_provenance():
    run_columns = set(models.InvestigationRun.__table__.columns.keys())
    assert {"provider_name", "provider_mode", "model_version", "model_duration_ms", "input_tokens", "output_tokens"} <= run_columns
    assert models.DocumentChunk.__table__.c.embedding.type.dim == 64
    assert models.DocumentChunk.__table__.c.embedding_1536.type.dim == 1536
    assert {"embedding_1536_model", "embedding_1536_dimensions"} <= set(models.DocumentChunk.__table__.columns.keys())
    assert {"status", "processing_error", "version", "chunk_count", "indexed_at"} <= set(models.KnowledgeDocument.__table__.columns.keys())
    assert "source_chunk_id" in models.InvestigationRunContextReference.__table__.columns


def test_external_integrations_have_scoped_idempotency_and_approval_linkage():
    github = models.GitHubDelivery.__table__
    assert any(constraint.name == "uq_github_delivery" for constraint in github.constraints)
    jira = models.ExternalIssueLink.__table__
    assert any(constraint.name == "uq_external_issue_recommendation_integration" for constraint in jira.constraints)
    assert any(tuple(element.parent.name for element in fk.elements) == ("workspace_id", "project_id", "recommendation_id")
               for fk in jira.foreign_key_constraints)
