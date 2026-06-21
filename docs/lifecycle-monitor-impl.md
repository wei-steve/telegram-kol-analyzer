# Layer 2 定时轮询监控器 — 最终实现规格

> **核心优化**：按合约分组 → 找最旧信号时间 → 一次性拉取 K 线 → 按时间顺序逐根遍历比对。

---

## 一、为什么按合约分组是关键

### 1.1 合约 vs Symbol

```
"BTC"   → Gate 合约名 "BTC_USDT"
"ETH"   → Gate 合约名 "ETH_USDT"  
"SOL"   → Gate 合约名 "SOL_USDT"
```

同一个合约 `BTC_USDT` 可能对应：
- KOL A 的 BTC long 信号（10:00 发出，入场 62000-62200）
- KOL B 的 BTC short 信号（12:00 发出，入场 64000-64500）
- KOL A 的 BTC long 信号（14:00 发出，入场 61000-61500）

这三个信号共享同一份 K 线数据。如果分别拉取，就是 3 次 HTTP 请求。按合约分组后：**1 次 HTTP 请求**。

### 1.2 时间跨度：找最旧信号

```
BTC_USDT 的 pending/entered 信号：
  ├─ 信号1: signal_at = 2024-01-15 10:00（3天前）
  ├─ 信号2: signal_at = 2024-01-17 14:00（6小时前）
  └─ 信号3: signal_at = 2024-01-18 09:00（1小时前）

→ 最旧时间 = 2024-01-15 10:00
→ 拉取 K 线：from=2024-01-15 10:00, to=now
```

### 1.3 API 调用量对比

| 方案 | 20 个信号，5 个合约 | HTTP 请求数 |
|------|-------------------|------------|
| 旧方案（每 signal 独立） | 各有不同 since | 最多 20 次 |
| 按 symbol 去重 | 5 个 symbol | 5 次 |
| **按合约分组+最旧时间** | **5 个合约** | **5 次** |

实际上是 K 线查询的绝对最优解：**C 次请求，C = 不同合约数**。

---

## 二、时间顺序遍历：核心算法

### 2.1 基本思路

```
对每个合约 contract（如 BTC_USDT）：

  Step 1: 找出该合约所有 pending / entered 信号
  Step 2: 找出最早的 signal_at → since
  Step 3: 调 Gate API 拉取 since→now 的 1m K 线
  Step 4: 按 signal_at 升序排列信号
  Step 5: 逐根 K 线遍历（时间从旧到新）
          对每根 K 线：
            检查每个"此时已生效"的信号是否满足条件
              - pending: 入场区间是否被触及？
              - entered: SL/TP 是否被触发？
          如果触发 → 记录状态转换（含触发时间 = K线时间）
  Step 6: 持久化所有转换
```

### 2.2 为什么必须按时间顺序

```
场景（同合约 BTC_USDT）：

  10:00 信号A: pending (entry 62000-62200)
  10:30 K线: low=62100 → 信号A 入场触发 → entered
  11:00 K线: low=60900 → 信号A 止损触发 → exited
  12:00 信号B: pending (entry 63000-63200)
  12:30 K线: low=63100 → 信号B 入场触发 → entered
  13:00 信号C: pending (entry 61000-61200)  ← 注意：信号C在信号A之后

如果打乱顺序处理：
  ❌ 先检查信号C → 可能错误地认为"入场触发"，但信号C是 13:00 才发出的
  ❌ 先检查信号A的止损 → 但还没处理入场

按时间顺序处理：
  ✅ 10:00 只检查信号A
  ✅ 10:30 信号A入场
  ✅ 11:00 信号A止损离场
  ✅ 12:00 开始检查信号B
  ✅ 12:30 信号B入场
  ✅ 13:00 开始检查信号C
```

### 2.3 算法伪代码

