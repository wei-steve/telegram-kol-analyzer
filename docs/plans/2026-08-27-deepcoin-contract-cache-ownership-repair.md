# Deepcoin Contract Cache Ownership Repair Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 修复 split runtime 中 `telegram-kol-worker` 无法在 sticky 数据目录原子替换 Deepcoin 合约规格缓存的问题，并在保持 fail-closed、零历史重放和独立生产授权边界的前提下恢复未来自动入场。

**Architecture:** 保留现有缓存路径与原子发布协议，新增固定目标、描述符校验的权限模块和 root `ExecStartPre` helper，将缓存收敛为 `telegram-kol-worker:telegram-kol-runtime 0660`；Runtime Agent sanitizer 按文件类别处理 owner；worker 暴露有界只读健康投影，生产 monitor 将 freshness、owner 漂移和新同步拒绝纳入判断；部署器事务化安装 helper/unit 并支持完整回滚。生产切换分成“本地候选、候选集成、冻结部署、显式恢复”四个独立授权阶段。

**Tech Stack:** Python 3.11、FastAPI、SQLAlchemy/SQLite、pytest、Bash、systemd、POSIX 文件描述符与 ACL、现有 `telegram-kol-update` 治理部署器。

---

## 执行边界

- 本计划只定义实施步骤，不授权推送、部署、重启、生产数据库写入、交易设置写入或交易所写入。
- 每个用户会话只执行一个阶段；阶段状态写入
  `docs/deepcoin-contract-cache-ownership-repair-status.md`。
- 在主目录 `/Users/steven/Documents/telegram获取消息` 实施，不创建或使用
  `runtime-serialization` worktree。
- 禁止 `git add -A`；每次提交都显式列出路径并先检查
  `git diff --cached --name-only`。
- Task 12 在每次冻结前动态记录 `contract_spec_sync_unavailable` 的 exact set；
  集合内每条都必须保持 `verified_refusal` 且
  `attempted_exchange_write=0`，任何阶段都不重放、不补单。
- 运行时发布协议
  `mkstemp -> fsync -> strict reload -> os.replace -> directory fsync`
  不改动。

## 阶段总览

| 阶段 | 权限范围 | 结束条件 |
| --- | --- | --- |
| 1. 本地 RED→GREEN | 本地文件、测试、本地提交 | focused tests 与最终完整套件通过，形成精确候选 SHA |
| 2. 候选集成 | 单独批准后 push | 远端目标分支精确指向候选 SHA；生产未变更 |
| 3. 冻结部署 | 单独批准生产设置写入、部署和一次计划内重启 | `auto_trade=false`，候选已部署，缓存 fresh 且 owner 合同正确，仍未恢复交易 |
| 4. 恢复未来入场 | 所有者再次明确批准 | 只处理恢复水位之后的新信号并完成 L2 观察 |

## 阶段 1：本地 RED→GREEN

### Task 1: 建立唯一状态文件并认领本地阶段

**Files:**

- Create: `docs/deepcoin-contract-cache-ownership-repair-status.md`
- Reference: `docs/plans/2026-08-27-deepcoin-contract-cache-ownership-repair-design.md`
- Reference: `docs/plans/2026-08-27-deepcoin-contract-cache-ownership-repair.md`

**Step 1: 创建最小状态合同**

状态文件至少记录：

```yaml
workflow: deepcoin-contract-cache-ownership-repair
design_status: approved
current_phase: local_red_green
phase_state: claimed
claimed_by: <current Codex task id>
candidate_sha: null
production_sha: null
auto_trade_frozen: false
freeze_raw_message_id: null
restore_raw_message_id: null
historical_replay_allowed: false
```

同时写明：`claimed` 或 `in_progress` 且 owner 不是本会话时立即停止；阶段完成或暂停时必须记录已验证内容与剩余事项。

**Step 2: 校验文档格式**

Run:

```bash
git diff --check -- docs/deepcoin-contract-cache-ownership-repair-status.md
```

Expected: exit 0，无尾随空格。

**Step 3: 提交状态认领**

```bash
git add -- docs/deepcoin-contract-cache-ownership-repair-status.md
git diff --cached --name-only
git commit -m "docs: track Deepcoin cache repair"
```

Expected: cached 文件列表只有状态文件。

### Task 2: 为权限合同编写失败测试

**Files:**

- Create: `tests/test_contract_cache_permissions.py`
- Future implementation: `src/telegram_kol_research/contract_cache_permissions.py`

**Step 1: 先写导入和合同测试**

测试先导入尚未实现的：

