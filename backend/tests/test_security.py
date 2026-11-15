from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.core.security import create_token, current_user, hash_password, verify_password


class FakeUserStore:
    def __init__(self, user):
        self.user = user

    def get(self, model, user_id):
        return self.user if self.user.id == user_id else None


def test_password_hash_is_verifiable_and_not_plaintext():
    encoded = hash_password("Demo-password-2026!")
    assert encoded != "Demo-password-2026!"
    assert verify_password("Demo-password-2026!", encoded)
    assert not verify_password("wrong-password", encoded)


def test_signed_access_token_and_tampering():
    user = SimpleNamespace(id=uuid4(), active=True)
    token = create_token(str(user.id))
    assert current_user(token, FakeUserStore(user)) is user
    tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
    with pytest.raises(HTTPException) as error:
        current_user(tampered, FakeUserStore(user))
    assert error.value.status_code == 401
