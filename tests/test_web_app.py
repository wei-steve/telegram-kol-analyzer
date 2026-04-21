from fastapi.testclient import TestClient

from telegram_kol_research.web_app import create_web_app


def test_root_page_renders_successfully(tmp_path):
    app = create_web_app(database_path=tmp_path / "research.db")
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