```python
from telegram_kol_research.contract_cache_permissions import (
    ContractCachePermissionError,
    converge_contract_cache_permissions,
    inspect_contract_cache_permissions,
)
```

覆盖以下矩阵：

- 目标缺失：`inspect` 返回 `exists=False`，converge 不创建空缓存；
- root-owned 或 worker-owned 单链接普通文件：可收敛到 worker UID、runtime GID、`0660`；
- 未知 owner：拒绝且不改变 inode；
- symlink、目录、FIFO、硬链接数大于 1：拒绝；
- 重复 converge 幂等；
- `--check`/inspect 只读，不改变 owner、mode、mtime 或内容；
- 权限错误只返回有界类别，不回显路径外数据或异常详情。

**Step 2: 写 Linux/root 条件集成测试**

在 `sys.platform == "linux" and os.geteuid() == 0` 时运行真实 sticky 语义：

```python
# parent mode 01777, target owner root
# child process drops to uid/gid 65534
# repair before: child os.replace(...) raises PermissionError
# converge target owner to 65534
# repair after: child os.replace(...) succeeds
```

非 Linux 或非 root 环境明确 `pytest.skip`；不能用 mock 冒充内核 sticky 行为。此测试必须在阶段 3 的生产前临时目录验收中补跑，且不得指向真实缓存。

**Step 3: 运行 RED**

Run:

```bash
pytest -q tests/test_contract_cache_permissions.py
```

Expected: 因 API 尚未实现而失败；记录首个预期失败，不继续堆叠无关错误。

**Step 4: 提交 RED 测试**

```bash
git add -- tests/test_contract_cache_permissions.py
git diff --cached --name-only
git commit -m "test: define contract cache ownership contract"
```

Expected: 只有测试文件进入提交；空实现文件不暂存。

### Task 3: 实现描述符安全的权限模块和固定目标 helper

**Files:**

- Modify: `src/telegram_kol_research/contract_cache_permissions.py`
- Create: `deploy/systemd/telegram-kol-worker-prepare-contract-cache`
- Modify: `tests/test_contract_cache_permissions.py`
- Modify: `tests/test_runtime_role_selection.py`

**Step 1: 实现纯权限模块**

模块提供两个边界：

```python
def inspect_contract_cache_permissions(
    path: Path,
    *,
    worker_uid: int,
    runtime_gid: int,
    agent_user: str,
) -> ContractCachePermissionStatus: ...

def converge_contract_cache_permissions(
    path: Path,
    *,
    worker_uid: int,
    runtime_gid: int,
    agent_user: str,
) -> ContractCachePermissionStatus: ...
```

实现要求：

- 父目录和目标都用 `O_NOFOLLOW | O_CLOEXEC`；目标允许缺失；
- `fstat` 验证 regular file、`st_nlink == 1`；
- 只接受 `st_uid in {0, worker_uid}`；
- 通过 fd 执行 `fchown`、`fchmod`；ACL 只设置
  `telegram-kol-agent:---`，不递归；
- converge 后重新 `fstat` 与读取 ACL，任何字段不一致均失败；
- 缺失目标不创建文件，让 worker 的首次成功原子发布自然创建 inode；
- 结果只包含布尔合同、允许枚举和必要时间，不含缓存内容。

**Step 2: 实现固定生产 helper**

`deploy/systemd/telegram-kol-worker-prepare-contract-cache`：

- 必须 root 运行；
- 固定目标为
  `/opt/telegram-kol-analyzer/data/deepcoin_contract_specs_cache.json`；
- 固定解析 `telegram-kol-worker`、`telegram-kol-runtime`、
  `telegram-kol-agent`；
- 只接受无参数或 `--check`，拒绝任意路径参数；
- 无参数执行 converge，`--check` 只读；
- 成功 exit 0，合同不满足或未知状态 exit non-zero。

**Step 3: 让测试转 GREEN**

Run:

```bash
pytest -q tests/test_contract_cache_permissions.py tests/test_runtime_role_selection.py
```

Expected: macOS 本地的内核跨 UID 测试被明确 skip，其余全部通过。

**Step 4: 静态验证 helper**

```bash
python3 -m py_compile \
  src/telegram_kol_research/contract_cache_permissions.py \
  deploy/systemd/telegram-kol-worker-prepare-contract-cache
git diff --check -- \
  src/telegram_kol_research/contract_cache_permissions.py \
  deploy/systemd/telegram-kol-worker-prepare-contract-cache \
  tests/test_contract_cache_permissions.py \
  tests/test_runtime_role_selection.py
```

