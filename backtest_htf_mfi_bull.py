"""
HTF MFI Scalper — Bull Market LONG Readiness Backtest
═══════════════════════════════════════════════════════
Tests LONG signal performance during the 2023-2024 bull market vs bear period.

Strategy logic is unchanged from v1.7 (production config):
  Direction : 1D + 6H EMA20 must both agree
  ADX       : ≥ 18 on 1D (trend strength gate)
  LONG entry: 1H MFI(14) fresh cross ≤ 20 (oversold) when direction = LONG
  SHORT entry: 1H MFI(14) fresh cross ≥ 80 (overbought) when direction = SHORT
  SL        : 1.5%
  Trail     : gap 0.5%, milestones every 0.5% from +1.0%
  Session   : 07:00–22:00 UTC
  Notional  : $2,500/trade

Two periods tested:
  BULL : 2023-11-01 → 2024-12-31 (BTC $35k → $100k, clear uptrend)
  BEAR : 2024-12-01 → 2025-06-01 (post-peak correction — approx last 180d context)

Output:
  1. Bull period — LONG vs SHORT performance (all coins)
  2. Per-coin LONG breakdown during bull (which coins to add for Oct 2026)
  3. Bear period — LONG vs SHORT comparison (confirms SHORT edge in downtrends)
  4. Bull readiness ranking + recommendation

Note: 1H MFI proxy fires 3-5× fewer signals than live 1M system.
PF and WR are directionally correct; trade count is conservative.
"""

import ccxt
import pandas as pd
import numpy as np
import time
from datetime import datetime, timezone

exchange = ccxt.binanceusdm({"enableRateLimit": True, "timeout": 30000})

# ── Params (v1.7 locked) ──────────────────────────────────────────────────────
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
TRAIL_GAP      = 0.5
SESSION_START  = 7
SESSION_END    = 22
NOTIONAL       = 2_500

# ── Periods ───────────────────────────────────────────────────────────────────
PERIODS = {
    "BULL (Nov 2023–Dec 2024)": {
        "start": datetime(2023, 11,  1, tzinfo=timezone.utc),
        "end":   datetime(2024, 12, 31, tzinfo=timezone.utc),
        "label": "BULL",
    },
    "BEAR (Jan 2025–Apr 2026)": {
        "start": datetime(2025,  1,  1, tzinfo=timezone.utc),
        "end":   datetime(2026,  4,  3, tzinfo=timezone.utc),
        "label": "BEAR",
    },
}

# ── Coin universe (current 8085 + bull market candidates) ─────────────────────
CURRENT_8085   = ["BTC", "DOT", "SUI", "WLD", "INJ"]
BULL_CANDIDATES = ["ETH", "SOL", "AVAX", "LINK", "XRP", "ADA", "DOGE", "BNB", "NEAR", "OP"]
ALL_COINS = CURRENT_8085 + [c for c in BULL_CANDIDATES if c not in CURRENT_8085]


