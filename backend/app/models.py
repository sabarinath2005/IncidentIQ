"""Relational domain model. Scope columns are repeated intentionally for composite FKs."""
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, Float, ForeignKey, ForeignKeyConstraint,
    Index, Integer, JSON, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import Uuid
from pgvector.sqlalchemy import Vector
from app.db import Base
from app.run_lifecycle import RUN_ACTIVE_STATUSES, status_check_sql


def utcnow():
    return datetime.now(timezone.utc)


class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[object] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class User(Base):
    __tablename__ = "users"
    id: Mapped[object] = mapped_column(Uuid, primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(160), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkspaceMember(Base):
    __tablename__ = "workspace_members"
    workspace_id: Mapped[object] = mapped_column(Uuid, ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[object] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(20), default="member")
    __table_args__ = (CheckConstraint("role in ('owner','admin','member')"),)


class Team(Base):
    __tablename__ = "teams"
    id: Mapped[object] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[object] = mapped_column(Uuid, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    __table_args__ = (UniqueConstraint("workspace_id", "id"), UniqueConstraint("workspace_id", "name"))


class TeamMember(Base):
    __tablename__ = "team_members"
    workspace_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    team_id: Mapped[object] = mapped_column(Uuid, primary_key=True)
    user_id: Mapped[object] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(30), default="member")
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "team_id"], ["teams.workspace_id", "teams.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["workspace_id", "user_id"], ["workspace_members.workspace_id", "workspace_members.user_id"], ondelete="CASCADE"),
    )


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[object] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[object] = mapped_column(Uuid, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("workspace_id", "id"), UniqueConstraint("workspace_id", "name"))


class ProjectMember(Base):
    __tablename__ = "project_members"
    workspace_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    project_id: Mapped[object] = mapped_column(Uuid, primary_key=True)
    user_id: Mapped[object] = mapped_column(Uuid, primary_key=True)
    role: Mapped[str] = mapped_column(String(20), default="member")
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "project_id"], ["projects.workspace_id", "projects.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["workspace_id", "user_id"], ["workspace_members.workspace_id", "workspace_members.user_id"], ondelete="CASCADE"),
        CheckConstraint("role in ('admin','member','engineer','incident_manager','viewer')"),
    )


class ProjectTeam(Base):
    __tablename__ = "project_teams"
    workspace_id: Mapped[object] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[object] = mapped_column(Uuid, primary_key=True)
    team_id: Mapped[object] = mapped_column(Uuid, primary_key=True)
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "project_id"], ["projects.workspace_id", "projects.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["workspace_id", "team_id"], ["teams.workspace_id", "teams.id"], ondelete="CASCADE"),
    )


class Service(Base):
    __tablename__ = "services"
    id: Mapped[object] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    project_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    criticality: Mapped[str] = mapped_column(String(20), default="medium")
    technology: Mapped[dict] = mapped_column(JSON, default=dict)
    environment: Mapped[str] = mapped_column(String(80), default="demo")
    owner_team_id: Mapped[object | None] = mapped_column(Uuid)
    health: Mapped[str] = mapped_column(String(30), default="unknown")
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("workspace_id", "project_id", "id"),
        UniqueConstraint("project_id", "environment", "name"),
        CheckConstraint("criticality in ('critical','high','medium','low')", name="ck_service_criticality"),
        ForeignKeyConstraint(["workspace_id", "project_id"], ["projects.workspace_id", "projects.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["workspace_id", "owner_team_id"], ["teams.workspace_id", "teams.id"]),
    )


class ServiceDependency(Base):
    __tablename__ = "service_dependencies"
    id: Mapped[object] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    project_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    source_service_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    target_service_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    relationship_type: Mapped[str] = mapped_column(String(40), default="calls")
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "project_id", "source_service_id"], ["services.workspace_id", "services.project_id", "services.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["workspace_id", "project_id", "target_service_id"], ["services.workspace_id", "services.project_id", "services.id"], ondelete="CASCADE"),
        CheckConstraint("source_service_id <> target_service_id"),
        UniqueConstraint("source_service_id", "target_service_id", "relationship_type"),
    )


