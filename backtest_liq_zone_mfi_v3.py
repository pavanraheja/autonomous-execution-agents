#!/usr/bin/env python3
"""
Liquidation Zone MFI Reversal — Backtest v3
────────────────────────────────────────────
Strategy A — BTC (zone-filtered):
  Signal : MFI oversold (<20) or overbought (>80) reversal
  Filter : Price must be within ZONE_PROXIMITY_PCT of a recent swing high (SHORT)
           or swing low (LONG) — approximates ChartPrime liquidation zones
  Entry  : First reversal candle after MFI cross

Strategy B — Alt Following (ETH, XRP, AVAX, SUI):
  BTC fires a signal → enter same direction on alt immediately
  Alt confirmation: alt MFI must be on correct side (< 50 for LONG, > 50 for SHORT)
                    and alt is NOT in extreme opposite zone

Tests:
  - BTC: Zone proximity thresholds 1%, 2%, 3%, 4%
  - Alts: No zone filter (test BTC-signal-only) vs MFI alignment filter
  - SL: 1.5×ATR | TP: 3%, 5% fixed | Partial 50% at TP1, rest at TP2
"""

import ccxt
import pandas as pd
import numpy as np
import time
import warnings
from datetime import datetime, timedelta, timezone

warnings.filterwarnings("ignore")

# ─── CONFIG ──────────────────────────────────────────────────────────────────
BTC_SYMBOL     = "BTC/USDT:USDT"
ALT_SYMBOLS    = [
    "ETH/USDT:USDT",
    "XRP/USDT:USDT",
    "AVAX/USDT:USDT",
    "SUI/USDT:USDT",
]
TIMEFRAME      = "6h"
MFI_LENGTH     = 14
ATR_LENGTH     = 14
MFI_OB         = 80
MFI_OS         = 20
LOOKBACK_DAYS  = 730
MAX_HOLD       = 42        # candles
SWING_LOOKBACK = 40        # candles to find swing high/low

# BTC zone proximity thresholds to test
ZONE_THRESHOLDS = [1.0, 2.0, 3.0, 4.0]  # % within swing high/low

