# Codex 会话收尾与交接状态

日期：2026-09-05。背景：codex 订阅到期，多会话协作中止，未完成线索集中记录于此，
避免随会话关闭而丢失。

本文严格区分**已核实事实**与**未证实假设**。未标注为假设的条目均已在生产只读核实。

## 一、生产当前状态（已核实）

- 运行 release：`af8676dca5ce83acfc060a8b856ccf3884f25150`，四角色（web/ingest/worker/monitor）
  身份一致，`loaded_artifact_verified=true`。
- 交易所：零持仓、零普通挂单。
- `auto_trade_enabled=true`，`entry_admission_frozen=false`，monitor timer `enabled/active`。
- `authoritative_execution_attempts` 中 `status='uncertain'` 共 5 条：
  id 6 / 25 / 77 / 191 / 199（注意状态字面量是 `uncertain`，不是 `execution_uncertain`）。
- `execution_running = 0`。
- monitor 主 service 持续退出 1（详见第三节第 2 条），为已知误报。

## 二、待部署候选（已核实）

| 分支 | 候选 SHA | 状态 |
| --- | --- | --- |
| `codex/management-fraction-gate` | `3e8b8848dbdf9910afa225dddf1de9c7f404663c` | 未推送、未部署 |
| `codex/management-stop-price-gate` | `d1e3d8582501c54f1b9c105a0058c224e902c824` | 已被上者包含 |
| `codex/context-hold-owner-alert` | `9501a5f3…` | 告警收窄未完成，欠一轮判据调整 |

关于 `3e8b8848`（已实测确认，非推断）：

- `d1e3d858` 是 `3e8b8848` 的祖先，叠加成立；
- 生产 `af8676dc` 是 `3e8b8848` 的祖先，两者之间**仅 2 个提交**，为直线快进，无需合并；
- 本机独立复跑完整套件：**7444 passed、4 skipped、32 warnings，exit 0**，
  与交付报告一致。

它修复的两处缺陷**当前正在生产中生效**：

1. 管理指令止损价此前仅校验"正数"与"来源为消息正文"，无方向与幅度合理性比对。
   实证：raw 15013（大镖客，`trading_mode=auto_trade`）把签名中的 QQ 号 `158241758`
   解析为 BTC 多单显式止损价，该仓开仓均价 79519。
   provenance 校验反而为错误背书——QQ 号、电话、时间戳均满足"来自消息正文"。
2. 非法减仓比例（0、负数、"150%"）在 `_fraction_value` 中返回 `None` 被静默丢弃，
   与"消息未给数量"无法区分，随后兜底为 `DEFAULT_PARTIAL_CLOSE_FRACTION = 0.50`，
   即静默平掉一半仓位。危险在于"平一半"是看似合理的结果，事后审阅不易察觉。

## 三、未完成线索

### 1. `convergence_pending_alias_conflict` 未诊断（阻塞止盈）

TP 收敛任务 227（binding 339 / leg 583 / pos 1001125135694798）在 af8676dc 上线后
从 `waiting_backup_stop` 前进：`owned_stop_evidence_fingerprint` 已算出
`70423fdb4a67e9e16d7bc05e6c115c5648b9aa5ec6af68b3594573142fae8767`，
备份止损已挂出（触发价 77345，order `1001125143685194`，为 77500 的 20 bps 缓冲）。
但随即转为 `conflicted / convergence_pending_alias_conflict`，三档止盈
80200/81000/81700 始终未挂出，`position_take_profit_orders` 中 binding 339 无记录。

判据位于 `trigger_take_profit_convergence_executor.py` 约 503-509 行：
对 `read_complete_pending_tpsl_snapshot(BTC-USDT-SWAP)` 的**每一行**，
若 `_row_has_protection_fields` 为真且 `_native_tpsl_aliases_consistent` 为假，
即整体否决。这是全局否决，不区分该行是否属于目标仓位。

会话2 的方向语义修复本身部署正确（`posSide` 组已移除 `side`，并新增
`protection_order_sides_consistent`），因此失败的是其他别名组。

**未证实假设**：BTC 上存在历史遗留条件单污染该快照。需要 dump 交易所原始返回逐行判定，
该操作需要 worker 凭据，只读 SSH 无法取得。

该仓位其后已平，问题不再紧急，但**下次有持仓时会复现**。

### 2. monitor 因 preamble 16 永久误报（已定性，修复未做）

monitor 唯一原因码 `stale_entry_preamble_unresolved`，触发源唯一：
`entry_preambles.id=16`，群 -1003095914903（欧阳火箭滚仓班 11分组），
BTC short，pending 自 2026-09-05 05:45，该群此后无边界事件收尾。

该群配置为 `ai_strategy_enabled: true` + `trading_mode: notify_only`，
即只识别、从不下单，**用户已确认为有意设置**。该前导对执行安全无任何后果，
判据未按交易范围收敛才导致永久变红，会掩盖真实发现。

修复路径（已核实约束，勿另寻他路）：