class Issue(Base):
    __tablename__ = "issues"
    id: Mapped[object] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    project_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    key: Mapped[str] = mapped_column(String(32), nullable=False)
    type: Mapped[str] = mapped_column(String(30), default="Bug")
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="Open")
    priority: Mapped[str] = mapped_column(String(20), default="Medium")
    reporter_id: Mapped[object] = mapped_column(Uuid, ForeignKey("users.id"), nullable=False)
    assignee_id: Mapped[object | None] = mapped_column(Uuid)
    service_id: Mapped[object | None] = mapped_column(Uuid)
    due_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    labels: Mapped[list] = mapped_column(JSON, default=list)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    __table_args__ = (
        UniqueConstraint("workspace_id", "project_id", "id"),
        UniqueConstraint("project_id", "key"),
        ForeignKeyConstraint(["workspace_id", "project_id"], ["projects.workspace_id", "projects.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["workspace_id", "project_id", "service_id"], ["services.workspace_id", "services.project_id", "services.id"]),
        ForeignKeyConstraint(["project_id", "reporter_id"], ["project_members.project_id", "project_members.user_id"]),
        ForeignKeyConstraint(["project_id", "assignee_id"], ["project_members.project_id", "project_members.user_id"]),
        CheckConstraint("type in ('Bug','Task','Story','Improvement')"),
        CheckConstraint("status in ('Open','In Progress','In Review','Resolved','Closed')"),
        CheckConstraint("priority in ('Critical','High','Medium','Low')"),
        Index("ix_issue_project_status_priority", "project_id", "status", "priority", "updated_at"),
        Index("ix_issue_project_assignee_status", "project_id", "assignee_id", "status"),
    )


class IssueComment(Base):
    __tablename__ = "issue_comments"
    id: Mapped[object] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    project_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    issue_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    author_id: Mapped[object] = mapped_column(Uuid, ForeignKey("users.id"), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "project_id", "issue_id"], ["issues.workspace_id", "issues.project_id", "issues.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["project_id", "author_id"], ["project_members.project_id", "project_members.user_id"]),
    )


class IssueActivity(Base):
    __tablename__ = "issue_activities"
    id: Mapped[object] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    project_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    issue_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    actor_id: Mapped[object] = mapped_column(Uuid, ForeignKey("users.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "project_id", "issue_id"], ["issues.workspace_id", "issues.project_id", "issues.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["project_id", "actor_id"], ["project_members.project_id", "project_members.user_id"]),
    )


class Incident(Base):
    __tablename__ = "incidents"
    id: Mapped[object] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    project_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    severity: Mapped[str] = mapped_column(String(20), default="SEV-3")
    status: Mapped[str] = mapped_column(String(30), default="open")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    assignee_id: Mapped[object | None] = mapped_column(Uuid)
    impact: Mapped[str] = mapped_column(Text, default="")
    reporter_id: Mapped[object | None] = mapped_column(Uuid, ForeignKey("users.id"))
    reporter_type: Mapped[str] = mapped_column(String(20), default="user", server_default="user", nullable=False)
    resolution_notes: Mapped[str] = mapped_column(Text, default="")
    confirmed_root_cause: Mapped[str] = mapped_column(Text, default="")
    demo_scenario_key: Mapped[str | None] = mapped_column(String(80))
    __table_args__ = (
        UniqueConstraint("workspace_id", "project_id", "id"),
        UniqueConstraint("project_id", "demo_scenario_key"),
        ForeignKeyConstraint(["workspace_id", "project_id"], ["projects.workspace_id", "projects.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["project_id", "reporter_id"], ["project_members.project_id", "project_members.user_id"]),
        ForeignKeyConstraint(["project_id", "assignee_id"], ["project_members.project_id", "project_members.user_id"]),
        CheckConstraint("severity in ('SEV-1','SEV-2','SEV-3','SEV-4')"),
        CheckConstraint("status in ('open','acknowledged','investigating','mitigated','resolved','closed')"),
        CheckConstraint("(reporter_type = 'user' AND reporter_id IS NOT NULL) OR (reporter_type = 'system' AND reporter_id IS NULL)", name="ck_incident_reporter_identity"),
        Index("ix_incident_project_queue", "project_id", "status", "severity", "started_at"),
    )


class IncidentActivity(Base):
    __tablename__ = "incident_activities"
    id: Mapped[object] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    project_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    incident_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    actor_id: Mapped[object | None] = mapped_column(Uuid, ForeignKey("users.id"))
    actor_type: Mapped[str] = mapped_column(String(20), default="user", server_default="user", nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "project_id", "incident_id"], ["incidents.workspace_id", "incidents.project_id", "incidents.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["project_id", "actor_id"], ["project_members.project_id", "project_members.user_id"]),
        CheckConstraint("(actor_type = 'user' AND actor_id IS NOT NULL) OR (actor_type = 'system' AND actor_id IS NULL)", name="ck_incident_activity_actor_identity"),
        Index("ix_incident_activity_timeline", "incident_id", "created_at"),
    )


class IncidentService(Base):
    __tablename__ = "incident_services"
    workspace_id: Mapped[object] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[object] = mapped_column(Uuid, primary_key=True)
    incident_id: Mapped[object] = mapped_column(Uuid, primary_key=True)
    service_id: Mapped[object] = mapped_column(Uuid, primary_key=True)
    impact_role: Mapped[str] = mapped_column(String(40), default="affected")
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "project_id", "incident_id"], ["incidents.workspace_id", "incidents.project_id", "incidents.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["workspace_id", "project_id", "service_id"], ["services.workspace_id", "services.project_id", "services.id"], ondelete="CASCADE"),
    )


class EvidenceItem(Base):
    __tablename__ = "evidence_items"
    id: Mapped[object] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    project_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    incident_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(240), default="manual")
    service_id: Mapped[object | None] = mapped_column(Uuid)
    integration_id: Mapped[object | None] = mapped_column(Uuid)
    provider: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    external_id: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    collection_metadata: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_key: Mapped[str | None] = mapped_column(String(500))
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        UniqueConstraint("workspace_id", "project_id", "incident_id", "id"),
        UniqueConstraint("incident_id", "source_type", "content_hash", name="uq_evidence_source_content"),
        UniqueConstraint("integration_id", "incident_id", "source_type", "external_id", name="uq_evidence_external_source"),
        ForeignKeyConstraint(["workspace_id", "project_id", "incident_id"], ["incidents.workspace_id", "incidents.project_id", "incidents.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["workspace_id", "project_id", "service_id"], ["services.workspace_id", "services.project_id", "services.id"], name="fk_evidence_service_scope"),
        ForeignKeyConstraint(["workspace_id", "project_id", "integration_id"], ["integrations.workspace_id", "integrations.project_id", "integrations.id"], name="fk_evidence_integration_scope"),
        Index("ix_evidence_incident_observed", "incident_id", "observed_at"),
    )


class Deployment(Base):
    __tablename__ = "deployments"
    id: Mapped[object] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    project_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    service_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    version: Mapped[str] = mapped_column(String(120), nullable=False)
    commit_ref: Mapped[str] = mapped_column(String(160), default="")
    deployed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    environment: Mapped[str] = mapped_column(String(80), default="demo")
    __table_args__ = (ForeignKeyConstraint(["workspace_id", "project_id", "service_id"], ["services.workspace_id", "services.project_id", "services.id"], ondelete="CASCADE"),)


class InvestigationRun(Base):
    __tablename__ = "investigation_runs"
    id: Mapped[object] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    project_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    incident_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="queued")
    requested_by: Mapped[object] = mapped_column(Uuid, ForeignKey("users.id"), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(100), unique=True)
    workflow_version: Mapped[str] = mapped_column(String(40), default="mvp-1")
    prompt_version: Mapped[str] = mapped_column(String(40), default="mock-1")
    model_id: Mapped[str] = mapped_column(String(120), default="mock-demo")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_summary: Mapped[str] = mapped_column(Text, default="")
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False, server_default="0")
    lease_owner: Mapped[str | None] = mapped_column(String(160))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        UniqueConstraint("workspace_id", "project_id", "incident_id", "id"),
        UniqueConstraint("workspace_id", "project_id", "idempotency_key"),
        ForeignKeyConstraint(["workspace_id", "project_id", "incident_id"], ["incidents.workspace_id", "incidents.project_id", "incidents.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["project_id", "requested_by"], ["project_members.project_id", "project_members.user_id"]),
        CheckConstraint(status_check_sql(), name="ck_investigation_run_status"),
        Index("uq_one_active_run_per_incident", "workspace_id", "project_id", "incident_id", unique=True,
              postgresql_where=(status.in_(RUN_ACTIVE_STATUSES))),
    )


class InvestigationRunAttempt(Base):
    __tablename__ = "investigation_run_attempts"
    id: Mapped[object] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    project_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    incident_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    run_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str] = mapped_column(String(160), nullable=False)
    celery_task_id: Mapped[str] = mapped_column(String(160), default="")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_type: Mapped[str] = mapped_column(String(200), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    __table_args__ = (
        UniqueConstraint("run_id", "attempt_no", name="uq_run_attempt_no"),
        ForeignKeyConstraint(
            ["workspace_id", "project_id", "incident_id", "run_id"],
            ["investigation_runs.workspace_id", "investigation_runs.project_id", "investigation_runs.incident_id", "investigation_runs.id"],
            ondelete="CASCADE", name="fk_run_attempt_run",
        ),
        CheckConstraint("attempt_no > 0", name="ck_run_attempt_no_positive"),
        CheckConstraint("status in ('running','retrying','succeeded','failed','lease_expired','cancelled')", name="ck_run_attempt_status"),
        Index("ix_run_attempts_run_started", "run_id", "started_at"),
    )


class InvestigationRunSourceStatus(Base):
    __tablename__ = "investigation_run_source_statuses"
    id: Mapped[object] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    project_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    incident_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    run_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False)
    source_instance_ref: Mapped[str] = mapped_column(String(200), default="demo")
    query_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    query_window_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    query_window_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    result_count: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(Text, default="")
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "project_id", "incident_id", "run_id"], ["investigation_runs.workspace_id", "investigation_runs.project_id", "investigation_runs.incident_id", "investigation_runs.id"], ondelete="CASCADE"),
        UniqueConstraint("run_id", "source_type", "source_instance_ref", "query_fingerprint", "attempt_no"),
        CheckConstraint("status in ('completed','unavailable','failed','skipped')"),
        CheckConstraint("attempt_no > 0 and result_count >= 0"),
        CheckConstraint("query_window_start is null or query_window_end is null or query_window_start <= query_window_end"),
        Index("ix_source_status_run_status", "run_id", "status"),
    )


