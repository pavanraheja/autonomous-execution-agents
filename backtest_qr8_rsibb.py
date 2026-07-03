"""
QR-8 v0.1 — RSI(14)<30 + lower-Bollinger bounce, uptrend-gated LONG
════════════════════════════════════════════════════════════════════
Spec (registered 2026-07-03):
- 1H bars, 20-coin universe, 365d
- Entry: RSI(14) < 30 AND bar low touches lower BB(20, 2σ) AND coin in
  daily uptrend (prev DAILY close > 20-day SMA, shifted — no lookahead).
  LONG only (bounce + uptrend gate). Enter next bar open.
- TP: BB midline (20-SMA of 1H closes) touch, filled at midline value
- SL: 1.5 × ATR(14) below entry
- Time stop: 100 bars
- Baseline variant (no uptrend gate) run alongside per protocol.
Fees 0.10% RT, $2,500 notional. Gates: n>=30 gated, PF>=1.3.
Prior: unfiltered MFI-long analogue was PF 0.95; filtered 1.61 but starved.
"""

import ccxt
import numpy as np
import pandas as pd
import time

EX = ccxt.binanceusdm({"enableRateLimit": True, "timeout": 30000})
DAYS = 365
NOTIONAL = 2_500
FEE_RT = 0.10
COINS = ["BTC", "ETH", "XRP", "ADA", "DOGE", "AVAX", "LINK", "DOT", "SUI",
         "BNB", "NEAR", "APT", "ARB", "OP", "INJ", "WLD", "LTC", "SOL",
         "TON", "1000PEPE"]


def fetch_1h(coin, days):
    since = EX.milliseconds() - (days + 40) * 86_400_000
    now = EX.milliseconds()
    rows = []
    while since < now - 3_600_000:
        b = EX.fetch_ohlcv(f"{coin}/USDT:USDT", "1h", since=since, limit=1000)
        if not b:
            break
        rows += b
        since = b[-1][0] + 1
        time.sleep(0.1)
    df = pd.DataFrame(rows, columns=["ts", "o", "h", "l", "c", "v"])
    df = df.drop_duplicates("ts").reset_index(drop=True)
    df["dt"] = pd.to_datetime(df.ts, unit="ms", utc=True)
    return df


def rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def prep(df):
    df["rsi"] = rsi(df.c)
    mid = df.c.rolling(20).mean()
    sd = df.c.rolling(20).std()
    df["bb_lo"] = mid - 2 * sd
    df["bb_mid"] = mid
    tr = pd.concat([df.h - df.l, (df.h - df.c.shift()).abs(),
                    (df.l - df.c.shift()).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    # daily uptrend gate: previous UTC day's close > 20-day SMA (as of that close)
    day = df.dt.dt.floor("D").dt.tz_localize(None)   # tz-naive for index match
    daily_close = df.groupby(day.values).c.last()
    sma20 = daily_close.rolling(20).mean()
    up = (daily_close > sma20)
    up_map = up.shift(1)                      # known at day open
    df["uptrend"] = day.map(up_map).fillna(False).values
    assert df["uptrend"].sum() > 0 or len(df) < 500, "uptrend gate mapped to all-False"
    return df


def run(df, coin, gated, trades):
    o, h, l, c = df.o.values, df.h.values, df.l.values, df.c.values
    r, lo_b, mid, a = df.rsi.values, df.bb_lo.values, df.bb_mid.values, df.atr.values
    up = df.uptrend.values
    dt = df.dt.values
    tag = "QR8_gated" if gated else "QR8_raw"
    n = len(df)
    i = 30
    while i < n - 1:
        if r[i] < 30 and l[i] <= lo_b[i] and (up[i] or not gated):
            e_px = o[i + 1]
            sl = e_px - 1.5 * a[i]
            j = i + 1
            while j < n:
                x_px = why = None
                if l[j] <= sl:
                    x_px, why = sl, "SL"
                elif h[j] >= mid[j]:
                    x_px, why = mid[j], "TP"
                elif j - i >= 100:
                    x_px, why = c[j], "TIME"
                if x_px is not None:
                    net = (x_px - e_px) / e_px * 100 - FEE_RT
                    trades.append(dict(coin=coin, method=tag, entry_dt=str(dt[i + 1]),
                                       exit_dt=str(dt[j]), reason=why,
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
    for k, coin in enumerate(COINS):
        try:
            df = prep(fetch_1h(coin, DAYS))
            run(df, coin, True, trades)
            run(df, coin, False, trades)
            print(f"[{k+1}/{len(COINS)}] {coin} ok", flush=True)
        except Exception as e:
            print(f"[{k+1}/{len(COINS)}] {coin} FAILED {e}", flush=True)
    t = pd.DataFrame(trades)
    months = DAYS / 30.44
    print("\n── QR-8 results (20 coins, 365d 1H, LONG bounce) ──")
    for m, g in t.groupby("method"):
        g = g.sort_values("entry_dt")
        h1, h2 = g.iloc[: len(g) // 2], g.iloc[len(g) // 2:]
        print(f"{m}: n={len(g)} WR={100*(g.pnl_pct>0).mean():.1f}% PF={pf(g)} "
              f"(H1 {pf(h1)} / H2 {pf(h2)}) exp={g.pnl_pct.mean():+.3f}% "
              f"Mo${g.pnl_usd.sum()/months:+.0f} trades/mo={len(g)/months:.1f}")
    print("\nTop/bottom coins (gated, n>=3):")
    gg = t[t.method == "QR8_gated"]
    rows = [(cn, len(x), pf(x), round(x.pnl_usd.sum())) for cn, x in gg.groupby("coin")
            if len(x) >= 3]
    for cn, nn, p, usd in sorted(rows, key=lambda r: -(r[2] if r[2] != float("inf") else 99)):
        print(f"   {cn}: n={nn} PF={p} ${usd:+d}")
    t.to_csv("qr8_rsibb_trades.csv", index=False)
    print("Saved: qr8_rsibb_trades.csv")


if __name__ == "__main__":
    main()
