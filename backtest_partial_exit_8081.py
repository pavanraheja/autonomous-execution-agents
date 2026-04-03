"""
Backtest: Partial Exit Runner — Port 8081 Paper Trader
=======================================================
Tests: current system (100% exit at TP) vs new system (50% exit at TP, 50% runner)
Uses REAL 1-minute candle data fetched from Binance for each trade.

Run: python3 backtest_partial_exit_8081.py
"""

import json, time, sys, os, pickle
from datetime import datetime, timezone, timedelta
import ccxt

# ─── Config ───────────────────────────────────────────────────────────────────
TRADE_FILE   = "/Users/macbook/Downloads/Content Projects/02 - Trading Coin Hunter/Trade Logs/paper_trades.json"
CACHE_FILE   = "/tmp/backtest_8081_candle_cache.pkl"   # avoid re-fetching on re-runs
PARTIAL_EXIT = 0.50        # 50% out at TP
TRAIL_GAPS   = [1.0]   # test multiple trail sizes (%)
FETCH_EXTRA_HOURS = 24     # fetch candles this many hours past TP hit to give runner room

exchange = ccxt.binanceusdm({"enableRateLimit": True, "timeout": 30000})

# ─── Helpers ──────────────────────────────────────────────────────────────────
def parse_dt(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)

def fetch_1m_candles(symbol, since_dt, until_dt):
    """Fetch 1-minute OHLCV between two datetimes."""
    sym_ccxt = symbol + "/USDT:USDT" if "USDT" not in symbol else symbol
    since_ms = int(since_dt.timestamp() * 1000)
    until_ms = int(until_dt.timestamp() * 1000)
    all_ohlcv = []
    while since_ms < until_ms:
        try:
            bars = exchange.fetch_ohlcv(sym_ccxt, "1m", since=since_ms, limit=1000)
        except Exception as e:
            print(f"    [WARN] fetch failed for {symbol}: {e}")
            break
        if not bars:
            break
        all_ohlcv.extend(bars)
        since_ms = bars[-1][0] + 60_000
        if bars[-1][0] >= until_ms:
            break
        time.sleep(0.1)
    return [b for b in all_ohlcv if b[0] <= until_ms]

def simulate_runner(candles, tp_price, sl_price, trail_gap_pct, direction="short"):
    """
    Simulate the 50% runner starting from TP hit.
    SHORT: price goes DOWN = profit. Trail stop is ABOVE current low.
    Returns runner_pnl_pct (positive = profit for a short).
    """
    trail_gap = trail_gap_pct / 100.0
    trail_stop = tp_price * (1 + trail_gap)   # starts just above TP for short
    best_price = tp_price

    for bar in candles:
        _, o, h, l, c, _ = bar
        # Price fell further → tighten trail stop
        if l < best_price:
            best_price = l
            trail_stop = best_price * (1 + trail_gap)
        # Check if high touched trail stop
        if h >= trail_stop:
            exit_price = trail_stop
            pnl = (tp_price - exit_price) / tp_price * 100  # negative if reversed up
            return round(pnl, 3), exit_price, "trail_stop"
        # Check if hit original SL (safeguard — shouldn't happen from TP side)
        if h >= sl_price:
            return round((tp_price - sl_price) / tp_price * 100, 3), sl_price, "sl_hit"

    # If no candles triggered exit, use last close
    exit_price = candles[-1][4] if candles else tp_price
    pnl = (tp_price - exit_price) / tp_price * 100
    return round(pnl, 3), exit_price, "still_open"

# ─── Load candle cache ────────────────────────────────────────────────────────
candle_cache = {}
if os.path.exists(CACHE_FILE):
    with open(CACHE_FILE, "rb") as f:
        candle_cache = pickle.load(f)
    print(f"  Loaded candle cache ({len(candle_cache)} entries)\n")

