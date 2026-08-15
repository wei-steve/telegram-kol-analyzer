# Production Monitor v2 Task 12 删除清单

## 目的

这是阶段一的强制交接清单。新路径完成 shadow 和切换验证后，Task 12
必须删除每一项可达的旧 Monitor 逻辑。不允许以“暂时兼容”、隐藏开关或
无人调用的死代码形式永久保留。回滚依靠已审查 Git/unit 版本。

## 必须删除的文件和 unit

- 删除 `deploy/systemd/telegram-kol-monitor.service`。
- 删除 `deploy/systemd/telegram-kol-monitor.timer`。
- 删除 `deploy/systemd/telegram-kol-monitor-diagnostic.service`。
- 删除 `deploy/systemd/telegram-kol-monitor-test-notification.service`。
- 删除 `scripts/install_server_monitor.sh`。
- 删除或缩减到“不含任何旧 runtime 行为”的
  `src/telegram_kol_research/production_safety_monitor.py`。完成后正式代码不得再
  import 它。
- 删除生产上对应的四个精确旧路径
  `/etc/systemd/system/telegram-kol-monitor.service`、
  `/etc/systemd/system/telegram-kol-monitor.timer`、
  `/etc/systemd/system/telegram-kol-monitor-diagnostic.service` 和
  `/etc/systemd/system/telegram-kol-monitor-test-notification.service`。禁止使用
  `telegram-kol-monitor.*` glob，因为它会误删 v2 snapshot/audit/db-stage unit。
  另外删除
  `/etc/telegram-kol-monitor.env`、`/etc/telegram-kol-monitor.credentials` 和旧
  `/var/lib/telegram-kol-monitor/state.json`；这只能在 Task 15 的单独 cleanup 部署
  批准后执行，不是 Task 12 本地工作的授权。

## 必须删除或迁移的精确 Python 符号

以下符号的旧调度、判定、通知、恢复和 state 责任必须删除：

- `MonitorExpectations`, `MonitorSnapshot`, `MonitorResult`,
  `MonitorAlertPresentation`, `MonitorState`, `MonitorNotificationDecision`,
  `MonitorRunOutcome`, `ProductionSafetyAdapters`;
- `run_production_safety_monitor`, `evaluate_monitor_snapshot`,
  `fingerprint_monitor_result`, `decide_monitor_notification`,
  `format_monitor_alert`, `format_monitor_recovery`,
  `build_monitor_alert_presentation`, `send_monitor_test_notification`;
- `load_monitor_state`, `_load_monitor_state`, `save_monitor_state`,
  `_monitor_state_from_payload`, `_monitor_state_payload`;
- `should_run_daily_audit`, `run_daily_management_audit` 以及任何“full audit
  in sentinel”路径；低频审计只由 `run-production-monitor-audit` 拥有；
- v1 路由 `build_monitor_incident_capture_projection`,
  `send_monitor_incident_capture`，以及 Web receiver 中的 v1 monitor-capture
  parser/branch；
- 旧普通通知路由 `_load_monitor_bot_config`, `_NOTIFICATION_SUPPRESSION`,
  `_LOW_REPEAT_REASON_CODES`, `_ALERT_RULES`, `_ALERT_REASON_PRIORITY`,
  `MONITOR_TEST_NOTIFICATION_TEXT` 和所有直接 system-bot 发送/恢复分支；
- 旧退出码耦合 `MonitorRunOutcome.exit_code`（即
  `0 if self.result.healthy and self.monitor_error is None else 1`）；
- 重复 schema authority `_ADAPTER_NAMES`,
  `_MONITOR_CAPTURE_REASON_CODES`, `_MONITOR_CAPTURE_NOTIFICATION_ERRORS`。

以下符号只有迁移到当前唯一责任主人后才能保留，迁移后旧模块中
必须删除：

- `_run_bounded_command` 和相关 `_CommandResult`/`_kill_and_wait` 移到
  `src/telegram_kol_research/bounded_subprocess.py`，然后更新
  `runtime_agent_production_audit.py`;
- `capture_uncaptured_runtime_incident_sources` 移到 Runtime Incident 所有的
  `runtime_incident_adapters.py` 或 `runtime_incident_scanner.py`，更新 `web_app.py`;
- `capture_monitor_state` 和任何仍读旧 `state.json` 的 Runtime Incident adapter
  必须改读严格 v2 state/projection；删除
  `source_kind="production_safety_monitor"`、
  `source_kind="production_safety_monitor_notification"` 与只为 v1 保留的
  capture branch;
- `_read_reconciliation_json` 和仍被 entry fingerprint repair 需要的纯函数移到
  明确的 reconciliation/facts 模块，更新
  `tests/test_entry_assembly_fingerprint_repair.py`;
- `read_abnormal_execution_events`, `read_entry_preamble_invariants`,
  `read_adjacent_entry_invariants`, `read_composite_management_invariants`,
  `read_loopback_settings`, `read_message_operation_coverage` 只能在证明是当前 v2
  唯一 fact reader 时迁移到 `production_monitor_facts.py`；否则删除。

## 必须删除的 CLI 和环境项

- 整个 `monitor-production-safety` Typer command。
- 该旧 command 的 `--notify`, `--test-notification`, `--force-full-audit`,
  `--lookback-minutes`, `--runtime-incident-capture-url` 及旧
  `--state-path /var/lib/telegram-kol-monitor/state.json`。
