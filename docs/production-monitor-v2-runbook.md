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

## Sealed runtime dependency cache

这一节只能在 Task 11 获得普通代码部署的明确批准后执行；本地审查不得执行。
它只预热 Monitor v2 自己的依赖缓存，不启动、启用或重启任何 service/timer。
预热需要访问 Python 包索引，但不得携带 Deepcoin、Telegram、代理或自定义 CA 环境变量。
`APPROVED_SHA` 必须是本次明确批准的完整 40 位 SHA，且生产 checkout 必须已由既定部署流程
更新并保持 clean：

```bash
set -euo pipefail
PATH=/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
PRODUCTION_ROOT=/opt/telegram-kol-analyzer
BUILD_CACHE_TRUST_ANCHOR=/var/cache
BUILD_CACHE_PARENT=/var/cache/telegram-kol-monitor-v2-build
BUILD_CACHE=/var/cache/telegram-kol-monitor-v2-build/uv
APPROVED_SHA='<approved-40-character-sha>'
PYTHON_PATH="$(readlink -f "$(command -v python3.12)")"
UV_PATH="$(readlink -f "$(command -v uv)")"
for TRUSTED_TOOL in "$PYTHON_PATH" "$UV_PATH"; do
  TOOL_MODE="$(stat -c %a "$TRUSTED_TOOL")"
  test -f "$TRUSTED_TOOL"
  test ! -L "$TRUSTED_TOOL"
  test "$(stat -c %u "$TRUSTED_TOOL")" = 0
  test "$(stat -c %h "$TRUSTED_TOOL")" = 1
  (( (8#$TOOL_MODE & 8#022) == 0 ))
done
"$PYTHON_PATH" -I -S -c \
  'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)'
ANCHOR_MODE="$(stat -c %a "$BUILD_CACHE_TRUST_ANCHOR")"
test -d "$BUILD_CACHE_TRUST_ANCHOR"
test ! -L "$BUILD_CACHE_TRUST_ANCHOR"
test "$(stat -c %u "$BUILD_CACHE_TRUST_ANCHOR")" = 0
(( (8#$ANCHOR_MODE & 8#022) == 0 ))
for CACHE_COMPONENT in "$BUILD_CACHE_PARENT" "$BUILD_CACHE"; do
  if sudo /usr/bin/test -e "$CACHE_COMPONENT" ||
     sudo /usr/bin/test -L "$CACHE_COMPONENT"; then
    COMPONENT_MODE="$(sudo /usr/bin/stat -c %a "$CACHE_COMPONENT")"
    sudo /usr/bin/test -d "$CACHE_COMPONENT"
    sudo /usr/bin/test ! -L "$CACHE_COMPONENT"
    test "$(sudo /usr/bin/stat -c %u "$CACHE_COMPONENT")" = 0
    (( (8#$COMPONENT_MODE & 8#022) == 0 ))
  fi
done
test "$(git -C "$PRODUCTION_ROOT" rev-parse --verify HEAD)" = "$APPROVED_SHA"
git -C "$PRODUCTION_ROOT" diff --quiet "$APPROVED_SHA" -- .
git -C "$PRODUCTION_ROOT" diff --cached --quiet "$APPROVED_SHA" -- .
test -z "$(git -C "$PRODUCTION_ROOT" ls-files --others --exclude-standard -- .)"
PREWARM_ROOT=''
cleanup_monitor_prewarm() {
  case "$PREWARM_ROOT" in
    /var/tmp/telegram-kol-monitor-v2-prewarm.??????)
      sudo rm -rf -- "$PREWARM_ROOT"
      ;;
    *)
      echo 'refusing unsafe prewarm cleanup path' >&2
      return 1
      ;;
  esac
}
trap cleanup_monitor_prewarm EXIT
PREWARM_ROOT="$(sudo /usr/bin/mktemp -d \
  /var/tmp/telegram-kol-monitor-v2-prewarm.XXXXXX)"
sudo install -d -o root -g root -m 0700 "$BUILD_CACHE_PARENT" "$BUILD_CACHE"
sudo install -d -o root -g root -m 0700 "$PREWARM_ROOT/source"
git -C "$PRODUCTION_ROOT" archive "$APPROVED_SHA" |
  sudo tar --no-same-owner -x -C "$PREWARM_ROOT/source"
PREWARM_VALIDATOR="$PREWARM_ROOT/source/scripts/validate_production_monitor_uv_cache.py"
PREWARM_SOURCE_PROBLEM="$(
  sudo find "$PREWARM_ROOT/source" -xdev \
    \( ! -user root -o -perm /022 -o -type l \) -print -quit
)"
test -z "$PREWARM_SOURCE_PROBLEM"
VALIDATOR_MODE="$(sudo stat -c %a "$PREWARM_VALIDATOR")"
sudo test -f "$PREWARM_VALIDATOR"
sudo test ! -L "$PREWARM_VALIDATOR"
test "$(sudo stat -c %u "$PREWARM_VALIDATOR")" = 0
test "$(sudo stat -c %h "$PREWARM_VALIDATOR")" = 1
(( (8#$VALIDATOR_MODE & 8#022) == 0 ))
sudo /usr/bin/python3 \
  "$PREWARM_ROOT/source/scripts/validate_production_monitor_uv_cache.py" \
  --cache-root "$BUILD_CACHE" \
  --trust-anchor "$BUILD_CACHE_TRUST_ANCHOR" \
  --expected-owner-uid 0

sudo env -i HOME=/root PATH=/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  UV_CACHE_DIR="$BUILD_CACHE" UV_LINK_MODE=copy UV_NO_CONFIG=1 \
  UV_NO_MANAGED_PYTHON=1 UV_PYTHON="$PYTHON_PATH" \
  UV_BUILD_CONSTRAINT="$PREWARM_ROOT/source/config/production-monitor-build-constraints.txt" \
  "$UV_PATH" sync --project "$PREWARM_ROOT/source" --locked --no-dev
sudo env -i HOME=/root PATH=/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  UV_CACHE_DIR="$BUILD_CACHE" UV_NO_CONFIG=1 UV_NO_MANAGED_PYTHON=1 \
  "$UV_PATH" cache prune --ci
sudo /usr/bin/python3 \
  "$PREWARM_ROOT/source/scripts/validate_production_monitor_uv_cache.py" \
  --cache-root "$BUILD_CACHE" \
  --trust-anchor "$BUILD_CACHE_TRUST_ANCHOR" \
  --expected-owner-uid 0

sudo rm -rf -- "$PREWARM_ROOT/source/.venv"
sudo env -i HOME=/root PATH=/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  UV_CACHE_DIR="$BUILD_CACHE" UV_LINK_MODE=copy UV_NO_CONFIG=1 \
  UV_NO_MANAGED_PYTHON=1 UV_PYTHON="$PYTHON_PATH" UV_OFFLINE=1 \
  UV_BUILD_CONSTRAINT="$PREWARM_ROOT/source/config/production-monitor-build-constraints.txt" \
  UV_PROJECT_ENVIRONMENT="$PREWARM_ROOT/offline-venv" \
  "$UV_PATH" sync --project "$PREWARM_ROOT/source" --locked --offline --no-dev
sudo env -i HOME=/root PATH=/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  UV_CACHE_DIR="$BUILD_CACHE" UV_NO_CONFIG=1 UV_NO_MANAGED_PYTHON=1 \
  "$UV_PATH" cache prune --ci
sudo /usr/bin/python3 \
  "$PREWARM_ROOT/source/scripts/validate_production_monitor_uv_cache.py" \
  --cache-root "$BUILD_CACHE" \
  --trust-anchor "$BUILD_CACHE_TRUST_ANCHOR" \
  --expected-owner-uid 0
cleanup_monitor_prewarm
trap - EXIT
```

