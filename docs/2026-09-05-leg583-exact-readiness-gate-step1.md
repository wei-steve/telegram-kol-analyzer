# Leg 583 / convergence 227：第一步紧急只读结论

**结论：路径 A 全部通过；路径 B 因把止损的买卖方向 side 当成仓位方向 posSide 的别名，误判冲突，过滤掉已归属且已验证的主止损。当前代码与交易所响应形态不变，227 不会因时间经过或周期重查自行转 ready。无需等待 lineage live 切换：它不解除这个过滤。**

现场窗口：2026-09-05 07:36:41.856466–07:36:44.851168 UTC（北京时间 15:36:41–15:36:44）。生产身份于 07:34:46 UTC 回读，release=`9501a5f39f0c5f196cc29f24f3e3b8786267126b`、loaded_artifact_verified=true。本次诊断直接导入此 immutable release，未从工作树导入。

## 1. 原始持仓全部字段及路径 A

list_positions 原始返回只有以下一行；该列表也就是本次传给纯校验函数的 open_positions。没有把其他手动 ETH merge 仓位混入目标。

```json
{
  "instType": "SWAP",
  "mgnMode": "cross",
  "instId": "BTC-USDT-SWAP",
  "posId": "1001125135694798",
  "posSide": "long",
  "pos": "6",
  "avgPx": "79519",
  "lever": "125",
  "liqPx": "32538.5",
  "useMargin": "3.816912",
  "unrealizedProfit": "1.029600000000035",
  "lastPx": "79690.6",
  "tpTriggerPx": "",
  "slTriggerPx": "77500",
  "mrgPosition": "split",
  "isLeading": false,
  "isFollow": false,
  "ccy": "USDT",
  "uTime": "1788577607000",
  "cTime": "1788577607000"
}
```

前提：binding 339 存在，symbol=BTC、side=long、position_mode=split、margin_mode=cross；leg 583 存在，order_kind=market、status=active、attribution_status=verified、pos_id=1001125135694798。

| 路径 A 条件 | 实际值 | 判定 |
| --- | --- | --- |
| instId == BTC-USDT-SWAP | BTC-USDT-SWAP | 通过 |
| posId == leg.pos_id | 两端均为 1001125135694798 | 通过 |
| posSide/side == binding.side | long == long | 通过 |
| mrgPosition/posMode 小写值 == split | mrgPosition=split | 通过 |
| _positive_position_size 非空 | Decimal("6") | 通过 |

匹配数为 **1**。无 binding / 无 pos_id 分支均不成立。convergence 227 的 pos_id 当前是 null，这是就绪函数写入前的状态，不等于 leg.pos_id 缺失；其前置冲突判断允许 None。

## 2. 路径 B 的真实输入

使用与生产调用点完全相同的 stop_rows 查询条件：binding_id=339、leg_id=583、pos_id=1001125135694798、status=verified、purpose in (stop_loss,combined)。实际查到 **1 行**：

| 字段 | 实际值 |
| --- | --- |
| id | 659 |
| execution_binding_id / execution_order_leg_id | 339 / 583 |
| pos_id | 1001125135694798 |
| order_id | 1001125135694875 |
| purpose | stop_loss |
| trigger_price | 77500.0 |
| status | verified |
| evidence_source | entry_protection_response |
| evidence_json | {"match":"exchange_returned_order_id_exact_readback"} |

position 为上面完整原始行；open_positions 为仅包含该行的完整原始列表；position_size 为 Decimal("6")。

BTC read_trigger_orders_pending 原始响应 code=0，data 共 **3 行**，全部保存在证据中：

| ordId | triggerOrderType | posSide / side | triggerPx / ordPx | sz | 止损价格字段 |
| --- | --- | --- | --- | --- | --- |
| 1001125122023458 | Conditional | short / sell | 76410 / 76410 | 24 | closeSLTriggerPrice=76000 |
| 1001125135694875 | TPSL | long / sell | 0 / 0 | 0 | slTriggerPrice=77500、closeSLTriggerPrice=77500 |
| 1001125135694951 | Conditional | long / buy | 78290 / 78290 | 8 | closeSLTriggerPrice=77500 |

