#!/usr/bin/env python3
"""
Liquidation Zone MFI Reversal — v4b: Regime Fix
─────────────────────────────────────────────────
Problem in v4: BTC zone≤2% gives only 2 bear signals because in downtrends,
MFI overbought signals fire while price is well BELOW old swing highs (last 40 candles).

Fix: For SHORT signals, measure proximity to the RECENT local high (last 10 candles)
     not the full 40-candle swing high. This matches real behaviour: when MFI >80
     and crosses back down, price IS at the current bounce top (=zone).

     For LONG signals: measure from 40-candle swing low (unchanged — dip buying
     near support works the same way in both regimes).

Also: Proper regime-split for all alts using 730-day data (more bear coverage).
"""

import ccxt
import pandas as pd
import numpy as np
import time
import warnings
from datetime import datetime, timedelta, timezone

warnings.filterwarnings("ignore")

BTC_SYMBOL     = "BTC/USDT:USDT"
ALT_SYMBOLS    = {
    "ETH":  "ETH/USDT:USDT",
    "XRP":  "XRP/USDT:USDT",
    "AVAX": "AVAX/USDT:USDT",
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

# Zone filters
LONG_SWING_WINDOW  = 40    # look back 40 candles for support (long entries)
SHORT_SWING_WINDOW = 10    # look back 10 candles for recent bounce top (short entries)
LONG_ZONE_PCT      = 2.0   # within 2% of 40-candle swing low
SHORT_ZONE_PCT     = 3.0   # within 3% of 10-candle recent high (bounce top)

# TP/SL — optimal from v4
CONFIGS = {
    "BTC":  {"sl": 1.5, "tp1": 5.0, "tp2": 8.0},
    "ETH":  {"sl": 2.0, "tp1": 3.0, "tp2": 10.0},
    "XRP":  {"sl": 1.5, "tp1": 4.0, "tp2": 8.0},
    "AVAX": {"sl": 1.5, "tp1": 4.0, "tp2": 6.0},
    "SUI":  {"sl": 1.5, "tp1": 3.0, "tp2": 6.0},
}
PARTIAL_PCT = 0.5


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
    all_c = []
    while True:
        batch = exchange.fetch_ohlcv(symbol, TIMEFRAME, since=since, limit=1000)
        if not batch:
            break
        all_c += batch
        if len(batch) < 1000:
            break
        since = batch[-1][0] + 1
        time.sleep(0.15)
    df = pd.DataFrame(all_c, columns=["ts","open","high","low","close","volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df.set_index("ts", inplace=True)
    df.drop_duplicates(inplace=True)
    return df


def find_btc_signals(df):
    sigs   = []
    mfi    = df["mfi"].values
    atr    = df["atr"].values
    opens  = df["open"].values
    closes = df["close"].values
    highs  = df["high"].values
    lows   = df["low"].values

    for i in range(LONG_SWING_WINDOW + MFI_LENGTH + 5, len(df) - MAX_HOLD - 2):
        cur_mfi   = mfi[i]
        prev_mfis = mfi[i-5:i]
        cur_atr   = atr[i]
        entry     = closes[i]
        if np.isnan(cur_mfi) or np.isnan(cur_atr) or cur_atr == 0:
            continue

        ts     = df.index[i]
        regime = "BEAR" if ts >= BULL_END else "BULL"
        green  = closes[i] > opens[i]
        red    = closes[i] < opens[i]

        # LONG: near 40-candle swing low
        if np.any(prev_mfis < MFI_OS) and cur_mfi > MFI_OS and green:
            sl_start = max(0, i - LONG_SWING_WINDOW)
            swing_low = lows[sl_start:i].min()
            dist = (entry - swing_low) / entry * 100
            if dist <= LONG_ZONE_PCT:
                sigs.append({"idx":i,"ts":ts,"direction":"LONG","entry":entry,
                             "atr":cur_atr,"mfi":round(cur_mfi,1),"regime":regime,
                             "dist":round(dist,2)})

        # SHORT: near 10-candle recent bounce high (current bounce top)
        elif np.any(prev_mfis > MFI_OB) and cur_mfi < MFI_OB and red:
            sh_start = max(0, i - SHORT_SWING_WINDOW)
            recent_high = highs[sh_start:i+1].max()   # include current candle
            dist = (recent_high - entry) / entry * 100
            if dist <= SHORT_ZONE_PCT:
                sigs.append({"idx":i,"ts":ts,"direction":"SHORT","entry":entry,
                             "atr":cur_atr,"mfi":round(cur_mfi,1),"regime":regime,
                             "dist":round(dist,2)})

    return sigs


def find_alt_signals(btc_sigs, alt_df):
    sigs = []
    for sig in btc_sigs:
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
        alt_mfi, alt_atr, alt_entry = row["mfi"], row["atr"], row["close"]
        if np.isnan(alt_mfi) or np.isnan(alt_atr) or alt_atr == 0:
            continue
        if sig["direction"] == "LONG"  and alt_mfi > 60: continue
        if sig["direction"] == "SHORT" and alt_mfi < 40: continue
        sigs.append({"idx":i,"ts":alt_df.index[i],"direction":sig["direction"],
                     "entry":alt_entry,"atr":alt_atr,"mfi":round(alt_mfi,1),
                     "regime":sig["regime"]})
    return sigs


def simulate(df, sig, sl_mult, tp1_pct, tp2_pct):
    i, entry, atr, d = sig["idx"], sig["entry"], sig["atr"], sig["direction"]
    sl_dist = atr * sl_mult
    sl  = entry - sl_dist if d=="LONG" else entry + sl_dist
    tp1 = entry*(1+tp1_pct/100) if d=="LONG" else entry*(1-tp1_pct/100)
    tp2 = entry*(1+tp2_pct/100) if d=="LONG" else entry*(1-tp2_pct/100)

    partial_done = False
    partial_pnl  = 0.0
    final_pnl    = 0.0
    outcome      = "TIMEOUT"
    tp1_hit = tp2_hit = False
    max_fav  = 0.0

    H = df["high"].values
    L = df["low"].values
    C = df["close"].values

    for j in range(i+1, min(i+1+MAX_HOLD, len(df))):
        h, l = H[j], L[j]
        if d == "LONG":
            max_fav = max(max_fav, (h-entry)/entry*100)
            if l <= sl:
                rem = (sl-entry)/entry*100
                final_pnl = partial_pnl + rem*(0.5 if partial_done else 1.0)
                outcome = "BE+" if final_pnl > 0.05 else "LOSS"; break
            if not partial_done and h >= tp1:
                partial_pnl = (tp1-entry)/entry*100*PARTIAL_PCT
                partial_done = True; tp1_hit = True
            if h >= tp2:
                final_pnl = partial_pnl + (tp2-entry)/entry*100*(0.5 if partial_done else 1.0)
                outcome = "WIN"; tp2_hit = True; break
        else:
            max_fav = max(max_fav, (entry-l)/entry*100)
            if h >= sl:
                rem = (entry-sl)/entry*100
                final_pnl = partial_pnl + rem*(0.5 if partial_done else 1.0)
                outcome = "BE+" if final_pnl > 0.05 else "LOSS"; break
            if not partial_done and l <= tp1:
                partial_pnl = (entry-tp1)/entry*100*PARTIAL_PCT
                partial_done = True; tp1_hit = True
            if l <= tp2:
                final_pnl = partial_pnl + (entry-tp2)/entry*100*(0.5 if partial_done else 1.0)
                outcome = "WIN"; tp2_hit = True; break
    else:
        last = C[min(i+MAX_HOLD, len(df)-1)]
        rem = ((last-entry)/entry*100 if d=="LONG" else (entry-last)/entry*100)
        final_pnl = partial_pnl + rem*(0.5 if partial_done else 1.0)

    return {"outcome":outcome,"final_pnl":round(final_pnl,3),
            "tp1_hit":tp1_hit,"tp2_hit":tp2_hit,
            "max_fav":round(max_fav,2),"sl_pct":round(sl_dist/entry*100,2)}


def analyse(df, sigs, cfg, label):
    records = []
    for sig in sigs:
        r = simulate(df, sig, cfg["sl"], cfg["tp1"], cfg["tp2"])
        r.update(sig)
        records.append(r)

    if not records:
        return

    rdf = pd.DataFrame(records)
    print(f"\n  {'═'*66}")
    print(f"  {label}  |  SL={cfg['sl']}×ATR  TP1={cfg['tp1']}% TP2={cfg['tp2']}%")
    print(f"  {'═'*66}")

    for regime, direction in [("BULL","LONG"), ("BEAR","SHORT"), ("ALL","BOTH")]:
        if regime == "ALL":
            sub = rdf
        else:
            sub = rdf[rdf["regime"]==regime]
        if len(sub) < 3:
            continue

        if direction != "BOTH":
            dir_sub = sub[sub["direction"]==direction]
        else:
            dir_sub = sub

        n      = len(sub)
        wins   = sub[sub["outcome"]=="WIN"]
        losses = sub[sub["outcome"]=="LOSS"]
        be     = sub[sub["outcome"]=="BE+"]
        tout   = sub[sub["outcome"]=="TIMEOUT"]

        longs  = sub[sub["direction"]=="LONG"]
        shorts = sub[sub["direction"]=="SHORT"]
        l_wr   = (longs["outcome"]=="WIN").sum()/len(longs)*100 if len(longs) else 0
        s_wr   = (shorts["outcome"]=="WIN").sum()/len(shorts)*100 if len(shorts) else 0
        wr     = len(wins)/n*100
        net    = sub["final_pnl"].sum()
        pf     = (wins["final_pnl"].sum()/abs(losses["final_pnl"].sum())
                  if len(losses) and losses["final_pnl"].sum()!=0 else 99.0)
        avg_w  = wins["final_pnl"].mean() if len(wins) else 0
        avg_l  = losses["final_pnl"].mean() if len(losses) else 0
        mfe    = sub["max_fav"].mean()
        spm    = n/(LOOKBACK_DAYS/30)

        # Primary direction WR for this regime
        pri_wr = l_wr if direction=="LONG" else (s_wr if direction=="SHORT" else wr)
        pri_n  = len(longs) if direction=="LONG" else (len(shorts) if direction=="SHORT" else n)

        flag = ""
        if pri_wr >= 55: flag = "  ★ STRONG"
        elif pri_wr >= 40: flag = "  ✓ VIABLE"

        print(f"\n  [{regime}]{flag}")
        print(f"    All trades  : {n} ({spm:.1f}/mo) | LONG:{len(longs)} SHORT:{len(shorts)}")
        print(f"    WR (all)    : {wr:.1f}%  |  Long WR: {l_wr:.1f}%  |  Short WR: {s_wr:.1f}%")
        print(f"    Primary dir : {direction} ({pri_n} trades) — WR {pri_wr:.1f}%")
        print(f"    PF: {min(pf,99):.2f}  |  Net: {net:+.1f}%  |  AvgW: +{avg_w:.2f}%  AvgL: {avg_l:.2f}%")
        print(f"    TP1 hit: {sub['tp1_hit'].sum()/n*100:.1f}%  |  TP2 hit: {sub['tp2_hit'].sum()/n*100:.1f}%  |  MFE: {mfe:.1f}%")
        print(f"    WIN={len(wins)}  LOSS={len(losses)}  BE+={len(be)}  TIMEOUT={len(tout)}")

    return rdf


def main():
    print("=" * 68)
    print("  Liq Zone MFI Reversal v4b — Regime-Aware (Zone Fix)")
    print(f"  LONG zone: 40c swing low ≤{LONG_ZONE_PCT}%  |  SHORT zone: 10c bounce high ≤{SHORT_ZONE_PCT}%")
    print(f"  BULL ends 2025-10-05  |  {LOOKBACK_DAYS}d history")
    print("=" * 68)

    exchange = ccxt.binance({"options": {"defaultType": "future"}})

    print("\n  Fetching BTC...", flush=True)
    btc_df = fetch_candles(exchange, BTC_SYMBOL, LOOKBACK_DAYS)
    btc_df["mfi"] = calc_mfi(btc_df)
    btc_df["atr"] = calc_atr(btc_df)

    btc_sigs = find_btc_signals(btc_df)
    bull_sigs = [s for s in btc_sigs if s["regime"]=="BULL"]
    bear_sigs = [s for s in btc_sigs if s["regime"]=="BEAR"]
    print(f"  BTC signals: {len(btc_sigs)} total | BULL:{len(bull_sigs)} (L:{sum(1 for s in bull_sigs if s['direction']=='LONG')} S:{sum(1 for s in bull_sigs if s['direction']=='SHORT')}) | BEAR:{len(bear_sigs)} (L:{sum(1 for s in bear_sigs if s['direction']=='LONG')} S:{sum(1 for s in bear_sigs if s['direction']=='SHORT')})")

    all_dfs = {"BTC": btc_df}
    analyse(btc_df, btc_sigs, CONFIGS["BTC"], "BTC (own trades, zone-filtered)")

    for coin, symbol in ALT_SYMBOLS.items():
        print(f"\n  Fetching {coin}...", flush=True)
        try:
            df = fetch_candles(exchange, symbol, LOOKBACK_DAYS)
            df["mfi"] = calc_mfi(df)
            df["atr"] = calc_atr(df)
            all_dfs[coin] = df

            alt_sigs = find_alt_signals(btc_sigs, df)
            bull_a = [s for s in alt_sigs if s["regime"]=="BULL"]
            bear_a = [s for s in alt_sigs if s["regime"]=="BEAR"]
            print(f"    Signals: {len(alt_sigs)} | BULL:{len(bull_a)} BEAR:{len(bear_a)}")

            analyse(df, alt_sigs, CONFIGS[coin], f"{coin} (follows BTC signal)")
        except Exception as e:
            print(f"  ERROR {coin}: {e}")
        time.sleep(0.5)

    # ── Final Master Table ─────────────────────────────────────────────────────
    print("\n\n" + "="*68)
    print("  MASTER SUMMARY — Implementation Guide")
    print("="*68)
    print("\n  BEAR MARKET NOW (SHORT strategy — until ~Oct 2026):")
    print(f"  {'Coin':<6} {'SL':<10} {'TP1':>5} {'TP2':>5}   Use SHORT when BTC MFI >80 reversal")
    print(f"  {'─'*6} {'─'*10} {'─'*5} {'─'*5}")
    for coin in ["BTC","ETH","XRP","AVAX","SUI"]:
        c = CONFIGS[coin]
        print(f"  {coin:<6} {c['sl']}×ATR    {c['tp1']:>4.0f}% {c['tp2']:>4.0f}%")

    print("\n  BULL MARKET NEXT (~Oct 2026) (LONG strategy):")
    print(f"  {'Coin':<6} {'SL':<10} {'TP1':>5} {'TP2':>5}   Use LONG when BTC MFI <20 reversal")
    print(f"  {'─'*6} {'─'*10} {'─'*5} {'─'*5}")
    for coin in ["BTC","ETH","XRP","AVAX","SUI"]:
        c = CONFIGS[coin]
        print(f"  {coin:<6} {c['sl']}×ATR    {c['tp1']:>4.0f}% {c['tp2']:>4.0f}%")

    print("="*68)


if __name__ == "__main__":
    main()
