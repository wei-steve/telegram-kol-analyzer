import asyncio
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image

from telegram_kol_research.telegram_client import (
    _download_media_if_present,
    discover_dialogs,
    fetch_dialog_messages,
    is_usable_image_file,
)


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


def test_discover_dialogs_can_bound_reconcile_scan_to_archived_folder():
    class DialogClient:
        def __init__(self):
            self.calls = []

        async def iter_dialogs(self, **kwargs):
            self.calls.append(kwargs)
            yield type(
                "Dialog",
                (),
                {
                    "id": 9001,
                    "title": "VIP BTC Room",
                    "archived": True,
                    "is_group": True,
                    "is_channel": False,
                },
            )()

    client = DialogClient()

    dialogs = asyncio.run(discover_dialogs(client, archived_only=True))

    assert client.calls == [{"archived": True}]
    assert dialogs == [
        {
            "id": 9001,
            "title": "VIP BTC Room",
            "archived": True,
            "is_group": True,
            "is_channel": False,
        }
    ]


def _write_jpeg(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1, 1)).save(path, format="JPEG")


def test_is_usable_image_file_rejects_empty_and_corrupt_files(tmp_path):
    empty = tmp_path / "empty.jpg"
    empty.write_bytes(b"")
    corrupt = tmp_path / "corrupt.jpg"
    corrupt.write_bytes(b"not-an-image")

    assert not is_usable_image_file(empty)
    assert not is_usable_image_file(corrupt)


def test_fetch_dialog_messages_extracts_sender_name_and_edit_date():
    dialog = {"id": 9001, "title": "VIP BTC Room", "archived": True}
    payloads = asyncio.run(fetch_dialog_messages(_FakeClient(), dialog, limit=10))
    assert payloads[0]["sender_name"] == "Alice Trader"
    assert payloads[0]["edit_date"] == datetime(2026, 4, 7, 1, 0, tzinfo=UTC)
    assert payloads[0]["media"]["kind"] == "_fakemedia"


def test_fetch_dialog_messages_downloads_media_to_local_path(tmp_path):
    class DownloadingClient(_FakeClient):
        async def download_media(self, media, file):
            output_path = Path(file).with_suffix(".jpg")
            _write_jpeg(output_path)
            return str(output_path)

    dialog = {"id": 9001, "title": "VIP BTC Room", "archived": True}
    payloads = asyncio.run(
        fetch_dialog_messages(
            DownloadingClient(),
            dialog,
            limit=10,
            media_root=tmp_path / "downloaded-media",
            media_download_timeout_seconds=3,
        )
    )

    media_root = tmp_path / "downloaded-media"
    assert payloads[0]["media"]["path"] == "9001/77.jpg"
    downloaded_path = media_root / payloads[0]["media"]["path"]
    assert downloaded_path.exists()
    assert downloaded_path.is_file()
    assert downloaded_path.is_relative_to(media_root)


def test_fetch_dialog_messages_does_not_persist_invalid_download_output(tmp_path):
    class DownloadingClient(_FakeClient):
        async def download_media(self, media, file):
            output_path = Path(file).with_suffix(".jpg")
            output_path.write_bytes(b"not-an-image")
            return str(output_path)

    payloads = asyncio.run(
        fetch_dialog_messages(
            DownloadingClient(),
            {"id": 9001, "title": "VIP BTC Room", "archived": True},
            limit=10,
            media_root=tmp_path / "downloaded-media",
        )
    )

    assert payloads[0]["media"]["path"] is None
    assert not list((tmp_path / "downloaded-media" / "9001").glob(".download-77*"))


def test_fetch_dialog_messages_rejects_download_output_outside_media_directory(tmp_path):
    escaped = tmp_path / "escaped.jpg"
    _write_jpeg(escaped)

    class DownloadingClient(_FakeClient):
        async def download_media(self, media, file):
            return str(escaped)

    payloads = asyncio.run(
        fetch_dialog_messages(
            DownloadingClient(),
            {"id": 9001, "title": "VIP BTC Room", "archived": True},
            limit=10,
            media_root=tmp_path / "downloaded-media",
        )
    )

    assert payloads[0]["media"]["path"] is None
    assert escaped.exists()