- `trading_mode` 只存在于 `groups.yaml`，数据库无任何字段承载；
- monitor 以 `telegram-kol-monitor` 身份运行，`groups.yaml` 为
  `root:telegram-kol-runtime 0640`，**monitor 无权读取**；
  不得通过加组或改权限让其直接读取，那会破坏刻意的凭据隔离；
- monitor 已在调用 `GET http://127.0.0.1:8000/api/trading-settings`，
  而 web 进程持有 `app.state.group_config`，`web_app.py` 约 8263 行已在计算
  `trading_mode == "auto_trade"`。因此由 web 只读暴露自动交易群集合是可行路径。

设计要求：notify_only 群的发现不得直接丢弃，应拆为不影响 healthy 的独立计数；
取集合失败时必须保持现有全量判定并报降级码，不得退化为"全部视为不在范围"。

### 3. 告警量仍偏高

`codex/context-hold-owner-alert` 分支未完成，当前 8.82/天（近期 13.33/天），
需要按候选生命周期集合做时间窗聚合。

### 4. 5 条 uncertain 无对账闭环

id 6 / 25 / 77 / 191 / 199。其中 199 对应 raw 15013，
`error_class=ExecutionBoundaryOutcomeUnknown`、`error_summary=partial_failed`，
16:58:57 起、7 秒后判定。事后核对该仓位仍为 6 张、两张止损均在，半仓平仓未发生；
但 `outcome_unknown` 意味着**不能断言交易所侧完全未受影响**。

这类记录以每天数条的速度累积，恢复闭环（会话3 的设计）尚未实现。

### 5. 账面残留

binding 337 与腿 579 / 580 仍为 `active`，但其 `pos_id`
（1001125123045253 / 1001125126414222）在交易所已不存在。
无资金风险。当前巡检脚本只查"有仓无归属"，不查"有归属无仓"这一反向情形。

### 6. 欧阳群从未产生真实下单（与第 2 条相关，但性质不同）

该群自 2026-07-01 起再无任何 binding（历史仅 3 条，全为 ETH），
其 BTC lifecycle（1044/1051/1064/1073/1089）`execution_binding_id` 全为 null，
即纯纸面跟踪。**用户已确认这是有意设置**，非缺陷。此条仅为避免后续误判而记录。

## 四、Deepcoin API 关键发现

### 已证实

- 入场限价腿当前走 `POST /deepcoin/trade/trigger-order`，
  且 `build_deepcoin_trigger_order_payload` 中 `triggerPrice` 恒等于 `price`，
  即触发语义在入场腿上完全为空，却承担了父子两层身份断裂的代价。
- `_deepcoin_embedded_sltp_fields` 的 docstring 明确：
  "Return stop-only trigger protection; TP waits for exact filled `posId`"。
  即 trigger-order 只嵌止损，止盈刻意等待成交后的确切 posId——这正是卡死处。
- `build_deepcoin_place_order_payload`（普通限价单，`ordType=limit` + `px` + `clOrdId`）
  **已存在于代码中但生产零调用点**，仅有一个测试引用。
- ETH 实验证明 trigger-order 身份不传播：自定义 clOrdId 未传播；
  父 ID `1001125142960934` 与子普通单 `1001125142983995` 不同；
  新 TPSL 既无父单引用也无 posId。
- 普通 `order` 附带 `tpTriggerPx`/`slTriggerPx` **已两次成功复现**：
  pos `1001125143973255`（均价 2472.62，保护单 `1001125143973254`）、
  pos `1001125145471184`（均价 2478.78，保护单 `1001125145471183`）。
  两次保护单 ID 均恰为仓位 ID 减 1。
- 首轮并发双单实验失败：HTTP 200、外层 `code=0`，但内层
  `sCode=14 / sMsg=DuplicateAction / ordId` 为空，无订单落地。
  **外层 code=0 不等于成功**，这与 binding 338 那次"父单带 slTriggerPx 返回 code 0、
  成交后查无保护"是同一类陷阱。

### 未证实

- 保护单 ID 与仓位 ID 相邻（−1）仅 2 个样本，且 **ID 相邻不构成任何归属契约**，
  不得作为关联依据。
- `DuplicateAction` 的根因未定。两个 clOrdId 确实不同且符合官方 1–20 位格式，
  不能据此断定是格式问题或并发必然失败。下一步应单笔、单变量验证。
- 即便切换到普通 order，系统仍把实验仓位判为无法归属，
  说明**归属链路需要单独设计，不是换接口就自动解决**。

## 五、下一主题：REST + WebSocket

用户已确定方向。交接时需明确两点判断：

1. **WebSocket 解决时序，不解决身份。** 它消灭轮询间隔导致的漏事件、
   让触发时刻可观测、大幅收窄 `outcome_unknown` 窗口；
   但若推送本身不带父触发单引用，得到的仍是强启发式而非契约。
   真正创造标识符的是"普通 order 取代 trigger-order"。
   建议：普通 order 是地基，WebSocket 是放大器，两者同做。

