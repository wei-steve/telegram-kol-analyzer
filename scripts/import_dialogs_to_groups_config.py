import json
from pathlib import Path

import yaml


def main() -> None:
    dialogs_path = Path("data/telegram_dialogs.json")
    config_path = Path("config/groups.yaml")
    dialogs = json.loads(dialogs_path.read_text(encoding="utf-8"))
    groups = []
    for dialog in dialogs:
        title = dialog.get("title")
        chat_id = dialog.get("id")
        if not title or chat_id is None:
            continue
        groups.append(
            {
                "chat_title": title,
                "chat_id": int(chat_id),
                "enabled": True,
                "ai_strategy_enabled": False,
                "trading_mode": "notify_only",
                "max_loss_usdt": 100.0,
                "symbol_whitelist": ["BTC", "ETH"],
                "custom_group_label": title,
                "tracked_senders": [],
            }
        )
    config_path.write_text(
        yaml.safe_dump({"groups": groups}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print(f"imported={len(groups)} path={config_path}")


if __name__ == "__main__":
    main()