任一步失败都停止，不运行 installer。尤其不能把普通用户的 `~/.cache/uv` 或共享
`/var/cache/uv` 直接交给 installer；标准 uv cache 内部的相对 symlink 只有在解析后仍
完全位于上述 root-owned 专用 cache 内时才合法。离开 cache、断链、所有权不符或
group/other 可写都会 fail closed。

## Runtime Incident bridge 的四段 policy staging

这四段只是 Task 11 获得单独生产授权后的操作顺序；本地审查期间不执行。
每次修改 root-owned runtime policy 前都要重新证明安全窗口。`systemctl daemon-reload`
不会重读主服务的环境变量；因此下面所说的 policy reload 是已审查的
`systemctl restart telegram-kol.service`，不是只跑 daemon-reload。每次 restart 后先证明
主服务、Telegram intake 和对账恢复，再继续。

### Policy stage 1 — 先把 legacy-all 变成明确 allowlist

保持 `TELEGRAM_KOL_RUNTIME_INCIDENT_CAPTURE_TYPES` 完全不变。把
`TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_TYPES` 和
`TELEGRAM_KOL_RUNTIME_AGENT_TYPES` 设成“当前已批准的旧 incident types”的精确列表，
且两个列表都不得包含 `production_monitor_incident`。缺省 selector 表示
legacy-all，不能用于这次 staging。在安全窗口内依次执行：

