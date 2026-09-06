# Trigger protection lineage：shadow → live 再评估

## 结论：丙，应当换一种上线方式

**不建议把当前 9501a5f3 的 lineage 设置直接切到 live。建议先完成“原始响应 → 当前适配器 → 判据”的无权威回放与契约校正，再评估未来 intent 的受控启用。不是继续等新旧结论一致。**

旧的 `candidate_predates_fill` 门确实不能作为正确性基准。但这不等于 877fbc33 已能处理真实返回值。本轮发现两个实际阻断点：

1. `_trigger_protection_child_fill_rows` 要求父触发历史行的 client-ID aliases 精确等于本地父 clOrdId。会话4保存的 T3 ETH 触发历史 100 行没有一行包含非空 clOrdId；其中 25 行精确命中系统父单。抽取生产函数进行只读回放，25 行全部在父身份门返回空 child 证据。**把本地 clOrdId 写进测试父对象，并不等于交易所会回传它。**
2. 新路径要求历史完整；当前读取只取每合约一页，满 100 行即标记不完整。会话4已经保存了满页历史。切 live 后这类输入会继续等待完整快照，而不是获得新归属。

两者都不能靠修改 mode 消失。第一项在完整性通过后仍然存在；第二项可能更早拦住第一项。切 live 还会禁止 watermark 后 intent 回退旧归属，所以可能把旧路径原本可成功的场景也变成拒绝。

本轮仅新增本文档。未切设置、未实现修复、未部署、未重启、未处置任何存量、未调用交易所写接口。

## 1. 核查身份与证据范围

2026-09-05 07:38:50 UTC worker 8002 的 deployment-identity 回读：

- release `9501a5f39f0c5f196cc29f24f3e3b8786267126b`；`loaded_artifact_verified=true`。
- 本地 `execution_bindings.py`、`entry_protection_ledger_repair.py`、`trigger_protection_assignment.py`、`trading_settings.py` 四文件 SHA-256 与该 release 一致。
- 生产 DB：MAX(intent.id)=**183**；`trigger_protection_assignment_shadow_plan` 全表 **0 条**。
- `trading_settings` 的 `key=global` JSON：lineage=`shadow`，watermark=`null`，liveness_v2=`live`。据有效模式计算函数，effective lineage 为 shadow。
- 当前 7 条历史记录准确状态为：125/131/135/148/163/177 六条 failed/manual_review、各 5 次；166 已 resolved/terminal、4 次。不是七条都仍 failed。

SQL 全部使用 `file:...research.db?mode=ro`、`PRAGMA query_only=ON`、显式只读事务；远端 Python 均 `python3 -B`。只调用 worker 身份 GET，业务原始字段复用会话4已保存文件。本轮没有重新采集交易所原始历史，也没有调用会写 observation 的 snapshot loader、reconciler 或 planner 包装入口。

主要证据：

- [会话4原始字段观测](2026-09-05-session4-trigger-raw-observation.md)，509 份成功客户端返回值；总索引 `/var/lib/telegram-kol-cutover-evidence/session4-raw-trigger-20260905T0705Z/final-evidence-index.json`，SHA-256 `a1965f750ab3d7fc8f7a7306a6ba7bab1977fa3e3a547dd677e606c805d2e6a2`。
- 本轮父身份门回放输入：`/var/lib/telegram-kol-cutover-evidence/session4-raw-trigger-T3-20260905T072050Z/T0-05-read_trigger_order_history-ETH-USDT-SWAP.raw.json`。使用 AST 从生产源码原样抽取 `_exchange_string_aliases` 与 `_trigger_protection_child_fill_rows` 到内存，不 import 应用、不连接 ORM、不落生产文件。只验证父身份前置门，不冒充完整归属回放。25 条含 leg 509；输入父对象原样保留，没有补 clOrdId。即使后续提供 child，该前置门也已返回。
- [旧拒绝与历史成功对照](2026-09-04-trigger-protection-lineage-read-only-validation.md)、[实现及独立评审证据](2026-09-04-trigger-protection-lineage-attribution-implementation-evidence.md)、[115 条 waiting 分层](2026-09-05-tp-waiting-backup-stop-cohort-read-only-diagnosis.md)。历史结论沿用其注明的观测时点，不冒充本轮实时全账户对账。

