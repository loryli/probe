from uuid import uuid4

QUESTIONS = "/question-bank/questions"
CATEGORIES = "/question-bank/categories"
ORIGINAL = {
    "question_text": "  RAG 检索\r\n如何评估？  ",
    "standard_answer_text": "原文\r\n```sql\nSELECT 1;\n```\n",
    "user_notes": "自己的笔记\n![图](source.png)",
}


def create_category(client, name="RAG"):
    result = client.post(CATEGORIES, json={"request_id": str(uuid4()), "name": name})
    assert result.status_code == 201, result.text
    return result.json()


def create_question(client):
    result = client.post(QUESTIONS, json=ORIGINAL)
    assert result.status_code == 201, result.text
    return result.json()


def test_classification_preserves_original_history_and_survives_restart(bank):
    with bank.client() as client:
        category = create_category(client)
        question = create_question(client)
        url = f"{QUESTIONS}/{question['id']}"
        result = client.patch(url, json={"expected_revision": 1, "category_id": category["id"]})
        assert result.status_code == 200, result.text
        saved = result.json()
        assert saved["revision"] == 2 and saved["content_version"] == 1
        for key, value in ORIGINAL.items():
            assert saved[key] == value
        assert client.get(f"{url}/versions").json()["total"] == 1
        assert client.get(QUESTIONS, params={"category_id": category["id"], "search": "检索"}).json()["total"] == 1
        assert client.get(QUESTIONS, params={"uncategorized": True}).json()["total"] == 0
        counts = client.get(CATEGORIES).json()
        assert counts["total_questions"] == 1
        assert counts["items"][0]["question_count"] == 1
    with bank.client() as client:
        assert client.get(url).json()["category_id"] == category["id"]
        removed = client.patch(url, json={"expected_revision": 2, "category_id": None})
        assert removed.status_code == 200
        assert removed.json()["category_id"] is None
        assert removed.json()["content_version"] == 1
        assert client.get(CATEGORIES).json()["uncategorized_count"] == 1


def test_bulk_classification_rolls_back_every_question_on_stale_revision(bank):
    with bank.client() as client:
        category = create_category(client)
        questions = sorted([create_question(client), create_question(client)], key=lambda q: q["id"])
        moves = [{"id": q["id"], "expected_revision": 1} for q in questions]
        moves[1]["expected_revision"] = 2
        failed = client.post(f"{QUESTIONS}/classify", json={"category_id": category["id"], "questions": moves})
        assert failed.status_code == 409, failed.text
        for q in questions:
            saved = client.get(f"{QUESTIONS}/{q['id']}").json()
            assert saved["category_id"] is None and saved["revision"] == 1
        moves[1]["expected_revision"] = 1
        saved = client.post(f"{QUESTIONS}/classify", json={"category_id": category["id"], "questions": moves})
        assert saved.status_code == 200, saved.text
        assert len(saved.json()) == 2
        assert all(item["category_id"] == category["id"] for item in saved.json())
        assert all(item["content_version"] == 1 for item in saved.json())
        assert client.get(CATEGORIES).json()["items"][0]["question_count"] == 2


def test_categories_replay_validate_names_and_reject_stale_renames(bank):
    with bank.client() as client:
        payload = {"request_id": str(uuid4()), "name": " Java "}
        category = client.post(CATEGORIES, json=payload).json()
        assert category["name"] == "Java"
        assert client.post(CATEGORIES, json=payload).status_code == 200
        assert client.post(CATEGORIES, json={"request_id": str(uuid4()), "name": "ＪＡＶＡ"}).status_code == 409
        assert client.post(CATEGORIES, json={"request_id": str(uuid4()), "name": "  "}).status_code == 422
        url = f"{CATEGORIES}/{category['id']}"
        renamed = client.patch(url, json={"expected_revision": 1, "name": "Java 基础"})
        assert renamed.status_code == 200
        assert client.patch(url, json={"expected_revision": 1, "name": "旧窗口的修改"}).status_code == 409
        question = create_question(client)
        missing = client.patch(f"{QUESTIONS}/{question['id']}", json={"expected_revision": 1, "category_id": str(uuid4())})
        assert missing.status_code == 404
        assert client.get(f"{QUESTIONS}/{question['id']}").json()["revision"] == 1
        assert client.get(QUESTIONS, params={"category_id": category["id"], "uncategorized": True}).status_code == 422