```python
async def scan_contract(self, contract: str):
    """扫描一个合约的所有待跟踪信号。"""

    # ── Step 1-2: 获取信号 + 最旧时间 ──
    signals = self._load_active_signals_by_contract(contract)
    if not signals:
        return []

    oldest_signal_at = min(s.signal_at for s in signals)

    # ── Step 3: 一次性拉取全部 K 线 ──
    candles = await self._fetch_candles(contract, from_=oldest_signal_at)
    if not candles:
        return []

    # ── Step 4: 按 signal_at 升序排列 ──
    signals.sort(key=lambda s: s.signal_at)

    # ── Step 5: 逐根 K 线遍历 ──
    transitions: list[StateTransition] = []
    signal_index = 0  # 下一个待激活的信号

    # 每个信号维护自己的"已处理到哪根K线"的游标
    active_checks: dict[int, SignalCheckState] = {}

    for candle in candles:
        candle_time = candle.opened_at

        # 5a: 激活新到达 signal_at 的信号
        while signal_index < len(signals) and signals[signal_index].signal_at <= candle_time:
            sig = signals[signal_index]
            active_checks[sig.id] = SignalCheckState(
                signal=sig,
                status=sig.lifecycle_status,  # pending_entry 或 entered
                entry_triggered_at=None,
                exit_triggered_at=None,
            )
            signal_index += 1

        # 5b: 检查所有已激活的信号
        for sig_id, check in list(active_checks.items()):
            if check.status == "done":
                continue

            sig = check.signal

            if check.status == "pending_entry":
                # 入场检查
                if self._candle_overlaps_range(candle, sig.entry_range_low, sig.entry_range_high):
                    check.status = "entered"
                    check.entry_triggered_at = candle_time
                    check.entry_price = self._pick_entry_price(candle, sig.entry_range_low, sig.entry_range_high)
                    # 如果没设 SL/TP，入场即完成
                    if sig.stop_loss is None and not sig.take_profit:
                        check.status = "done"
                        transitions.append(StateTransition(
                            signal=sig,
                            from_status="pending_entry",
                            to_status="entered",
                            trigger_price=check.entry_price,
                            occurred_at=candle_time,
                        ))

            elif check.status == "entered":
                # 止损检查（优先于止盈）
                if sig.stop_loss is not None:
                    if (sig.side == "long" and candle.low <= sig.stop_loss) or \
                       (sig.side == "short" and candle.high >= sig.stop_loss):
                        check.status = "done"
                        transitions.append(StateTransition(
                            signal=sig,
                            from_status="entered",
                            to_status="exited",
                            exit_reason="stop_loss",
                            trigger_price=sig.stop_loss,
                            occurred_at=candle_time,
                        ))
                        continue

                # 止盈检查（多止盈位）
                tp_levels = self._parse_take_profits(sig.take_profit)
                if tp_levels:
                    tp_hit = self._check_tp_for_candle(candle, sig.side, tp_levels, sig.filled_tp_index)
                    if tp_hit is not None:
                        sig.filled_tp_index = tp_hit.index
                        if tp_hit.is_last:
                            check.status = "done"
                            transitions.append(StateTransition(
                                signal=sig,
                                from_status="entered",
                                to_status="exited",
                                exit_reason="take_profit",
                                trigger_price=tp_hit.price,
                                occurred_at=candle_time,
                            ))

        # 5c: 记录每根 K 线处理后的状态（用于调试和审计）
        # （可选）

    # ── Step 6: 过期检测（遍历完所有 K 线后） ──
    # 注意：过期检测在 K 线遍历之后，因为遍历可能触发入场
    for sig in signals:
        check = active_checks.get(sig.id)
        if check and check.status == "pending_entry":
            if self._is_expired(sig):
                transitions.append(StateTransition(
                    signal=sig,
                    from_status="pending_entry",
                    to_status="expired",
                    exit_reason="expired",
                    occurred_at=self._now(),
                ))

    return transitions
```

### 2.4 关键细节：`SignalCheckState` 游标

每个信号独立维护它在 K 线遍历中的状态：

```python
@dataclass
class SignalCheckState:
    signal: StrategyLifecycle
    status: str                # pending_entry | entered | done
    entry_triggered_at: datetime | None
    entry_price: float | None
    exit_triggered_at: datetime | None
```

这解决了时间顺序问题：
- 信号只有在 `candle_time >= signal.signal_at` 之后才激活
- 信号先入场才能后离场
- 入场和离场的触发时间精确到具体那根 K 线的时间戳

---

## 三、K 线数据获取策略

### 3.1 Gate API 限制

