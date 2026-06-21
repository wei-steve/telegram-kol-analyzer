import asyncio
from pathlib import Path

from opentele.api import UseCurrentSession
from opentele.td import TDesktop


TDATA_PATH = Path(
    r"C:\Users\dgtan\AppData\Local\Packages\TelegramMessengerLLP.TelegramDesktop_t4vj0pshhgkwm"
    r"\LocalCache\Roaming\Telegram Desktop UWP\tdata"
)
SESSION_PATH = Path("data/telegram.session")


async def main() -> None:
    if not TDATA_PATH.exists():
        raise SystemExit(f"tdata path not found: {TDATA_PATH}")

    SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
    for path in [
        SESSION_PATH,
        SESSION_PATH.with_name(f"{SESSION_PATH.name}-journal"),
        SESSION_PATH.with_name(f"{SESSION_PATH.name}.lock"),
    ]:
        if path.exists():
            path.unlink()

    desktop = TDesktop(str(TDATA_PATH))
    if not desktop.isLoaded():
        raise SystemExit("Telegram Desktop tdata could not be loaded")

    client = await desktop.ToTelethon(str(SESSION_PATH), flag=UseCurrentSession)
    try:
        await client.connect()
        print(f"authorized={await client.is_user_authorized()}")
    finally:
        await client.disconnect()
    print(f"session_path={SESSION_PATH.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
