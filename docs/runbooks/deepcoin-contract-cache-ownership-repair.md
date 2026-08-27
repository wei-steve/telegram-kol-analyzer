# Deepcoin 合约规格缓存所有权修复 Runbook

本 runbook 只定义后续独立批准阶段的安全操作。阶段 1 不授权 push、部署、
SSH、重启、生产数据库写入、交易设置修改或任何 Deepcoin 写操作。原始 JSON、
交易所明细和长日志只写入 root-owned `0600` 的 server evidence file；状态文件
只记录 SHA、时间窗、门禁结论和证据路径。

缓存目标固定为
`/opt/telegram-kol-analyzer/data/deepcoin_contract_specs_cache.json`。存在时必须是
owner/group `telegram-kol-worker:telegram-kol-runtime`、mode `0660`、regular file、
`st_nlink == 1`，且 ACL 包含 `u:telegram-kol-agent:---`。父目录即使是
sticky 1777（mode `1777`），组写权限也不能替代目标 owner。只允许固定 helper
`/usr/local/libexec/telegram-kol-worker-prepare-contract-cache` 收敛该单一目标；
禁止递归 `chown`、递归 `chmod`、目录级 ACL 重写、手工替换缓存或删除未知对象。

## 1. 只读 preflight

授权要求：只读生产诊断许可。任何结果缺失、不完整或歧义都按 unknown 处理并
停止，不得按 0 或健康处理。证明存在安全窗口前不得进行下一段 mutation。

将下列输出重定向到本次 server evidence file：

```bash
git -C /opt/telegram-kol-analyzer rev-parse HEAD
git -C /opt/telegram-kol-analyzer status --short
systemctl is-active telegram-kol.service telegram-kol-ingest.service \
  telegram-kol-worker.service telegram-kol-web.service
systemctl is-active telegram-kol-monitor.timer
curl -fsS http://127.0.0.1:8000/api/trading-settings
```

只读门禁必须共同证明：权威 checkout/branch/候选 exact SHA 正确、tracked tree
干净、只有一个完整 runtime topology、无时间敏感策略操作、所有 active-write
查询完整且为 `active_write_count=0`、队列/claim/recovery/监听/对账无异常、
Deepcoin 当前 position、pending regular order 与 pending trigger/TPSL 查询完整，
active row 均能唯一归因。schema-valid 但达到 100-row 上限的 history/fills 只记为
有界历史覆盖；除非 active row 需要窗口外证据，否则不作为缓存迁移 blocker。

旧生产只允许一个已识别可迁移旧版漂移：缓存是固定 regular single-link 目标、
group/mode 正确，root owner 与缺失的 Agent deny ACL 是全部差异。候选 updater 的
固定 helper 负责在冻结部署事务内同时收敛这两项。任何
unknown owner/type/link/group/mode/ACL、父目录或 directory-entry binding 异常仍
fail-closed。

若旧生产已有 contract-spec health endpoint，它必须返回 HTTP 200 和完整 schema。
只有 production SHA 已核验为 previous SHA、closed legacy monitor env 通过且端点
明确返回 HTTP 404 时，才能记录 `legacy_capability_absent`。401/403、timeout、
非 404 HTTP 错误或 malformed schema 都是 blocker。最近新增的
`contract_spec_sync_unavailable` exact set 也必须完整。此处禁止制造 Telegram
流量、历史信号 replay、补单或调用 Deepcoin 写接口。

失败后的安全终态：代码和设置均未改变，`auto_trade` 保持原值，阶段保持
`in_progress`，记录缺失证据后停止。

## 2. 冻结写入

授权要求：单独、明确的生产交易设置冻结批准。写入前重新 GET 完整 settings，
只改 `auto_trade_enabled` 为 `false`，不得复用旧快照覆盖其他字段：

```bash
curl -fsS http://127.0.0.1:8000/api/trading-settings \
  > /run/telegram-kol-contract-cache-settings.current.json
jq '.auto_trade_enabled=false' \
  /run/telegram-kol-contract-cache-settings.current.json \
  > /run/telegram-kol-contract-cache-settings.frozen.json
curl -fsS -X POST -H 'Content-Type: application/json' \
  --data-binary @/run/telegram-kol-contract-cache-settings.frozen.json \
  http://127.0.0.1:8000/api/trading-settings
curl -fsS http://127.0.0.1:8000/api/trading-settings \
  | jq -e '.auto_trade_enabled == false'
```