# Fixed TP levels (partial 50% + full)
TP1_PCT  = 4.0   # partial TP (50% close)
TP2_PCT  = 7.0   # full TP (remaining 50%)
SL_MULT  = 1.5   # ATR multiplier for SL
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

    df = pd.DataFrame(all_candles, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df.set_index("ts", inplace=True)
    df.drop_duplicates(inplace=True)
    return df


def find_swing_levels(df, i, lookback):
    """Return most recent significant swing high and swing low in last N candles."""
    start = max(0, i - lookback)
    window_highs = df["high"].iloc[start:i]
    window_lows  = df["low"].iloc[start:i]
    swing_high = window_highs.max()
    swing_low  = window_lows.min()
    return swing_high, swing_low


def find_btc_signals(df, zone_thresh):
    """
    Find BTC signals with zone proximity filter.
    LONG:  MFI was <20 in last 5, crossed back above 20, green candle,
           AND price within zone_thresh% ABOVE the recent swing low
    SHORT: MFI was >80 in last 5, crossed back below 80, red candle,
           AND price within zone_thresh% BELOW the recent swing high
    """
    signals = []
    mfi    = df["mfi"].values
    atr    = df["atr"].values
    opens  = df["open"].values
    closes = df["close"].values

    for i in range(SWING_LOOKBACK + MFI_LENGTH + 5, len(df) - MAX_HOLD - 2):
        cur_mfi   = mfi[i]
        prev_mfis = mfi[i-5:i]
        cur_atr   = atr[i]
        entry     = closes[i]

        if np.isnan(cur_mfi) or np.isnan(cur_atr) or cur_atr == 0:
            continue

        green = closes[i] > opens[i]
        red   = closes[i] < opens[i]

        swing_high, swing_low = find_swing_levels(df, i, SWING_LOOKBACK)

        # LONG: MFI oversold reversal + price near swing low (green zone)
        if np.any(prev_mfis < MFI_OS) and cur_mfi > MFI_OS and green:
            dist_from_low_pct = (entry - swing_low) / entry * 100
            if dist_from_low_pct <= zone_thresh:
                signals.append({
                    "idx":               i,
                    "ts":                df.index[i],
                    "direction":         "LONG",
                    "entry":             entry,
                    "atr":               cur_atr,
                    "mfi":               round(cur_mfi, 1),
                    "swing_low":         round(swing_low, 2),
                    "swing_high":        round(swing_high, 2),
                    "dist_from_zone":    round(dist_from_low_pct, 2),
                    "zone_thresh":       zone_thresh,
                })

        # SHORT: MFI overbought reversal + price near swing high (red zone)
        elif np.any(prev_mfis > MFI_OB) and cur_mfi < MFI_OB and red:
            dist_from_high_pct = (swing_high - entry) / entry * 100
            if dist_from_high_pct <= zone_thresh:
                signals.append({
                    "idx":               i,
                    "ts":                df.index[i],
                    "direction":         "SHORT",
                    "entry":             entry,
                    "atr":               cur_atr,
                    "mfi":               round(cur_mfi, 1),
                    "swing_low":         round(swing_low, 2),
                    "swing_high":        round(swing_high, 2),
                    "dist_from_zone":    round(dist_from_high_pct, 2),
                    "zone_thresh":       zone_thresh,
                })

    return signals


def find_alt_signals(btc_signals, alt_df):
    """
    For each BTC signal timestamp, find the matching candle in alt_df.
    Filter: alt MFI must be aligned with BTC direction
      - LONG:  alt MFI < 60 (not overbought, has room to run)
      - SHORT: alt MFI > 40 (not oversold, has room to run)
    """
    signals = []
    for sig in btc_signals:
        ts = sig["ts"]
        # Find nearest candle in alt (exact match or closest)
        if ts not in alt_df.index:
            # Find closest timestamp
            diff = (alt_df.index - ts).abs()
            closest_idx = diff.argmin()
            if diff[closest_idx].total_seconds() > 7200:  # more than 2H off
                continue
            alt_row = alt_df.iloc[closest_idx]
            i = closest_idx
        else:
            i = alt_df.index.get_loc(ts)
            alt_row = alt_df.iloc[i]

        if i >= len(alt_df) - MAX_HOLD - 2:
            continue

        alt_mfi = alt_row["mfi"]
        alt_atr = alt_row["atr"]
        alt_entry = alt_row["close"]

        if np.isnan(alt_mfi) or np.isnan(alt_atr) or alt_atr == 0:
            continue

        direction = sig["direction"]

        # MFI alignment filter
        if direction == "LONG" and alt_mfi > 60:
            continue   # alt already overbought — don't chase
        if direction == "SHORT" and alt_mfi < 40:
            continue   # alt already oversold — don't chase

        signals.append({
            "idx":        i,
            "ts":         alt_df.index[i],
            "direction":  direction,
            "entry":      alt_entry,
            "atr":        alt_atr,
            "mfi":        round(alt_mfi, 1),
            "btc_ts":     ts,
        })

    return signals


def simulate_trade(df, sig):
    """
    Simulate with partial TP:
    - TP1 (50% close) at TP1_PCT
    - TP2 (50% remaining) at TP2_PCT
    - SL at SL_MULT × ATR
    Returns outcome dict.
    """
    i      = sig["idx"]
    entry  = sig["entry"]
    atr    = sig["atr"]
    direct = sig["direction"]

    sl_dist = atr * SL_MULT

    if direct == "LONG":
        sl   = entry - sl_dist
        tp1  = entry * (1 + TP1_PCT / 100)
        tp2  = entry * (1 + TP2_PCT / 100)
    else:
        sl   = entry + sl_dist
        tp1  = entry * (1 - TP1_PCT / 100)
        tp2  = entry * (1 - TP2_PCT / 100)

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

    for j in range(i + 1, min(i + 1 + MAX_HOLD, len(df))):
        h = highs[j]
        l = lows[j]

        if direct == "LONG":
            max_fav = max(max_fav, (h - entry) / entry * 100)
            if l <= sl:
                if partial_done:
                    final_pnl = partial_pnl + (sl - entry) / entry * 100 * 0.5
                else:
                    final_pnl = (sl - entry) / entry * 100
                outcome = "BE+" if final_pnl > 0.05 else "LOSS"
                break
            if not partial_done and h >= tp1:
                partial_pnl  = (tp1 - entry) / entry * 100 * 0.5
                partial_done = True
                tp1_hit      = True
            if h >= tp2:
                final_pnl = partial_pnl + (tp2 - entry) / entry * 100 * 0.5
                outcome   = "WIN"
                tp2_hit   = True
                break
        else:
            max_fav = max(max_fav, (entry - l) / entry * 100)
            if h >= sl:
                if partial_done:
                    final_pnl = partial_pnl + (entry - sl) / entry * 100 * 0.5
                else:
                    final_pnl = (entry - sl) / entry * 100
                outcome = "BE+" if final_pnl > 0.05 else "LOSS"
                break
            if not partial_done and l <= tp1:
                partial_pnl  = (entry - tp1) / entry * 100 * 0.5
                partial_done = True
                tp1_hit      = True
            if l <= tp2:
                final_pnl = partial_pnl + (entry - tp2) / entry * 100 * 0.5
                outcome   = "WIN"
                tp2_hit   = True
                break
    else:
        last = closes[min(i + MAX_HOLD, len(df) - 1)]
        if direct == "LONG":
            rem = (last - entry) / entry * 100 * (0.5 if partial_done else 1.0)
        else:
            rem = (entry - last) / entry * 100 * (0.5 if partial_done else 1.0)
        final_pnl = partial_pnl + rem
        outcome   = "TIMEOUT"

    return {
        "outcome":     outcome,
        "final_pnl":   round(final_pnl, 3),
        "tp1_hit":     tp1_hit,
        "tp2_hit":     tp2_hit,
        "max_fav_pct": round(max_fav, 2),
        "sl_pct":      round(sl_dist / entry * 100, 2),
    }


def summarise(records, label):
    if not records:
        return None
    rdf = pd.DataFrame(records)
    n   = len(rdf)
    wins     = rdf[rdf["outcome"] == "WIN"]
    losses   = rdf[rdf["outcome"] == "LOSS"]
    timeouts = rdf[rdf["outcome"] == "TIMEOUT"]
    be_plus  = rdf[rdf["outcome"] == "BE+"]

    wr   = len(wins) / n * 100
    pf   = (wins["final_pnl"].sum() / abs(losses["final_pnl"].sum())
            if len(losses) and losses["final_pnl"].sum() != 0 else float("inf"))
    net  = rdf["final_pnl"].sum()
    avg_win  = wins["final_pnl"].mean() if len(wins) else 0
    avg_loss = losses["final_pnl"].mean() if len(losses) else 0
    tp1_rate = rdf["tp1_hit"].sum() / n * 100
    tp2_rate = rdf["tp2_hit"].sum() / n * 100
    mfe      = rdf["max_fav_pct"].mean()
    avg_sl   = rdf["sl_pct"].mean()

    longs  = rdf[rdf["direction"] == "LONG"]
    shorts = rdf[rdf["direction"] == "SHORT"]
    l_wr   = (longs["outcome"] == "WIN").sum() / len(longs) * 100 if len(longs) else 0
    s_wr   = (shorts["outcome"] == "WIN").sum() / len(shorts) * 100 if len(shorts) else 0

    return {
        "label":    label,
        "n":        n,
        "n_long":   len(longs),
        "n_short":  len(shorts),
        "wr":       round(wr, 1),
        "wr_long":  round(l_wr, 1),
        "wr_short": round(s_wr, 1),
        "pf":       round(pf, 2),
        "net":      round(net, 1),
        "avg_win":  round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "tp1_rate": round(tp1_rate, 1),
        "tp2_rate": round(tp2_rate, 1),
        "mfe":      round(mfe, 2),
        "avg_sl":   round(avg_sl, 2),
        "spm":      round(n / (LOOKBACK_DAYS / 30), 1),
        "n_win":    len(wins),
        "n_loss":   len(losses),
        "n_be":     len(be_plus),
        "n_timeout":len(timeouts),
    }


def print_summary(s, indent="  "):
    if not s:
        print(f"{indent}  No trades.")
        return
    print(f"{indent}  Trades : {s['n']} total ({s['spm']}/month) | Long:{s['n_long']} Short:{s['n_short']}")
    print(f"{indent}  WR     : {s['wr']}%  (L:{s['wr_long']}%  S:{s['wr_short']}%)")
    print(f"{indent}  PF     : {s['pf']}  |  Net: {s['net']:+.1f}%  |  Avg Win/Loss: +{s['avg_win']}% / {s['avg_loss']}%")
    print(f"{indent}  TP1    : {s['tp1_rate']}% hit  |  TP2: {s['tp2_rate']}% hit")
    print(f"{indent}  Outcomes: WIN={s['n_win']} LOSS={s['n_loss']} BE+={s['n_be']} TIMEOUT={s['n_timeout']}")
    print(f"{indent}  Avg MFE: {s['mfe']}%  |  Avg SL dist: {s['avg_sl']}%")


def main():
    print("=" * 72)
    print("  Liq Zone MFI Reversal v3 — Zone Filter + Alt Following")
    print(f"  BTC 6H | {LOOKBACK_DAYS}d | TP1={TP1_PCT}% (50%) + TP2={TP2_PCT}% (50%) | SL={SL_MULT}×ATR")
    print("=" * 72)

    exchange = ccxt.binance({"options": {"defaultType": "future"}})

    # ── 1. Fetch and prepare BTC ──────────────────────────────────────────────
    print("\n  Fetching BTC 6H data...", flush=True)
    btc_df = fetch_candles(exchange, BTC_SYMBOL, LOOKBACK_DAYS)
    btc_df["mfi"] = calc_mfi(btc_df, MFI_LENGTH)
    btc_df["atr"] = calc_atr(btc_df, ATR_LENGTH)

    # ── 2. BTC — Zone Proximity Filter Grid ──────────────────────────────────
    print("\n" + "─" * 72)
    print("  STRATEGY A: BTC — MFI Reversal + Zone Proximity Filter")
    print(f"  (price must be within X% of recent swing high/low)")
    print("─" * 72)

    btc_results_by_thresh = {}
    all_btc_rows = []

    for thresh in ZONE_THRESHOLDS:
        sigs = find_btc_signals(btc_df, thresh)
        records = []
        for sig in sigs:
            r = simulate_trade(btc_df, sig)
            r.update(sig)
            records.append(r)

        s = summarise(records, f"BTC zone≤{thresh}%")
        if s:
            btc_results_by_thresh[thresh] = (sigs, records, s)
            all_btc_rows.append(s)
            print(f"\n  Zone ≤ {thresh}%  ({s['n']} trades, {s['spm']}/month):")
            print_summary(s)

    # ── 3. Alt Following Strategy ─────────────────────────────────────────────
    print("\n" + "─" * 72)
    print("  STRATEGY B: Alt Following — BTC signals drive alt entries")
    print(f"  (Alt filter: MFI < 60 for LONG, > 40 for SHORT)")
    print("─" * 72)

    # Use BTC signals at best zone threshold for alt following
    # Best = highest n for coverage, then filter by WR — use 3% as baseline
    base_thresh = 3.0
    if base_thresh in btc_results_by_thresh:
        btc_base_sigs = btc_results_by_thresh[base_thresh][0]
    else:
        btc_base_sigs = find_btc_signals(btc_df, 3.0)

    print(f"\n  BTC base signals (zone≤3%): {len(btc_base_sigs)} signals\n")

    alt_summary_rows = []
    for symbol in ALT_SYMBOLS:
        coin = symbol.split("/")[0]
        print(f"  Fetching {coin}...", end=" ", flush=True)
        try:
            alt_df = fetch_candles(exchange, symbol, LOOKBACK_DAYS)
            alt_df["mfi"] = calc_mfi(alt_df, MFI_LENGTH)
            alt_df["atr"] = calc_atr(alt_df, ATR_LENGTH)

            alt_sigs = find_alt_signals(btc_base_sigs, alt_df)
            print(f"{len(alt_sigs)} signals matched")

            records = []
            for sig in alt_sigs:
                r = simulate_trade(alt_df, sig)
                r.update(sig)
                records.append(r)

            s = summarise(records, coin)
            if s:
                alt_summary_rows.append(s)
                print_summary(s)

        except Exception as e:
            print(f"  ERROR: {e}")
        print()
        time.sleep(0.5)

    # ── 4. Final Comparison Table ─────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("  FINAL COMPARISON — BTC Zone-Filtered vs Alt Following")
    print("=" * 72)
    print(f"  {'Label':<22} {'N':>5} {'SPM':>5} {'WR':>7} {'WR-L':>7} {'WR-S':>7} {'PF':>6} {'Net%':>8} {'TP1%':>6} {'TP2%':>6}")
    print(f"  {'-'*22}-+-{'-'*5}-+-{'-'*5}-+-{'-'*7}-+-{'-'*7}-+-{'-'*7}-+-{'-'*6}-+-{'-'*8}-+-{'-'*6}-+-{'-'*6}")

    for s in all_btc_rows:
        pf_str = f"{s['pf']:.2f}" if s['pf'] != float('inf') else " inf"
        print(f"  {s['label']:<22} {s['n']:>5} {s['spm']:>5} {s['wr']:>6.1f}% {s['wr_long']:>6.1f}% {s['wr_short']:>6.1f}% {pf_str:>6} {s['net']:>+8.1f}% {s['tp1_rate']:>5.1f}% {s['tp2_rate']:>5.1f}%")

    print()
    for s in alt_summary_rows:
        pf_str = f"{s['pf']:.2f}" if s['pf'] != float('inf') else " inf"
        print(f"  {'Alt→'+s['label']:<22} {s['n']:>5} {s['spm']:>5} {s['wr']:>6.1f}% {s['wr_long']:>6.1f}% {s['wr_short']:>6.1f}% {pf_str:>6} {s['net']:>+8.1f}% {s['tp1_rate']:>5.1f}% {s['tp2_rate']:>5.1f}%")

    # ── 5. Key Findings ───────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("  KEY FINDINGS")
    print("=" * 72)

    if all_btc_rows:
        best_btc = max(all_btc_rows, key=lambda x: x['net'])
        print(f"\n  BTC Best Config : {best_btc['label']}")
        print(f"    WR {best_btc['wr']}% | PF {best_btc['pf']} | Net {best_btc['net']:+.1f}% | {best_btc['n']} trades | {best_btc['spm']}/month")

    if alt_summary_rows:
        best_alt = max(alt_summary_rows, key=lambda x: x['net'])
        print(f"\n  Best Alt Follower : {best_alt['label']}")
        print(f"    WR {best_alt['wr']}% | PF {best_alt['pf']} | Net {best_alt['net']:+.1f}% | {best_alt['n']} trades | {best_alt['spm']}/month")

    # Save
    out_btc = "/Users/macbook/Downloads/Content Projects/02 - Trading Coin Hunter/Trade Logs/backtest_liq_zone_v3_btc.csv"
    out_alt = "/Users/macbook/Downloads/Content Projects/02 - Trading Coin Hunter/Trade Logs/backtest_liq_zone_v3_alts.csv"
    if all_btc_rows:
        pd.DataFrame(all_btc_rows).to_csv(out_btc, index=False)
    if alt_summary_rows:
        pd.DataFrame(alt_summary_rows).to_csv(out_alt, index=False)
    print(f"\n  Saved → {out_btc}")
    print(f"  Saved → {out_alt}")
    print("=" * 72)


if __name__ == "__main__":
    main()
