# 2026-09-28 核对问题修复 — 问题 2 / 3 / 4 实施状态（分支 `audit-fix-b`）

- 设计稿：`docs/plans/2026-09-28-audit-fixes-design.md`（已批准，第 6 节全部按推荐）。
- 范围：问题 2、3、4。问题 1 由另一条线实施，本分支不碰
  `protection_attribution` / `strategy_management_executor` / `strategy_management_planner` /
  `web_app` / `protection_authority`。
- 基线：`f2846c41`。不推送、不部署、不改识别提示词。

| 问题 | 状态 | 提交 |
|---|---|---|
| 2 比例错配 + 「加仓后」 | 完成（本地测试） | 见 git log「audit fix 2」「audit fix 2b」 |
| 3 告警送达 | 完成（本地测试） | 见 git log「audit fix 3」 |
| 4 模糊止损修饰词 | 完成（本地测试） | 见 git log「audit fix 4」 |

## 问题 2

- `management_directives._percentage_values`：找到最近的数量动词后，调用
  `_percent_belongs_to_verb(clause)`；返回 False 时跳过该百分数（`previous_quantity=False`）。
- `resolve_management_directive`：风险增加词判定前剔除「平加仓」和「加仓后」（仅这一写法）。
- **与设计稿字面的偏差（有意）**：设计稿写「动词与百分数之间出现句子边界就跳过」。按字面实现会让
  既有的加固测试失败——`减仓\n150%`、`减仓；比例120%`、`保留\n-20%`、`减仓 -\n10%`、`减仓1,5%`
  必须仍被拒绝（A-8 的不变量：给出了无效内容就不能退回默认 50%）。设计稿同一段也写了
  「其余情况与现在完全一样」。所以实现为：把动词与百分数之间的文字按句子边界
  （换行、`。！!？?；;‼`）和分句逗号（`，`，以及不夹在两个数字之间的 `,`）切块，**只要百分数所在块之前的任一块含数字**
  就跳过。即：动词已经带了自己的数字/陈述，百分数属于另一句话。
  - 设计稿的全部回放用例（R2-a…f）都满足这一条（#19597 中间有「83200」，#17900/#18153 中间有
    「80600」「64100」，#18603 中间有「95点」，`平仓78031.7，盈利126.05%` 中间有「78031.7」）。
  - 残留缺口：「全部出局！持仓收益高达370％」这种动词和百分数之间**没有任何数字**的写法仍会被拒
    （与修复前相同）。生产样本里没有见到这种形态；如需覆盖，要先决定怎样区分它和 `减仓；比例120%`。
- 测试：`tests/test_management_directives.py` 中 `test_r2a_*`…`test_r2f_*`；修复前 6 个失败、修复后全部通过。

### 问题 2 补充（审阅后第二个提交，「audit fix 2b」）

- 审阅发现 #17936（陈哥群，auto_trade）仍被拼接误拒：原文「…止盈70%保留底仓…\n@Tarderfengge QQ:158241758」
  拼上 observed_text「…止盈70%保留底仓…」，原文里的「保留」绑到复述里的 70%，中间唯一的数字是 QQ 号，
  被 `scrub_contact_identifiers` 抹掉，所以「中间块含数字」规则看不到。
- 修法：拼接处做成**硬边界**，与数字规则无关。
  - `management_directives.AUTHORITATIVE_TEXT_PART_SEPARATOR = "\n \n"`（换行 + U+2029 段落分隔符 + 换行）；
    `message_recognition._authoritative_current_message_text` 改用它拼接。
  - `_percentage_values` 先按 U+2029 切成各部分、**再**逐部分清洗联系方式并提取（`previous_quantity` 每部分重置），
    所以联系方式清洗不可能抹掉边界，一部分里的动词也不可能认领另一部分的百分数。
  - `_CLAUSE_BREAK` 加入「、」：`平仓价78,136.4、盈利+136.13%` 前一块带数字 → 不绑定；`减仓、150%` 前一块无数字 → 仍然拒绝。
- 拼接文字的全部消费者（已逐个核对，U+2029 两侧仍各有一个换行，按行读取的逻辑看到的仍是换行）：
  - `management_fraction_gate.validate_management_fraction_payload`、`resolve_management_directive`（在
    `_apply_lifecycle_event_decision`、`_apply_deterministic_management_scope_if_matched`、多目标准入 / 校验、
    `_attribute_unapplied_lifecycle_event` 里）：本修复的目标。
  - `_exit_decision_looks_like_management_update` / `_management_action_for_exit_downgrade` / `_should_move_stop_to_protect`
    / `text_names_market_entry`：只做关键词包含判断，不受影响。
  - `_extract_explicit_stop_loss_from_management_text`：正则 `[^0-9]{0,20}` / `{0,8}` / `{0,12}` 窗口，U+2029 与原来的
    `\n` 一样能被跨过，但拼接处现在多 2 个字符，跨拼接处的匹配窗口相应缩短 2 个字符（只影响「止损」在一部分末尾、价格在
    另一部分开头这种本就不该成立的配对）。
  - `_text_contains_explicit_stop_value`：前 32 / 后 16 字符的来源窗口，同理只在跨拼接处少看 2 个字符。
  - `build_management_instruction_contract(current_message_text=…)`：写进合同 JSON 并参与合同指纹。指纹只和
    **同一行存的 JSON** 比对（`strategy_management_batches` 两处），不会用文字重算，所以新旧行各自自洽；只是
    raw 与 observed_text 不同的消息，新写入合同里的 `current_message_text` 多了 U+2029。`management_stop_price_gate`
    对它只判断非空。`strategy_management_planner` 构造合同用的是 `raw_message.text`，不经过拼接。
  - `json.dumps(ensure_ascii=False)` / SQLite 可以原样存取 U+2029。
  - 原始消息自身若含 U+2029（罕见），也会被当成边界切开，只影响百分数绑定。
