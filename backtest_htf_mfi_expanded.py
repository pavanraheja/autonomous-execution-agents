"""
HTF MFI Scalper — Expanded Coin Backtest (v1.7 config)
════════════════════════════════════════════════════════
Uses the proven backtest machinery from backtest_htf_mfi.py.
Tests 20+ coins with exact v1.7 live config.

v1.7 config:
  Direction   : 1D+6H agree (proxy for live 6H+1H — avoids 1H/MFI signal conflict)
  ADX         : ≥ 18 on 1D
  Entry proxy : 1H MFI(14) fresh cross ≥ 80 → SHORT | ≤ 20 → LONG
  SL          : 1.5%
  Trail       : gap 0.5%, milestones every 0.5% from +1.0%
  Session     : 07:00–22:00 UTC
  Cooldown    : wait for MFI to cross back through 50
  No zone filter (removed v1.5)
  Notional    : $2,500/trade

Note: 1H proxy is conservative (live 1M fires 3-5× more signals).
PF and WR directionally correct; trade count will be lower than live.
"""

import ccxt
import pandas as pd
import numpy as np
import time
from datetime import datetime, timedelta, timezone

exchange = ccxt.binanceusdm({"enableRateLimit": True, "timeout": 30000})

# ── Config (v1.7 locked) ──────────────────────────────────────────────────────
LOOKBACK_DAYS  = 180
EMA_PERIOD     = 20
EMA_SLOPE_BARS = 3
ADX_PERIOD     = 14
ADX_MIN        = 18
MFI_PERIOD     = 14
MFI_OB         = 80
MFI_OS         = 20
SL_PCT         = 1.5
TRAIL_START    = 1.0
TRAIL_STEP     = 0.5
TRAIL_GAP      = 0.5   # v1.6: tightened from 0.7%
SESSION_START  = 7
SESSION_END    = 22
NOTIONAL       = 2_500

# ── Coin universe ─────────────────────────────────────────────────────────────
LIVE_COINS = ["BTC", "ETH", "SOL"]
ALL_COINS  = [
    "BTC", "ETH", "SOL",        # current live
    "XRP", "BNB", "DOGE",       # large cap
    "ADA", "AVAX", "LINK",      # mid cap liquid
    "DOT", "LTC", "ATOM",       # established alts
    "NEAR", "INJ", "SUI",       # newer alts
    "APT", "ARB", "OP",         # L2s
    "AAVE", "UNI",              # DeFi
    "TIA", "TON", "WLD", "SEI", # misc
]


# ── Indicators ────────────────────────────────────────────────────────────────
def calc_ema(series):
    return series.ewm(span=EMA_PERIOD, adjust=False).mean()

def calc_mfi(df):
    tp  = (df["high"] + df["low"] + df["close"]) / 3
    rmf = tp * df["volume"]
    pos = rmf.where(tp > tp.shift(1), 0.0)
    neg = rmf.where(tp < tp.shift(1), 0.0)
    ps  = pos.rolling(MFI_PERIOD).sum()
    ns  = neg.rolling(MFI_PERIOD).sum().replace(0, 1e-10)
    return (100 - (100 / (1 + ps / ns))).fillna(50)

def calc_adx(df):
    hi, lo, cl = df["high"], df["low"], df["close"]
    up, down   = hi.diff(), lo.diff().mul(-1)
    pdm  = up.where((up > down) & (up > 0), 0.0)
    mdm  = down.where((down > up) & (down > 0), 0.0)
    tr   = pd.concat([hi - lo, (hi - cl.shift(1)).abs(), (lo - cl.shift(1)).abs()], axis=1).max(axis=1)
    a    = 1 / ADX_PERIOD
    atr  = tr.ewm(alpha=a, adjust=False).mean().replace(0, 1e-10)
    pdi  = 100 * pdm.ewm(alpha=a, adjust=False).mean() / atr
    mdi  = 100 * mdm.ewm(alpha=a, adjust=False).mean() / atr
    dx   = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, 1e-10)
    return dx.ewm(alpha=a, adjust=False).mean()