## 2. 会话4证明了什么，没有证明什么

07:10:29 首次可见 TPSL，07:15:50 成交，早 321 秒。普通订单和 TPSL 的 `cTime` 都是 07:10:25，即 `1788592225000`。因此新门中的“candidate cTime 与 child cTime 原始精度相等”在这两个对象上相容；新门并不要求等于 fillTime。

但原报告明确：普通订单为 limit，父 Conditional 未捕获，source 空，仓位是 **merge**，且系统没有该成交腿的 binding。877 builder 要求系统 trigger_limit 证言及 **split** 仓位。因此这不是可直接标记为“877 应接受”的正样本。它否定“止损必定晚于成交”的一般假设，不能单独证明这张 TPSL 的系统所有权，更不能替代 split 触发链正样本。历史 lifecycle 1050/1072 是更接近目标的失败样本，但还需要完整原始输入的适配器回放。

关于“42% ID 相等”，必须分清父单和子单。本轮口径为 `purpose='entry' AND pos_id IS NOT NULL AND pos_id<>''`，得到 359 条：

| order_kind | 条数 | leg.order_id = leg.pos_id |
| --- | ---: | ---: |
| trigger_limit | 202 | 0 |
| market | 151 | 150 |
| limit | 5 | 3 |
| unknown | 1 | 0 |

此口径与此前 358 的时点/筛选不完全相同，不强行对齐。关键是 trigger leg.order_id 保存**父触发单 ID**；0/202 不能反证 child regular ordId = posId，也不能证明该规则。当前适配器在无显式 posId 时用 child ordId 补 pos_id，builder 更要求 child.order_id 和 pos_id 同时等于腿 pos_id（`entry_protection_ledger_repair.py:473`）。这是需单独验证的 split 触发子单不变量，不是完整交易所外键契约。merge 样本不能替它背书。

## 3. shadow 门槛应改为何种验证

**必要的是非权威验证，不是生产 shadow 记录这个载体，也不是新旧一致率。** 有完整原始来源、真实本地证言与账户竞争集合的离线回放，可以替代等待某条 production shadow 日志；不能把缺少的字段补成理想 fixture 后称为实证。

应使用以下判定矩阵：

| 样本 | 预期 |
| --- | --- |
| 有独立证言支持的旧误拒绝，完整适用 split 链 | 新算法唯一接受；旧拒绝是预期差异 |
| 已核实的旧成功控制组 | 新算法保持同一精确 owner/order，不以任何成功替代同一身份 |
| 竞争 owner/child/candidate、别名冲突、已有 owner 冲突 | 零 action |
| 缺父证据、缺完整页、缺 split 身份或其他未知 | 明确拒绝并记录缺什么，不计作健康成功 |
| 没进入 selector/context 的腿 | 记录覆盖缺口，不能计入已验证分母 |

最低证据集应包含 1050/1072 的真实适配器输入、成功控制组、合法 pre-fill 场景的 split 触发链，以及针对下述每条门的反例。若历史必要原始数据已丢失，标为不可回放；随后只观察自然发生的系统成交，不下测试单。若 API 根本不返回要求字段，继续等 shadow 是死循环，应先纠正契约设计。

## 4. 零 shadow 记录：不能解释成“旧保护成功才能观测”

调用链依据 `execution_bindings.py:2104,2156,2270,2382`：

1. potential owner 要求 entry、trigger_limit、active、非空 posId；可分配 owner 再要求唯一且身份一致 intent、attribution_status=verified。
2. parent event 必须唯一且本地 client ID 一致，当前仓位构造必须成功，才能形成 assignment context。
3. shadow logger 遍历 **contexts**，同时记录 proposed action 和 refusal；没有要求主 SL 已归属，也没有要求旧 protection planner 成功。context 构造还不限 pending/retrying；due 状态主要控制后面的权威执行。
4. 无 context、快照错误提前返回、owner universe 无法界定，都可能没有 shadow 行。现有日志没有 selector 排除理由或覆盖漏斗。

