"""Authenticated encryption for integration credentials using a stable Fernet key."""
import json
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from app.core.config import settings


def _fernet() -> Fernet:
    if not settings.integration_encryption_key:
        raise HTTPException(503, "Integration credential encryption is not configured")
    try:
        return Fernet(settings.integration_encryption_key.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        raise HTTPException(503, "Integration credential encryption key is invalid")


def encrypt_credentials(credentials: dict) -> str:
    token = _fernet().encrypt(json.dumps(credentials, separators=(",", ":")).encode())
    return token.decode("ascii")


def decrypt_credentials(ciphertext: str, *, provider: str, connector_provider: str) -> dict:
    if not connector_provider or connector_provider != provider:
        raise PermissionError("Only the matching registered connector may access credentials")
    try:
        return json.loads(_fernet().decrypt(ciphertext.encode("ascii")))
    except InvalidToken:
        raise ValueError("Credential ciphertext could not be authenticated")


def masked_credentials_present(ciphertext: str | None) -> str | None:
    # Fixed-width masking avoids exposing a secret's length or content.
    return "********" if ciphertext else None