def dir_series(df, adx_min=None):
    ema   = calc_ema(df["close"])
    slope = ema > ema.shift(EMA_SLOPE_BARS)
    above = df["close"] > ema
    dirs  = pd.Series("FLAT", index=df.index)
    if adx_min is not None:
        adx = calc_adx(df)
        ok  = adx >= adx_min
        dirs[above &  slope & ok]  = "LONG"
        dirs[~above & ~slope & ok] = "SHORT"
    else:
        dirs[above &  slope]  = "LONG"
        dirs[~above & ~slope] = "SHORT"
    return dirs


# ── Data fetch ────────────────────────────────────────────────────────────────
def fetch_df(symbol, tf, since_ms):
    all_c, since = [], since_ms
    while True:
        try:
            c = exchange.fetch_ohlcv(symbol, tf, since=since, limit=1000)
        except Exception as e:
            print(f"    [{symbol} {tf}] {e}")
            break
        if not c:
            break
        all_c.extend(c)
        if len(c) < 1000:
            break
        since = c[-1][0] + 1
        time.sleep(0.08)
    if not all_c:
        return pd.DataFrame()
    df = pd.DataFrame(all_c, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.drop_duplicates("ts").set_index("ts").sort_index().astype(float)


# ── Trailing SL simulation ────────────────────────────────────────────────────
def simulate_trail(entry, direction, future_df):
    if future_df.empty:
        return 0.0
    sl_price = entry * (1 + SL_PCT / 100) if direction == "SHORT" else entry * (1 - SL_PCT / 100)
    peak     = 0.0

    def milestone_sl(peak_pct):
        if peak_pct < TRAIL_START:
            return sl_price
        m = int(peak_pct / TRAIL_STEP) * TRAIL_STEP
        m = max(m, TRAIL_START)
        locked_pnl = 0.0 if m == TRAIL_START else round(m - TRAIL_GAP, 4)
        if direction == "SHORT":
            return entry * (1 - locked_pnl / 100)
        else:
            return entry * (1 + locked_pnl / 100)

    for _, row in future_df.iloc[:200].iterrows():
        hi, lo = row["high"], row["low"]
        if direction == "SHORT":
            curr = (entry - lo) / entry * 100
            if curr > peak:
                peak = curr
                sl_price = min(sl_price, milestone_sl(peak))
            if hi >= sl_price:
                return round((entry - sl_price) / entry * 100, 4)
        else:
            curr = (hi - entry) / entry * 100
            if curr > peak:
                peak = curr
                sl_price = max(sl_price, milestone_sl(peak))
            if lo <= sl_price:
                return round((sl_price - entry) / entry * 100, 4)

    last = float(future_df.iloc[-1]["close"])
    return round(((entry - last) if direction == "SHORT" else (last - entry)) / entry * 100, 4)


# ── Backtest one coin ─────────────────────────────────────────────────────────
def backtest_coin(coin, d1h, d6h, d1d):
    cutoff   = pd.Timestamp(datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS))
    d1h_bt   = d1h[d1h.index >= cutoff]
    if len(d1h_bt) < MFI_PERIOD + 5:
        return []

    # Direction: 1D (with ADX) + 6H, forward-filled to 1H index
    dir_1d = dir_series(d1d, adx_min=ADX_MIN) if not d1d.empty else pd.Series("FLAT", index=d1d.index)
    dir_6h = dir_series(d6h)                   if not d6h.empty else pd.Series("FLAT", index=d6h.index)
    dir_1d_ff = dir_1d.reindex(d1h_bt.index, method="ffill").fillna("FLAT")
    dir_6h_ff = dir_6h.reindex(d1h_bt.index, method="ffill").fillna("FLAT")

    # Combined: 1D and 6H must agree
    dir_combined = pd.Series("FLAT", index=d1h_bt.index)
    agree = dir_1d_ff == dir_6h_ff
    dir_combined[agree & (dir_1d_ff == "LONG")]  = "LONG"
    dir_combined[agree & (dir_1d_ff == "SHORT")] = "SHORT"

    # MFI on 1H
    d1h_bt = d1h_bt.copy()
    d1h_bt["mfi"] = calc_mfi(d1h_bt)
    mfi       = d1h_bt["mfi"]
    ob_cross  = (mfi.shift(1) < MFI_OB) & (mfi >= MFI_OB)
    os_cross  = (mfi.shift(1) > MFI_OS) & (mfi <= MFI_OS)
    session   = (d1h_bt.index.hour >= SESSION_START) & (d1h_bt.index.hour < SESSION_END)

    trades       = []
    in_cooldown  = False
    cool_side    = None

    for i in range(MFI_PERIOD + 2, len(d1h_bt) - 1):
        if not session[i]:
            continue
        mfi_now = float(mfi.iloc[i])

        if in_cooldown:
            if cool_side == "above50" and mfi_now > 50:
                in_cooldown = False
            elif cool_side == "below50" and mfi_now < 50:
                in_cooldown = False
            else:
                continue

        direction = dir_combined.iloc[i]
        if direction == "FLAT":
            continue

        is_short = bool(ob_cross.iloc[i]) and direction == "SHORT"
        is_long  = bool(os_cross.iloc[i]) and direction == "LONG"
        if not is_short and not is_long:
            continue

        side   = "SHORT" if is_short else "LONG"
        entry  = float(d1h_bt["close"].iloc[i])
        future = d1h_bt.iloc[i + 1:]
        pnl    = simulate_trail(entry, side, future)

        trades.append({
            "coin":      coin,
            "entry_ts":  d1h_bt.index[i],
            "direction": side,
            "entry":     entry,
            "pnl_pct":   pnl,
            "pnl_usd":   round(pnl / 100 * NOTIONAL, 2),
            "win":       pnl > 0.05,
        })
        in_cooldown = True
        cool_side   = "above50" if side == "SHORT" else "below50"

    return trades


