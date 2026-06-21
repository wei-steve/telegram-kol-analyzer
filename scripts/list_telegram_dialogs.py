import asyncio
import json
from pathlib import Path

from telegram_kol_research.telegram_client import (
    create_telegram_client,
    load_telegram_auth_config,
)


async def main() -> None:
    client = create_telegram_client(load_telegram_auth_config())
    await client.connect()
    rows = []
    async for dialog in client.iter_dialogs():
        if not (getattr(dialog, "is_group", False) or getattr(dialog, "is_channel", False)):
            continue
        entity = getattr(dialog, "entity", None)
        rows.append(
            {
                "id": getattr(dialog, "id", None),
                "title": getattr(dialog, "title", None),
                "archived": bool(getattr(dialog, "archived", False)),
                "is_group": bool(getattr(dialog, "is_group", False)),
                "is_channel": bool(getattr(dialog, "is_channel", False)),
                "megagroup": bool(getattr(entity, "megagroup", False)),
                "broadcast": bool(getattr(entity, "broadcast", False)),
            }
        )
    await client.disconnect()

    rows.sort(key=lambda row: (not row["archived"], (row["title"] or "").lower()))
    output_path = Path("data/telegram_dialogs.json")
    output_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"exported={len(rows)} path={output_path}")
    for row in rows[:120]:
        status = "archived" if row["archived"] else "active"
        kind = "group" if row["is_group"] else "channel"
        print(f"{row['id']} | {status} | {kind} | {row['title']}")


if __name__ == "__main__":
    asyncio.run(main())