Expected: 均 exit 0。

**Step 5: 提交权限模块**

```bash
git add -- \
  src/telegram_kol_research/contract_cache_permissions.py \
  deploy/systemd/telegram-kol-worker-prepare-contract-cache \
  tests/test_contract_cache_permissions.py \
  tests/test_runtime_role_selection.py
git diff --cached --name-only
git commit -m "fix: enforce worker ownership for contract cache"
```

### Task 4: 修正 Runtime Agent sanitizer 的文件分类

**Files:**

- Modify: `deploy/systemd/telegram-kol-runtime-agent-prepare-db-acl`
- Modify: `tests/test_runtime_role_selection.py`
- Modify: `scripts/install_runtime_agent_sidecar.sh`

**Step 1: 写 sanitizer 回归 RED**

测试证明当前统一逻辑会执行：

```python
os.fchown(cache_fd, 0, runtime_gid)
```

新增期望：

- contract cache 调用 worker-owned 收敛函数；
- session、session lock 与 sidecar 仍为 root/shared；
- SQLite 仍走 Runtime Agent 专用 ACL；
- `--sanitize-non-db` 重跑后缓存 owner 不回到 root；
- unknown owner、symlink、hardlink fail closed；
- 不递归接触备份、probe 或其他 data 文件。

Run:

```bash
pytest -q tests/test_runtime_role_selection.py -k 'cache or acl or sanitizer'
```

Expected: 新断言先失败。

**Step 2: 拆分文件集合与 fd helper**

将原 `SHARED_RUNTIME_DATA_FILES` 拆成：

```python
WORKER_OWNED_RUNTIME_FILES = {"deepcoin_contract_specs_cache.json"}
ROOT_SHARED_RUNTIME_FILES = {<现有 session 与 lock 文件>}
```

缓存路径复用 Task 3 的权限模块；session 保留现有 root owner 语义。安装脚本的 sticky bit 和 Agent 拒绝 ACL 保持不变。

**Step 3: 运行 GREEN**

```bash
pytest -q tests/test_runtime_role_selection.py
bash -n scripts/install_runtime_agent_sidecar.sh
python3 -m py_compile deploy/systemd/telegram-kol-runtime-agent-prepare-db-acl
```

Expected: 全部通过。

**Step 4: 提交 sanitizer 修复**

```bash
git add -- \
  deploy/systemd/telegram-kol-runtime-agent-prepare-db-acl \
  scripts/install_runtime_agent_sidecar.sh \
  tests/test_runtime_role_selection.py
git diff --cached --name-only
git commit -m "fix: preserve worker-owned contract cache in sanitizer"
```

### Task 5: 将 helper 接入 worker unit，并让部署器事务化安装与回滚

**Files:**

- Modify: `deploy/systemd/telegram-kol-worker.service`
- Modify: `deploy/telegram-kol-update`
- Modify: `scripts/bootstrap_server_updater.sh`
- Modify: `scripts/server_git_update.sh`
- Modify: `scripts/server_git_update.ps1`
- Modify: `tests/test_runtime_role_selection.py`
- Modify: `tests/test_server_update_scripts.py`
- Modify: `tests/test_minimal_server_updater.py`

**Step 1: 写 unit/updater RED**

先断言：

- worker unit 在 `ExecStart` 前存在唯一
  `ExecStartPre=/usr/local/libexec/telegram-kol-worker-prepare-contract-cache`；
- updater 在 split topology 停止、checkout、pip install 后，worker 启动前：
  1. 备份现有 helper 与 worker unit；
  2. 从 staged exact SHA 安装 helper `0755 root:root`；
  3. 安装 worker unit `0644 root:root`；
  4. `systemctl daemon-reload`；
- monolith topology 不擅自迁移真实缓存 owner；
- helper/unit 安装失败、daemon-reload 失败或 worker `ExecStartPre` 失败时：
  恢复旧 helper/unit、再次 daemon-reload、恢复旧代码与旧服务；
- 之前不存在的 artifact 在失败回滚时只删除该精确目标；
- 成功路径顺序为
  `second-active-check < checkout < pip < artifact-install < daemon-reload < worker-start < health`。

Run:

```bash
pytest -q \
  tests/test_runtime_role_selection.py \
  tests/test_server_update_scripts.py \
  tests/test_minimal_server_updater.py
```

Expected: 新顺序与回滚测试先失败。

**Step 2: 实现受控 artifact transaction**

在 `deploy/telegram-kol-update` 增加固定目的地：

