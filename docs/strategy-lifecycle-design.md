# 策略生命周期持续跟踪与 Web 界面改造 设计方案 v2

> **核心洞察**：KOL 给出策略 ≠ 立即入场。入场/离场的判断需要**跨时间的持续跟踪**，不能仅在消息识别时一次性判定。

---

## 一、现状深度分析

### 1.1 当前系统已经做了什么

```
KOL消息 → AI识别/规则解析 → SignalCandidate
  ↓
recovery_scan (一次性评估)：
  ├─ 检查 entry_range 是否被历史K线触及 (_entry_range_was_touched)
  ├─ 检查当前价格是否在 entry_range 中 (_price_in_range)
  └─ 生成 RecoveryDecisionRecord (eligible / manual_review / skip)
  ↓
recovery_execution_queue：
  └─ 展示 approved_for_order 的决策 → 用户在 Web UI 确认 → Deepcoin 下单
  ↓
execution_bindings（Deepcoin 实际持仓绑定）
  └─ list_active_positions()：查询 ExecutionBinding(open/active) + TradeIdea(open)
```

### 1.2 当前系统的关键缺口

| 缺口 | 说明 |
|------|------|
| **无持续价格监控** | recovery_scan 是"一次性快照"，不会持续跟踪价格是否触及入场区间 |
| **入场判断滞后** | 只有 recovery_scan 运行时才检查，如果价格在两次 scan 之间触及入场区间又离开，可能漏判 |
| **无止盈止损监控** | 入场后没有自动检测 SL/TP 是否被触发 |
| **无离场状态追踪** | KOL 发出离场信号后，没有机制将其与已有持仓关联并标记为"已离场" |
| **无过期机制** | 入场信号如果 3 天都没触及，应该自动标记为"已过期" |
| **Web UI 缺少已离场视图** | 只有"已入场"和"待入场"两个面板 |

### 1.3 为什么"仅在消息识别时判断入场/离场"不够

```
场景举例：
  10:00  KOL 发消息："BTC 62000-62200 做多，SL 61000，TP 64000"
        → AI 识别：是策略，entry_signal
        → 此时 BTC 价格 = 63000，高于入场区间
        → 状态应为：待入场（等回调到 62000-62200）
  
  14:00  BTC 价格跌到 62100，触及入场区间
        → 系统应该：待入场 → 持有中
        → 但当前系统没有持续监控，这个状态转换不会发生
  
  16:00  BTC 跌到 60900，触及止损
        → 系统应该：持有中 → 已离场（止损）
        → 同样无法自动检测
  
  18:00  KOL 发消息："BTC 多单止盈了"
        → AI 识别：exit_signal
        → 系统应该匹配到之前的持仓并标记为已离场
```

**结论：入场/离场的判断需要一个跨时间的持续跟踪层，而不是消息识别时的瞬时判断。**

---

## 二、策略生命周期状态机

### 2.1 状态定义

```
                    ┌──────────────┐
                    │  pending_entry│  待入场：信号已识别，等价格触及入场区间
                    │   (待入场)    │
                    └──────┬───────┘
                           │
            ┌──────────────┼──────────────┐
            │ 价格触及入场  │ 超时未触及    │ KOL取消/新信号覆盖
            ▼              ▼              ▼
     ┌──────────┐  ┌──────────┐  ┌──────────┐
     │ entered  │  │ expired  │  │cancelled │
     │ (持有中) │  │ (已过期)  │  │ (已取消)  │
     └────┬─────┘  └──────────┘  └──────────┘
          │
    ┌─────┼─────────────┬──────────────┐
    │止损触发 │止盈触发   │KOL离场信号   │手动平仓
    ▼         ▼          ▼              ▼
┌────────┐┌────────┐┌──────────┐┌──────────┐
│exited  ││exited  ││exited    ││exited    │
│_loss   ││_profit ││_signal   ││_manual   │
│(止损)  ││(止盈)  ││(KOL信号) ││(手动)    │
└────────┘└────────┘└──────────┘└──────────┘
```

