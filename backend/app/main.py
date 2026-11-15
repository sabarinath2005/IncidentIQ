from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from uuid import UUID
import json
import shutil
import hmac
import re
from urllib.parse import urlsplit
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, Query, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import or_, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.core.config import settings
from app.core.security import current_user, create_token, hash_password, verify_password, require_project, require_role
from app.db import Base, engine, get_db, SessionLocal
from app.models import *
from app.schemas import *
from app.run_lifecycle import InvalidRunTransition, RUN_ACTIVE_STATUSES, transition_run
from app.services.credentials import encrypt_credentials, masked_credentials_present
from app.services.connectors import ConnectionResult, registry as connector_registry
from app.ai_providers import provider_mode, provider_status

ACTIVE_RUNS = RUN_ACTIVE_STATUSES
ALLOWED_ISSUE_TRANSITIONS = {
    "Open": {"In Progress"}, "In Progress": {"In Review", "Resolved"},
    "In Review": {"In Progress", "Resolved"}, "Resolved": {"Closed", "In Progress"},
    "Closed": set(),
}


def iso(row):
    data = {c.name: getattr(row, c.name) for c in row.__table__.columns}
    for key, value in data.items():
        if isinstance(value, UUID):
            data[key] = str(value)
    return data


def knowledge_out(row):
    data = iso(row)
    data.pop("content", None)
    return data


def audit(db, workspace_id, actor_id, action, resource_type, resource_id, detail=None, project_id=None):
    db.add(AuditEvent(workspace_id=workspace_id, actor_id=actor_id, action=action,
                      project_id=project_id or (resource_id if resource_type == "project" else None),
                      resource_type=resource_type, resource_id=resource_id, detail=detail or {}))


def ensure_workspace_member(db, workspace_id, user_id):
    if not db.query(WorkspaceMember).filter_by(workspace_id=workspace_id, user_id=user_id).first():
        raise HTTPException(403, "Workspace membership required")


def project_or_404(db, project_id, user):
    require_project(str(project_id), user, db)
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return project


def incident_or_404(db, incident_id, user):
    incident = db.get(Incident, incident_id)
    if not incident:
        raise HTTPException(404, "Incident not found")
    require_project(str(incident.project_id), user, db)
    return incident


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Schema is managed by Alembic. Avoid implicit create_all in application startup.
    yield


app = FastAPI(title="IncidentIQ", version="1.0.0", description="Evidence-grounded incident investigation and issue management demo.", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=[settings.frontend_origin], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])
from app.core.telemetry import configure_telemetry
configure_telemetry(app, settings.otlp_endpoint)


@app.get("/health")
def health():
    return {"status": "ok", "service": "incident-platform"}


@app.get("/api/v1/ai/provider")
def ai_provider_status(user: User = Depends(current_user)):
    """Expose safe runtime status only; never return provider URLs or credentials."""
    return provider_status()