```
GET /api/v4/futures/usdt/candlesticks
  interval: 1m
  limit: 最大 1000（很多交易所接口的默认上限，Gate 文档未明确但实测如此）
```

1000 根 1m K 线 ≈ 16.7 小时。

如果最旧信号是 3 天前（4320 分钟），需要分页拉取。

### 3.2 分页方案：循环拉取直到覆盖整个区间

```python
async def _fetch_candles_full(
    self, contract: str, from_: datetime, to_: datetime
) -> list[PriceCandle]:
    """拉取 from_ 到 to_ 的完整 1m K 线，自动处理分页。"""
    all_candles: list[PriceCandle] = []
    cursor = int(from_.timestamp())  # epoch seconds
    end_ts = int(to_.timestamp())
    per_page = 1000  # 每次最多拉 1000 根

    while cursor < end_ts:
        response = await self._http.get(
            f"{self._base_url}/api/v4/futures/{self._settle}/candlesticks",
            params={
                "contract": contract,
                "interval": "1m",
                "from": cursor,
                "to": end_ts,
                "limit": per_page,
            },
            timeout=10.0,
        )
        response.raise_for_status()
        batch = [_candle_from_payload(row) for row in response.json()]

        if not batch:
            break

        all_candles.extend(batch)

        # 下一页：从最后一根之后开始
        cursor = int(batch[-1].opened_at.timestamp()) + 60  # +60s = 下1分钟

        if len(batch) < per_page:
            break  # 已拉完

    return all_candles
```

### 3.3 实际数据量估算

| 最旧信号年龄 | 1m K线数 | API请求数 | 耗时估算 |
|------------|---------|----------|---------|
| 1 小时 | 60 | 1 | ~100ms |
| 12 小时 | 720 | 1 | ~150ms |
| 24 小时 | 1440 | 2 | ~250ms |
| 3 天 | 4320 | 5 | ~600ms |
| 7 天 | 10080 | 11 | ~1200ms |

对于 5 个不同合约，最坏情况（都有 7 天前的旧信号）下是 5 × 11 = 55 次 HTTP 请求，耗时约 6 秒。远在 60 秒的周期内。

但实际中绝大部分 pending 信号不会超过 3 天还没入场，所以实际请求量会低很多。

### 3.4 优化：先用 5m K 线粗筛，再用 1m 精确定位

```
对于超过 24 小时的区间：
  Step 1: 拉 5m K 线（数据量减少 5 倍）
  Step 2: 找到入场/离场触发的大致时间窗口
  Step 3: 只对触发窗口拉 1m K 线精确定位
```

这个优化可以进一步减少 80% 的 API 调用，但增加了复杂度。作为 V1 直接用 1m 拉全量即可。

---

## 四、完整的扫描循环

### 4.1 每分钟主流程

```python
class LifecycleMonitor:
    """按合约分组 + 一次拉线 + 时间顺序遍历的监控器。"""

    def __init__(self, session_factory, http_client, broker, now_provider, config):
        self._session_factory = session_factory
        self._http = http_client
        self._broker = broker
        self._now = now_provider or (lambda: datetime.now(UTC))
        self._config = config

    async def run_loop(self):
        while True:
            try:
                await self._run_one_cycle()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("LifecycleMonitor cycle failed")
            await asyncio.sleep(self._config.cycle_interval_seconds)

    async def _run_one_cycle(self):
        now = self._now()

        # ── 1. 加载所有待跟踪信号，按合约分组 ──
        all_signals = self._load_active_signals()
        if not all_signals:
            return

        by_contract: dict[str, list[StrategyLifecycle]] = {}
        for sig in all_signals:
            contract = _symbol_to_contract(sig.symbol)  # "BTC" → "BTC_USDT"
            by_contract.setdefault(contract, []).append(sig)

        # ── 2. 逐合约扫描 ──
        all_transitions: list[StateTransition] = []
        for contract, signals in by_contract.items():
            transitions = await self._scan_contract(contract, signals, now)
            all_transitions.extend(transitions)

        # ── 3. 持久化 + SSE ──
        self._apply_transitions(all_transitions)
```

### 4.2 加载信号：一次 DB 查询

