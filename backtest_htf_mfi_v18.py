"""
HTF MFI Scalper — v1.8 Candidate Backtest
═══════════════════════════════════════════
Tests 3 proposed improvements against current baseline on new coin set.

Coins : BTC, DOT, SUI, WLD, INJ (current 8085 after 2026-04-03 swap)
Period: 180 days

Configs tested:
  A — Baseline    : Session 07-22 | MFI ≥ 80 | flat sizing $2,500
  B — Session only: Session 07-17 | MFI ≥ 80 | flat sizing $2,500
  C — MFI only    : Session 07-22 | MFI ≥ 85 | flat sizing $2,500
  D — B + C       : Session 07-17 | MFI ≥ 85 | flat sizing $2,500
  E — Full v1.8   : Session 07-17 | MFI ≥ 85 | conviction sizing
                    MFI 85-89 → $3,750 (1.5×) | MFI 90+ → $5,000 (2×) | below 85 → skip

All other params fixed (v1.7): SL 1.5% | Trail gap 0.5% | 1D+6H direction | ADX ≥ 18
"""

import ccxt
import pandas as pd
import numpy as np
import time
from datetime import datetime, timedelta, timezone

exchange = ccxt.binanceusdm({"enableRateLimit": True, "timeout": 30000})

DAYS_BACK      = 180
EMA_PERIOD     = 20
EMA_SLOPE_BARS = 3
ADX_PERIOD     = 14
ADX_MIN        = 18
MFI_PERIOD     = 14
SL_PCT         = 1.5
TRAIL_START    = 1.0
TRAIL_STEP     = 0.5
TRAIL_GAP      = 0.5
BASE_NOTIONAL  = 2_500   # $500 margin × 5×

COINS = ["BTC", "DOT", "SUI", "WLD", "INJ"]

# ── Configs ───────────────────────────────────────────────────────────────────
CONFIGS = {
    "A — Baseline":     {"session_end": 22, "mfi_ob": 80, "conviction": False},
    "B — Session 07-17":{"session_end": 17, "mfi_ob": 80, "conviction": False},
    "C — MFI ≥ 85":     {"session_end": 22, "mfi_ob": 85, "conviction": False},
    "D — Sess+MFI85":   {"session_end": 17, "mfi_ob": 85, "conviction": False},
    "E — Full v1.8":    {"session_end": 17, "mfi_ob": 85, "conviction": True},
}


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
        ok = calc_adx(df) >= adx_min
        dirs[above &  slope & ok] = "LONG"
        dirs[~above & ~slope & ok] = "SHORT"
    else:
        dirs[above &  slope] = "LONG"
        dirs[~above & ~slope] = "SHORT"
    return dirs