### 2.2 状态转换触发条件

| 转换 | 触发条件 | 检测方式 |
|------|---------|---------|
| `pending_entry → entered` | 当前价 / 最新 K 线触达入场区间 | **持续价格监控** |
| `pending_entry → entered` | Deepcoin 上出现匹配的持仓 | **交易所仓位同步** |
| `pending_entry → entered` | 用户在 Web UI 确认"已入场" | **手动触发** |
| `pending_entry → expired` | 超过 N 小时未触及入场 | **定时扫描** |
| `pending_entry → cancelled` | KOL 新消息明确取消 / 反向开仓 | **消息识别** |
| `entered → exited_loss` | 价格触及止损价 | **持续价格监控** |
| `entered → exited_profit` | 价格触及止盈价 | **持续价格监控** |
| `entered → exited_signal` | KOL 发出离场信号 | **消息识别 → 匹配持仓** |
| `entered → exited_manual` | Deepcoin 上仓位已平 | **交易所仓位同步** |

---

## 三、方案设计

### 方案 1：定时轮询监控器（推荐 ✅）

#### 思路

在现有 `asyncio` 任务基础上，新增一个 **策略生命周期监控器**（`LifecycleMonitor`），每 60 秒运行一次：

1. 扫描所有 `pending_entry` 信号，查当前价格，判断是否触及入场区间
2. 扫描所有 `entered` 持仓，查当前价格，判断是否触及 SL/TP
3. 扫描所有超时未入场的信号，标记为 `expired`
4. 状态变更通过 SSE 推送到 Web UI

#### 架构

```
┌──────────────────────────────────────────────────┐
│                   Web App (FastAPI)               │
│                                                    │
│  ┌─────────────┐  ┌─────────────────────────────┐ │
│  │LiveListener │  │ LifecycleMonitor (NEW)       │ │
│  │ (已有)       │  │  asyncio.Task, every 60s     │ │
│  │ 消息同步     │  │                              │ │
│  └─────────────┘  │ 1. fetch_price(symbols)      │ │
│                    │ 2. check_entry_triggers()    │ │
│  ┌─────────────┐  │ 3. check_sl_tp_triggers()    │ │
│  │Reconcile    │  │ 4. check_expiry()            │ │
│  │ (已有)       │  │ 5. SSE broadcast changes     │ │
│  │ 定期同步     │  └─────────────────────────────┘ │
│  └─────────────┘                                    │
│                                                    │
│  ┌──────────────────────────────────────────────┐ │
│  │         LiveUpdateBroker (已有, SSE)           │ │
│  │  lifecycle_status_changed → push to Web UI   │ │
│  └──────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────┘
```

#### 新增数据模型

```python
# 策略生命周期追踪表
class StrategyLifecycle(Base):
    __tablename__ = "strategy_lifecycles"

    id: int (PK)
    signal_candidate_id: int (FK → signal_candidates.id, unique)
    chat_id: int
    message_id: int
    symbol: str
    side: str

    # 核心状态
    lifecycle_status: str  # pending_entry | entered | exited | expired | cancelled
    exit_reason: str | None  # stop_loss | take_profit | kol_signal | manual | expired

    # 关键时间戳
    signal_at: datetime      # KOL 发出信号的时间
    entered_at: datetime | None
    exited_at: datetime | None
    expired_at: datetime | None
    last_checked_at: datetime | None

    # 入场/离场价格快照
    entry_range_low: float | None
    entry_range_high: float | None
    stop_loss: float | None
    take_profit: str | None  # JSON: [tp1, tp2, ...]
    entry_price_actual: float | None  # 实际入场价（来自交易所或估算）
    exit_price_actual: float | None

    # 关联
    execution_binding_id: int | None (FK)
    trade_idea_id: int | None (FK)
    exit_signal_candidate_id: int | None (FK, 离场信号)
```

