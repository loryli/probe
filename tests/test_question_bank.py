import asyncio
import io
from argparse import Namespace
from unittest.mock import patch
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import SQLAlchemyError

from interview_assistant.database import utc_now
from interview_assistant.question_bank.models import Question, QuestionMetadata, QuestionVersion
from interview_assistant.question_bank.schemas import QuestionPatch
from interview_assistant.question_bank.service import QuestionService, RevisionConflict

ROOT = "/question-bank/questions"
ORIGINAL = {
    "question_text": "  什么是事务？\r\n请解释原子性。  ",
    "standard_answer_text": (
        "\r\n【标准回答】\n```python\nprint('事务 ✅')\n```\n\n![图](images/acID.png)  "
    ),
    "user_notes": "不要修改我的原文\n> 补充笔记\n",
}


def create_question(client, **overrides):
    response = client.post(ROOT, json={**ORIGINAL, **overrides})
    assert response.status_code == 201, response.text
    return response.json()


def test_original_markdown_and_history_survive_app_restart(bank):
    with bank.client() as client:
        question = create_question(client)
        question_id = question["id"]
        assert question["content_version"] == question["revision"] == 1
        for field, value in ORIGINAL.items():
            assert question[field] == value
        assert question["created_at"].endswith("Z")
        assert (
            client.get(f"{ROOT}/{question_id}/versions/1").json()["standard_answer_text"]
            == ORIGINAL["standard_answer_text"]
        )
    with bank.client() as restarted:
        saved = restarted.get(f"{ROOT}/{question_id}")
        assert saved.status_code == 200
        for field, value in ORIGINAL.items():
            assert saved.json()[field] == value


def test_content_edits_append_versions_and_stale_edits_are_rejected(bank):
    with bank.client() as client:
        question = create_question(client)
        url = f"{ROOT}/{question['id']}"
        changed = client.patch(
            url, json={"expected_revision": 1, "standard_answer_text": "新答案\n"}
        )
        assert changed.status_code == 200
        assert changed.json()["revision"] == changed.json()["content_version"] == 2
        assert (
            client.patch(url, json={"expected_revision": 1, "user_notes": "旧窗口修改"}).status_code
            == 409
        )
        history = client.get(url + "/versions").json()
        assert history["total"] == 2
        assert [item["version"] for item in history["items"]] == [2, 1]
        assert history["items"][1]["standard_answer_text"] == ORIGINAL["standard_answer_text"]
        assert client.get(url).json()["user_notes"] == ORIGINAL["user_notes"]
        assert client.get(url + "/versions/999").status_code == 404


def test_enable_disable_and_no_op_do_not_create_content_versions(bank):
    with bank.client() as client:
        question = create_question(client)
        url = f"{ROOT}/{question['id']}"
        same = client.patch(url, json={"expected_revision": 1, **ORIGINAL})
        assert same.json()["revision"] == same.json()["content_version"] == 1
        disabled = client.patch(url, json={"expected_revision": 1, "is_active": False})
        assert disabled.json()["revision"] == 2
        assert disabled.json()["content_version"] == 1
        assert client.get(ROOT, params={"is_active": True}).json()["total"] == 0
        assert client.get(ROOT, params={"is_active": False}).json()["total"] == 1
        enabled = client.patch(url, json={"expected_revision": 2, "is_active": True})
        assert enabled.json()["revision"] == 3
        assert enabled.json()["content_version"] == 1
        assert client.get(url + "/versions").json()["total"] == 1


def test_list_pagination_literal_search_and_deleted_filter(bank):
    with bank.client() as client:
        first = create_question(client, question_text="包含 100%_ 字符")
        create_question(client, question_text="另一道题", is_active=False)
        create_question(client, question_text="包含 100xx 字符")
        page = client.get(ROOT, params={"limit": 1, "offset": 1}).json()
        assert page["total"] == 3 and len(page["items"]) == 1
        literal = client.get(ROOT, params={"search": "%_"}).json()
        assert literal["total"] == 1
        assert literal["items"][0]["id"] == first["id"]
        url = f"{ROOT}/{first['id']}"
        assert client.delete(url, params={"expected_revision": 1}).status_code == 204
        assert client.get(ROOT).json()["total"] == 2
        assert client.get(url).status_code == 404
        assert client.get(url + "/versions").status_code == 404
        assert (
            client.patch(url, json={"expected_revision": 2, "is_active": True}).status_code == 404
        )
        assert client.delete(url, params={"expected_revision": 2}).status_code == 404
    with bank.engine.connect() as connection:
        assert (
            connection.scalar(
                select(func.count())
                .select_from(QuestionVersion)
                .where(QuestionVersion.question_id == first["id"])
            )
            == 1
        )


def test_stale_delete_cannot_remove_a_newer_edit(bank):
    with bank.client() as client:
        question = create_question(client)
        url = f"{ROOT}/{question['id']}"
        client.patch(url, json={"expected_revision": 1, "user_notes": "已修改"})
        assert client.delete(url, params={"expected_revision": 1}).status_code == 409
        assert client.get(url).json()["user_notes"] == "已修改"


def test_creation_key_deduplicates_retries_and_rejects_reuse(bank):
    headers = {"Idempotency-Key": "create-001"}
    with bank.client() as client:
        first = client.post(ROOT, json=ORIGINAL, headers=headers)
        retried = client.post(ROOT, json=ORIGINAL, headers=headers)
        assert first.status_code == 201 and retried.status_code == 200
        assert first.json()["id"] == retried.json()["id"]
        assert client.get(ROOT).json()["total"] == 1
        assert (
            client.post(ROOT, json={"question_text": "其他问题"}, headers=headers).status_code
            == 409
        )
        url = f"{ROOT}/{first.json()['id']}"
        client.patch(url, json={"expected_revision": 1, "user_notes": "现在的备注"})
        assert (
            client.post(ROOT, json=ORIGINAL, headers=headers).json()["user_notes"] == "现在的备注"
        )
        assert client.delete(url, params={"expected_revision": 2}).status_code == 204
        assert client.post(ROOT, json=ORIGINAL, headers=headers).status_code == 409


