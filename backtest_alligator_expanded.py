"""
Alligator Expanded Coin Backtest — v2.1 Config
───────────────────────────────────────────────
Tests Williams Alligator strategy across 20+ coins using the EXACT
live v2.1 config running on ports 8084 / 8086.

Config (locked — no sweeping):
  Variant      : C only (2nd 2H sub-candle, both must confirm)
  Direction    : SHORT only
  Spread filter: < 2.0% (Jaw–Lips / Lips)
  SL           : 1.5%
  TP           : 3.75%  (2.5× RR)
  Exit         : 100% at TP (no partial, no runner)
  Notional     : $10,000 per trade ($2,000 margin × 5×)
  Period       : 180 days

Output:
  Per-coin table: Trades | WR | PF | EV/trade | Monthly trades | Est $/month
  Summary: top coins ranked by PF, recommendations
"""

import ccxt
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone

# ── Exchange ──────────────────────────────────────────────────────────────────
exchange = ccxt.binanceusdm({"enableRateLimit": True, "timeout": 30000})

DAYS_BACK = 180

# ── v2.1 config (fixed) ───────────────────────────────────────────────────────
SL_PCT        = 1.5    # Stop loss %
TP_PCT        = 3.75   # Take profit %
SPREAD_MAX    = 2.0    # Max alligator spread % (Jaw–Lips / Lips)
VARIANT       = "C"    # 2nd 2H sub-candle entry, both must confirm direction
SHORT_ONLY    = True   # LONG WR 27-33% — no edge
NOTIONAL      = 10_000 # USD per trade ($2k margin × 5×)

# Alligator periods (Smoothed MA via EWM)
JAW_PERIOD   = 13; JAW_SHIFT   = 8
TEETH_PERIOD =  8; TEETH_SHIFT = 5
LIPS_PERIOD  =  5; LIPS_SHIFT  = 3

# ── Coin universe ─────────────────────────────────────────────────────────────
# Current live coins + expansion candidates
# Excluded: PAXG (0/4 WR, blacklisted), LINK (WR 29.7% PF 1.06 — no edge per v2.1 audit)
COINS = [
    # Currently live — baseline reference
    ("XRP",  "XRP/USDT:USDT"),   # 8086 — WR 56.2% PF 2.5 (180d backtest)
    ("BTC",  "BTC/USDT:USDT"),   # 8086 — WR 42.9% PF 1.36 (180d backtest)
    ("ETH",  "ETH/USDT:USDT"),   # 8084 — live 67% WR (6 trades)
    ("SOL",  "SOL/USDT:USDT"),   # 8084 — new, unknown

    # Expansion candidates — large liquid futures
    ("BNB",  "BNB/USDT:USDT"),
    ("DOGE", "DOGE/USDT:USDT"),
    ("ADA",  "ADA/USDT:USDT"),
    ("AVAX", "AVAX/USDT:USDT"),
    ("DOT",  "DOT/USDT:USDT"),
    ("LTC",  "LTC/USDT:USDT"),
    ("ATOM", "ATOM/USDT:USDT"),
    ("NEAR", "NEAR/USDT:USDT"),
    ("INJ",  "INJ/USDT:USDT"),
    ("SUI",  "SUI/USDT:USDT"),
    ("APT",  "APT/USDT:USDT"),
    ("ARB",  "ARB/USDT:USDT"),
    ("OP",   "OP/USDT:USDT"),
    ("TIA",  "TIA/USDT:USDT"),
    ("TON",  "TON/USDT:USDT"),
    ("AAVE", "AAVE/USDT:USDT"),
    ("UNI",  "UNI/USDT:USDT"),
    ("FIL",  "FIL/USDT:USDT"),
    ("SEI",  "SEI/USDT:USDT"),
    ("WLD",  "WLD/USDT:USDT"),
]


