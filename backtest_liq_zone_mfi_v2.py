#!/usr/bin/env python3
"""
Liquidation Zone MFI Reversal — Backtest v2
────────────────────────────────────────────
Purpose: Map exactly how far price moves after MFI reversal signal.
         Find optimal fixed-% TP levels that approximate liquidation zones.
         Also test: entry refinement (require 2 confirmation candles).

Key questions:
  1. What % of MFI reversals reach 2%, 3%, 4%, 5%, 6%, 7%, 8%, 10%?
  2. Does requiring a stronger entry confirmation improve WR?
  3. Does BTC SHORT work better than LONG in current bear market conditions?
  4. What is the realistic SL that keeps WR high?
"""

import ccxt
import pandas as pd
import numpy as np
import time
import warnings
from datetime import datetime, timedelta, timezone

warnings.filterwarnings("ignore")

# ─── CONFIG ──────────────────────────────────────────────────────────────────
COINS         = ["BTC/USDT:USDT", "ETH/USDT:USDT"]
TIMEFRAME     = "6h"
MFI_LENGTH    = 14
MFI_OB        = 80
MFI_OS        = 20
ATR_LENGTH    = 14
LOOKBACK_DAYS = 730
MAX_HOLD      = 42        # 10.5 days

# Fixed % TP levels to probe
TP_LEVELS_PCT = [2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 10.0]

# SL to test
SL_ATR_MULT   = 1.5       # fixed for this pass

# Entry modes
ENTRY_MODES = {
    "basic":    "MFI cross + 1 green/red candle",
    "confirm":  "MFI cross + 2nd confirmation candle (body > 0.3×ATR)",
}
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


def find_signals(df, mode="basic"):
    """
    basic:   MFI crossed zone + first reversal candle
    confirm: require the NEXT candle to also confirm (body > 0.3×ATR minimum size)
    """
    signals = []
    mfi    = df["mfi"].values
    atr    = df["atr"].values
    opens  = df["open"].values
    closes = df["close"].values

    for i in range(MFI_LENGTH + 10, len(df) - MAX_HOLD - 2):
        cur_mfi   = mfi[i]
        prev_mfis = mfi[i-5:i]
        cur_atr   = atr[i]

        if np.isnan(cur_mfi) or np.isnan(cur_atr) or cur_atr == 0:
            continue

        green = closes[i] > opens[i]
        red   = closes[i] < opens[i]
        body  = abs(closes[i] - opens[i])

        if mode == "confirm":
            # Need next candle to be same direction + meaningful body
            if i + 1 >= len(df):
                continue
            next_green = closes[i+1] > opens[i+1]
            next_red   = closes[i+1] < opens[i+1]
            next_body  = abs(closes[i+1] - opens[i+1])
            min_body   = cur_atr * 0.3
            entry_idx  = i + 1
            entry_price = closes[i+1]
        else:
            entry_idx   = i
            entry_price = closes[i]

        # LONG signal: was oversold, crossed back above MFI_OS
        if np.any(prev_mfis < MFI_OS) and cur_mfi > MFI_OS and green:
            if mode == "confirm":
                if not (next_green and next_body >= min_body):
                    continue
            signals.append({
                "idx":      entry_idx,
                "date":     df.index[entry_idx],
                "direction": "LONG",
                "entry":    entry_price,
                "atr":      cur_atr,
                "mfi":      round(cur_mfi, 1),
            })

        # SHORT signal: was overbought, crossed back below MFI_OB
        elif np.any(prev_mfis > MFI_OB) and cur_mfi < MFI_OB and red:
            if mode == "confirm":
                if not (next_red and next_body >= min_body):
                    continue
            signals.append({
                "idx":      entry_idx,
                "date":     df.index[entry_idx],
                "direction": "SHORT",
                "entry":    entry_price,
                "atr":      cur_atr,
                "mfi":      round(cur_mfi, 1),
            })

    return signals


def probe_signal(df, sig):
    """
    For each signal, record:
    - Max favourable excursion (peak move %)
    - Whether each fixed % TP level was hit before SL
    - SL (1.5×ATR)
    """
    i      = sig["idx"]
    entry  = sig["entry"]
    atr    = sig["atr"]
    direct = sig["direction"]
    sl_dist = atr * SL_ATR_MULT

    sl = entry - sl_dist if direct == "LONG" else entry + sl_dist

    highs  = df["high"].values
    lows   = df["low"].values

    max_fav = 0.0
    sl_hit  = False
    tp_hit  = {tp: False for tp in TP_LEVELS_PCT}

    for j in range(i + 1, min(i + 1 + MAX_HOLD, len(df))):
        h = highs[j]
        l = lows[j]

        if direct == "LONG":
            fav = (h - entry) / entry * 100
            max_fav = max(max_fav, fav)

            # SL check first
            if l <= sl:
                sl_hit = True
                break

            for tp_pct in TP_LEVELS_PCT:
                if not tp_hit[tp_pct]:
                    tp_price = entry * (1 + tp_pct / 100)
                    if h >= tp_price:
                        tp_hit[tp_pct] = True

        else:  # SHORT
            fav = (entry - l) / entry * 100
            max_fav = max(max_fav, fav)

            if h >= sl:
                sl_hit = True
                break

            for tp_pct in TP_LEVELS_PCT:
                if not tp_hit[tp_pct]:
                    tp_price = entry * (1 - tp_pct / 100)
                    if l <= tp_price:
                        tp_hit[tp_pct] = True

    return {
        "max_fav_pct": round(max_fav, 2),
        "sl_hit": sl_hit,
        **{f"tp_{tp}pct": hit for tp, hit in tp_hit.items()}
    }


