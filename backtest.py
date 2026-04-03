"""
Backtest — v2_strict Strategy
──────────────────────────────
- Finds all historical MFI reversal signals on 6H chart
- Entry: close of signal candle
- Stop Loss: highest high of last 3 x 1H candles above entry
- Take Profit: entry - (SL distance × 1.5)  → Risk:Reward = 2:3
- Simulates forward on 1H candles to see which hits first
- Reports: Win rate, P&L, avg win, avg loss

Run:
    python backtest.py
"""

import ccxt
import pandas as pd
import time
from datetime import datetime

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────
MFI_LENGTH      = 14
MFI_OVERBOUGHT  = 80
RR_RATIO        = 1.5      # 2:3 = risk 2 to make 3 = 1.5x reward
SL_LOOKBACK_1H  = 3        # Use high of last 3 x 1H candles as SL
MAX_SL_PCT      = 0.03     # FIX 1: Cap stop loss at max 3% from entry
MIN_VOLUME_USDT = 5_000_000  # FIX 2: Min $5M 24h volume — filters micro caps
MAX_HOLD_1H     = 48       # Max 48 x 1H candles to hold (2 days)
CANDLE_LIMIT_6H = 300      # How far back to scan 6H signals
CANDLE_LIMIT_1H = 500      # 1H candles for forward simulation

exchange = ccxt.binanceusdm()

def get_all_coins():
    markets  = exchange.load_markets()
    tickers  = exchange.fetch_tickers()   # includes 24h quoteVolume
    filtered = []
    for s, m in markets.items():
        if not (m.get("quote") == "USDT" and m.get("active") and not m.get("expiry")):
            continue
        vol = tickers.get(s, {}).get("quoteVolume") or 0
        if vol >= MIN_VOLUME_USDT:
            filtered.append(s)
    print(f"After $5M volume filter: {len(filtered)} coins (removed low-cap)")
    return filtered

# ─────────────────────────────────────────
# MFI
# ─────────────────────────────────────────
def calculate_mfi(df, length=14):
    tp  = (df["high"] + df["low"] + df["close"]) / 3
    rmf = tp * df["volume"]
    pos = rmf.where(tp > tp.shift(1), 0)
    neg = rmf.where(tp < tp.shift(1), 0)
    ps  = pos.rolling(length).sum()
    ns  = neg.rolling(length).sum().replace(0, 1e-10)
    return 100 - (100 / (1 + ps / ns))

# ─────────────────────────────────────────
# SIGNAL FINDERS — v1_loose & v2_strict
# ─────────────────────────────────────────
def find_signals_v1_loose(df_6h):
    df_6h["mfi"]    = calculate_mfi(df_6h, MFI_LENGTH)
    df_6h["is_red"] = df_6h["close"] < df_6h["open"]
    signals = []

    for i in range(MFI_LENGTH + 3, len(df_6h) - 1):
        c0 = df_6h.iloc[i]
        c1 = df_6h.iloc[i - 1]
        c2 = df_6h.iloc[i - 2]

        signal_1 = c1["mfi"] >= MFI_OVERBOUGHT and c0["is_red"]
        signal_2 = c2["mfi"] >= MFI_OVERBOUGHT and c1["is_red"] and c0["is_red"]

        if signal_1 or signal_2:
            signals.append({
                "entry_time":  c0["timestamp"],
                "entry_price": c0["close"],
                "signal_mfi":  round(c1["mfi"] if signal_1 else c2["mfi"], 2),
            })
    return signals

