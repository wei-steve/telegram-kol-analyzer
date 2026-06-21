import asyncio
import getpass
from pathlib import Path

import qrcode

from telegram_kol_research.telegram_client import (
    create_telegram_client,
    load_telegram_auth_config,
)


async def main() -> None:
    client = create_telegram_client(load_telegram_auth_config())
    await client.connect()
    try:
        if await client.is_user_authorized():
            print("authorized=True")
            return

        qr_login = await client.qr_login()
        output_path = Path("data/telegram_qr.png")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        qrcode.make(qr_login.url).save(output_path)
        print(f"QR_PATH={output_path.resolve()}")
        print("Open Telegram on your phone: Settings > Devices > Link Desktop Device, then scan the QR.")

        try:
            await qr_login.wait(timeout=180)
        except Exception as exc:
            if exc.__class__.__name__ != "SessionPasswordNeededError":
                raise
            password = getpass.getpass("Telegram 2FA password: ")
            await client.sign_in(password=password)

        print(f"authorized={await client.is_user_authorized()}")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
