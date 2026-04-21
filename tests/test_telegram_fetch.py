import asyncio
from datetime import UTC, datetime
from pathlib import Path

from telegram_kol_research.telegram_client import fetch_dialog_messages


class _FakeMedia:
    pass


class _FakeMessage:
    def __init__(self):
        self.id = 77
        self.sender_id = 501
        self.message = "BTC long 68000-68200"
        self.reply_to_msg_id = 70
        self.date = datetime(2026, 4, 7, tzinfo=UTC)
        self.edit_date = datetime(2026, 4, 7, 1, 0, tzinfo=UTC)
        self.media = _FakeMedia()
        self.photo = True
        self.document = None

    async def get_sender(self):
        return type("Sender", (), {"first_name": "Alice", "last_name": "Trader"})()


class _FakeClient:
    async def iter_messages(self, chat_id, limit):
        yield _FakeMessage()


def test_fetch_dialog_messages_extracts_sender_name_and_edit_date():
    dialog = {"id": 9001, "title": "VIP BTC Room", "archived": True}
    payloads = asyncio.run(fetch_dialog_messages(_FakeClient(), dialog, limit=10))
    assert payloads[0]["sender_name"] == "Alice Trader"
    assert payloads[0]["edit_date"] == datetime(2026, 4, 7, 1, 0, tzinfo=UTC)
    assert payloads[0]["media"]["kind"] == "_fakemedia"


def test_fetch_dialog_messages_downloads_media_to_local_path(tmp_path):
    class DownloadingClient(_FakeClient):
        async def download_media(self, media, file):
            output_path = Path(file) / "9001-77.bin"
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"image-bytes")
            return str(output_path)

    dialog = {"id": 9001, "title": "VIP BTC Room", "archived": True}
    payloads = asyncio.run(
        fetch_dialog_messages(
            DownloadingClient(),
            dialog,
            limit=10,
            media_root=tmp_path / "downloaded-media",
        )
    )

    downloaded_path = Path(payloads[0]["media"]["path"])
    assert downloaded_path.exists()
    assert downloaded_path.is_file()
    assert downloaded_path.is_relative_to(tmp_path / "downloaded-media")


def test_fetch_dialog_messages_skips_video_download(tmp_path):
    class VideoMedia:
        pass

    class VideoMessage(_FakeMessage):
        def __init__(self):
            super().__init__()
            self.media = VideoMedia()
            self.photo = None
            self.document = type("Document", (), {"mime_type": "video/mp4"})()

    class ClientWithVideo:
        def __init__(self):
            self.download_calls = 0

        async def iter_messages(self, chat_id, limit):
            yield VideoMessage()

        async def download_media(self, media, file):
            self.download_calls += 1
            output_path = Path(file).with_suffix(".mp4")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"video")
            return str(output_path)

    client = ClientWithVideo()
    dialog = {"id": 9001, "title": "VIP BTC Room", "archived": True}

    payloads = asyncio.run(
        fetch_dialog_messages(
            client,
            dialog,
            limit=10,
            media_root=tmp_path / "downloaded-media",
        )
    )

    assert payloads[0]["media"]["path"] is None
    assert client.download_calls == 0