def test_concurrent_media_downloads_use_distinct_temporary_paths(tmp_path):
    class DownloadingClient:
        def __init__(self):
            self.targets = []

        async def download_media(self, media, file):
            self.targets.append(file)
            output_path = Path(file).with_suffix(".jpg")
            _write_jpeg(output_path)
            await asyncio.sleep(0)
            return str(output_path)

    client = DownloadingClient()
    async def download_both():
        return await asyncio.gather(
            _download_media_if_present(
                client, dialog_id=9001, message=_FakeMessage(), media_root=tmp_path
            ),
            _download_media_if_present(
                client, dialog_id=9001, message=_FakeMessage(), media_root=tmp_path
            ),
        )

    first, second = asyncio.run(download_both())

    assert first == second == "9001/77.jpg"
    assert len(set(client.targets)) == 2


def test_fetch_dialog_messages_skips_media_download_below_min_message_id(tmp_path):
    class DownloadingClient(_FakeClient):
        def __init__(self):
            self.download_calls = 0

        async def download_media(self, media, file):
            self.download_calls += 1
            output_path = Path(file).with_suffix(".jpg")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"image")
            return str(output_path)

    client = DownloadingClient()
    dialog = {"id": 9001, "title": "VIP BTC Room", "archived": True}

    payloads = asyncio.run(
        fetch_dialog_messages(
            client,
            dialog,
            limit=10,
            media_root=tmp_path / "downloaded-media",
            media_download_min_message_id=77,
        )
    )

    assert payloads[0]["media"]["path"] is None
    assert client.download_calls == 0


def test_fetch_dialog_messages_reuses_existing_media_file_without_redownload(tmp_path):
    media_root = tmp_path / "downloaded-media"
    existing = media_root / "9001" / "77.jpg"
    existing.parent.mkdir(parents=True)
    _write_jpeg(existing)

    class DownloadingClient(_FakeClient):
        def __init__(self):
            self.download_calls = 0

        async def download_media(self, media, file):
            self.download_calls += 1
            output_path = Path(file).with_suffix(".jpg")
            _write_jpeg(output_path)
            return str(output_path)

    client = DownloadingClient()
    dialog = {"id": 9001, "title": "VIP BTC Room", "archived": True}

    payloads = asyncio.run(
        fetch_dialog_messages(
            client,
            dialog,
            limit=10,
            media_root=media_root,
        )
    )

    assert payloads[0]["media"]["path"] == "9001/77.jpg"
    assert client.download_calls == 0
    assert is_usable_image_file(existing)


def test_fetch_dialog_messages_redownloads_a_zero_byte_existing_media_file(tmp_path):
    media_root = tmp_path / "downloaded-media"
    existing = media_root / "9001" / "77.jpg"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"")

    class DownloadingClient(_FakeClient):
        def __init__(self):
            self.download_calls = 0

        async def download_media(self, media, file):
            self.download_calls += 1
            output_path = Path(file).with_suffix(".jpg")
            _write_jpeg(output_path)
            return str(output_path)

    client = DownloadingClient()
    dialog = {"id": 9001, "title": "VIP BTC Room", "archived": True}

    payloads = asyncio.run(
        fetch_dialog_messages(client, dialog, limit=10, media_root=media_root)
    )

    assert payloads[0]["media"]["path"] == "9001/77.jpg"
    assert client.download_calls == 1
    assert is_usable_image_file(existing)


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


def test_fetch_dialog_messages_keeps_message_when_media_download_times_out(tmp_path):
    class SlowDownloadingClient(_FakeClient):
        async def download_media(self, media, file):
            await asyncio.sleep(0.05)
            return str(Path(file).with_suffix(".jpg"))

    dialog = {"id": 9001, "title": "VIP BTC Room", "archived": True}
    payloads = asyncio.run(
        fetch_dialog_messages(
            SlowDownloadingClient(),
            dialog,
            limit=10,
            media_root=tmp_path / "downloaded-media",
            media_download_timeout_seconds=0.001,
        )
    )

    assert payloads[0]["message_id"] == 77
    assert payloads[0]["media"]["path"] is None
