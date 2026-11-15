from datetime import datetime, timezone
from typing import Literal
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from uuid import UUID


class UserCreate(BaseModel):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=160)
    password: str = Field(min_length=10, max_length=128)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    email: str
    full_name: str


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)


class WorkspaceMemberCreate(BaseModel):
    email: EmailStr
    role: str = "member"


class WorkspacePatch(BaseModel):
    name: str = Field(min_length=2, max_length=120)


class TeamCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)


class TeamMemberCreate(BaseModel):
    user_id: UUID
    role: str = "member"


class ServiceCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    environment: str = Field(default="production", max_length=80)
    description: str = Field(default="", max_length=10000)
    criticality: str = "medium"
    technology: dict = {}
    owner_team_id: UUID | None = None


class ServicePatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    environment: str | None = Field(default=None, max_length=80)
    description: str | None = Field(default=None, max_length=10000)
    criticality: str | None = None
    technology: dict | None = None
    owner_team_id: UUID | None = None
    health: str | None = None


class IntegrationCredentialsUpdate(BaseModel):
    credentials: dict = Field(min_length=1)


class ServiceDependencyCreate(BaseModel):
    target_service_id: UUID
    relationship_type: str = Field(default="calls", max_length=40)


class IntegrationCreate(BaseModel):
    provider: str = Field(min_length=2, max_length=80)
    config: dict = {}
    permission_scope: list[str] = []


class IntegrationPatch(BaseModel):
    config: dict | None = None
    permission_scope: list[str] | None = None
    enabled: bool | None = None


class ProjectCreate(BaseModel):
    workspace_id: UUID
    name: str = Field(min_length=2, max_length=160)
    description: str = ""


class IssueCreate(BaseModel):
    type: str = "Bug"
    title: str
    description: str = ""
    priority: str = "Medium"
    assignee_id: UUID | None = None
    service_id: UUID | None = None
    labels: list[str] = []


class IssuePatch(BaseModel):
    title: str | None = None
    description: str | None = None
    status: str | None = None
    priority: str | None = None
    assignee_id: UUID | None = None
    labels: list[str] | None = None
    due_date: datetime | None = None


class CommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=10000)


class IncidentCreate(BaseModel):
    title: str
    description: str = ""
    severity: str = "SEV-3"
    service_ids: list[UUID] = []
    started_at: datetime | None = None
    impact: str = ""
    assignee_id: UUID | None = None


class IncidentPatch(BaseModel):
    title: str | None = Field(default=None, min_length=2, max_length=240)
    description: str | None = None
    impact: str | None = None
    severity: str | None = None
    status: str | None = None
    assignee_id: UUID | None = None
    service_ids: list[UUID] | None = None


class IncidentCommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=10000)


class IncidentResolve(BaseModel):
    resolution_notes: str = Field(min_length=1, max_length=10000)
    confirmed_root_cause: str = Field(default="", max_length=10000)


class EvidenceCreate(BaseModel):
    source_type: str = "diagnostic"
    summary: str = Field(min_length=1, max_length=50000)
    observed_at: datetime | None = None
    metadata_json: dict = {}


class AlertmanagerAlertPayload(BaseModel):
    status: Literal["firing", "resolved"]
    labels: dict[str, str] = Field(default_factory=dict, max_length=80)
    annotations: dict[str, str] = Field(default_factory=dict, max_length=80)
    startsAt: datetime
    endsAt: datetime | None = None
    generatorURL: str = Field(default="", max_length=1000)
    fingerprint: str = Field(min_length=8, max_length=128, pattern=r"^[A-Fa-f0-9]+$")

    @field_validator("startsAt", "endsAt")
    @classmethod
    def timestamps_are_aware(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("Alert timestamps must include a timezone")
        return value


class AlertmanagerWebhookPayload(BaseModel):
    status: Literal["firing", "resolved"]
    receiver: str = Field(default="", max_length=200)
    groupLabels: dict[str, str] = Field(default_factory=dict, max_length=80)
    commonLabels: dict[str, str] = Field(default_factory=dict, max_length=80)
    commonAnnotations: dict[str, str] = Field(default_factory=dict, max_length=80)
    alerts: list[AlertmanagerAlertPayload] = Field(min_length=1, max_length=100)


class TelemetryCollectRequest(BaseModel):
    start: datetime | None = None
    end: datetime | None = None

    @field_validator("start", "end")
    @classmethod
    def query_times_are_aware(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("Time range must include a timezone")
        return value


class ReviewDecision(BaseModel):
    status: str
    note: str = ""


class ApprovalDecision(BaseModel):
    status: str
    note: str = ""