class InvestigationRunEvidence(Base):
    __tablename__ = "investigation_run_evidence"
    id: Mapped[object] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    project_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    incident_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    run_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    evidence_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    collection_status: Mapped[str] = mapped_column(String(20), default="available")
    usage_type: Mapped[str] = mapped_column(String(30), nullable=False)
    __table_args__ = (
        UniqueConstraint("run_id", "evidence_id"),
        UniqueConstraint("workspace_id", "project_id", "incident_id", "run_id", "evidence_id"),
        ForeignKeyConstraint(["workspace_id", "project_id", "incident_id", "run_id"], ["investigation_runs.workspace_id", "investigation_runs.project_id", "investigation_runs.incident_id", "investigation_runs.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["workspace_id", "project_id", "incident_id", "evidence_id"], ["evidence_items.workspace_id", "evidence_items.project_id", "evidence_items.incident_id", "evidence_items.id"], ondelete="CASCADE"),
        CheckConstraint("collection_status = 'available'"),
        CheckConstraint("usage_type in ('preexisting_attached','collected_for_run')"),
        Index("ix_run_evidence_run_status", "run_id", "collection_status"),
    )


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"
    id: Mapped[object] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    project_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(300), default="manual")
    source_kind: Mapped[str] = mapped_column(String(40), default="runbook")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(Text, default="")
    __table_args__ = (
        UniqueConstraint("workspace_id", "project_id", "id"),
        ForeignKeyConstraint(["workspace_id", "project_id"], ["projects.workspace_id", "projects.id"], ondelete="CASCADE"),
    )


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    id: Mapped[object] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    project_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    document_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list | None] = mapped_column(Vector(64))
    embedding_model: Mapped[str] = mapped_column(String(120), default="demo-hash-v1")
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index"),
        ForeignKeyConstraint(["workspace_id", "project_id", "document_id"], ["knowledge_documents.workspace_id", "knowledge_documents.project_id", "knowledge_documents.id"], ondelete="CASCADE"),
        Index("ix_doc_chunks_project", "project_id", "document_id"),
        Index("ix_doc_chunks_embedding", "embedding", postgresql_using="hnsw", postgresql_ops={"embedding": "vector_cosine_ops"}),
    )