- 只服务于旧 Monitor 的 `TELEGRAM_KOL_MONITOR_EXPECTED_*`、
  `TELEGRAM_KOL_SYSTEM_BOT_*` 安装分支和
  `/etc/telegram-kol-monitor.env` 生成逻辑。v2 sentinel 仍需要的精确 expected
  值必须留在 v2 自己的 root-owned 环境中，不得误删。

## 必须删除的旧 state schema

- `_LEGACY_STATE_FIELDS` 和 `_STATE_FIELDS` 旧定义。
- 字段 `last_window_at`, `last_full_audit_date`, `anomaly_fingerprint`,
  `last_notification_at`, `active_reason_codes`。
- 接受“旧四字段”并自动补 `active_reason_codes` 的 compatibility reader。
- `/var/lib/telegram-kol-monitor/state.json` 的 Runtime Agent ACL/read branch：
  `scripts/install_runtime_agent_sidecar.sh` 中 `MONITOR_STATE_DIRECTORY`,
  `MONITOR_STATE_PATH` 和对该路径的 `setfacl`。运行时 Agent 如仍需监控证据，
  必须改读严格 v2 投影，不得保留旧 reader。

## 必须删除的 unit directive 和 installer branch

- 旧 `ExecStart=... monitor-production-safety ... --notify`。
- diagnostic 的 `ExecStart=... --force-full-audit`。
- test notification 的 `ExecStart=... --notify --test-notification`。
- 旧 `Unit=telegram-kol-monitor.service`、30 分钟 timer cadence 和
  `systemctl enable --now telegram-kol-monitor.timer` 分支。
- 三个旧 service 中
  `BindReadOnlyPaths=-/opt/telegram-kol-analyzer/data/web_cache/deepcoin_live_positions.json`；
  `live_position_snapshot.py` 和 Web UI 本身不删，只删 Monitor 依赖。
- `scripts/install_server_monitor.sh` 中 `--enable`、三个
  `--expected-entry-*-mode` 解析分支、bot credential allowlist、
  `LIVE_POSITION_SNAPSHOT` 可读性分支、旧 state 创建/所有权分支、diagnostic/test
  unit 复制分支和旧 timer enable 分支。

## 必须删除或重写的测试和 fixtures

- 删除/重写 `tests/test_production_safety_monitor.py`；以下旧专属组不得留下：
  `test_monitor_state_*`, `test_legacy_four_field_monitor_state_*`,
  `test_*notification*`, `test_*recovery_notice*`, `test_*formatter*`,
  `test_*presentation*`, `test_*daily_audit*`, `test_*force_full_audit*`,
  `test_monitor_orchestration_*`, `test_*live_position*`。仍有价值的纯 fact/read-only
  断言必须随新主人移到 v2/Runtime Incident 测试。
- 删除/重写 `tests/test_server_monitor_installation.py`；删除对旧四个 unit、
  `install_server_monitor.sh`、`--enable`、`--notify`、`--test-notification`、
  `--force-full-audit`、旧 timer cleanup 和 UI cache mount 的 fixtures/assertions。
- 修改 `tests/test_cli_smoke.py`：删除 `monitor-production-safety` 可见性/help/调用、
  `MonitorExpectations`/`MonitorSnapshot`/`evaluate_monitor_snapshot` imports 以及旧三个 flag
  测试。
- 修改 `tests/test_entry_assembly_fingerprint_repair.py`：不得再从
  `production_safety_monitor` import `_read_reconciliation_json` 或
  `read_entry_preamble_invariants`。
- 修改 `tests/test_runtime_agent_cli.py`、Runtime Agent installer/static tests以及 Web tests：
  删除旧 `monitor-state.json` fixture 和
  `production_safety_monitor_notification` 来源依赖，改用严格 v2 投影。
- 新增端到端静态缺失测试：全仓 `rg` 不得命中旧 command/unit/安装器/
  state schema/direct-notify/UI-cache-monitor 路径；历史 plan 文档可保留为不可执行记录，
  但当前操作文档不得再推荐旧命令。

## 必须更新的当前文档

- `docs/server-deployment.md`：删除旧 Monitor 安装/启用/回滚命令，改为
  v2 唯一当前路径。
- `docs/migration-handoff.md`：将 30 分钟旧 timer 段落标记为已完成的历史
  evidence，不再当作当前操作说明。
- `docs/runbook.md`、`docs/runtime-incident-agent-runbook.md`、
  `docs/runtime-incident-agent-status.md` 和任何当前引用
  `install_server_monitor.sh`、旧 unit、`--notify`、旧 state 的段落。
- `docs/entry-preamble-live-verification.md` 中对旧 timer 的停用/恢复步骤，
  必须改为 v2 结构化门禁步骤或明确归档为历史。
- 保留旧设计/plan 作为历史记录时，必须明确它们不是现行 runbook；
  不得改写历史为“从未存在”。

## Task 12 完成证据

Task 12 必须附带：旧符号/路径空集的静态证明、v2 测试、Runtime
Incident/Agent 回归、部署门禁 fail-closed 测试和独立审查 `0 Critical / 0
Important`。阶段二完成前，项目不得宣布“旧 Monitor 已清理”。
