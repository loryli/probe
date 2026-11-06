from fastapi.testclient import TestClient

from interview_assistant.config import Settings
from interview_assistant.main import create_app


def test_workspace_assets_and_openapi_are_available_without_database():
    settings = Settings(_env_file=None, environment="test", database_url=None)
    with TestClient(create_app(settings)) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert page.headers["content-type"].startswith("text/html")
        assert page.headers["cache-control"] == "no-cache"
        assert 'lang="zh-CN"' in page.text
        for path, content_type in [
            ("/assets/app.js", "javascript"),
            ("/assets/imports.js", "javascript"),
            ("/assets/styles.css", "text/css"),
            ("/assets/favicon.svg", "image/svg+xml"),
        ]:
            response = client.get(path)
            assert response.status_code == 200
            assert content_type in response.headers["content-type"]
        schema = client.get("/openapi.json").json()
        assert "/" not in schema["paths"]
        assert "/question-bank/questions" in schema["paths"]
        assert client.get("/assets/../config.py").status_code == 404
        assert client.get("/assets/missing.js").status_code == 404
        assert client.get("/health/ready").status_code == 503