def get_candles(symbol, since_dt, until_dt):
    key = f"{symbol}_{int(since_dt.timestamp())}"
    if key in candle_cache:
        return candle_cache[key]
    bars = fetch_1m_candles(symbol, since_dt, until_dt)
    candle_cache[key] = bars
    with open(CACHE_FILE, "wb") as f:
        pickle.dump(candle_cache, f)
    return bars

# ─── Load trades ──────────────────────────────────────────────────────────────
with open(TRADE_FILE) as f:
    all_trades = json.load(f)

wins   = [t for t in all_trades if t.get("status") == "WIN"]
losses = [t for t in all_trades if t.get("status") == "LOSS"]
bes    = [t for t in all_trades if t.get("status") == "BE+"]
total  = len(all_trades)

print("=" * 70)
print(f"  Port 8081 Partial-Exit Backtest  |  {len(wins)} WIN trades to simulate")
print("=" * 70)
print()

# ─── Per-trade simulation ─────────────────────────────────────────────────────
results_by_trail = {g: [] for g in TRAIL_GAPS}
trade_details = []

for i, t in enumerate(wins):
    sym        = t["symbol"]
    entry      = t["entry"]
    tp_price   = t["tp"]        # actual TP price hit
    sl_price   = t["sl"]
    opened_at  = parse_dt(t["opened_at"])
    closed_at  = parse_dt(t["closed_at"])   # time TP was hit
    size_mult  = t.get("size_mult", 1.0)
    original_pnl = t.get("pnl_pct", 4.5)

    # Fetch 1m candles from TP hit (closed_at) to +FETCH_EXTRA_HOURS
    fetch_until = closed_at + timedelta(hours=FETCH_EXTRA_HOURS)
    print(f"  [{i+1:02d}/{len(wins)}] {sym:<8} TP hit {t['closed_at']}  fetching {FETCH_EXTRA_HOURS}h of candles...", end=" ", flush=True)

    candles = get_candles(sym, closed_at, fetch_until)
    print(f"{len(candles)} bars")

    row = {"symbol": sym, "entry": entry, "tp_price": tp_price, "sl_price": sl_price,
           "opened_at": t["opened_at"], "closed_at": t["closed_at"],
           "size_mult": size_mult, "original_pnl": original_pnl,
           "candle_count": len(candles)}

    for trail_gap in TRAIL_GAPS:
        if candles:
            runner_pnl, runner_exit, reason = simulate_runner(
                candles, tp_price, sl_price, trail_gap, direction="short")
        else:
            runner_pnl, runner_exit, reason = 0.0, tp_price, "no_data"

        # Blended PnL: 50% exits at TP, 50% runner
        partial_pnl = original_pnl * PARTIAL_EXIT
        runner_contribution = runner_pnl * (1 - PARTIAL_EXIT)
        blended_pnl = round(partial_pnl + runner_contribution, 3)

        row[f"runner_{trail_gap}"] = runner_pnl
        row[f"blended_{trail_gap}"] = blended_pnl
        row[f"reason_{trail_gap}"] = reason
        row[f"exit_{trail_gap}"] = round(runner_exit, 6)

    trade_details.append(row)
    time.sleep(0.2)

# ─── Aggregate Results ────────────────────────────────────────────────────────
print()
print("=" * 70)
print("  RESULTS — Per Trail Gap")
print("=" * 70)

# Baseline: current system
base_win_pnl   = sum(t.get("pnl_pct", 4.5) * t.get("size_mult", 1) for t in wins)
base_loss_pnl  = sum(t.get("pnl_pct", -3.0) * t.get("size_mult", 1) for t in losses)
base_be_pnl    = sum(t.get("pnl_pct", 0) * t.get("size_mult", 1) for t in bes)
base_total_pnl = base_win_pnl + base_loss_pnl + base_be_pnl
win_count      = len(wins)
loss_count     = len(losses)
wr             = win_count / (win_count + loss_count) * 100