class InvestigationRunContextReference(Base):
    __tablename__ = "investigation_run_context_references"
    id: Mapped[object] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    project_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    incident_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    run_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    origin_workspace_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    origin_project_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    source_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    source_incident_id: Mapped[object | None] = mapped_column(Uuid)
    source_evidence_id: Mapped[object | None] = mapped_column(Uuid)
    source_document_id: Mapped[object | None] = mapped_column(Uuid)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    retrieval_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    retrieval_score: Mapped[float] = mapped_column(Float, nullable=False)
    retrieval_method: Mapped[str] = mapped_column(String(40), nullable=False)
    source_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_outcome_snapshot: Mapped[str] = mapped_column(Text, default="")
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "project_id", "incident_id", "run_id"], ["investigation_runs.workspace_id", "investigation_runs.project_id", "investigation_runs.incident_id", "investigation_runs.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["origin_workspace_id", "origin_project_id", "source_incident_id"], ["incidents.workspace_id", "incidents.project_id", "incidents.id"]),
        ForeignKeyConstraint(["origin_workspace_id", "origin_project_id", "source_incident_id", "source_evidence_id"], ["evidence_items.workspace_id", "evidence_items.project_id", "evidence_items.incident_id", "evidence_items.id"]),
        ForeignKeyConstraint(["origin_workspace_id", "origin_project_id", "source_document_id"], ["knowledge_documents.workspace_id", "knowledge_documents.project_id", "knowledge_documents.id"]),
        CheckConstraint("origin_workspace_id = workspace_id and origin_project_id = project_id", name="ck_context_same_project"),
        CheckConstraint("(source_kind='incident' and source_incident_id is not null and source_evidence_id is null and source_document_id is null) or (source_kind='evidence' and source_incident_id is not null and source_evidence_id is not null and source_document_id is null) or (source_kind='document' and source_incident_id is null and source_evidence_id is null and source_document_id is not null)", name="ck_context_source_kind"),
        UniqueConstraint("workspace_id", "project_id", "incident_id", "run_id", "id"),
        UniqueConstraint("run_id", "source_kind", "source_incident_id", "source_evidence_id", "source_document_id", name="uq_context_reference_source"),
        Index("ix_context_run_rank", "run_id", "retrieval_rank"),
        Index("uq_context_incident_source", "run_id", "source_incident_id", unique=True, postgresql_where=(source_kind == "incident")),
        Index("uq_context_evidence_source", "run_id", "source_evidence_id", unique=True, postgresql_where=(source_kind == "evidence")),
        Index("uq_context_document_source", "run_id", "source_document_id", unique=True, postgresql_where=(source_kind == "document")),
    )