# ── Data fetch ────────────────────────────────────────────────────────────────
def fetch_df(symbol, tf, since_ms):
    all_c, since = [], since_ms
    while True:
        try:
            c = exchange.fetch_ohlcv(symbol, tf, since=since, limit=1000)
        except Exception as e:
            print(f"    [{symbol} {tf}] {e}"); break
        if not c: break
        all_c.extend(c)
        if len(c) < 1000: break
        since = c[-1][0] + 1
        time.sleep(0.05)
    if not all_c: return pd.DataFrame()
    df = pd.DataFrame(all_c, columns=["ts","open","high","low","close","volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.drop_duplicates("ts").set_index("ts").sort_index().astype(float)


# ── Trail simulation ──────────────────────────────────────────────────────────
def simulate_trail(entry, direction, future_df):
    if future_df.empty: return 0.0
    sl = entry * (1 + SL_PCT/100) if direction == "SHORT" else entry * (1 - SL_PCT/100)
    peak = 0.0
    for _, row in future_df.iloc[:200].iterrows():
        hi, lo = row["high"], row["low"]
        if direction == "SHORT":
            curr = (entry - lo) / entry * 100
            if curr > peak:
                peak = curr
                m = int(peak / TRAIL_STEP) * TRAIL_STEP
                if m >= TRAIL_START:
                    locked = 0.0 if m == TRAIL_START else m - TRAIL_GAP
                    sl = min(sl, entry * (1 - locked/100))
            if hi >= sl:
                return round((entry - sl) / entry * 100, 4)
        else:
            curr = (hi - entry) / entry * 100
            if curr > peak:
                peak = curr
                m = int(peak / TRAIL_STEP) * TRAIL_STEP
                if m >= TRAIL_START:
                    locked = 0.0 if m == TRAIL_START else m - TRAIL_GAP
                    sl = max(sl, entry * (1 + locked/100))
            if lo <= sl:
                return round((sl - entry) / entry * 100, 4)
    last = float(future_df.iloc[-1]["close"])
    return round(((entry - last) if direction == "SHORT" else (last - entry)) / entry * 100, 4)


# ── Core backtest ─────────────────────────────────────────────────────────────
def backtest(coin_data, session_end, mfi_ob, conviction):
    """Run across all coins with given config. Returns trade list."""
    cutoff   = pd.Timestamp(datetime.now(timezone.utc) - timedelta(days=DAYS_BACK))
    all_trades = []

    for coin in COINS:
        if coin not in coin_data: continue
        d1h = coin_data[coin]["1h"]
        d6h = coin_data[coin]["6h"]
        d1d = coin_data[coin]["1d"]

        d1h_bt = d1h[d1h.index >= cutoff].copy()
        if len(d1h_bt) < MFI_PERIOD + 5: continue

        # Direction: 1D (ADX) + 6H, ffill to 1H
        dir_1d = dir_series(d1d, adx_min=ADX_MIN) if not d1d.empty else pd.Series("FLAT", index=d1d.index)
        dir_6h = dir_series(d6h)                   if not d6h.empty else pd.Series("FLAT", index=d6h.index)
        dir_1d_ff = dir_1d.reindex(d1h_bt.index, method="ffill").fillna("FLAT")
        dir_6h_ff = dir_6h.reindex(d1h_bt.index, method="ffill").fillna("FLAT")
        agree = dir_1d_ff == dir_6h_ff
        dir_comb = pd.Series("FLAT", index=d1h_bt.index)
        dir_comb[agree & (dir_1d_ff == "LONG")]  = "LONG"
        dir_comb[agree & (dir_1d_ff == "SHORT")] = "SHORT"

        # MFI
        d1h_bt["mfi"] = calc_mfi(d1h_bt)
        mfi = d1h_bt["mfi"]

        # MFI crosses (use base 80 for conviction sizing, custom threshold for filtering)
        mfi_cross_80 = (mfi.shift(1) < 80)  & (mfi >= 80)   # always detect 80 for sizing ref
        mfi_cross_ob = (mfi.shift(1) < mfi_ob) & (mfi >= mfi_ob)  # actual entry threshold
        os_cross     = (mfi.shift(1) > 20)  & (mfi <= 20)

        session = (d1h_bt.index.hour >= 7) & (d1h_bt.index.hour < session_end)

        in_cd, cd_side = False, None

        for i in range(MFI_PERIOD + 2, len(d1h_bt) - 1):
            if not session[i]: continue
            mfi_now = float(mfi.iloc[i])

            if in_cd:
                if cd_side == "above50" and mfi_now > 50: in_cd = False
                elif cd_side == "below50" and mfi_now < 50: in_cd = False
                else: continue

            direction = dir_comb.iloc[i]
            if direction == "FLAT": continue

            is_short = bool(mfi_cross_ob.iloc[i]) and direction == "SHORT"
            is_long  = bool(os_cross.iloc[i])     and direction == "LONG"
            if not is_short and not is_long: continue

            side  = "SHORT" if is_short else "LONG"
            entry = float(d1h_bt["close"].iloc[i])

            # Conviction sizing
            if conviction:
                mfi_val = float(mfi.iloc[i])
                if mfi_val >= 90:
                    notional = BASE_NOTIONAL * 2.0
                    size_tag = "2×"
                elif mfi_val >= 85:
                    notional = BASE_NOTIONAL * 1.5
                    size_tag = "1.5×"
                else:
                    notional = BASE_NOTIONAL
                    size_tag = "1×"
            else:
                notional = BASE_NOTIONAL
                size_tag = "1×"

            future = d1h_bt.iloc[i + 1:]
            pnl_pct = simulate_trail(entry, side, future)
            pnl_usd = round(pnl_pct / 100 * notional, 2)

            # Session label
            h = d1h_bt.index[i].hour
            if 13 <= h < 17:   sess = "NY Open"
            elif 7  <= h < 10: sess = "London"
            else:               sess = "Off-Peak"

            all_trades.append({
                "coin":      coin,
                "entry_ts":  d1h_bt.index[i],
                "direction": side,
                "mfi":       float(mfi.iloc[i]),
                "entry":     entry,
                "pnl_pct":   pnl_pct,
                "pnl_usd":   pnl_usd,
                "notional":  notional,
                "size_tag":  size_tag,
                "session":   sess,
                "win":       pnl_pct > 0.05,
            })
            in_cd   = True
            cd_side = "above50" if side == "SHORT" else "below50"

    return all_trades


# ── Stats ─────────────────────────────────────────────────────────────────────
def stats(trades):
    if not trades: return None
    wins   = [t for t in trades if t["win"]]
    losses = [t for t in trades if not t["win"]]
    pnls   = [t["pnl_pct"] for t in trades]
    usd    = [t["pnl_usd"] for t in trades]
    gw     = sum(t["pnl_pct"] for t in wins)
    gl     = abs(sum(t["pnl_pct"] for t in losses))
    pf     = round(gw/gl, 2) if gl > 0 else (float("inf") if gw > 0 else 0.0)
    mo     = DAYS_BACK / 30
    return {
        "n":        len(trades),
        "wins":     len(wins),
        "losses":   len(losses),
        "wr":       round(len(wins)/len(trades)*100, 1),
        "pf":       pf,
        "ev":       round(np.mean(pnls), 3),
        "total_usd":round(sum(usd), 0),
        "mo_trades":round(len(trades)/mo, 1),
        "mo_usd":   round(sum(usd)/mo, 0),
        "avg_win":  round(np.mean([t["pnl_pct"] for t in wins]),  3) if wins   else 0,
        "avg_loss": round(np.mean([t["pnl_pct"] for t in losses]),3) if losses else 0,
    }

def per_coin_stats(trades):
    by_coin = {}
    for t in trades:
        by_coin.setdefault(t["coin"], []).append(t)
    return {c: stats(v) for c, v in by_coin.items()}

def per_session_stats(trades):
    by_sess = {}
    for t in trades:
        by_sess.setdefault(t["session"], []).append(t)
    return {s: stats(v) for s, v in by_sess.items()}

def monthly_breakdown(trades):
    by_mo = {}
    for t in trades:
        mo = str(t["entry_ts"])[:7]
        by_mo.setdefault(mo, []).append(t)
    return [{"month": mo, "n": len(v),
             "wins":  sum(1 for t in v if t["win"]),
             "wr":    round(sum(1 for t in v if t["win"])/len(v)*100, 0),
             "usd":   round(sum(t["pnl_usd"] for t in v), 0)}
            for mo in sorted(by_mo) for v in [by_mo[mo]]]


# ── Main ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("\n" + "═"*80)
    print("  HTF MFI SCALPER — v1.8 CANDIDATE BACKTEST")
    print(f"  Coins: {', '.join(COINS)} | 180d | SL {SL_PCT}% | Trail {TRAIL_GAP}% | ADX≥{ADX_MIN}")
    print("═"*80)

    print("\nFetching data...")
    since_1h  = int((datetime.now(timezone.utc) - timedelta(days=DAYS_BACK + 5)).timestamp()  * 1000)
    since_htf = int((datetime.now(timezone.utc) - timedelta(days=DAYS_BACK + 35)).timestamp() * 1000)
    coin_data = {}
    for coin in COINS:
        sym = f"{coin}/USDT:USDT"
        print(f"  {coin}", end=" ", flush=True)
        try:
            d1h = fetch_df(sym, "1h",  since_1h)
            d6h = fetch_df(sym, "6h",  since_htf)
            d1d = fetch_df(sym, "1d",  since_htf)
            coin_data[coin] = {"1h": d1h, "6h": d6h, "1d": d1d}
            print(f"→ {len(d1h)} 1H candles")
        except Exception as e:
            print(f"→ ERROR: {e}")

    # ── Run all configs ───────────────────────────────────────────────────────
    results = {}
    print("\nRunning configs...")
    for name, cfg in CONFIGS.items():
        trades = backtest(coin_data, cfg["session_end"], cfg["mfi_ob"], cfg["conviction"])
        results[name] = trades
        m = stats(trades)
        if m:
            print(f"  {name:<22} → {m['n']:>3} trades | WR {m['wr']:>5}% | PF {m['pf']:>5} | ~{m['mo_trades']:>4}/mo | +${m['mo_usd']:>6,.0f}/mo")

    # ── Section 1: Config comparison ─────────────────────────────────────────
    print("\n" + "═"*95)
    print("  SECTION 1 — CONFIG COMPARISON (all 5 coins combined)")
    print(f"  {'Config':<22} {'Trades':>7} {'WR':>6} {'PF':>6} {'EV%':>7} "
          f"{'Mo Trd':>7} {'Mo $':>9} {'Total $':>10} {'AvgW':>7} {'AvgL':>7}")
    print("  " + "-"*93)
    for name, trades in results.items():
        m = stats(trades)
        if not m: continue
        pf_s = f"{m['pf']:.2f}" if m['pf'] != float("inf") else "∞"
        best = " ◄ BEST" if name == max(results, key=lambda k: (stats(results[k]) or {}).get("mo_usd", -999)) else ""
        print(f"  {name:<22} {m['n']:>7} {m['wr']:>5}% {pf_s:>6} {m['ev']:>+6.3f}% "
              f"{m['mo_trades']:>7} {m['mo_usd']:>+9,.0f} {m['total_usd']:>+10,.0f} "
              f"{m['avg_win']:>+6.3f}% {m['avg_loss']:>+6.3f}%{best}")

    # ── Section 2: Per-coin breakdown for best config ─────────────────────────
    best_name = max(results, key=lambda k: (stats(results[k]) or {}).get("mo_usd", -999))
    best_trades = results[best_name]
    print(f"\n{'═'*80}")
    print(f"  SECTION 2 — PER-COIN BREAKDOWN: {best_name}")
    print(f"{'═'*80}")
    print(f"  {'Coin':<6} {'Trades':>7} {'WR':>6} {'PF':>6} {'EV%':>7} {'Mo Trd':>7} {'Mo $':>9}")
    print("  " + "-"*55)
    for coin, m in per_coin_stats(best_trades).items():
        if not m: continue
        pf_s = f"{m['pf']:.2f}" if m['pf'] != float("inf") else "∞"
        print(f"  {coin:<6} {m['n']:>7} {m['wr']:>5}% {pf_s:>6} {m['ev']:>+6.3f}% "
              f"{m['mo_trades']:>7} {m['mo_usd']:>+9,.0f}")

    # ── Section 3: Session breakdown for each config ──────────────────────────
    print(f"\n{'═'*80}")
    print("  SECTION 3 — SESSION BREAKDOWN PER CONFIG")
    print(f"{'═'*80}")
    sessions = ["NY Open", "London", "Off-Peak"]
    print(f"  {'Config':<22}", end="")
    for s in sessions:
        print(f"  {s:<12} WR     PF   Mo$", end="")
    print()
    print("  " + "-"*90)
    for name, trades in results.items():
        ss = per_session_stats(trades)
        print(f"  {name:<22}", end="")
        for s in sessions:
            m = ss.get(s)
            if m and m["n"] >= 2:
                pf_s = f"{m['pf']:.1f}" if m['pf'] != float("inf") else "∞"
                print(f"  {m['n']:>3}tr {m['wr']:>4}%WR {pf_s:>4}PF {m['mo_usd']:>+5,.0f}", end="")
            else:
                print(f"  {'—':>30}", end="")
        print()

    # ── Section 4: Conviction sizing breakdown (Config E) ────────────────────
    e_trades = results.get("E — Full v1.8", [])
    if e_trades:
        print(f"\n{'═'*70}")
        print("  SECTION 4 — CONVICTION SIZING BREAKDOWN (Config E)")
        print(f"{'═'*70}")
        for size in ["1×", "1.5×", "2×"]:
            t = [x for x in e_trades if x["size_tag"] == size]
            m = stats(t)
            if m:
                pf_s = f"{m['pf']:.2f}" if m['pf'] != float("inf") else "∞"
                mfi_label = {"1×": "MFI 80-84", "1.5×": "MFI 85-89", "2×": "MFI 90+"}[size]
                print(f"  {size} ({mfi_label:<12}): {m['n']:>3} trades | WR {m['wr']:>5}% | "
                      f"PF {pf_s:>5} | Mo ${m['mo_usd']:>+6,.0f}")

    # ── Section 5: Monthly consistency ───────────────────────────────────────
    print(f"\n{'═'*70}")
    print(f"  SECTION 5 — MONTHLY PnL CONSISTENCY: {best_name}")
    print(f"{'═'*70}")
    print(f"  {'Month':<10} {'Trades':>8} {'Wins':>6} {'WR':>6} {'PnL $':>10}")
    print(f"  {'-'*44}")
    positive = negative = 0
    for row in monthly_breakdown(best_trades):
        sign = "+" if row["usd"] >= 0 else ""
        flag = " ✅" if row["usd"] > 0 else " ❌"
        print(f"  {row['month']:<10} {row['n']:>8} {row['wins']:>6} {row['wr']:>5}% {row['usd']:>+10,.0f}{flag}")
        if row["usd"] >= 0: positive += 1
        else: negative += 1
    print(f"\n  Profitable months: {positive} / {positive+negative}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'═'*70}")
    print("  SUMMARY — RECOMMENDATION")
    print(f"{'═'*70}")
    base_m = stats(results["A — Baseline"])
    best_m = stats(results[best_name])
    if base_m and best_m:
        trade_delta = best_m["n"] - base_m["n"]
        usd_delta   = best_m["mo_usd"] - base_m["mo_usd"]
        wr_delta    = best_m["wr"] - base_m["wr"]
        pf_delta    = best_m["pf"] - base_m["pf"]
        print(f"\n  Baseline (Config A):   {base_m['n']} trades | WR {base_m['wr']}% | PF {base_m['pf']:.2f} | +${base_m['mo_usd']:,.0f}/mo")
        print(f"  Best config ({best_name[:3]}): {best_m['n']} trades | WR {best_m['wr']}% | PF {best_m['pf']:.2f} | +${best_m['mo_usd']:,.0f}/mo")
        print(f"\n  Delta: {trade_delta:+} trades | WR {wr_delta:+.1f}% | PF {pf_delta:+.2f} | ${usd_delta:+,.0f}/mo")
        print(f"\n  Verdict: {'UPGRADE' if best_m['mo_usd'] > base_m['mo_usd'] else 'MARGINAL'} — "
              f"{'apply v1.8' if best_m['pf'] > base_m['pf'] else 'keep baseline'}")
    print()