#### 监控器核心逻辑

```python
class LifecycleMonitor:
    """策略生命周期持续跟踪器"""

    async def run_cycle(self):
        prices = await self._fetch_current_prices(all_tracked_symbols)

        # 1. 待入场 → 持有中？
        pending = self._load_pending_entry_signals()
        for signal in pending:
            current_price = prices.get(signal.symbol)
            if self._price_in_range(current_price, signal.entry_range):
                self._transition_to_entered(signal, current_price)

        # 2. 持有中 → 已离场（止损/止盈）？
        entered = self._load_entered_positions()
        for pos in entered:
            current_price = prices.get(pos.symbol)
            if self._stop_loss_hit(current_price, pos):
                self._transition_to_exited(pos, "stop_loss", current_price)
            elif self._take_profit_hit(current_price, pos):
                self._transition_to_exited(pos, "take_profit", current_price)

        # 3. 待入场 → 已过期？
        expired = self._find_expired_signals(max_age_hours=72)
        for signal in expired:
            self._transition_to_exited(signal, "expired")

        # 4. 推送状态变更
        self._broadcast_changes()
```

#### 触发时机

| 触发源 | 频率 | 说明 |
|--------|------|------|
| 定时轮询 | 每 60 秒 | `LifecycleMonitor.run_cycle()` |
| KOL 新消息到达 | 实时 | `persist_live_message_event()` 后触发检查 |
| 用户点击"刷新策略" | 按需 | Web UI 按钮 → `POST /api/recovery-dry-run` |
| Web UI 页面加载 | 按需 | `GET /` 时同步查询最新状态 |

#### 优点

1. **持续跟踪**：价格触及入场/SL/TP 能在 60 秒内检测到
2. **与现有架构融合**：复用 `asyncio.Task`、`LiveUpdateBroker`(SSE)、`GateMarketDataProvider`
3. **实现成本可控**：约 400-600 行新代码，不改变现有消息识别流程
4. **可降级**：如果价格 API 挂了，状态不会错误变更，只是暂停更新
5. **可扩展**：新增监控条件只需加 checker 函数

#### 缺点

1. **60 秒延迟**：极端行情下可能错过快速涨跌
2. **价格 API 调用频率**：每个 symbol 每 60 秒一次，免费 API 可能有频率限制
3. **tick 级别精度无法保证**：只能检测到"曾经触及"，不能精确到具体成交

---

### 方案 2：WebSocket 实时价格推送

#### 思路

订阅 Binance/Gate WebSocket 实时价格，价格每变化一次就触发检查。最灵敏但也最复杂。

#### 架构

```
Binance/Gate WebSocket
  ↓ (实时tick)
PriceFeedManager (NEW)
  ├─ 维护内存中的 symbol→price 映射
  ├─ 价格变化时触发 callback
  └─ callback 调用 LifecycleChecker
  ↓
LifecycleChecker (轻量)
  ├─ 只检查价格变化的 symbol 关联的信号
  └─ 状态变更 → SSE 推送
```

#### 优点

1. **实时性最高**：价格变化即刻检测
2. **精确**：可以记录实际触发价格
3. **API 调用少**：不需要轮询 REST API

#### 缺点

1. **实现复杂度高**：需要管理 WebSocket 连接、重连、心跳
2. **资源消耗**：24/7 维持 WebSocket 连接
3. **过度设计**：对于分钟级别的 KOL 策略跟踪，60 秒轮询已经足够
4. **依赖外部库**：可能需要 `websockets` 或 `binance-connector`

---

### 方案 3：事件驱动 + 惰性评估（最轻量）

#### 思路

不做持续价格监控。只在以下**事件发生时**才评估状态：

1. KOL 新消息到达 → 检查是否匹配已有信号（入场确认/离场信号）
2. Web UI 页面加载 → 获取当前价格，评估所有信号状态
3. 用户点击"刷新策略" → 同上
4. 定期 reconcile（已有，5 分钟一次）→ 同时做一次状态评估