class InvestigationStep(Base):
    __tablename__ = "investigation_steps"
    id: Mapped[object] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[object] = mapped_column(Uuid, ForeignKey("investigation_runs.id", ondelete="CASCADE"), nullable=False)
    step_key: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    latest_attempt_no: Mapped[int | None] = mapped_column(Integer)
    latest_successful_attempt_no: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    __table_args__ = (
        UniqueConstraint("run_id", "step_key", name="uq_investigation_step_run_key"),
        ForeignKeyConstraint(["run_id", "step_key", "latest_attempt_no"],
            ["investigation_step_attempts.run_id", "investigation_step_attempts.step_key", "investigation_step_attempts.attempt_no"],
            name="fk_step_latest_attempt", use_alter=True),
        ForeignKeyConstraint(["run_id", "step_key", "latest_successful_attempt_no"],
            ["investigation_step_attempts.run_id", "investigation_step_attempts.step_key", "investigation_step_attempts.attempt_no"],
            name="fk_step_latest_successful_attempt", use_alter=True),
        CheckConstraint("status in ('pending','running','retrying','succeeded','failed','cancelled')", name="ck_investigation_step_status"),
        CheckConstraint("latest_attempt_no is null or latest_attempt_no > 0", name="ck_step_latest_attempt_positive"),
        CheckConstraint("latest_successful_attempt_no is null or latest_successful_attempt_no > 0", name="ck_step_latest_success_positive"),
        Index("ix_step_run_status", "run_id", "status"),
    )


