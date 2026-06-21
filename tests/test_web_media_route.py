from fastapi.testclient import TestClient

from telegram_kol_research.web_app import create_web_app


def test_media_route_serves_downloaded_file(tmp_path):
    media_file = tmp_path / "media" / "77.jpg"
    media_file.parent.mkdir(parents=True)
    media_file.write_bytes(b"fake-image")

    app = create_web_app(
        database_path=tmp_path / "research.db", media_root=tmp_path / "media"
    )
    client = TestClient(app)

    response = client.get("/local-media/77.jpg")

    assert response.status_code == 200
    assert response.content == b"fake-image"


def test_media_route_returns_404_for_cleaned_file(tmp_path):
    app = create_web_app(
        database_path=tmp_path / "research.db", media_root=tmp_path / "media"
    )
    client = TestClient(app)

    response = client.get("/local-media/cleaned.jpg")

    assert response.status_code == 404