- #17936 实际结果（设计稿没写，审阅者预期 0.3）：拼接文字现在通过比例校验；`resolve_management_directive` 对
  原文、复述、拼接文字**都**返回 `partial_take_profit`、比例 **0.8**、`reason_code=tail_retention`。原因：「保留底仓」是
  `_TAIL_TERMS` 里的尾仓词，这个分支在百分数分支之前，固定按 `DEFAULT_TAIL_CLOSE_FRACTION`（0.8）平仓，
  不看「止盈70%」。基线 `f2846c41` 上原文单独也是 0.8，属既有语义，本分支未改；KOL 原意更可能是平 70%，
  是否让明确百分数优先于尾仓默认值需要另行裁定。
- 测试（`tests/test_management_directives.py`）：`test_r2g_17936_*`、`test_r2g_a_verb_never_claims_a_percent_across_the_join`、
  `test_r2g_malformed_content_inside_one_part_is_still_rejected`（含原文单独 `减仓\n150%`）、
  `test_r2g_contact_scrubbing_cannot_blank_the_join`、`test_r2g_enumeration_comma_*`；
  `tests/test_message_recognition.py::test_authoritative_current_message_text_excludes_model_reasons` 的期望拼接串随之更新。

## 问题 3

- 3a：`config.ALWAYS_NOTIFIED_INCIDENT_TYPES` 加入 `management_fraction_rejected`。notify_only 群的行在写入时
  已被 `record_fraction_rejection` 标成 `suppressed`，不会多发。没有既有测试断言该集合的精确内容（只有
  「某类型在集合里」的断言），无需改动。
- 3b：`oncall_alerts`
  - `compose_case_alerts` 的「已恢复」告警、`compose_diagnosis_alert` 的诊断告警：不再检查上限、不再计数。
  - 开案：`_case_open_bypasses_cap(case)` 为真则不受上限拦截，但仍计数；其余开案维持上限 30。
    合并通知、上限通知、每日汇报不变。
  - **「auto_trade 群」的判定（设计稿未说明，本实现的选择）**：采用保守规则
    「严重度 high/critical，且（规则含 D6c，或案例带 `raw_message_id`）」。原因：群的交易模式只在
    `groups.yaml` 里，生产库没有；值守进程不读它，且架构边界测试只允许 `oncall_alerts` 导入
    `oncall_codex` / `oncall_state`；要拿到模式就得给值守新增一次生产配置读取。带 `raw_message_id`
    的规则（D1a–c、D2、D3、D6a、D6b）都只在群里有持仓 / 批次 / 删除退出时才开案，实际就是交易群。
    仍受上限约束的：D1d（medium）、D4/D5 健康类、D5a 读库失败。合并规则的 `evidence["rules"]` 里含 D6c 也算 D6c。
  - 既有测试 `test_the_daily_cap_stops_at_the_limit_and_resets_the_next_beijing_day` 用的是高严重度、带消息的案例，
    按批准的设计它们现在不受上限拦截；改为 medium（D1d）案例，测试的上限机制本身不变。
- 测试：`tests/test_system_operator_bot.py::test_r3a_*`（2 个），`tests/test_oncall_alerts.py::test_r3b_*`（2 个）、
  `test_r3c_*`（1 个）；修复前全部失败、修复后通过。
- 部署提醒（设计稿第 5 节）：值守代码有改动，部署后要单独重启 `telegram-kol-oncall.service`。

## 问题 4

- `entry_price_geometry`：新增 `_FUZZY_STOP_QUALIFIERS_RE`（跌破 突破 涨破 破位 站上 有效 小幅 下方 上方 以下 以上
  之下 之上 一点 一些 少许 左右 上下），只在 `_proves_absolute_candidate_field(field="stop_loss")` 里、字段标签剥离之后使用。
  `entry_prices` / `take_profit` 的标签集合没有变（有测试钉住）。止损取抽出的唯一价 X，不加缓冲。
- 仍拒绝：两个价格（`跌破2520或2510`、`跌破2520/2510`）、相对写法（`跌破2520 20个点`、`入场价下方30点`，由既有相对表达检测拦下）、
  无数字（`跌破前低`）；多单 `2560下方一点` → `entry_price_geometry_stop_side_invalid`。
- 调用方核对：`validate_candidate_entry_price_geometry` 的调用方只有 `auto_trade_execution`（两处）、`trading_decision`、
  `recovery_scan`，都走这一个函数。下游不再用别的解析器**校验**止损文字：下单草稿
  `deepcoin_order_builder._parse_optional_price` 用同一个 `extract_normalized_prices` 取第一个价（得 2520）；
  `lifecycle_monitor._parse_single_float` / `message_recognition._parse_single_float` 取文字里第一个数字（同样 2520），
  只是取值不是校验；`validate_order_draft_price_geometry` 用草稿里已经是数字的止损。
- 未改任何识别提示词。
- 测试：`tests/test_entry_price_geometry.py` 中 `test_r4a_*`、`test_r4b_*`、`test_r4c_*`；修复前 14 个失败（R4-c 的拒绝用例修复前后都通过）。