#### 优点

1. **零额外成本**：不需要新增后台任务，不增加 API 调用
2. **实现最简单**：在现有流程上挂载评估逻辑即可
3. **足够应对大部分场景**：KOL 策略通常是小时级别，几分钟的延迟可以接受

#### 缺点

1. **状态可能长时间不更新**：如果用户不打开页面、没有新消息，状态就一直是旧的
2. **无法及时检测 SL/TP 触发**：用户可能在几小时后打开页面才发现已经止损了
3. **不适合快速行情**：如果 BTC 在 5 分钟内暴跌触及止损又反弹，可能完全检测不到

---

## 四、方案对比

| 维度 | 方案1：定时轮询 | 方案2：WebSocket | 方案3：惰性评估 |
|------|:---:|:---:|:---:|
| **实时性** | ⭐⭐⭐⭐ 60s 延迟 | ⭐⭐⭐⭐⭐ tick级 | ⭐⭐ 分钟~小时 |
| **实现复杂度** | ⭐⭐⭐ 中等 | ⭐ 高 | ⭐⭐⭐⭐⭐ 最低 |
| **API 调用成本** | ~每symbol/分钟 | 极低（WS） | 极低（按需） |
| **可靠性** | ⭐⭐⭐⭐ 可降级 | ⭐⭐⭐ 需处理断线 | ⭐⭐⭐⭐⭐ 无状态 |
| **SL/TP 检测** | ⭐⭐⭐⭐ 60s内 | ⭐⭐⭐⭐⭐ 即时 | ⭐ 可能漏检 |
| **代码量估计** | ~500行 | ~800行 | ~200行 |
| **适合本场景** | ✅ 推荐 | 过度设计 | 作为兜底 |

---

## 五、推荐方案：方案1（定时轮询）+ 方案3（事件触发）混合

### 5.1 双层监控模型

```
Layer 1: 事件触发（实时，低延迟）
  ├─ KOL新消息 → 立即检查相关信号
  ├─ Web UI刷新 → 获取当前价格并评估
  └─ reconcile完成 → 评估新消息关联的信号

Layer 2: 定时轮询（准实时，60s 间隔）
  ├─ 检查所有 pending_entry → 入场触发
  ├─ 检查所有 entered → SL/TP 触发
  └─ 检查过期信号 → 标记 expired
```

### 5.2 具体实施步骤

#### Step 1: 新增 `StrategyLifecycle` 数据模型

在 `models.py` 中新增上述 lifecycle 表。同时在 `SignalCandidate` 创建时自动创建对应的 lifecycle 记录。

#### Step 2: 创建 `lifecycle_monitor.py`

```python
# 核心模块：src/telegram_kol_research/lifecycle_monitor.py

class LifecycleMonitor:
    def __init__(self, session_factory, market_data_provider, broker):
        ...

    async def run_cycle(self):
        """定时执行一次完整的状态检查"""
        ...

    async def run_loop(self, interval_seconds=60):
        """后台循环"""
        while True:
            await self.run_cycle()
            await asyncio.sleep(interval_seconds)

    def on_new_message(self, raw_message_id):
        """当新消息到达时触发（事件驱动）"""
        # 检查新消息是否为已有信号的离场确认
        # 检查新消息是否覆盖/取消了某个待入场信号
        ...

    def evaluate_on_demand(self, chat_id=None):
        """Web UI 触发的按需评估"""
        ...
```

#### Step 3: 修改 `web_app.py`

- 在 `lifespan` 中启动 `LifecycleMonitor.run_loop()` 作为后台任务
- SSE 事件类型增加 `lifecycle_status_changed`
- `_build_trader_dashboard_state()` 增加 `holding_count`、`exited_count`、`exited_positions`

#### Step 4: 修改 `index.html`

