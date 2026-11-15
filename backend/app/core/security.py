import base64
import hashlib
import hmac
import json
import time
from uuid import UUID
from datetime import timedelta
from pwdlib import PasswordHash
from fastapi import Depends, HTTPException
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
from app.core.config import settings
from app.db import get_db
from app.models import User, ProjectMember

password_hash = PasswordHash.recommended()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")

# Product roles map to explicit capabilities; legacy "member" remains an engineer alias.
ROLE_ALIASES = {"admin": "project_admin", "member": "engineer", "owner": "organization_admin"}
ROLE_PERMISSIONS = {
    "organization_admin": {"workspace_manage", "project_manage", "integration_manage", "resource_write", "approve"},
    "project_admin": {"project_manage", "integration_manage", "resource_write", "approve"},
    "incident_manager": {"incident_manage", "resource_write", "approve"},
    "engineer": {"resource_write"},
    "viewer": set(),
}


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, encoded: str) -> bool:
    return password_hash.verify(password, encoded)


def create_token(user_id: str) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps({"sub": user_id, "exp": int(time.time()) + settings.access_token_minutes * 60}).encode()).decode().rstrip("=")
    body = f"{header}.{payload}"
    signature = hmac.new(settings.jwt_secret.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{base64.urlsafe_b64encode(signature).decode().rstrip('=')}"


def current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    try:
        header, payload, signature = token.split(".")
        body = f"{header}.{payload}"
        expected = base64.urlsafe_b64encode(hmac.new(settings.jwt_secret.encode(), body.encode(), hashlib.sha256).digest()).decode().rstrip("=")
        if not hmac.compare_digest(expected, signature):
            raise ValueError("signature")
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if claims["exp"] < time.time():
            raise ValueError("expired")
        user = db.get(User, UUID(claims["sub"]))
        if user is None or not user.active:
            raise ValueError("user")
        return user
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired access token", headers={"WWW-Authenticate": "Bearer"})


def require_project(project_id: str, user: User, db: Session) -> ProjectMember:
    membership = db.query(ProjectMember).filter_by(project_id=UUID(str(project_id)), user_id=user.id).first()
    if membership is None:
        raise HTTPException(status_code=403, detail="Explicit project membership is required")
    return membership


def require_role(project_id: str, user: User, db: Session, roles: set[str]) -> ProjectMember:
    membership = require_project(project_id, user, db)
    normalized = ROLE_ALIASES.get(membership.role, membership.role)
    accepted = {ROLE_ALIASES.get(role, role) for role in roles}
    if "member" in roles:
        accepted.add("incident_manager")
    if normalized not in accepted:
        raise HTTPException(status_code=403, detail="Your project role cannot perform this action")
    return membership


def require_permission(project_id: str, user: User, db: Session, permission: str) -> ProjectMember:
    membership = require_project(project_id, user, db)
    normalized = ROLE_ALIASES.get(membership.role, membership.role)
    if permission not in ROLE_PERMISSIONS.get(normalized, set()):
        raise HTTPException(status_code=403, detail="Your project role cannot perform this action")
    return membership
