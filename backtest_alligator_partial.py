"""
Alligator Partial-Exit Backtest — Port 8086
============================================
Fixed config: SL 1.5% / TP 3.0% / Variant A+C / BTC+ETH / 180 days
Compares:
  Baseline     : 100% exits at TP 3.0%
  Partial 0.5% : 50% exits at TP, 50% runner with 0.5% trail
  Partial 1.0% : 50% exits at TP, 50% runner with 1.0% trail

Run: python3 backtest_alligator_partial.py
"""

import ccxt, pickle, os, time
import pandas as pd
from datetime import datetime, timedelta, timezone

# ── Config ───────────────────────────────────────────────────────────────────
SYMBOLS   = ["BTC/USDT:USDT", "ETH/USDT:USDT"]
DAYS_BACK = 180
SL_PCT    = 1.5
TP_PCT    = 3.0
VARIANTS  = ["A", "C"]

SYSTEMS = [
    {"label": "Baseline (100% at TP)",       "partial": 1.0, "trail": 0.5},
    {"label": "50% exit + 0.5% trail",        "partial": 0.5, "trail": 0.5},
    {"label": "50% exit + 1.0% trail",        "partial": 0.5, "trail": 1.0},
]

CACHE_FILE = "/tmp/backtest_alligator_candles.pkl"

# Alligator smoothed MA periods
JAW_PERIOD = 13; JAW_SHIFT = 8
TEETH_PERIOD = 8; TEETH_SHIFT = 5
LIPS_PERIOD  = 5; LIPS_SHIFT  = 3

exchange = ccxt.binanceusdm({"enableRateLimit": True, "timeout": 30000})

