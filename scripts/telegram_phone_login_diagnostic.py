import asyncio
import getpass
from pathlib import Path

from telethon.errors import (
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)

from telegram_kol_research.telegram_client import (
    create_telegram_client,
    load_telegram_auth_config,
)


def describe_sent_code(sent_code) -> None:
    code_type = getattr(sent_code, "type", None)
    next_type = getattr(sent_code, "next_type", None)
    timeout = getattr(sent_code, "timeout", None)
    print(f"sent_code_type={type(code_type).__name__ if code_type else None}")
    print(f"next_code_type={type(next_type).__name__ if next_type else None}")
    print(f"timeout={timeout}")


async def main() -> None:
    config = load_telegram_auth_config()
    session_path = Path(config.session_path)
    print(f"session_path={session_path.resolve()}")
    client = create_telegram_client(config)
    await client.connect()
    try:
        if await client.is_user_authorized():
            print("authorized=True")
            return

        phone = input("Telegram phone number, include country code, e.g. +8613800000000: ").strip()
        sent_code = await client.send_code_request(phone)
        print("Code request sent. Check Telegram official chat first; SMS may be unavailable initially.")
        describe_sent_code(sent_code)

        resend = input("If you still have no code, type SMS to force SMS; otherwise press Enter: ").strip().lower()
        if resend == "sms":
            sent_code = await client.send_code_request(phone, force_sms=True)
            print("Forced SMS request sent if Telegram allowed it.")
            describe_sent_code(sent_code)

        code = input("Telegram login code: ").strip().replace(" ", "")
        try:
            await client.sign_in(
                phone=phone,
                code=code,
                phone_code_hash=sent_code.phone_code_hash,
            )
        except SessionPasswordNeededError:
            password = getpass.getpass("Telegram 2FA password: ")
            await client.sign_in(password=password)
        except PhoneCodeInvalidError:
            raise SystemExit("The login code was invalid. Rerun this script and request a fresh code.")
        except PhoneCodeExpiredError:
            raise SystemExit("The login code expired. Rerun this script and request a fresh code.")

        print(f"authorized={await client.is_user_authorized()}")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
