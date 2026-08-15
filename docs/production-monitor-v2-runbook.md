# Production Monitor v2 操作手册（阶段一）

## 当前状态和安全边界

这份手册当前只描述已完成的本地设计和将来经单独批准后的操作。
Monitor v2 没有因为本文档而部署、启动或取代生产的
`telegram-kol-monitor.timer`。本地完成后必须停在 Task 10 的普通代码
部署审批边界。

不得用这套监控停止交易主服务、改数据库、重放历史消息、调用
Deepcoin 写接口或启用 MiMo v2。停服双 dry-run、Batch 119 apply 和普通
代码部署仍是三个分开的明确审批边界。

## 先看懂“两个仪表”

监控每次给出两个互不代替的结果：

| 仪表 | 值 | 通俗含义 |
| --- | --- | --- |
| `execution_status` | `COMPLETED` | 监控程序运行完，并成功保存本次结果。 |
| `execution_status` | `FAILED` | 监控程序自己没有完成，systemd 才应该显示失败。 |
| `observed_health` | `HEALTHY` | 所有必需证据完整、新鲜、时间顺序正确，且未发现异常。 |
| `observed_health` | `UNHEALTHY` | 有完整证据证明异常。程序仍可以是 `COMPLETED`。 |
| `observed_health` | `UNKNOWN` | 证据缺失、过期、部分返回或时间对不上，所以不猜。 |

例如 `COMPLETED + UNHEALTHY` 表示“体检报告顺利写完，报告确认有问题”，
不是“监控程序崩溃”。

## 三种不应过早报错的状态

- `SETTLING`：交易所可能还在回传结果。在该操作自己的
  `execution_deadline_at` 前不报故障。超过截止时间后，位置或保护单
  这类跨系统差异通常还要两个不同的、完整的交易所代次，而且
  两个代次都必须晚于本地 `last_progress_at` 和截止时间。
- `STARTING`：主服务已启动，但 Deepcoin 对账、管理 worker 或消息
  supervisor 还没有完成第一个成功周期。单纯“等了几分钟”不能把它
  自动变健康。
- `UNKNOWN`：当下无法可靠判断。这不是已证明的业务故障，所以不向
  Agent 提交猜测的事故；但它也绝不等于健康。

`SETTLING`、`STARTING`、`UNKNOWN`、`UNHEALTHY`、`FAILED` 都阻止部署。
systemd 显示最近一次 oneshot 退出码为 0，不能覆盖这条规则。

## 精确新鲜度和时间预算

| 证据 | 边界 |
| --- | --- |
| 密封 Deepcoin 快照 | 完成时间距 sentinel 检查时间不得超过 5 分钟；单次抓取必须大于 0 且不超过 45 秒。 |
| 对账、管理 worker、消息 supervisor 成功心跳 | 不得超过 5 分钟；未来时间戳直接不可用。 |
| message-operation coverage 心跳 | 不得超过 5 分钟；超过当前时间 1 分钟也不可用。 |
| 结构化 journal 捕获 | 完成时间不得超过 5 分钟；捕获窗口不得超过 1 分钟。 |
| sentinel 结构化结果 | 运行频率是 5 分钟；部署门禁在 Task 13 中将以“年龄不超过 10 分钟”强制判定，超过 10 分钟即阻止。在 Task 13 完成前不得宣称新门禁已生效。 |
| incident intake | 首次失败后 10 分钟内只重查，不直接 fallback。 |
| deterministic notification | incident 接受后 10 分钟内只查通道状态；只有过期且重查仍失败才能用固定 fallback。 |
| fallback retry | 失败后等待 5 分钟再试。 |

一个合法的空列表是“已完整读取，结果为空”。超时、限流、
分页未完成或部分空列表是“没读全”，不得当作没有持仓/挂单。

## 只看脱敏状态

以下命令只显示状态、时间、原因码和截断后的指纹，不显示订单、
消息、密钥或完整账号指纹：

```bash
sudo jq '{schema_version,latest_completed_result,
  candidates:[.candidates[]?|{reason_code,lifecycle,first_observed_at,
    last_observed_at,execution_deadline_at,
    fingerprint:(.fingerprint[0:12]+"...")}],
  incident_acceptances:[.incident_acceptances[]?|{accepted_at,routing_terminal,
    submission_id:(.submission_id[0:12]+"...")}],fallback,latest_audit_result}' \
  /var/lib/telegram-kol-monitor-v2/sentinel/sentinel-v2.json

sudo jq '{latest_attempt:(.latest_attempt|{generation,outcome,
  request_started_at,request_completed_at,failure_code,
  uid_scope_hash:(.uid_scope_hash[0:12]+"..."),
  collections:[.collections[]?|{name,available,schema_valid,complete,
    page_count,row_count,reason_code}]}),
  retained_generations:[.generations[]|{generation,outcome,
    request_started_at,request_completed_at}]}' \
  /var/lib/telegram-kol-monitor-v2/snapshot/manifest.json
```

不要用 `cat` 或 `journalctl -o cat` 输出原始快照、原始事故证据或环境文件。

## 单独批准后的 no-notify shadow

下列命令只能在 Task 10 后收到普通代码部署的明确批准、安全窗口
已证明、v2 单元已以 disabled/inactive 安装且读只凭证已证明时执行。
不得在本地 Task 9 执行。

