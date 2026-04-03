#!/usr/bin/env python3
"""
MFI SHORT Strategy Backtest — Binance USDM Futures, 6H candles, 90 days
Strategy: 3 green candles + MFI>=80, then 1 red candle with MFI>=70 → Short
Entry: next candle open
SL: entry + ATR(14) * 1.5
TP_RR: entry - (SL - entry) * 2   (2:1 R:R)
TP_ATR: entry - ATR(14) * 3
"""

import ccxt
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
import time
import warnings
warnings.filterwarnings("ignore")

# ─── CONFIG ──────────────────────────────────────────────────────────────────
CANDLE_TF       = "6h"
LOOKBACK_DAYS   = 90
MFI_PERIOD      = 14
ATR_PERIOD      = 14
SL_ATR_MULT     = 1.5
TP_ATR_MULT     = 4.0          # changed: ATR×4 (was ATR×3 = same as RR)
TP_RR_RATIO     = 2.0
MIN_VOLUME_USD  = 50_000_000  # $50M daily volume filter (was $5M)
TOP_N_COINS     = 30
COOLDOWN_HOURS  = 24
# ─────────────────────────────────────────────────────────────────────────────


def fetch_ohlcv(exchange, symbol, tf, since_ms, limit=1000):
    """Fetch all OHLCV candles from since_ms to now, paginating as needed."""
    all_candles = []
    while True:
        try:
            candles = exchange.fetch_ohlcv(symbol, tf, since=since_ms, limit=limit)
        except Exception as e:
            print(f"  [WARN] fetch_ohlcv failed for {symbol}: {e}")
            break
        if not candles:
            break
        all_candles.extend(candles)
        if len(candles) < limit:
            break
        since_ms = candles[-1][0] + 1
        time.sleep(0.05)
    return all_candles


