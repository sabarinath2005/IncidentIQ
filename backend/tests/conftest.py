import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.db import Base
import app.models  # noqa: F401
from tests.pg_test_safety import validated_test_url


@pytest.fixture
def pg_session_factory():
    """Isolated schema in an explicitly disposable PostgreSQL/pgvector test DB."""
    raw_url = os.getenv("TEST_DATABASE_URL")
    if not raw_url:
        pytest.skip("Set TEST_DATABASE_URL to a disposable PostgreSQL/pgvector database")
    try:
        url = validated_test_url(raw_url)
    except ValueError as exc:
        pytest.fail(str(exc), pytrace=False)

    admin_engine = create_engine(url)
    schema = "reliability_" + uuid4().hex[:12]
    test_engine = None
    try:
        with admin_engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        test_engine = create_engine(url, connect_args={"options": f"-csearch_path={schema},public"})
        Base.metadata.create_all(test_engine)
        yield sessionmaker(bind=test_engine, autoflush=False, expire_on_commit=False)
    finally:
        if test_engine is not None:
            test_engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin_engine.dispose()