```bash
systemctl is-active telegram-kol-runtime-agent.service
systemctl is-enabled telegram-kol-runtime-agent.service
sudo systemctl restart telegram-kol.service
sudo systemctl try-restart telegram-kol-runtime-agent.service
```

先把 Agent 的 active/enabled 结果写入当次脱敏基线。`try-restart` 只能重启本来
active 的 Agent，不会启动本来 inactive 的单元；命令后的 active/enabled 必须与
基线一致。主服务 restart 也必须已由 Task 11 的普通生产部署/policy 变更
边界明确批准；本地审查、Batch 119 或任何之前的“同意”都不替代该授权。
验证旧 selector 内的事故仍按原政策处理，且没有任何未批准类型可被
Telegram 或 Agent claim。如果 Agent 本来未启用，保持未启用，但 selector 仍必须
显式且排除 monitor 类型。

### Policy stage 2 — 记录安全水位

先证明所有当前应发的 deterministic notification 已终态，Agent 没有正在
claim/诊断的事故。然后用 SQLite `mode=ro` 事务只读取证，不得手改数据库：

```sql
SELECT COALESCE(MAX(id), 0) FROM runtime_incidents;
SELECT COUNT(*) FROM runtime_incidents
 WHERE incident_type = 'production_monitor_incident';
```

第一个值是要 **record the exclusive notification watermark** 的安全基线；第二个值
必须为 0，也就是 **zero production_monitor_incident rows**。把第一个值写入
root-owned policy 的 `TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_AFTER_ID`，保持两个
selector 仍排除 monitor，然后在安全窗口重启主服务。不得通过设水位
跳过任何尚未终态的旧通知。Agent 没有同等的全局 ID 水位，所以“新类型行为
0”是后面放宽 Agent selector 的必要门禁。
bridge 如果把水位报成 `absent` 或 `invalid_refused` 都必须停止；后者包括配置
解析失败后的 fail-closed SQLite 最大值，不能冒充已设好的安全水位。

### Policy stage 3 — capture-only shadow 和真正的无副作用 GET

只把 `production_monitor_incident` 加入
`TELEGRAM_KOL_RUNTIME_INCIDENT_CAPTURE_TYPES`；Telegram 和 Agent selector 仍明确排除它，
安全水位不变。重启主服务后，用认证的本机 GET 读取：

```bash
set +x
builtin printf 'x-monitor-capture-token: %s\n' \
  "$TELEGRAM_KOL_RUNTIME_MONITOR_CAPTURE_TOKEN" \
  | curl --fail --silent --show-error --request GET --header @- \
  http://127.0.0.1:8000/api/runtime-incidents/monitor-v2-bridge-readiness \
  | jq -e '.schema_version == 2 and .contract == "production_monitor_v2" and
    .available == false and .capture_selector == "included" and
    (.notification_channel == "enabled" or .notification_channel == "disabled") and
    .notification_selector == "excluded" and .agent_selector == "excluded" and
    (.agent_channel == "enabled" or .agent_channel == "disabled") and
    .notification_watermark == "configured"'
```

token 只能由 root-owned policy 进入当前 root shell，必须关闭 shell tracing，不得
echo/记录 token。上面用 shell builtin 把 header 通过 stdin 传给 curl，token 不得
出现在 curl argv、临时文件、错误输出或进程列表。这个 endpoint 只验证 contract v2 以及 capture、notification、Agent
selectors，不读写 ledger，不 claim，不投递。禁止用 **empty POST** 或任何
`monitor-capture` POST 当探针：POST 是真正的 incident intake，不是 no-op。