这里的 verified 是**入场腿到仓位的归属**，不是**止损到该仓位的归属**。leg 559 的旧故障就是入场已经 verified，主止损仍被拒。故“旧保护算法失败时 shadow 必然沉默”的推论不成立。

正确的局限是：**对入场尚未 active/verified/posId 的失败，现有 shadow 没有观测价值；对已满足入口的保护归属失败，理论上能记录拒绝。** 后一种不必等旧算法成功。

本轮 DB 中 active trigger 腿只有 579/580，均是历史 adopted；会话4 T3 完整仓位列表没有它们的 posId。583 是 market；584 是 pending/unassigned/无 posId；会话4 ETH 测试没有系统腿。这解释了所见近期样本为何没有可用 context。DB 与 T3 是相邻但不同时间的证据，不能据此证明整个 shadow 时段每一轮的唯一原因。

因此零记录既不是通过，也不是已证实的全部失败盲区。需补的观测是：all intents → potential owners → consistent owners → contexts → attestation → graph → action/refusal，各层数量、排除原因、数据时点及完整性。不要靠加大日志频率替代缺少层级。

## 5. 七条合取门与真正的 fail-open 风险

以下把现有约束归纳为七组，不声称源码只有七个 if：

| 门 | 能排除什么 | 实证/剩余限制 |
| --- | --- | --- |
| 本地 intent/leg/event/binding 请求与成功回执一致 | 串腿、错误请求、合成 attached 标记单独认领 | attached 仍不是交易所保护 ID |
| 父 → 唯一 child → 已验证 split posId | 多 child、错误仓位、merge、数量漂移 | 父 clOrdId 回传前置与真实历史不匹配；child-ID 不变量尚缺分类验证 |
| owner 自己的完整提交前基线 | 把已存在旧单认给新 owner | 无法排除基线后外部操作产生的新同形单 |
| 当前 TPSL 精确类型、合约、方向、数量、止损及 alias | 形状不一致、组合保护、显式冲突 | 形状不是所有权外键 |
| candidate/child cTime 精确相同，且位于 intent 与 snapshot 之间 | 别的创建时段、过早/未来对象 | 原始毫秒字符串不保证交易所真有毫秒区分能力；本次样本均整秒 |
| 完整账户候选、双向唯一、阻断 owner | 已表示集合内的多解 | 已建模 owner 不等于交易所全部潜在创建者；外部/manual 来源缺本地腿时不能自动假设不存在 |
| finalize 事务内重验 intent、ledger、logical owner | 并发覆盖、重复认领、不可变 owner 被改写 | 事务能保一致性，不能把前面错误认领变成真所有权；也不替代新鲜交易所回读 |

这些门是有价值的防线，但**尚不足以宣称排除了全部 fail-open**。例如附带止损未实际可见，基线后同一交易所时间粒度出现一张外部同形 TPSL，而潜在外部 owner 不在本地 universe：字段层面可能不可区分。这里是需反证或明确拒绝的安全前提，不是本轮已复现的实际错误认领。当前缺字段会让路径提前拒绝，也不能用“现在无法到达认领”代替解决契约后路径的安全证明。

`_trigger_protection_candidate_child_rows` 还会跳过具有非匹配 client/parent aliases 的行；若普通历史 clOrdId 返回子单自身 ID，而不是父 client ID，需要单独验证其拒绝及竞争保留行为。不能简单删掉父 clOrdId 检查、把缺失字段从本地补进去就上线。

两次独立评审及 7170 passed 是实现防回归证据，**不等于真实 API 契约已验证**。现有 adapter 测试（如 `tests/test_execution_bindings.py:3180` 附近）构造的 parent 包含 clOrdId。实证发现与该前提不同，需要重新评审原始响应边界。历史报告“未发现 fail-open”是当时审查范围内结论，不是永久安全证书。

当前直接切 live 的风险有两类：错误认领会给后续撤换/保护操作错误权威；更明确、已经有输入支持的风险则是**新路径因缺字段或满页而始终拒绝，且旧路径被禁止回退**。不能只比较理论 fail-open 与既有 fail-closed，而漏掉实际没有改善的可能。