def analyse(exchange, symbol, mode):
    coin = symbol.split("/")[0]
    df = fetch_candles(exchange, symbol, TIMEFRAME, LOOKBACK_DAYS)
    df["mfi"] = calc_mfi(df, MFI_LENGTH)
    df["atr"] = calc_atr(df, ATR_LENGTH)

    signals = find_signals(df, mode)
    if not signals:
        return None

    records = []
    for sig in signals:
        probe = probe_signal(df, sig)
        records.append({**sig, **probe})

    rdf = pd.DataFrame(records)
    longs  = rdf[rdf["direction"] == "LONG"]
    shorts = rdf[rdf["direction"] == "SHORT"]
    n      = len(rdf)

    sl_rate   = rdf["sl_hit"].sum() / n * 100
    avg_mfe   = rdf["max_fav_pct"].mean()
    sl_pct    = SL_ATR_MULT * rdf["atr"].mean() / rdf["entry"].mean() * 100

    rows = []
    for tp in TP_LEVELS_PCT:
        col = f"tp_{tp}pct"
        total_hit  = rdf[col].sum()
        # WR = hit TP before SL (we want both conditions: not sl_hit AND tp_hit)
        clean_wins = rdf[col & ~rdf["sl_hit"] if False else col].sum()
        # More precisely: among all signals, % that hit TP regardless of SL order
        # (we already stop on SL, so tp_hit=True means it hit BEFORE sl in the simulation)
        wr_all     = total_hit / n * 100
        l_wr = longs[col].sum() / len(longs) * 100 if len(longs) else 0
        s_wr = shorts[col].sum() / len(shorts) * 100 if len(shorts) else 0

        # Simulated PnL at this TP level (50% partial at TP, 50% held — simplified: full exit at TP)
        # Win: +tp_pct, Loss: -sl_pct (avg)
        sim_pnl = (total_hit * tp - (n - total_hit) * sl_pct) / n

        rows.append({
            "coin": coin,
            "mode": mode,
            "tp_pct": tp,
            "n_signals": n,
            "n_long": len(longs),
            "n_short": len(shorts),
            "wr_all": round(wr_all, 1),
            "wr_long": round(l_wr, 1),
            "wr_short": round(s_wr, 1),
            "sl_rate": round(sl_rate, 1),
            "avg_mfe": round(avg_mfe, 2),
            "avg_sl_pct": round(sl_pct, 2),
            "sim_pnl_per_trade": round(sim_pnl, 2),
            "signals_per_month": round(n / (LOOKBACK_DAYS / 30), 1),
        })

    return rows


def main():
    print("=" * 72)
    print("  Liquidation Zone MFI Reversal — v2: Target Zone Mapping")
    print(f"  Coins: BTC, ETH | 6H | {LOOKBACK_DAYS}d | SL={SL_ATR_MULT}×ATR")
    print("=" * 72)

    exchange = ccxt.binance({"options": {"defaultType": "future"}})

    all_rows = []

    for symbol in COINS:
        coin = symbol.split("/")[0]
        for mode in ENTRY_MODES:
            print(f"\n  Fetching {coin} [{mode}]...", flush=True)
            try:
                rows = analyse(exchange, symbol, mode)
                if rows:
                    all_rows.extend(rows)
            except Exception as e:
                print(f"  ERROR: {e}")
            time.sleep(0.5)

    if not all_rows:
        print("No results.")
        return

    df = pd.DataFrame(all_rows)

    for coin in ["BTC", "ETH"]:
        for mode in ENTRY_MODES:
            sub = df[(df["coin"] == coin) & (df["mode"] == mode)]
            if sub.empty:
                continue
            n  = sub.iloc[0]["n_signals"]
            nl = sub.iloc[0]["n_long"]
            ns = sub.iloc[0]["n_short"]
            sl = sub.iloc[0]["sl_rate"]
            mfe = sub.iloc[0]["avg_mfe"]
            avg_sl = sub.iloc[0]["avg_sl_pct"]
            spm = sub.iloc[0]["signals_per_month"]

            print(f"\n{'=' * 72}")
            print(f"  {coin} — {ENTRY_MODES[mode]}")
            print(f"  {n} signals ({spm}/month) | Long:{nl} Short:{ns} | SL hit:{sl:.0f}% | AvgMFE:{mfe:.1f}%  AvgSL:{avg_sl:.1f}%")
            print(f"{'=' * 72}")
            print(f"  {'TP Target':>10} | {'WR All':>7} | {'WR Long':>8} | {'WR Short':>9} | {'Sim PnL/tr':>11}")
            print(f"  {'-'*10}-+-{'-'*7}-+-{'-'*8}-+-{'-'*9}-+-{'-'*11}")
            for _, row in sub.iterrows():
                marker = " ◄" if row["sim_pnl_per_trade"] == sub["sim_pnl_per_trade"].max() else ""
                print(f"  {row['tp_pct']:>8.0f}%  | {row['wr_all']:>6.1f}% | {row['wr_long']:>7.1f}% | {row['wr_short']:>8.1f}% | {row['sim_pnl_per_trade']:>+10.2f}%{marker}")

    out = "/Users/macbook/Downloads/Content Projects/02 - Trading Coin Hunter/Trade Logs/backtest_liq_zone_v2.csv"
    df.to_csv(out, index=False)
    print(f"\n  Saved → {out}")
    print("=" * 72)


if __name__ == "__main__":
    main()