```bash
WORKER_CACHE_HELPER_PATH=/usr/local/libexec/telegram-kol-worker-prepare-contract-cache
WORKER_UNIT_PATH=/etc/systemd/system/telegram-kol-worker.service
```

仅 test mode 允许显式临时路径覆盖；生产路径不可由未验证环境变量重定向。备份放在已验证的 `$STAGE_PARENT` 临时文件中，权限收紧，cleanup 成功后删除。回滚发生在旧服务启动之前。

**Step 3: 更新 updater bootstrap 合同**

bootstrap 与 workstation helper 必须验证候选 updater 同时包含：

- dual topology 合同；
- worker cache artifact transaction 合同；
- exact SHA 与现有 SHA256 传输校验。

不得增加 operator override、force checkout 或模糊分支部署。

**Step 4: 运行 GREEN 和语法检查**

```bash
pytest -q \
  tests/test_runtime_role_selection.py \
  tests/test_server_update_scripts.py \
  tests/test_minimal_server_updater.py
bash -n deploy/telegram-kol-update
bash -n scripts/bootstrap_server_updater.sh
bash -n scripts/server_git_update.sh
```

Expected: 全部通过，成功/失败事件顺序与断言一致。

**Step 5: 提交部署事务**

```bash
git add -- \
  deploy/systemd/telegram-kol-worker.service \
  deploy/telegram-kol-update \
  scripts/bootstrap_server_updater.sh \
  scripts/server_git_update.sh \
  scripts/server_git_update.ps1 \
  tests/test_runtime_role_selection.py \
  tests/test_server_update_scripts.py \
  tests/test_minimal_server_updater.py
git diff --cached --name-only
git commit -m "fix: deploy worker cache ownership transactionally"
```

### Task 6: 增加 worker-owned 合约规格健康投影

**Files:**

- Modify: `src/telegram_kol_research/deepcoin_contract_spec_cache.py`
- Modify: `src/telegram_kol_research/web_app.py`
- Modify: `tests/test_deepcoin_contract_spec_cache.py`
- Modify: `tests/test_web_app.py`

**Step 1: 写投影 RED**

为 `GET /api/runtime-incidents/contract-spec-health` 写测试：

- 仅 loopback 且正确 `x-monitor-capture-token` 可读；其他来源返回 404；
- 只在 worker role 提供；web/ingest 不提供或返回 404；
- 固定 schema：

```json
{
  "schema_version": 1,
  "state": "fresh",
  "fetched_at": "2026-08-27T00:00:00Z",
  "expires_at": "2026-08-27T12:00:00Z",
  "last_success_at": "2026-08-27T00:00:00Z",
  "last_refresh_succeeded": true,
  "error_category": null,
  "ownership_contract_satisfied": true
}
```

- `state` 仅 `fresh|stale|unavailable`；
- 错误仅允许例如 `refresh_timeout`、`permission_denied`、
  `validation_failed`、`transport_failed`、`unknown`，禁止原始异常文本；
- 不含原始规格、数量、API URL、凭据、签名、响应体或绝对路径；
- fresh 但最近 refresh 失败时保持 `state=fresh`，同时暴露有界预警；
- owner/group/mode/type/link/ACL 任一不符时
  `ownership_contract_satisfied=false`。

**Step 2: 扩展 orchestrator 状态**

保留现有状态语义，只补 `fetched_at`、`last_refresh_succeeded` 与错误类别归一化；禁止把 refresh 失败自动替换成静态规格。

**Step 3: 实现受保护 endpoint**

复用现有 `require_monitor_capture_auth`，从
`app.state.contract_spec_refresh_orchestrator` 和只读权限 inspection 生成投影。
缺失 orchestrator 或任何读取异常返回 schema 完整的 `unavailable`，不抛出敏感详情。

**Step 4: 运行 GREEN**

```bash
pytest -q tests/test_deepcoin_contract_spec_cache.py
pytest -q tests/test_web_app.py -k 'contract_spec or monitor_capture_auth'
```

Expected: 全部选中测试通过。

**Step 5: 提交投影**

```bash
git add -- \
  src/telegram_kol_research/deepcoin_contract_spec_cache.py \
  src/telegram_kol_research/web_app.py \
  tests/test_deepcoin_contract_spec_cache.py \
  tests/test_web_app.py
git diff --cached --name-only
git commit -m "feat: project bounded contract cache health"
```

### Task 7: 将缓存健康和新同步拒绝接入 monitor

**Files:**