# ── Fetch + cache ─────────────────────────────────────────────────────────────
def fetch_all(symbol, tf, days):
    since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    all_ohlcv = []
    while True:
        batch = exchange.fetch_ohlcv(symbol, tf, since=since, limit=1000)
        if not batch:
            break
        all_ohlcv.extend(batch)
        since = batch[-1][0] + 1
        if len(batch) < 1000:
            break
        time.sleep(0.05)
    df = pd.DataFrame(all_ohlcv, columns=["ts","open","high","low","close","vol"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)

cache = {}
if os.path.exists(CACHE_FILE):
    with open(CACHE_FILE, "rb") as f:
        cache = pickle.load(f)
    print(f"  Loaded candle cache ({len(cache)} keys)")

data = {}
for sym in SYMBOLS:
    name = sym.split("/")[0]
    for tf in ["6h", "2h"]:
        key = f"{sym}_{tf}"
        if key not in cache:
            print(f"  Fetching {name} {tf}...", end=" ", flush=True)
            cache[key] = fetch_all(sym, tf, DAYS_BACK)
            print(f"{len(cache[key])} bars")
            with open(CACHE_FILE, "wb") as f:
                pickle.dump(cache, f)
        data[key] = cache[key]

# ── Alligator signal ──────────────────────────────────────────────────────────
def calc_alligator(df):
    df = df.copy()
    df["jaw"]   = df["close"].ewm(alpha=1/JAW_PERIOD,   adjust=False).mean().shift(JAW_SHIFT)
    df["teeth"] = df["close"].ewm(alpha=1/TEETH_PERIOD, adjust=False).mean().shift(TEETH_SHIFT)
    df["lips"]  = df["close"].ewm(alpha=1/LIPS_PERIOD,  adjust=False).mean().shift(LIPS_SHIFT)
    def _dir(r):
        if pd.isna(r["jaw"]): return "FLAT"
        if r["jaw"] > r["teeth"] > r["lips"]: return "SHORT"
        if r["lips"] > r["teeth"] > r["jaw"]: return "LONG"
        return "FLAT"
    df["direction"] = df.apply(_dir, axis=1)
    return df

# ── Core simulator ────────────────────────────────────────────────────────────
def simulate(df6h, df2h, sl_pct, tp_pct, partial_exit, trail_gap, variant):
    df6h   = calc_alligator(df6h)
    traded = {}
    trades = []

    for i in range(2, len(df6h) - 1):
        c6_row    = df6h.iloc[i]
        prev_row  = df6h.iloc[i - 1]
        direction = prev_row["direction"]
        if direction == "FLAT":
            continue

        c6_open_ts = c6_row["ts"]
        if traded.get(variant) == str(c6_open_ts):
            continue

        sub = df2h[df2h["ts"] >= c6_open_ts].head(3)
        if len(sub) == 0:
            continue

        if variant == "A":
            entry_candle = sub.iloc[0]
        else:
            if len(sub) < 2:
                continue
            s1 = "SHORT" if sub.iloc[0]["close"] < sub.iloc[0]["open"] else "LONG"
            s2 = "SHORT" if sub.iloc[1]["close"] < sub.iloc[1]["open"] else "LONG"
            if s1 != direction or s2 != direction:
                continue
            entry_candle = sub.iloc[1]

        entry    = entry_candle["close"]
        entry_ts = entry_candle["ts"]
        traded[variant] = str(c6_open_ts)

        if direction == "SHORT":
            sl_price = entry * (1 + sl_pct / 100)
            tp_price = entry * (1 - tp_pct / 100)
        else:
            sl_price = entry * (1 - sl_pct / 100)
            tp_price = entry * (1 + tp_pct / 100)

        future = df2h[df2h["ts"] > entry_ts].head(72)

        tp_hit        = False
        tp_exit_price = None
        trail_sl      = None
        exit_price    = None
        result        = None

        for _, row in future.iterrows():
            h, l = row["high"], row["low"]

            if direction == "SHORT":
                if not tp_hit and h >= sl_price:
                    exit_price = sl_price; result = "LOSS"; break
                if not tp_hit and l <= tp_price:
                    tp_hit = True; tp_exit_price = tp_price
                    trail_sl = tp_price * (1 + trail_gap / 100)
                    # Baseline: 100% exits at TP — no runner
                    if partial_exit >= 1.0:
                        exit_price = tp_price; result = "WIN"; break
                    continue
                if tp_hit:
                    new_t = l * (1 + trail_gap / 100)
                    if new_t < trail_sl: trail_sl = new_t
                    if h >= trail_sl:
                        exit_price = trail_sl; break
            else:
                if not tp_hit and l <= sl_price:
                    exit_price = sl_price; result = "LOSS"; break
                if not tp_hit and h >= tp_price:
                    tp_hit = True; tp_exit_price = tp_price
                    trail_sl = tp_price * (1 - trail_gap / 100)
                    if partial_exit >= 1.0:
                        exit_price = tp_price; result = "WIN"; break
                    continue
                if tp_hit:
                    new_t = h * (1 - trail_gap / 100)
                    if new_t > trail_sl: trail_sl = new_t
                    if l <= trail_sl:
                        exit_price = trail_sl; break

        if exit_price is None and len(future) > 0:
            exit_price = future.iloc[-1]["close"]
        if exit_price is None:
            continue

        if direction == "SHORT":
            raw_pnl = (entry - exit_price) / entry * 100
        else:
            raw_pnl = (exit_price - entry) / entry * 100

        if result == "LOSS":
            pnl = -sl_pct
        elif partial_exit >= 1.0:
            pnl = tp_pct; result = "WIN"
        elif tp_hit:
            runner_pnl = (tp_exit_price - exit_price) / tp_exit_price * 100 if direction == "SHORT" \
                         else (exit_price - tp_exit_price) / tp_exit_price * 100
            pnl    = partial_exit * tp_pct + (1 - partial_exit) * runner_pnl
            result = "WIN" if pnl > 0.05 else ("BE" if pnl > -0.1 else "LOSS")
        else:
            pnl    = raw_pnl
            result = "WIN" if pnl > 0.05 else ("BE" if pnl > -0.1 else "LOSS")

        trades.append({
            "symbol": sym.split("/")[0], "direction": direction, "variant": variant,
            "entry": entry, "tp_price": tp_price, "sl_price": sl_price,
            "exit_price": exit_price, "tp_hit": tp_hit,
            "runner_exit": exit_price if tp_hit else None,
            "pnl": round(pnl, 4), "result": result,
            "entry_ts": str(entry_ts),
        })

    return trades

# ── Run all systems ───────────────────────────────────────────────────────────
print()
print("=" * 72)
print(f"  Alligator Partial-Exit Backtest  |  {DAYS_BACK}d  |  BTC + ETH")
print(f"  Fixed: SL {SL_PCT}% / TP {TP_PCT}% / Variants A+C")
print("=" * 72)

all_results = {}

for sys_cfg in SYSTEMS:
    label   = sys_cfg["label"]
    partial = sys_cfg["partial"]
    trail   = sys_cfg["trail"]
    sys_trades = []

    for sym in SYMBOLS:
        df6h = data[f"{sym}_6h"]
        df2h = data[f"{sym}_2h"]
        for var in VARIANTS:
            t = simulate(df6h, df2h, SL_PCT, TP_PCT, partial, trail, var)
            sys_trades.extend(t)

    wins   = [t for t in sys_trades if t["result"] == "WIN"]
    losses = [t for t in sys_trades if t["result"] == "LOSS"]
    bes    = [t for t in sys_trades if t["result"] == "BE"]
    total  = len(sys_trades)
    wr     = len(wins) / (len(wins) + len(losses)) * 100 if (wins or losses) else 0
    total_pnl = sum(t["pnl"] for t in sys_trades)
    avg_pnl   = total_pnl / total if total else 0
    pf_gross  = sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses)) if losses else 999

    # Runner stats (for partial systems)
    tp_trades = [t for t in sys_trades if t.get("tp_hit")]
    if tp_trades:
        runner_pnls = []
        for t in tp_trades:
            if t["result"] != "LOSS" and t.get("runner_exit") and t["direction"] == "SHORT":
                rp = (t["tp_price"] - t["runner_exit"]) / t["tp_price"] * 100
            elif t["result"] != "LOSS" and t.get("runner_exit"):
                rp = (t["runner_exit"] - t["tp_price"]) / t["tp_price"] * 100
            else:
                rp = -SL_PCT
            runner_pnls.append(rp)
        avg_runner = sum(runner_pnls) / len(runner_pnls)
        runner_positive = sum(1 for r in runner_pnls if r > 0)
    else:
        avg_runner = 0; runner_positive = 0

    all_results[label] = {
        "total": total, "wins": len(wins), "losses": len(losses), "bes": len(bes),
        "wr": wr, "total_pnl": total_pnl, "avg_pnl": avg_pnl, "pf": pf_gross,
        "avg_runner": avg_runner, "runner_positive": runner_positive,
        "tp_trades": len(tp_trades), "trades": sys_trades,
    }

