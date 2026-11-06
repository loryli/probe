import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from interview_assistant.config import Settings
from interview_assistant.database import Database, utc_now
from interview_assistant.main import create_app
from interview_assistant.question_bank.models import QuestionMetadata

ROOT = "/question-bank/questions"


def test_local_mode_bootstraps_and_preserves_data_across_restarts(tmp_path):
    data_dir = tmp_path / "个人资料" / "data"
    settings = Settings(_env_file=None, environment="local", database_url=None, data_dir=data_dir)
    with patch("aiomysql.connect", side_effect=AssertionError("Must not contact MySQL")) as mysql:
        with TestClient(create_app(settings)) as client:
            assert client.get("/health/ready").json() == {"status": "ok", "database": "ok"}
            response = client.post(
                ROOT,
                json={
                    "question_text": "原题",
                    "standard_answer_text": "```python\nprint('原文')\n```\r\n",
                },
            )
            assert response.status_code == 201
            question = response.json()
            changed = client.patch(
                f"{ROOT}/{question['id']}",
                json={"expected_revision": 1, "user_notes": "补充笔记"},
            )
            assert changed.status_code == 200
        assert (data_dir / "interview.sqlite3").is_file()
        with TestClient(create_app(settings)) as restarted:
            saved = restarted.get(f"{ROOT}/{question['id']}").json()
            assert saved["user_notes"] == "补充笔记"
            assert saved["content_version"] == 2
            versions = restarted.get(f"{ROOT}/{question['id']}/versions").json()
            assert [item["version"] for item in versions["items"]] == [2, 1]
            assert versions["items"][1]["standard_answer_text"] == question["standard_answer_text"]
        mysql.assert_not_called()


def test_default_sqlite_connections_enforce_foreign_keys(tmp_path):
    settings = Settings(_env_file=None, environment="local", database_url=None, data_dir=tmp_path)

    async def verify():
        database = Database(settings.resolved_database_url)
        try:
            await database.initialize_local()
            async with database.sessions() as session:
                assert await session.scalar(text("PRAGMA foreign_keys")) == 1
                await session.rollback()
                with pytest.raises(IntegrityError):
                    async with session.begin():
                        session.add(
                            QuestionMetadata(
                                id="invalid-metadata",
                                question_id="missing-question",
                                content_version=1,
                                metadata_json={},
                                origin="ai",
                                status="draft",
                                created_at=utc_now(),
                            )
                        )
                        await session.flush()
        finally:
            await database.close()

    asyncio.run(verify())


def test_startup_failure_disposes_local_database(tmp_path):
    settings = Settings(_env_file=None, environment="local", database_url=None, data_dir=tmp_path)
    with patch("interview_assistant.main.Database") as database_type:
        database = database_type.return_value
        database.is_sqlite = True
        database.initialize_local = AsyncMock(side_effect=RuntimeError("Initialization failed"))
        database.close = AsyncMock()
        with pytest.raises(RuntimeError, match="Initialization failed"):
            with TestClient(create_app(settings)):
                pass
        database.close.assert_awaited_once()


def test_alembic_cli_uses_local_sqlite_without_mysql_configuration(tmp_path, monkeypatch):
    config_path = Path(__file__).resolve().parents[1] / "alembic.ini"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("INTERVIEW_ENVIRONMENT", "local")
    monkeypatch.setenv("INTERVIEW_DATA_DIR", str(tmp_path))
    command.upgrade(Config(str(config_path)), "head")
    engine = create_engine(f"sqlite:///{(tmp_path / 'interview.sqlite3').as_posix()}")
    try:
        with engine.connect() as connection:
            assert {"question", "question_version", "question_metadata"}.issubset(
                inspect(connection).get_table_names()
            )
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "0002_markdown_import"
            )
    finally:
        engine.dispose()
