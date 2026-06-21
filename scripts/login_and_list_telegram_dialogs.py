import asyncio
import getpass

from list_telegram_dialogs import main as list_dialogs
from telegram_kol_research.telegram_client import (
    create_telegram_client,
    ensure_telegram_login,
    load_telegram_auth_config,
)


async def main() -> None:
    client = create_telegram_client(load_telegram_auth_config())
    try:
        await ensure_telegram_login(
            client,
            prompt_phone=lambda: input("Telegram phone number: "),
            prompt_code=lambda: input("Telegram login code: "),
            prompt_password=lambda: getpass.getpass("Telegram 2FA password: "),
            echo=print,
        )
        authorized = await client.is_user_authorized()
        print(f"authorized={authorized}")
        if not authorized:
            raise SystemExit("Telegram login did not complete; please rerun and enter the code.")
    finally:
        await client.disconnect()
    await list_dialogs()


if __name__ == "__main__":
    asyncio.run(main())