## 6. watermark 的效果及精确取值方式

`_lineage_mode_applies_to_intent`（`execution_bindings.py:1839`）在 live 下要求整数非负 W 且 `intent.id > W`；shadow 不使用此限界。effective live 还要求 effective liveness_v2=live。

本轮 MAX 是 **183，不是 182**；这不是未来切换值。未来授权切换时，应在账户权威互斥及配置切换临界区内，读取同一生产数据库的 `SELECT COALESCE(MAX(id),0) FROM trigger_protection_intents`，将精确整数 W 与 mode 一起绑定到已审核变更清单；记录读取时间、DB/release 身份、设置版本并回读有效模式。必须避免“读 MAX → 新 intent 创建 → 稍后改 mode”的间隙把切换前新增 intent 纳入权威。读事务本身不阻止并发插入；若现有配置路径不能保证这个临界区，先解决切换协议，不能宣称已严格 future-only。

这会排除所有 id≤W，包括七条历史记录，以及已存在的 pending intent 181/182/183，即使它们**切换后才成交**。限制对象是 intent 创建顺序，不是成交时间。六条 failed 和一条 resolved 也不在 pending/retrying 执行队列。

`execution_bindings.py:2391–2498` 同时证明：新 authority 的拒绝被标为 globally processed，不回退宽松旧认领；watermark 前对象继续原语义。**watermark 能阻止七条旧 intent 获得新血缘权威；它不是关闭所有旧管理路径，也不是恢复机制。** 不修改历史状态、不降低 W、不新建替身 intent 绕过它。

## 7. 切 live 的收益范围

| 对象 | 是否靠本次 mode 切换解开 |
| --- | --- |
| leg 583 / convergence 227 | 无此依据。market 主 SL 已通过提交响应的精确 order-ID 回读归属；阻断是后续就绪验证，lineage mode 不在该 readiness 条件中 |
| 七条历史 predates intent | 不会；watermark 排除且已终态，切换不重新排队 |
| 74 条无 posId waiting | 当前不满足入口。67 条终态；7 条 pending/unassigned 即使以后成交，若 intent 已在 W 内仍无新权威 |
| 40 条历史 posId 已无仓 waiting | 不应补挂；其中本地 active 不证明交易所活仓，切 mode 不清理残留 |
| 1 条活仓 waiting | 即 leg 583，不能承诺收益 |
| 未来 id>W 的 trigger intent | 只有真实适配器、完整快照、全部 fencing 均通过，才可能修复匿名主 SL 认领；下游 backup/TP/管理还有独立回读和互斥门 |

115 的分层来自会话2 06:24:11 UTC 调查，不把后来手工 ETH merge 仓位加入系统 waiting 分母。会话2已说明 583 的准确原始字段失败子项未保存；本报告不另行推断其根因。当前没有证据表明仅切 lineage live 能解开这 115 条中的任何一条。

## 8. 推荐的分步路径与独立授权

**阶段 A：原始契约回放。** 保持当前生产模式。另行授权本地诊断/测试工作后，使用已保存输入建立只读适配器回放，输出逐层覆盖与每条门结果；缺少父 clOrdId 的实际行、满页历史必须原样参与。历史重建只在隔离内存/副本做，绝不把合成状态写回生产。当前样本已经足以开始，不需要为等待生产 shadow 空日志而停滞。

**阶段 B：设计与实现契约修正，独立授权。** 先说明如何在不伪造引用、不降低 fencing 的前提下处理缺 clOrdId、child-ID 不变量和完整历史；无法证明的继续拒绝。是否可用完整分页/有证明的历史窗口、如何保留窗口外竞争 owner，都要有单独证据。不要仅把 100 行当完整，或仅增加 limit。若必须增补观测，部署一个只输出各门结果、不落 adoption/不激活 revision/不解锁下游的中间态；当前 mode 设置没有这个完整漏斗。