# ── Print comparison table ────────────────────────────────────────────────────
print()
print(f"  {'System':<30}  {'Trades':>6}  {'WR':>6}  {'Total PnL':>10}  {'Avg/trade':>10}  {'PF':>6}  {'Avg runner':>11}")
print("  " + "-" * 82)

base_pnl = all_results[SYSTEMS[0]["label"]]["total_pnl"]
for sys_cfg in SYSTEMS:
    label = sys_cfg["label"]
    r     = all_results[label]
    delta = r["total_pnl"] - base_pnl
    flag  = f"  (Δ {delta:+.1f}%)" if delta != 0 else "  (baseline)"
    runner_str = f"{r['avg_runner']:+.2f}% ({r['runner_positive']}/{r['tp_trades']} ran)" if r["tp_trades"] else "  —"
    print(f"  {label:<30}  {r['total']:>6}  {r['wr']:>5.1f}%  {r['total_pnl']:>+9.1f}%{flag:<12}  {r['avg_pnl']:>+9.3f}%  {r['pf']:>6.2f}  {runner_str}")

# ── Per-symbol breakdown for best partial system ──────────────────────────────
best_label = max(
    [s["label"] for s in SYSTEMS[1:]],
    key=lambda l: all_results[l]["total_pnl"]
)
best_r = all_results[best_label]

print()
print(f"  BEST PARTIAL SYSTEM: {best_label}")
print()
print(f"  {'Symbol':<5}  {'Variant':<8}  {'Trades':>6}  {'WR':>6}  {'Total PnL':>10}  {'Avg/trade':>10}")
print("  " + "-" * 55)

for sym in SYMBOLS:
    sym_name = sym.split("/")[0]
    for var in VARIANTS:
        t_sub = [t for t in best_r["trades"] if t["symbol"] == sym_name and t["variant"] == var]
        if not t_sub: continue
        w = sum(1 for t in t_sub if t["result"] == "WIN")
        l = sum(1 for t in t_sub if t["result"] == "LOSS")
        wr = w / (w + l) * 100 if (w + l) else 0
        tp = round(sum(t["pnl"] for t in t_sub), 1)
        avg = tp / len(t_sub)
        print(f"  {sym_name:<5}  {var:<8}  {len(t_sub):>6}  {wr:>5.1f}%  {tp:>+9.1f}%  {avg:>+9.3f}%")

# ── Verdict ───────────────────────────────────────────────────────────────────
base_total = all_results[SYSTEMS[0]["label"]]["total_pnl"]
best_total = all_results[best_label]["total_pnl"]
delta      = best_total - base_total

print()
print("=" * 72)
print("  VERDICT")
print("=" * 72)
print(f"  Baseline (100% at TP):  {base_total:+.1f}%  |  EV {all_results[SYSTEMS[0]['label']]['avg_pnl']:+.3f}%/trade")
print(f"  {best_label:<30}: {best_total:+.1f}%  |  EV {best_r['avg_pnl']:+.3f}%/trade")
print(f"  Difference:              {delta:+.1f}%")
print()
if delta > 5:
    print("  APPLY — Clear improvement. Partial exit significantly better.")
elif delta > 0:
    print("  APPLY — Marginal improvement. Worthwhile given upside potential.")
elif delta > -5:
    print("  MARGINAL — Near breakeven. Current system comparable.")
else:
    print("  SKIP — Partial exit hurts Alligator. Keep 100% TP exit.")
print("=" * 72)
