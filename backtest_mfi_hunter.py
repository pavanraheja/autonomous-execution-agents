#!/usr/bin/env python3
"""
MFI Coin Hunter — Backtest (matches paper_trader.py v1.3 exactly)
─────────────────────────────────────────────────────────────────
Signal  : MFI ≥ 80 in 3-candle OB window → c1 quality red candle
Entry   : Close of first 2H sub-candle of c2 (if red)
SL      : entry × 1.03  (fixed 3%)
TP      : entry × (1 − 0.045)  (4.5%, 1.5×RR)
Exit sim: 1H candles forward, max 48H hold
Universe: Binance USDⓈ-M futures, ≥$5M/day volume, last 90 days
"""

import ccxt
import pandas as pd
import numpy as np
import time
import warnings
from datetime import datetime, timedelta, timezone

warnings.filterwarnings("ignore")

# ─── CONFIG ──────────────────────────────────────────────────────────────────
MFI_LENGTH         = 14
MFI_OVERBOUGHT     = 80
MAX_SL_PCT         = 0.03       # 3% fixed SL
RR_RATIO           = 1.5        # TP = 4.5%
MIN_VOLUME_USDT    = 5_000_000  # $5M daily volume filter
LOOKBACK_DAYS      = 180
MAX_HOLD_1H        = 48         # max 48 × 1H candles before timeout exit
COOLDOWN_HOURS     = 24         # same-coin cooldown after loss
# ─────────────────────────────────────────────────────────────────────────────


def calc_mfi(df, length=14):
    tp  = (df["high"] + df["low"] + df["close"]) / 3
    rmf = tp * df["volume"]
    pos = rmf.where(tp > tp.shift(1), 0.0)
    neg = rmf.where(tp < tp.shift(1), 0.0)
    ps  = pos.rolling(length).sum()
    ns  = neg.rolling(length).sum().replace(0, 1e-10)
    return 100 - (100 / (1 + ps / ns))