- Modify: `src/telegram_kol_research/production_safety_monitor.py`
- Modify: `src/telegram_kol_research/cli.py`
- Modify: `deploy/systemd/telegram-kol-monitor.service`
- Modify: `tests/test_production_safety_monitor.py`
- Modify: `tests/test_server_monitor_installation.py`
- Modify: `tests/test_runtime_role_selection.py`

**Step 1: 写 monitor RED**

增加 `contract_specs` adapter，并覆盖：

- URL 只允许精确
  `http://127.0.0.1:8002/api/runtime-incidents/contract-spec-health`，
  禁止 query、其他 host/port/scheme；
- 使用 monitor token，超时、非 2xx、重复 JSON key、额外字段、类型错误均
  `adapter_failure`；
- 只读 DB 查询：

```sql
SELECT COUNT(*)
FROM instruction_execution_contracts
WHERE reason_code = 'contract_spec_sync_unavailable'
  AND terminal_at >= :since
```

  查询不完整为 unknown，不得按 0 处理；
- `actual auto_trade=true` 且 `deepcoin_contract_specs_mode=live`：
  非 fresh、owner 合同失败或新拒绝数大于 0 -> unhealthy；
- `actual auto_trade=false`：同样信息写入
  `details.restore_ready=false`，但不产生交易安全事故 reason；
- fresh 但最近 refresh 失败 -> 预警 reason，不改变可交易 snapshot；
- static/shadow 不把 live 缓存状态误判为当前交易阻断；
- capture/projection 的 adapter 闭集同步接受 `contract_specs`。

**Step 2: 扩展 adapter、snapshot 与 evaluator**

新增：

```python
contract_spec_health: Mapping[str, Any] | None
contract_spec_sync_refusal_count: int | None
```

`run_production_safety_monitor` 必须以同一个 `since` 水位采集 DB 拒绝；任一来源不完整时把 `contract_specs` 放入 adapter failures。reason code 使用固定枚举，例如：

```text
contract_spec_unavailable
contract_spec_ownership_drift
contract_spec_sync_refusal_detected
contract_spec_refresh_warning
```

**Step 3: 接入 CLI 与 systemd URL**

增加 `--contract-spec-health-url`，unit 固定指向 worker 8002；不得给 monitor 读取缓存文件或 Deepcoin 凭据的权限。

**Step 4: 运行 GREEN**

```bash
pytest -q \
  tests/test_production_safety_monitor.py \
  tests/test_server_monitor_installation.py \
  tests/test_runtime_role_selection.py
```

Expected: evaluator、HTTP allowlist、capture 闭集与 unit 参数全部通过。

**Step 5: 提交 monitor 规则**

```bash
git add -- \
  src/telegram_kol_research/production_safety_monitor.py \
  src/telegram_kol_research/cli.py \
  deploy/systemd/telegram-kol-monitor.service \
  tests/test_production_safety_monitor.py \
  tests/test_server_monitor_installation.py \
  tests/test_runtime_role_selection.py
git diff --cached --name-only
git commit -m "feat: monitor contract cache readiness"
```

### Task 8: 让冻结状态成为受治理的 monitor 期望

**Files:**

- Modify: `scripts/install_server_monitor.sh`
- Modify: `deploy/systemd/telegram-kol-monitor.service`
- Modify: `deploy/telegram-kol-update`
- Modify: `scripts/bootstrap_server_updater.sh`
- Modify: `scripts/server_git_update.sh`
- Modify: `scripts/server_git_update.ps1`
- Modify: `tests/test_server_monitor_installation.py`
- Modify: `tests/test_server_update_scripts.py`
- Modify: `tests/test_minimal_server_updater.py`

**Step 1: 写冻结期望 RED**

安装器新增必填：

```text
--expected-auto-trade-state enabled|disabled
```

测试要求：

- 只接受这两个精确值；
- 映射成 root-owned `0600` env 中唯一一行：
  `TELEGRAM_KOL_MONITOR_EXPECTED_AUTO_TRADE_OPTION=`，值只能是
  `--expected-auto-trade-enabled` 或 `--no-expected-auto-trade-enabled`；
- systemd unit 只把该值作为一个 CLI 参数展开；
- updater 校验该行唯一且在白名单内，更新 expected HEAD 时保留它；
- updater 可接收必填、白名单化的
  `EXPECTED_AUTO_TRADE_STATE=enabled|disabled`，并与 expected HEAD 同一原子 env
  transaction 更新；
- shell workstation helper 要求显式环境变量；PowerShell helper 使用必填
  `ValidateSet("enabled", "disabled")` 参数；二者只把已验证值传给 updater；
