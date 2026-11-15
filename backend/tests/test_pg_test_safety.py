import pytest

from tests.pg_test_safety import validated_test_url


def test_test_database_url_accepts_only_explicit_disposable_database():
    parsed = validated_test_url("postgresql+psycopg://user:secret@localhost:5432/incident_platform_test")
    assert parsed.database == "incident_platform_test"
    assert parsed.drivername == "postgresql+psycopg"


@pytest.mark.parametrize("value", [
    "postgresql+psycopg://user:secret@localhost:5432/incident_platform",
    "postgresql+psycopg://user:secret@localhost:5432/other_test",
    "postgresql://user:secret@localhost:5432/incident_platform_test",
    "not-a-url",
])
def test_test_database_url_rejects_nonapproved_target_or_driver(value):
    with pytest.raises(ValueError):
        validated_test_url(value)


def test_missing_test_url_never_falls_back_to_application_database():
    assert validated_test_url(None) is None