# ── Fetch helpers ─────────────────────────────────────────────────────────────
def fetch_all(symbol, tf, days):
    since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    all_oh = []
    while True:
        try:
            batch = exchange.fetch_ohlcv(symbol, tf, since=since, limit=1000)
        except Exception as e:
            print(f"    fetch error {symbol}/{tf}: {e}")
            break
        if not batch:
            break
        all_oh.extend(batch)
        since = batch[-1][0] + 1
        if len(batch) < 1000:
            break
    if not all_oh:
        return None
    df = pd.DataFrame(all_oh, columns=["ts", "open", "high", "low", "close", "vol"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)


# ── Alligator indicator ───────────────────────────────────────────────────────
def calc_alligator(df):
    df = df.copy()
    df["jaw"]   = df["close"].ewm(alpha=1 / JAW_PERIOD,   adjust=False).mean().shift(JAW_SHIFT)
    df["teeth"] = df["close"].ewm(alpha=1 / TEETH_PERIOD, adjust=False).mean().shift(TEETH_SHIFT)
    df["lips"]  = df["close"].ewm(alpha=1 / LIPS_PERIOD,  adjust=False).mean().shift(LIPS_SHIFT)

    def _dir(r):
        if pd.isna(r["jaw"]):
            return "FLAT"
        if r["jaw"] > r["teeth"] > r["lips"]:
            return "SHORT"
        if r["lips"] > r["teeth"] > r["jaw"]:
            return "LONG"
        return "FLAT"

    df["direction"] = df.apply(_dir, axis=1)
    return df


# ── Core backtest (v2.1 exact logic) ─────────────────────────────────────────
def backtest_coin(coin, df6h, df2h):
    """
    Replicates exact v2.1 live logic:
    - Variant C: 2nd 2H sub-candle, both sub-candles must confirm SHORT
    - Spread filter: skip if (jaw - lips) / lips > 2%
    - SHORT-only
    - SL 1.5%, TP 3.75%, 100% exit at TP
    """
    df6h = calc_alligator(df6h)
    trades = []
    traded_candles = set()  # dedup: one trade per 6H candle

    for i in range(2, len(df6h) - 1):
        c6_row   = df6h.iloc[i]
        prev_row = df6h.iloc[i - 1]

        direction = prev_row["direction"]
        if direction != "SHORT":
            continue  # SHORT-only

        c6_open_ts = c6_row["ts"]
        if c6_open_ts in traded_candles:
            continue

        # Alligator spread filter (from closed 6H candle)
        jaw_v   = prev_row["jaw"]
        lips_v  = prev_row["lips"]
        if pd.isna(jaw_v) or pd.isna(lips_v) or lips_v == 0:
            continue
        spread_pct = abs(jaw_v - lips_v) / lips_v * 100
        if spread_pct > SPREAD_MAX:
            continue  # Alligator too wide — tangled/unclear trend

        # Sub-candles within this 6H candle
        sub = df2h[df2h["ts"] >= c6_open_ts].head(3)
        if len(sub) < 2:
            continue

        sub1, sub2 = sub.iloc[0], sub.iloc[1]

        # Variant C: BOTH sub-candles must be bearish (red) for SHORT
        if sub1["close"] >= sub1["open"]:
            continue  # 1st candle not red
        if sub2["close"] >= sub2["open"]:
            continue  # 2nd candle not red

        entry    = float(sub2["close"])
        entry_ts = sub2["ts"]
        sl_price = entry * (1 + SL_PCT / 100)
        tp_price = entry * (1 - TP_PCT / 100)

        traded_candles.add(c6_open_ts)

        # Simulate on subsequent 2H candles (up to 6 days)
        future = df2h[df2h["ts"] > entry_ts].head(72)

        exit_price = None
        result     = None

        for _, row in future.iterrows():
            h, l = row["high"], row["low"]

            # SL hit first
            if h >= sl_price:
                exit_price = sl_price
                result     = "LOSS"
                break

            # TP hit
            if l <= tp_price:
                exit_price = tp_price
                result     = "WIN"
                break

        # Timeout — treat as still open at last price (conservative: close at last close)
        if exit_price is None:
            if len(future) == 0:
                continue
            exit_price = float(future.iloc[-1]["close"])
            raw_pnl = (entry - exit_price) / entry * 100
            result = "WIN" if raw_pnl > 0.05 else ("BE" if raw_pnl > -0.1 else "LOSS")
            pnl = raw_pnl
        else:
            pnl = TP_PCT if result == "WIN" else -SL_PCT

        trades.append({
            "coin":       coin,
            "entry_ts":   str(entry_ts),
            "direction":  direction,
            "entry":      entry,
            "exit":       exit_price,
            "sl":         sl_price,
            "tp":         tp_price,
            "spread_pct": round(spread_pct, 2),
            "result":     result,
            "pnl_pct":    round(pnl, 4),
            "pnl_usd":    round(pnl / 100 * NOTIONAL, 2),
        })

    return trades


# ── Metrics calculator ────────────────────────────────────────────────────────
def metrics(trades, days=DAYS_BACK):
    if not trades:
        return None
    wins   = [t for t in trades if t["result"] == "WIN"]
    losses = [t for t in trades if t["result"] == "LOSS"]
    pnls   = [t["pnl_pct"] for t in trades]
    gw     = sum(t["pnl_pct"] for t in wins)
    gl     = abs(sum(t["pnl_pct"] for t in losses))
    pf     = round(gw / gl, 2) if gl > 0 else float("inf")
    wr     = round(len(wins) / len(trades) * 100, 1)
    ev     = round(np.mean(pnls), 3)
    total_usd = sum(t["pnl_usd"] for t in trades)
    months = days / 30
    monthly_trades = round(len(trades) / months, 1)
    monthly_usd    = round(total_usd / months, 0)
    avg_w  = round(np.mean([t["pnl_pct"] for t in wins]),   2) if wins   else 0
    avg_l  = round(np.mean([t["pnl_pct"] for t in losses]), 2) if losses else 0
    return {
        "n":              len(trades),
        "wins":           len(wins),
        "losses":         len(losses),
        "wr":             wr,
        "pf":             pf,
        "ev":             ev,
        "avg_win":        avg_w,
        "avg_loss":       avg_l,
        "total_usd":      round(total_usd, 0),
        "monthly_trades": monthly_trades,
        "monthly_usd":    monthly_usd,
    }


# ── Monthly breakdown ─────────────────────────────────────────────────────────
def monthly_breakdown(trades):
    by_month = {}
    for t in trades:
        mo = t["entry_ts"][:7]
        by_month.setdefault(mo, []).append(t)
    rows = []
    for mo in sorted(by_month):
        mt = by_month[mo]
        wins = sum(1 for t in mt if t["result"] == "WIN")
        rows.append({
            "month":  mo,
            "trades": len(mt),
            "wins":   wins,
            "wr":     round(wins / len(mt) * 100, 0) if mt else 0,
            "pnl_usd": round(sum(t["pnl_usd"] for t in mt), 0),
        })
    return rows


# ── Main ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\n" + "═"*80)
    print("  ALLIGATOR v2.1 — EXPANDED COIN BACKTEST")
    print(f"  Config: SHORT-only | Variant C | Spread <{SPREAD_MAX}% | SL {SL_PCT}% | TP {TP_PCT}% | 180d")
    print(f"  Notional: ${NOTIONAL:,}/trade | Coins: {len(COINS)}")
    print("═"*80)

    print("\nFetching candle data...")
    data = {}
    for coin, sym in COINS:
        print(f"  {coin:<6}", end=" ", flush=True)
        try:
            df6h = fetch_all(sym, "6h", DAYS_BACK)
            df2h = fetch_all(sym, "2h", DAYS_BACK)
            if df6h is None or df2h is None or len(df6h) < 50:
                print("→ SKIP (insufficient data)")
                continue
            data[coin] = {"6h": df6h, "2h": df2h, "sym": sym}
            print(f"→ {len(df6h)} 6H | {len(df2h)} 2H candles")
        except Exception as e:
            print(f"→ ERROR: {e}")

    print("\nRunning backtests...")
    results = {}
    all_trades = []
    for coin in data:
        trades = backtest_coin(coin, data[coin]["6h"], data[coin]["2h"])
        results[coin] = trades
        all_trades.extend(trades)
        m = metrics(trades)
        status = f"{len(trades)} trades" if m else "no trades"
        print(f"  {coin:<6} → {status}")

    # ── Section 1: Per-coin ranking table ────────────────────────────────────
    print("\n" + "═"*95)
    print("  SECTION 1 — PER-COIN RESULTS (ranked by Profit Factor)")
    print(f"  {'Coin':<6} {'Trades':>7} {'WR':>6} {'PF':>6} {'EV%':>7} {'AvgW':>6} {'AvgL':>6} "
          f"{'Mo Trd':>7} {'Mo $':>9} {'Total $':>10} {'Status'}")
    print("  " + "-"*93)

    ranked = []
    for coin, trades in results.items():
        m = metrics(trades)
        if not m:
            continue
        ranked.append((coin, m, trades))

    # Sort by PF descending, but require at least 5 trades
    ranked.sort(key=lambda x: (-x[1]["pf"] if x[1]["n"] >= 5 else -999, -x[1]["ev"]))

    live_coins = {"XRP", "BTC", "ETH", "SOL"}

    for coin, m, _ in ranked:
        tag = "● LIVE" if coin in live_coins else ""
        pf_str = f"{m['pf']:.2f}" if m["pf"] != float("inf") else "∞"
        flag = ""
        if m["n"] < 5:
            flag = " ⚠ low sample"
        elif m["pf"] >= 1.5 and m["wr"] >= 45:
            flag = " ✅ STRONG"
        elif m["pf"] >= 1.2 and m["wr"] >= 40:
            flag = " ✓ viable"
        elif m["pf"] < 1.0:
            flag = " ✗ losing"
        print(f"  {coin:<6} {m['n']:>7} {m['wr']:>5}% {pf_str:>6} {m['ev']:>+6.3f}% "
              f"{m['avg_win']:>+5.2f}% {m['avg_loss']:>+5.2f}% "
              f"{m['monthly_trades']:>7} {m['monthly_usd']:>+9,.0f} {m['total_usd']:>+10,.0f}  "
              f"{tag}{flag}")

    # ── Section 2: Add / Skip / Remove recommendations ───────────────────────
    print("\n" + "═"*80)
    print("  SECTION 2 — RECOMMENDATIONS")
    print("═"*80)

    strong_add  = [(c, m) for c, m, _ in ranked if c not in live_coins and m["pf"] >= 1.5 and m["wr"] >= 45 and m["n"] >= 5]
    viable_add  = [(c, m) for c, m, _ in ranked if c not in live_coins and 1.2 <= m["pf"] < 1.5 and m["n"] >= 5]
    skip        = [(c, m) for c, m, _ in ranked if c not in live_coins and m["pf"] < 1.0 and m["n"] >= 5]
    live_review = [(c, m) for c, m, _ in ranked if c in live_coins]

    print("\n  ADD — Strong edge (PF ≥ 1.5, WR ≥ 45%, n ≥ 5):")
    if strong_add:
        for c, m in strong_add:
            print(f"    {c:<6}  PF {m['pf']:.2f} | WR {m['wr']}% | ~{m['monthly_trades']}/mo | +${m['monthly_usd']:,.0f}/mo")
    else:
        print("    None meet the bar.")

    print("\n  WATCH — Viable but below strong threshold (PF 1.2-1.5):")
    if viable_add:
        for c, m in viable_add:
            print(f"    {c:<6}  PF {m['pf']:.2f} | WR {m['wr']}% | ~{m['monthly_trades']}/mo | +${m['monthly_usd']:,.0f}/mo")
    else:
        print("    None.")

    print("\n  SKIP — Losing money (PF < 1.0):")
    if skip:
        for c, m in skip:
            pf_str = f"{m['pf']:.2f}" if m["pf"] != float("inf") else "∞"
            print(f"    {c:<6}  PF {pf_str} | WR {m['wr']}% | ${m['total_usd']:+,.0f} total")
    else:
        print("    None — all coins profitable.")

    print("\n  LIVE COIN HEALTH CHECK (existing ports 8084/8086):")
    for c, m in live_review:
        pf_str = f"{m['pf']:.2f}" if m["pf"] != float("inf") else "∞"
        health = "✅ strong" if m["pf"] >= 1.5 else ("⚠ marginal" if m["pf"] >= 1.0 else "❌ losing")
        print(f"    {c:<6}  PF {pf_str} | WR {m['wr']}% | {m['n']} trades | {health}")

    # ── Section 3: Best coins combined monthly PnL projection ────────────────
    top_add = [c for c, m in strong_add] + [c for c, m in viable_add[:2]]
    if top_add:
        print("\n" + "═"*80)
        print(f"  SECTION 3 — IF WE ADD TOP COINS: COMBINED MONTHLY PnL PROJECTION")
        print("═"*80)

        current_coins = [c for c, m, _ in ranked if c in live_coins]
        new_portfolio = current_coins + top_add
        print(f"  Portfolio: {', '.join(new_portfolio)}")

        combined = []
        for c in new_portfolio:
            if c in results:
                combined.extend(results[c])
        combined.sort(key=lambda t: t["entry_ts"])

        m_all = metrics(combined)
        if m_all:
            print(f"\n  Combined stats ({len(new_portfolio)} coins, 180d):")
            print(f"    Total trades: {m_all['n']} | WR: {m_all['wr']}% | PF: {m_all['pf']:.2f}")
            print(f"    Est monthly trades: {m_all['monthly_trades']} | Est monthly PnL: +${m_all['monthly_usd']:,.0f}")

        # Monthly breakdown
        print(f"\n  Monthly breakdown (combined portfolio):")
        print(f"  {'Month':<10} {'Trades':>8} {'Wins':>6} {'WR':>6} {'PnL $':>10}")
        print(f"  {'-'*46}")
        for row in monthly_breakdown(combined):
            print(f"  {row['month']:<10} {row['trades']:>8} {row['wins']:>6} {row['wr']:>5}% {row['pnl_usd']:>+10,.0f}")

    # ── Section 4: Spread distribution for top candidates ────────────────────
    print("\n" + "═"*80)
    print("  SECTION 4 — SPREAD DISTRIBUTION (signal quality check)")
    print("  Avg spread at entry — lower = tighter alligator = cleaner signal")
    print("═"*80)
    for coin, trades in results.items():
        if not trades:
            continue
        spreads = [t["spread_pct"] for t in trades]
        wins    = [t["spread_pct"] for t in trades if t["result"] == "WIN"]
        losses  = [t["spread_pct"] for t in trades if t["result"] == "LOSS"]
        print(f"  {coin:<6}  avg {np.mean(spreads):.2f}%  "
              f"[wins avg {np.mean(wins):.2f}% | losses avg {np.mean(losses):.2f}%]"
              if wins and losses else
              f"  {coin:<6}  avg {np.mean(spreads):.2f}%  [wins: {len(wins)} losses: {len(losses)}]")

    print("\nDone.\n")
