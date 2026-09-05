# 保护订单方向语义修复：实现、验证与影响边界

## 结论

本地修复把 TPSL 的持仓方向和订单买卖方向分开检查：**long + sell、short + buy 正常；long + buy、short + sell 拒绝**。没有简单删除 side，也没有用 side 猜归属。leg 583 的原始 `posSide=long / side=sell / sz=0` 主止损行在本地真实匹配器中可通过，`exact_owned_stop_evidence_fingerprint` 返回非空。其他身份、价格、数量、快照与所有权前置仍必须通过。

**不部署、不自动补挂。** 本报告证明本地候选纠正了已实证断点，不代表生产任务已 ready、止盈已提交或当前仓位保护已改变。当前活仓与历史处置仍归所有者另行授权。

分支 `codex/protection-order-side-semantics`，隔离 worktree `.worktrees/protection-order-side-semantics`，基线 `41c618936f9a598b3641d7be4e6a04d8b98b0f3f`。基线与生产 `9501a5f39f0c5f196cc29f24f3e3b8786267126b` 的 src/tests 零差异，仅增加已提交文档；没有带入告警改动。未提交、推送、合入、部署、重启、改 schema/业务数据或调用交易所写操作。

## 选择与准确语义

采用所有者授权选项中的**单独反向关系检查**，不做统一 buy/sell → long/short 后再比较：两类字段保留不同语义，读代码和排查时都能明确区分。

| 输入域 | 正常 | 异常/处理 |
| --- | --- | --- |
| 保护 TPSL | posSide=long、side=sell；posSide=short、side=buy | long+buy、short+sell 拒绝 |
| 持仓方向别名 | posSide / pos_side 等价值一致 | 两个别名冲突仍拒绝 |
| 保护单缺失/空白 side | 保持旧兼容；其余身份与匹配判据仍须满足 | 不能凭缺失 side 推断新归属 |
| 保护单提供 side 但缺持仓方向 | 不从 buy/sell 推断目标仓位 | 拒绝 |
| 非法 side | 无 | unknown、long/short 作为订单 side、数字 0、False 均拒绝 |
| list_positions 活仓行 | 保留原来的持仓方向别名解释 | **不施加平仓反向转换** |
| Conditional/入场请求 | 保留原来开仓方向语义 | 不把入场 buy/long、sell/short 当保护反向冲突 |

输入大小写/首尾空白按既有规范归一化，显式持仓字段里既有 buy/long、sell/short 等价归一化保留；订单字段只接受 buy/sell。可选字段缺失不等于已证明归属，后续 native matcher 的 posSide/合约/订单等门禁继续生效。

## 逐处核查与改动

| 位置（原基线行号） | 实际比较对象 | 处置 |
| --- | --- | --- |
| TP executor :1269 别名组 | pending TPSL 的 posSide 和订单 side | 拆开；posSide/pos_side 一致性 + 独立反向关系 |
| TP executor :889 | 备用保护订单回读方向与目标仓位方向 | 校验反向关系后，仅用持仓方向集合比较目标 |
| TP executor :1146 | 未归属 TP 对目标持仓方向的影响范围 | 同上；真实异常行仍 fail-closed，不把冲突过滤成“没有未归属单” |
| TP executor :1213 / :1247 | list_positions 活仓方向 | **不改**，不是保护订单方向 |
| trigger_backup_stop_executor :836 附近 | 独立重复实现的 TPSL 别名组 | 同样修正；主止损回读与备用回读均覆盖 |
| trigger_backup_stop_executor :655 | 未归属止损影响范围中的 pending TPSL 方向集合 | 首轮全套暴露的遗漏；反向关系校验后仅比较持仓方向集合，真实异常仍阻断 |
| entry_protection_ledger_repair :1989 | 账户级 pending TPSL candidate.side_aliases | 排除订单 side 的同义混用，并验证反向关系；异常候选保留为 evidence_complete=False，不能隐藏竞争 |
| entry repair :766 | 活仓快照方向 | **不改**，属于持仓域 |
| entry repair 的 _request_side / 父触发和 child fill 证言 | 入场提交/成交域 | **不改**；不能按平仓方向翻转 |
| entry repair 的 legacy protection candidate / response-anchored repair | 保护订单作用域筛选后确证 | 在原候选范围内发现方向矛盾直接拒绝；不通过删行制造唯一候选 |
| protection_snapshot.py | 调用 normalize_native_tpsl / match_native_tpsl_order，读取 posSide（或既有 PosiDirection） | 未发现将订单 side 混为同义别名；**未改** |
| backup_stop_repair.py :148 / :528 | 活仓行 posSide/side | 属于持仓域，**未改** |
| backup_stop_repair.py :487 / :510 | 通过 native normalizer/matcher 读取 TPSL 持仓方向 | 没有混合订单 side 的别名组，**未改** |