def fetch_df(exchange, symbol, tf, since_ms, limit=1000):
    all_c = []
    while True:
        try:
            c = exchange.fetch_ohlcv(symbol, tf, since=since_ms, limit=limit)
        except Exception as e:
            print(f"    [WARN] {symbol} {tf}: {e}")
            break
        if not c:
            break
        all_c.extend(c)
        if len(c) < limit:
            break
        since_ms = c[-1][0] + 1
        time.sleep(0.04)
    if not all_c:
        return pd.DataFrame()
    df = pd.DataFrame(all_c, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.set_index("ts").sort_index().astype(float)


def get_universe(exchange):
    print("Fetching universe …")
    tickers = exchange.fetch_tickers()
    rows = []
    for sym, t in tickers.items():
        if not sym.endswith("/USDT:USDT"):
            continue
        vol = t.get("quoteVolume") or 0
        if vol >= MIN_VOLUME_USDT:
            rows.append({"symbol": sym, "vol": vol})
    df = pd.DataFrame(rows).sort_values("vol", ascending=False)
    syms = df["symbol"].tolist()
    print(f"  {len(syms)} symbols pass ${MIN_VOLUME_USDT/1e6:.0f}M volume filter\n")
    return syms


def run_backtest(exchange, symbols, since_ms, label="Full Universe"):
    trades = []

    for i, symbol in enumerate(symbols, 1):
        coin = symbol.replace("/USDT:USDT", "")
        print(f"  [{i:03d}/{len(symbols)}] {coin:<14}", end=" ", flush=True)

        df6h = fetch_df(exchange, symbol, "6h",  since_ms - 30 * 86400_000)
        df2h = fetch_df(exchange, symbol, "2h",  since_ms - 10 * 86400_000)
        df1h = fetch_df(exchange, symbol, "1h",  since_ms - 5  * 86400_000)

        if df6h.empty or df2h.empty or df1h.empty or len(df6h) < 25:
            print("not enough data")
            continue

        df6h["mfi"]     = calc_mfi(df6h, MFI_LENGTH)
        df6h["is_red"]  = (df6h["close"] < df6h["open"])
        df6h["avg_vol"] = df6h["volume"].rolling(20, min_periods=5).mean()

        df2h_idx = df2h.index  # DatetimeIndex for fast lookup
        df1h_arr = df1h         # keep as df for slicing

        coin_trades     = []
        last_trade_ts   = None   # cooldown tracker

        arr_ts    = df6h.index.values
        arr_open  = df6h["open"].values
        arr_high  = df6h["high"].values
        arr_low   = df6h["low"].values
        arr_close = df6h["close"].values
        arr_mfi   = df6h["mfi"].values
        arr_avgv  = df6h["avg_vol"].values
        arr_vol   = df6h["volume"].values

        for idx in range(20, len(df6h) - 1):
            # OB window: [idx-4, idx-3, idx-2]  → 3 candles before c1
            mfi_window = arr_mfi[idx - 4: idx - 1]
            if mfi_window.max() < MFI_OVERBOUGHT:
                continue

            # peak OB candle index (for high/close comparison)
            ob_idx    = idx - 4 + int(np.argmax(mfi_window))
            ob_high   = arr_high[ob_idx]
            ob_close  = arr_close[ob_idx]
            ob_mfi    = arr_mfi[ob_idx]

            # c1 = idx - 1 (last fully closed 6H candle)
            c1_open   = arr_open[idx - 1]
            c1_close  = arr_close[idx - 1]
            c1_high   = arr_high[idx - 1]
            c1_low    = arr_low[idx - 1]
            c1_vol    = arr_vol[idx - 1]
            avg_vol   = arr_avgv[idx - 1]

            c1_range  = c1_high - c1_low
            if c1_range == 0:
                continue

            c1_body_ratio = (c1_open - c1_close) / c1_range
            c1_lower_wick = (min(c1_open, c1_close) - c1_low) / c1_range

            first_red = (
                c1_close < c1_open and
                c1_body_ratio >= 0.40 and
                c1_lower_wick <= 0.35 and
                c1_high  < ob_high  and
                c1_close < ob_close and
                (avg_vol == 0 or c1_vol >= avg_vol)
            )
            if not first_red:
                continue

            # c2 opens at arr_ts[idx] — find its first 2H sub-candle
            c2_open_ts = pd.Timestamp(arr_ts[idx], tz="UTC")

            # Tolerance: match 2H candle within ±1 min of c2 open
            tol = pd.Timedelta("1min")
            mask = (df2h_idx >= c2_open_ts - tol) & (df2h_idx <= c2_open_ts + tol)
            matches = df2h_idx[mask]
            if len(matches) == 0:
                continue
            sub = df2h.loc[matches[0]]

            # 2H sub-candle must be red
            if sub["close"] >= sub["open"]:
                continue

            entry_price = float(sub["close"])
            entry_ts    = matches[0]  # close of first 2H candle = entry time

            # Only trade candles within lookback
            if entry_ts < pd.Timestamp(since_ms, unit="ms", tz="UTC"):
                continue

            # Cooldown check
            if last_trade_ts is not None:
                elapsed = (entry_ts - last_trade_ts).total_seconds() / 3600
                if elapsed < COOLDOWN_HOURS:
                    continue

            sl = entry_price * (1 + MAX_SL_PCT)
            tp = entry_price * (1 - MAX_SL_PCT * RR_RATIO)

            # Simulate exit on 1H candles from entry onwards
            future_1h = df1h_arr[df1h_arr.index > entry_ts]
            result   = None
            exit_pnl = None

            for _, row in future_1h.iloc[:MAX_HOLD_1H].iterrows():
                sl_hit = row["high"] >= sl
                tp_hit = row["low"]  <= tp

                if sl_hit and tp_hit:
                    o = row["open"]
                    if o <= tp:
                        result = "win"; exit_pnl = MAX_SL_PCT * RR_RATIO * 100
                    else:
                        result = "loss"; exit_pnl = -MAX_SL_PCT * 100
                    break
                elif sl_hit:
                    result = "loss"; exit_pnl = -MAX_SL_PCT * 100
                    break
                elif tp_hit:
                    result = "win"; exit_pnl = MAX_SL_PCT * RR_RATIO * 100
                    break

            if result is None:
                # Timeout — exit at last available close
                last_close = future_1h.iloc[min(MAX_HOLD_1H - 1, len(future_1h) - 1)]["close"] if len(future_1h) > 0 else entry_price
                pct = (entry_price - last_close) / entry_price * 100
                result   = "win" if pct > 0 else "loss"
                exit_pnl = round(pct, 3)
            else:
                exit_pnl = round(exit_pnl, 3)

            coin_trades.append({
                "symbol":     symbol,
                "coin":       coin,
                "entry_ts":   str(entry_ts)[:16],
                "entry_price": entry_price,
                "sl":          round(sl, 6),
                "tp":          round(tp, 6),
                "ob_mfi":      round(ob_mfi, 1),
                "result":      result,
                "pnl_pct":     exit_pnl,
            })
            last_trade_ts = entry_ts

        trades.extend(coin_trades)
        print(f"{len(coin_trades)} trades")

    return pd.DataFrame(trades)


def print_stats(df, label):
    if df.empty:
        print(f"\n{label}: No trades.")
        return
    wins   = df[df["result"] == "win"]
    losses = df[df["result"] == "loss"]
    n      = len(df)
    wr     = len(wins) / n * 100
    avg_w  = wins["pnl_pct"].mean()   if len(wins)   else 0
    avg_l  = losses["pnl_pct"].mean() if len(losses) else 0
    gw     = wins["pnl_pct"].sum()    if len(wins)   else 0
    gl     = losses["pnl_pct"].abs().sum() if len(losses) else 0
    pf     = gw / gl if gl > 0 else float("inf")
    exp    = (wr / 100 * avg_w) + ((1 - wr / 100) * avg_l)
    total  = df["pnl_pct"].sum()

    print(f"\n{'='*62}")
    print(f"  {label}")
    print(f"{'='*62}")
    print(f"  Total trades  : {n}")
    print(f"  Wins / Losses : {len(wins)} / {len(losses)}")
    print(f"  Win Rate      : {wr:.1f}%")
    print(f"  Avg Win       : +{avg_w:.2f}%")
    print(f"  Avg Loss      : {avg_l:.2f}%")
    print(f"  Expectancy    : {exp:+.3f}% per trade")
    print(f"  Profit Factor : {pf:.2f}")
    print(f"  Total PnL     : {total:+.2f}%  (equal-size per trade)")
    print(f"{'='*62}")
    return exp


def print_coin_breakdown(df, top_n=10):
    if df.empty:
        return pd.DataFrame()
    grouped = df.groupby("coin").agg(
        trades  = ("pnl_pct", "count"),
        wins    = ("result",  lambda x: (x == "win").sum()),
        pnl     = ("pnl_pct", "sum"),
    ).reset_index()
    grouped["wr"]  = grouped["wins"] / grouped["trades"] * 100
    grouped["exp"] = grouped["wr"] / 100 * 4.5 + (1 - grouped["wr"] / 100) * (-3.0)
    grouped = grouped.sort_values("pnl", ascending=False)

    print(f"\n--- Per-Coin Breakdown ({len(grouped)} coins) ---")
    hdr = f"  {'Coin':<14} {'Trades':>7} {'WR%':>7} {'PnL%':>9} {'Exp%/trade':>12}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for _, r in grouped.iterrows():
        print(f"  {r['coin']:<14} {int(r['trades']):>7} {r['wr']:>6.1f}%"
              f" {r['pnl']:>+8.2f}%  {r['exp']:>+9.3f}%")
    return grouped


def main():
    print("=" * 70)
    print("  MFI Coin Hunter — Backtest (matches paper_trader.py v1.3)")
    print(f"  Period  : last {LOOKBACK_DAYS} days | Universe: ≥${MIN_VOLUME_USDT/1e6:.0f}M/day")
    print(f"  Signal  : MFI≥80 OB window + c1 quality red candle + 2H sub-candle red")
    print(f"  Entry   : close of first 2H sub-candle of c2 (if red)")
    print(f"  SL      : {MAX_SL_PCT*100:.0f}% fixed  |  TP: {MAX_SL_PCT*RR_RATIO*100:.1f}%  ({RR_RATIO}× RR)")
    print(f"  Cooldown: {COOLDOWN_HOURS}H per coin after loss")
    print("=" * 70)

    exchange = ccxt.binanceusdm({"enableRateLimit": True})
    exchange.load_markets()

    since_ms = int((datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).timestamp() * 1000)

    symbols = get_universe(exchange)

    print(f"Running backtest on {len(symbols)} symbols …\n")
    t0 = time.time()
    df = run_backtest(exchange, symbols, since_ms)
    print(f"\nCompleted in {time.time()-t0:.1f}s — {len(df)} total trades.\n")

    if df.empty:
        print("No trades found.")
        return

    # ── Overall stats ────────────────────────────────────────────────────────
    print_stats(df, "FULL UNIVERSE — MFI Coin Hunter (3%SL / 4.5%TP)")

    # ── Per-coin breakdown ───────────────────────────────────────────────────
    grouped = print_coin_breakdown(df)

    # ── MFI tier breakdown ───────────────────────────────────────────────────
    bins   = [0, 89.999, 94.999, 200]
    labels = ["MFI 80-89 (0.5×)", "MFI 90-94 (1×)", "MFI 95+ (1.5×)"]
    df["mfi_tier"] = pd.cut(df["ob_mfi"], bins=bins, labels=labels)
    print("\n--- Performance by MFI Tier ---")
    for tier in labels:
        sub = df[df["mfi_tier"] == tier]
        if sub.empty:
            continue
        w  = (sub["result"] == "win").sum()
        n  = len(sub)
        wr = w / n * 100
        pnl = sub["pnl_pct"].sum()
        print(f"  {tier:<22}  {n:>4} trades  WR: {wr:>5.1f}%  PnL: {pnl:>+7.2f}%")

    # ── Top 10 by Win Rate (≥3 trades) ──────────────────────────────────────
    if not grouped.empty:
        eligible = grouped[grouped["trades"] >= 3].sort_values("wr", ascending=False)
        print(f"\n--- Top 10 Coins by Win Rate (≥3 trades) ---")
        hdr = f"  {'Coin':<14} {'Trades':>7} {'WR%':>7} {'PnL%':>9} {'Exp%/trade':>12}"
        print(hdr)
        print("  " + "-" * (len(hdr) - 2))
        for _, r in eligible.head(10).iterrows():
            print(f"  {r['coin']:<14} {int(r['trades']):>7} {r['wr']:>6.1f}%"
                  f" {r['pnl']:>+8.2f}%  {r['exp']:>+9.3f}%")

        # ── Focused sub-backtest: top-5 profitable coins ─────────────────────
        # Criteria: ≥3 trades, positive expectancy, sorted by WR
        profitable = eligible[eligible["exp"] > 0]
        top5_coins = profitable.head(5)["coin"].tolist()

        if top5_coins:
            top5_syms = [c + "/USDT:USDT" for c in top5_coins]
            print(f"\n{'='*70}")
            print(f"  FOCUSED SUB-BACKTEST — Top {len(top5_coins)} Coins (≥3 trades, positive expectancy)")
            print(f"  Coins: {', '.join(top5_coins)}")
            print(f"{'='*70}")

            df_top5 = df[df["coin"].isin(top5_coins)]
            print_stats(df_top5, f"Top-{len(top5_coins)} Coins — MFI Coin Hunter")

            print(f"\n--- Top-{len(top5_coins)} Per-Coin Detail ---")
            sub = df_top5.groupby("coin").agg(
                trades = ("pnl_pct", "count"),
                wins   = ("result",  lambda x: (x == "win").sum()),
                pnl    = ("pnl_pct", "sum"),
            ).reset_index()
            sub["wr"]  = sub["wins"] / sub["trades"] * 100
            sub["exp"] = sub["wr"] / 100 * 4.5 + (1 - sub["wr"] / 100) * (-3.0)
            sub = sub.sort_values("wr", ascending=False)
            hdr2 = f"  {'Coin':<14} {'Trades':>7} {'WR%':>7} {'PnL%':>9} {'Expectancy':>12}"
            print(hdr2)
            print("  " + "-" * (len(hdr2) - 2))
            for _, r in sub.iterrows():
                print(f"  {r['coin']:<14} {int(r['trades']):>7} {r['wr']:>6.1f}%"
                      f" {r['pnl']:>+8.2f}%  {r['exp']:>+9.3f}%")

            # Sample trades for top-5
            print(f"\n--- Sample trades (last 10 from top-{len(top5_coins)} coins) ---")
            cols = ["coin", "entry_ts", "entry_price", "sl", "tp", "ob_mfi", "result", "pnl_pct"]
            sample = df_top5.tail(10)[cols].copy()
            sample["pnl_pct"] = sample["pnl_pct"].map("{:+.2f}%".format)
            print(sample.to_string(index=False))
        else:
            print("\nNo coins with positive expectancy and ≥3 trades found in top-10.")

    print("\nDone.")


if __name__ == "__main__":
    main()