def find_signals(df_6h):
    df_6h["mfi"] = calculate_mfi(df_6h, MFI_LENGTH)
    signals = []

    for i in range(MFI_LENGTH + 5, len(df_6h) - 1):
        c_ob   = df_6h.iloc[i - 3]
        c1     = df_6h.iloc[i - 2]
        c2     = df_6h.iloc[i - 1]
        c_live = df_6h.iloc[i]

        # MFI overbought + declining
        if c_ob["mfi"] < MFI_OVERBOUGHT: continue
        if c1["mfi"] >= c_ob["mfi"]: continue

        # First red candle quality
        c1_range = c1["high"] - c1["low"]
        if c1_range == 0: continue

        c1_body_ratio = (c1["open"] - c1["close"]) / c1_range
        c1_lower_wick = (min(c1["open"], c1["close"]) - c1["low"]) / c1_range
        avg_vol       = df_6h["volume"].iloc[max(0, i-20):i].mean()

        first_red_valid = (
            c1["close"] < c1["open"] and
            c1_body_ratio >= 0.40 and
            c1_lower_wick <= 0.35 and
            c1["high"] < c_ob["high"] and
            c1["close"] < c_ob["close"] and
            c1["volume"] >= avg_vol
        )
        if not first_red_valid: continue

        # Second red candle
        c2_range = c2["high"] - c2["low"]
        if c2_range == 0: continue

        second_valid = (
            c2["close"] < c2["open"] and
            c2["high"] < c1["high"] and
            c2["close"] < c1["close"] and
            abs(c2["open"] - c2["close"]) / c2_range <= 0.80
        )
        if not second_valid: continue

        # Consistent downtrend
        if not (c_ob["close"] > c1["close"] > c2["close"]): continue

        signals.append({
            "entry_time":  c_live["timestamp"],
            "entry_price": c_live["close"],
            "signal_mfi":  round(c_ob["mfi"], 2),
        })

    return signals

# ─────────────────────────────────────────
# SIMULATE TRADE ON 1H CANDLES
# ─────────────────────────────────────────
def simulate_trade(signal, df_1h):
    entry_price = signal["entry_price"]
    entry_time  = signal["entry_time"]

    # Get 1H candles from entry point onward
    future_1h = df_1h[df_1h["timestamp"] >= entry_time].reset_index(drop=True)
    if len(future_1h) < SL_LOOKBACK_1H + 2:
        return None

    # SL = highest high of first 3 x 1H candles (above entry = short stop)
    sl_candles    = future_1h.iloc[:SL_LOOKBACK_1H]
    sl_price_raw  = sl_candles["high"].max()

    # Ensure SL is above entry (short trade)
    if sl_price_raw <= entry_price:
        sl_price_raw = entry_price * 1.015   # Default 1.5% SL if no clear high

    # FIX 1: Cap SL at MAX_SL_PCT (3%) — prevents catastrophic losses on volatile coins
    max_sl_price = entry_price * (1 + MAX_SL_PCT)
    sl_price     = min(sl_price_raw, max_sl_price)

    sl_distance = sl_price - entry_price
    tp_price    = entry_price - (sl_distance * RR_RATIO)   # 2:3 risk:reward

    # Ensure TP is below entry
    if tp_price >= entry_price:
        return None

    # Simulate forward candle by candle
    for _, candle in future_1h.iloc[SL_LOOKBACK_1H:SL_LOOKBACK_1H + MAX_HOLD_1H].iterrows():
        if candle["low"] <= tp_price:
            pnl_pct = ((entry_price - tp_price) / entry_price) * 100
            return {
                "result":     "WIN",
                "pnl_pct":    round(pnl_pct, 3),
                "entry":      round(entry_price, 6),
                "sl":         round(sl_price, 6),
                "tp":         round(tp_price, 6),
                "sl_dist_pct": round((sl_distance / entry_price) * 100, 2),
                "entry_time": str(entry_time)[:16],
            }
        if candle["high"] >= sl_price:
            pnl_pct = -((sl_price - entry_price) / entry_price) * 100
            return {
                "result":     "LOSS",
                "pnl_pct":    round(pnl_pct, 3),
                "entry":      round(entry_price, 6),
                "sl":         round(sl_price, 6),
                "tp":         round(tp_price, 6),
                "sl_dist_pct": round((sl_distance / entry_price) * 100, 2),
                "entry_time": str(entry_time)[:16],
            }

    # Timeout — exit at last candle close
    last_close  = future_1h.iloc[min(MAX_HOLD_1H, len(future_1h)-1)]["close"]
    pnl_pct     = ((entry_price - last_close) / entry_price) * 100
    return {
        "result":     "WIN" if last_close < entry_price else "LOSS",
        "pnl_pct":    round(pnl_pct, 3),
        "entry":      round(entry_price, 6),
        "sl":         round(sl_price, 6),
        "tp":         round(tp_price, 6),
        "sl_dist_pct": round((sl_distance / entry_price) * 100, 2),
        "entry_time": str(entry_time)[:16],
        "note":       "Timeout exit",
    }

