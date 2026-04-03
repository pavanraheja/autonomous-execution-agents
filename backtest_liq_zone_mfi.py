#!/usr/bin/env python3
"""
Liquidation Zone MFI Reversal — Backtest
─────────────────────────────────────────
Strategy:
  LONG  : MFI < 20 in last 5 candles → MFI crosses back above 20 → green candle → entry
  SHORT : MFI > 80 in last 5 candles → MFI crosses back below 80 → red candle → entry

Liquidation zone proxy (ChartPrime approximation):
  TP targets are set at swing high/low levels computed from rolling window,
  scaled by ATR to find realistic zone distances.

Tests:
  - SL at 1.0×, 1.5×, 2.0×, 2.5× ATR
  - TP1 (partial 50%) at 1.5× RR vs 2.0× RR vs swing midpoint
  - TP2 (full exit) at swing high/low
  - Max hold: 42 candles (10.5 days)

Coins: BTC, ETH — 6H candles — 2 years history
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
COINS          = ["BTC/USDT:USDT", "ETH/USDT:USDT"]
TIMEFRAME      = "6h"
MFI_LENGTH     = 14
MFI_OB         = 80        # overbought threshold (short signal)
MFI_OS         = 20        # oversold threshold (long signal)
ATR_LENGTH     = 14
LOOKBACK_DAYS  = 730       # 2 years
MAX_HOLD       = 42        # candles before timeout exit
SWING_WINDOW   = 20        # candles to look back for swing high/low

# SL/TP grid to test
SL_ATR_MULTS   = [1.0, 1.5, 2.0, 2.5]
PARTIAL_RR     = [1.5, 2.0]   # TP1 as RR ratio (e.g. SL × 1.5 = TP1)
PARTIAL_PCT    = 0.5           # close 50% at TP1
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


def fetch_candles(exchange, symbol, timeframe, days):
    since = exchange.parse8601(
        (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")
    )
    all_candles = []
    while True:
        batch = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=1000)
        if not batch:
            break
        all_candles += batch
        if len(batch) < 1000:
            break
        since = batch[-1][0] + 1
        time.sleep(0.2)
    df = pd.DataFrame(all_candles, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df.set_index("ts", inplace=True)
    df.drop_duplicates(inplace=True)
    return df


def find_signals(df):
    """
    Find LONG and SHORT reversal signals.

    LONG:  MFI was below MFI_OS in last 5 candles (inclusive of current-1)
           AND current MFI > MFI_OS (crossed back up)
           AND current candle is green (close > open)

    SHORT: MFI was above MFI_OB in last 5 candles (inclusive of current-1)
           AND current MFI < MFI_OB (crossed back down)
           AND current candle is red (close < open)
    """
    signals = []
    mfi = df["mfi"].values
    atr = df["atr"].values
    opens  = df["open"].values
    closes = df["close"].values
    highs  = df["high"].values
    lows   = df["low"].values

    for i in range(SWING_WINDOW + MFI_LENGTH + 5, len(df) - MAX_HOLD - 1):
        cur_mfi   = mfi[i]
        prev_mfis = mfi[i-5:i]   # last 5 candles before current
        cur_green = closes[i] > opens[i]
        cur_red   = closes[i] < opens[i]
        cur_atr   = atr[i]
        entry     = closes[i]

        if np.isnan(cur_mfi) or np.isnan(cur_atr) or cur_atr == 0:
            continue

        # Swing high/low: look back SWING_WINDOW candles for the most recent significant level
        swing_high = highs[i - SWING_WINDOW:i].max()
        swing_low  = lows[i  - SWING_WINDOW:i].min()

        # LONG signal
        if (np.any(prev_mfis < MFI_OS) and cur_mfi > MFI_OS and cur_green):
            # TP2 = swing high above entry (must be higher than entry)
            tp2 = swing_high if swing_high > entry else entry * 1.05
            mid = entry + (tp2 - entry) * 0.5   # midpoint for partial

            signals.append({
                "idx": i,
                "date": df.index[i],
                "direction": "LONG",
                "entry": entry,
                "atr": cur_atr,
                "mfi_at_signal": round(cur_mfi, 2),
                "tp2": tp2,
                "tp2_pct": (tp2 - entry) / entry * 100,
                "mid": mid,
            })

        # SHORT signal
        elif (np.any(prev_mfis > MFI_OB) and cur_mfi < MFI_OB and cur_red):
            tp2 = swing_low if swing_low < entry else entry * 0.95
            mid = entry - (entry - tp2) * 0.5

            signals.append({
                "idx": i,
                "date": df.index[i],
                "direction": "SHORT",
                "entry": entry,
                "atr": cur_atr,
                "mfi_at_signal": round(cur_mfi, 2),
                "tp2": tp2,
                "tp2_pct": (tp2 - entry) / entry * -100,
                "mid": mid,
            })

    return signals


def simulate_trade(df, sig, sl_mult, partial_rr):
    """
    Simulate one trade forward from signal candle.
    Returns dict with outcome metrics.
    """
    i      = sig["idx"]
    entry  = sig["entry"]
    atr    = sig["atr"]
    direct = sig["direction"]
    tp2    = sig["tp2"]
    mid    = sig["mid"]

    sl_dist = atr * sl_mult
    tp1_rr  = sl_dist * partial_rr   # TP1 distance from entry

    if direct == "LONG":
        sl   = entry - sl_dist
        tp1  = entry + tp1_rr
    else:
        sl   = entry + sl_dist
        tp1  = entry - tp1_rr

    partial_closed = False
    partial_pnl    = 0.0
    final_pnl      = 0.0
    outcome        = "TIMEOUT"
    hold_candles   = 0
    tp1_hit        = False
    tp2_hit        = False
    max_fav        = 0.0   # max favourable excursion %

    highs  = df["high"].values
    lows   = df["low"].values
    closes = df["close"].values

    for j in range(i + 1, min(i + 1 + MAX_HOLD, len(df))):
        h = highs[j]
        l = lows[j]
        c = closes[j]
        hold_candles = j - i

        if direct == "LONG":
            fav = (h - entry) / entry * 100
            max_fav = max(max_fav, fav)

            # Check SL first (worst case within candle)
            if l <= sl:
                if partial_closed:
                    final_pnl = partial_pnl + (sl - entry) / entry * 100 * (1 - PARTIAL_PCT)
                else:
                    final_pnl = (sl - entry) / entry * 100
                outcome = "BE+" if final_pnl > 0 else "LOSS"
                break

            # Check TP1 (partial)
            if not partial_closed and h >= tp1:
                partial_pnl   = (tp1 - entry) / entry * 100 * PARTIAL_PCT
                partial_closed = True
                tp1_hit        = True

            # Check TP2 (full exit)
            if h >= tp2:
                remaining_pnl = (tp2 - entry) / entry * 100 * (1 - PARTIAL_PCT if partial_closed else 1.0)
                final_pnl     = partial_pnl + remaining_pnl
                outcome        = "WIN"
                tp2_hit        = True
                break

        else:  # SHORT
            fav = (entry - l) / entry * 100
            max_fav = max(max_fav, fav)

            if h >= sl:
                if partial_closed:
                    final_pnl = partial_pnl + (entry - sl) / entry * 100 * (1 - PARTIAL_PCT)
                else:
                    final_pnl = (entry - sl) / entry * 100
                outcome = "BE+" if final_pnl > 0 else "LOSS"
                break

            if not partial_closed and l <= tp1:
                partial_pnl    = (entry - tp1) / entry * 100 * PARTIAL_PCT
                partial_closed = True
                tp1_hit        = True

            if l <= tp2:
                remaining_pnl = (entry - tp2) / entry * 100 * (1 - PARTIAL_PCT if partial_closed else 1.0)
                final_pnl     = partial_pnl + remaining_pnl
                outcome        = "WIN"
                tp2_hit        = True
                break
    else:
        # Timeout — close at last close
        last = closes[min(i + MAX_HOLD, len(df) - 1)]
        if direct == "LONG":
            remaining_pnl = (last - entry) / entry * 100 * (1 - PARTIAL_PCT if partial_closed else 1.0)
        else:
            remaining_pnl = (entry - last) / entry * 100 * (1 - PARTIAL_PCT if partial_closed else 1.0)
        final_pnl = partial_pnl + remaining_pnl
        outcome   = "TIMEOUT"

    return {
        "outcome":       outcome,
        "final_pnl":     round(final_pnl, 3),
        "tp1_hit":       tp1_hit,
        "tp2_hit":       tp2_hit,
        "hold_candles":  hold_candles,
        "max_fav_pct":   round(max_fav, 2),
        "sl_pct":        round(sl_dist / entry * 100, 2),
    }


def run_backtest(exchange, symbol, sl_mult, partial_rr):
    coin = symbol.split("/")[0]
    print(f"\n  [{coin}] SL={sl_mult}×ATR  TP1={partial_rr}×RR", end=" ... ", flush=True)

    df = fetch_candles(exchange, symbol, TIMEFRAME, LOOKBACK_DAYS)
    df["mfi"] = calc_mfi(df, MFI_LENGTH)
    df["atr"] = calc_atr(df, ATR_LENGTH)

    signals = find_signals(df)
    if not signals:
        print("no signals")
        return None

    results = []
    for sig in signals:
        r = simulate_trade(df, sig, sl_mult, partial_rr)
        r.update(sig)
        results.append(r)

    rdf = pd.DataFrame(results)

    wins     = rdf[rdf["outcome"] == "WIN"]
    losses   = rdf[rdf["outcome"] == "LOSS"]
    timeouts = rdf[rdf["outcome"] == "TIMEOUT"]
    be_plus  = rdf[rdf["outcome"] == "BE+"]

    n        = len(rdf)
    wr       = len(wins) / n * 100
    avg_win  = wins["final_pnl"].mean() if len(wins) else 0
    avg_loss = losses["final_pnl"].mean() if len(losses) else 0
    net      = rdf["final_pnl"].sum()
    pf       = (wins["final_pnl"].sum() / abs(losses["final_pnl"].sum())
                if len(losses) and losses["final_pnl"].sum() != 0 else float("inf"))
    tp1_rate = rdf["tp1_hit"].sum() / n * 100
    tp2_rate = rdf["tp2_hit"].sum() / n * 100
    avg_hold = rdf["hold_candles"].mean()
    avg_mfe  = rdf["max_fav_pct"].mean()
    avg_sl   = rdf["sl_pct"].mean()

    longs  = rdf[rdf["direction"] == "LONG"]
    shorts = rdf[rdf["direction"] == "SHORT"]
    l_wr   = (longs["outcome"] == "WIN").sum() / len(longs) * 100 if len(longs) else 0
    s_wr   = (shorts["outcome"] == "WIN").sum() / len(shorts) * 100 if len(shorts) else 0

    print(f"n={n}  WR={wr:.0f}%  PF={pf:.2f}  net={net:.1f}%  TP1={tp1_rate:.0f}%  TP2={tp2_rate:.0f}%")

    return {
        "coin": coin,
        "sl_mult": sl_mult,
        "partial_rr": partial_rr,
        "n": n,
        "n_long": len(longs),
        "n_short": len(shorts),
        "wr_pct": round(wr, 1),
        "wr_long": round(l_wr, 1),
        "wr_short": round(s_wr, 1),
        "pf": round(pf, 2),
        "net_pct": round(net, 1),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "tp1_rate": round(tp1_rate, 1),
        "tp2_rate": round(tp2_rate, 1),
        "n_wins": len(wins),
        "n_losses": len(losses),
        "n_timeout": len(timeouts),
        "n_be_plus": len(be_plus),
        "avg_hold_candles": round(avg_hold, 1),
        "avg_mfe_pct": round(avg_mfe, 2),
        "avg_sl_pct": round(avg_sl, 2),
        "signals_per_month": round(n / (LOOKBACK_DAYS / 30), 1),
    }


def main():
    print("=" * 70)
    print("  Liquidation Zone MFI Reversal — Backtest")
    print(f"  Coins: {[c.split('/')[0] for c in COINS]} | TF: {TIMEFRAME} | {LOOKBACK_DAYS}d history")
    print(f"  SL grid: {SL_ATR_MULTS}×ATR | Partial RR: {PARTIAL_RR} | TP1 size: {int(PARTIAL_PCT*100)}%")
    print("=" * 70)

    exchange = ccxt.binance({"options": {"defaultType": "future"}})

    all_results = []

    for symbol in COINS:
        for sl_mult, p_rr in product(SL_ATR_MULTS, PARTIAL_RR):
            try:
                r = run_backtest(exchange, symbol, sl_mult, p_rr)
                if r:
                    all_results.append(r)
            except Exception as e:
                print(f"  ERROR {symbol} sl={sl_mult}: {e}")
            time.sleep(0.3)

    if not all_results:
        print("No results.")
        return

    df = pd.DataFrame(all_results)

    print("\n" + "=" * 70)
    print("  FULL RESULTS GRID")
    print("=" * 70)
    cols = ["coin", "sl_mult", "partial_rr", "n", "wr_pct", "wr_long", "wr_short",
            "pf", "net_pct", "tp1_rate", "tp2_rate", "avg_win", "avg_loss",
            "avg_hold_candles", "avg_mfe_pct", "avg_sl_pct", "signals_per_month"]
    print(df[cols].to_string(index=False))

    print("\n" + "=" * 70)
    print("  TOP 5 CONFIGURATIONS BY NET PNL")
    print("=" * 70)
    top5 = df.nlargest(5, "net_pct")[cols]
    print(top5.to_string(index=False))

    print("\n" + "=" * 70)
    print("  TOP 5 CONFIGURATIONS BY PROFIT FACTOR")
    print("=" * 70)
    top5_pf = df[df["pf"] != float("inf")].nlargest(5, "pf")[cols]
    print(top5_pf.to_string(index=False))

    # Per-coin summary at best config
    print("\n" + "=" * 70)
    print("  PER-COIN BEST CONFIG SUMMARY")
    print("=" * 70)
    for coin in df["coin"].unique():
        best = df[df["coin"] == coin].nlargest(1, "net_pct").iloc[0]
        print(f"\n  {coin}:")
        print(f"    Best config : SL={best.sl_mult}×ATR, TP1={best.partial_rr}×RR")
        print(f"    Signals     : {best.n} total ({best.signals_per_month}/month)")
        print(f"    Long / Short: {best.n_long} / {best.n_short}")
        print(f"    Win Rate    : {best.wr_pct}%  (L:{best.wr_long}%  S:{best.wr_short}%)")
        print(f"    Profit Fact : {best.pf}")
        print(f"    Net PnL     : {best.net_pct}%")
        print(f"    Avg Win/Loss: +{best.avg_win}% / {best.avg_loss}%")
        print(f"    TP1/TP2 hit : {best.tp1_rate}% / {best.tp2_rate}%")
        print(f"    Avg hold    : {best.avg_hold_candles} candles ({best.avg_hold_candles * 6:.0f}h)")
        print(f"    Avg SL      : {best.avg_sl_pct}% from entry")
        print(f"    Avg max fav : {best.avg_mfe_pct}% peak move")

    # Save results CSV
    out_path = "/Users/macbook/Downloads/Content Projects/02 - Trading Coin Hunter/Trade Logs/backtest_liq_zone_mfi.csv"
    df.to_csv(out_path, index=False)
    print(f"\n  Results saved → {out_path}")
    print("=" * 70)


if __name__ == "__main__":
    main()
