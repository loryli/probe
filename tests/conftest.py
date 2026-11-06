import asyncio
import os
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from interview_assistant.api.dependencies import get_database
from interview_assistant.config import Settings
from interview_assistant.main import create_app


@pytest.fixture(autouse=True)
def isolate_application_environment(monkeypatch):
    """Developer-local settings must not change offline test behavior."""
    for name in tuple(os.environ):
        if name.startswith("INTERVIEW_"):
            monkeypatch.delenv(name)


def enable_foreign_keys(connection, record):
    cursor = connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


@pytest.fixture
def bank(tmp_path):
    database_path = (tmp_path / "questions.sqlite").as_posix()
    engine = create_engine(f"sqlite:///{database_path}", poolclass=NullPool)
    event.listen(engine, "connect", enable_foreign_keys)
    migration_config = Config("alembic.ini")
    with engine.begin() as connection:
        migration_config.attributes["connection"] = connection
        command.upgrade(migration_config, "head")
    async_engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}", poolclass=NullPool)
    event.listen(async_engine.sync_engine, "connect", enable_foreign_keys)
    sessions = async_sessionmaker(async_engine, expire_on_commit=False, autoflush=False)

    @contextmanager
    def client():
        app = create_app(Settings(_env_file=None, environment="test", database_url=None))
        app.dependency_overrides[get_database] = lambda: SimpleNamespace(sessions=sessions)
        with TestClient(app) as http:
            yield http

    yield SimpleNamespace(client=client, engine=engine, sessions=sessions)
    asyncio.run(async_engine.dispose())
    engine.dispose()