```python
def _load_active_signals(self) -> list[StrategyLifecycle]:
    """加载所有 pending_entry 和 entered 状态、且在 7 天内的信号。"""
    cutoff = self._now() - timedelta(days=7)

    with self._session_factory() as session:
        return (
            session.query(StrategyLifecycle)
            .filter(
                StrategyLifecycle.lifecycle_status.in_(["pending_entry", "entered"]),
                StrategyLifecycle.signal_at >= cutoff,
            )
            .order_by(StrategyLifecycle.signal_at.asc())
            .all()
        )
```

只扫描 7 天内的信号。超过 7 天还没入场的，在下一次 `_check_expiry` 中直接标记为 expired，不拉 K 线。

### 4.3 完整的合约扫描

```python
async def _scan_contract(
    self,
    contract: str,
    signals: list[StrategyLifecycle],
    now: datetime,
) -> list[StateTransition]:
    """扫描一个合约的所有信号。"""

    if not signals:
        return []

    # ── 找最旧信号时间 ──
    oldest_signal_at = min(s.signal_at for s in signals)

    # ── 拉取 K 线 ──
    candles = await self._fetch_candles_full(contract, oldest_signal_at, now)
    if not candles:
        logger.warning("No candles for %s, skipping", contract)
        return []

    # ── 按 signal_at 排序 ──
    signals.sort(key=lambda s: s.signal_at)

    # ── 时间线遍历 ──
    transitions: list[StateTransition] = []
    signal_index = 0
    active_checks: dict[int, SignalCheckState] = {}

    for candle in candles:
        ct = candle.opened_at

        # 激活新到达的信号
        while signal_index < len(signals) and signals[signal_index].signal_at <= ct:
            sig = signals[signal_index]
            active_checks[sig.id] = SignalCheckState(
                signal=sig,
                status=sig.lifecycle_status,
            )
            signal_index += 1

        # 检查所有活跃信号
        for sig_id, check in list(active_checks.items()):
            if check.status == "done":
                continue

            sig = check.signal

            # ── pending_entry → entered ──
            if check.status == "pending_entry":
                if self._entry_triggered(sig, candle):
                    check.status = "entered"
                    check.entry_triggered_at = ct
                    check.entry_price = self._resolve_entry_price(sig, candle)

                    if not self._has_exit_conditions(sig):
                        # 没设 SL/TP，入场即算完成（后续靠 KOL 离场信号或手动）
                        check.status = "done"

                    transitions.append(StateTransition(
                        signal_id=sig.id,
                        from_status="pending_entry",
                        to_status="entered",
                        trigger_price=check.entry_price,
                        occurred_at=ct,
                    ))

            # ── entered → exited ──
            elif check.status == "entered":
                exit_result = self._check_exit(sig, candle)
                if exit_result:
                    check.status = "done"
                    transitions.append(StateTransition(
                        signal_id=sig.id,
                        from_status="entered",
                        to_status="exited",
                        exit_reason=exit_result.reason,
                        trigger_price=exit_result.price,
                        occurred_at=ct,
                    ))

    # ── 遍历完 K 线后的收尾 ──
    for sig in signals:
        check = active_checks.get(sig.id)
        if check is None:
            # 信号时间在未来（新信号，还没到）、或者还没激活
            # 这发生在 signal_at > candles[-1].opened_at 时
            continue

        if check.status == "pending_entry" and self._is_expired(sig, now):
            transitions.append(StateTransition(
                signal_id=sig.id,
                from_status="pending_entry",
                to_status="expired",
                exit_reason="expired",
                occurred_at=now,
            ))

    return transitions


def _entry_triggered(self, sig: StrategyLifecycle, candle: PriceCandle) -> bool:
    """判断这根 K 线是否触发了入场。"""
    if sig.entry_range_low is None or sig.entry_range_high is None:
        return False
    return candle.low <= sig.entry_range_high and candle.high >= sig.entry_range_low


def _resolve_entry_price(self, sig: StrategyLifecycle, candle: PriceCandle) -> float:
    """估算入场价：取 K 线区间与入场区间重叠部分的中点。"""
    overlap_low = max(candle.low, sig.entry_range_low)
    overlap_high = min(candle.high, sig.entry_range_high)
    return (overlap_low + overlap_high) / 2


def _check_exit(self, sig: StrategyLifecycle, candle: PriceCandle) -> ExitResult | None:
    """检查这根 K 线是否触发离场。先止损后止盈。"""
    # 止损优先
    if sig.stop_loss is not None:
        if sig.side == "long" and candle.low <= sig.stop_loss:
            return ExitResult(reason="stop_loss", price=sig.stop_loss)
        if sig.side == "short" and candle.high >= sig.stop_loss:
            return ExitResult(reason="stop_loss", price=sig.stop_loss)

    # 止盈（多止盈位）
    tp_levels = self._parse_take_profits(sig.take_profit)
    if tp_levels:
        for i in range(sig.filled_tp_index + 1, len(tp_levels)):
            tp = tp_levels[i]
            hit = False
            if sig.side == "long" and candle.high >= tp:
                hit = True
            elif sig.side == "short" and candle.low <= tp:
                hit = True

            if hit:
                sig.filled_tp_index = i
                if i == len(tp_levels) - 1:
                    return ExitResult(reason="take_profit", price=tp)
                # else: 中间 TP，记录但不离场（save filled_tp_index 在 _apply_transitions 中处理）
                break

    return None
```

