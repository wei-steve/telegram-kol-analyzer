"""历史数据修复，已执行完毕，保留仅为可追溯；不得被在线路径 import。

这个子包里的每个模块都是一次性的历史数据修复工具：它们要修的那批数据已经处置完毕，
文档里有对应的完成记录，代码上也已经没有任何在线引用。保留它们只是为了让当时的判定
口径、指纹和 CAS 门可追溯，不是为了再次运行。

- 在线路径（`web_app.py` 的 lifespan 任务、`RUNTIME_ROLE_SINGLETON_TASKS` 的各个 loop、
  `message_processing_worker.py`、`strategy_management_worker.py`）**不得** import 本子包，
  由 `tests/test_one_off_isolation.py` 静态守护，只有 `cli.py` 例外。
- 新的一次性修复工具不要直接放进来。先按 `docs/plans/2026-09-06-post-migration-cleanup/
  step-5-inventory.md` 的口径确认它确实执行完毕、且没有在线引用，再移动过来。
- 反过来，还没在生产 apply 过的工具（例如
  `batch150_management_terminalization`）属于在建工作，**不属于**这里。
"""
