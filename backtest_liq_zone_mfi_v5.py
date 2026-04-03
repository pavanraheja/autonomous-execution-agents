#!/usr/bin/env python3
"""
Liquidation Zone MFI Reversal — Backtest v5 (Full Coin Universe)
─────────────────────────────────────────────────────────────────
Signal  : BTC 6H MFI reversal (zone-filtered)
Coins   : BTC (self-trade) + 12 alts following BTC signal
          BNB, SOL, ADA, DOGE, LTC, UNI, LINK, DOT (new)
          ETH, XRP, AVAX, SUI (re-confirmed)

Per coin: best TP config per regime (BULL/BEAR)
Output  : Ranked table — which coins to include in the bot
"""

import ccxt
import pandas as pd
import numpy as np
import time
import warnings
from datetime import datetime, timedelta, timezone
from itertools import product as iproduct

warnings.filterwarnings("ignore")

# ─── CONFIG ──────────────────────────────────────────────────────────────────
BTC_SYMBOL = "BTC/USDT:USDT"

ALL_COINS = {
    "BTC":  "BTC/USDT:USDT",   # self-trade when signal fires
    "ETH":  "ETH/USDT:USDT",
    "BNB":  "BNB/USDT:USDT",
    "SOL":  "SOL/USDT:USDT",
    "XRP":  "XRP/USDT:USDT",
    "ADA":  "ADA/USDT:USDT",
    "DOGE": "DOGE/USDT:USDT",
    "AVAX": "AVAX/USDT:USDT",
    "LTC":  "LTC/USDT:USDT",
    "UNI":  "UNI/USDT:USDT",
    "LINK": "LINK/USDT:USDT",
    "DOT":  "DOT/USDT:USDT",
    "SUI":  "SUI/USDT:USDT",
}

TIMEFRAME      = "6h"
MFI_LENGTH     = 14
ATR_LENGTH     = 14
MFI_OB         = 80
MFI_OS         = 20
LOOKBACK_DAYS  = 1100
MAX_HOLD       = 42
BULL_END       = pd.Timestamp("2025-10-05", tz="UTC")

# Zone filter for BTC signal
LONG_SWING_WINDOW  = 40
SHORT_SWING_WINDOW = 10
LONG_ZONE_PCT      = 2.0
SHORT_ZONE_PCT     = 3.0

# TP grid per coin
TP1_OPTIONS = [3.0, 4.0, 5.0]
TP2_OPTIONS = [6.0, 8.0, 10.0]
PARTIAL_PCT = 0.5

# SL multipliers — ETH wider, everything else 1.5×
SL_MULTS = {"ETH": 2.0, "BTC": 1.5}   # default 1.5× for rest
# ─────────────────────────────────────────────────────────────────────────────