# ─────────────────────────────────────────
# SUMMARISE + PRINT RESULTS
# ─────────────────────────────────────────
def print_summary(all_trades, coin_stats, version_name, out_filename):
    if not all_trades:
        print(f"\n  No trades found for {version_name}.")
        return {}

    wins_all   = [t for t in all_trades if t["result"] == "WIN"]
    losses_all = [t for t in all_trades if t["result"] == "LOSS"]
    total      = len(all_trades)
    win_rate   = round(len(wins_all) / total * 100, 1)
    total_pnl  = round(sum(t["pnl_pct"] for t in all_trades), 2)
    avg_win    = round(sum(t["pnl_pct"] for t in wins_all) / len(wins_all), 3) if wins_all else 0
    avg_loss   = round(sum(t["pnl_pct"] for t in losses_all) / len(losses_all), 3) if losses_all else 0
    expectancy = round((win_rate/100 * avg_win) + ((1 - win_rate/100) * avg_loss), 3)

    print(f"\n{'═'*60}")
    print(f"  RESULTS — {version_name}")
    print(f"{'═'*60}")
    print(f"  Total Trades   : {total}")
    print(f"  Wins           : {len(wins_all)}  ({win_rate}%)")
    print(f"  Losses         : {len(losses_all)}")
    print(f"  Total PnL      : {total_pnl}%")
    print(f"  Avg Win        : +{avg_win}%")
    print(f"  Avg Loss       : {avg_loss}%")
    print(f"  Expectancy     : {expectancy}% per trade")
    verdict = "✅ PROFITABLE" if total_pnl > 0 and expectancy > 0 else "❌ UNPROFITABLE"
    print(f"  Verdict        : {verdict}")

    print(f"\n  Top 10 Coins by PnL:")
    print(f"  {'Coin':<10} {'Trades':>7} {'W':>5} {'L':>5} {'WR%':>7} {'PnL%':>8}")
    print("  " + "-"*46)
    for cs in sorted(coin_stats, key=lambda x: x["total_pnl"], reverse=True)[:10]:
        print(f"  {cs['coin']:<10} {cs['trades']:>7} {cs['wins']:>5} {cs['losses']:>5} {cs['win_rate']:>6}% {cs['total_pnl']:>7}%")

    out_path = f"/Users/macbook/Downloads/Content Projects/02 - Trading Coin Hunter/Trade Logs/{out_filename}"
    pd.DataFrame(all_trades).to_csv(out_path, index=False)
    print(f"\n  Saved to: {out_filename}")

    return {
        "version": version_name, "total": total, "wins": len(wins_all),
        "losses": len(losses_all), "win_rate": win_rate,
        "total_pnl": total_pnl, "avg_win": avg_win,
        "avg_loss": avg_loss, "expectancy": expectancy
    }

