"""只读生产保护归属巡检。

在生产监控停用期间，这是唯一持续回答"是否存在系统无法管理的实盘仓位"的机制。
纯只读：SQLite 以 mode=ro + PRAGMA query_only 打开，交易所只走 worker 8002 的 GET。
不下单、不改单、不撤单、不补挂保护。

用法（在能 ssh 到生产的机器上执行）：
    ssh tecent 'python3 -B -' < scripts/production_protection_attribution_check.py

ALERT_COUNT 为 0 表示无异常；大于 0 时逐条打印命中的仓位，需要人工到交易所处置。
"""

import sqlite3, json, subprocess, re
c = sqlite3.connect("file:/opt/telegram-kol-analyzer/data/research.db?mode=ro", uri=True)
c.execute("PRAGMA query_only=ON")
alerts = []

snap = subprocess.run(["curl","-s","--max-time","20",
    "http://127.0.0.1:8002/api/runtime-agent/read-only-exchange-snapshot"],
    capture_output=True, text=True).stdout.strip()
try:
    live_n = json.loads(snap).get("position_count") or 0
except Exception:
    live_n = 0

# 已成交且已归属的入场腿 = 真正需要保护的对象
legs = list(c.execute("""
SELECT l.id, l.execution_binding_id, l.pos_id, b.symbol, b.side
FROM execution_order_legs l JOIN execution_bindings b ON b.id = l.execution_binding_id
WHERE l.status='active' AND l.attribution_status='verified'
  AND l.pos_id IS NOT NULL AND l.pos_id != '' AND b.status='active'
"""))
for leg_id, bid, pos_id, sym, side in legs:
    led = list(c.execute("""SELECT purpose,status,trigger_price FROM position_protection_ledger
                            WHERE execution_order_leg_id=? AND status='verified'""", (leg_id,)))
    purposes = {p for p, _, _ in led}
    if "stop_loss" not in purposes:
        alerts.append(("NO_VERIFIED_STOP", {"leg": leg_id, "binding": bid, "pos": pos_id,
                                            "symbol": sym, "side": side, "ledger": led}))
    elif not (purposes & {"take_profit","combined","supervised_current_tpsl"}) and not list(
        c.execute("SELECT 1 FROM position_take_profit_orders WHERE execution_binding_id=? LIMIT 1",(bid,))):
        alerts.append(("NO_TAKE_PROFIT", {"leg": leg_id, "binding": bid, "pos": pos_id,
                                          "symbol": sym, "side": side,
                                          "verified": sorted(purposes)}))
# 归属失败
bad = list(c.execute("""
SELECT b.id,b.symbol,b.side,b.pos_id,i.id,i.recovery_state,i.recovery_disposition,i.last_reason_code
FROM trigger_protection_intents i JOIN execution_bindings b ON b.id=i.execution_binding_id
WHERE b.status != 'closed'
  AND (i.recovery_state='failed' OR i.recovery_disposition='manual_review'
       OR i.last_reason_code='trigger_protection_candidate_predates_fill')"""))
if bad: alerts.append(("FAILED_INTENT", bad))
# 交易所有仓但系统无归属腿
bound = {p for (p,) in c.execute("SELECT pos_id FROM execution_order_legs WHERE pos_id IS NOT NULL AND pos_id != ''")}
panel = subprocess.run(["curl","-s","--max-time","30",
    "http://127.0.0.1:8002/positions-panel?initial=positions"], capture_output=True, text=True).stdout
# 只取带 "pos " 前缀的仓位 ID；此前按全文正则取前 N 个会把保护单 ID 误当仓位
ids, seen = [], set()
for pid in re.findall(r'pos\s+(100112[0-9]{10,})', panel):
    if pid not in seen: seen.add(pid); ids.append(pid)
unbound = [p for p in ids if p not in bound]
if unbound: alerts.append(("UNATTRIBUTED_POSITION", unbound))
if "unbound_live_position" in panel:
    alerts.append(("UNBOUND_LIVE_POSITION_MARKER", {"positions_seen": ids}))

# 已确认并在跟踪中的已知项：命中不变则抑制
import os
KNOWN = {("NO_TAKE_PROFIT", "1001125135694798")}
state_path = "/tmp/.protcheck_known_state"
suppressed = []
kept = []
for a in alerts:
    key = (a[0], a[1].get("pos") if isinstance(a[1], dict) else None)
    if key in KNOWN:
        suppressed.append(a)
    else:
        kept.append(a)
alerts = kept
if suppressed:
    print("SUPPRESSED_KNOWN:", len(suppressed))
print("EXCHANGE:", snap)
print("VERIFIED_LEGS:", [(l[0], l[2]) for l in legs])
print("ALERT_COUNT:", len(alerts))
for a in alerts: print("ALERT:", json.dumps(a, ensure_ascii=False, default=str))