- bootstrap 验证 durable updater 已包含 auto-trade expectation transaction 合同；
- updater 事务化安装 candidate monitor service unit 并 daemon-reload；失败恢复旧 unit 与 env；
- 自动入场设置本身不由 updater 修改；
- rollback 不把 `auto_trade` 改回 true。

**Step 2: 实现安装器与 unit**

安装器内部先将 state 映射成固定 option，再写 env。禁止把用户原始字符串直接注入 `ExecStart`。

**Step 3: 实现 updater monitor transaction**

顺序要求：

```text
capture timer state
  -> stop timer and wait oneshots
  -> atomically pin previous HEAD + requested auto-trade expectation
  -> runtime stop/check/checkout/install
  -> install candidate monitor unit + daemon-reload
  -> pin candidate HEAD, preserve requested expectation
  -> restore prior timer enabled/active state
```

失败时恢复 previous HEAD、旧 monitor unit、旧 env 与原 timer 状态；交易设置保持操作者已经写入的 false。

**Step 4: 运行 GREEN**

```bash
pytest -q \
  tests/test_server_monitor_installation.py \
  tests/test_server_update_scripts.py \
  tests/test_minimal_server_updater.py
bash -n scripts/install_server_monitor.sh
bash -n deploy/telegram-kol-update
bash -n scripts/bootstrap_server_updater.sh
bash -n scripts/server_git_update.sh
```

Expected: 全部通过，测试日志不输出 env 内秘密。

**Step 5: 提交冻结监控合同**

```bash
git add -- \
  scripts/install_server_monitor.sh \
  deploy/systemd/telegram-kol-monitor.service \
  deploy/telegram-kol-update \
  scripts/bootstrap_server_updater.sh \
  scripts/server_git_update.sh \
  scripts/server_git_update.ps1 \
  tests/test_server_monitor_installation.py \
  tests/test_server_update_scripts.py \
  tests/test_minimal_server_updater.py
git diff --cached --name-only
git commit -m "fix: govern monitor expectations during trade freeze"
```

### Task 9: 更新部署文档与受控恢复 runbook

**Files:**

- Modify: `docs/server-deployment.md`
- Create: `docs/runbooks/deepcoin-contract-cache-ownership-repair.md`
- Modify: `docs/deepcoin-contract-cache-ownership-repair-status.md`
- Modify: `tests/test_server_update_scripts.py`
- Modify: `tests/test_runtime_role_selection.py`

**Step 1: 写文档合同 RED**

静态测试要求文档明确包含：

- 精确 owner/group/mode/ACL/type/link 合同；
- sticky `1777` 下组写权限不能替代目标 owner；
- 只用固定 helper，不递归 `chown/chmod`；
- monolith 停止且 active-write 为 0 后才迁移；
- 冻结前/部署后/恢复前门禁；
- `EXPECTED_AUTO_TRADE_STATE=disabled` 与恢复时 `enabled`；
- 冻结时动态记录的历史拒绝 exact set 永不重放；
- 代码回滚不自动恢复交易设置；
- 原始 JSON、交易所明细和长日志只写 server evidence 文件。

**Step 2: 写 runbook**

runbook 分成四段：只读 preflight、冻结写入、exact-SHA 部署与权限/刷新验证、单独恢复。每个 mutation 前列出授权要求和失败后的安全终态。

**Step 3: 更新状态为本地实现完成待全量验证**

记录 focused test 命令与结果，`phase_state` 暂保持 `in_progress`，直到 Task 10 完整套件通过。

**Step 4: 运行静态检查并提交**

```bash
pytest -q tests/test_server_update_scripts.py tests/test_runtime_role_selection.py
git diff --check -- \
  docs/server-deployment.md \
  docs/runbooks/deepcoin-contract-cache-ownership-repair.md \
  docs/deepcoin-contract-cache-ownership-repair-status.md
git add -- \
  docs/server-deployment.md \
  docs/runbooks/deepcoin-contract-cache-ownership-repair.md \
  docs/deepcoin-contract-cache-ownership-repair-status.md \
  tests/test_server_update_scripts.py \
  tests/test_runtime_role_selection.py
git diff --cached --name-only
git commit -m "docs: add controlled contract cache recovery runbook"
```

### Task 10: 形成最终本地候选

**Files:**

- Modify: `docs/deepcoin-contract-cache-ownership-repair-status.md`

**Step 1: 运行最终 focused 集合**