冻结读回成功后，以 `MAX(raw_messages.id)` 记录
`freeze_raw_message_id`。它只标记冻结窗口的审计起点，不是恢复后的执行水位：
冻结期间到达但因 `auto_trade_enabled=false` 而终止的消息同样不得在恢复时重放。
冻结时动态记录全部历史 `contract_spec_sync_unavailable` exact set；每条必须保持
`verified_refusal` 且 `attempted_exchange_write=0`。该集合永不重放、永不补单，也
不因修复缓存而自动执行。另外 4 条旧的 zero-write 非终态执行合同必须在重跑
Task 12 前仅用生产只读证据解释；本地文档不得猜测、重分类或修改它们。
不得发送测试 Telegram 消息。

失败后的安全终态：若写入结果或读回不完整，禁止部署并人工核对；不得猜测当前
设置。只要已确认 false，就保持冻结，不进行自动恢复。

## 3. exact-SHA 部署与权限/刷新验证

授权要求：独立的 exact-SHA 部署批准；必须携带已评审的完整 40 位 SHA，且
冻结读回、preflight 与无在途操作门禁仍有效。部署 helper 必须显式携带：

```bash
EXPECTED_COMMIT='<reviewed-40-char-sha>' \
EXPECTED_AUTO_TRADE_STATE=disabled \
BRANCH=codex/deepcoin-auto-trading-v1 \
./scripts/server_git_update.sh
```

updater 必须保持 monitor timer/oneshot 的原状态，原子更新 expected HEAD 与
冻结期望，事务化安装 candidate monitor unit；失败时恢复旧 unit/env。
代码回滚不自动恢复交易设置，尤其不得把 `auto_trade_enabled` 改回 true。

仅在 monolith 已停止、第二次 active-write 查询再次得到 0 后继续；证据中必须
出现 `telegram-kol.service stopped` 和 `active_write_count=0`。split worker 启动
前仅运行固定 helper：

```bash
/usr/local/libexec/telegram-kol-worker-prepare-contract-cache
/usr/local/libexec/telegram-kol-worker-prepare-contract-cache --check
```

随后用 worker 的受控 refresh 路径生成/刷新缓存，再从 worker-only 健康端点验证
`fresh`、成功时间、expiry、instrument count 和所有权合同。检查 8000/8001/8002
进程角色、backlog、重复处理、监听/对账、monitor reason codes，以及冻结后自然
新消息没有产生 exchange write。Deepcoin 验证仅允许只读订单/持仓历史；禁止
place/cancel/modify。

部署后不再接受 `legacy_capability_absent` 或任何已识别可迁移旧版漂移：helper
`--check` 必须满足完整候选合同，authenticated contract-spec health 必须返回
HTTP 200 和 exact schema，然后才可进入冻结观察。

失败后的安全终态：runtime 按 updater 的已测回滚路径恢复；monitor old unit/env
恢复；timer 回到原状态；交易仍冻结。权限、刷新或外部查询 unknown 时不得启动
恢复段。

## 4. 单独恢复

授权要求：独立的恢复自动交易批准。必须重新执行全部只读 preflight，并确认
缓存 fresh、worker ownership/ACL 合同通过、零新增同步拒绝、无 backlog/duplicate/
recovery、自然 future-only 流量验收通过，且 Deepcoin 当前账户快照完整、active row
唯一归因。100-row 有界历史覆盖不冒充完整账户历史；任何 active row 需要窗口外
证据时仍 fail-closed。恢复部署/监控期望必须显式使用：

```bash
EXPECTED_COMMIT='<deployed-40-char-sha>' \
EXPECTED_AUTO_TRADE_STATE=enabled \
BRANCH=codex/deepcoin-auto-trading-v1 \
./scripts/server_git_update.sh
```

updater 成功后、紧邻 settings 恢复写入前，重新查询 `MAX(raw_messages.id)`，把
结果记录为 `restore_raw_message_id`。只有之后自然到达、满足
`raw_messages.id > restore_raw_message_id` 的新消息才可进入现有权威识别和执行链；
所有 `raw_messages.id <= restore_raw_message_id` 的消息（包括冻结前、冻结期间以及
updater 执行间隙到达的消息）都保持历史终态，禁止回扫、重放或补单。

然后重新 GET 当前完整 settings，只把 `auto_trade_enabled` 改为 `true`，POST 后
立即读回。不得修改 `restore_raw_message_id`，也不得把
`freeze_raw_message_id` 误用为恢复水位。

失败后的安全终态：保持 `auto_trade_enabled=false` 和 monitor disabled expectation，
不重试交易、不补单；记录 evidence path 并等待新的恢复授权。