# ─────────────────────────────────────────
# SCAN ONE VERSION ACROSS ALL COINS
# ─────────────────────────────────────────
def scan_version(coins, signal_finder, version_name):
    all_trades = []
    coin_stats = []

    print(f"\n{'─'*60}")
    print(f"  Running {version_name} on {len(coins)} coins...")
    print(f"{'─'*60}")

    for symbol in coins:
        coin = symbol.replace("/USDT:USDT", "").replace(":USDT", "")
        try:
            ohlcv_6h = exchange.fetch_ohlcv(symbol, "6h", limit=CANDLE_LIMIT_6H)
            df_6h    = pd.DataFrame(ohlcv_6h, columns=["timestamp","open","high","low","close","volume"])
            df_6h["timestamp"] = pd.to_datetime(df_6h["timestamp"], unit="ms")

            ohlcv_1h = exchange.fetch_ohlcv(symbol, "1h", limit=CANDLE_LIMIT_1H)
            df_1h    = pd.DataFrame(ohlcv_1h, columns=["timestamp","open","high","low","close","volume"])
            df_1h["timestamp"] = pd.to_datetime(df_1h["timestamp"], unit="ms")

            signals = signal_finder(df_6h)
            wins = losses = 0
            coin_pnl = 0

            for sig in signals:
                trade = simulate_trade(sig, df_1h)
                if trade:
                    trade["coin"] = coin
                    all_trades.append(trade)
                    if trade["result"] == "WIN":
                        wins += 1; coin_pnl += trade["pnl_pct"]
                    else:
                        losses += 1; coin_pnl += trade["pnl_pct"]

            total = wins + losses
            if total > 0:
                wr = round(wins / total * 100, 1)
                print(f"  {coin:<12} {len(signals)} signals → {wins}W/{losses}L | WR:{wr}% | PnL:{round(coin_pnl,2)}%")
                coin_stats.append({
                    "coin": coin, "trades": total, "wins": wins,
                    "losses": losses, "win_rate": wr, "total_pnl": round(coin_pnl, 2)
                })

            time.sleep(0.15)

        except Exception as e:
            pass  # Skip failed coins silently

    return all_trades, coin_stats

# ─────────────────────────────────────────
# MAIN BACKTEST
# ─────────────────────────────────────────
def run_backtest():
    print("\n" + "═"*60)
    print("  BACKTEST — 6H Signal | 1H SL/TP | Risk:Reward 2:3")
    print(f"  Date: {datetime.now().strftime('%d %b %Y %H:%M')}")
    print("═"*60)

    print("\nLoading all Binance USDT Futures coins...")
    all_coins = get_all_coins()
    print(f"Found {len(all_coins)} coins.")

    # ── RUN v2_strict ─────────────────────────────────────────
    trades_v2, stats_v2 = scan_version(all_coins, find_signals, "v2_strict + vol filter + 3% SL cap")
    summary_v2 = print_summary(trades_v2, stats_v2, "v2_strict + vol filter + 3% SL cap", "backtest_v2_strict_v3.csv")

    # ── RUN v1_loose ─────────────────────────────────────────
    trades_v1, stats_v1 = scan_version(all_coins, find_signals_v1_loose, "v1_loose + vol filter + 3% SL cap")
    summary_v1 = print_summary(trades_v1, stats_v1, "v1_loose + vol filter + 3% SL cap", "backtest_v1_loose_v3.csv")

    # ── HEAD TO HEAD COMPARISON ───────────────────────────────
    print(f"\n{'═'*60}")
    print("  HEAD TO HEAD COMPARISON")
    print(f"{'═'*60}")
    print(f"  {'Metric':<22} {'v2_strict':>15} {'v1_loose':>15}")
    print("  " + "─"*52)

    metrics = [
        ("Total Trades",   "total",      ""),
        ("Win Rate",       "win_rate",   "%"),
        ("Total PnL",      "total_pnl",  "%"),
        ("Avg Win",        "avg_win",    "%"),
        ("Avg Loss",       "avg_loss",   "%"),
        ("Expectancy",     "expectancy", "% per trade"),
    ]

    for label, key, unit in metrics:
        v2_val = summary_v2.get(key, "N/A")
        v1_val = summary_v1.get(key, "N/A")
        print(f"  {label:<22} {str(v2_val)+unit:>15} {str(v1_val)+unit:>15}")

    print(f"\n  {'Verdict v2_strict':<22} {'✅ PROFITABLE' if summary_v2.get('expectancy',0) > 0 else '❌ UNPROFITABLE':>15}")
    print(f"  {'Verdict v1_loose':<22} {'✅ PROFITABLE' if summary_v1.get('expectancy',0) > 0 else '❌ UNPROFITABLE':>15}")
    print("═"*60 + "\n")

    pass  # all work done above

if __name__ == "__main__":
    run_backtest()
