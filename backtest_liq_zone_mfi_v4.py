#!/usr/bin/env python3
"""
Liquidation Zone MFI Reversal — Backtest v4 (Deep / Regime-Aware)
──────────────────────────────────────────────────────────────────
Key insight: Bull and Bear markets have opposite optimal directions.
  BULL (before 2025-10-05): LONG signals dominate
  BEAR (after  2025-10-05): SHORT signals dominate

Strategy:
  BTC  : MFI reversal + zone proximity (price within 2% of swing high/low)
  Alts : Follow BTC signal timestamp + MFI alignment filter
         ETH tested with 2.0×ATR SL (larger SL to handle ETH volatility)

Tests per regime:
  - WR, PF, Net PnL separately for BULL and BEAR periods
  - TP grids: TP1 at 3%, 4%, 5% / TP2 at 6%, 8%, 10%
  - Per coin: best TP config in each regime

Market regimes:
  BULL: 2022-11-01 → 2025-10-04 (~1064 days)
  BEAR: 2025-10-05 → present (~364 days ongoing)
"""

import ccxt
import pandas as pd
import numpy as np
import time
import warnings
from datetime import datetime, timedelta, timezone
from itertools import product

warnings.filterwarnings("ignore")

# ─── CONFIG ──────────────────────────────────────────────────────────────────
BTC_SYMBOL    = "BTC/USDT:USDT"
ALT_SYMBOLS   = {
    "ETH":  "ETH/USDT:USDT",
    "XRP":  "XRP/USDT:USDT",
    "AVAX": "AVAX/USDT:USDT",
    "SUI":  "SUI/USDT:USDT",
}
TIMEFRAME     = "6h"
MFI_LENGTH    = 14
ATR_LENGTH    = 14
MFI_OB        = 80
MFI_OS        = 20
LOOKBACK_DAYS = 1100      # back to ~Nov 2022, capturing full bull market
MAX_HOLD      = 42
SWING_LOOKBACK = 40

BULL_END = pd.Timestamp("2025-10-05", tz="UTC")   # bear starts here

# BTC zone filter: use 2% (best from v3)
BTC_ZONE_PCT = 2.0

# Alt SL multipliers
ALT_SL_MULTS = {
    "ETH":  2.0,   # wider SL for ETH as user confirmed it works
    "XRP":  1.5,
    "AVAX": 1.5,
    "SUI":  1.5,
}
BTC_SL_MULT = 1.5

# TP grid: test combinations
TP1_OPTIONS = [3.0, 4.0, 5.0]    # partial close (50%) at this %
TP2_OPTIONS = [6.0, 8.0, 10.0]   # full exit at this %
PARTIAL_PCT = 0.5
# ─────────────────────────────────────────────────────────────────────────────


def calc_mfi(df, length=14):
    tp  = (df["high"] + df["low"] + df["close"]) / 3
    rmf = tp * df["volume"]
    pos = rmf.where(tp > tp.shift(1), 0.0)
    neg = rmf.where(tp < tp.shift(1), 0.0)
    ps  = pos.rolling(length).sum()
    ns  = neg.rolling(length).sum().replace(0, 1e-10)
    return 100 - (100 / (1 + ps / ns))


def calc_atr(df, length=14):
    hl  = df["high"] - df["low"]
    hpc = (df["high"] - df["close"].shift(1)).abs()
    lpc = (df["low"]  - df["close"].shift(1)).abs()
    tr  = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
    return tr.rolling(length).mean()