def test_metadata_writes_and_content_versions_remain_separate(bank):
    with bank.client() as client:
        question = create_question(client)
        url = f"{ROOT}/{question['id']}"
        with bank.engine.begin() as connection:
            connection.execute(
                QuestionMetadata.__table__.insert().values(
                    id=str(uuid4()),
                    question_id=question["id"],
                    content_version=1,
                    metadata_json={"tags": ["事务"], "difficulty": "medium"},
                    origin="ai",
                    status="draft",
                    created_at=utc_now(),
                )
            )
        unchanged = client.get(url).json()
        for field, value in ORIGINAL.items():
            assert unchanged[field] == value
        client.patch(url, json={"expected_revision": 1, "standard_answer_text": "新版本"})
    with bank.engine.connect() as connection:
        metadata = connection.execute(select(QuestionMetadata.__table__)).mappings().one()
        assert metadata["content_version"] == 1
        assert metadata["metadata_json"]["tags"] == ["事务"]


def test_failed_snapshot_rolls_back_the_entire_edit(bank, capsys):
    with bank.client() as client:
        question = create_question(client)
        url = f"{ROOT}/{question['id']}"
        with patch.object(
            QuestionService, "_snapshot", side_effect=SQLAlchemyError("private-answer")
        ):
            failed = client.patch(
                url, json={"expected_revision": 1, "standard_answer_text": "不应保存"}
            )
        assert failed.status_code == 503
        assert "private-answer" not in failed.text
        saved = client.get(url).json()
        assert saved["revision"] == saved["content_version"] == 1
        assert saved["standard_answer_text"] == ORIGINAL["standard_answer_text"]
        assert client.get(url + "/versions").json()["total"] == 1
    assert "private-answer" not in capsys.readouterr().out


def test_concurrent_writer_cannot_overwrite_a_stale_identity_map(bank):
    with bank.client() as client:
        question = create_question(client)

    async def concurrent_edits():
        async with bank.sessions() as first, bank.sessions() as second:
            stale = await first.get(Question, question["id"])
            await first.commit()
            await QuestionService(second).patch(
                question["id"], QuestionPatch(expected_revision=1, user_notes="先提交的内容")
            )
            assert stale.revision == 1
            with pytest.raises(RevisionConflict):
                await QuestionService(first).patch(
                    question["id"], QuestionPatch(expected_revision=1, user_notes="过期内容")
                )

    asyncio.run(concurrent_edits())
    with bank.client() as client:
        saved = client.get(f"{ROOT}/{question['id']}").json()
        assert saved["user_notes"] == "先提交的内容"
        assert saved["content_version"] == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"question_text": " \n "},
        {"question_text": None},
        {"standard_answer_text": None},
        {"user_notes": None},
        {"is_active": None},
        {},
        {"category": "不能混入原文接口"},
    ],
)
def test_patch_rejects_ambiguous_or_unknown_fields(bank, changes):
    with bank.client() as client:
        question = create_question(client)
        result = client.patch(f"{ROOT}/{question['id']}", json={"expected_revision": 1, **changes})
        assert result.status_code == 422
        assert client.get(f"{ROOT}/{question['id']}").json()["revision"] == 1


def test_empty_answer_and_notes_can_be_saved_intentionally(bank):
    with bank.client() as client:
        question = create_question(client)
        response = client.patch(
            f"{ROOT}/{question['id']}",
            json={"expected_revision": 1, "standard_answer_text": "", "user_notes": ""},
        )
        assert response.status_code == 200
        assert response.json()["standard_answer_text"] == response.json()["user_notes"] == ""


def test_input_validation_and_missing_question(bank):
    with bank.client() as client:
        assert client.post(ROOT, json={"question_text": " "}).status_code == 422
        assert (
            client.post(ROOT, json={"question_text": "问题", "metadata_json": {}}).status_code
            == 422
        )
        assert client.get(ROOT, params={"limit": 101}).status_code == 422
        assert client.get(ROOT, params={"offset": -1}).status_code == 422
        assert client.get(ROOT + "/not-a-uuid").status_code == 422
        assert client.get(f"{ROOT}/{uuid4()}").status_code == 404


def test_migration_round_trip_and_model_consistency(bank):
    with bank.engine.begin() as connection:
        config = Config("alembic.ini")
        config.attributes["connection"] = connection
        command.check(config)
        command.downgrade(config, "base")
        assert set(inspect(connection).get_table_names()) == {"alembic_version"}
        command.upgrade(config, "head")
        assert set(inspect(connection).get_table_names()) == {
            "alembic_version",
            "question",
            "question_version",
            "question_metadata",
            "markdown_import",
            "question_import_source",
        }


def test_offline_migration_generates_mysql_sql_without_credentials():
    output = io.StringIO()
    config = Config("alembic.ini", output_buffer=output)
    config.cmd_opts = Namespace(x=["dialect=mysql"])
    command.upgrade(config, "head", sql=True)
    sql = output.getvalue()
    assert "CREATE TABLE question" in sql
    assert "CREATE TABLE question_version" in sql
    assert "CREATE TABLE question_metadata" in sql
    assert "CREATE TABLE markdown_import" in sql
    assert "CREATE TABLE question_import_source" in sql
    assert "LONGTEXT" in sql and "DATETIME(6)" in sql
    assert "utf8mb4" in sql and "utf8mb4_bin" in sql