bridge channel health 不得写入 `adapter_failures`，也不得改变异常证据的
completeness、`observed_health` 或已确认新 episode 的 incident projection。Live 运行时，
capture 或 deterministic notification 配置/通道失效必须进入原有 intake 或
notification SLA，超期且 recheck 仍失败时才允许固定 fallback。Agent 只是独立
channel-health 事实，无论 disabled、排队或超时都不得阻断 deterministic intake/
notification。只有 shadow/activation 流程会因 bridge policy 不符合而停止；这是部署
门禁，不是生产异常证据。

此时才运行下一节的 `--shadow-only`。shadow CLI 会用上面的认证 GET 确认
capture-only selector 组合，但绝不调用 POST；它仍强制 `incident_router=None`。运行前后
重新读取水位，必须证明 runtime incident 数量、Telegram notification 水位和 Agent
claim 水位全部不变，且 monitor 类型行仍为 0。

### Policy stage 4 — 先放宽 deterministic notification，Agent 独立处理

只有 capture-only shadow 验收合格，并再次证明没有旧
`production_monitor_incident` 行后，才可把它加入
`TELEGRAM_KOL_RUNTIME_INCIDENT_TELEGRAM_TYPES`，同时保留 stage 2 的 exclusive
Telegram 水位。这是 deterministic intake/notification 闭环的必需步骤。

Agent selector 是独立的。只有 stage 1 基线证明 Runtime Agent 原本已经
active/enabled，并且另外获得 Agent policy 的明确批准后，才可把
`production_monitor_incident` 加入 `TELEGRAM_KOL_RUNTIME_AGENT_TYPES`。如果 Agent 原本
inactive 或 disabled，保持未启用且 selector 显式排除 monitor；不得为了
monitor cutover 启动 Agent，也不得把 selector staging 当作启用 Agent 的授权。

在新的安全窗口内重启 `telegram-kol.service`。只有 Agent 基线为 active
且本次 Agent policy 已单独批准时，才可执行
`systemctl try-restart telegram-kol-runtime-agent.service`；原本 inactive/disabled 的单元
必须保持原状。

此时同一个 GET 必须返回 `schema_version=2`、capture selector `included`、
notification channel `enabled`、notification selector `included`、水位 `configured`，
且 `available=true`。Agent channel 不参与 `available`；它必须继续如实独立
报告。已单独批准并放宽的 Agent 应显示 channel `enabled` 且 selector
`included`；未启用或未批准的 Agent 应保持 `disabled`/`excluded`。Agent 不可用
不得阻断 deterministic cutover 或已单独批准的 live sentinel，也不得阻断
新确认异常进入既有 intake/notification SLA。完成这一步 GET 本身不会
创建 incident；只有后续单独批准启用的 live sentinel 才能 POST 已确认的异常。

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
ExecStart=/opt/telegram-kol-monitor-v2/current/.venv/bin/telegram-kol-research run-production-monitor-sentinel --state-path /var/lib/telegram-kol-monitor-v2/sentinel/shadow-sentinel-v2.json --snapshot-path /var/cache/telegram-kol-monitor-v2/sentinel/snapshot.json --database-path /var/cache/telegram-kol-monitor-v2/sentinel/research-snapshot.db --checkout-path /opt/telegram-kol-monitor-v2/current --settings-url http://127.0.0.1:8000/api/trading-settings --coverage-path /var/cache/telegram-kol-monitor-v2/sentinel/coverage.json --journal-path /var/cache/telegram-kol-monitor-v2/sentinel/journal.json --expected-head ${TELEGRAM_KOL_MONITOR_EXPECTED_HEAD} --expected-auto-trade true --expected-position-limit 4 --expected-management-mode live --expected-preamble-mode live --readiness-url http://127.0.0.1:8000/api/runtime-monitor-readiness --incident-loopback-url http://127.0.0.1:8000/api/runtime-incidents/monitor-capture --shadow-only
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
5. 再 `enable --now telegram-kol-sentinel.timer`；必须先证明认证的只读
   `monitor-v2-bridge-readiness` GET 和四段 policy staging 全部正常，才允许
   Runtime Incident 拥有正常通知。不得用 capture POST 当 no-op 探针。
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
