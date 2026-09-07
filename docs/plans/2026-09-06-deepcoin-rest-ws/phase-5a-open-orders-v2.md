# 阶段 5a：`list_open_orders` 切到 V2 `/trade/v2/orders-pending`（L3，会激活从未触发的撤单路径，需用户单独批准）

状态文件：`docs/rest-ws-trading-status.md`。先领取；证据区须有本阶段批准记录。分支 `rest-ws/phase-5a-open-orders-v2`，工作树 `.worktrees/rest-ws-phase-5a`。
阶段 5 的迁移本体在此之前不得部署。

## 问题（阶段 5 会话实测）

`DeepcoinRestClient.list_open_orders` 调的 V1 `/deepcoin/trade/orders-pending` 对活着的普通限价单**返回空**；
V2 `/deepcoin/trade/v2/orders-pending` 三种参数（全量 / 按 instId / 按精确 ordId）全部命中。V1 无文档页，官方侧边栏
「获取未成交订单列表」指向 V2。生产至今没有普通限价挂单（限价腿走 trigger-order），所以 V1 恒返回空一直没暴露。
阶段 5 把限价入场改为普通 order 后，`list_open_orders` 的 16 个调用点会对新入场单读到「没有挂单」。

同时，切到 V2 后这些调用点第一次会真的看到挂单，其中 `terminal_entry_cleanup` 等路径会开始撤它以前看不见的单。
这是交易所写入后果，必须逐调用点分析。

## 任务

1. 客户端：`list_open_orders` 改调 V2，实现分页（按官方文档的分页参数，直到返回不足一页），任何一页失败即抛
   `DeepcoinClientError`，**不返回部分结果**（不完整 = 未知，不是空）。响应字段与 V1 的差异做显式映射并写测试。
2. 逐调用点分析：对 16 个 `list_open_orders(` 调用点各写一行：调用方、用途（只读展示 / 判定 / 触发写入）、
   切 V2 后行为变化、是否可能对**当前生产已有对象**产生写入。写进 `docs/plans/2026-09-06-deepcoin-rest-ws/phase-5a-callsites.md`。
   任何"可能撤销或修改现有对象"的调用点，本阶段先加显式护栏：只允许对 `execution_order_legs` 里
   `order_kind` 为普通 order 且由本系统提交的 ordId 动作；其他对象一律只记录不动作。
3. 部署前只读：用 V2 拉一次生产当前全部挂单，确认为空（当前生产没有普通限价挂单）或逐条能归属；不能归属的对象
   写进证据并出示给用户，不得由本阶段撤它。
4. 与阶段 2 的合约名映射、阶段 5 的 `unknown_exchange_outcome` 语义一致。

## 禁止

- 不返回部分分页结果。不对任何非本系统提交的对象做写入。不改阶段 5 迁移本体。不用 `git add -A`。

## 验证（L3 按写入语义）

- focused：分页、失败即抛、字段映射、护栏。全量 0 failed。
- 部署 tg-deploy，记录回滚 SHA。观察一个 ≥5 条真实消息的 30 分钟健康窗口（后台监视器持续观察直到凑够）；
  交易所直读确认窗口内零撤单/零非预期写入；`list_open_orders` 相关日志无异常。

## 完成条件

更新状态文件：`current_phase: 5b`、`current_phase_file: docs/plans/2026-09-06-deepcoin-rest-ws/phase-5b-rate-limiter.md`；
`send_message` 给 `brain_session_id`：分支、SHA、部署与回滚 SHA、调用点表摘要、护栏命中记录、窗口结论。
