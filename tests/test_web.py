from fastapi.testclient import TestClient

from interview_assistant.config import Settings
from interview_assistant.main import create_app


def test_frontend_redirect_and_retired_assets_without_database():
    settings = Settings(_env_file=None, environment="test", database_url=None)
    with TestClient(create_app(settings)) as client:
        page = client.get("/", follow_redirects=False)
        assert page.status_code == 307
        assert page.headers["location"] == "/app/"
        for path in (
            "/assets/app.js", "/assets/imports.js", "/assets/styles.css",
            "/assets/favicon.svg", "/assets/design-system.html",
        ):
            assert client.get(path).status_code == 404
        schema = client.get("/openapi.json").json()
        assert "/" not in schema["paths"]
        assert "/question-bank/questions" in schema["paths"]
        assert client.get("/assets/../config.py").status_code == 404
        assert client.get("/assets/missing.js").status_code == 404
        assert client.get("/health/ready").status_code == 503
