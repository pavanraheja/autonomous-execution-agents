"""
Williams Alligator — Comprehensive Backtest
─────────────────────────────────────────────
Compares:
  System A (old) : Trail only, activates at +1.0%, gap 0.7%
  System B (new) : 75% exit at TP +2.0%, 25% runner with trail gap 0.7%

Also sweeps parameters to find optimal:
  - SL: 0.8%, 1.0%, 1.2%, 1.5%
  - TP: 1.5%, 2.0%, 2.5%, 3.0%
  - Partial exit: 50%, 65%, 75%
  - Trail gap: 0.5%, 0.7%, 1.0%
  - Variant: A (1st 2H sub-candle) vs C (2nd 2H sub-candle)

Coins  : BTC, ETH
Period : 180 days of 6H candles + 2H sub-candles
"""

import ccxt
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
from itertools import product

# ── Exchange ────────────────────────────────────────────────────────────────
exchange = ccxt.binanceusdm({"enableRateLimit": True, "timeout": 30000})

SYMBOLS   = ["BTC/USDT:USDT", "ETH/USDT:USDT"]
DAYS_BACK = 180

# Alligator params (fixed)
JAW_PERIOD   = 13; JAW_SHIFT   = 8
TEETH_PERIOD =  8; TEETH_SHIFT = 5
LIPS_PERIOD  =  5; LIPS_SHIFT  = 3