- KPI 三栏：持有中 / 待入场 / 已离场
- 新增"已离场策略"面板（展示最近 7 天离场记录）
- 每个策略卡片显示当前 lifecycle 状态 + 最后检查时间

#### Step 5: 修改 `message_recognition.py`

- AI prompt 增加 `strategy_kind` 输出（entry / exit / position_mgmt）
- entry → 创建 SignalCandidate + StrategyLifecycle(pending_entry)
- exit → 查找匹配的 StrategyLifecycle(entered) → 触发离场
- position_mgmt → 更新已有 lifecycle 的保护价/止盈参数

#### Step 6: 新增 `list_exited_strategies()` 查询

从 `StrategyLifecycle` + `ExecutionBinding` + `TradeIdea` 中查询已离场策略。

### 5.3 最终数据流全景

```
┌─────────────────────────────────────────────────────────┐
│                    KOL 新消息到达                         │
│                         ↓                                │
│  ┌──────────────────────────────────────────────────┐   │
│  │ AI 识别 (DeepSeek / GLM-OCR → DeepSeek)          │   │
│  │  输出: recognition_result, strategy_kind, fields  │   │
│  └────────────────────┬─────────────────────────────┘   │
│                       ↓                                  │
│  ┌────────────┬──────────────┬─────────────────────┐    │
│  │ entry      │ exit         │ position_mgmt       │    │
│  │ 创建       │ 匹配已有     │ 更新已有            │    │
│  │ Signal +   │ Lifecycle →  │ Lifecycle 的        │    │
│  │ Lifecycle  │ exited_signal│ SL/TP 参数           │    │
│  │ (pending)  │              │                     │    │
│  └─────┬──────┴──────┬───────┴─────────────────────┘    │
│        │             │                                    │
├────────┼─────────────┼────────────────────────────────────┤
│        ↓             ↓                                    │
│  ┌──────────────────────────────────────────────────┐   │
│  │         LifecycleMonitor (持续跟踪)                │   │
│  │                                                    │   │
│  │  每60秒:                                           │   │
│  │    pending → check_entry_trigger()                │   │
│  │    entered → check_sl_tp_trigger()                │   │
│  │    pending → check_expiry()                       │   │
│  │                                                    │   │
│  │  实时触发 (事件):                                   │   │
│  │    KOL新消息 → on_new_message()                   │   │
│  │    Web UI刷新 → evaluate_on_demand()              │   │
│  └────────────────────┬─────────────────────────────┘   │
│                       ↓                                  │
│  ┌──────────────────────────────────────────────────┐   │
│  │         LiveUpdateBroker (SSE)                    │   │
│  │   lifecycle_status_changed → push to Web         │   │
│  └──────────────────────────────────────────────────┘   │
│                       ↓                                  │
│  ┌──────────────────────────────────────────────────┐   │
│  │              Web UI 三面板                         │   │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────────────┐  │   │
│  │  │持有中策略 │ │待入场策略 │ │已离场策略         │  │   │
│  │  │ (entered)│ │(pending) │ │(exited/expired)  │  │   │
│  │  │ SL/TP    │ │ 入场区间  │ │ 离场原因+盈亏     │  │   │
│  │  │ 实时监控 │ │ 等待触及  │ │ 时间+最终结果    │  │   │
│  │  └──────────┘ └──────────┘ └──────────────────┘  │   │
│  └──────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
```

### 5.4 Web UI 改造细节