```bash
pytest -q \
  tests/test_contract_cache_permissions.py \
  tests/test_deepcoin_contract_spec_cache.py \
  tests/test_runtime_role_selection.py \
  tests/test_server_monitor_installation.py \
  tests/test_server_update_scripts.py \
  tests/test_minimal_server_updater.py \
  tests/test_production_safety_monitor.py \
  tests/test_web_app.py
```

Expected: 全部通过；仅 Linux/root sticky 内核测试在 macOS 明确 skip。

**Step 2: 运行一次且仅一次最终完整套件**

```bash
pytest -q
```

Expected: 全部通过。若之后修改任何 production code，先重跑受影响 focused tests，再重跑一次完整套件；只改文档无需重跑完整套件。

**Step 3: 完整候选静态门禁**

```bash
git diff --check
git status --short
git log --oneline --decorate -10
```

Expected: 没有未解释的文件；不包含其他会话改动。

**Step 4: 更新状态并提交候选 SHA**

先取得代码候选：

```bash
git rev-parse HEAD
```

把完整 SHA、focused/full-suite 结果、Linux/root 待生产前补跑项写入状态文件，设置：

```yaml
current_phase: candidate_integration
phase_state: planned
candidate_sha: <40-char SHA>
```

然后：

```bash
git add -- docs/deepcoin-contract-cache-ownership-repair-status.md
git diff --cached --name-only
git commit -m "docs: hand off Deepcoin cache repair candidate"
git rev-parse HEAD
```

最终用于集成和部署的是最后一个状态提交 SHA；状态文件必须更新为该最终 SHA 时，使用一次只改状态文件的补充提交并记录“candidate content SHA”和“handoff SHA”，避免自引用歧义。

## 阶段 2：候选集成（独立会话、独立批准）

### Task 11: 审查并非强制推送精确候选

**Step 1: 重新验证本地权威路径、branch、HEAD 和工作树**

```bash
pwd -P
git branch --show-current
git rev-parse HEAD
git status --short
git diff <approved-base-sha>..<candidate-sha> --stat
```

Expected: 路径、branch、候选 SHA 与状态文件完全一致，工作树干净；任一不符立即停止，不自行修复。

**Step 2: 做代码审查**

重点审查：descriptor/ACL TOCTOU、unknown owner fail-closed、updater rollback、monitor secret redaction、冻结期望与历史重放边界。

**Step 3: 经明确批准后 push**

```bash
git push origin HEAD:codex/deepcoin-auto-trading-v1
git ls-remote origin refs/heads/codex/deepcoin-auto-trading-v1
```

Expected: 远端精确为批准的 40 位 SHA；禁止 force push。生产状态不变。

## 阶段 3：冻结状态下生产部署（独立会话、独立生产授权）

### Task 12: 冻结前只读门禁

将长输出保存到 server evidence 文件，只回传摘要。证明：

- 生产 checkout/branch/HEAD/dirty tree 与 monitor expected HEAD；
- split 三服务 active、monolith inactive、PID 与 `NRestarts`；
- Telegram session 仅 ingest 持有；
- SQLite `PRAGMA quick_check=ok`、WAL 状态；
- active exchange write、claimed job、active management、worker command、revision claim 全为 0；
- Deepcoin 当前 position、pending regular order、pending trigger/TPSL 查询完整，
  且所有 active row 可唯一归因；schema-valid 的 100-row history/fills 上限只记为
  有界历史覆盖，除非 active row 需要窗口外证据，否则不作为缓存迁移 blocker；
- 当前设置原值与 `MAX(raw_messages.id)`；
- Linux/root 临时目录 sticky 集成测试通过；
- helper `--check` 必须通过完整候选合同，或只报告已识别可迁移旧版漂移：固定
  regular single-link 目标的 group/mode 正确，且 root owner 与缺失的 Agent deny ACL
  是全部差异。unknown owner/type/link/group/mode/ACL、父目录或 entry-binding 异常
  仍 fail-closed；
- contract-spec health 已存在时必须 HTTP 200 且 schema 完整。只有生产仍为已核验
  previous SHA、closed legacy monitor env 通过且端点返回 HTTP 404 时，才可记为
  `legacy_capability_absent`；401/403、timeout、非 404 HTTP 错误和 malformed schema
  均阻断。

任一外部查询不完整只允许一次有理由的重试；仍不完整则停止并保持生产未修改。
版本感知只允许候选 updater 已覆盖且可回滚的已知旧版差异，不豁免不可迁移门禁。

### Task 13: 冻结、部署并保持关闭

获得明确生产授权后：

