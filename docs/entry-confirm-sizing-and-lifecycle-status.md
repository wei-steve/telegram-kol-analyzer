# 入场确认：仓位语义与生命周期完整性 —— 实施状态

- **阶段 1（4.1.1–4.1.4）与阶段 2（4.2.1–4.2.4）**：已完成实现，
  **未部署、未推送、未连服务器、未触碰交易所、未改自动交易开关、未改提示词**。
- **阶段 3、阶段 4**：未开始，本次不碰。
- **3.3（收紧 A-16b 重复入场护栏）**：用户 2026-09-23 拍板**不采纳**，未实施。
- **分支 / worktree**：`worktree-agent-a26f573661d8b77ae`
  （`/Users/steven/Documents/telegram获取消息/.claude/worktrees/agent-a26f573661d8b77ae`）
- **基线**：`codex/deepcoin-auto-trading-v1` 尖端 `eb5923a2`（设计稿提交）。线上回退点 `77df50d4`。
- **约束性文档**：`docs/plans/2026-09-23-entry-confirm-sizing-and-lifecycle-integrity-design.md`
  （冲突时以它为准）。

---

## 1. 阶段 1 做了什么

### 1.1 标记（4.1.1）

`message_recognition._upsert_entry_confirmation_candidate` 写出的候选现在带
`management_action = "entry_confirm"`。`event_type` 仍是 `"entry_signal"`，
`target_lifecycle_id` 仍是 `NULL`，该列早已存在，**不需要迁移**。历史候选不回填（4.1.4）。

### 1.2 统一判据（4.1.2）

新模块 `src/telegram_kol_research/entry_confirmation_candidates.py`：

- `is_entry_confirmation_candidate(candidate)`
- `is_entry_confirmation_signature(event_type=…, parse_source=…, management_action=…)`
  （给只拿到列值、拿不到行对象的两处用）

四处原先直接判 `parse_source in {"entry_confirm_heuristic", "lifecycle_ai"}` 的地方改用它：

| 位置 | 改后行为 |
|---|---|
| `auto_trade_execution.py` 入场闸门（原 `:925`） | 命中 → `lifecycle_event_not_new_entry` 跳过，**在任何交易所写入之前** |
| `auto_trade_execution._infer_entry_execution_type`（原 `:2465`） | 多收一个 `management_action` 参数 |
| `strategy_alerts._alert_type_for_candidate`（原 `:892`） | 「临时入场」标签不变 |
| `strategy_alerts._resolve_order_type`（原 `:985`） | 多收一个 `management_action` 参数 |

指令项照常生成（`_instruction_kind` 只看 `event_type`，仍是 `"entry"`），
以「已核实跳过」终态收尾，审计与通知保留。

### 1.3 例外：市价 + 自带止损 = 策略（4.1.3）

`_apply_lifecycle_event_decision` 的 `entry_confirm` 分支之前判定两件事：

- **市价字眼**：`entry_price_geometry.text_names_market_entry(...)`。词表
  `MARKET_ENTRY_LABEL_TOKENS` 从 `auto_trade_execution._infer_entry_execution_type`
  原地提取到 `entry_price_geometry`，两边**共用同一份**，没有新起词表。
- **自带止损**：`_own_message_stop_loss(decision, authoritative_payload)`，只读
  `lifecycle_event.stop_loss` 与 `evidence.text.fields.stop_loss.value`，必须能解析为
  **单个绝对价格**（恰好一个数字、无百分号、>0）。**绝不回退到 `lifecycle.stop_loss`。**

两者同时成立 → 不走确认分支：以**本消息**为键 `_upsert_entry_signal_candidate` +
`_ensure_lifecycle_record`，止损取本消息值，止盈取本消息值（缺就是 `None`），
目标的旧 pending 生命周期**一个字段都不动**。下单走决策 D 的既有路径。

任一不成立 → 1.1/1.2 的路径，不开仓。

---

## 2. 阶段 2 做了什么

### 2.1 确定性仓位词（4.2.1）

新模块 `src/telegram_kol_research/entry_position_sizing_terms.py`，纯函数
`entry_position_risk_multiplier(text) -> Decimal | None`：

| 文本 | 倍率 |
|---|---|
| 半仓 / 轻仓 | 0.5 |
| 一成…九成（仓/仓位） | 0.1…0.9 |
| N%（仓位/仓），1–99 | N/100 |
| 正常仓位 / 重仓 / 满仓 / 十成仓 / 100% / 无仓位词 | 不产出 |
| 多个互相冲突的仓位词 | 不产出 |

只产出 `0 < m < 1`。百分数必须**紧挨** `仓`/`仓位`，所以「止盈了 50%」「回撤了 30%」不算仓位词。

### 2.2 确认消息产出 preamble（4.2.2）