主止损完整原始行：

```json
{
  "instType": "SWAP",
  "instId": "BTC-USDT-SWAP",
  "ordId": "1001125135694875",
  "triggerPx": "0",
  "ordPx": "0",
  "sz": "0",
  "ordType": "",
  "side": "sell",
  "posSide": "long",
  "tdMode": "cross",
  "triggerOrderType": "TPSL",
  "triggerPxType": "last",
  "lever": "125",
  "slPrice": "0",
  "slTriggerPrice": "77500",
  "tpPrice": "0",
  "tpTriggerPrice": "0",
  "closeSLPrice": "0",
  "closeSLTriggerPrice": "77500",
  "closeTPPrice": "0",
  "closeTPTriggerPrice": "0",
  "cTime": "1788577608000",
  "uTime": "1788577608000"
}
```

list_open_orders 全账户原始返回 []。备用止损查询 active 且非空 order_id 的精确本腿/本仓位记录为 **0 行**。备用不是必须条件，只是主止损失败后的另一个 OR 分支；主止损直接通过本可放行。

## 3. 精确返回点与纯函数复算

以原始响应和只读 ORM Session 调用已安装 release 的校验函数，结果如下；**没有调用 _ready_verified_trigger_take_profit_convergences 或任何会写任务状态/提交订单的执行函数**。

1. `exact_owned_stop_evidence_fingerprint`，`trigger_take_profit_convergence_executor.py:951`，先调用 `has_verified_exact_owned_stop`。
2. 主止损分支进入 `_verified_native_primary_stop_row`（约 :1050），先用 `_native_tpsl_aliases_consistent` 过滤 pending。
3. `_native_tpsl_aliases_consistent`（:1264）把 `posSide,pos_side,side` 放在同一 text_group，统一调用 `_normalize_position_side_alias`（约 :1288）。
4. 对真实主止损行：`posSide=long → long`，`side=sell → short`，集合是 **{long,short}**，长度 2，违反 `<= 1`。该行被过滤，`aliases_consistent=False`。
5. 主止损匹配分别尝试期望数量 6 与 0。过滤后的列表里已无 ordId=1001125135694875，两次均为 **not_found**；不是 ledger 查询为空，不是止损不存在，也不是全仓 sz=0 不被支持。
6. 主止损分支 False；备用分支没有 active 本腿记录，返回 False。于是 `has_verified_exact_owned_stop=False`，fingerprint 在第一个 `if not ...: return None` 返回。
7. 调用点进入路径 B，继续写入 waiting_backup_stop / convergence_waiting_backup_stop，不能进入 mark_ready。

同一原始行的其他文本组：ordId 仅一个值、instId 仅一个值、posId 别名集合为空（该函数允许 0 个）、triggerOrderType 仅 TPSL；这些不构成本次失败。

为隔离根因，另外做了**仅内存诊断的对照调用**，不修改任何生产函数或输入字段：将原始主止损直接传给 match_native_tpsl_order，数量 6 得 mismatch，数量 **0 得 verified**。这证明造成这次失败的是前置方向别名过滤；原始全仓止损在现有 native matcher 的数量 0 分支可以通过。此对照不是启用绕过，更没有持久化 ready。

## 4. 227 能否自行 ready，以及需要什么变化

当前 227 仍为 waiting_backup_stop，reason_code=convergence_waiting_backup_stop，reserved_at=null、request_json=null；updated_at=2026-09-05 07:36:33.481143。本次证据没有提交止盈，普通 pending 为空。

**在现有代码和该正常平多止损响应形态持续存在的情况下，周期重查无法解决。** 价格涨跌、等待更久、重复写入 ledger verified、补一个相同语义的订单 ID，都不会消除 long 与 sell 被当作同义字段比较的冲突。