1. 停止 monitor timer，等待全部 monitor oneshot inactive，但不 disable；
2. 将全局 `auto_trade_enabled` 写为 false，立即读回验证；
3. 记录冻结 `raw_message_id` 水位和 terminal zero-write refusal exact set，等待所有
   在途执行归零；
4. 用批准的 exact SHA 和
   `EXPECTED_AUTO_TRADE_STATE=disabled` 调用现有 workstation helper；
5. updater 在 worker 启动前安装 helper/unit，`ExecStartPre` 收敛缓存 owner；
6. 候选部署成功后验证 monitor env 的 expected HEAD 和 disabled expectation，再启动 timer；
7. 不恢复 `auto_trade`。

失败终态：自动入场保持 false；代码若已变更则 updater 回滚到 previous SHA；monitor unit/env 恢复。若旧 unit 无法表达 disabled expectation，timer 保持 inactive，并用旧 CLI 的
`--no-expected-auto-trade-enabled` 做一次手工只读 monitor 检查，明确报告该暂态，不擅自恢复交易。

### Task 14: 缓存、门禁和冻结观察验收

验证：

- helper `--check` exit 0；目标 regular、nlink 1、worker/runtime、`0660`、Agent deny；
- 旧版 `legacy_capability_absent` 不再允许：authenticated contract-spec health 必须
  HTTP 200、schema 完整并满足完整候选合同；
- worker 后台 refresh 成功，缓存 fresh、TTL/摘要/严格规格边界成立；
- Deepcoin 产品查询完整且与缓存摘要输入一致；
- parent 仍 `1777`，session/DB/backup 权限未漂移；
- sanitizer 重跑后 helper `--check` 仍通过；
- 一次计划内 worker restart 后新 inode 仍 worker-owned，PID/NRestarts 稳定；
- 冻结水位之后没有新增 `contract_spec_sync_unavailable`；
- monitor 报 `restore_ready=true`，但实际设置仍 false；
- 队列无 missing/orphan/duplicate/stuck，交易所零意外写入。

按 L2 观察 30 分钟且至少 5 条自然消息，尽量 2 个群；30 分钟内不足 5 条就停止，不无限延长，状态保持 `in_progress` 并记录有限流量。

## 阶段 4：显式恢复未来自动入场（独立会话、所有者再次批准）

### Task 15: 恢复前重跑全部只读门禁

重新证明 Task 12 与 Task 14 的关键门禁，额外确认：

- 生产 exact SHA 未漂移；
- 缓存 fresh 且最近 refresh 成功；
- owner 合同通过；
- 冻结水位后同步拒绝数为 0；
- Deepcoin 仓位/委托/trigger/TPSL 与冻结基线可解释；
- active-write/queue/management/command/revision claim 为 0；
- 冻结时动态记录的 refusal exact set 仍全部为 terminal verified refusal 且
  `attempted_exchange_write=0`，集合内没有 replay、backfill 或 resubmit。

### Task 16: 只恢复未来新信号

获得所有者明确批准后：

1. 停止 monitor timer 并等待 oneshot inactive；
2. 记录恢复 `raw_message_id` 水位；
3. 将 monitor expectation 事务化改为 enabled；
4. 恢复 `auto_trade_enabled` 的冻结前原值并立即读回；
5. 启动 monitor timer；
6. 只允许恢复水位之后的新信号进入自动交易路径；
7. 不查询或重投历史拒绝作为待执行项。

执行 L2 观察：30 分钟且至少 5 条自然消息，尽量 2 个群；检查 worker/Web/ingest、队列、同步拒绝、SQLite、未知交易结果和直接 exchange history。若任何门禁失败，立即再次冻结为 false；这项写入需要本阶段已批准的安全回滚权限。

### Task 17: 完成状态交接

状态文件记录：生产 SHA、冻结与恢复水位、观察窗口、消息数/群数、monitor 模式、异常、server evidence 路径，以及“历史拒绝零重放、零补单”。仅文档更新不要求再次跑完整测试套件。

## 最终成功条件

只有同时满足以下条件才可把 workflow 标为 complete：

1. worker 持续通过原子替换发布 fresh 缓存；
2. restart、sanitizer 和 split/monolith 转换不再导致 owner 漂移；
3. live + auto trade 时 stale/unavailable/owner drift/new refusal 会被 monitor 捕获；
4. freeze 时同一异常只降低 `restore_ready`，不误报为交易开启状态事故；
5. 未来新信号恢复正常规格检查；
6. 冻结时动态记录的历史拒绝 exact set 保持原终态，零重放、零补单；
7. 没有改变 symbol、TTL、下单计算、识别权威、队列权威或 exchange-write 归属语义。
