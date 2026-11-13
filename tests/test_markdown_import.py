import asyncio
import hashlib
from unittest.mock import patch
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.exc import SQLAlchemyError

from interview_assistant.database import utc_now
from interview_assistant.question_bank.imports import ImportConfirm, ImportService
from interview_assistant.question_bank.markdown import parse_markdown
from interview_assistant.question_bank.models import MarkdownImport, Question, QuestionImportSource
from interview_assistant.question_bank.service import QuestionService

ROOT = "/question-bank/imports"
DOCUMENT = (
    "\ufeff# 我的笔记\r\n\r\n## 数据库\r\n\r\n"
    "### 1\\. 什么是事务？\r\n\r\n【标准回答话术】 原子性。  \r\n"
    "```md\r\n### 代码里的标题不是题目\r\nQ: 也不是题目\r\n```\r\n"
    "![图](./事务.png)\r\n\r\n补充：追问要放在同一段里。\r\n\r\n"
    "### 锁\r\n面试题：乐观锁是什么？ 【标准回答】 通过版本检查。\r\n"
    "## 另一分类\r\n这段分类说明不应属于上一题。\r\n"
    "### 未完成的题目？\r\n"
)


def preview(client, content=DOCUMENT, **extra):
    response = client.post(ROOT, json={"file_name": "个人问答.md", "content": content, **extra})
    assert response.status_code in (200, 201), response.text
    return response.json()


def test_heading_parser_preserves_source_fences_categories_and_explicit_question():
    parsed = parse_markdown(DOCUMENT)
    assert len(parsed.candidates) == 3
    first, second, third = parsed.candidates
    assert first.question_text == "1\\. 什么是事务？"
    assert first.heading_path == ["我的笔记", "数据库"]
    assert first.standard_answer_text.startswith("【标准回答话术】 原子性。  \r\n")
    assert "### 代码里的标题不是题目\r\nQ: 也不是题目" in first.standard_answer_text
    assert first.standard_answer_text.endswith("补充：追问要放在同一段里。\r\n\r\n")
    assert any("图片" in warning for warning in first.warnings)
    assert second.question_text == "乐观锁是什么？ "
    assert second.standard_answer_text == "【标准回答】 通过版本检查。\r\n"
    assert third.standard_answer_text == "" and not third.recommended
    assert DOCUMENT[first.start_offset : first.end_offset].startswith("### 1\\.")
    assert DOCUMENT.splitlines()[first.start_line - 1].startswith("### 1\\.")


@pytest.mark.parametrize("prefix", ["", "\ufeff"])
def test_qa_parser_preserves_bom_offsets_and_multiline_answers(prefix):
    document = prefix + "Q: 问题一？\r\nA: 第一行\r\n第二行\r\nQ：问题二？\r\nA：答案二\r\n"
    parsed = parse_markdown(document)
    assert parsed.strategy == "qa"
    assert [c.question_text for c in parsed.candidates] == ["问题一？", "问题二？"]
    assert parsed.candidates[0].standard_answer_text == "A: 第一行\r\n第二行\r\n"


def test_ambiguous_duplicate_and_unclosed_fence_are_visible_not_silently_rewritten():
    document = (
        "### 普通说明\n没有答案标记\n### 相同？\n【标准回答】 是\n### 相同？\n【标准回答】 是\n"
    )
    result = parse_markdown(document)
    assert [c.recommended for c in result.candidates] == [False, True, False]
    assert "完全相同" in result.candidates[2].warnings[0]
    incomplete = parse_markdown("### 问题？\n```python\n### 隐藏\n")
    assert len(incomplete.candidates) == 1
    assert any("未闭合" in warning for warning in incomplete.warnings)
    assert parse_markdown("# 只有前言").candidates == []
    assert parse_markdown("## 问题？\n答案\n", "heading_2").candidates[0].question_text == "问题？"