class InvestigationStepAttempt(Base):
    __tablename__ = "investigation_step_attempts"
    id: Mapped[object] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[object] = mapped_column(Uuid, ForeignKey("investigation_runs.id", ondelete="CASCADE"), nullable=False)
    step_key: Mapped[str] = mapped_column(String(80), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    output_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("run_id", "step_key", "attempt_no", name="uq_step_attempt_number"),
        ForeignKeyConstraint(["run_id", "step_key"], ["investigation_steps.run_id", "investigation_steps.step_key"],
                             ondelete="CASCADE", name="fk_step_attempt_step"),
        CheckConstraint("attempt_no > 0", name="ck_step_attempt_no_positive"),
        CheckConstraint("status in ('running','retrying','succeeded','failed','cancelled')", name="ck_step_attempt_status"),
        Index("ix_step_attempt_history", "run_id", "step_key", "attempt_no"),
    )


class Finding(Base):
    __tablename__ = "findings"
    id: Mapped[object] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    project_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    incident_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    run_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    hypothesis: Mapped[str] = mapped_column(Text, nullable=False)
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False)
    confidence_method: Mapped[str] = mapped_column(String(80), default="evidence_heuristic")
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    review_status: Mapped[str] = mapped_column(String(24), default="pending")
    __table_args__ = (
        UniqueConstraint("run_id", "rank"),
        UniqueConstraint("workspace_id", "project_id", "incident_id", "run_id", "id"),
        UniqueConstraint("workspace_id", "project_id", "incident_id", "id"),
        ForeignKeyConstraint(["workspace_id", "project_id", "incident_id", "run_id"], ["investigation_runs.workspace_id", "investigation_runs.project_id", "investigation_runs.incident_id", "investigation_runs.id"], ondelete="CASCADE"),
        CheckConstraint("confidence_score >= 0 and confidence_score <= 1"),
        CheckConstraint("review_status in ('pending','confirmed','rejected','needs_more_evidence')", name="ck_finding_review_status"),
    )


class FindingEvidenceLink(Base):
    __tablename__ = "finding_evidence_links"
    workspace_id: Mapped[object] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[object] = mapped_column(Uuid, primary_key=True)
    incident_id: Mapped[object] = mapped_column(Uuid, primary_key=True)
    run_id: Mapped[object] = mapped_column(Uuid, primary_key=True)
    finding_id: Mapped[object] = mapped_column(Uuid, primary_key=True)
    evidence_id: Mapped[object] = mapped_column(Uuid, primary_key=True)
    stance: Mapped[str] = mapped_column(String(20), primary_key=True)
    explanation: Mapped[str] = mapped_column(Text, default="")
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "project_id", "incident_id", "run_id", "finding_id"], ["findings.workspace_id", "findings.project_id", "findings.incident_id", "findings.run_id", "findings.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["workspace_id", "project_id", "incident_id", "run_id", "evidence_id"], ["investigation_run_evidence.workspace_id", "investigation_run_evidence.project_id", "investigation_run_evidence.incident_id", "investigation_run_evidence.run_id", "investigation_run_evidence.evidence_id"], ondelete="CASCADE"),
        CheckConstraint("stance in ('supports','contradicts','context')"),
    )