def calc_mfi(df, period=14):
    """Money Flow Index."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    mf = tp * df["volume"]
    up   = mf.where(tp > tp.shift(1), 0.0)
    down = mf.where(tp < tp.shift(1), 0.0)
    up_sum   = up.rolling(period).sum()
    down_sum = down.rolling(period).sum()
    mfr = np.where(down_sum == 0, 100.0, up_sum / down_sum)
    return 100 - (100 / (1 + mfr))


def calc_atr(df, period=14):
    """Average True Range."""
    h, l, pc = df["high"], df["low"], df["close"].shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(span=period, min_periods=period, adjust=False).mean()


def get_top_symbols(exchange, top_n=30, min_vol=5_000_000, verbose=True):
    """Return top-N symbols by 24h quote volume, filtered by min_vol."""
    if verbose:
        print("Fetching tickers to select top coins by volume …")
    try:
        tickers = exchange.fetch_tickers()
    except Exception as e:
        print(f"  [ERROR] fetch_tickers: {e}")
        return []

    rows = []
    for sym, t in tickers.items():
        if not sym.endswith("/USDT:USDT"):
            continue
        vol = t.get("quoteVolume") or 0
        if vol >= min_vol:
            rows.append({"symbol": sym, "quoteVolume": vol})

    df_t = pd.DataFrame(rows).sort_values("quoteVolume", ascending=False)
    chosen_df = df_t.head(top_n)
    chosen = chosen_df["symbol"].tolist()

    if verbose:
        print(f"\n  Coins selected (top {len(chosen)} by volume, ≥${min_vol/1e6:.0f}M/day):")
        print(f"  {'#':<4} {'Coin':<12} {'24h Vol (USD)':>18}")
        print(f"  {'-'*4} {'-'*12} {'-'*18}")
        for rank, (_, row) in enumerate(chosen_df.iterrows(), 1):
            coin = row["symbol"].replace("/USDT:USDT", "")
            print(f"  {rank:<4} {coin:<12} ${row['quoteVolume']:>17,.0f}")
        print()

    return chosen


def run_backtest(exchange, symbols, since_ms):
    trades = []

    for i, symbol in enumerate(symbols, 1):
        print(f"  [{i:02d}/{len(symbols)}] {symbol} …", end=" ", flush=True)
        raw = fetch_ohlcv(exchange, symbol, CANDLE_TF, since_ms)
        if len(raw) < 30:
            print("not enough data, skip")
            continue

        df = pd.DataFrame(raw, columns=["ts","open","high","low","close","volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df = df.set_index("ts").sort_index()
        df = df.astype(float)

        df["mfi"]  = calc_mfi(df, MFI_PERIOD)
        df["atr"]  = calc_atr(df, ATR_PERIOD)
        df["is_green"] = (df["close"] > df["open"]).astype(int)
        df["is_red"]   = (df["close"] < df["open"]).astype(int)

        # We need at least 5 candles for the signal window + 1 entry candle
        min_idx = 5
        df = df.dropna(subset=["mfi", "atr"])
        arr_open   = df["open"].values
        arr_high   = df["high"].values
        arr_low    = df["low"].values
        arr_close  = df["close"].values
        arr_mfi    = df["mfi"].values
        arr_atr    = df["atr"].values
        arr_green  = df["is_green"].values
        arr_red    = df["is_red"].values
        arr_ts     = df.index

        last_trade_ts = None   # cooldown tracker (datetime)
        open_trade    = False  # one open trade at a time per coin
        coin_trades   = []

        for idx in range(min_idx, len(df) - 1):
            # Signal candles
            # iloc[-5] = idx-4, iloc[-4] = idx-3, iloc[-3] = idx-2  → 3-green window
            # iloc[-2] = idx-1  → first red candle
            # iloc[-1] = idx    → entry candle (we use its open)

            # 3 candles before the red candle (the "green window")
            g1 = arr_green[idx - 4]
            g2 = arr_green[idx - 3]
            g3 = arr_green[idx - 2]
            # first red candle
            red = arr_red[idx - 1]
            # MFI checks
            mfi_green_window = arr_mfi[idx - 4: idx - 1]  # slices [idx-4, idx-3, idx-2]
            mfi_red_candle   = arr_mfi[idx - 1]

            if not (g1 and g2 and g3):
                continue
            if not red:
                continue
            if not (mfi_green_window.max() >= 80):
                continue
            if not (mfi_red_candle >= 70):
                continue

            # Cooldown check
            signal_ts = arr_ts[idx]
            if last_trade_ts is not None:
                elapsed = (signal_ts - last_trade_ts).total_seconds() / 3600
                if elapsed < COOLDOWN_HOURS:
                    continue

            # Skip if already in an open trade for this coin
            if open_trade:
                continue

            # Entry
            entry_price = arr_open[idx]
            if entry_price <= 0:
                continue
            atr_val     = arr_atr[idx - 1]  # ATR at time of signal (last closed candle)
            sl          = entry_price + atr_val * SL_ATR_MULT
            tp_rr       = entry_price - (sl - entry_price) * TP_RR_RATIO
            tp_atr      = entry_price - atr_val * TP_ATR_MULT

            # Simulate exit on subsequent candles
            result_rr  = None
            result_atr = None
            exit_candle_rr  = None
            exit_candle_atr = None

            for j in range(idx + 1, len(df)):
                h = arr_high[j]
                l = arr_low[j]

                # SL hit
                sl_hit = h >= sl
                # TP_RR hit (price went DOWN to tp_rr, use low)
                tp_rr_hit  = l <= tp_rr
                # TP_ATR hit
                tp_atr_hit = l <= tp_atr

                # For TP_RR track
                if result_rr is None:
                    if sl_hit and tp_rr_hit:
                        # ambiguous — use open to decide
                        o = arr_open[j]
                        if o <= tp_rr:
                            result_rr = "win"
                        else:
                            result_rr = "loss"
                        exit_candle_rr = j
                    elif sl_hit:
                        result_rr = "loss"
                        exit_candle_rr = j
                    elif tp_rr_hit:
                        result_rr = "win"
                        exit_candle_rr = j

                # For TP_ATR track
                if result_atr is None:
                    if sl_hit and tp_atr_hit:
                        o = arr_open[j]
                        if o <= tp_atr:
                            result_atr = "win"
                        else:
                            result_atr = "loss"
                        exit_candle_atr = j
                    elif sl_hit:
                        result_atr = "loss"
                        exit_candle_atr = j
                    elif tp_atr_hit:
                        result_atr = "win"
                        exit_candle_atr = j

                if result_rr is not None and result_atr is not None:
                    break

            # If never exited (open at end of data) → skip trade
            if result_rr is None or result_atr is None:
                continue

            # PnL % (short: profit when price goes down)
            sl_pct   = (sl - entry_price) / entry_price * 100      # loss size %
            tp_rr_pct  = (entry_price - tp_rr) / entry_price * 100  # win size %
            tp_atr_pct = (entry_price - tp_atr) / entry_price * 100

            pnl_rr  = tp_rr_pct  if result_rr  == "win" else -sl_pct
            pnl_atr = tp_atr_pct if result_atr == "win" else -sl_pct

            coin_trades.append({
                "symbol":      symbol,
                "entry_ts":    signal_ts,
                "entry_price": entry_price,
                "sl":          sl,
                "tp_rr":       tp_rr,
                "tp_atr":      tp_atr,
                "atr":         atr_val,
                "result_rr":   result_rr,
                "result_atr":  result_atr,
                "pnl_rr":      pnl_rr,
                "pnl_atr":     pnl_atr,
                "sl_pct":      sl_pct,
                "tp_rr_pct":   tp_rr_pct,
                "tp_atr_pct":  tp_atr_pct,
            })

            last_trade_ts = signal_ts
            open_trade = False  # reset after exit simulated

        trades.extend(coin_trades)
        print(f"{len(coin_trades)} trades")

    return pd.DataFrame(trades)


def print_stats(df, label):
    if df.empty:
        print(f"\n{label}: No trades.")
        return
    col_result = "result_rr" if "rr" in label.lower() else "result_atr"
    col_pnl    = "pnl_rr"   if "rr" in label.lower() else "pnl_atr"

    wins   = df[df[col_result] == "win"]
    losses = df[df[col_result] == "loss"]
    n      = len(df)
    wr     = len(wins) / n * 100
    avg_win  = wins[col_pnl].mean()   if len(wins)   > 0 else 0
    avg_loss = losses[col_pnl].mean() if len(losses) > 0 else 0
    gross_win  = wins[col_pnl].sum()   if len(wins)   > 0 else 0
    gross_loss = losses[col_pnl].abs().sum() if len(losses) > 0 else 0
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    total_pnl = df[col_pnl].sum()

    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    print(f"  Total trades : {n}")
    print(f"  Wins / Losses: {len(wins)} / {len(losses)}")
    print(f"  Win Rate     : {wr:.1f}%")
    print(f"  Avg Win      : +{avg_win:.2f}%")
    print(f"  Avg Loss     : {avg_loss:.2f}%")
    print(f"  Profit Factor: {pf:.2f}")
    print(f"  Total PnL    : {total_pnl:+.2f}%  (sum of all trade %, equal-size)")
    print(f"{'='*60}")


def print_coin_breakdown(df):
    if df.empty:
        return
    print("\n--- Per-Coin Breakdown (TP_RR method) ---")
    grouped = df.groupby("symbol").agg(
        trades   = ("pnl_rr", "count"),
        wins_rr  = ("result_rr", lambda x: (x == "win").sum()),
        pnl_rr   = ("pnl_rr", "sum"),
        pnl_atr  = ("pnl_atr", "sum"),
    ).reset_index()
    grouped["wr_rr"] = grouped["wins_rr"] / grouped["trades"] * 100
    grouped = grouped.sort_values("pnl_rr", ascending=False)

    header = f"{'Symbol':<20} {'Trades':>7} {'WR%':>7} {'PnL_RR%':>10} {'PnL_ATR%':>11}"
    print(header)
    print("-" * len(header))
    for _, r in grouped.iterrows():
        sym = r["symbol"].replace("/USDT:USDT", "")
        print(f"  {sym:<18} {int(r['trades']):>7} {r['wr_rr']:>6.1f}% "
              f"{r['pnl_rr']:>+9.2f}%  {r['pnl_atr']:>+9.2f}%")

    print("\n--- Top 10 Best Coins (by TP_RR PnL) ---")
    for _, r in grouped.head(10).iterrows():
        sym = r["symbol"].replace("/USDT:USDT", "")
        print(f"  {sym:<18}  PnL_RR: {r['pnl_rr']:>+8.2f}%   PnL_ATR: {r['pnl_atr']:>+8.2f}%")

    print("\n--- Top 10 Worst Coins (by TP_RR PnL) ---")
    for _, r in grouped.tail(10).iterrows():
        sym = r["symbol"].replace("/USDT:USDT", "")
        print(f"  {sym:<18}  PnL_RR: {r['pnl_rr']:>+8.2f}%   PnL_ATR: {r['pnl_atr']:>+8.2f}%")


def main():
    print("=" * 70)
    print("  MFI SHORT Strategy Backtest — Binance USDM Futures 6H")
    print(f"  Backtest period : last {LOOKBACK_DAYS} days")
    print(f"  Universe        : top {TOP_N_COINS} coins by volume (≥${MIN_VOLUME_USD/1e6:.0f}M/day)")
    print(f"  MFI period      : {MFI_PERIOD}  |  ATR period: {ATR_PERIOD}")
    print(f"  SL multiplier   : ATR × {SL_ATR_MULT}")
    print(f"  TP_RR           : 2:1 R:R  |  TP_ATR: ATR × {TP_ATR_MULT}")
    print("=" * 70)

    exchange = ccxt.binanceusdm({
        "enableRateLimit": True,
        "options": {"defaultType": "future"},
    })
    exchange.load_markets()

    symbols = get_top_symbols(exchange, TOP_N_COINS, MIN_VOLUME_USD)
    if not symbols:
        print("No symbols found — exiting.")
        return

    since_ms = int(
        (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).timestamp() * 1000
    )

    print(f"\nRunning backtest on {len(symbols)} symbols …\n")
    t0 = time.time()
    df = run_backtest(exchange, symbols, since_ms)
    elapsed = time.time() - t0
    print(f"\nBacktest completed in {elapsed:.1f}s — {len(df)} total trades found.\n")

    if df.empty:
        print("No trades generated. Check data / signal parameters.")
        return

    # ── Overall stats ──────────────────────────────────────────────────────
    print_stats(df, "TP_RR Method (2:1 Risk:Reward)")
    print_stats(df, f"TP_ATR Method (ATR × {TP_ATR_MULT})")

    # ── Comparison ────────────────────────────────────────────────────────
    print("\n--- TP_RR vs TP_ATR Comparison ---")
    rr_pnl  = df["pnl_rr"].sum()
    atr_pnl = df["pnl_atr"].sum()
    rr_wr   = (df["result_rr"]  == "win").mean() * 100
    atr_wr  = (df["result_atr"] == "win").mean() * 100
    print(f"  TP_RR  — Total PnL: {rr_pnl:>+8.2f}%   Win Rate: {rr_wr:.1f}%")
    print(f"  TP_ATR — Total PnL: {atr_pnl:>+8.2f}%   Win Rate: {atr_wr:.1f}%")
    winner = "TP_RR" if rr_pnl >= atr_pnl else "TP_ATR"
    print(f"  >> {winner} performs better overall by total PnL.")

    # ── Per-coin breakdown ─────────────────────────────────────────────────
    print_coin_breakdown(df)

    # ── Focused sub-backtest: top-5 coins by win rate (TP_RR, ≥2 trades) ──
    grouped = df.groupby("symbol").agg(
        trades  = ("pnl_rr", "count"),
        wins_rr = ("result_rr", lambda x: (x == "win").sum()),
    ).reset_index()
    grouped["wr_rr"] = grouped["wins_rr"] / grouped["trades"] * 100
    eligible = grouped[grouped["trades"] >= 2].sort_values("wr_rr", ascending=False)
    top5_syms = eligible.head(5)["symbol"].tolist()

    if top5_syms:
        print("\n" + "=" * 70)
        print("  FOCUSED SUB-BACKTEST — Top 5 Coins by Win Rate (TP_RR, ≥2 trades)")
        print("  Coins: " + ", ".join(s.replace("/USDT:USDT", "") for s in top5_syms))
        print("=" * 70)
        df_top5 = df[df["symbol"].isin(top5_syms)]
        print_stats(df_top5, "TP_RR Method — Top-5 Coins")
        print_stats(df_top5, f"TP_ATR Method — Top-5 Coins (ATR × {TP_ATR_MULT})")

        print("\n--- Top-5 Per-Coin Detail ---")
        sub = df_top5.groupby("symbol").agg(
            trades   = ("pnl_rr", "count"),
            wins_rr  = ("result_rr", lambda x: (x == "win").sum()),
            pnl_rr   = ("pnl_rr", "sum"),
            wins_atr = ("result_atr", lambda x: (x == "win").sum()),
            pnl_atr  = ("pnl_atr", "sum"),
        ).reset_index()
        sub["wr_rr"]  = sub["wins_rr"]  / sub["trades"] * 100
        sub["wr_atr"] = sub["wins_atr"] / sub["trades"] * 100
        sub = sub.sort_values("wr_rr", ascending=False)

        header2 = f"{'Coin':<14} {'Trades':>7} {'WR_RR%':>8} {'PnL_RR%':>10} {'WR_ATR%':>9} {'PnL_ATR%':>11}"
        print(header2)
        print("-" * len(header2))
        for _, r in sub.iterrows():
            coin = r["symbol"].replace("/USDT:USDT", "")
            print(f"  {coin:<12} {int(r['trades']):>7} {r['wr_rr']:>7.1f}% "
                  f"{r['pnl_rr']:>+9.2f}%  {r['wr_atr']:>7.1f}%  {r['pnl_atr']:>+9.2f}%")

    # ── Sample trades ─────────────────────────────────────────────────────
    print("\n--- Sample of last 10 trades ---")
    cols = ["symbol","entry_ts","entry_price","sl","tp_rr","tp_atr",
            "result_rr","result_atr","pnl_rr","pnl_atr"]
    sample = df.tail(10)[cols].copy()
    sample["symbol"] = sample["symbol"].str.replace("/USDT:USDT", "", regex=False)
    sample["entry_ts"] = sample["entry_ts"].dt.strftime("%Y-%m-%d %H:%M")
    sample["pnl_rr"]  = sample["pnl_rr"].map("{:+.2f}%".format)
    sample["pnl_atr"] = sample["pnl_atr"].map("{:+.2f}%".format)
    print(sample.to_string(index=False))

    print("\nDone.")


if __name__ == "__main__":
    main()