共 4 个生产文件：native_tpsl.py 仅新增三个纯函数（方向集合、反向关系、原始订单 ID 唯一计数），TP executor、backup executor、entry ledger repair 接入。**没有修改通用 normalize_native_tpsl / match_native_tpsl_order 的行为**，也不宣称所有全系统入口都新增了方向关系门禁；本轮修复已定位的混淆和其直接复用路径。

## 不变门禁与独立评审发现

本地 AST 对比基线确认以下函数完全不变：

- `execution_bindings._ready_verified_trigger_take_profit_convergences`：包含路径 A 的合约、posId、持仓方向、split 和正数量五项判据。
- TP executor 的 `_live_position_aliases_match`、`_live_position_aliases_consistent` 和 `exact_owned_stop_evidence_fingerprint`。
- native normalizer、match_native_tpsl_order、_exact_order_matches、_leg_matches。

主止损匹配仍按当前仓位数量、再 `Decimal("0")` 尝试；没有改变全仓 sz=0 的解释，没有时间容差、最近订单、同价猜归属或绕过 ownership/lease/exact_position_write_gate。

**独立评审曾阻止两个 fail-open 回归，已补 RED→GREEN 后修复：**

1. 同一原始快照两行声称相同 order ID，其中一行方向矛盾。如果先过滤再数，只剩合法行会把原先 ambiguous 变 verified。现在先对所有原始 ID 别名计“行数”，再进行方向过滤；同一行多个等价 ID 不重复计，不同行相同 ID 必须阻断。覆盖主止损、TP 回读、exact backup 和 backup 两种回读。
2. legacy adoption 的不同 ID 同形状竞争候选，其中一行方向矛盾。如果在作用域筛选时删掉，会把原先不唯一变成可认领。现在作用域筛选保持原样，在原候选域内直接返回 `trigger_protection_candidate_side_conflict`；response 锚定路径的缺失/重复/异常证据继续拒绝（`returned_order_not_pending`）。账户分配器保留异常候选为阻断证据。

这是为保留原有拒绝能力而增加的**原始集合唯一性**检查，不是放松其他门禁。一般别名异常行、数字别名、价格/数量/合约/posId/订单 ID 冲突仍被测试固定为拒绝。

## 115 条任务：实际收益范围

2026-09-05 **13:23:21 UTC**，只使用 worker `127.0.0.1:8002` 的 GET `/api/runtime-incidents/live-position-sizes`（现有鉴权，仅在内存使用、不输出凭据）：`complete=true`，唯一仓位 `1001125135694798`、数量 6。与相邻只读 DB 事务中 waiting 队列关联：

| 当前前置状况 | waiting 任务数 | 本 bug 能否解除当前阻断 |
| --- | ---: | --- |
| 没有 leg.pos_id | 74 | 否，尚无可管理的精确仓位 |
| 有历史 posId，但不在完整活仓列表 | 40 | 否，精确活仓前置不成立；不应补挂历史仓位 |
| 活仓匹配：227 / leg 583 | **1** | **已实证本 bug 阻断主 SL 回读；本地修复解除这一必要前置** |

因此 **确证当前受本 bug 阻断的为 1/115，不是 115/115**。它原先已归属主止损，不是缺确定性入场血缘。修复后仍须每轮 fresh snapshot、归属和其余执行前置全部成功才允许下一步，不承诺替所有者立即挂出 80200。