def test_preview_does_not_create_questions_and_survives_restart(bank):
    with bank.client() as client:
        draft = preview(client)
        assert draft["status"] == "draft"
        assert client.get("/question-bank/questions").json()["total"] == 0
        assert client.get(f"{ROOT}/{draft['id']}/document").content == DOCUMENT.encode("utf-8")
        assert draft["content_sha256"] == hashlib.sha256(DOCUMENT.encode("utf-8")).hexdigest()
        duplicate = preview(client)
        assert duplicate["id"] == draft["id"]
    with bank.client() as client:
        assert client.get(f"{ROOT}/{draft['id']}").json() == draft


def test_confirmation_is_atomic_selective_and_replay_safe_with_immutable_source(bank):
    with bank.client() as client:
        draft = preview(client)
        url = f"{ROOT}/{draft['id']}/confirm"
        response = client.post(url, json={"selected_indices": [1, 0]})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["imported_count"] == 2 and not result["replayed"]
        question_id = result["question_ids"][0]
        question = client.get(f"/question-bank/questions/{question_id}").json()
        assert question["source"] == "markdown" and question["content_version"] == 1
        assert question["question_text"] == draft["candidates"][0]["question_text"]
        assert question["standard_answer_text"] == draft["candidates"][0]["standard_answer_text"]
        source = client.get(f"/question-bank/questions/{question_id}/source").json()
        assert source["candidate"] == draft["candidates"][0]
        assert (
            client.patch(
                f"/question-bank/questions/{question_id}",
                json={"expected_revision": 1, "question_text": "我修订后的题干"},
            ).status_code
            == 200
        )
        assert client.get(f"/question-bank/questions/{question_id}/source").json() == source
        replay = client.post(url, json={"selected_indices": [0, 1]}).json()
        assert replay["replayed"] and replay["question_ids"] == result["question_ids"]
        assert client.post(url, json={"selected_indices": [0]}).status_code == 409
        assert client.get("/question-bank/questions").json()["total"] == 2
        assert preview(client)["status"] == "confirmed"
    with bank.client() as client:
        assert client.get(f"/question-bank/questions/{question_id}/source").json() == source


def test_confirm_failure_rolls_back_batch_questions_versions_and_sources(bank, capsys):
    with bank.client() as client:
        draft = preview(client)
        with patch.object(
            QuestionService, "_snapshot", side_effect=SQLAlchemyError("private-upload")
        ):
            failed = client.post(f"{ROOT}/{draft['id']}/confirm", json={"selected_indices": [0, 1]})
        assert failed.status_code == 503 and "private-upload" not in failed.text
        assert client.get(f"{ROOT}/{draft['id']}").json()["status"] == "draft"
        assert client.get("/question-bank/questions").json()["total"] == 0
        assert (
            client.post(f"{ROOT}/{draft['id']}/confirm", json={"selected_indices": [0]}).status_code
            == 200
        )
    with bank.engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(Question)) == 1
        assert connection.scalar(select(func.count()).select_from(QuestionImportSource)) == 1
        assert connection.scalar(select(func.count()).select_from(MarkdownImport)) == 1
    assert "private-upload" not in capsys.readouterr().out


@pytest.mark.parametrize("indices", [[], [0, 0], [-1], [999]])
def test_invalid_selection_never_writes_questions(bank, indices):
    with bank.client() as client:
        draft = preview(client)
        assert (
            client.post(
                f"{ROOT}/{draft['id']}/confirm", json={"selected_indices": indices}
            ).status_code
            == 422
        )
        assert client.get("/question-bank/questions").json()["total"] == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"file_name": "../题库.md"},
        {"file_name": "笔记.pdf"},
        {"content": "a\x00b"},
        {"content": "中" * 350000},
        {"strategy": "model"},
    ],
)
def test_invalid_uploads_are_rejected(bank, overrides):
    with bank.client() as client:
        response = client.post(
            ROOT, json={"file_name": "笔记.md", "content": DOCUMENT, **overrides}
        )
        assert response.status_code == 422


def test_candidate_limit_unknown_batch_and_missing_source(bank):
    with bank.client() as client:
        oversized = "### 题目？\n答案\n" * 501
        assert (
            client.post(ROOT, json={"file_name": "large.md", "content": oversized}).status_code
            == 422
        )
        assert client.get(f"{ROOT}/{uuid4()}").status_code == 404
        manual = client.post("/question-bank/questions", json={"question_text": "手工题"}).json()
        assert client.get(f"/question-bank/questions/{manual['id']}/source").status_code == 404