class FindingContextLink(Base):
    __tablename__ = "finding_context_links"
    workspace_id: Mapped[object] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[object] = mapped_column(Uuid, primary_key=True)
    incident_id: Mapped[object] = mapped_column(Uuid, primary_key=True)
    run_id: Mapped[object] = mapped_column(Uuid, primary_key=True)
    finding_id: Mapped[object] = mapped_column(Uuid, primary_key=True)
    context_reference_id: Mapped[object] = mapped_column(Uuid, primary_key=True)
    explanation: Mapped[str] = mapped_column(Text, default="")
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "project_id", "incident_id", "run_id", "finding_id"], ["findings.workspace_id", "findings.project_id", "findings.incident_id", "findings.run_id", "findings.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["workspace_id", "project_id", "incident_id", "run_id", "context_reference_id"], ["investigation_run_context_references.workspace_id", "investigation_run_context_references.project_id", "investigation_run_context_references.incident_id", "investigation_run_context_references.run_id", "investigation_run_context_references.id"], ondelete="CASCADE"),
    )


class Recommendation(Base):
    __tablename__ = "recommendations"
    id: Mapped[object] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    project_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    incident_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    run_id: Mapped[object | None] = mapped_column(Uuid)
    idempotency_key: Mapped[str | None] = mapped_column(String(64))
    finding_id: Mapped[object | None] = mapped_column(Uuid)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    risk_level: Mapped[str] = mapped_column(String(20), default="medium")
    status: Mapped[str] = mapped_column(String(24), default="proposed")
    __table_args__ = (
        UniqueConstraint("workspace_id", "project_id", "id"),
        UniqueConstraint("run_id", "idempotency_key", name="uq_recommendation_run_key"),
        ForeignKeyConstraint(["workspace_id", "project_id", "incident_id"], ["incidents.workspace_id", "incidents.project_id", "incidents.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["workspace_id", "project_id", "incident_id", "run_id"], ["investigation_runs.workspace_id", "investigation_runs.project_id", "investigation_runs.incident_id", "investigation_runs.id"], name="fk_recommendation_run"),
        ForeignKeyConstraint(["workspace_id", "project_id", "incident_id", "finding_id"], ["findings.workspace_id", "findings.project_id", "findings.incident_id", "findings.id"]),
    )


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"
    id: Mapped[object] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    project_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    recommendation_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    requested_by: Mapped[object] = mapped_column(Uuid, ForeignKey("users.id"), nullable=False)
    reviewed_by: Mapped[object | None] = mapped_column(Uuid, ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(20), default="pending")
    action_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    action_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("workspace_id", "project_id", "id"),
        ForeignKeyConstraint(["workspace_id", "project_id", "recommendation_id"], ["recommendations.workspace_id", "recommendations.project_id", "recommendations.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["project_id", "requested_by"], ["project_members.project_id", "project_members.user_id"]),
        ForeignKeyConstraint(["project_id", "reviewed_by"], ["project_members.project_id", "project_members.user_id"]),
        CheckConstraint("status in ('pending','approved','rejected')"),
        Index("uq_one_pending_approval_per_recommendation", "recommendation_id", unique=True,
              postgresql_where=(status == "pending")),
    )


class RemediationExecution(Base):
    __tablename__ = "remediation_executions"
    id: Mapped[object] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    project_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    approval_request_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True)
    status: Mapped[str] = mapped_column(String(24), default="simulated")
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (ForeignKeyConstraint(["workspace_id", "project_id", "approval_request_id"], ["approval_requests.workspace_id", "approval_requests.project_id", "approval_requests.id"], ondelete="CASCADE"),)


class IncidentIssueLink(Base):
    __tablename__ = "incident_issue_links"
    workspace_id: Mapped[object] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[object] = mapped_column(Uuid, primary_key=True)
    incident_id: Mapped[object] = mapped_column(Uuid, primary_key=True)
    issue_id: Mapped[object] = mapped_column(Uuid, primary_key=True)
    relationship_type: Mapped[str] = mapped_column(String(30), default="follow_up")
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "project_id", "incident_id"], ["incidents.workspace_id", "incidents.project_id", "incidents.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["workspace_id", "project_id", "issue_id"], ["issues.workspace_id", "issues.project_id", "issues.id"], ondelete="CASCADE"),
    )


class Integration(Base):
    __tablename__ = "integrations"
    id: Mapped[object] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    project_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    secret_ref: Mapped[str] = mapped_column(String(300), default="")
    credentials_ciphertext: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="disabled")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    permission_scope: Mapped[list] = mapped_column(JSON, default=list)
    health_status: Mapped[str] = mapped_column(String(24), default="unknown", nullable=False)
    last_successful_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str] = mapped_column(Text, default="", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_test_result: Mapped[str] = mapped_column(String(32), default="not_tested", nullable=False)
    __table_args__ = (
        UniqueConstraint("workspace_id", "project_id", "id"),
        UniqueConstraint("project_id", "provider", name="uq_integration_project_provider"),
        CheckConstraint("health_status in ('unknown','healthy','degraded','unavailable')", name="ck_integration_health_status"),
        ForeignKeyConstraint(["workspace_id", "project_id"], ["projects.workspace_id", "projects.id"], ondelete="CASCADE"),
    )


class IntegrationSyncRun(Base):
    __tablename__ = "integration_sync_runs"
    id: Mapped[object] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    project_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    integration_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    incident_id: Mapped[object | None] = mapped_column(Uuid)
    source_type: Mapped[str] = mapped_column(String(50), default="", nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    error_code: Mapped[str] = mapped_column(String(64), default="")
    result_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    details: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "project_id", "integration_id"], ["integrations.workspace_id", "integrations.project_id", "integrations.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["workspace_id", "project_id", "incident_id"], ["incidents.workspace_id", "incidents.project_id", "incidents.id"], ondelete="CASCADE", name="fk_sync_run_incident_scope"),
        Index("ix_integration_sync_history", "integration_id", "started_at"),
    )


class AlertmanagerAlert(Base):
    __tablename__ = "alertmanager_alerts"
    id: Mapped[object] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    project_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    integration_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    service_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    incident_id: Mapped[object | None] = mapped_column(Uuid)
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    alertname: Mapped[str] = mapped_column(String(240), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    labels: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    annotations: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    last_received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        UniqueConstraint("integration_id", "fingerprint", name="uq_alertmanager_integration_fingerprint"),
        ForeignKeyConstraint(["workspace_id", "project_id", "integration_id"], ["integrations.workspace_id", "integrations.project_id", "integrations.id"], ondelete="CASCADE", name="fk_alertmanager_integration_scope"),
        ForeignKeyConstraint(["workspace_id", "project_id", "service_id"], ["services.workspace_id", "services.project_id", "services.id"], name="fk_alertmanager_service_scope"),
        ForeignKeyConstraint(["workspace_id", "project_id", "incident_id"], ["incidents.workspace_id", "incidents.project_id", "incidents.id"], name="fk_alertmanager_incident_scope"),
        CheckConstraint("status in ('firing','resolved')", name="ck_alertmanager_alert_status"),
        CheckConstraint("severity in ('SEV-1','SEV-2','SEV-3','SEV-4')", name="ck_alertmanager_alert_severity"),
        Index("ix_alertmanager_project_status", "project_id", "status", "last_received_at"),
    )


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    id: Mapped[object] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    project_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(80), nullable=False)
    aggregate_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(140), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str] = mapped_column(Text, default="")
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "project_id"], ["projects.workspace_id", "projects.id"], ondelete="CASCADE"),
        CheckConstraint("status in ('pending','publishing','published','dead')"),
        Index("ix_outbox_dispatch", "status", "available_at", "created_at"),
        Index("ix_outbox_aggregate", "aggregate_type", "aggregate_id", "created_at"),
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[object] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[object] = mapped_column(Uuid, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    project_id: Mapped[object | None] = mapped_column(Uuid)
    actor_id: Mapped[object | None] = mapped_column(Uuid, ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_id: Mapped[object] = mapped_column(Uuid, nullable=False)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        ForeignKeyConstraint(["workspace_id", "project_id"], ["projects.workspace_id", "projects.id"]),
        Index("ix_audit_workspace_time", "workspace_id", "created_at"),
        Index("ix_audit_project_time", "project_id", "created_at"),
    )
