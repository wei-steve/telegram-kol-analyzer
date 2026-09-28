# 陈哥 BTC 过期重发 / 队头阻塞 / 准入死锁 · 实施状态

- 设计：`docs/plans/2026-09-28-chen-btc-expired-repost-and-queue-block-design.md`，用户裁定见其 §9。
- 分支：`chen-btc-repost-queue-design`，基于 `origin/main` `4ff0d43c`。生产 HEAD 是 `9363a6c8`，两者在 `src/` 下没有差异。
- 状态：**候选已完成，全量测试已跑，未部署**。部署按 L2 执行，由调度会话排期，并须由用户确认。

| 项 | 提交 | 级别 | 状态 |
|---|---|---|---|
| 缺陷 4：重试耗尽的识别失败不再挡住入场 | `38318d17`，并由 `19e40d52`、`d7a8b39d` 收紧 | L2 | 候选 |
| incident 详细摘要白名单 | `38318d17` | L1 | 候选 |
| 缺陷 2（2a、2b）：过期后原样重发按新开仓处理 | `4798d360` | L2 | 候选 |
| 3a：拒收诊断里记下被拒的目标 id | `2e60c628` | L1 | 候选 |
| 3d：取消指向已终态目标时无害结束 | `2e60c628`，并由 `a0b00007` 收紧 | L2 | 候选 |
| 3b：lifecycle id 自动映射成 thread id | — | — | 不做（先看 3a 的观察数据） |
| 3c：契约类失败不重问 | — | — | 并入首次分析四分类阶段 3 |

## 审阅中收紧过的三处

1. **`19e40d52`**：重试已耗尽的挡路消息，如果是发在策略**之后**、同币种同方向的撤销或离场，入场仍然挡住，保持原来的「默认不下单」。子代理顺带发现，worker 的唤醒路径也会绕过这道检查，所以同一道检查在唤醒入口也加了一遍。
2. **`d7a8b39d`**：撤销消息精确指向一条 lifecycle 时，只有这条 lifecycle 还活着（`pending_entry`、`entered`、`holding`）才算指向了别的仓位。如果指向的是已经终态的旧 lifecycle，就按同币种同方向处理，入场继续挡住。本案 19490 就被首轮钉在了已过期的 1327 上。
3. **`a0b00007`**：首轮里只要带有实际策略内容，3d 就不适用，防止「撤掉旧单并重发新单」这类消息把新单一起吞掉。已用生产数据核对：19490 的首轮是「非策略」，`strategy={}`，所以 3d 对真实场景有效。

## 给首次分析四分类那条线（需要对齐的改动点）

- `authoritative_recognition.requires_context_resolution` 的 `apparent_entry_may_be_revision`：重叠只算 `status == "pending_entry"` 的候选（2a）。阶段 3 重写触发判据时必须保留这一条，已写进 `docs/first-pass-classification-status.md` 阶段 3 验收回放项第 3 条（raw 19481）。
- `authoritative_recognition._resolved_mimo_result`：新增终态目标守卫（2b）。首轮是「是策略」、上下文判 `manage_thread` 或 `revise_thread` 且 `action=null`、所有目标都已 `expired`、交易所上没有我方敞口时，保留首轮结论，并写入 `_context_resolution.override_rejected = "terminal_target"`。这是阶段 3 第 ②「降级不再抹平首次分析」的一个窄子集，阶段 3 上线后可以由第 ② 项吸收。
- `authoritative_recognition.assess_message_authoritatively`：捕获上下文失败的 `except` 分支前面，新增了 `except ContextResolutionError` 分支（3d）。满足条件时结果落为 `非策略` + `skipped / target_terminal_noop`，不再是 `authoritative_failed`。阶段 3 第 ③「契约类失败不重问」会改同一处，实施时要把 3d 的判断保留在它前面，或者直接并入它。
- `context_resolution._rejected_response_diagnostic`：遇到 `target_outside_candidate_set` 时，额外记录 `rejected_target_ids` 和 `rejected_ids_matching_candidate_lifecycle`（3a）。

## 测试

- 在最终候选 `a0b00007` 上跑了全量：10190 passed，18 failed，4 skipped。
  - 其中 15 条在 `tests/test_minimal_server_updater.py` 和 `tests/test_server_update_scripts.py`。它们在 `origin/main` 上同样失败，与本线无关，是本地环境导致的旧失败。
  - 另外 3 条是本线新增的 caplog 断言受执行顺序影响：包日志器的 `propagate` 被 `configure_application_logging` 关掉了。已在测试提交里修复，按全量中的顺序复现过：修复前 3 条失败，修复后全部通过。只动了测试文件，生产代码在这次全量之后没有变。
- 回放用例覆盖了 19481（2a、2b）、19490（3a、3d、4a、4b）和 19491（入场放行）。每项都用 `git stash` 核对过：修复前失败，修复后通过。

## 部署要点（L2，待排期）

- `pyproject.toml`、schema、systemd 单元都没有变，不需要 `pip install`，也不需要手动 `cp` 单元文件。
- 回滚 sha 是部署前的生产 HEAD（目前是 `9363a6c8`）。
- 观察窗口 30 分钟，至少 5 条真实消息。重点检查：
  - `target_terminal_noop` 和 `mimo_authoritative_failed_exhausted` 出现时，是否与预期一致；
  - incident 是否已带上详细摘要，日志里是否不再出现 `failed open`；
  - 没有新的入场被错误放行或错误挡住。
- 本次部署**不涉及任何数据改动**。2026-09-28 那次人工 L3 修复的记录，见设计稿 §6。