`--shadow-only` 会继续读取完整证据并写入独立 shadow state，但强制
`incident_router=None`，并跳过 intake/fallback 重查；因此不会创建 incident、
发 Telegram 通知或排入 Agent。必须使用与正式 state 不同的文件：

```bash
sudo systemctl start telegram-kol-monitor-snapshot.service

# 临时 drop-in 仅修改两处：独立 shadow state 和 --shadow-only。
# ExecStartPre、身份、mount 和 sandbox 全部沿用已安装审查版本。
sudo systemctl edit --runtime --drop-in=shadow-only.conf --stdin \
  telegram-kol-sentinel.service <<'EOF'
[Service]
ExecStart=
ExecStart=/opt/telegram-kol-analyzer/.venv/bin/telegram-kol-research run-production-monitor-sentinel --state-path /var/lib/telegram-kol-monitor-v2/sentinel/shadow-sentinel-v2.json --snapshot-path /var/cache/telegram-kol-monitor-v2/sentinel/snapshot.json --database-path /var/cache/telegram-kol-monitor-v2/sentinel/research-snapshot.db --checkout-path /opt/telegram-kol-analyzer --settings-url http://127.0.0.1:8000/api/trading-settings --coverage-path /var/cache/telegram-kol-monitor-v2/sentinel/coverage.json --journal-path /var/cache/telegram-kol-monitor-v2/sentinel/journal.json --expected-head ${TELEGRAM_KOL_MONITOR_EXPECTED_HEAD} --expected-auto-trade true --expected-position-limit 4 --expected-management-mode live --expected-preamble-mode live --readiness-url http://127.0.0.1:8000/api/runtime-monitor-readiness --incident-loopback-url http://127.0.0.1:8000/api/runtime-incidents/monitor-capture --shadow-only
EOF
sudo systemctl daemon-reload
sudo systemctl start telegram-kol-sentinel.service
sudo systemctl --no-pager --full status telegram-kol-sentinel.service

# 每次 shadow 结束后立即删除这个精确的 runtime drop-in；不用
# systemctl revert，避免触碰 /etc 中已安装的 reviewed unit。
sudo rm -f /run/systemd/system/telegram-kol-sentinel.service.d/shadow-only.conf
sudo systemctl daemon-reload
if systemctl cat telegram-kol-sentinel.service | grep -Fq -- '--shadow-only'; then
  echo 'shadow-only runtime override was not removed' >&2
  exit 1
fi
```

上面的命令必须与当次已审查、已安装 unit 的 `ExecStart` 逐字比较；
如当次 reviewed unit 的参数已变，停止并更新手册/审查，不得猜测。命令中
`${TELEGRAM_KOL_MONITOR_EXPECTED_HEAD}` 由 root-owned `EnvironmentFile` 在 systemd 内展开，
不得手填 SHA。运行后立即用前一节的
同样脱敏视图检查下面这个“精确 shadow 路径”，不得误读正式
`sentinel-v2.json`：

```bash
sudo jq '{schema_version,latest_completed_result,
  candidates:[.candidates[]?|{reason_code,lifecycle,first_observed_at,
    last_observed_at,execution_deadline_at,
    fingerprint:(.fingerprint[0:12]+"...")}],
  incident_acceptances:[.incident_acceptances[]?|{accepted_at,routing_terminal,
    submission_id:(.submission_id[0:12]+"...")}],fallback,latest_audit_result}' \
  /var/lib/telegram-kol-monitor-v2/sentinel/shadow-sentinel-v2.json
```

同时证明 runtime incident ledger、Telegram 通知和 Agent claim 水位全部不变。

## 激活顺序

只有 Task 11 的单独批准和所有先决证据齐全后才能激活：

1. 安装 v2 单元后，先证明六个 v2 service/timer 全部 disabled/inactive；
   旧 `telegram-kol-monitor.timer` 状态不变。
2. 证明 snapshot 专用凭证在交易所侧确实只读，然后才允许一次手动
   refresher canary。
3. 运行不通知的 shadow，稳定状态和可用的 settling 模拟全部通过，
   并证明没有 incident/通知/Agent claim。
4. 先 `enable --now telegram-kol-monitor-snapshot.timer`；等到三个完整、不同、
   新鲜的代次。
5. 再 `enable --now telegram-kol-sentinel.timer`；必须先证明身份验证的幂等
   no-op capture 正常，才允许 Runtime Incident 拥有正常通知。
6. 最后 `enable --now telegram-kol-monitor-audit.timer`。重审计不得放回五分钟
   sentinel 路径。
7. 证明新路径稳定后再安排 Task 12 删除旧 Monitor。阶段一不删旧路径，
   但也绝不会把双路径保留为永久兼容。

## 回滚

切换失败时，不改数据库、不重放消息、不更改交易状态：

```bash
sudo systemctl disable --now telegram-kol-monitor-audit.timer
sudo systemctl disable --now telegram-kol-sentinel.timer
sudo systemctl disable --now telegram-kol-monitor-snapshot.timer
sudo systemctl stop telegram-kol-monitor-audit.service \
  telegram-kol-sentinel.service telegram-kol-monitor-snapshot.service
```

然后按 Task 11 事前记录的确切状态恢复已审查的旧 timer，并重新检查主服务、
Telegram intake、reconciliation 和 Runtime Agent。回滚使用已审查的 Git/unit
版本，不靠保留死代码开关。如果新旧两套都无法安全运行，记录
`monitoring_paused`，并继续阻止部署；绝不伪造 `HEALTHY`。