**阶段 C：测试及独立评审，单独验收。** 原始正/负样本、竞争者、外部同形单、缺字段、历史满页、切换竞态、重复 tick、崩溃恢复、watermark 前后、回退保留账本都要覆盖。新代码最终候选跑聚焦测试和一次全套，独立评审针对真实输入与差异，不只重看 planner 理想对象。若改 schema，按 L3 在生产库副本演练。

**阶段 D：未来权威启用，另行授权。** 只有上述门通过，才提交确切候选 SHA、W 绑定方式、影响范围、切换/回退清单。当前设置只有下界 W，**不能限制一条腿、一个合约或最多一条新 intent**；不得把它冒充自动 canary。需要硬范围限制就必须先另行设计实现。不能用“看见第一条后人工切回”承诺最多执行一条。

本轮文档评估为 L0，静态检查足够。未来观测代码若完全 dormant/shadow 且无权威/写入可按 L1；权威切换、恢复、生命周期为 L2；schema、生产数据处置以及真实交易所写语义变化为 L3，并明确纳入授权。认领虽是 DB 写，也可能在同一后续循环解锁 backup/TP，不能当作普通静态设置调整。代码、测试/评审、部署启用、存量处置分别授权；批准本文不自动批准其中任何一步。

未来启用的观察应至少满足 L2 的 30 分钟及真实消息要求，并额外区分“消息到达”与“适用 trigger fill 到达”。没有适用成交，不能宣称归属验证完成，也不无限延长窗口。记录漏斗计数、旧拒绝/新接受差异、精确 intent/leg/pos/order、完整性、认领延迟、下游每个 logical effect 的提交次数/未知状态、owner 冲突及长期停滞。`next_attempt_at=NULL` 的未完成/人工复核停滞须单独计龄告警；不能只靠现有 overdue 查询。恢复设计见[独立恢复闭环草案](plans/2026-09-05-trigger-protection-recovery-loop-design.md)。

## 9. 回滚不是撤销认领

`finalize_trigger_protection_adoption`（`entry_protection_ledger_repair.py:840–969`）不仅写 ledger，还绑定 logical primary、将 intent 标为 adopted，并激活 protection revision。模式切回 shadow/disabled，或回退 release，**都不会撤销这些持久化事实或已产生的交易所保护**。存量账本仍可能被管理路径消费。

应在启用前批准两类不同处置：

- **没有错误归属，仅运行异常/无进展：** 停止新的 lineage 权威，保留正确已验证映射和已回读保护；未知 mutation 只读对账，不重发、不自动撤单重建。mode 切换不会中止已经运行中的 tick，停止边界需与账户 authority 锁及在途操作核对。
- **疑似错误归属：** 单纯切 shadow 不够。先按预授权方案阻断受影响的管理/保护写入并确认在途结果，保全 intent、ledger、logical legs、revision、mutation、exchange 原始证据。逐笔独立确认后，正确记录保留；错误或无法确认的映射必须隔离为不可用权威。现有配置没有证明具备逐腿隔离能力，不能假设已有一键能力。任何修改账本/恢复 prior revision/交易所处置另列 L3 精确计划、备份及授权；不能盲删 ledger 或恢复整个旧 DB。

回滚触发条件应包含：一次 owner/order/pos 认领冲突或错配；一个 logical effect 重复写入；新 authority 作用于 id≤W；不完整快照产生 action；出现无法追溯的认领/未知提交；完整真实输入持续卡于同一契约门。前四项立即停止新增权威并进入受影响范围保护处置，不能等观察窗口结束。只缺成交样本属于“验证未完成”，不等于出现错误归属。

原设计的“保留已经正确认领的映射”是合理回退原则；**它不覆盖认领本身判错的事故**。在当前没有已证明的隔离路径、且原始输入还无法通过适配器时，直接切 live 的收益与回退保障均不足。推荐先走上面的无权威契约回放与校正路径。

## 本轮验证

仅进行了源码/文档核对、上述只读 SQL、worker 身份 GET、保存原始父单的前置门回放及文档静态检查。未跑 pytest、未提交 Git、未改其他会话文档。30 条 execution_running、3 条 execution_uncertain、七条历史 intent、115 条 waiting 均未由本轮修改或处置。
