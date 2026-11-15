from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException

from app.core.config import settings
from app.core.security import require_permission, require_role
from app.main import integration_out, mask_config, replace_integration_credentials
from app.models import ProjectMember
from app.schemas import IntegrationCredentialsUpdate
from app.services.credentials import decrypt_credentials, encrypt_credentials
from app.services.connectors import ConnectionResult, Connector, registry


def test_fernet_credentials_encrypt_and_decrypt_with_stable_key(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "integration_encryption_key", key)
    secret = {"access_token": "never-return-this"}
    encrypted = encrypt_credentials(secret)
    assert "never-return-this" not in encrypted
    assert decrypt_credentials(encrypted, provider="slack", connector_provider="slack") == secret


def test_encryption_fails_closed_for_missing_and_invalid_keys(monkeypatch):
    monkeypatch.setattr(settings, "integration_encryption_key", "")
    with pytest.raises(HTTPException) as missing:
        encrypt_credentials({"token": "secret"})
    assert missing.value.status_code == 503
    monkeypatch.setattr(settings, "integration_encryption_key", "not-a-fernet-key")
    with pytest.raises(HTTPException) as invalid:
        encrypt_credentials({"token": "secret"})
    assert invalid.value.status_code == 503


def test_masking_and_integration_api_serialization_never_expose_ciphertext():
    row = SimpleNamespace(id=uuid4(), workspace_id=uuid4(), project_id=uuid4(), provider="slack",
        config={"base_url": "https://example.invalid"}, permission_scope=[], status="disabled", enabled=False,
        health_status="unknown", last_successful_sync_at=None, last_error="", updated_at=datetime.now(timezone.utc),
        last_checked_at=None, last_test_result="not_tested", credentials_ciphertext="gAAAA-secret-ciphertext")
    result = integration_out(row)
    assert result["credentials"] == "********"
    assert result["has_credentials"] is True
    assert "gAAAA-secret-ciphertext" not in repr(result)
    assert mask_config({"nested": [{"password": "plaintext"}]}) == {"nested": [{"password": "********"}]}
    assert len(result["credentials"]) == 8
    assert mask_config({"token": "x", "secret": "a much longer value"}) == {
        "token": "********", "secret": "********"
    }
    assert mask_config({"base_url": "https://metrics.example/api?token=embedded-secret"}) == {
        "base_url": "********"
    }


def test_connector_catalog_marks_observability_adapters_implemented_and_validates_provider_config():
    assert {row["provider"] for row in registry.catalog()} >= {
        "prometheus", "alertmanager", "loki", "opentelemetry", "tempo", "grafana", "github", "jira", "slack"
    }
    connector = registry.get("prometheus")
    assert connector.implemented is True
    assert connector.test_connection({"base_url": "file:///tmp"}).result == ConnectionResult.INVALID_CONFIGURATION


def test_connector_credential_accessor_rejects_provider_mismatch(monkeypatch):
    monkeypatch.setattr(settings, "integration_encryption_key", Fernet.generate_key().decode())
    row = SimpleNamespace(provider="slack", credentials_ciphertext=encrypt_credentials({"token": "secret"}))
    with pytest.raises(PermissionError):
        decrypt_credentials(row.credentials_ciphertext, provider=row.provider, connector_provider="github")


class FakeQuery:
    def __init__(self, membership): self.membership = membership
    def filter_by(self, **kwargs): return self
    def first(self): return self.membership


class FakeDB:
    def __init__(self, membership): self.membership = membership
    def query(self, model):
        assert model is ProjectMember
        return FakeQuery(self.membership)


def test_rbac_denies_viewer_writes_and_allows_engineer_resource_edits():
    user = SimpleNamespace(id=uuid4())
    with pytest.raises(HTTPException) as denied:
        require_role(str(uuid4()), user, FakeDB(SimpleNamespace(role="viewer")), {"admin", "member"})
    assert denied.value.status_code == 403
    allowed = require_role(str(uuid4()), user, FakeDB(SimpleNamespace(role="engineer")), {"admin", "member"})
    assert allowed.role == "engineer"


@pytest.mark.parametrize("role,can_write,can_manage_integrations", [
    ("owner", True, True),  # Organization Admin permission alias
    ("admin", True, True),  # Project Admin
    ("incident_manager", True, False),
    ("engineer", True, False),
    ("viewer", False, False),
])
def test_all_role_capability_boundaries(role, can_write, can_manage_integrations):
    user = SimpleNamespace(id=uuid4())
    db = FakeDB(SimpleNamespace(role=role))
    if can_write:
        assert require_permission(str(uuid4()), user, db, "resource_write").role == role
    else:
        with pytest.raises(HTTPException) as denied:
            require_permission(str(uuid4()), user, db, "resource_write")
        assert denied.value.status_code == 403
    if can_manage_integrations:
        assert require_permission(str(uuid4()), user, db, "integration_manage").role == role
    else:
        with pytest.raises(HTTPException) as denied:
            require_permission(str(uuid4()), user, db, "integration_manage")
        assert denied.value.status_code == 403


def test_project_membership_is_required_even_for_existing_resource_ids():
    user = SimpleNamespace(id=uuid4())
    with pytest.raises(HTTPException) as denied:
        require_role(str(uuid4()), user, FakeDB(None), {"admin", "member"})
    assert denied.value.status_code == 403


def test_credential_replacement_api_returns_only_masked_secret(monkeypatch):
    class CredentialDB(FakeDB):
        def __init__(self, row): super().__init__(SimpleNamespace(role="admin")); self.row = row
        def get(self, model, key): return self.row
        def add(self, row): pass
        def commit(self): pass
        def refresh(self, row): pass

    import app.main as main
    monkeypatch.setattr(settings, "integration_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(main, "audit", lambda *args, **kwargs: None)
    row = SimpleNamespace(id=uuid4(), workspace_id=uuid4(), project_id=uuid4(), provider="slack",
        config={}, permission_scope=[], status="disabled", enabled=False, health_status="unknown",
        last_successful_sync_at=None, last_error="", updated_at=datetime.now(timezone.utc),
        last_checked_at=None, last_test_result="not_tested", credentials_ciphertext=None)
    response = replace_integration_credentials(row.id, IntegrationCredentialsUpdate(credentials={"token": "write-only-secret"}),
                                               SimpleNamespace(id=uuid4()), CredentialDB(row))
    assert response["has_credentials"] is True
    assert "write-only-secret" not in repr(response)
    assert "write-only-secret" not in row.credentials_ciphertext