`entry_confirm` 分支内、**与生命周期更新同一事务**，调用现成的
`persist_entry_preamble_in_session`。`symbol`/`side` 取目标生命周期，`confidence` 取本次识别置信度，
`reason` 注明来自确认消息；`evidence_version_id` 取本消息当前（未被 superseded 的最高版本）证据版本，
`recognition_generation` 取权威代次。指纹、同消息旧 pending 的作废、`consumed`/`expired` 流转全部复用现有逻辑。

### 2.3 确认候选不再是硬边界（4.2.3）

`entry_assembly_admission._load_source_facts` 里候选转 fact 的分支，
`is_entry_confirmation_candidate` 为真时 `kind = "entry_confirm"`：既不在
`HARD_BOUNDARY_KINDS` 里，也不是 fragment，也不是 unresolved（仍然产出 fact，所以该消息算「已表示」，
不会掉进 unresolved）。其余候选行为逐字不变。

---

## 3. 设计稿没写、由实施者拍板的事

按「设计沉默处取对现有运行语义改动最小的选项」执行。

1. **1.1 的连带影响：`web_queries._candidate_action_kind`**。它 `if management_action:` 短路，
   会把新标记当成动作 kind 返回 `"entry_confirm"`，于是消息卡的 MiMo intent 匹配失败，
   「系统已接纳」翻成「系统未接纳」。**已修**：该函数对确认候选跳过标记，仍由 `event_type` 回答
   `"entry"`——与标记出现之前逐字一致。不映射成 `"confirm_entry"`，因为那会**改变**今天的显示结果。
2. **两处对 `management_action IS NULL` 的硬断言，故意不动**：
   `entry_assembly_fingerprint_repair._candidate_is_entry_strategy` 与
   `production_safety_monitor._has_exact_legacy_finalized_entry_fingerprint_reconciliation`。
   两者都要求候选挂着一条 `entry_strategy_assemblies` 行，而 assembly 只在
   `auto_trade_execution` 里、**1.2 的闸门之后**才会写；阶段 1 之后确认候选永远走不到那里，
   历史行又不回填（4.1.4），所以这两处在新值下不可达。真要不巧到达，两者都是 fail-closed
   （拒绝修复 / 报一次告警），方向安全。
3. **`strategy_alerts.py:519`（`parse_source == "lifecycle_ai"` 决定告警里显示哪个入场价）没动。**
   设计稿只点名 `:892` 与 `:985`；它只影响展示，动它会改变权威路径确认消息的告警显示。
4. **价格缺失的纯市价例外（1.3）的 `entry_text` 规范化**：确认消息带自带止损、但
   `lifecycle_event.entry_price` 为空时，候选的 `entry_text` 写成规范值 `"市价"`——
   这是决策 D 的 `is_pure_market_entry_text` 认得的形状，让它走实时价 + 3 分钟时效那条路。
   这是对消息原话的规范化，不是凭空补一个没人写过的价格。
5. **preamble 写不出去时静默放弃**：没有当前证据版本或没有权威代次时不写（两列都是 NOT NULL）。
   后果是下一条策略按满仓走——与今天完全一样。
6. **启发式确认路径（`_apply_entry_confirmation_signal_if_matched`）不产出 preamble**：
   设计稿 2.2 说的是 `entry_confirm` 分支（`_apply_lifecycle_event_decision`），
   而且启发式路径既没有权威代次也没有证据版本。它仍然会被 1.1 打上标记（同一个 upsert 函数），
   本来也已经被 `parse_source` 判据挡住。
7. **`_apply_lifecycle_event_decision` 新增 `authoritative_payload` 关键字参数**（默认 `None`），
   只为让 1.3 读到 `evidence.text.fields.stop_loss`。权威调用点传入，多目标递归原样透传，
   其余调用点不传，行为不变。

---

## 4. 测试

新增两个文件：

- `tests/test_entry_position_sizing_terms.py` —— 设计稿第 5 节第 6 条（词表）
- `tests/test_entry_confirm_sizing_and_lifecycle.py` —— 第 1–4、7–9 条

第 8 条（端到端 10696 → 10697 相隔 65 秒）是核心验收，并且**已验证可证伪**：
把 2.3 改回 `complete_entry` 后，它与 `test_a_confirmation_candidate_does_not_bound_the_preamble_on_its_own_message`
双双失败（`risk_multiplier` 退回 `Decimal('1')`，`boundary_evidence` 非空）；改回 `entry_confirm` 后两条都通过。

第 5 条（真正的新策略不受影响）由既有全量套件覆盖。

---

## 5. 下一步（不在本次范围）

- 阶段 1+2 合并为一次 `tg-deploy`，回退点 `77df50d4`；部署与观察由主会话决定。
- 阶段 3（3.1 生命周期完整性 + 3.2 脱节告警）、阶段 4（全平顺带平孤儿）。
- 缺陷 2（入场被抢走唤醒、从未下单）——设计稿 3.5 建议优先级高于阶段 4。
