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
Deepcoin 只读订单和持仓历史能完成唯一归因。合约规格健康投影和最近新增的
`contract_spec_sync_unavailable` 数也必须完整。此处禁止制造 Telegram 流量、
历史信号 replay、补单或调用 Deepcoin 写接口。

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

记录冻结后的首个自然到达 `raw_message_id` 作为 future-only 水位。已知的 14 条
历史 `contract_spec_sync_unavailable` 拒绝永不重放、永不补单，也不因修复缓存
而自动执行。不得发送测试 Telegram 消息。

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

失败后的安全终态：runtime 按 updater 的已测回滚路径恢复；monitor old unit/env
恢复；timer 回到原状态；交易仍冻结。权限、刷新或外部查询 unknown 时不得启动
恢复段。

## 4. 单独恢复

授权要求：独立的恢复自动交易批准。必须重新执行全部只读 preflight，并确认
缓存 fresh、worker ownership/ACL 合同通过、零新增同步拒绝、无 backlog/duplicate/
recovery、自然 future-only 流量验收通过且 Deepcoin 只读历史完整。恢复部署/监控
期望必须显式使用：

```bash
EXPECTED_COMMIT='<deployed-40-char-sha>' \
EXPECTED_AUTO_TRADE_STATE=enabled \
BRANCH=codex/deepcoin-auto-trading-v1 \
./scripts/server_git_update.sh
```

然后重新 GET 当前完整 settings，只把 `auto_trade_enabled` 改为 `true`，POST 后
立即读回。不得修改水位，不得回扫或重放冻结前消息；只允许冻结后水位之后自然
到达的未来新信号进入现有权威识别和执行链。

失败后的安全终态：保持 `auto_trade_enabled=false` 和 monitor disabled expectation，
不重试交易、不补单；记录 evidence path 并等待新的恢复授权。