worker 身份只读 GET `/api/runtime/deployment-identity` 返回 release 9501a5f3、runtime_role=worker、loaded_artifact_verified=true。未在生产调用本地修复函数、ready/reconcile、补挂或提交函数。30/3 存量未查询处置或更新。

## 全历史可能误判主止损的范围与不能归因的边界

**131 万条 pending_tpsl_snapshot_observations 只保存 order_ids_json / complete / observed_at，没有 side、posSide 或完整订单行。** 不能用连续存在的 ID 单独证明当时返回了哪种方向组合。

为避免把缺失证据当作没有影响，本轮扫描现存 9 个业务表中的原始回读/历史快照 JSON，不使用订单请求参数伪装交易所响应：execution_events、execution_order_legs、strategy_management_legs、position_protection_ledger、trigger_protection_intents、trigger_protection_stop_rescues、position_take_profit_orders、position_protection_legs、position_protection_incidents。

13:18:43 UTC 的同一 `mode=ro + query_only=ON` 事务发现：

- **517 个嵌套 TPSL 原始行出现次数，294 个不同订单 ID**，均带正常 long/sell 或 short/buy。重复 JSON 出现不能重复计订单。
- 与 stop_loss/combined 账本按 **order ID + instrument_id + 持仓方向 + 实际 SL 触发价** 对齐：**66 个订单 ID、41 条历史 leg**。只按 order ID 初筛会得到 45 条腿；额外字段核对排除 4 条，采用 41 这一收紧口径。
- leg 583 的 ledger 659 仅保存归属摘要，不保存完整原始单行；但会话 4 的完整现场证据给出订单 1001125135694875，且本轮 fixture 复现。因此现有可追溯材料合并为 **42 条腿 / 67 张主止损订单**具有会触发旧方向过滤的形态。这是**代码路径的潜在暴露集合，不是 42 次已证实历史误判**。
- 41 条旧腿创建月份为 7 月 20、8 月 17、9 月 4。waiting 与这 41 条相交仅任务 **16/leg352、18/leg355、26/leg365**；它们的腿在 7 月已关闭。加入 227，waiting 中有 4 条能找到该形态，但前三条不能因此认定为本 bug 导致 waiting。

41 条精确对齐历史 leg：

```text
287,293,308,310,314,330,337,352,355,357,364,365,375,391,392,400,
403,404,408,416,432,435,436,439,456,457,460,487,488,496,497,503,
504,511,530,540,553,560,565,567,579
```

**时间上的关键排除：**Git `-S 'def _native_tpsl_aliases_consistent'` 定位到 877fbc33（提交 2026-09-04 12:17:16 UTC）；其前一个版本没有这道函数。部署报告 `docs/2026-09-04-trigger-protection-lineage-production-activation.md` 记录该版本在 09-04 16:10–16:12 UTC 激活，激活前交易所空仓。不能把 7–8 月已有 TP 的历史策略倒算成后来新过滤器的故障。

41 条中只有 leg 560 的记录更新时间延伸到上线后（源记录最后更新 09-05 06:43:56、腿 manually_closed 最后更新 06:44:20）；其他 40 条的腿及证据更新时间均早于引入该函数。**更新时间不是当时仍有真实仓位或门禁失败的证据**，更不能推翻激活前空仓记录。因此 560 只能记为需进一步证据的历史候选；本轮未发现第二条像 583 一样有完整同时刻输入、旧函数返回及对照验证的真实故障。

最终归因：**583 已确认；42 条是形态暴露集合；历史实际误拒总数无法由当前保存字段精确重建。** 要得到精确历史失败数，需要当时 readiness/backup 校验的完整原始输入、版本与子判据结果；当前缺这些，不能推测“41 条也都被卡住”。本轮不回放/修改任何历史任务。