```
┌──────────────────────────────────────────────────────┐
│  交易执行台                                           │
│  ┌──────────┐ ┌──────────┐ ┌──────────────────┐     │
│  │ 持有中   │ │ 待入场   │ │ 已离场           │     │
│  │   3      │ │   5      │ │   12 (本周)      │     │
│  └──────────┘ └──────────┘ └──────────────────┘     │
│                                                       │
│  ┌ 持有中策略 ──────────────────────────────────┐    │
│  │ BTC long | 入场 62100 | SL 61000 | TP 64000  │    │
│  │ 当前价 62500 | 浮盈 +0.6% | 入场时间 14:00   │    │
│  │ 来源：KOL A | 状态监控中 ● (绿色脉冲)         │    │
│  ├──────────────────────────────────────────────┤    │
│  │ ETH short | 入场 3400 | SL 3500 | TP 3200    │    │
│  │ 当前价 3350 | 浮盈 +1.5% | 入场时间 12:30    │    │
│  │ 来源：KOL B | 状态监控中 ●                    │    │
│  └──────────────────────────────────────────────┘    │
│                                                       │
│  ┌ 待入场策略 ──────────────────────────────────┐    │
│  │ SOL long | 入场区间 140-145 | SL 130 | TP 160 │    │
│  │ 当前价 152（高于入场区间）| 等待回调            │    │
│  │ 信号时间 10:00 | 已等待 5h | ⏳ 等待价格触及   │    │
│  ├──────────────────────────────────────────────┤    │
│  │ DOGE long | 入场区间 0.12-0.13 | 当前价 0.125 │    │
│  │ ✅ 当前价已进入入场区间！可入场                │    │
│  │ 信号时间 15:00 | [确认入场] 按钮               │    │
│  └──────────────────────────────────────────────┘    │
│                                                       │
│  ┌ 已离场策略（最近7天）────────────────────────┐    │
│  │ BTC long | 离场原因：止盈 | +3.2%              │    │
│  │ 入场 62100 → 出场 64100 | 持有 6h              │    │
│  ├──────────────────────────────────────────────┤    │
│  │ ETH short | 离场原因：止损 | -1.8%             │    │
│  │ 入场 3400 → 出场 3460 | 持有 2h               │    │
│  └──────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────┘
```

---

## 六、实施路线图

### Phase 1: 数据模型 + Web UI（1-2天）

- [ ] 新增 `StrategyLifecycle` 表
- [ ] 新增 `list_exited_strategies()` 查询
- [ ] Web UI KPI 改为三栏
- [ ] 新增"已离场策略"面板
- [ ] `_build_trader_dashboard_state()` 接入新数据

### Phase 2: 生命周期监控器（2-3天）

- [ ] 创建 `lifecycle_monitor.py`
- [ ] 实现 `check_entry_trigger()` / `check_sl_tp_trigger()` / `check_expiry()`
- [ ] 在 `web_app.py` lifespan 中启动后台监控
- [ ] SSE 事件 `lifecycle_status_changed`
- [ ] 事件驱动触发：KOL新消息 → `on_new_message()`

### Phase 3: AI Prompt 增强（1天）

- [ ] `DEFAULT_RECOGNITION_PROMPT` 增加 `strategy_kind`
- [ ] `_result_from_ai_payload()` 解析 `strategy_kind`
- [ ] `exit_signal` → 自动匹配并触发已离场转换
- [ ] 降级到本地规则引擎的兜底逻辑

### Phase 4: 优化 + 测试（1-2天）

- [ ] 入口/止损/止盈触发的单元测试
- [ ] 过期逻辑测试
- [ ] SSE 推送端到端测试
- [ ] KOL 离场信号匹配测试

---

## 七、风险与缓解

| 风险 | 缓解 |
|------|------|
| 价格 API 限频 | 缓存当前价格，同一 symbol 60s 内不重复请求；使用 Gate 免费 API（频率较高） |
| 入场区间模糊（KOL说"62000附近"） | AI prompt 要求给出数值区间；无区间时标记为 manual_review |
| KOL 离场信号匹配失败 | 按 chat_id + symbol + side 匹配，匹配不到时在 UI 展示"未匹配离场信号" |
| 市场剧烈波动导致误判 | 检查 K 线高低价而非仅收盘价；过滤瞬时插针（可选） |
| 旧数据无 lifecycle 记录 | 提供迁移脚本从 SignalCandidate + ExecutionBinding 重建 lifecycle |