@app.post("/api/v1/auth/register", status_code=201)
def register(payload: UserCreate, db: Session = Depends(get_db)):
    email = str(payload.email).lower()
    if db.query(User).filter(func.lower(User.email) == email).first():
        raise HTTPException(409, "An account with this email already exists")
    user = User(email=email, full_name=payload.full_name, password_hash=hash_password(payload.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"access_token": create_token(str(user.id)), "token_type": "bearer", "user": {"id": str(user.id), "email": user.email, "full_name": user.full_name}}


@app.post("/api/v1/auth/login")
def login(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(User).filter(func.lower(User.email) == form.username.lower()).first()
    if user is None or not verify_password(form.password, user.password_hash):
        raise HTTPException(401, "Incorrect email or password")
    return {"access_token": create_token(str(user.id)), "token_type": "bearer", "user": {"id": str(user.id), "email": user.email, "full_name": user.full_name}}


@app.get("/api/v1/auth/me")
def me(user: User = Depends(current_user)):
    return {"id": str(user.id), "email": user.email, "full_name": user.full_name}


@app.get("/api/v1/workspaces")
def workspaces(user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.query(Workspace, WorkspaceMember.role).join(WorkspaceMember, Workspace.id == WorkspaceMember.workspace_id).filter(WorkspaceMember.user_id == user.id).all()
    return [{**iso(w), "role": role} for w, role in rows]


@app.patch("/api/v1/workspaces/{workspace_id}")
def update_workspace(workspace_id: UUID, payload: WorkspacePatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    membership = db.query(WorkspaceMember).filter_by(workspace_id=workspace_id, user_id=user.id).one_or_none()
    if not membership: raise HTTPException(404, "Workspace not found")
    if membership.role not in {"owner", "admin"}: raise HTTPException(403, "Workspace owner or admin permission required")
    workspace = db.get(Workspace, workspace_id); workspace.name = payload.name.strip()
    audit(db, workspace.id, user.id, "workspace.updated", "workspace", workspace.id, {"name": workspace.name})
    db.commit(); db.refresh(workspace); return {**iso(workspace), "role": membership.role}


@app.get("/api/v1/workspaces/{workspace_id}/members")
def list_workspace_members(workspace_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ensure_workspace_member(db, workspace_id, user.id)
    rows = db.query(WorkspaceMember, User).join(User, User.id == WorkspaceMember.user_id).filter(WorkspaceMember.workspace_id == workspace_id).all()
    return [{"user_id": str(m.user_id), "email": u.email, "full_name": u.full_name, "role": m.role} for m, u in rows]


@app.delete("/api/v1/workspaces/{workspace_id}/members/{member_id}", status_code=204)
def remove_workspace_member(workspace_id: UUID, member_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    actor = db.query(WorkspaceMember).filter_by(workspace_id=workspace_id, user_id=user.id).one_or_none()
    target = db.query(WorkspaceMember).filter_by(workspace_id=workspace_id, user_id=member_id).one_or_none()
    if not actor or not target: raise HTTPException(404, "Workspace member not found")
    if actor.role not in {"owner", "admin"}: raise HTTPException(403, "Workspace owner or admin permission required")
    if target.role == "owner": raise HTTPException(409, "Transfer workspace ownership before removing the owner")
    audit(db, workspace_id, user.id, "workspace.member_removed", "user", member_id)
    db.delete(target); db.commit(); return None


@app.post("/api/v1/workspaces", status_code=201)
def create_workspace(payload: WorkspaceCreate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    workspace = Workspace(name=payload.name.strip())
    db.add(workspace)
    db.flush()
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role="owner"))
    audit(db, workspace.id, user.id, "workspace.created", "workspace", workspace.id)
    db.commit()
    db.refresh(workspace)
    return iso(workspace)


@app.post("/api/v1/workspaces/{workspace_id}/members", status_code=201)
def add_workspace_member(workspace_id: UUID, payload: WorkspaceMemberCreate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    membership = db.query(WorkspaceMember).filter_by(workspace_id=workspace_id, user_id=user.id).one_or_none()
    if membership is None or membership.role not in {"owner", "admin"}:
        raise HTTPException(403, "Workspace owner or admin permission required")
    invited = db.query(User).filter(func.lower(User.email) == str(payload.email).lower()).one_or_none()
    if invited is None:
        raise HTTPException(404, "Register the user before adding them to a workspace")
    role = payload.role
    if role not in {"admin", "member"}:
        raise HTTPException(422, "Role must be admin or member")
    row = db.query(WorkspaceMember).filter_by(workspace_id=workspace_id, user_id=invited.id).one_or_none()
    if row is None:
        row = WorkspaceMember(workspace_id=workspace_id, user_id=invited.id, role=role)
        db.add(row)
        action = "workspace.member_added"
        previous_role = None
    else:
        previous_role = row.role
        row.role = role
        action = "workspace.member_role_changed" if previous_role != role else None
    if action:
        audit(db, workspace_id, user.id, action, "user", invited.id,
              {"role": role, **({"previous_role": previous_role} if previous_role else {})})
    db.commit()
    return {"workspace_id": str(workspace_id), "user_id": str(invited.id), "email": invited.email, "role": row.role}


@app.get("/api/v1/projects")
def list_projects(workspace_id: UUID | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    query = db.query(Project, ProjectMember.role).join(ProjectMember, (ProjectMember.project_id == Project.id) & (ProjectMember.workspace_id == Project.workspace_id)).filter(ProjectMember.user_id == user.id)
    if workspace_id:
        query = query.filter(Project.workspace_id == workspace_id)
    return [{**iso(row), "role": role} for row, role in query.order_by(Project.created_at.desc()).all()]


@app.post("/api/v1/projects", status_code=201)
def create_project(payload: ProjectCreate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ensure_workspace_member(db, payload.workspace_id, user.id)
    project = Project(workspace_id=payload.workspace_id, name=payload.name.strip(), description=payload.description)
    db.add(project)
    db.flush()
    db.add(ProjectMember(workspace_id=project.workspace_id, project_id=project.id, user_id=user.id, role="admin"))
    audit(db, project.workspace_id, user.id, "project.created", "project", project.id, project_id=project.id)
    db.commit()
    db.refresh(project)
    return iso(project)


@app.get("/api/v1/workspaces/{workspace_id}/teams")
def list_teams(workspace_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ensure_workspace_member(db, workspace_id, user.id)
    return [iso(team) for team in db.query(Team).filter_by(workspace_id=workspace_id).order_by(Team.name).all()]


@app.post("/api/v1/workspaces/{workspace_id}/teams", status_code=201)
def create_team(workspace_id: UUID, payload: TeamCreate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    membership = db.query(WorkspaceMember).filter_by(workspace_id=workspace_id, user_id=user.id).one_or_none()
    if not membership: raise HTTPException(404, "Workspace not found")
    if membership.role not in {"owner", "admin"}: raise HTTPException(403, "Workspace owner or admin permission required")
    team = Team(workspace_id=workspace_id, name=payload.name.strip()); db.add(team); db.flush()
    audit(db, workspace_id, user.id, "team.created", "team", team.id)
    db.commit(); db.refresh(team); return iso(team)


@app.post("/api/v1/teams/{team_id}/members", status_code=201)
def add_team_member(team_id: UUID, payload: TeamMemberCreate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    team = db.get(Team, team_id)
    if not team: raise HTTPException(404, "Team not found")
    actor = db.query(WorkspaceMember).filter_by(workspace_id=team.workspace_id, user_id=user.id).one_or_none()
    if not actor or actor.role not in {"owner", "admin"}: raise HTTPException(403, "Workspace owner or admin permission required")
    if payload.role not in {"lead", "member"}: raise HTTPException(422, "Team role must be lead or member")
    if not db.query(WorkspaceMember).filter_by(workspace_id=team.workspace_id, user_id=payload.user_id).first(): raise HTTPException(422, "Team member must belong to this workspace")
    row = db.query(TeamMember).filter_by(team_id=team.id, user_id=payload.user_id).one_or_none()
    if row is None:
        row = TeamMember(workspace_id=team.workspace_id, team_id=team.id, user_id=payload.user_id, role=payload.role)
        db.add(row)
        action = "team.member_added"
        previous_role = None
    else:
        previous_role = row.role
        row.role = payload.role
        action = "team.member_role_changed" if previous_role != payload.role else None
    if action:
        audit(db, team.workspace_id, user.id, action, "team_member", payload.user_id,
              {"team_id": str(team.id), "role": payload.role,
               **({"previous_role": previous_role} if previous_role else {})})
    db.commit(); return {"team_id": str(team.id), "user_id": str(payload.user_id), "role": row.role}


@app.get("/api/v1/projects/{project_id}/members")
def project_members(project_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project_or_404(db, project_id, user)
    rows = db.query(ProjectMember, User).join(User, User.id == ProjectMember.user_id).filter(ProjectMember.project_id == project_id).all()
    return [{"user_id": str(m.user_id), "email": u.email, "full_name": u.full_name, "role": m.role} for m, u in rows]


@app.post("/api/v1/projects/{project_id}/members", status_code=201)
def add_project_member(project_id: UUID, payload: dict, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project = project_or_404(db, project_id, user)
    require_role(str(project_id), user, db, {"admin"})
    invite = db.query(User).filter(func.lower(User.email) == str(payload.get("email", "")).lower()).one_or_none()
    if invite is None:
        raise HTTPException(404, "Register the user before adding them to a project")
    ensure_workspace_member(db, project.workspace_id, invite.id)
    role = payload.get("role", "member")
    if role not in {"admin", "member", "engineer", "incident_manager", "viewer"}:
        raise HTTPException(422, "Role must be project admin, incident manager, engineer, or viewer")
    membership = db.query(ProjectMember).filter_by(project_id=project.id, user_id=invite.id).one_or_none()
    if membership is None:
        membership = ProjectMember(workspace_id=project.workspace_id, project_id=project.id, user_id=invite.id, role=role)
        db.add(membership)
        action = "project.member_added"
        previous_role = None
    else:
        previous_role = membership.role
        membership.role = role
        action = "project.member_role_changed" if previous_role != role else None
    if action:
        audit(db, project.workspace_id, user.id, action, "project_member", invite.id,
              {"role": role, **({"previous_role": previous_role} if previous_role else {})}, project.id)
    db.commit()
    return {"user_id": str(invite.id), "email": invite.email, "role": membership.role}


@app.delete("/api/v1/projects/{project_id}/members/{member_id}", status_code=204)
def remove_project_member(project_id: UUID, member_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project = project_or_404(db, project_id, user)
    require_role(str(project_id), user, db, {"admin"})
    target = db.query(ProjectMember).filter_by(project_id=project.id, user_id=member_id).one_or_none()
    if target is None:
        raise HTTPException(404, "Project member not found")
    if target.user_id == user.id:
        raise HTTPException(409, "Project admins cannot remove their own membership")
    try:
        audit(db, project.workspace_id, user.id, "project.member_removed", "project_member", target.user_id,
              {"role": target.role}, project.id)
        db.delete(target)
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "This member has project history and cannot be removed")
    return None


@app.get("/api/v1/projects/{project_id}/services")
def list_services(project_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project_or_404(db, project_id, user)
    return [iso(x) for x in db.query(Service).filter_by(project_id=project_id).filter(Service.archived_at.is_(None)).order_by(Service.name).all()]


@app.post("/api/v1/projects/{project_id}/services", status_code=201)
def create_service(project_id: UUID, payload: ServiceCreate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project = project_or_404(db, project_id, user)
    require_role(str(project_id), user, db, {"admin", "member"})
    if payload.criticality not in {"critical", "high", "medium", "low"}:
        raise HTTPException(422, "Criticality must be critical, high, medium, or low")
    if payload.owner_team_id and not db.query(Team).filter_by(id=payload.owner_team_id, workspace_id=project.workspace_id).first():
        raise HTTPException(422, "Owner team must belong to this workspace")
    row = Service(workspace_id=project.workspace_id, project_id=project.id, **payload.model_dump())
    db.add(row)
    db.flush()
    audit(db, project.workspace_id, user.id, "service.created", "service", row.id, project_id=project.id)
    db.commit()
    db.refresh(row)
    return iso(row)


@app.get("/api/v1/services/{service_id}")
def service_detail(service_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = db.get(Service, service_id)
    if not row:
        raise HTTPException(404, "Service not found")
    project_or_404(db, row.project_id, user)
    deps = db.query(ServiceDependency, Service).join(Service, Service.id == ServiceDependency.target_service_id).filter(ServiceDependency.source_service_id == row.id).all()
    incidents = db.query(Incident).join(IncidentService, IncidentService.incident_id == Incident.id).filter(IncidentService.service_id == row.id).order_by(Incident.detected_at.desc()).all()
    issues = db.query(Issue).filter_by(project_id=row.project_id, service_id=row.id).order_by(Issue.updated_at.desc()).all()
    team = db.get(Team, row.owner_team_id) if row.owner_team_id else None
    membership = require_project(str(row.project_id), user, db)
    return {**iso(row), "owner_team": iso(team) if team else None,
            "viewer_role": membership.role,
            "dependencies": [{**iso(link), "service": iso(target)} for link, target in deps],
            "incidents": [iso(x) for x in incidents], "issues": [iso(x) for x in issues]}


@app.patch("/api/v1/services/{service_id}")
def update_service(service_id: UUID, payload: ServicePatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = db.get(Service, service_id)
    if not row:
        raise HTTPException(404, "Service not found")
    require_role(str(row.project_id), user, db, {"admin", "member"})
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("criticality") and changes["criticality"] not in {"critical", "high", "medium", "low"}:
        raise HTTPException(422, "Invalid criticality")
    if changes.get("owner_team_id") and not db.query(Team).filter_by(id=changes["owner_team_id"], workspace_id=row.workspace_id).first():
        raise HTTPException(422, "Owner team must belong to this workspace")
    for key, value in changes.items(): setattr(row, key, value)
    audit(db, row.workspace_id, user.id, "service.updated", "service", row.id,
          {"changed_fields": sorted(changes)}, row.project_id)
    db.commit(); db.refresh(row)
    return iso(row)


@app.post("/api/v1/services/{service_id}/archive")
def archive_service(service_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = db.get(Service, service_id)
    if not row: raise HTTPException(404, "Service not found")
    require_role(str(row.project_id), user, db, {"admin"})
    if db.query(IncidentService).filter_by(service_id=row.id).join(Incident, Incident.id == IncidentService.incident_id).filter(Incident.status.notin_(["resolved", "closed"])).first():
        raise HTTPException(409, "Resolve active incidents before archiving this service")
    row.archived_at = datetime.now(timezone.utc)
    audit(db, row.workspace_id, user.id, "service.archived", "service", row.id, project_id=row.project_id)
    db.commit(); return iso(row)


@app.post("/api/v1/services/{service_id}/dependencies", status_code=201)
def add_service_dependency(service_id: UUID, payload: ServiceDependencyCreate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    source = db.get(Service, service_id)
    if not source: raise HTTPException(404, "Service not found")
    require_role(str(source.project_id), user, db, {"admin", "member"})
    target = db.get(Service, payload.target_service_id)
    if not target or target.project_id != source.project_id or target.workspace_id != source.workspace_id:
        raise HTTPException(404, "Dependency target must be a service in this project")
    if source.id == target.id: raise HTTPException(422, "A service cannot depend on itself")
    link = db.query(ServiceDependency).filter_by(source_service_id=source.id, target_service_id=target.id, relationship_type=payload.relationship_type).one_or_none()
    if link: return iso(link)
    link = ServiceDependency(workspace_id=source.workspace_id, project_id=source.project_id, source_service_id=source.id, target_service_id=target.id, relationship_type=payload.relationship_type)
    db.add(link); audit(db, source.workspace_id, user.id, "service.dependency_added", "service", source.id, {"target_service_id": str(target.id)}, source.project_id)
    db.commit(); return iso(link)


INTEGRATION_PROVIDERS = {"prometheus", "alertmanager", "loki", "opentelemetry", "tempo", "grafana", "github", "jira", "slack", "pagerduty"}
SENSITIVE_CONFIG_KEYS = {"token", "password", "secret", "api_key", "apikey", "client_secret", "authorization"}


def sensitive_config_key(key: str) -> bool:
    normalized = "".join(ch for ch in key.lower() if ch.isalnum())
    return any(term in normalized for term in ("token", "password", "secret", "apikey", "authorization", "credential"))


def contains_credential(value):
    if isinstance(value, dict):
        return any(sensitive_config_key(str(key)) or contains_credential(item) for key, item in value.items())
    if isinstance(value, list):
        return any(contains_credential(item) for item in value)
    if isinstance(value, str):
        if re.search(r"(?i)(?:bearer\s+|(?:password|token|secret|api[_-]?key|authorization)\s*[:=])", value):
            return True
        if value.startswith(("http://", "https://")):
            parsed = urlsplit(value)
            return bool(parsed.username or parsed.password or parsed.query)
    return False


def mask_config(value):
    if isinstance(value, dict):
        return {str(key): ("********" if sensitive_config_key(str(key)) else mask_config(item))
                for key, item in value.items()}
    if isinstance(value, list): return [mask_config(item) for item in value]
    if isinstance(value, str) and contains_credential(value): return "********"
    return value


def integration_out(row):
    return {"id": str(row.id), "workspace_id": str(row.workspace_id), "project_id": str(row.project_id),
            "provider": row.provider, "config": mask_config(row.config or {}), "permission_scope": row.permission_scope,
            "status": row.status, "enabled": row.enabled, "health_status": row.health_status,
            "last_successful_sync_at": row.last_successful_sync_at, "last_error": row.last_error, "updated_at": row.updated_at,
            "last_checked_at": row.last_checked_at, "last_test_result": row.last_test_result,
            "credentials": masked_credentials_present(row.credentials_ciphertext), "has_credentials": bool(row.credentials_ciphertext),
            "implementation_status": "implemented" if (connector_registry.get(row.provider) and connector_registry.get(row.provider).implemented) else "not_implemented"}


@app.get("/api/v1/integrations/catalog")
def integration_catalog(user: User = Depends(current_user)):
    return connector_registry.catalog()


@app.get("/api/v1/projects/{project_id}/integrations")
def list_integrations(project_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project = project_or_404(db, project_id, user)
    rows = db.query(Integration).filter_by(workspace_id=project.workspace_id, project_id=project.id).order_by(Integration.provider).all()
    return [integration_out(x) for x in rows]


@app.post("/api/v1/projects/{project_id}/integrations", status_code=201)
def create_integration(project_id: UUID, payload: IntegrationCreate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project = project_or_404(db, project_id, user); require_role(str(project.id), user, db, {"admin"})
    provider = payload.provider.lower()
    if provider not in INTEGRATION_PROVIDERS: raise HTTPException(422, "Unsupported integration provider")
    if db.query(Integration).filter_by(project_id=project.id, provider=provider).first(): raise HTTPException(409, "Integration already configured for this project")
    if contains_credential(payload.config): raise HTTPException(422, "Credentials must be submitted through the encrypted credentials endpoint")
    row = Integration(workspace_id=project.workspace_id, project_id=project.id, provider=provider,
                      config=payload.config, permission_scope=payload.permission_scope)
    db.add(row); db.flush(); audit(db, project.workspace_id, user.id, "integration.created", "integration", row.id, {"provider": provider}, project.id)
    db.commit(); db.refresh(row); return integration_out(row)


@app.patch("/api/v1/integrations/{integration_id}")
def update_integration(integration_id: UUID, payload: IntegrationPatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = db.get(Integration, integration_id)
    if not row: raise HTTPException(404, "Integration not found")
    require_role(str(row.project_id), user, db, {"admin"})
    changes = payload.model_dump(exclude_unset=True)
    if "config" in changes and contains_credential(changes["config"]):
        raise HTTPException(422, "Credentials must be submitted through the encrypted credentials endpoint")
    for key, value in changes.items(): setattr(row, key, value)
    row.status = "enabled" if row.enabled else "disabled"
    audit(db, row.workspace_id, user.id, "integration.updated", "integration", row.id, {k: v for k, v in changes.items() if k != "config"}, row.project_id)
    db.commit(); db.refresh(row); return integration_out(row)


@app.put("/api/v1/integrations/{integration_id}/credentials")
def replace_integration_credentials(integration_id: UUID, payload: IntegrationCredentialsUpdate,
                                    user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = db.get(Integration, integration_id)
    if not row: raise HTTPException(404, "Integration not found")
    require_role(str(row.project_id), user, db, {"admin"})
    # Fernet provides authenticated encryption. Missing/invalid configured keys fail closed.
    row.credentials_ciphertext = encrypt_credentials(payload.credentials)
    audit(db, row.workspace_id, user.id, "integration.credentials_replaced", "integration", row.id, {"provider": row.provider}, row.project_id)
    db.commit(); db.refresh(row)
    return integration_out(row)


@app.post("/api/v1/integrations/{integration_id}/test")
def test_integration(integration_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = db.get(Integration, integration_id)
    if not row: raise HTTPException(404, "Integration not found")
    require_role(str(row.project_id), user, db, {"admin"})
    connector = connector_registry.get(row.provider)
    if not connector: raise HTTPException(422, "Provider has no registered connector")
    test_config = dict(row.config or {})
    if row.provider == "github":
        repositories = db.query(GitHubRepositoryMapping.repository_full_name).filter_by(
            workspace_id=row.workspace_id, project_id=row.project_id, integration_id=row.id,
        ).order_by(GitHubRepositoryMapping.repository_full_name).limit(
            connector.max_repositories_per_test + 1).all()
        test_config["repositories"] = [repository for (repository,) in repositories]
    errors = connector.validate_config(test_config)
    if errors:
        from app.services.connectors import ConnectorTest
        result = ConnectorTest(ConnectionResult.INVALID_CONFIGURATION, "invalid_configuration", "; ".join(errors))
    else:
        try:
            result = connector.test_connection(test_config, connector_registry.credentials_for(row))
        except ValueError:
            from app.services.connectors import ConnectorTest
            result = ConnectorTest(ConnectionResult.UNAVAILABLE, "credential_decryption_failed", "Stored credentials could not be authenticated.")
    row.last_checked_at = datetime.now(timezone.utc)
    row.last_test_result = result.result.value.lower()
    row.health_status = ("healthy" if result.result == ConnectionResult.CONNECTED else
                         "unavailable" if result.result in {ConnectionResult.NOT_IMPLEMENTED, ConnectionResult.UNAVAILABLE} else "degraded")
    row.last_error = result.message if result.result != ConnectionResult.CONNECTED else ""
    if result.result == ConnectionResult.CONNECTED:
        row.last_successful_sync_at = row.last_checked_at
    history = IntegrationSyncRun(workspace_id=row.workspace_id, project_id=row.project_id, integration_id=row.id,
                                 status=result.result.value.lower(), error_code=result.code,
                                 started_at=row.last_checked_at, completed_at=row.last_checked_at)
    db.add(history)
    audit(db, row.workspace_id, user.id, "integration.connection_tested", "integration", row.id,
          {"provider": row.provider, "result": result.result.value, "validation_error_count": len(errors)}, row.project_id)
    db.commit()
    return {**integration_out(row), "connection_test": result.result.value, "message": result.message}


@app.get("/api/v1/integrations/{integration_id}/sync-runs")
def integration_sync_history(integration_id: UUID, limit: int = Query(50, ge=1, le=200),
                             user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = db.get(Integration, integration_id)
    if row is None:
        raise HTTPException(404, "Integration not found")
    project_or_404(db, row.project_id, user)
    runs = db.query(IntegrationSyncRun).filter_by(workspace_id=row.workspace_id,
        project_id=row.project_id, integration_id=row.id).order_by(IntegrationSyncRun.started_at.desc()).limit(limit).all()
    return [iso(run) for run in runs]


@app.get("/api/v1/projects/{project_id}/knowledge-documents")
def list_knowledge_documents(project_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project = project_or_404(db, project_id, user)
    return [knowledge_out(row) for row in db.query(KnowledgeDocument).filter_by(
        workspace_id=project.workspace_id, project_id=project.id).filter(
            KnowledgeDocument.status != "removed").order_by(KnowledgeDocument.created_at.desc()).all()]


@app.post("/api/v1/projects/{project_id}/knowledge-documents", status_code=201)
async def upload_knowledge_document(project_id: UUID, file: UploadFile = File(...),
        source_kind: str = Form("runbook"), user: User = Depends(current_user), db: Session = Depends(get_db)):
    project = project_or_404(db, project_id, user)
    require_role(str(project.id), user, db, {"admin", "member"})
    allowed_kinds = {"runbook", "troubleshooting_guide", "architecture", "procedure", "incident_summary"}
    if source_kind not in allowed_kinds:
        raise HTTPException(422, "Unsupported knowledge document type")
    content = await file.read(settings.knowledge_max_file_bytes + 1)
    if len(content) > settings.knowledge_max_file_bytes:
        raise HTTPException(413, "Document exceeds the configured size limit")
    from app.services.knowledge import content_digest, parse_document, process_document
    try:
        filename, mime_type, text_content = parse_document(file.filename or "", file.content_type or "", content)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    digest = content_digest(text_content)
    existing = db.query(KnowledgeDocument).filter_by(workspace_id=project.workspace_id,
        project_id=project.id, content_hash=digest).filter(KnowledgeDocument.status != "removed").one_or_none()
    if existing:
        return {**knowledge_out(existing), "deduplicated": True}
    row = KnowledgeDocument(workspace_id=project.workspace_id, project_id=project.id,
        title=filename.rsplit(".", 1)[0][:240], filename=filename, mime_type=mime_type,
        file_size=len(content), source_ref=filename, source_kind=source_kind,
        content_hash=digest, content=text_content, status="pending")
    db.add(row)
    db.flush()
    audit(db, project.workspace_id, user.id, "knowledge_document.uploaded", "knowledge_document", row.id,
          {"source_kind": source_kind, "content_hash": digest, "size": len(content)}, project.id)
    db.commit()
    db.refresh(row)
    return knowledge_out(process_document(db, row))


@app.put("/api/v1/knowledge-documents/{document_id}")
def update_knowledge_document(document_id: UUID, payload: KnowledgeDocumentUpdate,
        user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = db.get(KnowledgeDocument, document_id)
    if row is None or row.status == "removed":
        raise HTTPException(404, "Knowledge document not found")
    require_role(str(row.project_id), user, db, {"admin"})
    values = payload.model_dump(exclude_unset=True)
    text_changed = "content" in values and values["content"] != row.content
    if text_changed:
        if len(values["content"].encode("utf-8")) > settings.knowledge_max_file_bytes:
            raise HTTPException(413, "Document exceeds the configured size limit")
        from app.services.knowledge import content_digest
        digest = content_digest(values["content"])
        duplicate = db.query(KnowledgeDocument).filter(KnowledgeDocument.project_id == row.project_id,
            KnowledgeDocument.id != row.id, KnowledgeDocument.content_hash == digest,
            KnowledgeDocument.status != "removed").first()
        if duplicate:
            raise HTTPException(409, "Identical document content already exists in this project")
        row.content = values.pop("content")
        row.content_hash = digest
        row.file_size = len(row.content.encode("utf-8"))
        row.version += 1
        row.status = "pending"
        row.chunk_count = 0
        row.indexed_at = None
    for key, value in values.items():
        setattr(row, key, value)
    audit(db, row.workspace_id, user.id, "knowledge_document.updated", "knowledge_document", row.id,
          {"version": row.version, "content_changed": text_changed}, row.project_id)
    db.commit()
    db.refresh(row)
    if text_changed:
        from app.services.knowledge import process_document
        row = process_document(db, row)
    return knowledge_out(row)


@app.get("/api/v1/knowledge-documents/{document_id}")
def get_knowledge_document(document_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = db.get(KnowledgeDocument, document_id)
    if row is None or row.status == "removed":
        raise HTTPException(404, "Knowledge document not found")
    project_or_404(db, row.project_id, user)
    return iso(row)


@app.post("/api/v1/knowledge-documents/{document_id}/retry")
def retry_knowledge_document(document_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = db.get(KnowledgeDocument, document_id)
    if row is None or row.status == "removed":
        raise HTTPException(404, "Knowledge document not found")
    require_role(str(row.project_id), user, db, {"admin", "member"})
    if row.status != "failed":
        raise HTTPException(409, "Only failed document indexing can be retried")
    audit(db, row.workspace_id, user.id, "knowledge_document.retry_requested", "knowledge_document", row.id,
          {"version": row.version}, row.project_id)
    db.commit()
    from app.services.knowledge import process_document
    return knowledge_out(process_document(db, row))


@app.delete("/api/v1/knowledge-documents/{document_id}", status_code=204)
def delete_knowledge_document(document_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = db.get(KnowledgeDocument, document_id)
    if row is None or row.status == "removed":
        raise HTTPException(404, "Knowledge document not found")
    require_role(str(row.project_id), user, db, {"admin"})
    audit(db, row.workspace_id, user.id, "knowledge_document.deleted", "knowledge_document", row.id,
          {"content_hash": row.content_hash, "version": row.version}, row.project_id)
    has_context_refs = db.query(InvestigationRunContextReference).filter_by(
        source_document_id=row.id).first() is not None
    if has_context_refs:
        row.status = "removed"
        row.processing_error = "Removed from active retrieval; retained for investigation provenance."
    else:
        db.delete(row)
    db.commit()
    return None


@app.get("/api/v1/projects/{project_id}/github/repositories")
def list_github_repositories(project_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project = project_or_404(db, project_id, user)
    return [iso(row) for row in db.query(GitHubRepositoryMapping).filter_by(
        workspace_id=project.workspace_id, project_id=project.id).order_by(GitHubRepositoryMapping.repository_full_name).all()]


@app.post("/api/v1/integrations/{integration_id}/github/repositories", status_code=201)
def map_github_repository(integration_id: UUID, payload: GitHubRepositoryMappingCreate,
        user: User = Depends(current_user), db: Session = Depends(get_db)):
    integration = db.get(Integration, integration_id)
    if not integration or integration.provider != "github":
        raise HTTPException(404, "GitHub integration not found")
    project = project_or_404(db, integration.project_id, user)
    require_role(str(project.id), user, db, {"admin"})
    if payload.service_id:
        service = db.query(Service).filter_by(workspace_id=project.workspace_id, project_id=project.id,
            id=payload.service_id).one_or_none()
        if service is None:
            raise HTTPException(404, "Project service not found")
    row = db.query(GitHubRepositoryMapping).filter_by(integration_id=integration.id,
        repository_id=payload.repository_id).one_or_none()
    if row is None:
        row = GitHubRepositoryMapping(workspace_id=project.workspace_id, project_id=project.id,
            integration_id=integration.id, repository_id=payload.repository_id,
            repository_full_name=payload.repository_full_name, service_id=payload.service_id)
        db.add(row)
    else:
        row.repository_full_name = payload.repository_full_name
        row.service_id = payload.service_id
    audit(db, project.workspace_id, user.id, "github.repository_mapped", "github_repository_mapping", row.id,
          {"repository": payload.repository_full_name, "service_id": str(payload.service_id) if payload.service_id else None}, project.id)
    db.commit()
    db.refresh(row)
    return iso(row)


@app.delete("/api/v1/github/repositories/{mapping_id}", status_code=204)
def delete_github_repository_mapping(mapping_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = db.get(GitHubRepositoryMapping, mapping_id)
    if row is None:
        raise HTTPException(404, "Repository mapping not found")
    project_or_404(db, row.project_id, user)
    require_role(str(row.project_id), user, db, {"admin"})
    audit(db, row.workspace_id, user.id, "github.repository_unmapped", "github_repository_mapping", row.id,
          {"repository": row.repository_full_name}, row.project_id)
    db.delete(row)
    db.commit()
    return None


@app.get("/api/v1/projects/{project_id}/github/events")
def github_events(project_id: UUID, limit: int = Query(50, ge=1, le=200),
        user: User = Depends(current_user), db: Session = Depends(get_db)):
    project = project_or_404(db, project_id, user)
    return [iso(row) for row in db.query(GitHubDelivery).filter_by(
        workspace_id=project.workspace_id, project_id=project.id).order_by(
            GitHubDelivery.received_at.desc()).limit(limit).all()]


@app.post("/api/v1/webhooks/github/{integration_id}", status_code=202)
async def github_webhook(integration_id: UUID, request: Request,
        delivery_id: str | None = Header(default=None, alias="X-GitHub-Delivery"),
        event_type: str | None = Header(default=None, alias="X-GitHub-Event"),
        signature: str | None = Header(default=None, alias="X-Hub-Signature-256"),
        db: Session = Depends(get_db)):
    integration = db.get(Integration, integration_id)
    if not integration or integration.provider != "github" or not integration.enabled:
        raise HTTPException(404, "GitHub integration not found or disabled")
    if not delivery_id or len(delivery_id) > 200 or not event_type or len(event_type) > 40:
        raise HTTPException(400, "GitHub delivery headers are missing or invalid")
    length = request.headers.get("content-length")
    if length and (not length.isdigit() or int(length) > 1_000_000):
        raise HTTPException(413, "GitHub payload exceeds the webhook size limit")
    body = await request.body()
    if len(body) > 1_000_000:
        raise HTTPException(413, "GitHub payload exceeds the webhook size limit")
    from app.services.github import ingest_delivery, valid_signature
    try:
        credentials = connector_registry.credentials_for(integration)
    except Exception:
        raise HTTPException(503, "GitHub webhook credentials are unavailable")
    secret = credentials.get("webhook_secret") if isinstance(credentials, dict) else None
    if not valid_signature(body, signature, secret):
        raise HTTPException(401, "GitHub webhook signature is invalid")
    try:
        payload = json.loads(body)
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        row, deduplicated = ingest_delivery(db, integration, delivery_id, event_type, payload)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, "GitHub webhook payload is invalid") from exc
    except IntegrityError:
        db.rollback()
        row = db.query(GitHubDelivery).filter_by(integration_id=integration.id, delivery_id=delivery_id).one_or_none()
        if row is None:
            raise HTTPException(409, "GitHub delivery could not be recorded")
        deduplicated = True
    audit(db, integration.workspace_id, None, "github.webhook_received", "github_delivery", row.id,
          {"event_type": event_type, "deduplicated": deduplicated, "status": row.status}, integration.project_id)
    db.commit()
    return {"accepted": True, "deduplicated": deduplicated, "status": row.status}


@app.get("/api/v1/projects/{project_id}/jira/project-mapping")
def get_jira_mapping(project_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project = project_or_404(db, project_id, user)
    row = db.query(JiraProjectMapping).filter_by(workspace_id=project.workspace_id, project_id=project.id).one_or_none()
    return iso(row) if row else None


@app.get("/api/v1/projects/{project_id}/jira/issues")
def list_jira_issues(project_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project = project_or_404(db, project_id, user)
    return [iso(row) for row in db.query(ExternalIssueLink).join(Integration,
        Integration.id == ExternalIssueLink.integration_id).filter(
            ExternalIssueLink.workspace_id == project.workspace_id,
            ExternalIssueLink.project_id == project.id, Integration.provider == "jira")
        .order_by(ExternalIssueLink.updated_at.desc()).limit(200).all()]


@app.put("/api/v1/integrations/{integration_id}/jira/project-mapping")
def put_jira_mapping(integration_id: UUID, payload: JiraProjectMappingCreate,
        user: User = Depends(current_user), db: Session = Depends(get_db)):
    integration = db.get(Integration, integration_id)
    if not integration or integration.provider != "jira":
        raise HTTPException(404, "Jira integration not found")
    project = project_or_404(db, integration.project_id, user)
    require_role(str(project.id), user, db, {"admin"})
    row = db.query(JiraProjectMapping).filter_by(integration_id=integration.id).one_or_none()
    if row is None:
        row = JiraProjectMapping(workspace_id=project.workspace_id, project_id=project.id,
            integration_id=integration.id, jira_project_key=payload.jira_project_key,
            jira_project_name=payload.jira_project_name)
        db.add(row)
    else:
        row.jira_project_key = payload.jira_project_key
        row.jira_project_name = payload.jira_project_name
    audit(db, project.workspace_id, user.id, "jira.project_mapped", "jira_project_mapping", row.id,
          {"jira_project_key": row.jira_project_key}, project.id)
    db.commit()
    db.refresh(row)
    return iso(row)


@app.post("/api/v1/recommendations/{recommendation_id}/jira-issue")
def create_jira_issue(recommendation_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    recommendation = db.get(Recommendation, recommendation_id)
    if recommendation is None:
        raise HTTPException(404, "Recommendation not found")
    project = project_or_404(db, recommendation.project_id, user)
    require_role(str(project.id), user, db, {"admin"})
    approved = db.query(ApprovalRequest).filter_by(workspace_id=project.workspace_id,
        project_id=project.id, recommendation_id=recommendation.id, status="approved").first()
    if approved is None:
        raise HTTPException(409, "A human-approved recommendation is required before Jira issue creation")
    integration = db.query(Integration).filter_by(project_id=project.id, provider="jira", enabled=True).one_or_none()
    if integration is None:
        raise HTTPException(409, "An enabled Jira integration is required")
    mapping = db.query(JiraProjectMapping).filter_by(integration_id=integration.id).one_or_none()
    if mapping is None:
        raise HTTPException(409, "Map this IncidentIQ project to a Jira project first")
    link = db.query(ExternalIssueLink).filter_by(recommendation_id=recommendation.id,
        integration_id=integration.id).one_or_none()
    if link is None:
        idempotency = sha256(f"jira:{integration.id}:{recommendation.id}".encode()).hexdigest()
        link = ExternalIssueLink(workspace_id=project.workspace_id, project_id=project.id,
            incident_id=recommendation.incident_id, recommendation_id=recommendation.id,
            integration_id=integration.id, idempotency_key=idempotency,
            jira_project_key=mapping.jira_project_key, status="pending")
        db.add(link)
        db.flush()
    if link.status == "created":
        return iso(link)
    link.status = "pending"
    link.sync_error = ""
    db.commit()
    marker = "IIQ" + link.idempotency_key[:32].upper()
    try:
        from app.services.jira import (JiraAdapter, JiraProviderError, build_incident_description,
                                       incident_url_for_origin, jira_priority_for_risk)
        credentials = connector_registry.credentials_for(integration)
        adapter = JiraAdapter(integration.config.get("base_url", ""), credentials.get("email", ""),
                              credentials.get("api_token", ""))
        incident = db.query(Incident).filter_by(workspace_id=project.workspace_id,
            project_id=project.id, id=recommendation.incident_id).one_or_none()
        if incident is None:
            raise JiraProviderError("The recommendation's IncidentIQ incident is unavailable")
        incident_url = incident_url_for_origin(settings.frontend_origin, str(incident.id))
        investigation = db.get(InvestigationRun, recommendation.run_id) if recommendation.run_id else None
        investigation_context = ""
        if investigation is not None:
            investigation_context = (f"Run ID: {investigation.id}; provider: "
                f"{investigation.provider_name or 'unknown'}; model: {investigation.model_id or 'unknown'}; "
                f"status: {investigation.status}")
        issue_description = build_incident_description(
            incident_id=incident.id, incident_title=incident.title, incident_url=incident_url,
            recommendation_description=recommendation.description,
            expected_benefit=recommendation.expected_benefit,
            required_permissions=recommendation.required_permissions,
            verification_steps=recommendation.verification_steps,
            evidence_refs=recommendation.evidence_refs,
            investigation_context=investigation_context,
        )
        result = adapter.create_or_reconcile(project_key=mapping.jira_project_key,
            summary=recommendation.title, description=issue_description, marker=marker,
            priority=jira_priority_for_risk(recommendation.risk_level))
        link.external_issue_id = str(result["id"])[:80]
        link.external_issue_key = str(result["key"])[:80]
        link.external_issue_url = adapter.base_url + "/browse/" + link.external_issue_key
        link.external_status = str((result.get("fields") or {}).get("status", {}).get("name", ""))[:80]
        link.status = "created"
        link.sync_error = ""
        audit(db, project.workspace_id, user.id, "jira.issue_created", "external_issue_link", link.id,
              {"issue_key": link.external_issue_key, "recommendation_id": str(recommendation.id)}, project.id)
    except Exception as exc:
        link.status = "failed"
        link.sync_error = str(exc)[:500] if exc.__class__.__name__ == "JiraProviderError" else "Jira issue creation failed; retry to reconcile its idempotency marker."
        audit(db, project.workspace_id, user.id, "jira.issue_creation_failed", "external_issue_link", link.id,
              {"error_type": type(exc).__name__}, project.id)
        db.commit()
        raise HTTPException(502, link.sync_error)
    db.commit()
    db.refresh(link)
    return iso(link)


@app.post("/api/v1/external-issue-links/{link_id}/sync")
def sync_jira_issue(link_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    link = db.get(ExternalIssueLink, link_id)
    if link is None:
        raise HTTPException(404, "External issue link not found")
    project_or_404(db, link.project_id, user)
    require_role(str(link.project_id), user, db, {"admin"})
    integration = db.get(Integration, link.integration_id)
    if integration is None or not link.external_issue_key:
        raise HTTPException(409, "Jira issue is not linked")
    try:
        credentials = connector_registry.credentials_for(integration)
        from app.services.jira import JiraAdapter
        adapter = JiraAdapter(integration.config.get("base_url", ""), credentials.get("email", ""),
                              credentials.get("api_token", ""))
        link.external_status = adapter.issue_status(link.external_issue_key)
        link.last_synced_at = datetime.now(timezone.utc)
        link.sync_error = ""
        audit(db, link.workspace_id, user.id, "jira.issue_status_synced", "external_issue_link", link.id,
              {"external_status": link.external_status}, link.project_id)
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(502, "Jira status synchronization failed") from exc
    return iso(link)


@app.post("/api/v1/webhooks/alertmanager/{integration_id}")
async def alertmanager_webhook(integration_id: UUID, payload: AlertmanagerWebhookPayload,
                              authorization: str | None = Header(default=None),
                              db: Session = Depends(get_db)):
    row = db.query(Integration).filter_by(id=integration_id, provider="alertmanager", enabled=True).one_or_none()
    if row is None:
        raise HTTPException(404, "Alertmanager integration not found")
    try:
        credentials = connector_registry.credentials_for(row)
    except Exception:
        raise HTTPException(503, "Webhook authentication is unavailable") from None
    expected = credentials.get("webhook_token") or credentials.get("token")
    supplied = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else ""
    if not isinstance(expected, str) or not expected or not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(401, "Invalid webhook credentials")
    # A small per-integration Redis window protects the unauthenticated public route.
    try:
        import redis
        bucket = int(datetime.now(timezone.utc).timestamp() // 60)
        client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=1)
        key = f"incidentiq:alertmanager:rate:{integration_id}:{bucket}"
        count = client.incr(key)
        if count == 1:
            client.expire(key, 120)
        if count > 120:
            raise HTTPException(429, "Webhook rate limit exceeded")
    except HTTPException:
        raise
    except Exception:
        # Fail closed if rate-limiting infrastructure is unavailable.
        raise HTTPException(503, "Webhook ingestion is temporarily unavailable") from None
    from app.services.alertmanager import process_alertmanager_webhook
    return process_alertmanager_webhook(db, row, payload)


@app.post("/api/v1/incidents/{incident_id}/telemetry/collect", status_code=202)
def request_telemetry_collection(incident_id: UUID, payload: TelemetryCollectRequest,
                                 user: User = Depends(current_user), db: Session = Depends(get_db)):
    incident = incident_or_404(db, incident_id, user)
    require_role(str(incident.project_id), user, db, {"admin", "member"})
    now = datetime.now(timezone.utc)
    end = payload.end or now
    start = payload.start or end - timedelta(hours=1)
    if start.tzinfo is None or end.tzinfo is None or end <= start or end - start > timedelta(hours=24):
        raise HTTPException(422, "Collection range must be ordered and no longer than 24 hours")
    key = f"telemetry:{incident.id}:{int(start.timestamp())}:{int(end.timestamp())}"
    event = db.query(OutboxEvent).filter_by(idempotency_key=key).one_or_none()
    if event is None:
        event = OutboxEvent(workspace_id=incident.workspace_id, project_id=incident.project_id,
            aggregate_type="incident", aggregate_id=incident.id, event_type="telemetry.collect.requested",
            payload={"incident_id": str(incident.id), "start": start.isoformat(), "end": end.isoformat()},
            idempotency_key=key)
        db.add(event)
        audit(db, incident.workspace_id, user.id, "incident.telemetry_collection_requested", "incident", incident.id,
              {"start": start.isoformat(), "end": end.isoformat()}, incident.project_id)
        db.commit()
        try:
            from app.workers import dispatch_outbox_event
            dispatch_outbox_event.delay(str(event.id))
        except Exception:
            pass  # the periodic outbox dispatcher recovers committed requests
    return {"status": event.status, "outbox_event_id": str(event.id)}


@app.delete("/api/v1/integrations/{integration_id}", status_code=204)
def disconnect_integration(integration_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = db.get(Integration, integration_id)
    if not row: raise HTTPException(404, "Integration not found")
    require_role(str(row.project_id), user, db, {"admin"})
    audit(db, row.workspace_id, user.id, "integration.disconnected", "integration", row.id, {"provider": row.provider}, row.project_id)
    db.delete(row); db.commit(); return None


@app.get("/api/v1/projects/{project_id}/issues")
def list_issues(project_id: UUID, status: str | None = None, q: str | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project_or_404(db, project_id, user)
    query = db.query(Issue).filter_by(project_id=project_id)
    if status:
        query = query.filter(Issue.status == status)
    if q:
        query = query.filter(or_(Issue.title.ilike(f"%{q}%"), Issue.key.ilike(f"%{q}%"), Issue.description.ilike(f"%{q}%")))
    return [iso(x) for x in query.order_by(Issue.updated_at.desc()).limit(300)]


@app.get("/api/v1/issues/{issue_id}")
def issue_detail(issue_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = db.get(Issue, issue_id)
    if not row:
        raise HTTPException(404, "Issue not found")
    project_or_404(db, row.project_id, user)
    return {**iso(row),
        "comments": [iso(x) for x in db.query(IssueComment).filter_by(issue_id=row.id).order_by(IssueComment.created_at).all()],
        "activity": [iso(x) for x in db.query(IssueActivity).filter_by(issue_id=row.id).order_by(IssueActivity.created_at.desc()).all()]}


@app.post("/api/v1/projects/{project_id}/issues", status_code=201)
def create_issue(project_id: UUID, payload: IssueCreate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project = project_or_404(db, project_id, user)
    require_role(str(project_id), user, db, {"admin", "member"})
    count = db.query(Issue).filter_by(project_id=project.id).count() + 1
    row = Issue(workspace_id=project.workspace_id, project_id=project.id, key=f"INC-{count:04d}",
                reporter_id=user.id, **payload.model_dump())
    db.add(row)
    db.flush()
    db.add(IssueActivity(workspace_id=project.workspace_id, project_id=project.id, issue_id=row.id,
                         actor_id=user.id, event_type="created", new_value=row.title))
    audit(db, project.workspace_id, user.id, "issue.created", "issue", row.id, project_id=project.id)
    db.commit()
    db.refresh(row)
    return iso(row)


@app.patch("/api/v1/issues/{issue_id}")
def update_issue(issue_id: UUID, payload: IssuePatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = db.get(Issue, issue_id)
    if not row:
        raise HTTPException(404, "Issue not found")
    require_role(str(row.project_id), user, db, {"admin", "member"})
    changes = payload.model_dump(exclude_unset=True)
    if "status" in changes and changes["status"] != row.status:
        if changes["status"] not in ALLOWED_ISSUE_TRANSITIONS[row.status]:
            raise HTTPException(422, f"Invalid issue transition: {row.status} â†’ {changes['status']}")
    for key, value in changes.items():
        old = getattr(row, key)
        setattr(row, key, value)
        if old != value:
            db.add(IssueActivity(workspace_id=row.workspace_id, project_id=row.project_id, issue_id=row.id,
                actor_id=user.id, event_type=f"{key}.changed", old_value=str(old), new_value=str(value)))
    row.version += 1
    audit(db, row.workspace_id, user.id, "issue.updated", "issue", row.id,
          {"changed_fields": sorted(changes)}, row.project_id)
    db.commit()
    db.refresh(row)
    return iso(row)


@app.delete("/api/v1/issues/{issue_id}", status_code=204)
def delete_issue(issue_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = db.get(Issue, issue_id)
    if not row:
        raise HTTPException(404, "Issue not found")
    require_role(str(row.project_id), user, db, {"admin"})
    audit(db, row.workspace_id, user.id, "issue.deleted", "issue", row.id, project_id=row.project_id)
    db.delete(row)
    db.commit()
    return None


@app.get("/api/v1/issues/{issue_id}/comments")
def issue_comments(issue_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    issue = db.get(Issue, issue_id)
    if not issue: raise HTTPException(404, "Issue not found")
    project_or_404(db, issue.project_id, user)
    return [iso(x) for x in db.query(IssueComment).filter_by(issue_id=issue.id).order_by(IssueComment.created_at)]


@app.post("/api/v1/issues/{issue_id}/comments", status_code=201)
def add_comment(issue_id: UUID, payload: CommentCreate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    issue = db.get(Issue, issue_id)
    if not issue: raise HTTPException(404, "Issue not found")
    project_or_404(db, issue.project_id, user)
    require_role(str(issue.project_id), user, db, {"admin", "member"})
    row = IssueComment(workspace_id=issue.workspace_id, project_id=issue.project_id, issue_id=issue.id, author_id=user.id, body=payload.body)
    db.add(row)
    db.add(IssueActivity(workspace_id=issue.workspace_id, project_id=issue.project_id, issue_id=issue.id, actor_id=user.id, event_type="commented", new_value=payload.body[:200]))
    db.commit()
    db.refresh(row)
    return iso(row)


@app.get("/api/v1/projects/{project_id}/incidents")
def list_incidents(project_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project_or_404(db, project_id, user)
    rows = db.query(Incident).filter_by(project_id=project_id).order_by(Incident.detected_at.desc()).all()
    return [iso(x) for x in rows]


@app.post("/api/v1/projects/{project_id}/incidents", status_code=201)
def create_incident(project_id: UUID, payload: IncidentCreate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project = project_or_404(db, project_id, user)
    require_role(str(project_id), user, db, {"admin", "member"})
    if payload.assignee_id and not db.query(ProjectMember).filter_by(workspace_id=project.workspace_id, project_id=project.id, user_id=payload.assignee_id).first():
        raise HTTPException(422, "Incident assignee must be a member of this project")
    if len(set(payload.service_ids)) != len(payload.service_ids): raise HTTPException(422, "Affected services must be unique")
    for service_id in payload.service_ids:
        if not db.query(Service).filter_by(workspace_id=project.workspace_id, project_id=project.id, id=service_id).first():
            raise HTTPException(422, "Affected service must belong to this project")
    incident = Incident(workspace_id=project.workspace_id, project_id=project.id, title=payload.title,
                        description=payload.description, severity=payload.severity, reporter_id=user.id,
                        impact=payload.impact, assignee_id=payload.assignee_id,
                        started_at=payload.started_at or datetime.now(timezone.utc))
    db.add(incident)
    db.flush()
    db.add(IncidentActivity(workspace_id=project.workspace_id, project_id=project.id, incident_id=incident.id,
                            actor_id=user.id, event_type="created", detail={"title": incident.title}))
    for service_id in payload.service_ids:
        db.add(IncidentService(workspace_id=project.workspace_id, project_id=project.id,
                               incident_id=incident.id, service_id=service_id))
    audit(db, project.workspace_id, user.id, "incident.created", "incident", incident.id, project_id=project.id)
    db.commit()
    db.refresh(incident)
    return iso(incident)


@app.get("/api/v1/incidents/{incident_id}")
def incident_detail(incident_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    incident = incident_or_404(db, incident_id, user)
    evidence = db.query(EvidenceItem).filter_by(incident_id=incident.id).order_by(EvidenceItem.observed_at.desc()).all()
    runs = db.query(InvestigationRun).filter_by(incident_id=incident.id).order_by(InvestigationRun.created_at.desc()).all()
    services = db.query(Service).join(IncidentService, IncidentService.service_id == Service.id).filter(IncidentService.incident_id == incident.id).all()
    links = db.query(IncidentIssueLink, Issue).join(Issue, Issue.id == IncidentIssueLink.issue_id).filter(IncidentIssueLink.incident_id == incident.id).all()
    activity = db.query(IncidentActivity).filter_by(workspace_id=incident.workspace_id, project_id=incident.project_id, incident_id=incident.id).order_by(IncidentActivity.created_at.desc()).all()
    latest_sync = {}
    for run in db.query(IntegrationSyncRun).filter_by(workspace_id=incident.workspace_id,
            project_id=incident.project_id, incident_id=incident.id).order_by(IntegrationSyncRun.started_at.desc()).all():
        latest_sync.setdefault(run.source_type, run)
    enabled_sources = {row.provider: row for row in db.query(Integration).filter_by(
        workspace_id=incident.workspace_id, project_id=incident.project_id, enabled=True).all()}
    source_map = {"prometheus": "metrics", "loki": "application_log", "tempo": "traces"}
    telemetry_sources = []
    for provider, source_type in source_map.items():
        run = latest_sync.get(provider)
        telemetry_sources.append({"provider": provider, "source_type": source_type,
            "configured": provider in enabled_sources,
            "status": run.status if run else ("not_collected" if provider in enabled_sources else "not_configured"),
            "result_count": run.result_count if run else 0,
            "error_code": run.error_code if run else "",
            "last_checked_at": run.completed_at if run else None})
    membership = require_project(str(incident.project_id), user, db)
    return {**iso(incident), "viewer_role": membership.role, "evidence": [iso(x) for x in evidence], "runs": [iso(x) for x in runs],
            "services": [iso(x) for x in services], "issues": [iso(i) for _, i in links], "activity": [iso(x) for x in activity],
            "telemetry_sources": telemetry_sources}


INCIDENT_TRANSITIONS = {"open": {"acknowledged", "investigating"}, "acknowledged": {"investigating", "mitigated"},
                        "investigating": {"acknowledged", "mitigated", "resolved"}, "mitigated": {"investigating", "resolved"},
                        "resolved": {"closed", "investigating"}, "closed": set()}


@app.patch("/api/v1/incidents/{incident_id}")
def update_incident(incident_id: UUID, payload: IncidentPatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    incident = incident_or_404(db, incident_id, user)
    require_role(str(incident.project_id), user, db, {"admin", "member"})
    changes = payload.model_dump(exclude_unset=True)
    if "status" in changes and changes["status"] != incident.status and changes["status"] not in INCIDENT_TRANSITIONS.get(incident.status, set()):
        raise HTTPException(422, "Invalid incident status transition")
    if changes.get("severity") and changes["severity"] not in {"SEV-1", "SEV-2", "SEV-3", "SEV-4"}:
        raise HTTPException(422, "Invalid incident severity")
    if "assignee_id" in changes and changes["assignee_id"] and not db.query(ProjectMember).filter_by(workspace_id=incident.workspace_id, project_id=incident.project_id, user_id=changes["assignee_id"]).first():
        raise HTTPException(422, "Incident assignee must be a member of this project")
    service_ids = changes.pop("service_ids", None)
    if service_ids is not None:
        for sid in service_ids:
            if not db.query(Service).filter_by(workspace_id=incident.workspace_id, project_id=incident.project_id, id=sid).first():
                raise HTTPException(422, "Affected service must belong to this project")
        db.query(IncidentService).filter_by(workspace_id=incident.workspace_id, project_id=incident.project_id, incident_id=incident.id).delete()
        for sid in service_ids:
            db.add(IncidentService(workspace_id=incident.workspace_id, project_id=incident.project_id, incident_id=incident.id, service_id=sid))
    old_values = {}
    for key, value in changes.items():
        old = getattr(incident, key)
        if old != value:
            old_values[key] = str(old) if old is not None else None
            setattr(incident, key, value)
    if changes.get("status") == "resolved": incident.resolved_at = datetime.now(timezone.utc)
    if changes.get("status") == "closed": incident.closed_at = datetime.now(timezone.utc)
    if old_values or service_ids is not None:
        detail = {"old": old_values, "new": {k: str(v) if isinstance(v, UUID) else v for k, v in changes.items()}, "service_ids": [str(x) for x in service_ids] if service_ids is not None else None}
        db.add(IncidentActivity(workspace_id=incident.workspace_id, project_id=incident.project_id, incident_id=incident.id, actor_id=user.id, event_type="updated", detail=detail))
        audit(db, incident.workspace_id, user.id, "incident.updated", "incident", incident.id,
              {"changed_fields": sorted([*changes, *(["service_ids"] if service_ids is not None else [])])},
              incident.project_id)
    db.commit(); db.refresh(incident); return iso(incident)


@app.post("/api/v1/incidents/{incident_id}/comments", status_code=201)
def add_incident_comment(incident_id: UUID, payload: IncidentCommentCreate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    incident = incident_or_404(db, incident_id, user)
    require_role(str(incident.project_id), user, db, {"admin", "member"})
    row = IncidentActivity(workspace_id=incident.workspace_id, project_id=incident.project_id, incident_id=incident.id,
                           actor_id=user.id, event_type="comment", detail={"body": payload.body})
    db.add(row); audit(db, incident.workspace_id, user.id, "incident.comment_added", "incident", incident.id, project_id=incident.project_id)
    db.commit(); db.refresh(row); return iso(row)


@app.post("/api/v1/incidents/{incident_id}/evidence", status_code=201)
def add_evidence(incident_id: UUID, payload: EvidenceCreate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    incident = incident_or_404(db, incident_id, user)
    require_role(str(incident.project_id), user, db, {"admin", "member"})
    digest = sha256(payload.summary.encode()).hexdigest()
    existing = db.query(EvidenceItem).filter_by(incident_id=incident.id, source_type=payload.source_type, content_hash=digest).one_or_none()
    if existing:
        return {**iso(existing), "deduplicated": True}
    row = EvidenceItem(workspace_id=incident.workspace_id, project_id=incident.project_id, incident_id=incident.id,
                       source_type=payload.source_type, summary=payload.summary, observed_at=payload.observed_at or datetime.now(timezone.utc),
                       content_hash=digest, metadata_json=payload.metadata_json)
    db.add(row)
    db.add(IncidentActivity(workspace_id=incident.workspace_id, project_id=incident.project_id, incident_id=incident.id,
                            actor_id=user.id, event_type="evidence_added", detail={"source_type": payload.source_type, "content_hash": digest}))
    db.flush()
    audit(db, incident.workspace_id, user.id, "incident.evidence_added", "evidence", row.id, {"source_type": payload.source_type}, incident.project_id)
    db.commit()
    db.refresh(row)
    return iso(row)


@app.post("/api/v1/incidents/{incident_id}/evidence/upload", status_code=201)
async def upload_evidence(incident_id: UUID, source_type: str = Form("application_log"), file: UploadFile = File(...),
                          user: User = Depends(current_user), db: Session = Depends(get_db)):
    incident = incident_or_404(db, incident_id, user)
    require_role(str(incident.project_id), user, db, {"admin", "member"})
    allowed = {"text/plain", "text/csv", "application/json", "application/octet-stream"}
    if file.content_type not in allowed:
        raise HTTPException(415, "Upload a text, CSV, or JSON diagnostic file")
    content = await file.read(5_000_001)
    if len(content) > 5_000_000:
        raise HTTPException(413, "Evidence files are limited to 5 MB in the demo")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(415, "Evidence upload must be UTF-8 text")
    digest = sha256(content).hexdigest()
    existing = db.query(EvidenceItem).filter_by(incident_id=incident.id, source_type=source_type, content_hash=digest).one_or_none()
    if existing:
        return {**iso(existing), "deduplicated": True}
    artifact_dir = Path(settings.artifact_dir).resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / digest
    artifact_path.write_bytes(content)
    summary = text[:12000]
    row = EvidenceItem(workspace_id=incident.workspace_id, project_id=incident.project_id, incident_id=incident.id,
                       source_type=source_type, source_ref=file.filename or "upload", summary=summary,
                       content_hash=digest, artifact_key=str(artifact_path),
                       metadata_json={"content_type": file.content_type, "size": len(content)})
    db.add(row)
    db.add(IncidentActivity(workspace_id=incident.workspace_id, project_id=incident.project_id, incident_id=incident.id,
                            actor_id=user.id, event_type="evidence_uploaded", detail={"source_type": source_type, "content_hash": digest}))
    db.flush()
    audit(db, incident.workspace_id, user.id, "incident.evidence_uploaded", "evidence", row.id, {"source_type": source_type}, incident.project_id)
    db.commit()
    db.refresh(row)
    return iso(row)


@app.post("/api/v1/incidents/{incident_id}/investigations", status_code=202)
def start_investigation(incident_id: UUID, idempotency_key: str = Query(..., min_length=8, max_length=100),
                        user: User = Depends(current_user), db: Session = Depends(get_db)):
    incident = incident_or_404(db, incident_id, user)
    require_role(str(incident.project_id), user, db, {"admin", "member"})
    if incident.status in {"resolved", "closed"}:
        raise HTTPException(409, "Cannot investigate an incident that is already resolved or closed")
    if incident.status != "investigating":
        old_status = incident.status; incident.status = "investigating"
        db.add(IncidentActivity(workspace_id=incident.workspace_id, project_id=incident.project_id, incident_id=incident.id,
                                actor_id=user.id, event_type="investigation_started", detail={"from": old_status, "to": "investigating"}))
        audit(db, incident.workspace_id, user.id, "incident.investigation_started", "incident", incident.id, project_id=incident.project_id)
    existing = db.query(InvestigationRun).filter_by(workspace_id=incident.workspace_id,
        project_id=incident.project_id, idempotency_key=idempotency_key).one_or_none()
    if existing:
        if existing.incident_id != incident.id:
            raise HTTPException(409, "This idempotency key is already used by another incident in this project")
        return {**iso(existing), "deduplicated": True}
    active = db.query(InvestigationRun).filter(InvestigationRun.incident_id == incident.id,
        InvestigationRun.status.in_(ACTIVE_RUNS)).one_or_none()
    if active:
        return {**iso(active), "deduplicated": True}
    provider_name, provider_state, model_name = provider_mode()
    if provider_state == "unavailable":
        raise HTTPException(503, "The configured AI provider is unavailable or missing required configuration")
    run = InvestigationRun(workspace_id=incident.workspace_id, project_id=incident.project_id,
        incident_id=incident.id, requested_by=user.id, idempotency_key=idempotency_key,
        thread_id=f"investigation-{uuid4()}", status="queued",
        model_id=model_name or "unavailable", provider_name=provider_name, provider_mode=provider_state,
        model_version=model_name or "", prompt_version="incident-analysis-v1" if provider_name == "openai" else "demo-rules-v1")
    db.add(run)
    try:
        db.flush()
        event = OutboxEvent(workspace_id=incident.workspace_id, project_id=incident.project_id,
            aggregate_type="investigation_run", aggregate_id=run.id, event_type="investigation.requested",
            payload={"run_id": str(run.id)}, idempotency_key=f"investigation:{run.id}")
        db.add(event)
        audit(db, incident.workspace_id, user.id, "investigation.queued", "investigation_run", run.id, project_id=incident.project_id)
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.query(InvestigationRun).filter_by(workspace_id=incident.workspace_id,
            project_id=incident.project_id, idempotency_key=idempotency_key).one_or_none()
        if existing:
            if existing.incident_id != incident.id:
                raise HTTPException(409, "This idempotency key is already used by another incident in this project")
            return {**iso(existing), "deduplicated": True}
        active = db.query(InvestigationRun).filter(InvestigationRun.incident_id == incident.id,
            InvestigationRun.status.in_(ACTIVE_RUNS)).one_or_none()
        if active:
            return {**iso(active), "deduplicated": True}
        raise HTTPException(409, "An investigation with this idempotency key already exists")
    db.refresh(run)
    if settings.task_mode == "celery":
        from app.workers import dispatch_outbox_event
        dispatch_outbox_event.delay(str(event.id))
    else:
        from app.services.investigations import execute_run
        worker_id = f"inline:{uuid4()}"
        try:
            execute_run(str(run.id), worker_id=worker_id)
        except Exception as exc:
            from app.services.investigations import record_execution_failure
            record_execution_failure(str(run.id), worker_id, exc, retryable=False)
            raise
        event.status = "published"
        event.published_at = datetime.now(timezone.utc)
        event.attempt_count += 1
        db.commit()
        db.refresh(run)
        return iso(run)
    return iso(run)


@app.get("/api/v1/investigations/{run_id}")
def investigation_detail(run_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    run = db.get(InvestigationRun, run_id)
    if not run: raise HTTPException(404, "Investigation not found")
    require_project(str(run.project_id), user, db)
    steps = db.query(InvestigationStep).filter_by(run_id=run.id).all()
    attempts = db.query(InvestigationStepAttempt).filter_by(run_id=run.id).order_by(InvestigationStepAttempt.started_at).all()
    execution_attempts = db.query(InvestigationRunAttempt).filter_by(run_id=run.id).order_by(InvestigationRunAttempt.attempt_no).all()
    gaps = db.query(InvestigationRunSourceStatus).filter_by(run_id=run.id).all()
    run_evidence = db.query(InvestigationRunEvidence, EvidenceItem).join(EvidenceItem, EvidenceItem.id == InvestigationRunEvidence.evidence_id).filter(InvestigationRunEvidence.run_id == run.id).all()
    contexts = db.query(InvestigationRunContextReference).filter_by(run_id=run.id).all()
    findings = db.query(Finding).filter_by(run_id=run.id).order_by(Finding.rank).all()
    findings_json = []
    for finding in findings:
        citations = db.query(FindingEvidenceLink, EvidenceItem).join(EvidenceItem, EvidenceItem.id == FindingEvidenceLink.evidence_id).filter(FindingEvidenceLink.finding_id == finding.id).all()
        ctx = db.query(FindingContextLink, InvestigationRunContextReference).join(InvestigationRunContextReference, InvestigationRunContextReference.id == FindingContextLink.context_reference_id).filter(FindingContextLink.finding_id == finding.id).all()
        findings_json.append({**iso(finding), "evidence": [{**iso(e), "stance": l.stance, "explanation": l.explanation} for l, e in citations],
            "historical_context": [{**iso(c), "explanation": l.explanation} for l, c in ctx]})
    recs = db.query(Recommendation).filter_by(run_id=run.id).all()
    return {**iso(run), "steps": [iso(x) for x in steps], "attempts": [iso(x) for x in attempts],
        "execution_attempts": [iso(x) for x in execution_attempts],
        "source_statuses": [iso(x) for x in gaps],
        "evidence": [{**iso(e), "usage_type": link.usage_type, "collection_status": link.collection_status} for link, e in run_evidence],
        "historical_context": [iso(x) for x in contexts], "findings": findings_json, "recommendations": [iso(x) for x in recs]}


@app.patch("/api/v1/findings/{finding_id}/review")
def review_finding(finding_id: UUID, payload: ReviewDecision, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if payload.status not in {"confirmed", "rejected", "needs_more_evidence"}:
        raise HTTPException(422, "Invalid finding review decision")
    candidate = db.get(Finding, finding_id)
    if not candidate:
        raise HTTPException(404, "Finding not found")
    require_role(str(candidate.project_id), user, db, {"admin", "member"})
    run = db.query(InvestigationRun).filter_by(id=candidate.run_id).with_for_update().populate_existing().one()
    finding = db.query(Finding).filter_by(id=finding_id).with_for_update().populate_existing().one()
    if run.status != "waiting_for_review":
        raise HTTPException(409, "This investigation is not awaiting human review")
    if finding.review_status != "pending":
        if finding.review_status == payload.status:
            return iso(finding)
        raise HTTPException(409, "This finding has already been reviewed")
    finding.review_status = payload.status
    audit(db, finding.workspace_id, user.id, "finding.reviewed", "finding", finding.id,
          {"status": payload.status}, finding.project_id)
    # Sessions used by workers/tests can disable autoflush. Persist the decision
    # inside this transaction before evaluating whether any review remains.
    db.flush([finding])
    pending = db.query(Finding).filter_by(run_id=run.id, review_status="pending").count()
    if pending == 0:
        try:
            transition_run(run, "completed")
        except InvalidRunTransition as exc:
            raise HTTPException(409, str(exc))
        audit(db, run.workspace_id, user.id, "investigation.review_completed", "investigation_run",
              run.id, {"reviewed_finding_count": db.query(Finding).filter_by(run_id=run.id).count()}, run.project_id)
    db.commit()
    db.refresh(finding)
    return iso(finding)


@app.get("/api/v1/incidents/{incident_id}/recommendations")
def recommendations(incident_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    incident = incident_or_404(db, incident_id, user)
    rows = db.query(Recommendation).filter_by(incident_id=incident.id).all()
    result = []
    for row in rows:
        approvals = db.query(ApprovalRequest).filter_by(recommendation_id=row.id).all()
        external_links = db.query(ExternalIssueLink).filter_by(recommendation_id=row.id).all()
        result.append({**iso(row), "approvals": [iso(x) for x in approvals],
                       "external_issue_links": [iso(x) for x in external_links]})
    return result


@app.post("/api/v1/recommendations/{recommendation_id}/approvals", status_code=201)
def create_approval(recommendation_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    rec = db.get(Recommendation, recommendation_id)
    if not rec: raise HTTPException(404, "Recommendation not found")
    project_or_404(db, rec.project_id, user)
    require_role(str(rec.project_id), user, db, {"admin", "member"})
    payload = {"recommendation_id": str(rec.id), "title": rec.title, "description": rec.description,
        "risk": rec.risk_level, "expected_benefit": rec.expected_benefit,
        "evidence_refs": rec.evidence_refs, "required_permissions": rec.required_permissions,
        "verification_steps": rec.verification_steps, "mode": "simulation"}
    digest = sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    row = ApprovalRequest(workspace_id=rec.workspace_id, project_id=rec.project_id, recommendation_id=rec.id,
        requested_by=user.id, action_snapshot=payload, action_hash=digest)
    db.add(row)
    db.flush()
    audit(db, row.workspace_id, user.id, "approval.requested", "approval_request", row.id,
          {"recommendation_id": str(rec.id), "risk": rec.risk_level}, row.project_id)
    db.commit()
    db.refresh(row)
    return iso(row)


@app.post("/api/v1/approvals/{approval_id}/decision")
def decide_approval(approval_id: UUID, payload: ApprovalDecision, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = db.get(ApprovalRequest, approval_id)
    if not row: raise HTTPException(404, "Approval request not found")
    require_role(str(row.project_id), user, db, {"admin"})
    if row.status != "pending" or payload.status not in {"approved", "rejected"}:
        raise HTTPException(409, "Approval is already decided or decision is invalid")
    row.status = payload.status
    row.reviewed_by = user.id
    row.decision_note = payload.note
    row.decided_at = datetime.now(timezone.utc)
    audit(db, row.workspace_id, user.id, f"approval.{payload.status}", "approval_request", row.id, project_id=row.project_id)
    db.commit()
    return iso(row)


@app.post("/api/v1/approvals/{approval_id}/simulate", status_code=201)
def simulate_remediation(approval_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    row = db.get(ApprovalRequest, approval_id)
    if not row: raise HTTPException(404, "Approval request not found")
    require_role(str(row.project_id), user, db, {"admin"})
    if row.status != "approved":
        raise HTTPException(409, "Human approval is required before simulation")
    key = f"simulation:{row.id}"
    old = db.query(RemediationExecution).filter_by(idempotency_key=key).one_or_none()
    if old:
        return iso(old)
    result = RemediationExecution(workspace_id=row.workspace_id, project_id=row.project_id,
        approval_request_id=row.id, idempotency_key=key, status="simulated",
        result={"message": "No production systems were changed.", "action_hash": row.action_hash})
    db.add(result)
    audit(db, row.workspace_id, user.id, "remediation.simulated", "approval_request", row.id, project_id=row.project_id)
    db.commit()
    db.refresh(result)
    return iso(result)


@app.post("/api/v1/incidents/{incident_id}/resolve")
def resolve_incident(incident_id: UUID, payload: IncidentResolve, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if isinstance(payload, dict): payload = IncidentResolve.model_validate(payload)
    incident = incident_or_404(db, incident_id, user)
    require_role(str(incident.project_id), user, db, {"admin", "member"})
    from app.services.investigations import cancel_active_runs
    incident = db.query(Incident).filter_by(id=incident.id).with_for_update().one()
    cancel_active_runs(db, incident, user.id, "Incident resolved by a project member.")
    if incident.status == "closed": raise HTTPException(409, "A closed incident cannot be resolved again")
    incident.status = "resolved"
    incident.resolved_at = datetime.now(timezone.utc)
    incident.resolution_notes = payload.resolution_notes
    incident.confirmed_root_cause = payload.confirmed_root_cause
    db.add(IncidentActivity(workspace_id=incident.workspace_id, project_id=incident.project_id, incident_id=incident.id,
                            actor_id=user.id, event_type="resolved", detail={"resolution_notes": payload.resolution_notes, "confirmed_root_cause": payload.confirmed_root_cause}))
    audit(db, incident.workspace_id, user.id, "incident.resolved", "incident", incident.id, project_id=incident.project_id)
    db.commit()
    return iso(incident)


@app.post("/api/v1/incidents/{incident_id}/close")
def close_incident(incident_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    incident = incident_or_404(db, incident_id, user)
    require_role(str(incident.project_id), user, db, {"admin", "member"})
    if incident.status != "resolved": raise HTTPException(409, "Only resolved incidents can be closed")
    incident.status = "closed"; incident.closed_at = datetime.now(timezone.utc)
    db.add(IncidentActivity(workspace_id=incident.workspace_id, project_id=incident.project_id, incident_id=incident.id,
                            actor_id=user.id, event_type="closed", detail={}))
    audit(db, incident.workspace_id, user.id, "incident.closed", "incident", incident.id, project_id=incident.project_id)
    db.commit(); return iso(incident)


@app.post("/api/v1/projects/{project_id}/demo-scenario", status_code=201)
def seed_demo_scenario(project_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project = project_or_404(db, project_id, user)
    require_role(str(project_id), user, db, {"admin", "member"})
    key = "checkout-release-regression-v1"
    current = db.query(Incident).filter_by(project_id=project.id, demo_scenario_key=key).one_or_none()
    if current:
        return iso(current)
    service = db.query(Service).filter_by(project_id=project.id, name="checkout-api", environment="demo").one_or_none()
    if service is None:
        service = Service(workspace_id=project.workspace_id, project_id=project.id,
                          name="checkout-api", environment="demo", health="degraded")
        db.add(service)
        db.flush()
    historical_key = "checkout-timeout-history-v1"
    old = db.query(Incident).filter_by(project_id=project.id, demo_scenario_key=historical_key).one_or_none()
    if old is None:
        old = Incident(workspace_id=project.workspace_id, project_id=project.id,
                       title="Historical checkout timeout after connection pool change",
                       description="Resolved demo incident used only as historical context.",
                       severity="SEV-3", status="resolved", reporter_id=user.id,
                       resolution_notes="The connection pool limit was restored.",
                       confirmed_root_cause="A reduced connection pool limit caused request timeouts.",
                       demo_scenario_key=historical_key, resolved_at=datetime.now(timezone.utc))
        db.add(old)
        db.flush()
        old_summary = "Historical outcome: connection pool limit caused checkout request timeouts."
        db.add(EvidenceItem(workspace_id=project.workspace_id, project_id=project.id, incident_id=old.id,
            source_type="historical_incident", source_ref="seeded-demo", summary=old_summary,
            content_hash=sha256(old_summary.encode()).hexdigest(), metadata_json={"demo": True}))
    doc_title = "Checkout API release rollback and connection pool runbook"
    doc_text = "For checkout 5xx errors shortly after a deployment, compare the release timestamp with error-rate changes. Check connection pool limits and dependency timeouts. Rollback requires incident commander approval."
    document = db.query(KnowledgeDocument).filter_by(project_id=project.id, title=doc_title).one_or_none()
    if document is None:
        document = KnowledgeDocument(workspace_id=project.workspace_id, project_id=project.id,
            title=doc_title, source_ref="seeded-runbook", source_kind="runbook",
            content_hash=sha256(doc_text.encode()).hexdigest(), content=doc_text,
            outcome="A human must validate the cause before rollback.", filename="seeded-runbook.md",
            mime_type="text/markdown", file_size=len(doc_text.encode()), status="pending",
            processing_error="Seeded demo content is unembedded until a configured real embedding provider indexes it.")
        db.add(document)
    current = Incident(workspace_id=project.workspace_id, project_id=project.id,
        title="Checkout errors increased after release 2.4.0",
        description="Demo scenario: checkout error rate rose shortly after a release. Logs and deployment evidence are seeded; metrics and traces are intentionally unavailable.",
        severity="SEV-2", status="open", reporter_id=user.id, demo_scenario_key=key)
    db.add(current)
    db.flush()
    db.add(IncidentService(workspace_id=project.workspace_id, project_id=project.id,
                           incident_id=current.id, service_id=service.id))
    observations = [
        ("application_log", "ERROR checkout handler returned 503 after release version 2.4.0; payment request failed."),
        ("metric_snapshot", "Checkout error rate increased from 0.4% to 8.1% within five minutes of release 2.4.0."),
        ("deployment_event", "Deployment version 2.4.0 completed for checkout-api shortly before the first 503 errors."),
    ]
    for source_type, summary in observations:
        db.add(EvidenceItem(workspace_id=project.workspace_id, project_id=project.id, incident_id=current.id,
            source_type=source_type, source_ref="seeded-demo", summary=summary,
            content_hash=sha256(summary.encode()).hexdigest(), metadata_json={"demo": True, "scenario": key}))
    audit(db, project.workspace_id, user.id, "demo.scenario_seeded", "incident", current.id, project_id=project.id)
    db.commit()
    db.refresh(current)
    return iso(current)


@app.post("/api/v1/incidents/{incident_id}/issues/{issue_id}", status_code=201)
def link_issue(incident_id: UUID, issue_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    incident = incident_or_404(db, incident_id, user)
    issue = db.get(Issue, issue_id)
    if not issue or issue.project_id != incident.project_id:
        raise HTTPException(404, "Issue not found in this project")
    require_role(str(incident.project_id), user, db, {"admin", "member"})
    link = db.query(IncidentIssueLink).filter_by(incident_id=incident.id, issue_id=issue.id).one_or_none()
    if link:
        return iso(link)
    link = IncidentIssueLink(workspace_id=incident.workspace_id, project_id=incident.project_id,
                             incident_id=incident.id, issue_id=issue.id, relationship_type="follow_up")
    db.add(link)
    db.flush()
    audit(db, incident.workspace_id, user.id, "incident.issue_linked", "incident_issue_link", issue.id,
          {"incident_id": str(incident.id), "issue_id": str(issue.id)}, incident.project_id)
    db.commit()
    return iso(link)


@app.delete("/api/v1/incidents/{incident_id}/issues/{issue_id}", status_code=204)
def unlink_issue(incident_id: UUID, issue_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    incident = incident_or_404(db, incident_id, user)
    require_role(str(incident.project_id), user, db, {"admin", "member"})
    link = db.query(IncidentIssueLink).filter_by(incident_id=incident.id, issue_id=issue_id).one_or_none()
    if link is None:
        raise HTTPException(404, "Incident-issue link not found")
    audit(db, incident.workspace_id, user.id, "incident.issue_unlinked", "incident_issue_link", issue_id,
          {"incident_id": str(incident.id), "issue_id": str(issue_id)}, incident.project_id)
    db.delete(link)
    db.commit()
    return None


@app.get("/api/v1/analytics/summary")
def analytics(project_id: UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    project_or_404(db, project_id, user)
    issue_counts = db.query(Issue.status, func.count(Issue.id)).filter_by(project_id=project_id).group_by(Issue.status).all()
    incident_counts = db.query(Incident.severity, func.count(Incident.id)).filter_by(project_id=project_id).group_by(Incident.severity).all()
    return {"issues_by_status": dict(issue_counts), "incidents_by_severity": dict(incident_counts),
            "open_incidents": db.query(Incident).filter(Incident.project_id == project_id, Incident.status.notin_(["resolved", "closed"])).count(),
            "issue_total": db.query(Issue).filter_by(project_id=project_id).count()}


@app.get("/api/v1/audit")
def audit_events(project_id: UUID, limit: int = Query(100, le=300), user: User = Depends(current_user), db: Session = Depends(get_db)):
    project = project_or_404(db, project_id, user)
    return [iso(x) for x in db.query(AuditEvent).filter_by(project_id=project.id).order_by(AuditEvent.created_at.desc()).limit(limit)]