def test_concurrent_confirmation_creates_one_set_of_questions(bank):
    with bank.client() as client:
        draft = preview(client)

    async def confirm():
        async with bank.sessions() as session:
            return await ImportService(session).confirm(
                draft["id"], ImportConfirm(selected_indices=[0, 1])
            )

    async def race():
        return await asyncio.gather(confirm(), confirm())

    first, second = asyncio.run(race())
    assert first.question_ids == second.question_ids
    assert {first.replayed, second.replayed} == {False, True}
    with bank.client() as client:
        assert client.get("/question-bank/questions").json()["total"] == 2


def test_reupload_reuses_draft_and_reimports_after_imported_questions_are_deleted(bank):
    with bank.client() as client:
        draft = preview(client)
        renamed = client.post(ROOT, json={"file_name": "renamed.md", "content": DOCUMENT}).json()
        assert renamed["id"] == draft["id"] and renamed["file_name"] == draft["file_name"]
        url = f"{ROOT}/{draft['id']}/confirm"
        question_id = client.post(url, json={"selected_indices": [0]}).json()["question_ids"][0]
        assert (
            client.delete(f"/question-bank/questions/{question_id}?expected_revision=1").status_code
            == 204
        )
        replay = client.post(url, json={"selected_indices": [0]}).json()
        assert replay["replayed"] and replay["question_ids"] == [question_id]
        assert client.get("/question-bank/questions").json()["total"] == 0
        assert client.get(f"/question-bank/questions/{question_id}/source").status_code == 404

        again = preview(client)
        assert again["id"] != draft["id"] and again["status"] == "draft"
        assert again["selected_indices"] == []
        reimported = client.post(
            f"{ROOT}/{again['id']}/confirm", json={"selected_indices": [0]}
        ).json()
        assert not reimported["replayed"] and reimported["question_ids"] != [question_id]
        assert client.get("/question-bank/questions").json()["total"] == 1
        assert client.get(f"{ROOT}/{draft['id']}").json()["status"] == "confirmed"


def test_code_and_blank_answer_whitespace_stay_in_source_without_becoming_questions():
    document = "## 分类\nQ: 真实问题？\nA: 答案\n    Q: 缩进代码\n\tQ: 也是代码\n"
    assert len(parse_markdown(document).candidates) == 1
    blank = parse_markdown("### 空答案？\r\n\r\n").candidates[0]
    assert blank.standard_answer_text == "" and not blank.recommended
    categories = parse_markdown("# 全书\n## 分类一\nQ: 甲？\nA: 甲\n## 分类二\nQ: 乙？\nA: 乙\n")
    assert categories.candidates[1].heading_path == ["全书", "分类二"]


def test_upgrade_keeps_existing_questions_and_versions(tmp_path):
    from interview_assistant.question_bank.models import QuestionVersion

    engine = create_engine(f"sqlite:///{(tmp_path / 'upgrade.sqlite').as_posix()}")
    try:
        with engine.begin() as connection:
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "0001_question_bank")
            identifier = str(uuid4())
            now = utc_now()
            connection.execute(
                Question.__table__.insert(),
                {
                    "id": identifier,
                    "question_text": "旧题目",
                    "standard_answer_text": "旧答案\r\n",
                    "user_notes": "备注",
                    "created_at": now,
                    "updated_at": now,
                },
            )
            connection.execute(
                QuestionVersion.__table__.insert(),
                {
                    "question_id": identifier,
                    "version": 1,
                    "question_text": "旧题目",
                    "standard_answer_text": "旧答案\r\n",
                    "user_notes": "备注",
                    "source": "manual",
                    "created_at": now,
                },
            )
            command.upgrade(config, "head")
            command.check(config)
            assert {"markdown_import", "question_import_source"}.issubset(
                inspect(connection).get_table_names()
            )
            assert (
                connection.scalar(
                    select(Question.standard_answer_text).where(Question.id == identifier)
                )
                == "旧答案\r\n"
            )
            assert connection.scalar(select(QuestionVersion.standard_answer_text)) == "旧答案\r\n"
    finally:
        engine.dispose()