# ── Fetch helpers ────────────────────────────────────────────────────────────
def fetch_all(symbol, tf, days):
    """Fetch full history via pagination."""
    since  = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    all_oh = []
    while True:
        batch = exchange.fetch_ohlcv(symbol, tf, since=since, limit=1000)
        if not batch:
            break
        all_oh.extend(batch)
        since = batch[-1][0] + 1
        if len(batch) < 1000:
            break
    df = pd.DataFrame(all_oh, columns=["ts","open","high","low","close","vol"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)

# ── Alligator ────────────────────────────────────────────────────────────────
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

# ── Core simulator ───────────────────────────────────────────────────────────
def simulate(df6h, df2h, sl_pct, tp_pct, partial_exit, trail_gap, variant):
    """
    Returns list of trade dicts.
    variant: "A" = 1st 2H sub-candle | "C" = 2nd 2H sub-candle (both confirm)
    """
    df6h = calc_alligator(df6h)
    traded = {}   # coin_variant → last c6_open_ts traded (dedup)
    trades = []

    for i in range(2, len(df6h) - 1):
        c6_row   = df6h.iloc[i]
        prev_row = df6h.iloc[i - 1]
        direction = prev_row["direction"]   # signal from last CLOSED 6H candle
        if direction == "FLAT":
            continue

        c6_open_ts = c6_row["ts"]
        dedup_key  = f"{variant}"
        if traded.get(dedup_key) == str(c6_open_ts):
            continue

        # Sub-candles within this 6H candle
        sub = df2h[df2h["ts"] >= c6_open_ts].head(3)
        if len(sub) == 0:
            continue

        # Entry selection
        if variant == "A":
            if len(sub) < 1:
                continue
            entry_candle = sub.iloc[0]
        else:  # C — both sub-candles must confirm direction
            if len(sub) < 2:
                continue
            sub1_dir = "SHORT" if sub.iloc[0]["close"] < sub.iloc[0]["open"] else "LONG"
            sub2_dir = "SHORT" if sub.iloc[1]["close"] < sub.iloc[1]["open"] else "LONG"
            if sub1_dir != direction or sub2_dir != direction:
                continue
            entry_candle = sub.iloc[1]

        entry  = entry_candle["close"]
        entry_ts = entry_candle["ts"]

        if direction == "SHORT":
            sl_price = round(entry * (1 + sl_pct / 100), 6)
            tp_price = round(entry * (1 - tp_pct / 100), 6)
        else:
            sl_price = round(entry * (1 - sl_pct / 100), 6)
            tp_price = round(entry * (1 + tp_pct / 100), 6)

        traded[dedup_key] = str(c6_open_ts)

        # Simulate forward on 15m candles (approximate with 2H candles post-entry)
        future = df2h[df2h["ts"] > entry_ts].head(72)   # ~6 days of 2H candles max

        tp_hit       = False
        tp_exit_price= None
        trail_active = False
        trail_sl     = None
        exit_price   = None
        result       = None

        for _, row in future.iterrows():
            h, l = row["high"], row["low"]

            if direction == "SHORT":
                # SL check (before TP)
                if not tp_hit and h >= sl_price:
                    exit_price = sl_price
                    result     = "LOSS"
                    break

                # TP check
                if not tp_hit and l <= tp_price:
                    tp_hit        = True
                    tp_exit_price = tp_price
                    trail_active  = True
                    trail_sl      = round(tp_price * (1 + trail_gap / 100), 6)
                    continue

                # Runner trail
                if trail_active:
                    new_t = round(l * (1 + trail_gap / 100), 6)
                    if new_t < trail_sl:
                        trail_sl = new_t
                    if h >= trail_sl:
                        exit_price = trail_sl
                        break

            else:  # LONG
                if not tp_hit and l <= sl_price:
                    exit_price = sl_price
                    result     = "LOSS"
                    break

                if not tp_hit and h >= tp_price:
                    tp_hit        = True
                    tp_exit_price = tp_price
                    trail_active  = True
                    trail_sl      = round(tp_price * (1 - trail_gap / 100), 6)
                    continue

                if trail_active:
                    new_t = round(h * (1 - trail_gap / 100), 6)
                    if new_t > trail_sl:
                        trail_sl = new_t
                    if l <= trail_sl:
                        exit_price = trail_sl
                        break

        # Timeout — close at last available price
        if exit_price is None and len(future) > 0:
            exit_price = future.iloc[-1]["close"]

        if exit_price is None:
            continue

        # PnL calculation
        if direction == "SHORT":
            raw_pnl = (entry - exit_price) / entry * 100
        else:
            raw_pnl = (exit_price - entry) / entry * 100

        if result == "LOSS":
            pnl = -sl_pct
        elif tp_hit:
            runner_pnl = raw_pnl
            pnl = partial_exit * tp_pct + (1 - partial_exit) * runner_pnl
            result = "WIN" if pnl > 0.05 else ("BE" if pnl > -0.1 else "LOSS")
        else:
            pnl    = raw_pnl
            result = "WIN" if pnl > 0.05 else ("BE" if pnl > -0.1 else "LOSS")

        trades.append({
            "direction":  direction,
            "entry":      entry,
            "exit":       exit_price,
            "pnl":        round(pnl, 4),
            "result":     result,
            "tp_hit":     tp_hit,
            "entry_ts":   str(entry_ts),
        })

    return trades

# ── Metrics ──────────────────────────────────────────────────────────────────
def metrics(trades):
    if not trades:
        return {}
    wins   = [t for t in trades if t["result"] == "WIN"]
    losses = [t for t in trades if t["result"] == "LOSS"]
    pnls   = [t["pnl"] for t in trades]
    gw     = sum(t["pnl"] for t in wins)
    gl     = abs(sum(t["pnl"] for t in losses))
    pf     = round(gw / gl, 3) if gl > 0 else float("inf")
    wr     = round(len(wins) / len(trades) * 100, 1)
    ev     = round(np.mean(pnls), 4)
    total  = round(sum(pnls), 2)
    # Max drawdown
    cumulative = np.cumsum(pnls)
    peak       = np.maximum.accumulate(cumulative)
    dd         = round(float(np.max(peak - cumulative)), 2)
    tp_hit_pct = round(sum(1 for t in trades if t.get("tp_hit")) / len(trades) * 100, 1)
    return {
        "trades":    len(trades),
        "wr":        wr,
        "total_pnl": total,
        "pf":        pf,
        "ev":        ev,
        "max_dd":    dd,
        "tp_hit_pct": tp_hit_pct,
        "avg_win":   round(np.mean([t["pnl"] for t in wins]), 3) if wins else 0,
        "avg_loss":  round(np.mean([t["pnl"] for t in losses]), 3) if losses else 0,
    }

# ── Main ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\nFetching 180-day candle history...")
    data = {}
    for sym in SYMBOLS:
        coin = sym.split("/")[0]
        print(f"  {coin}: fetching 6H...", end=" ", flush=True)
        data[coin] = {
            "6h": fetch_all(sym, "6h", DAYS_BACK),
            "2h": fetch_all(sym, "2h", DAYS_BACK),
        }
        print(f"{len(data[coin]['6h'])} 6H | {len(data[coin]['2h'])} 2H candles")

    # ── SECTION 1: Old vs New comparison ─────────────────────────────────────
    print("\n" + "═"*70)
    print("  SECTION 1 — OLD SYSTEM vs NEW SYSTEM")
    print("═"*70)
    print(f"  Old: Trail only (activates at +1.0%, gap 0.7%)")
    print(f"  New: 75% at TP +2.0% | 25% runner trail gap 0.7% | SL 1.0%")
    print("═"*70)

    for coin, d in data.items():
        print(f"\n  ── {coin} ──")
        print(f"  {'System':<20} {'Trades':>7} {'WR':>7} {'Total PnL':>10} {'PF':>7} {'EV/trade':>10} {'MaxDD':>8} {'TP Hit%':>8}")
        print(f"  {'-'*80}")

        # Old: trail only — simulate with TP very far (5%) so it never hits, trail at 1%
        # We simulate "trail only" by setting partial_exit=1.0 and TP=1.0% (trail triggers at 1%)
        # Actually old system: no TP, trail activates at 1.0%. We model this as TP=1.0%, partial=100%
        for variant in ["A", "C"]:
            old_trades = []
            new_trades = []
            for sym in SYMBOLS:
                c = sym.split("/")[0]
                if c != coin:
                    continue
                # OLD: no fixed TP, trail activates at 1.0%, gap 0.7%
                #   → model as partial_exit=1.0 (100% at trail exit), TP=1.0% (activates trail)
                old = simulate(data[c]["6h"].copy(), data[c]["2h"].copy(),
                               sl_pct=1.0, tp_pct=1.0, partial_exit=1.0,
                               trail_gap=0.7, variant=variant)
                old_trades.extend(old)

                # NEW: 75% at TP 2.0%, 25% runner, trail gap 0.7%
                new = simulate(data[c]["6h"].copy(), data[c]["2h"].copy(),
                               sl_pct=1.0, tp_pct=2.0, partial_exit=0.75,
                               trail_gap=0.7, variant=variant)
                new_trades.extend(new)

            for label, t in [("Old (trail) " + variant, old_trades),
                              ("New (partial) " + variant, new_trades)]:
                m = metrics(t)
                if not m:
                    continue
                print(f"  {label:<20} {m['trades']:>7} {m['wr']:>6}% {m['total_pnl']:>+9.2f}% "
                      f"{m['pf']:>7} {m['ev']:>+9.4f}% {m['max_dd']:>7}% {m.get('tp_hit_pct',0):>7}%")

    # ── SECTION 2: Parameter sweep ────────────────────────────────────────────
    print("\n" + "═"*70)
    print("  SECTION 2 — PARAMETER SWEEP (BTC + ETH combined, Variant A)")
    print("  Finding best SL / TP / Partial Exit / Trail Gap combination")
    print("═"*70)

    SL_RANGE      = [0.8, 1.0, 1.2, 1.5]
    TP_RANGE      = [1.5, 2.0, 2.5, 3.0]
    PARTIAL_RANGE = [0.50, 0.65, 0.75]
    TRAIL_RANGE   = [0.5, 0.7, 1.0]

    results = []
    total   = len(SL_RANGE) * len(TP_RANGE) * len(PARTIAL_RANGE) * len(TRAIL_RANGE)
    done    = 0

    for sl, tp, pe, tg in product(SL_RANGE, TP_RANGE, PARTIAL_RANGE, TRAIL_RANGE):
        if tp <= sl:   # TP must be wider than SL for positive RR
            done += 1
            continue
        all_trades = []
        for sym in SYMBOLS:
            coin = sym.split("/")[0]
            t = simulate(data[coin]["6h"].copy(), data[coin]["2h"].copy(),
                         sl_pct=sl, tp_pct=tp, partial_exit=pe,
                         trail_gap=tg, variant="A")
            all_trades.extend(t)
        m = metrics(all_trades)
        if m and m["trades"] >= 20:
            results.append({
                "sl": sl, "tp": tp, "partial": pe, "trail": tg,
                "rr": round(tp / sl, 1), **m
            })
        done += 1
        if done % 20 == 0:
            print(f"  Progress: {done}/{total}...", end="\r", flush=True)

    results.sort(key=lambda x: (-x["ev"], -x["total_pnl"]))

    print(f"\n  {'SL':>5} {'TP':>5} {'Part%':>6} {'TGap':>5} {'RR':>4} "
          f"{'Trades':>7} {'WR':>6} {'Total':>9} {'PF':>6} {'EV':>9} {'MaxDD':>7}")
    print(f"  {'-'*80}")
    for r in results[:20]:
        print(f"  {r['sl']:>4}% {r['tp']:>4}% {r['partial']*100:>5.0f}% {r['trail']:>4}% "
              f"{r['rr']:>4} {r['trades']:>7} {r['wr']:>5}% {r['total_pnl']:>+8.2f}% "
              f"{r['pf']:>6} {r['ev']:>+8.4f}% {r['max_dd']:>6}%")

    # ── SECTION 3: Best config monthly breakdown ──────────────────────────────
    if results:
        best = results[0]
        print(f"\n{'═'*70}")
        print(f"  SECTION 3 — BEST CONFIG MONTHLY BREAKDOWN")
        print(f"  SL {best['sl']}% | TP {best['tp']}% | {best['partial']*100:.0f}% exit "
              f"| Trail {best['trail']}% | RR {best['rr']}:1")
        print(f"{'═'*70}")
        print(f"  {'Month':<12} {'BTC Trades':>11} {'BTC PnL':>9} {'ETH Trades':>11} {'ETH PnL':>9}")
        print(f"  {'-'*55}")

        for sym in SYMBOLS:
            coin = sym.split("/")[0]

        monthly = {}
        for sym in SYMBOLS:
            coin = sym.split("/")[0]
            t = simulate(data[coin]["6h"].copy(), data[coin]["2h"].copy(),
                         sl_pct=best["sl"], tp_pct=best["tp"],
                         partial_exit=best["partial"], trail_gap=best["trail"],
                         variant="A")
            for trade in t:
                mo = str(trade["entry_ts"])[:7]
                if mo not in monthly:
                    monthly[mo] = {}
                if coin not in monthly[mo]:
                    monthly[mo][coin] = []
                monthly[mo][coin].append(trade["pnl"])

        for mo in sorted(monthly.keys()):
            row = f"  {mo:<12}"
            for sym in SYMBOLS:
                coin = sym.split("/")[0]
                pnls = monthly[mo].get(coin, [])
                if pnls:
                    row += f"  {len(pnls):>9}  {sum(pnls):>+8.2f}%"
                else:
                    row += f"  {'—':>9}  {'—':>9}"
            print(row)

        print(f"\n  RECOMMENDATION:")
        print(f"  → Best config: SL {best['sl']}% | TP {best['tp']}% | "
              f"{best['partial']*100:.0f}% exit at TP | Trail gap {best['trail']}%")
        print(f"  → EV per trade: {best['ev']:+.4f}% | WR: {best['wr']}% | "
              f"PF: {best['pf']} | Total: {best['total_pnl']:+.2f}%")

    print("\nDone.\n")
