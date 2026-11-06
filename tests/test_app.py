import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from interview_assistant.api.dependencies import get_database
from interview_assistant.config import Settings
from interview_assistant.main import create_app


def make_settings(**overrides):
    return Settings(_env_file=None, environment="test", database_url=None, **overrides)


def test_api_starts_without_database_or_model_credentials():
    with TestClient(create_app(make_settings())) as client:
        assert client.get("/health/live").json() == {"status": "ok"}
        ready = client.get("/health/ready")
        assert ready.status_code == 503
        assert ready.json() == {"status": "not_ready", "database": "not_configured"}
        assert client.get("/openapi.json").status_code == 200


def test_question_bank_requires_database_configuration():
    with TestClient(create_app(make_settings())) as client:
        response = client.post(
            "/question-bank/questions", json={"question_text": "事务的原子性是什么？"}
        )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "database_not_configured"


def test_readiness_checks_database_without_requiring_a_model():
    app = create_app(make_settings())
    database = AsyncMock()
    app.dependency_overrides[get_database] = lambda: database
    with TestClient(app) as client:
        response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}
    database.ping.assert_awaited_once()


@pytest.mark.parametrize(
    "error",
    [
        OperationalError("SELECT 1", {}, Exception("secret-database-detail")),
        OSError("secret-connection-detail"),
    ],
)
def test_database_failure_does_not_expose_driver_details(error, capsys):
    app = create_app(make_settings())
    database = AsyncMock()
    database.ping.side_effect = error
    app.dependency_overrides[get_database] = lambda: database
    with TestClient(app) as client:
        response = client.get("/health/ready")
        assert client.get("/health/live").status_code == 200
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "database": "unavailable"}
    assert "secret-" not in response.text
    logs = capsys.readouterr().out
    assert type(error).__name__ in logs
    assert "secret-" not in logs


def test_slow_database_probe_times_out():
    app = create_app(make_settings(database_probe_timeout_seconds=0.01))

    async def slow_ping():
        await asyncio.sleep(1)

    database = AsyncMock()
    database.ping.side_effect = slow_ping
    app.dependency_overrides[get_database] = lambda: database
    with TestClient(app) as client:
        response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["database"] == "unavailable"


def test_database_is_scoped_to_lifespan_and_disposed_on_shutdown():
    settings = Settings(
        _env_file=None,
        environment="test",
        database_url="mysql+aiomysql://user:password@localhost/interview",
    )
    with patch("interview_assistant.main.Database") as database_type:
        database = database_type.return_value
        database.close = AsyncMock()
        app = create_app(settings)
        database_type.assert_not_called()
        with TestClient(app) as client:
            assert client.get("/health/live").status_code == 200
            database.ping.assert_not_called()
            assert app.state.database is database
        database.close.assert_awaited_once()
        assert app.state.database is None


def test_app_instances_do_not_share_settings():
    first = create_app(make_settings(database_probe_timeout_seconds=1))
    second = create_app(make_settings(database_probe_timeout_seconds=2))
    assert first.state.settings.database_probe_timeout_seconds == 1
    assert second.state.settings.database_probe_timeout_seconds == 2


@pytest.mark.parametrize("environment", ["local", "test", "production"])
def test_configured_database_is_not_contacted_during_startup_or_liveness(environment):
    settings = Settings(
        _env_file=None,
        environment=environment,
        database_url="mysql+aiomysql://user:password@localhost/interview",
    )
    with patch(
        "aiomysql.connect", side_effect=AssertionError("Unexpected database connection")
    ) as connect:
        with TestClient(create_app(settings)) as client:
            assert client.get("/health/live").status_code == 200
        connect.assert_not_called()