def get_sl(coin): return SL_MULTS.get(coin, 1.5)


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
    rows = []
    while True:
        batch = exchange.fetch_ohlcv(symbol, TIMEFRAME, since=since, limit=1000)
        if not batch: break
        rows += batch
        if len(batch) < 1000: break
        since = batch[-1][0] + 1
        time.sleep(0.12)
    df = pd.DataFrame(rows, columns=["ts","open","high","low","close","volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df.set_index("ts", inplace=True)
    return df.drop_duplicates()


def find_btc_signals(df):
    sigs   = []
    mfi    = df["mfi"].values
    atr    = df["atr"].values
    opens  = df["open"].values
    closes = df["close"].values
    highs  = df["high"].values
    lows   = df["low"].values

    for i in range(LONG_SWING_WINDOW + MFI_LENGTH + 5, len(df) - MAX_HOLD - 2):
        cmfi, catr, entry = mfi[i], atr[i], closes[i]
        if np.isnan(cmfi) or np.isnan(catr) or catr == 0: continue

        prev  = mfi[i-5:i]
        ts    = df.index[i]
        reg   = "BEAR" if ts >= BULL_END else "BULL"
        green = closes[i] > opens[i]
        red   = closes[i] < opens[i]

        if np.any(prev < MFI_OS) and cmfi > MFI_OS and green:
            sl = lows[max(0,i-LONG_SWING_WINDOW):i].min()
            if (entry - sl) / entry * 100 <= LONG_ZONE_PCT:
                sigs.append({"idx":i,"ts":ts,"direction":"LONG","entry":entry,
                             "atr":catr,"regime":reg})

        elif np.any(prev > MFI_OB) and cmfi < MFI_OB and red:
            rh = highs[max(0,i-SHORT_SWING_WINDOW):i+1].max()
            if (rh - entry) / entry * 100 <= SHORT_ZONE_PCT:
                sigs.append({"idx":i,"ts":ts,"direction":"SHORT","entry":entry,
                             "atr":catr,"regime":reg})
    return sigs


def map_to_coin(btc_sigs, coin_df, is_btc=False):
    """Map BTC signal timestamps to a coin's data. BTC self-trades skip MFI filter."""
    sigs = []
    for sig in btc_sigs:
        ts = sig["ts"]
        try:
            i = coin_df.index.get_loc(ts) if ts in coin_df.index else \
                (lambda d: d.argmin() if d.min().total_seconds() <= 7200 else None)(
                    (coin_df.index - ts).abs())
            if i is None: continue
        except Exception: continue
        if i >= len(coin_df) - MAX_HOLD - 2: continue

        row   = coin_df.iloc[i]
        cmfi  = row["mfi"]
        catr  = row["atr"]
        entry = row["close"]
        if np.isnan(cmfi) or np.isnan(catr) or catr == 0: continue

        d = sig["direction"]
        if not is_btc:
            if d == "LONG"  and cmfi > 60: continue
            if d == "SHORT" and cmfi < 40: continue

        sigs.append({"idx":i,"ts":coin_df.index[i],"direction":d,
                     "entry":entry,"atr":catr,"mfi":round(cmfi,1),
                     "regime":sig["regime"]})
    return sigs


def simulate(df, sig, sl_mult, tp1, tp2):
    i, entry, atr, d = sig["idx"], sig["entry"], sig["atr"], sig["direction"]
    sl_d = atr * sl_mult
    sl   = entry - sl_d if d=="LONG" else entry + sl_d
    t1   = entry*(1+tp1/100) if d=="LONG" else entry*(1-tp1/100)
    t2   = entry*(1+tp2/100) if d=="LONG" else entry*(1-tp2/100)

    pp, pf = 0.0, False
    out, tp1h, tp2h, mfe = "TIMEOUT", False, False, 0.0
    H, L, C = df["high"].values, df["low"].values, df["close"].values

    for j in range(i+1, min(i+1+MAX_HOLD, len(df))):
        h, l = H[j], L[j]
        if d == "LONG":
            mfe = max(mfe, (h-entry)/entry*100)
            if l <= sl:
                out = "BE+" if (pp+(sl-entry)/entry*100*(0.5 if pf else 1))>0.05 else "LOSS"
                return {"outcome":out,"pnl":round(pp+(sl-entry)/entry*100*(0.5 if pf else 1),3),
                        "tp1":tp1h,"tp2":tp2h,"mfe":round(mfe,2),"slpct":round(sl_d/entry*100,2)}
            if not pf and h >= t1: pp=(t1-entry)/entry*100*PARTIAL_PCT; pf=True; tp1h=True
            if h >= t2:
                out="WIN"; tp2h=True
                return {"outcome":out,"pnl":round(pp+(t2-entry)/entry*100*(0.5 if pf else 1),3),
                        "tp1":tp1h,"tp2":tp2h,"mfe":round(mfe,2),"slpct":round(sl_d/entry*100,2)}
        else:
            mfe = max(mfe, (entry-l)/entry*100)
            if h >= sl:
                out = "BE+" if (pp+(entry-sl)/entry*100*(0.5 if pf else 1))>0.05 else "LOSS"
                return {"outcome":out,"pnl":round(pp+(entry-sl)/entry*100*(0.5 if pf else 1),3),
                        "tp1":tp1h,"tp2":tp2h,"mfe":round(mfe,2),"slpct":round(sl_d/entry*100,2)}
            if not pf and l <= t1: pp=(entry-t1)/entry*100*PARTIAL_PCT; pf=True; tp1h=True
            if l <= t2:
                out="WIN"; tp2h=True
                return {"outcome":out,"pnl":round(pp+(entry-t2)/entry*100*(0.5 if pf else 1),3),
                        "tp1":tp1h,"tp2":tp2h,"mfe":round(mfe,2),"slpct":round(sl_d/entry*100,2)}

    last = C[min(i+MAX_HOLD, len(df)-1)]
    rem  = ((last-entry)/entry*100 if d=="LONG" else (entry-last)/entry*100)
    return {"outcome":"TIMEOUT","pnl":round(pp+rem*(0.5 if pf else 1),3),
            "tp1":tp1h,"tp2":tp2h,"mfe":round(mfe,2),"slpct":round(sl_d/entry*100,2)}


def best_config(df, sigs, sl_mult, coin):
    """Find best TP1/TP2 per regime by net PnL."""
    best = {}  # regime -> best row
    for tp1, tp2 in iproduct(TP1_OPTIONS, TP2_OPTIONS):
        if tp2 <= tp1: continue
        records = []
        for sig in sigs:
            r = simulate(df, sig, sl_mult, tp1, tp2)
            r.update(sig)
            records.append(r)
        if not records: continue
        rdf = pd.DataFrame(records)

        for regime in ["BULL","BEAR","ALL"]:
            sub = rdf if regime=="ALL" else rdf[rdf["regime"]==regime]
            if len(sub) < 3: continue

            wins = sub[sub["outcome"]=="WIN"]
            loss = sub[sub["outcome"]=="LOSS"]
            n    = len(sub)
            wr   = len(wins)/n*100
            net  = sub["pnl"].sum()
            pf   = (wins["pnl"].sum()/abs(loss["pnl"].sum())
                    if len(loss) and loss["pnl"].sum()!=0 else 99.0)
            longs  = sub[sub["direction"]=="LONG"]
            shorts = sub[sub["direction"]=="SHORT"]
            l_wr = (longs["outcome"]=="WIN").sum()/len(longs)*100 if len(longs) else 0
            s_wr = (shorts["outcome"]=="WIN").sum()/len(shorts)*100 if len(shorts) else 0

            row = {
                "coin":coin,"regime":regime,"tp1":tp1,"tp2":tp2,"sl":sl_mult,
                "n":n,"n_long":len(longs),"n_short":len(shorts),
                "wr":round(wr,1),"wr_long":round(l_wr,1),"wr_short":round(s_wr,1),
                "pf":round(min(pf,99),2),"net":round(net,1),
                "avg_win":round(wins["pnl"].mean() if len(wins) else 0,2),
                "avg_loss":round(loss["pnl"].mean() if len(loss) else 0,2),
                "tp1_rate":round(sub["tp1"].sum()/n*100,1),
                "tp2_rate":round(sub["tp2"].sum()/n*100,1),
                "mfe":round(sub["mfe"].mean(),2),
                "avg_sl":round(sub["slpct"].mean(),2),
                "spm":round(n/(LOOKBACK_DAYS/30),1),
                "n_win":len(wins),"n_loss":len(loss),
                "n_be":len(sub[sub["outcome"]=="BE+"]),
                "n_to":len(sub[sub["outcome"]=="TIMEOUT"]),
            }
            if regime not in best or net > best[regime]["net"]:
                best[regime] = row
    return best


def main():
    print("=" * 72)
    print("  Liq Zone MFI Reversal v5 — Full Coin Universe")
    print(f"  {len(ALL_COINS)} coins | 1100d | BTC trigger | BULL/BEAR regime split")
    print("=" * 72)

    exchange = ccxt.binance({"options": {"defaultType": "future"}})

    # ── Fetch + build BTC signal list ─────────────────────────────────────────
    print("\n  Fetching BTC signal data...", flush=True)
    btc_df = fetch_candles(exchange, BTC_SYMBOL, LOOKBACK_DAYS)
    btc_df["mfi"] = calc_mfi(btc_df)
    btc_df["atr"] = calc_atr(btc_df)
    btc_sigs = find_btc_signals(btc_df)

    bull_n = sum(1 for s in btc_sigs if s["regime"]=="BULL")
    bear_n = sum(1 for s in btc_sigs if s["regime"]=="BEAR")
    print(f"  BTC signals: {len(btc_sigs)} | BULL:{bull_n} BEAR:{bear_n}")

    # ── Process each coin ─────────────────────────────────────────────────────
    all_best = {}   # coin -> {regime -> best_row}

    for coin, symbol in ALL_COINS.items():
        is_btc = (coin == "BTC")
        sl     = get_sl(coin)

        if is_btc:
            df  = btc_df
            print(f"\n  {coin} (self-trade, SL={sl}×ATR)...", end=" ", flush=True)
            sigs = map_to_coin(btc_sigs, df, is_btc=True)
        else:
            print(f"\n  {coin} (follows BTC, SL={sl}×ATR)...", end=" ", flush=True)
            try:
                df = fetch_candles(exchange, symbol, LOOKBACK_DAYS)
                df["mfi"] = calc_mfi(df)
                df["atr"] = calc_atr(df)
            except Exception as e:
                print(f"FETCH ERROR: {e}")
                continue
            sigs = map_to_coin(btc_sigs, df, is_btc=False)

        bull_s = sum(1 for s in sigs if s["regime"]=="BULL")
        bear_s = sum(1 for s in sigs if s["regime"]=="BEAR")
        print(f"{len(sigs)} sigs (BULL:{bull_s} BEAR:{bear_s})")

        best = best_config(df, sigs, sl, coin)
        all_best[coin] = best
        time.sleep(0.3)

    # ── Print regime tables ───────────────────────────────────────────────────
    for regime, direction in [("BULL","LONG"), ("BEAR","SHORT")]:
        print(f"\n{'='*72}")
        print(f"  {regime} MARKET — {direction}S (best TP config per coin)")
        print(f"  {'Coin':<6} {'N':>5} {'SPM':>5} {'WR':>7} {'WR-'+direction:<9} {'PF':>6} "
              f"{'Net%':>8} {'TP1':>5} {'TP2':>5} {'AvgW':>7} {'AvgL':>7} {'AvgSL':>7} Grade")
        print(f"  {'─'*6}-+{'─'*5}-+{'─'*5}-+{'─'*7}-+{'─'*9}-+{'─'*6}-+"
              f"{'─'*8}-+{'─'*5}-+{'─'*5}-+{'─'*7}-+{'─'*7}-+{'─'*7}------")

        ranked = []
        for coin in ALL_COINS:
            b = all_best.get(coin, {}).get(regime)
            if not b: continue
            wr_dir = b["wr_long"] if direction=="LONG" else b["wr_short"]
            ranked.append((wr_dir, coin, b))
        ranked.sort(reverse=True)

        for wr_dir, coin, b in ranked:
            grade = "★★★" if wr_dir >= 70 else ("★★ " if wr_dir >= 55 else ("★  " if wr_dir >= 40 else "   "))
            pf_s  = f"{b['pf']:.2f}" if b['pf'] < 50 else ">50"
            print(f"  {coin:<6} {b['n']:>5} {b['spm']:>5.1f} {b['wr']:>6.1f}% {wr_dir:>8.1f}% "
                  f"{pf_s:>6} {b['net']:>+8.1f}% {b['tp1']:>4.0f}% {b['tp2']:>4.0f}% "
                  f"{b['avg_win']:>+7.2f}% {b['avg_loss']:>7.2f}% {b['avg_sl']:>7.2f}%  {grade}")

    # ── Bot Selection Table ───────────────────────────────────────────────────
    print(f"\n{'='*72}")
    print(f"  BOT COIN SELECTION — Include/Exclude decision")
    print(f"  Criteria: BEAR SHORT WR ≥ 40% AND PF ≥ 1.0 → INCLUDE NOW")
    print(f"            BULL LONG  WR ≥ 40% AND PF ≥ 1.0 → INCLUDE NEXT BULL")
    print(f"{'='*72}")
    print(f"  {'Coin':<6} {'Bear WR':>9} {'Bear PF':>9} {'Bull WR':>9} {'Bull PF':>9}  Now   Oct'26")
    print(f"  {'─'*6}-+{'─'*9}-+{'─'*9}-+{'─'*9}-+{'─'*9}-------+--------")

    for coin in ALL_COINS:
        bear = all_best.get(coin, {}).get("BEAR", {})
        bull = all_best.get(coin, {}).get("BULL", {})
        bear_wr = bear.get("wr_short", 0)
        bear_pf = bear.get("pf", 0)
        bull_wr = bull.get("wr_long", 0)
        bull_pf = bull.get("pf", 0)
        now   = "✓ YES" if (bear_wr >= 40 and bear_pf >= 1.0) else "✗ NO "
        next_ = "✓ YES" if (bull_wr >= 40 and bull_pf >= 1.0) else "✗ NO "
        bear_n = bear.get("n_short", 0)
        bull_n = bull.get("n_long", 0)
        print(f"  {coin:<6} {bear_wr:>8.1f}% {bear_pf:>9.2f} {bull_wr:>8.1f}% {bull_pf:>9.2f}  {now}  {next_}")

    # ── Final signal count if all included ───────────────────────────────────
    print(f"\n{'='*72}")
    print(f"  SIGNAL VOLUME ESTIMATE (if all ✓ coins included)")
    print(f"{'='*72}")
    included_now  = [c for c in ALL_COINS
                     if all_best.get(c,{}).get("BEAR",{}).get("wr_short",0) >= 40
                     and all_best.get(c,{}).get("BEAR",{}).get("pf",0) >= 1.0]
    included_bull = [c for c in ALL_COINS
                     if all_best.get(c,{}).get("BULL",{}).get("wr_long",0) >= 40
                     and all_best.get(c,{}).get("BULL",{}).get("pf",0) >= 1.0]

    bear_spm = sum(all_best.get(c,{}).get("BEAR",{}).get("spm",0) for c in included_now)
    bull_spm = sum(all_best.get(c,{}).get("BULL",{}).get("spm",0) for c in included_bull)
    print(f"  Bear mode coins : {included_now}")
    print(f"  Bear trades/mo  : ~{bear_spm:.0f} total across {len(included_now)} coins")
    print(f"  Bull mode coins : {included_bull}")
    print(f"  Bull trades/mo  : ~{bull_spm:.0f} total across {len(included_bull)} coins")
    print(f"{'='*72}")

    # Save
    rows = []
    for coin, regimes in all_best.items():
        for regime, b in regimes.items():
            rows.append(b)
    pd.DataFrame(rows).to_csv(
        "/Users/macbook/Downloads/Content Projects/02 - Trading Coin Hunter/Trade Logs/backtest_liq_zone_v5.csv",
        index=False)
    print(f"  Saved → backtest_liq_zone_v5.csv")


if __name__ == "__main__":
    main()