理论上：交易所不再返回 side、返回不同的字段形态，或者校验代码得到单独批准修正，才可能令主止损分支通过。不能期待交易所把平多止损 side 改为 buy；那是另一种交易方向。当前没有证据表明 API 会自行改变响应格式。新备用止损若也返回同一组合，仍会受同一个 aliases filter 影响；而当前备用分支还要求额外精确字段，不能将“未来可能有备用”当成可依赖的自愈路径。

就所有者当前决策而言：**不能把等待任务 227 自动挂出 80200 那档止盈作为可靠安排。** 本报告给出系统能力判断，不执行或替所有者决定手工挂单。

## 5. 与 predates_fill / lineage live 的关系

两者不是同一失败点：

- 会话 4 证据证明另一张测试单的 TPSL 先于入场成交可见；那涉及扫描候选的时间排除与血缘认领。
- 本腿 order_kind=market；ledger 659 已通过 entry_protection_response 和 exchange_returned_order_id_exact_readback 获得归属。此处不是重新扫描认领未知止损。
- 当前失败链 `_native_tpsl_aliases_consistent → primary stop False → fingerprint None` 没有调用 candidate_predates_fill，也不读取 lineage 的 live/shadow 开关。
- 因此**只把 lineage attribution 切到 live，不能连带解除 227 的这个方向过滤**。归属结果已存在，但另一道执行前重新验证把真实回读丢弃了。
- 本轮不改变门禁。将订单 side 与仓位 posSide 作为同义别名检查是否合理，需要另行修复交易语义层面的验证；本次第二步授权仅限原因码和可观测性，不能偷偷修掉这个条件。

## 6. 第二步交接边界

第二步本轮尚未实施。当前会话主要承载多轮原始采集；为先交付紧急结论，并避免在未掌握完整测试及持久化契约时修改生产门禁，将实现留给有完整上下文的后续会话。本报告不宣称已经完成 RED、全套 pytest 或独立评审。

接手时必须保留当前 A/B 判定结果与 waiting_backup_stop 状态语义，仅细化 reason_code 与最小诊断字段：

- A：binding 缺失、leg.pos_id 缺失、匹配数异常，以及 instId/posId/side/mode/positive_size 的失败集合。
- B：先覆盖本例 primary_stop_alias_conflict；最小字段至少 ledger/order ID、冲突字段组、posSide、side、期望仓位方向、匹配结果（如 not_found）和 active backup 数量，不能只写“缺备用”。其他内部失败点同样需要覆盖。
- 本例作为 B 的真实 fixture：posSide=long、side=sell、sz=0、ledger verified；在未改变语义的改动里仍须保持 not-ready，只把原因写准确。
- RED 先证明 A/B 当前原因码相同，之后实现区分；最终候选完整 pytest 一次及独立评审。不得改 side 判断、放宽门禁或部署。

## 7. 证据与安全

服务端目录：`/var/lib/telegram-kol-cutover-evidence/leg583-exact-gate-20260905T0735Z`。

- list_positions.json：原始完整列表及请求起止时间。
- read_trigger_orders_pending.json：原始 code/msg/data 全包及起止时间。
- list_open_orders.json：原始普通 pending 列表。
- gate-evaluation.json：A 各条件、完整只读 DB 对象、B 精确输入、函数结果与诊断对照。
- manifest.json：上述证据文件 SHA-256。

manifest SHA-256：`4c4ce6aedfa98a6f920db6e3e89e664a8475870293855da40a262af0881ddc11`。

所有交易所调用为现成客户端读取方法，调用 uid=989（telegram-kol-worker），root 读取凭据后立即降权；父进程不持有凭据，仅保存过滤鉴权字段后的结果。脚本 stdin 执行，未落盘临时脚本；子进程已回收。所有 import 使用 python -B；数据库 sqlite mode=ro + PRAGMA query_only=ON，实测 query_only=1，Session autoflush=False；未做建表或更新。

未改任何生产代码、数据库行、schema、设置、服务或订单；未处置 execution_running/execution_uncertain 或其他 waiting/conflicted 任务。仅新增此本地诊断文档及服务端证据。