# ── Stats ─────────────────────────────────────────────────────────────────────
def stats(trades):
    if not trades:
        return None
    wins   = [t for t in trades if t["win"]]
    losses = [t for t in trades if not t["win"]]
    pnls   = [t["pnl_pct"] for t in trades]
    gw     = sum(t["pnl_pct"] for t in wins)
    gl     = abs(sum(t["pnl_pct"] for t in losses))
    pf     = round(gw / gl, 2) if gl > 0 else (float("inf") if gw > 0 else 0.0)
    wr     = round(len(wins) / len(trades) * 100, 1)
    mo     = LOOKBACK_DAYS / 30
    total_usd     = sum(t["pnl_usd"] for t in trades)
    longs  = [t for t in trades if t["direction"] == "LONG"]
    shorts = [t for t in trades if t["direction"] == "SHORT"]
    return {
        "n":         len(trades),
        "wins":      len(wins),
        "losses":    len(losses),
        "wr":        wr,
        "pf":        pf,
        "ev":        round(np.mean(pnls), 3),
        "total_usd": round(total_usd, 0),
        "mo_trades": round(len(trades) / mo, 1),
        "mo_usd":    round(total_usd / mo, 0),
        "long_n":    len(longs),
        "long_wr":   round(len([t for t in longs if t["win"]]) / len(longs) * 100, 0) if longs else 0,
        "short_n":   len(shorts),
        "short_wr":  round(len([t for t in shorts if t["win"]]) / len(shorts) * 100, 0) if shorts else 0,
    }


def monthly_breakdown(trades):
    by_mo = {}
    for t in trades:
        mo = str(t["entry_ts"])[:7]
        by_mo.setdefault(mo, []).append(t)
    return [{"month": mo,
             "n":     len(v),
             "wins":  sum(1 for t in v if t["win"]),
             "wr":    round(sum(1 for t in v if t["win"]) / len(v) * 100, 0),
             "usd":   round(sum(t["pnl_usd"] for t in v), 0)}
            for mo in sorted(by_mo) for v in [by_mo[mo]]]


