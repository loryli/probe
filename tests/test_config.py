import pytest
from pydantic import ValidationError
from sqlalchemy.engine import make_url

from interview_assistant.config import Settings


def test_env_file_and_environment_precedence(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "INTERVIEW_ENVIRONMENT=production\nINTERVIEW_LOG_LEVEL=WARNING\nINTERVIEW_QWEN_API_KEY=\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("INTERVIEW_LOG_LEVEL", "DEBUG")
    monkeypatch.delenv("INTERVIEW_QWEN_API_KEY", raising=False)
    settings = Settings(_env_file=env_file, database_url=None)
    assert settings.log_level == "DEBUG"
    assert settings.environment == "production"
    assert settings.qwen_api_key is None


def test_secrets_are_not_in_settings_repr_or_validation_errors():
    secret = "private-value-that-must-not-appear"
    settings = Settings(
        _env_file=None,
        database_url=f"mysql+aiomysql://user:{secret}@localhost/interview",
        qwen_api_key=secret,
    )
    assert secret not in repr(settings)
    assert secret not in settings.model_dump_json()
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, database_url=f"mysql://user:{secret}@localhost/interview")
    assert secret not in str(error.value)
    with pytest.raises(ValidationError) as malformed_error:
        Settings(_env_file=None, database_url=f"mysql+aiomysql://localhost:{secret}/interview")
    assert secret not in str(malformed_error.value)


@pytest.mark.parametrize("url", ["not-a-url", "sqlite:///file.db", "mysql+aiomysql://localhost"])
def test_database_configuration_rejects_wrong_driver_or_missing_database(url):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url=url)


@pytest.mark.parametrize("timeout", [0, -1])
def test_probe_timeout_must_be_positive(timeout):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_probe_timeout_seconds=timeout)


def test_local_default_is_a_project_file_and_explicit_mysql_takes_precedence(tmp_path):
    settings = Settings(_env_file=None, database_url=None, data_dir=tmp_path)
    url = make_url(settings.resolved_database_url)
    assert url.drivername == "sqlite+aiosqlite"
    assert url.database == (tmp_path / "interview.sqlite3").as_posix()
    mysql = "mysql+aiomysql://user:password@localhost/interview"
    configured = Settings(_env_file=None, database_url=mysql, data_dir=tmp_path)
    assert configured.resolved_database_url == mysql


@pytest.mark.parametrize("environment", ["production", "test"])
def test_nonlocal_environment_has_no_implicit_database(environment):
    settings = Settings(_env_file=None, environment=environment, database_url=None)
    assert settings.resolved_database_url is None
