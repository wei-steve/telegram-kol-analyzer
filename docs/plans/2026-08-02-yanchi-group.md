# 颜驰 11分组接入 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将“颜驰 11分组”以仅同步、不启用 AI 或自动交易的方式接入生产系统。

**Architecture:** 使用生产 Telegram 会话按精确标题发现 `chat_id`，然后更新群组 YAML 和 KOL 短码映射。不改动消息识别、策略解析或交易执行代码。

**Tech Stack:** Python 3.12, PyYAML, Telethon, systemd, Git

---

### Task 1: 发现生产 Telegram chat_id

**Files:**
- Read: `config/groups.yaml`

**Step 1: 创建仅包含目标标题的临时发现配置**

```yaml
groups:
- chat_title: 颜驰 11分组
  enabled: true
```

**Step 2: 在服务器执行只发现同步**

Run: `.venv/bin/telegram-kol-research sync --config-path /tmp/yanchi-discover.yaml --database-path data/research.db --mode discover`

Expected: 如果群组已归档，输出唯一一个题为“颜驰 11分组”的群组及其 `chat_id`，且不写入消息。

**Step 3: 如果群组未归档，用会话副本只读枚举所有对话**

用 SQLite `.backup` 将正在使用的 Telethon 会话复制到 `/tmp`，通过 `discover_dialogs` 精确匹配标题。

Expected: 唯一匹配 `颜驰 11分组`，发现 `chat_id=-1003942765613`，且生产服务不停止。

### Task 2: 配置群组与归属短码

**Files:**
- Modify: `config/groups.yaml`
- Modify: `config/kol_codes.yaml`

**Step 1: 在本地生产配置副本中加入群组**

```yaml
- chat_title: 颜驰 11分组
  chat_id: <discovered-chat-id>
  enabled: true
  ai_strategy_enabled: false
  trading_mode: notify_only
  max_loss_usdt: 100.0
  symbol_whitelist:
  - BTC
  - ETH
```

**Step 2: 加入唯一 KOL 短码**

```yaml
kol_codes:
  "<discovered-chat-id>": YC
```

**Step 3: 加载配置验证**

Run: `PYTHONPATH=src python3 -c 'from telegram_kol_research.group_config import load_group_config; c=load_group_config("config/groups.yaml"); g=[x for x in c.groups if x.chat_title=="颜驰 11分组"]; assert len(g)==1 and g[0].chat_id and g[0].enabled and not g[0].ai_strategy_enabled and g[0].trading_mode=="notify_only"'`

Expected: PASS with exit code 0.

Run: `PYTHONPATH=src python3 -c 'from telegram_kol_research.kol_codes import load_kol_code_map; assert "YC" in load_kol_code_map().values()'`

Expected: PASS with exit code 0.

**Step 4: 执行相关回归测试**

Run: `uv run pytest tests/test_group_config.py tests/test_kol_codes.py -q`

Expected: all tests pass.

**Step 5: 提交可跟踪文件**

```bash
git add config/kol_codes.yaml docs/plans/2026-08-02-yanchi-group-design.md docs/plans/2026-08-02-yanchi-group.md
git commit -m "config: register Yanchi Telegram group"
```

### Task 3: 部署并验证

**Files:**
- Deploy: `config/groups.yaml`
- Deploy: `config/kol_codes.yaml`

**Step 1: 推送已审核提交**

Run: `git push origin codex/deepcoin-auto-trading-v1`

Expected: remote branch advances to the local commit.

**Step 2: 更新生产代码**

Run: `powershell -ExecutionPolicy Bypass -File .\\scripts\\server_git_update.ps1`

Expected: server pulls the branch, reinstalls the editable package, and restarts `telegram-kol.service`.

**Step 3: 安全部署未跟踪的群组配置**

Back up the server `config/groups.yaml`, copy the validated local file to the same path, and restart `telegram-kol.service` once.

**Step 4: 验证生产服务和配置**

Run: `systemctl is-active telegram-kol.service`

Expected: `active`.

Load both YAML files on the server and assert the exact `chat_id`, `ai_strategy_enabled: false`, `trading_mode: notify_only`, and KOL code `YC`.

**Step 5: 回滚路径**

If verification fails, restore the backed-up `config/groups.yaml`, revert the KOL-code commit, redeploy, and confirm the service returns to `active`.
