# 阶段 5b：Deepcoin 读限流与 401/50000 识别（L2）

状态文件：`docs/rest-ws-trading-status.md`。先领取。分支 `rest-ws/phase-5b-rate-limiter`，工作树 `.worktrees/rest-ws-phase-5b`。

## 问题（阶段 5 会话实测）

间歇 401 是限流：响应体 `{"code":"50000","msg":"Trigger the api frequency limiting"}`，头部
`X-Ratelimit-Limit: 5 / Remaining: 0 / Window: 1s / Retry-After: 1`。`50000` 不在官方错误码表里。
今日 129 次，全部是生产自己的并发调用用光 1 秒 5 次配额。现有代码把它当普通读失败（语义正确，不降级为零），
但没有识别、不尊重 `Retry-After`、同一轮 reconcile 会重复读同一个 pending 快照。

## 任务

1. 客户端：识别 `401 + code 50000` 为 `DeepcoinRateLimited`（`DeepcoinClientError` 子类，带 `retry_after`）。
   **只对 GET** 做一次有界重试（等待 `Retry-After`，上限 2 秒，最多 1 次）；POST 一律不重试，沿用
   `DeepcoinRequestOutcomeUnknown` 语义。
2. 读限流器：照 `DeepcoinTpslWriteLimiter` 的模式加进程内读限流（令牌桶，5/s，留 1 个余量给写入），
   所有 GET 经过它。三进程各自独立限流，因此把每进程配额设为 2/s，并在 ARCHITECTURE.md 记录理由。
   **限流器按物理 HTTP 请求计数，不按逻辑调用计数**：`list_open_orders` 切 V2 后一次逻辑读会展开成
   N 页 N 次请求（limit=100，每满一页多一次），每一页都要从令牌桶取一个令牌；否则限流器会低估真实速率
   （阶段 5a 裁定第 4 条）。
3. 合并同轮重复读：worker 的一轮 `deepcoin_reconcile` 内，同一 `instId` 的 `positions` / `trigger-orders-pending` /
   `orders-pending` 只读一次（轮内缓存，轮结束即失效；不跨轮缓存）。
4. 健康端点加 `rate_limited_last_hour`、`retry_after_waits_last_hour`。

## 禁止

- 不对 POST 重试。不跨轮缓存交易所状态。不用 `git add -A`。

## 验证（L2）

- focused：50000 识别、GET 单次重试、POST 不重试、令牌桶、轮内缓存失效。全量 0 failed。
- 部署 tg-deploy，记录回滚 SHA。观察一个 ≥5 条真实消息的 30 分钟健康窗口；`rate_limited_last_hour` 应显著低于
  部署前基线（今日 129 次/天），reconcile 结论与部署前一致。

## 完成条件

更新状态文件：`current_phase: 5`（回到阶段 5 本体，`phase_status: in_progress`，由原分支 `rest-ws/phase-5-order-entry` rebase 后继续）；
`send_message` 给 `brain_session_id`。