---

## 五、完整数据流总览

```
┌─────────────────────────────────────────────────────────────────┐
│                    run_loop() — 每 60 秒                         │
│                                                                   │
│  1. 查 DB：所有 pending_entry + entered，且 signal_at ≥ 7天前    │
│     ┌──────────┬──────────┬──────────┬──────────┐               │
│     │ BTC_USDT │ ETH_USDT │ SOL_USDT │ DOGE_USDT│  ← 按合约分组  │
│     │ 3个信号  │ 2个信号  │ 1个信号  │ 0个信号  │               │
│     └────┬─────┴────┬─────┴────┬─────┴────┬─────┘               │
│          │          │          │          │                      │
│  2. 每合约独立扫描（可并行）                                      │
│                                                                   │
│     BTC_USDT:                                                     │
│     ├─ 最旧信号 10:00（2天前）                                    │
│     ├─ 拉 K 线: from=10:00 to=now（~2880根，3次分页）            │
│     │                                                                 │
│     │  时间线: 10:00 ──┬── 12:30 ──┬── 14:00 ──┬── now          │
│     │                  │           │           │                  │
│     │  信号A(10:00) ─→激活      入场触发   止损触发→exited      │
│     │  信号B(13:00) ─────────→激活────入场触发→entered(仍持有) │
│     │                                                                 │
│     └─ transitions: [A:exited@14:00, B:entered@13:30]            │
│                                                                   │
│     ETH_USDT:                                                     │
│     ├─ 最旧信号 15:00（6小时前）                                  │
│     ├─ 拉 K 线: from=15:00 to=now（~360根，1次请求）             │
│     └─ ...                                                        │
│                                                                   │
│  3. 合并所有合约的 transitions                                    │
│                                                                   │
│  4. _apply_transitions() ─ 持久化 + SSE 推送                     │
│                                                                   │
│  5. sleep(60)                                                     │
└─────────────────────────────────────────────────────────────────┘
```

---

## 六、并行扫描优化

不同合约的 K 线查询互相独立，可以并行：

```python
async def _run_one_cycle(self):
    all_signals = self._load_active_signals()
    if not all_signals:
        return

    by_contract = self._group_by_contract(all_signals)

    # 并行扫描所有合约
    tasks = [
        self._scan_contract(contract, signals, self._now())
        for contract, signals in by_contract.items()
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_transitions = []
    for result in results:
        if isinstance(result, Exception):
            logger.exception("Contract scan failed: %s", result)
        else:
            all_transitions.extend(result)

    self._apply_transitions(all_transitions)
```

5 个合约并行扫描，即使每个需要 5 次分页（25 次 HTTP），总耗时还是约等于最慢的那个合约（~1.2s），而不是串行的 6s。

---

## 七、与 KOL 新消息的即时联动

当 KOL 新消息到达（`telegram_live_listener`），有两种情况需要即时处理：

### 7.1 新入场信号

不需要特殊处理——它会被创建为 `pending_entry`，下一轮 scan（最多 60 秒后）就会自动纳入跟踪。

