"""
QR-7 v0.2 — Session-VWAP reversion backtest
════════════════════════════════════════════
Spec (registered 2026-07-03):
- 5m bars, BTC + ETH, 365d. Session = UTC day; VWAP + volume-weighted σ bands
  reset at 00:00 UTC.
- LONG entry: bar LOW touches (VWAP - 2σ) AND bar CLOSES back above it
  (reclaim). Enter next bar open. Mirror SHORT at (VWAP + 2σ).
- TP: touch of session VWAP (fill at that bar's VWAP value).
- SL: entry-bar extreme ± 1σ (σ at signal time).
- Force-flat at session end (VWAP resets). No signals in first/last hour
  of session. One position per symbol.
- Variant B (trend-day filter): skip entries where price is beyond the 5m
  200-EMA in the deviation direction (long only if close >= EMA200, short
  only if close <= EMA200).
- Fees 0.05% taker per side (0.10% RT). Notional $2,500.
Gates: n>=100 pooled, PF>=1.3 in BOTH halves, after fees.
"""

import ccxt
import numpy as np
import pandas as pd
import time

EX = ccxt.binanceusdm({"enableRateLimit": True, "timeout": 30000})

DAYS     = 365
NOTIONAL = 2_500
FEE_RT   = 0.10
BAR_MS   = 300_000
COINS    = ["BTC", "ETH"]


def fetch_5m(coin, days):
    since = EX.milliseconds() - (days + 2) * 86_400_000
    now = EX.milliseconds()
    rows = []
    while since < now - BAR_MS:
        b = EX.fetch_ohlcv(f"{coin}/USDT:USDT", "5m", since=since, limit=1000)
        if not b:
            break
        rows += b
        since = b[-1][0] + 1
        time.sleep(0.1)
    df = pd.DataFrame(rows, columns=["ts", "o", "h", "l", "c", "v"])
    df = df.drop_duplicates("ts").reset_index(drop=True)
    df["dt"] = pd.to_datetime(df.ts, unit="ms", utc=True)
    print(f"{coin}: {len(df)} bars {df.dt.iloc[0]} → {df.dt.iloc[-1]}", flush=True)
    return df


def add_vwap(df):
    day = df.dt.dt.floor("D")
    typ = (df.h + df.l + df.c) / 3
    pv = typ * df.v
    pv2 = typ * typ * df.v
    cv = df.v.groupby(day).cumsum()
    df["vwap"] = pv.groupby(day).cumsum() / cv
    mean_sq = pv2.groupby(day).cumsum() / cv
    df["sigma"] = np.sqrt((mean_sq - df.vwap ** 2).clip(lower=0))
    df["bar_of_day"] = df.groupby(day.values).cumcount()
    df["bars_in_day"] = df.groupby(day.values)["ts"].transform("size")
    df["ema200"] = df.c.ewm(span=200, adjust=False).mean()
    return df


def run(df, coin, variant, trades):
    o, h, l, c = df.o.values, df.h.values, df.l.values, df.c.values
    vwap, sig, ema = df.vwap.values, df.sigma.values, df.ema200.values
    bod, bid = df.bar_of_day.values, df.bars_in_day.values
    dt = df.dt.values
    n = len(df)
    tag = f"QR7_{variant}"
    i = 12
    while i < n - 1:
        if bod[i] < 12 or bod[i] > bid[i] - 13 or sig[i] <= 0:
            i += 1
            continue
        lo_band = vwap[i] - 2 * sig[i]
        hi_band = vwap[i] + 2 * sig[i]
        side = None
        if l[i] <= lo_band and c[i] > lo_band:
            if variant == "A" or c[i] >= ema[i]:
                side = "L"
        elif h[i] >= hi_band and c[i] < hi_band:
            if variant == "A" or c[i] <= ema[i]:
                side = "S"
        if side and bod[i + 1] > 0:          # entry must be same session
            e_px = o[i + 1]
            sl = l[i] - sig[i] if side == "L" else h[i] + sig[i]
            j = i + 1
            x_px, why = None, None
            while j < n:
                if side == "L" and l[j] <= sl:
                    x_px, why = sl, "SL"
                elif side == "S" and h[j] >= sl:
                    x_px, why = sl, "SL"
                elif side == "L" and h[j] >= vwap[j]:
                    x_px, why = vwap[j], "VWAP"
                elif side == "S" and l[j] <= vwap[j]:
                    x_px, why = vwap[j], "VWAP"
                elif j + 1 >= n or bod[j] >= bid[j] - 1:
                    x_px, why = c[j], "EOD"
                if x_px is not None:
                    raw = (x_px - e_px) / e_px * 100 * (1 if side == "L" else -1)
                    net = raw - FEE_RT
                    trades.append(dict(coin=coin, method=tag, side=side,
                                       entry_dt=str(dt[i + 1]), entry=e_px,
                                       exit_dt=str(dt[j]), exit=x_px, reason=why,
                                       pnl_pct=round(net, 4),
                                       pnl_usd=round(net / 100 * NOTIONAL, 2)))
                    break
                j += 1
            i = j
        i += 1


def pf(g):
    w = g[g.pnl_pct > 0].pnl_pct.sum()
    lo = abs(g[g.pnl_pct <= 0].pnl_pct.sum())
    return round(w / lo, 2) if lo else float("inf")


def main():
    trades = []
    for coin in COINS:
        df = add_vwap(fetch_5m(coin, DAYS))
        for v in ("A", "B"):
            run(df, coin, v, trades)
    t = pd.DataFrame(trades)
    months = DAYS / 30.44
    print("\n── QR-7 results (pooled BTC+ETH, 365d 5m) ──")
    for m, g in t.groupby("method"):
        g = g.sort_values("entry_dt")
        h1, h2 = g.iloc[: len(g) // 2], g.iloc[len(g) // 2:]
        print(f"{m}: n={len(g)} WR={100*(g.pnl_pct>0).mean():.1f}% PF={pf(g)} "
              f"(H1 {pf(h1)} / H2 {pf(h2)}) exp={g.pnl_pct.mean():+.3f}% "
              f"Mo${g.pnl_usd.sum()/months:+.0f}")
        for (cn, sd), gg in g.groupby(["coin", "side"]):
            print(f"   {cn} {sd}: n={len(gg)} WR={100*(gg.pnl_pct>0).mean():.1f}% "
                  f"PF={pf(gg)} ${gg.pnl_usd.sum():+.0f}")
        for why, gg in g.groupby("reason"):
            print(f"   exit {why}: n={len(gg)} avg {gg.pnl_pct.mean():+.3f}%")
    t.to_csv("qr7_vwap_trades.csv", index=False)
    print("Saved: qr7_vwap_trades.csv")


if __name__ == "__main__":
    main()