# ── Main ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\n" + "═"*88)
    print("  HTF MFI SCALPER v1.7 — EXPANDED COIN BACKTEST")
    print(f"  Direction: 1D+6H agree | ADX≥{ADX_MIN} on 1D | 1H MFI proxy | SL {SL_PCT}% | Trail {TRAIL_GAP}% | {LOOKBACK_DAYS}d")
    print(f"  MFI: OB{MFI_OB}/OS{MFI_OS} | Session: {SESSION_START}:00–{SESSION_END}:00 UTC | ${NOTIONAL:,}/trade")
    print("═"*88)

    print("\nFetching data...")
    since_1h  = int((datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS + 5)).timestamp()  * 1000)
    since_htf = int((datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS + 35)).timestamp() * 1000)
    coin_data = {}
    for coin in ALL_COINS:
        sym = f"{coin}/USDT:USDT"
        print(f"  {coin:<6}", end=" ", flush=True)
        try:
            d1h = fetch_df(sym, "1h",  since_1h)
            d6h = fetch_df(sym, "6h",  since_htf)
            d1d = fetch_df(sym, "1d",  since_htf)
            if d1h.empty:
                print("→ SKIP")
                continue
            coin_data[coin] = {"1h": d1h, "6h": d6h, "1d": d1d}
            print(f"→ {len(d1h)} 1H | {len(d6h)} 6H | {len(d1d)} 1D")
        except Exception as e:
            print(f"→ ERROR: {e}")

    print("\nRunning backtests...")
    results = {}
    all_trades = []
    for coin in coin_data:
        d = coin_data[coin]
        trades = backtest_coin(coin, d["1h"], d["6h"], d["1d"])
        results[coin] = trades
        all_trades.extend(trades)
        m = stats(trades)
        if m:
            print(f"  {coin:<6} → {m['n']:>3} trades | WR {m['wr']:>5}% | PF {m['pf']:.2f} | ~{m['mo_trades']}/mo")
        else:
            print(f"  {coin:<6} → 0 trades")

    # ── Section 1: Per-coin table ─────────────────────────────────────────────
    ranked = [(c, stats(t), t) for c, t in results.items() if stats(t)]
    ranked.sort(key=lambda x: (-x[1]["pf"] if x[1]["n"] >= 5 else -999, -x[1]["ev"]))

    print("\n" + "═"*100)
    print("  SECTION 1 — PER-COIN RESULTS (ranked by PF, min 5 trades)")
    print(f"  {'Coin':<6} {'Trades':>7} {'WR':>6} {'PF':>6} {'EV%':>7} {'Mo Trd':>7} {'Mo $':>9} "
          f"{'Total$':>9} {'L':>5} {'L%':>5} {'S':>5} {'S%':>5} {'Status'}")
    print("  " + "-"*98)

    for coin, m, _ in ranked:
        pf_s = f"{m['pf']:.2f}" if m["pf"] != float("inf") else "∞"
        live = "● LIVE" if coin in LIVE_COINS else ""
        flag = (" ✅ STRONG" if m["n"] >= 5 and m["pf"] >= 1.5 and m["wr"] >= 50
                else " ✓ viable" if m["n"] >= 5 and m["pf"] >= 1.2
                else " ✗ losing" if m["n"] >= 5 and m["pf"] < 1.0
                else " ⚠ low n")
        print(f"  {coin:<6} {m['n']:>7} {m['wr']:>5}% {pf_s:>6} {m['ev']:>+6.3f}% "
              f"{m['mo_trades']:>7} {m['mo_usd']:>+9,.0f} {m['total_usd']:>+9,.0f} "
              f"{m['long_n']:>5} {m['long_wr']:>4}% {m['short_n']:>5} {m['short_wr']:>4}%  "
              f"{live}{flag}")

    # ── Section 2: Recommendations ───────────────────────────────────────────
    print("\n" + "═"*80)
    print("  SECTION 2 — RECOMMENDATIONS")
    print("═"*80)
    strong = [(c, m) for c, m, _ in ranked if c not in LIVE_COINS and m["pf"] >= 1.5 and m["wr"] >= 50 and m["n"] >= 5]
    viable = [(c, m) for c, m, _ in ranked if c not in LIVE_COINS and 1.2 <= m["pf"] < 1.5 and m["n"] >= 5]
    losing = [(c, m) for c, m, _ in ranked if c not in LIVE_COINS and m["pf"] < 1.0 and m["n"] >= 5]
    live_r = [(c, m) for c, m, _ in ranked if c in LIVE_COINS]

    print("\n  ADD — Strong (PF ≥ 1.5, WR ≥ 50%, n ≥ 5):")
    for c, m in (strong or [("—", None)]):
        if m:
            print(f"    {c:<6} PF {m['pf']:.2f} | WR {m['wr']}% | ~{m['mo_trades']}/mo | +${m['mo_usd']:,.0f}/mo")
        else:
            print("    None.")

    print("\n  WATCH — Viable (PF 1.2–1.5, n ≥ 5):")
    for c, m in (viable or [("—", None)]):
        if m:
            print(f"    {c:<6} PF {m['pf']:.2f} | WR {m['wr']}% | ~{m['mo_trades']}/mo | +${m['mo_usd']:,.0f}/mo")
        else:
            print("    None.")

    print("\n  SKIP (PF < 1.0, n ≥ 5):")
    for c, m in (losing or [("—", None)]):
        if m:
            pf_s = f"{m['pf']:.2f}" if m["pf"] != float("inf") else "∞"
            print(f"    {c:<6} PF {pf_s} | WR {m['wr']}% | ${m['total_usd']:+,.0f} total")
        else:
            print("    None.")

    print("\n  LIVE HEALTH (current 8085 coins):")
    for c, m in live_r:
        pf_s   = f"{m['pf']:.2f}" if m["pf"] != float("inf") else "∞"
        health = "✅ strong" if m["pf"] >= 1.5 else ("⚠ marginal" if m["pf"] >= 1.0 else "❌ losing")
        print(f"    {c:<6} PF {pf_s} | WR {m['wr']}% | {m['n']} trades | ~{m['mo_trades']}/mo | {health}")

    # ── Section 3: Combined projection ───────────────────────────────────────
    top_add = [c for c, m in strong] + [c for c, m in viable[:2]]
    if top_add:
        portfolio = list(LIVE_COINS) + top_add
        combined  = [t for c in portfolio if c in results for t in results[c]]
        combined.sort(key=lambda t: str(t["entry_ts"]))
        m_all = stats(combined)
        if m_all:
            print("\n" + "═"*80)
            print(f"  SECTION 3 — IF WE ADD: {', '.join(top_add)}")
            print("═"*80)
            print(f"  Portfolio ({len(portfolio)} coins): {', '.join(portfolio)}")
            print(f"  Combined: {m_all['n']} trades | WR {m_all['wr']}% | PF {m_all['pf']:.2f} | EV {m_all['ev']:+.3f}%")
            print(f"  Est monthly: {m_all['mo_trades']} trades | +${m_all['mo_usd']:,.0f}")
            print(f"  (Live 1M count ~3-5× higher → proportionally more $)\n")
            print(f"  {'Month':<10} {'Trades':>8} {'Wins':>6} {'WR':>6} {'PnL $':>10}")
            print(f"  {'-'*44}")
            for r in monthly_breakdown(combined):
                print(f"  {r['month']:<10} {r['n']:>8} {r['wins']:>6} {r['wr']:>5}% {r['usd']:>+10,.0f}")

    # ── Section 4: Direction breakdown ───────────────────────────────────────
    print("\n" + "═"*70)
    print("  SECTION 4 — LONG vs SHORT (which direction works per coin)")
    print(f"  {'Coin':<6} {'LONG':>5} {'L WR':>6} {'SHORT':>6} {'S WR':>6}  Note")
    print("  " + "-"*52)
    for coin, m, _ in ranked:
        if m["n"] < 3:
            continue
        note = ""
        if m["long_wr"] >= 60 and m["short_wr"] < 40:
            note = "→ LONG-biased"
        elif m["short_wr"] >= 60 and m["long_wr"] < 40:
            note = "→ SHORT-biased"
        elif m["long_wr"] >= 50 and m["short_wr"] >= 50:
            note = "→ both work"
        print(f"  {coin:<6} {m['long_n']:>5} {m['long_wr']:>5}% {m['short_n']:>6} {m['short_wr']:>5}%  {note}")

    print("\nDone.\n")