2. **WebSocket 事件不得成为真相来源**，只能作为触发既有 REST 精确校验的低延迟信号。
   本系统全部安全文化建立在"没有精确验证就不相信"之上，
   而推送流天然会丢包、重复、乱序、断线。正确形态：
   推送到达 → 触发针对性 REST 核验 → 核验通过才写账本。

配套约束：连接必须活在 worker 内（凭据隔离）；断线重连后必须用 REST 做缺口对账；
需要序列号/心跳的缺口检测。

动手前应先做只读调研，确认 Deepcoin WebSocket 频道清单、鉴权方式、
推送字段（重点：订单推送是否携带 clOrdId 与父触发单引用）、
断线重连与序列保证，并留证。避免重演"文档列了字段、实际返回空"的返工。

## 六、部署工具链约束（交接给非 codex 执行者）

sanctioned 路径为 `scripts/server_git_update.ps1`，stage 与 activate 为两条独立命令，
两者都不启用交易。**本机（macOS）未安装 PowerShell**，
且 `stage-action.json` / `activate-action.json` 不在仓库中，由每次部署现写。

不得以 ssh 手工步骤绕开该 helper：那会丢弃 manifest 校验、ExpectedCommit 绑定、
只允许快进、RollbackCommit 绑定与授权文件分离等全部安全属性。

---

## 七、2026-09-06 变更:停用 monitor 与部署门禁

用户决定:在系统设计冲刺期间停用生产监控与部署门禁,待改动平稳后再重新评估。
本节记录变更后的真实状态,**第六节关于 PowerShell 与 manifest 的描述已作废**。

### 决策依据(用户提出)

Deepcoin 不提供模拟盘,项目只能实盘运行;下单采用以损定量,单笔亏损有界且可接受。
在此前提下,门禁带来的迭代阻力大于其收益。

### 已执行

- `telegram-kol-monitor.timer` 已 `disable --now`,状态 `inactive (dead)`,
  并从 `timers.target.wants` 移除。
- 三个运行角色的 systemd drop-in 已重写为:
  `PYTHONPATH=/opt/telegram-kol-analyzer/src` + `PYTHONDONTWRITEBYTECODE=1`。
  不可变 release 路径、`ReadOnlyPaths`、`ENTRY_FROZEN` 均已移除。
  原 drop-in 备份于 `/root/<role>-dropin.pregate-removal-<ts>`。
- 服务器源仓库切到分支 `live`,当前 HEAD `0335de71`,
  切换前已逐字节比对确认与运行中的 release 源码一致(仅差 `__pycache__`),
  没有发生代码回退。
- 安装 `/usr/local/bin/tg-deploy <git-ref>`:fetch → 硬重置 → 清字节码 →
  按 worker → web → ingest 重启 → 打印 HEAD 与各 PID;`set -e`,失败即中止。

### 刻意保留(这些不是部署门禁)

- `PYTHONDONTWRITEBYTECODE=1`——快速迭代下残留 `.pyc` 会导致运行到不存在的旧代码,
  本项目曾因字节码污染卡过一次激活。成本为零。
- 凭据隔离:Deepcoin 密钥仅在 worker,Telegram session 仅在 ingest。这是安全边界。
- 只读保护巡检 `protcheck`(30 分钟一次):monitor 停用后,
  这是唯一持续回答"是否存在系统无法管理的实盘仓位"的机制,且无误报记录。
- 40 个历史 release(875MB)未删除,回退路径仍在。

### 前提条件与已知风险

"以损定量,亏损有界"成立的前提是**止损确实挂上**。该环节历史上并不稳定:
binding 338 的父单带 `slTriggerPx` 返回 `code 0`,成交后查无任何可验证保护;
leg 583 主止损在而三档止盈始终挂不上;当前仍有 5 条 `uncertain` 结果未知。
止损未挂时,亏损边界不是设定值而是爆仓价。保留 protcheck 的唯一理由即在于此。

### monitor 的设计缺陷(停用前已定位,复用时必须先修)

生产中出现过 16 种 `execution_events.status`,monitor 白名单只认 8 种;
其余 9 种(含 `completed`、`manual_review`、`reserved`)一律触发 critical 告警
「交易请求已经发出,但交易所结果无法确认」——对 `completed` 而言语义完全相反,
对 `manual_review`(门禁在提交前拒绝)而言根本没有向交易所发出任何请求。

根因是封闭世界白名单模型:每新增一个终态,默认即被判为 critical。
代码中已存在两处针对该问题的硬编码例外,属于打补丁而非改模型。
叠加 `healthy = not reason_codes` 的扁平布尔(40 个原因码不分级)与缺失的
"交易相关性"维度,导致常亮红与 8.82 条/天的告警量。

重新启用前应先做三件事,而非逐个补判据:
未识别状态默认降为观测而非 critical;引入 critical / degraded / observation 分级;
把"是否存在无法管理的实盘仓位"提升为主判据。