审计脚本 `/tmp/protection-side-history-readonly-20260905.py`、只读聚合结果 `/tmp/protection-side-history-readonly-20260905.txt`。远程仅 stdlib、Python -B；未 import immutable release 或写入业务库。初次时间分布查询遇到 execution_events 没有 updated_at，按真实 schema 改用 created_at 后完成；未猜列值为 0。目标原始证据见 `/var/lib/telegram-kol-cutover-evidence/leg583-exact-gate-20260905T0735Z`，manifest `4c4ce6aedfa98a6f920db6e3e89e664a8475870293855da40a262af0881ddc11`。

## 测试与冻结结果

- 初始真实形态/异常方向/账户归属 RED：**9 failed, 1 passed in 0.41s**。既复现正常行被丢，也复现真正反向错误被当别名接受。
- 非法 falsey side 边界 RED 4 项后修正；同 ID 异常重复及不同 ID legacy 竞争 RED 后修正。未用 mock 替代 matcher 或唯一性判据。
- 最终生产代码组装后聚焦 **313 passed in 15.15s**，覆盖两执行器、entry repair、native TPSL、protection_snapshot 和 backup_stop_repair。
- 独立评审 `protection_side_review`：首次两个 P1 阻止冻结，修复后重新实测原反例均拒绝；独立聚焦 270 passed，确认无新阻断问题。随后将建议的 legacy 不同 ID 竞争及所有原始 ID 别名唯一计数正式固化为回归测试。
- 首轮完整 pytest：**1 failed, 7351 passed, 4 skipped, 32 warnings in 490.20s**，日志 `/tmp/protection-order-side-full-pytest.5hk8Aw`。失败为 execution_bindings 中另一条把 short+sell TPSL 当正常的旧 fixture。改为 buy 应通过 / sell 应拒绝后，buy 仍 RED，进而发现 backup executor 的未归属止损影响范围还有一个混合方向集合；已按同一语义修正，未靠改成拒绝预期掩盖问题。因此本轮将对新的最终生产候选重新跑完整套件。
- 最后生产 delta 后扩展聚焦 **512 passed in 31.00s**，含 test_execution_bindings。独立评审又运行相关正负和 backup_submission 聚焦 **20 passed**，并独立确认 normal primary 不阻断、真实矛盾/未知单/缺失方向/未知 side/类型 alias 冲突均阻断，正常单一 Conditional 保留原排除逻辑。两 executor 全部方向归一化调用重新逐处核对，未发现剩余保护域混用。
- 最终完整 pytest：**7353 passed, 4 skipped, 32 warnings in 469.29s（7 分 49 秒）**，日志 `/tmp/protection-order-side-final-pytest.uR031c`。在最后生产改动后重新完整运行，没有复用首轮或告警分支结果；32 项为既有 YAML prompt seed / Python SQLite adapter 弃用警告。独立复审无剩余阻断发现，本地候选验证完成，不构成部署授权。

最终候选 src/tests 指纹（615 文件）：`57dbe389b432e68ce8c64d8ed382174091c93137cd27ce5831d44c033a5ddf94`。算法排除 __pycache__/pyc/pyo，按相对路径排序，每文件组成 `SHA256 + 两空格 + 路径 + LF`，再对清单 SHA256；不是 Git commit SHA。

| 生产文件 | SHA-256 |
| --- | --- |
| entry_protection_ledger_repair.py | 28b50a1a27b5a0d95998336c8e7d9b1aa3b8b93387c0c2cac3fa1e2404671975 |
| native_tpsl.py | 0426849b2bd7258c889f513f1da83665c42bff1699c1961cb543d80e8bb5a8df |
| trigger_backup_stop_executor.py | e7cddbb64cacf8599150a5336213371841598865ba7622bcb68928141f64d4a2 |
| trigger_take_profit_convergence_executor.py | 5cd2e9e70480fccfa2d5b48f0fb7fd41c4807799d0e80b6758129c051c093138 |

告警 worktree 的 src/tests 指纹仍为 `0ad63dab9453651283cb97139017d15cfb20de5148081376a77d55463a104b92`，本轮未触碰。当前 worktree `.venv` 是本地测试环境 symlink，不是交付文件。没有 Git 冲突，没有修改共享主分支和会话 1 的处置工作。