### 7.2 离场信号（exit_signal）

需要即时匹配。不走 K 线扫描，直接按 `(chat_id, symbol, side)` 查找 entered 状态的 signal 并标记离场：

```python
async def on_new_exit_signal(
    self, chat_id: int, symbol: str, side: str, message_id: int
):
    """KOL 发出离场信号，即时匹配并关闭对应持仓。"""
    contract = _symbol_to_contract(symbol)

    with self._session_factory() as session:
        matching = (
            session.query(StrategyLifecycle)
            .filter(
                StrategyLifecycle.chat_id == chat_id,
                StrategyLifecycle.symbol == symbol.upper(),
                StrategyLifecycle.side == side.lower(),
                StrategyLifecycle.lifecycle_status == "entered",
            )
            .order_by(StrategyLifecycle.entered_at.desc())
            .first()
        )

        if matching is None:
            logger.info("No matching entered position for exit signal (chat=%s, %s %s)",
                        chat_id, symbol, side)
            return

        matching.lifecycle_status = "exited"
        matching.exit_reason = "kol_signal"
        matching.exited_at = self._now()
        matching.exit_signal_message_id = message_id
        session.commit()

        # SSE 推送
        self._broker.publish_event(
            event_type="lifecycle_status_changed",
            payload={
                "lifecycle_id": matching.id,
                "symbol": matching.symbol,
                "side": matching.side,
                "chat_id": matching.chat_id,
                "from_status": "entered",
                "to_status": "exited",
                "exit_reason": "kol_signal",
            },
        )
```

---

## 八、状态转换表（更新后的数据模型）

```python
class StrategyLifecycle(Base):
    __tablename__ = "strategy_lifecycles"

    id: int (PK)
    signal_candidate_id: int (FK)
    chat_id: int
    message_id: int
    symbol: str           # "BTC"
    side: str             # "long" | "short"

    # 核心状态（本扫描器维护）
    lifecycle_status: str
    # "pending_entry" | "entered" | "exited" | "expired" | "cancelled"
    exit_reason: str | None
    # None | "stop_loss" | "take_profit" | "kol_signal" | "manual" | "expired"

    # 时间戳
    signal_at: datetime       # KOL 发出信号的时间
    entered_at: datetime | None
    exited_at: datetime | None
    last_checked_at: datetime | None  # 最后一次 K 线扫描的时间

    # 价格字段
    entry_range_low: float | None
    entry_range_high: float | None
    stop_loss: float | None
    take_profit: str | None      # JSON 数组 "[64000,66000,68000]"
    filled_tp_index: int = -1    # 已触发的止盈位索引，-1=未触发任何
    entry_price_actual: float | None
    exit_price_actual: float | None

    # 关联
    execution_binding_id: int | None (FK)
    trade_idea_id: int | None (FK)
    exit_signal_candidate_id: int | None (FK)
    exit_signal_message_id: int | None
```

---

## 九、性能总结

假设：**5 个合约、每合约平均 3 个待跟踪信号、最旧信号 2 天前**

| 操作 | 每轮开销 |
|------|---------|
| DB: 加载活跃信号 | 1 次查询 |
| HTTP: K 线拉取 | 5 × 3页 = 15 次（并行 ≈ 最慢合约 ~600ms） |
| 内存: K 线遍历 | 5 × 3000根 = 15000 次循环（每根 ~1μs，总 ~15ms） |
| DB: 持久化转换 | 1 次 commit（通常 0 条转换） |
| **总耗时（并行）** | **~700ms** |

远在 60 秒预算内。

---

## 十、与方案 A 的对比

| | 旧方案（每信号独立） | 新方案（按合约+一次拉线） |
|---|---|---|
| K 线请求数 | O(signals) | O(contracts) |
| 时间顺序保证 | ❌ 无 | ✅ K线→signal时间线对应 |
| 入场时间精度 | 最近一次检查时间 | ✅ 精确到具体1m K线 |
| 多信号共享数据 | ❌ 重复拉取 | ✅ 一份数据服务全部 |
| 并发能力 | 无 | 合约间并行 |
| 代码复杂度 | 较低 | 中等（时间线遍历逻辑） |