print(f"\n  BASELINE (current: 100% exit at TP)")
print(f"  Wins: {win_count}  Losses: {loss_count}  BE+: {len(bes)}")
print(f"  WR: {wr:.1f}%   Total PnL (size-weighted): +{base_total_pnl:.2f}%")
print(f"  Avg WIN  pnl: +{sum(t.get('pnl_pct',4.5) for t in wins)/len(wins):.2f}%")
print(f"  Avg LOSS pnl: {sum(t.get('pnl_pct',-3.0) for t in losses)/len(losses):.2f}%")

print()
print(f"  NEW SYSTEM: {int(PARTIAL_EXIT*100)}% exit at TP + {int((1-PARTIAL_EXIT)*100)}% runner")
print()

best_trail   = None
best_pnl     = -9999

for trail_gap in TRAIL_GAPS:
    new_win_pnl = sum(
        row[f"blended_{trail_gap}"] * row["size_mult"]
        for row in trade_details
    )
    new_total_pnl = new_win_pnl + base_loss_pnl + base_be_pnl
    delta = new_total_pnl - base_total_pnl

    avg_runner  = sum(row[f"runner_{trail_gap}"] for row in trade_details) / len(trade_details)
    avg_blended = sum(row[f"blended_{trail_gap}"] for row in trade_details) / len(trade_details)

    reasons = {}
    for row in trade_details:
        r = row[f"reason_{trail_gap}"]
        reasons[r] = reasons.get(r, 0) + 1

    indicator = " ← BEST" if new_total_pnl > best_pnl else ""
    if new_total_pnl > best_pnl:
        best_pnl   = new_total_pnl
        best_trail = trail_gap

    print(f"  Trail {trail_gap:.1f}%  |  Total PnL: {new_total_pnl:+.2f}%  (Δ {delta:+.2f}% vs baseline){indicator}")
    print(f"           Avg blended WIN: +{avg_blended:.2f}%  |  Avg runner after TP: {avg_runner:+.2f}%")
    print(f"           Exits: {reasons}")
    print()

# ─── Trade-by-trade detail for best trail ─────────────────────────────────────
print("=" * 70)
print(f"  TRADE DETAIL  —  Best trail gap: {best_trail}%")
print("=" * 70)
print(f"  {'Symbol':<8}  {'Entry':>8}  {'TP':>8}  {'Orig%':>6}  {'Runner%':>8}  {'Blended%':>9}  {'Reason'}")
print("  " + "-" * 65)
for row in trade_details:
    orig    = row["original_pnl"]
    runner  = row[f"runner_{best_trail}"]
    blended = row[f"blended_{best_trail}"]
    reason  = row[f"reason_{best_trail}"]
    delta   = blended - orig
    flag    = " ▲" if blended > orig else (" ▼" if blended < orig else "")
    print(f"  {row['symbol']:<8}  {row['entry']:>8.5f}  {row['tp_price']:>8.5f}  {orig:>+6.1f}%  {runner:>+8.2f}%  {blended:>+9.2f}%  {reason}{flag}")

# ─── Summary verdict ──────────────────────────────────────────────────────────
best_new_win  = sum(row[f"blended_{best_trail}"] * row["size_mult"] for row in trade_details)
best_new_total = best_new_win + base_loss_pnl + base_be_pnl
final_delta   = best_new_total - base_total_pnl

print()
print("=" * 70)
print(f"  VERDICT")
print("=" * 70)
print(f"  Baseline total PnL:  +{base_total_pnl:.2f}%")
print(f"  Best new system:     {best_new_total:+.2f}%  (trail gap {best_trail}%)")
print(f"  Difference:          {final_delta:+.2f}%")
print()
if final_delta > 1.0:
    print("  ✓  APPLY — Partial exit runner significantly improves results.")
elif final_delta > 0:
    print("  ~  MARGINAL — Slight improvement. Consider applying.")
elif final_delta > -2.0:
    print("  ✗  MINIMAL DIFFERENCE — Keep current system (simpler).")
else:
    print("  ✗  WORSE — Partial exit hurts this strategy. Keep current system.")
print("=" * 70)
