"""Guardrails shared by PostgreSQL integration tests."""

from sqlalchemy.engine import URL, make_url


ALLOWED_TEST_DATABASE = "incident_platform_test"
ALLOWED_TEST_DRIVER = "postgresql+psycopg"


def validated_test_url(value: str | None) -> URL | None:
    """Return a parsed URL only for the explicitly authorized disposable DB.

    There is deliberately no fallback to DATABASE_URL or application settings.
    """
    if not value:
        return None
    try:
        url = make_url(value)
    except Exception as exc:
        raise ValueError("TEST_DATABASE_URL is not a valid SQLAlchemy database URL") from exc
    if url.drivername != ALLOWED_TEST_DRIVER:
        raise ValueError(f"TEST_DATABASE_URL must use the installed {ALLOWED_TEST_DRIVER} driver")
    if url.database != ALLOWED_TEST_DATABASE:
        raise ValueError(f"TEST_DATABASE_URL must target exactly {ALLOWED_TEST_DATABASE}")
    if not url.host:
        raise ValueError("TEST_DATABASE_URL must specify a PostgreSQL host")
    return url