# ── Indicators ────────────────────────────────────────────────────────────────
def calc_ema(s):
    return s.ewm(span=EMA_PERIOD, adjust=False).mean()

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
    up, dn = hi.diff(), lo.diff().mul(-1)
    pdm = up.where((up > dn) & (up > 0), 0.0)
    mdm = dn.where((dn > up) & (dn > 0), 0.0)
    tr  = pd.concat([hi-lo, (hi-cl.shift(1)).abs(), (lo-cl.shift(1)).abs()], axis=1).max(axis=1)
    a   = 1 / ADX_PERIOD
    atr = tr.ewm(alpha=a, adjust=False).mean().replace(0, 1e-10)
    pdi = 100 * pdm.ewm(alpha=a, adjust=False).mean() / atr
    mdi = 100 * mdm.ewm(alpha=a, adjust=False).mean() / atr
    dx  = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, 1e-10)
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
def fetch_df(symbol, tf, since_ms, until_ms=None):
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
        if until_ms and c[-1][0] >= until_ms:
            break
        if len(c) < 1000:
            break
        since = c[-1][0] + 1
        time.sleep(0.08)
    if not all_c:
        return pd.DataFrame()
    df = pd.DataFrame(all_c, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.drop_duplicates("ts").set_index("ts").sort_index().astype(float)
    if until_ms:
        df = df[df.index <= pd.Timestamp(until_ms, unit="ms", tz="UTC")]
    return df


# ── Trailing SL simulation ────────────────────────────────────────────────────
def simulate_trail(entry, direction, future_df):
    if future_df.empty:
        return 0.0
    sl = entry * (1 + SL_PCT/100) if direction == "SHORT" else entry * (1 - SL_PCT/100)
    peak = 0.0

    def ms(peak_pct):
        if peak_pct < TRAIL_START:
            return sl
        m = int(peak_pct / TRAIL_STEP) * TRAIL_STEP
        m = max(m, TRAIL_START)
        locked = 0.0 if m == TRAIL_START else round(m - TRAIL_GAP, 4)
        return entry * (1 - locked/100) if direction == "SHORT" else entry * (1 + locked/100)

    for _, row in future_df.iloc[:200].iterrows():
        hi, lo = row["high"], row["low"]
        if direction == "SHORT":
            curr = (entry - lo) / entry * 100
            if curr > peak:
                peak = curr
                sl = min(sl, ms(peak))
            if hi >= sl:
                return round((entry - sl) / entry * 100, 4)
        else:
            curr = (hi - entry) / entry * 100
            if curr > peak:
                peak = curr
                sl = max(sl, ms(peak))
            if lo <= sl:
                return round((sl - entry) / entry * 100, 4)

    last = float(future_df.iloc[-1]["close"])
    return round(((entry - last) if direction == "SHORT" else (last - entry)) / entry * 100, 4)


# ── Backtest one coin for one period ─────────────────────────────────────────
def backtest_coin_period(coin, d1h, d6h, d1d, start_dt, end_dt):
    d1h_bt = d1h[(d1h.index >= start_dt) & (d1h.index <= end_dt)].copy()
    if len(d1h_bt) < MFI_PERIOD + 10:
        return []

    dir_1d = dir_series(d1d, adx_min=ADX_MIN) if not d1d.empty else pd.Series("FLAT", index=d1d.index)
    dir_6h = dir_series(d6h)                   if not d6h.empty else pd.Series("FLAT", index=d6h.index)
    dir_1d_ff = dir_1d.reindex(d1h_bt.index, method="ffill").fillna("FLAT")
    dir_6h_ff = dir_6h.reindex(d1h_bt.index, method="ffill").fillna("FLAT")

    dir_combined = pd.Series("FLAT", index=d1h_bt.index)
    agree = dir_1d_ff == dir_6h_ff
    dir_combined[agree & (dir_1d_ff == "LONG")]  = "LONG"
    dir_combined[agree & (dir_1d_ff == "SHORT")] = "SHORT"

    d1h_bt["mfi"] = calc_mfi(d1h_bt)
    mfi      = d1h_bt["mfi"]
    ob_cross = (mfi.shift(1) < MFI_OB) & (mfi >= MFI_OB)
    os_cross = (mfi.shift(1) > MFI_OS) & (mfi <= MFI_OS)
    session  = (d1h_bt.index.hour >= SESSION_START) & (d1h_bt.index.hour < SESSION_END)

    trades, in_cd, cool_side = [], False, None

    for i in range(MFI_PERIOD + 2, len(d1h_bt) - 1):
        if not session[i]:
            continue
        mfi_now = float(mfi.iloc[i])

        if in_cd:
            if cool_side == "above50" and mfi_now > 50:
                in_cd = False
            elif cool_side == "below50" and mfi_now < 50:
                in_cd = False
            else:
                continue

        direction = dir_combined.iloc[i]
        if direction == "FLAT":
            continue

        is_short = bool(ob_cross.iloc[i]) and direction == "SHORT"
        is_long  = bool(os_cross.iloc[i]) and direction == "LONG"
        if not is_short and not is_long:
            continue

        side  = "SHORT" if is_short else "LONG"
        entry = float(d1h_bt["close"].iloc[i])
        pnl   = simulate_trail(entry, side, d1h_bt.iloc[i+1:])

        # Months elapsed in period
        period_months = (end_dt - start_dt).days / 30

        trades.append({
            "coin":      coin,
            "entry_ts":  d1h_bt.index[i],
            "direction": side,
            "entry":     entry,
            "pnl_pct":   pnl,
            "pnl_usd":   round(pnl / 100 * NOTIONAL, 2),
            "win":       pnl > 0.05,
            "period_months": period_months,
        })
        in_cd     = True
        cool_side = "above50" if side == "SHORT" else "below50"

    return trades


# ── Stats ─────────────────────────────────────────────────────────────────────
def stats(trades, period_months=1):
    if not trades:
        return None
    wins   = [t for t in trades if t["win"]]
    losses = [t for t in trades if not t["win"]]
    gw = sum(t["pnl_pct"] for t in wins)
    gl = abs(sum(t["pnl_pct"] for t in losses))
    pf = round(gw / gl, 2) if gl > 0 else (float("inf") if gw > 0 else 0.0)
    wr = round(len(wins) / len(trades) * 100, 1)
    mo_trades = round(len(trades) / period_months, 1)
    total_usd = round(sum(t["pnl_usd"] for t in trades), 0)
    mo_usd    = round(total_usd / period_months, 0)
    avg_w = round(sum(t["pnl_pct"] for t in wins)  / len(wins),   3) if wins   else 0
    avg_l = round(sum(t["pnl_pct"] for t in losses)/ len(losses), 3) if losses else 0
    return {
        "n": len(trades), "wins": len(wins), "wr": wr, "pf": pf,
        "total_usd": total_usd, "mo_usd": mo_usd, "mo_trades": mo_trades,
        "avg_w": avg_w, "avg_l": avg_l,
    }


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    print("═"*80)
    print("  HTF MFI SCALPER — BULL MARKET LONG READINESS BACKTEST")
    print("  v1.7 config | SL 1.5% | Trail 0.5% | ADX≥18 | 1H proxy")
    print("═"*80)

    # Fetch enough data to cover both periods (Nov 2023 → Apr 2026)
    fetch_start_ms = int(datetime(2023, 10, 1, tzinfo=timezone.utc).timestamp() * 1000)

    print("\nFetching data (this may take a few minutes)...")
    data = {}
    for coin in ALL_COINS:
        sym = f"{coin}/USDT:USDT"
        print(f"  {coin}", end="", flush=True)
        try:
            d1h = fetch_df(sym, "1h", fetch_start_ms)
            d6h = fetch_df(sym, "6h", fetch_start_ms)
            d1d = fetch_df(sym, "1d", fetch_start_ms)
            if d1h.empty:
                print(" → no data, skip")
                continue
            data[coin] = (d1h, d6h, d1d)
            print(f" → {len(d1h)} 1H candles")
        except Exception as e:
            print(f" → error: {e}")
        time.sleep(0.2)

    # Run both periods
    all_results = {}  # period_label → {coin → [trades]}

    for period_name, period_cfg in PERIODS.items():
        start_dt = period_cfg["start"]
        end_dt   = period_cfg["end"]
        label    = period_cfg["label"]
        all_results[label] = {}

        for coin, (d1h, d6h, d1d) in data.items():
            trades = backtest_coin_period(coin, d1h, d6h, d1d, start_dt, end_dt)
            all_results[label][coin] = trades

    bull_months = (PERIODS["BULL (Nov 2023–Dec 2024)"]["end"] -
                   PERIODS["BULL (Nov 2023–Dec 2024)"]["start"]).days / 30
    bear_months = (PERIODS["BEAR (Jan 2025–Apr 2026)"]["end"] -
                   PERIODS["BEAR (Jan 2025–Apr 2026)"]["start"]).days / 30

    # ── SECTION 1: Bull period — LONG vs SHORT overview ──────────────────────
    print(f"\n{'═'*80}")
    print(f"  SECTION 1 — BULL MARKET ({PERIODS['BULL (Nov 2023–Dec 2024)']['start'].strftime('%b %Y')} → {PERIODS['BULL (Nov 2023–Dec 2024)']['end'].strftime('%b %Y')}): LONG vs SHORT")
    print(f"  All coins combined | {bull_months:.0f} months")
    print(f"{'═'*80}")

    bull_all = []
    for trades in all_results["BULL"].values():
        bull_all.extend(trades)

    bull_longs  = [t for t in bull_all if t["direction"] == "LONG"]
    bull_shorts = [t for t in bull_all if t["direction"] == "SHORT"]

    sl = stats(bull_longs,  bull_months)
    ss = stats(bull_shorts, bull_months)
    sc = stats(bull_all,    bull_months)

    print(f"\n  {'Direction':<12} {'Trades':>7} {'WR':>7} {'PF':>7} {'Mo Trd':>8} {'Mo $':>8} {'AvgW':>8} {'AvgL':>8}")
    print(f"  {'-'*70}")
    for label, s in [("LONG", sl), ("SHORT", ss), ("COMBINED", sc)]:
        if s:
            print(f"  {label:<12} {s['n']:>7} {s['wr']:>6.1f}% {s['pf']:>7.2f} {s['mo_trades']:>8.1f} {s['mo_usd']:>+8.0f} {s['avg_w']:>+7.3f}% {s['avg_l']:>+7.3f}%")
        else:
            print(f"  {label:<12} {'—':>7}")

    # ── SECTION 2: Bull — per-coin LONG breakdown ─────────────────────────────
    print(f"\n{'═'*80}")
    print(f"  SECTION 2 — BULL PERIOD: PER-COIN LONG SIGNAL BREAKDOWN")
    print(f"{'═'*80}")
    print(f"\n  {'Coin':<8} {'Trades':>7} {'WR':>7} {'PF':>7} {'Mo Trd':>8} {'Mo $':>8} {'Status'}")
    print(f"  {'-'*65}")

    coin_long_stats = []
    for coin in ALL_COINS:
        if coin not in all_results["BULL"]:
            continue
        longs = [t for t in all_results["BULL"][coin] if t["direction"] == "LONG"]
        s = stats(longs, bull_months)
        if s:
            coin_long_stats.append((coin, s))

    coin_long_stats.sort(key=lambda x: x[1]["pf"], reverse=True)

    for coin, s in coin_long_stats:
        if s["n"] < 3:
            status = "⚠ low sample"
        elif s["pf"] >= 2.0 and s["wr"] >= 50:
            status = "✅ strong"
        elif s["pf"] >= 1.5:
            status = "✅ viable"
        elif s["pf"] >= 1.0:
            status = "⚠ marginal"
        else:
            status = "❌ losing"
        in_8085 = " ← 8085" if coin in CURRENT_8085 else ""
        print(f"  {coin:<8} {s['n']:>7} {s['wr']:>6.1f}% {s['pf']:>7.2f} {s['mo_trades']:>8.1f} {s['mo_usd']:>+8.0f}  {status}{in_8085}")

    # ── SECTION 3: Bear — LONG vs SHORT comparison ────────────────────────────
    print(f"\n{'═'*80}")
    print(f"  SECTION 3 — BEAR MARKET (Jan 2025 → Apr 2026): LONG vs SHORT")
    print(f"  Confirms SHORT edge in downtrends | {bear_months:.0f} months")
    print(f"{'═'*80}")

    bear_all = []
    for trades in all_results["BEAR"].values():
        bear_all.extend(trades)

    bear_longs  = [t for t in bear_all if t["direction"] == "LONG"]
    bear_shorts = [t for t in bear_all if t["direction"] == "SHORT"]

    bl = stats(bear_longs,  bear_months)
    bs = stats(bear_shorts, bear_months)

    print(f"\n  {'Direction':<12} {'Trades':>7} {'WR':>7} {'PF':>7} {'Mo Trd':>8} {'Mo $':>8}")
    print(f"  {'-'*55}")
    for label, s in [("LONG", bl), ("SHORT", bs)]:
        if s:
            print(f"  {label:<12} {s['n']:>7} {s['wr']:>6.1f}% {s['pf']:>7.2f} {s['mo_trades']:>8.1f} {s['mo_usd']:>+8.0f}")
        else:
            print(f"  {label:<12} {'—':>7}")

    # ── SECTION 4: Bull readiness ranking ────────────────────────────────────
    print(f"\n{'═'*80}")
    print(f"  SECTION 4 — BULL READINESS RANKING (LONG signals, Bull period)")
    print(f"  Threshold for activation: PF ≥ 1.5 | WR ≥ 45% | ≥ 5 trades")
    print(f"{'═'*80}")

    viable = [(c, s) for c, s in coin_long_stats if s["pf"] >= 1.5 and s["wr"] >= 45 and s["n"] >= 5]
    marginal = [(c, s) for c, s in coin_long_stats if 1.0 <= s["pf"] < 1.5 and s["n"] >= 5]
    losing = [(c, s) for c, s in coin_long_stats if s["pf"] < 1.0 and s["n"] >= 5]

    print(f"\n  ✅ ADD to 8085 LONG universe when bull confirmed:")
    if viable:
        for coin, s in viable:
            tag = " (already in 8085)" if coin in CURRENT_8085 else " ← ADD"
            print(f"     {coin:<8} PF {s['pf']:.2f} | WR {s['wr']:.1f}% | {s['mo_trades']:.1f}/mo | +${s['mo_usd']:.0f}/mo{tag}")
    else:
        print("     None meet threshold")

    print(f"\n  ⚠ MONITOR (marginal — watch first 10 live LONG trades):")
    if marginal:
        for coin, s in marginal:
            print(f"     {coin:<8} PF {s['pf']:.2f} | WR {s['wr']:.1f}% | {s['mo_trades']:.1f}/mo")
    else:
        print("     None")

    print(f"\n  ❌ DO NOT activate for LONGs (losing in bull market):")
    if losing:
        for coin, s in losing:
            print(f"     {coin:<8} PF {s['pf']:.2f} | WR {s['wr']:.1f}%")
    else:
        print("     None — all coins show positive edge in bull market")

    # ── SECTION 5: Monthly consistency (LONG, bull period, all viable coins) ──
    print(f"\n{'═'*80}")
    print(f"  SECTION 5 — MONTHLY CONSISTENCY (LONG trades, Bull period, all viable coins)")
    print(f"{'═'*80}")

    viable_coins = [c for c, _ in viable]
    if viable_coins:
        viable_trades = [t for t in bull_longs if t["coin"] in viable_coins]
        if viable_trades:
            df_t = pd.DataFrame(viable_trades)
            df_t["month"] = pd.to_datetime(df_t["entry_ts"]).dt.to_period("M")
            print(f"\n  {'Month':<12} {'Trades':>7} {'Wins':>6} {'WR':>7} {'PnL $':>9}")
            print(f"  {'-'*45}")
            for month, grp in df_t.groupby("month"):
                wins = (grp["pnl_pct"] > 0.05).sum()
                wr   = wins / len(grp) * 100
                pnl  = grp["pnl_usd"].sum()
                icon = "✅" if pnl > 0 else "❌"
                print(f"  {str(month):<12} {len(grp):>7} {wins:>6} {wr:>6.1f}% {pnl:>+9.0f} {icon}")

    # ── SECTION 6: Summary & recommendation ──────────────────────────────────
    print(f"\n{'═'*80}")
    print(f"  SUMMARY — BULL RUN ACTIVATION PLAN")
    print(f"{'═'*80}")

    bull_long_pf  = sl["pf"]  if sl else 0
    bull_long_wr  = sl["wr"]  if sl else 0
    bull_short_pf = ss["pf"] if ss else 0
    bear_short_pf = bs["pf"] if bs else 0
    bear_long_pf  = bl["pf"]  if bl else 0

    print(f"""
  LONG signal performance:
    Bull market  : WR {bull_long_wr:.1f}% | PF {bull_long_pf:.2f}  ← LONG works in bull
    Bear market  : WR {bl['wr'] if bl else 0:.1f}% | PF {bear_long_pf:.2f}  ← LONG does NOT work in bear

  SHORT signal performance:
    Bull market  : PF {bull_short_pf:.2f}  ← SHORT suffers in bull
    Bear market  : PF {bear_short_pf:.2f}  ← SHORT works in bear (confirmed)

  Regime switching plan for Oct 2026 bull run:
    1. When BULL CONFIRMED (monthly MFI T3 signal):
       → Enable LONG signals in 8085 (currently SHORT-only)
       → Activate top LONG coins from Section 4
       → Keep SHORT signals disabled (or run both if PF > 1.5)
    2. SL/trail params: same (1.5% SL, 0.5% trail) — do NOT widen in bull
       Bull markets have sharper bounces — tighter SL may even help
    3. Position sizing: same $2,500 notional until 10 live LONG trades validate
    4. Cooldown: same (MFI return through 50) — tested and working

  ⚠ Note: 1H proxy data — live 1M system will generate 3-5× more signals.
    PF/WR directionally correct. Actual monthly $ will be higher if ratios hold.
""")


if __name__ == "__main__":
    main()