def fetch_candles(exchange, symbol, days):
    since = exchange.parse8601(
        (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")
    )
    all_candles = []
    while True:
        batch = exchange.fetch_ohlcv(symbol, TIMEFRAME, since=since, limit=1000)
        if not batch:
            break
        all_candles += batch
        if len(batch) < 1000:
            break
        since = batch[-1][0] + 1
        time.sleep(0.15)
    df = pd.DataFrame(all_candles, columns=["ts","open","high","low","close","volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df.set_index("ts", inplace=True)
    df.drop_duplicates(inplace=True)
    return df


def find_btc_signals(df):
    signals = []
    mfi    = df["mfi"].values
    atr    = df["atr"].values
    opens  = df["open"].values
    closes = df["close"].values
    highs  = df["high"].values
    lows   = df["low"].values

    for i in range(SWING_LOOKBACK + MFI_LENGTH + 5, len(df) - MAX_HOLD - 2):
        cur_mfi   = mfi[i]
        prev_mfis = mfi[i-5:i]
        cur_atr   = atr[i]
        entry     = closes[i]
        if np.isnan(cur_mfi) or np.isnan(cur_atr) or cur_atr == 0:
            continue

        start = max(0, i - SWING_LOOKBACK)
        swing_high = highs[start:i].max()
        swing_low  = lows[start:i].min()

        green = closes[i] > opens[i]
        red   = closes[i] < opens[i]
        ts    = df.index[i]
        regime = "BEAR" if ts >= BULL_END else "BULL"

        # LONG: MFI oversold reversal + price near swing low
        if np.any(prev_mfis < MFI_OS) and cur_mfi > MFI_OS and green:
            dist = (entry - swing_low) / entry * 100
            if dist <= BTC_ZONE_PCT:
                signals.append({
                    "idx": i, "ts": ts, "direction": "LONG",
                    "entry": entry, "atr": cur_atr,
                    "mfi": round(cur_mfi,1), "regime": regime,
                    "dist_from_zone": round(dist,2),
                })

        # SHORT: MFI overbought reversal + price near swing high
        elif np.any(prev_mfis > MFI_OB) and cur_mfi < MFI_OB and red:
            dist = (swing_high - entry) / entry * 100
            if dist <= BTC_ZONE_PCT:
                signals.append({
                    "idx": i, "ts": ts, "direction": "SHORT",
                    "entry": entry, "atr": cur_atr,
                    "mfi": round(cur_mfi,1), "regime": regime,
                    "dist_from_zone": round(dist,2),
                })

    return signals


def find_alt_signals(btc_signals, alt_df, direction_filter=None):
    """
    Map BTC signals to alt candles. direction_filter: 'LONG', 'SHORT', or None for all.
    MFI alignment: LONG → alt MFI < 60, SHORT → alt MFI > 40
    """
    signals = []
    for sig in btc_signals:
        if direction_filter and sig["direction"] != direction_filter:
            continue
        ts = sig["ts"]
        try:
            if ts in alt_df.index:
                i = alt_df.index.get_loc(ts)
            else:
                diff = (alt_df.index - ts).abs()
                i = diff.argmin()
                if diff[i].total_seconds() > 7200:
                    continue
        except Exception:
            continue

        if i >= len(alt_df) - MAX_HOLD - 2:
            continue

        row = alt_df.iloc[i]
        alt_mfi = row["mfi"]
        alt_atr = row["atr"]
        alt_entry = row["close"]

        if np.isnan(alt_mfi) or np.isnan(alt_atr) or alt_atr == 0:
            continue

        direction = sig["direction"]
        if direction == "LONG" and alt_mfi > 60:
            continue
        if direction == "SHORT" and alt_mfi < 40:
            continue

        signals.append({
            "idx": i, "ts": alt_df.index[i],
            "direction": direction, "entry": alt_entry,
            "atr": alt_atr, "mfi": round(alt_mfi,1),
            "regime": sig["regime"],
        })
    return signals


def simulate_trade(df, sig, sl_mult, tp1_pct, tp2_pct):
    i      = sig["idx"]
    entry  = sig["entry"]
    atr    = sig["atr"]
    direct = sig["direction"]
    sl_dist = atr * sl_mult

    if direct == "LONG":
        sl  = entry - sl_dist
        tp1 = entry * (1 + tp1_pct/100)
        tp2 = entry * (1 + tp2_pct/100)
    else:
        sl  = entry + sl_dist
        tp1 = entry * (1 - tp1_pct/100)
        tp2 = entry * (1 - tp2_pct/100)

    partial_done = False
    partial_pnl  = 0.0
    final_pnl    = 0.0
    outcome      = "TIMEOUT"
    tp1_hit      = False
    tp2_hit      = False
    max_fav      = 0.0

    highs  = df["high"].values
    lows   = df["low"].values
    closes = df["close"].values

    for j in range(i+1, min(i+1+MAX_HOLD, len(df))):
        h, l = highs[j], lows[j]

        if direct == "LONG":
            max_fav = max(max_fav, (h-entry)/entry*100)
            if l <= sl:
                rem = (sl-entry)/entry*100
                final_pnl = partial_pnl + rem*(0.5 if partial_done else 1.0)
                outcome = "BE+" if final_pnl > 0.05 else "LOSS"
                break
            if not partial_done and h >= tp1:
                partial_pnl  = (tp1-entry)/entry*100 * PARTIAL_PCT
                partial_done = True
                tp1_hit      = True
            if h >= tp2:
                final_pnl = partial_pnl + (tp2-entry)/entry*100*(1-PARTIAL_PCT if partial_done else 1.0)
                outcome = "WIN"; tp2_hit = True; break
        else:
            max_fav = max(max_fav, (entry-l)/entry*100)
            if h >= sl:
                rem = (entry-sl)/entry*100
                final_pnl = partial_pnl + rem*(0.5 if partial_done else 1.0)
                outcome = "BE+" if final_pnl > 0.05 else "LOSS"
                break
            if not partial_done and l <= tp1:
                partial_pnl  = (entry-tp1)/entry*100 * PARTIAL_PCT
                partial_done = True
                tp1_hit      = True
            if l <= tp2:
                final_pnl = partial_pnl + (entry-tp2)/entry*100*(1-PARTIAL_PCT if partial_done else 1.0)
                outcome = "WIN"; tp2_hit = True; break
    else:
        last = closes[min(i+MAX_HOLD, len(df)-1)]
        rem = ((last-entry)/entry*100 if direct=="LONG" else (entry-last)/entry*100)
        final_pnl = partial_pnl + rem*(0.5 if partial_done else 1.0)
        outcome = "TIMEOUT"

    return {
        "outcome":     outcome,
        "final_pnl":   round(final_pnl, 3),
        "tp1_hit":     tp1_hit,
        "tp2_hit":     tp2_hit,
        "max_fav_pct": round(max_fav, 2),
        "sl_pct":      round(sl_dist/entry*100, 2),
    }


def run_grid(df, signals, sl_mult, label=""):
    """Run TP1×TP2 grid, return best config per regime."""
    best = {}
    all_rows = []

    for tp1, tp2 in product(TP1_OPTIONS, TP2_OPTIONS):
        if tp2 <= tp1:
            continue

        records = []
        for sig in signals:
            r = simulate_trade(df, sig, sl_mult, tp1, tp2)
            r.update(sig)
            records.append(r)

        if not records:
            continue

        rdf = pd.DataFrame(records)

        for regime in ["BULL", "BEAR", "ALL"]:
            if regime == "ALL":
                sub = rdf
            else:
                sub = rdf[rdf["regime"] == regime]

            if len(sub) < 3:
                continue

            wins   = sub[sub["outcome"] == "WIN"]
            losses = sub[sub["outcome"] == "LOSS"]
            be     = sub[sub["outcome"] == "BE+"]
            tout   = sub[sub["outcome"] == "TIMEOUT"]
            n      = len(sub)

            wr   = len(wins)/n*100
            net  = sub["final_pnl"].sum()
            pf   = (wins["final_pnl"].sum() / abs(losses["final_pnl"].sum())
                    if len(losses) and losses["final_pnl"].sum() != 0 else 99.0)
            avg_w = wins["final_pnl"].mean() if len(wins) else 0
            avg_l = losses["final_pnl"].mean() if len(losses) else 0

            longs  = sub[sub["direction"]=="LONG"]
            shorts = sub[sub["direction"]=="SHORT"]
            l_wr = (longs["outcome"]=="WIN").sum()/len(longs)*100 if len(longs) else 0
            s_wr = (shorts["outcome"]=="WIN").sum()/len(shorts)*100 if len(shorts) else 0

            row = {
                "label": label, "regime": regime,
                "tp1": tp1, "tp2": tp2, "sl_mult": sl_mult,
                "n": n, "n_long": len(longs), "n_short": len(shorts),
                "wr": round(wr,1), "wr_long": round(l_wr,1), "wr_short": round(s_wr,1),
                "pf": round(min(pf,99),2), "net": round(net,1),
                "avg_win": round(avg_w,2), "avg_loss": round(avg_l,2),
                "tp1_rate": round(sub["tp1_hit"].sum()/n*100,1),
                "tp2_rate": round(sub["tp2_hit"].sum()/n*100,1),
                "mfe": round(sub["max_fav_pct"].mean(),2),
                "avg_sl": round(sub["sl_pct"].mean(),2),
                "spm": round(n/(LOOKBACK_DAYS/30),1),
                "n_win": len(wins), "n_loss": len(losses),
                "n_be": len(be), "n_timeout": len(tout),
            }
            all_rows.append(row)

            key = f"{label}_{regime}"
            if key not in best or net > best[key]["net"]:
                best[key] = row

    return all_rows, best


def print_best(b, title):
    if not b:
        return
    print(f"\n  {title}")
    for key in ["BULL","BEAR","ALL"]:
        k2 = f"{b.get('label','')}_{key}"
        # Find from dict
        pass

    # Print directly
    wr_note = ""
    dir_note = "LONG" if b.get("wr_long",0) > b.get("wr_short",0) else "SHORT"
    print(f"    Regime   : {b['regime']}  |  Best direction: {dir_note}")
    print(f"    TP config: TP1={b['tp1']}% (50%) → TP2={b['tp2']}%  |  SL={b['sl_mult']}×ATR")
    print(f"    Trades   : {b['n']} ({b['spm']}/month)  L:{b['n_long']} S:{b['n_short']}")
    print(f"    WR       : {b['wr']}%  (Long:{b['wr_long']}%  Short:{b['wr_short']}%)")
    print(f"    PF       : {b['pf']}  |  Net: {b['net']:+.1f}%")
    print(f"    Avg W/L  : +{b['avg_win']}% / {b['avg_loss']}%")
    print(f"    TP1/TP2  : {b['tp1_rate']}% / {b['tp2_rate']}% hit")
    print(f"    MFE      : {b['mfe']}%  |  Avg SL: {b['avg_sl']}%")
    print(f"    Outcomes : WIN={b['n_win']} LOSS={b['n_loss']} BE+={b['n_be']} TIMEOUT={b['n_timeout']}")


def main():
    print("=" * 72)
    print("  Liq Zone MFI Reversal v4 — Regime-Aware Deep Backtest")
    print(f"  {LOOKBACK_DAYS}d history | BULL ends 2025-10-05 | BEAR ongoing")
    print(f"  BTC zone≤{BTC_ZONE_PCT}% | TP grid: {TP1_OPTIONS}×{TP2_OPTIONS}")
    print("=" * 72)

    exchange = ccxt.binance({"options": {"defaultType": "future"}})

    # ── Fetch BTC ──────────────────────────────────────────────────────────────
    print("\n  Fetching BTC...", flush=True)
    btc_df = fetch_candles(exchange, BTC_SYMBOL, LOOKBACK_DAYS)
    btc_df["mfi"] = calc_mfi(btc_df, MFI_LENGTH)
    btc_df["atr"] = calc_atr(btc_df, ATR_LENGTH)

    btc_sigs = find_btc_signals(btc_df)
    bull_sigs = [s for s in btc_sigs if s["regime"]=="BULL"]
    bear_sigs = [s for s in btc_sigs if s["regime"]=="BEAR"]
    print(f"  BTC signals: {len(btc_sigs)} total | BULL:{len(bull_sigs)} BEAR:{len(bear_sigs)}")

    all_results = []
    all_bests   = {}

    # ── BTC Grid ───────────────────────────────────────────────────────────────
    print("\n" + "─"*72)
    print("  BTC — Zone-Filtered Grid (BULL vs BEAR)")
    print("─"*72)
    rows, best = run_grid(btc_df, btc_sigs, BTC_SL_MULT, label="BTC")
    all_results.extend(rows)
    all_bests.update(best)

    for regime in ["BULL","BEAR","ALL"]:
        b = best.get(f"BTC_{regime}")
        if b:
            print_best(b, f"BTC — {regime} best config:")

    # ── Alt Grids ──────────────────────────────────────────────────────────────
    print("\n" + "─"*72)
    print("  ALT FOLLOWING — BTC-driven signals, regime-separated")
    print("─"*72)

    alt_dfs = {}
    for coin, symbol in ALT_SYMBOLS.items():
        print(f"\n  Fetching {coin}...", flush=True)
        try:
            df = fetch_candles(exchange, symbol, LOOKBACK_DAYS)
            df["mfi"] = calc_mfi(df, MFI_LENGTH)
            df["atr"] = calc_atr(df, ATR_LENGTH)
            alt_dfs[coin] = df

            sl_mult = ALT_SL_MULTS[coin]
            alt_sigs = find_alt_signals(btc_sigs, df)
            alt_bull  = [s for s in alt_sigs if s["regime"]=="BULL"]
            alt_bear  = [s for s in alt_sigs if s["regime"]=="BEAR"]
            print(f"    Signals: {len(alt_sigs)} | BULL:{len(alt_bull)} BEAR:{len(alt_bear)}")

            rows, best = run_grid(df, alt_sigs, sl_mult, label=coin)
            all_results.extend(rows)
            all_bests.update(best)

            for regime in ["BULL","BEAR","ALL"]:
                b = best.get(f"{coin}_{regime}")
                if b:
                    print_best(b, f"{coin} — {regime} best config:")

        except Exception as e:
            print(f"  ERROR {coin}: {e}")
        time.sleep(0.5)

    # ── Regime Summary Table ───────────────────────────────────────────────────
    print("\n" + "="*72)
    print("  REGIME SUMMARY — Best config per coin per regime")
    print("  (This is what to trade and when)")
    print("="*72)

    coins_order = ["BTC", "ETH", "XRP", "AVAX", "SUI"]
    for regime in ["BULL","BEAR"]:
        direction = "LONG" if regime == "BULL" else "SHORT"
        print(f"\n  ── {regime} MARKET ({direction}S) ─────────────────────────────────")
        print(f"  {'Coin':<8} {'N':>5} {'SPM':>5} {'WR':>7} {'WR-dir':>8} {'PF':>6} {'Net%':>8} "
              f"{'TP1%':>6} {'TP2%':>6} {'AvgW':>7} {'AvgL':>7} {'AvgSL':>7}")
        print(f"  {'─'*8}-+-{'─'*5}-+-{'─'*5}-+-{'─'*7}-+-{'─'*8}-+-{'─'*6}-+-{'─'*8}-+-"
              f"{'─'*6}-+-{'─'*6}-+-{'─'*7}-+-{'─'*7}-+-{'─'*7}")
        for coin in coins_order:
            b = all_bests.get(f"{coin}_{regime}")
            if not b:
                print(f"  {coin:<8} {'—':>5}")
                continue
            wr_dir = b["wr_long"] if direction == "LONG" else b["wr_short"]
            n_dir  = b["n_long"] if direction == "LONG" else b["n_short"]
            pf_str = f"{b['pf']:.2f}" if b['pf'] < 50 else " >50"
            print(f"  {coin:<8} {b['n']:>5} {b['spm']:>5.1f} {b['wr']:>6.1f}% {wr_dir:>7.1f}% "
                  f"{pf_str:>6} {b['net']:>+8.1f}% "
                  f"{b['tp1']:>4.0f}%/{b['tp2']:>3.0f}% "
                  f"{b['avg_win']:>+7.2f}% {b['avg_loss']:>7.2f}% {b['avg_sl']:>7.2f}%")

    # ── TP Config Guide ────────────────────────────────────────────────────────
    print("\n" + "="*72)
    print("  OPTIMAL TP CONFIG PER COIN PER REGIME")
    print("  (For implementation: use these exact TP1/TP2 values)")
    print("="*72)
    print(f"\n  {'Coin':<8} {'Regime':<8} {'SL':<10} {'TP1 (50%)':<12} {'TP2 (50%)':<12} {'WR':<8} {'PF'}")
    print(f"  {'─'*8} {'─'*8} {'─'*10} {'─'*12} {'─'*12} {'─'*8} {'─'*6}")
    for coin in coins_order:
        sl = ALT_SL_MULTS.get(coin, BTC_SL_MULT)
        for regime in ["BULL","BEAR"]:
            b = all_bests.get(f"{coin}_{regime}")
            if not b:
                continue
            direction = "LONG" if regime=="BULL" else "SHORT"
            wr_dir = b["wr_long"] if direction=="LONG" else b["wr_short"]
            pf_str = f"{b['pf']:.2f}" if b['pf'] < 50 else ">50"
            print(f"  {coin:<8} {regime:<8} {sl}×ATR     {b['tp1']}%          {b['tp2']}%          "
                  f"{wr_dir:.1f}%    {pf_str}")

    # ── Key Insights ──────────────────────────────────────────────────────────
    print("\n" + "="*72)
    print("  KEY INSIGHTS")
    print("="*72)

    # Find signal counts for current bear
    print("\n  CURRENT MARKET (BEAR — until ~Oct 2026):")
    for coin in coins_order:
        b = all_bests.get(f"{coin}_BEAR")
        if b:
            wr_s = b["wr_short"]
            print(f"    {coin:<6}: SHORT WR {wr_s:.1f}%  |  PF {b['pf']:.2f}  |  "
                  f"TP1={b['tp1']}% TP2={b['tp2']}%  |  {b['n_short']} signals ({b['spm']:.1f}/month)")

    print("\n  NEXT BULL (~Oct 2026 onwards):")
    for coin in coins_order:
        b = all_bests.get(f"{coin}_BULL")
        if b:
            wr_l = b["wr_long"]
            print(f"    {coin:<6}: LONG  WR {wr_l:.1f}%  |  PF {b['pf']:.2f}  |  "
                  f"TP1={b['tp1']}% TP2={b['tp2']}%  |  {b['n_long']} signals ({b['spm']:.1f}/month)")

    # Save full results
    out = "/Users/macbook/Downloads/Content Projects/02 - Trading Coin Hunter/Trade Logs/backtest_liq_zone_v4.csv"
    pd.DataFrame(all_results).to_csv(out, index=False)
    print(f"\n  Full results saved → {out}")
    print("="*72)


if __name__ == "__main__":
    main()
