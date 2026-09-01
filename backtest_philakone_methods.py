"""
Philakone Methods Backtest — 4 codified strategies from @PhilakoneCrypto's teaching
════════════════════════════════════════════════════════════════════════════════
M1 — 55 EMA Pullback Swing (his Lesson 12 "Advanced 55 EMA Strategy"), 4H
M2 — Stoch RSI cross + HTF trend filter (his classic timing stack), 1H entry / 4H trend
M3 — 5/10 EMA cross swing (his beginner swing lesson), 4H, raw + trend-filtered
M4 — Golden Pocket fib ladder (his EW wave-3 proxy: 0.5-0.618 retrace, ladder,
     stop 0.786, TP1 at swing, TP2 at 1.618 ext), 4H

Execution conventions (match repo standards):
- Signals on CLOSED candles only, entry at next bar open (no lookahead)
- Same-bar SL+TP conflict → SL wins (conservative)
- Fees 0.05% per side (0.10% round trip), notional $2,500 per trade
- One open trade per coin per method
- Period: last 365 days. Stability check: PF split into half-1 / half-2.
Gates: n>=30 pooled, PF>=1.3.
"""

import ccxt
import pandas as pd
import numpy as np
import time
import json
from datetime import datetime, timezone

EX = ccxt.binanceusdm({"enableRateLimit": True, "timeout": 30000})

LOOKBACK_DAYS = 365
NOTIONAL      = 2_500
FEE_RT        = 0.10   # % round trip

# Coins Philakone actually traded/taught on (2017-18 Bitfinex era, still listed)
ERA_COINS = ["BTC", "ETH", "LTC", "XRP", "EOS", "ADA", "TRX", "XLM", "ETC",
             "IOTA", "ZEC", "NEO"]
# Modern liquid universe (current repo watchlists; SOL included for research
# despite strategy blacklist — flagged in output)
MODERN_COINS = ["SOL", "DOGE", "AVAX", "LINK", "DOT", "SUI", "BNB", "NEAR",
                "APT", "ARB", "OP", "INJ", "WLD", "1000PEPE", "WIF", "TON"]
ALL_COINS = ERA_COINS + MODERN_COINS

MS = {"1h": 3_600_000, "4h": 14_400_000}


def fetch(coin, tf, days, warmup_bars):
    sym = f"{coin}/USDT:USDT"
    bars_needed = int(days * 86_400_000 / MS[tf]) + warmup_bars
    since = EX.milliseconds() - bars_needed * MS[tf]
    rows = []
    while True:
        batch = EX.fetch_ohlcv(sym, tf, since=since, limit=1500)
        if not batch:
            break
        rows += batch
        since = batch[-1][0] + 1
        if len(batch) < 1500:
            break
        time.sleep(0.12)
    if len(rows) < warmup_bars + 100:
        return None
    df = pd.DataFrame(rows, columns=["ts", "o", "h", "l", "c", "v"])
    df = df.drop_duplicates("ts").reset_index(drop=True)
    df["dt"] = pd.to_datetime(df.ts, unit="ms", utc=True)
    return df


# ── Indicators ────────────────────────────────────────────────────────────────
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(close, n=14):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def stoch_rsi(close, rsi_n=14, stoch_n=14, k_n=3, d_n=3):
    r = rsi(close, rsi_n)
    lo = r.rolling(stoch_n).min()
    hi = r.rolling(stoch_n).max()
    stoch = 100 * (r - lo) / (hi - lo).replace(0, np.nan)
    k = stoch.rolling(k_n).mean()
    d = k.rolling(d_n).mean()
    return k, d


def atr(df, n=14):
    tr = pd.concat([df.h - df.l,
                    (df.h - df.c.shift()).abs(),
                    (df.l - df.c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


# ── Trade recording ───────────────────────────────────────────────────────────
def rec(trades, coin, method, side, e_dt, e_px, x_dt, x_px, reason, extra_pct=0.0):
    raw = (x_px - e_px) / e_px * 100 * (1 if side == "L" else -1) + extra_pct
    net = raw - FEE_RT
    trades.append(dict(coin=coin, method=method, side=side,
                       entry_dt=str(e_dt), entry=e_px,
                       exit_dt=str(x_dt), exit=x_px,
                       reason=reason, pnl_pct=round(net, 4),
                       pnl_usd=round(net / 100 * NOTIONAL, 2)))


# ── M1: 55 EMA pullback swing (4H) ───────────────────────────────────────────
# Regime: close vs EMA55, EMA55 slope over 5 bars, >=8 of last 10 closes on
# regime side. Trigger: bar touches EMA55 (wick through) but CLOSES back on
# regime side. Entry next open. SL beyond 3-bar extreme -0.25 ATR (risk 0.3-5%).
# TP = 2R. Time stop 30 bars (5 days).
def run_m1(df, coin, trades):
    e55, a = ema(df.c, 55), atr(df)
    slope_up = e55 > e55.shift(5)
    above = (df.c > e55).rolling(10).sum()
    i, n = 60, len(df)
    while i < n - 1:
        row_ok = False
        if slope_up[i] and df.c[i] > e55[i] and df.l[i] <= e55[i] and above[i - 1] >= 8:
            side, sl = "L", min(df.l[i - 2:i + 1].min(), e55[i]) - 0.25 * a[i]
            row_ok = True
        elif (not slope_up[i]) and df.c[i] < e55[i] and df.h[i] >= e55[i] \
                and (10 - above[i - 1]) >= 8:
            side, sl = "S", max(df.h[i - 2:i + 1].max(), e55[i]) + 0.25 * a[i]
            row_ok = True
        if row_ok:
            e_px = df.o[i + 1]
            risk = (e_px - sl) if side == "L" else (sl - e_px)
            if 0.003 * e_px <= risk <= 0.05 * e_px:
                tp = e_px + 2 * risk if side == "L" else e_px - 2 * risk
                j = i + 1
                while j < n:
                    hit_sl = df.l[j] <= sl if side == "L" else df.h[j] >= sl
                    hit_tp = df.h[j] >= tp if side == "L" else df.l[j] <= tp
                    if hit_sl:
                        rec(trades, coin, "M1_55EMA", side, df.dt[i + 1], e_px,
                            df.dt[j], sl, "SL")
                        break
                    if hit_tp:
                        rec(trades, coin, "M1_55EMA", side, df.dt[i + 1], e_px,
                            df.dt[j], tp, "TP")
                        break
                    if j - i >= 30:
                        rec(trades, coin, "M1_55EMA", side, df.dt[i + 1], e_px,
                            df.dt[j], df.c[j], "TIME")
                        break
                    j += 1
                i = j
        i += 1


# ── M2: Stoch RSI cross + 4H trend (1H entry) ────────────────────────────────
# Trend from CLOSED 4H bar (shifted, no lookahead): close vs EMA55.
# LONG: trend up + 1H %K crosses above %D with %K<20. Entry next 1H open.
# SL 1.5*ATR(1H). Exit: %K crosses below %D with %K>80, or SL, or 48-bar time.
def run_m2(df1h, df4h, coin, trades):
    e55_4h = ema(df4h.c, 55)
    trend4 = pd.Series(np.where(df4h.c > e55_4h, 1, -1), index=df4h.ts)
    # trend known only after 4H close → applies from bar close time onward
    t_map = pd.Series(trend4.values, index=df4h.ts.values + MS["4h"])
    idx = np.searchsorted(t_map.index.values, df1h.ts.values, side="right") - 1
    trend = np.where(idx >= 0, t_map.values[np.maximum(idx, 0)], 0)
    k, d = stoch_rsi(df1h.c)
    a = atr(df1h)
    x_up = (k > d) & (k.shift() <= d.shift())
    x_dn = (k < d) & (k.shift() >= d.shift())
    i, n = 60, len(df1h)
    while i < n - 1:
        sig = None
        if trend[i] == 1 and x_up[i] and k[i] < 20:
            sig = "L"
        elif trend[i] == -1 and x_dn[i] and k[i] > 80:
            sig = "S"
        if sig:
            e_px = df1h.o[i + 1]
            sl = e_px - 1.5 * a[i] if sig == "L" else e_px + 1.5 * a[i]
            j = i + 1
            while j < n:
                hit_sl = df1h.l[j] <= sl if sig == "L" else df1h.h[j] >= sl
                osc_out = (x_dn[j] and k[j] > 80) if sig == "L" else (x_up[j] and k[j] < 20)
                if hit_sl:
                    rec(trades, coin, "M2_StochRSI", sig, df1h.dt[i + 1], e_px,
                        df1h.dt[j], sl, "SL")
                    break
                if osc_out:
                    rec(trades, coin, "M2_StochRSI", sig, df1h.dt[i + 1], e_px,
                        df1h.dt[j], df1h.c[j], "OSC")
                    break
                if j - i >= 48:
                    rec(trades, coin, "M2_StochRSI", sig, df1h.dt[i + 1], e_px,
                        df1h.dt[j], df1h.c[j], "TIME")
                    break
                j += 1
            i = j
        i += 1


# ── M3: 5/10 EMA cross swing (4H) — raw + EMA55 trend-filtered ───────────────
# Cross up → LONG next open; exit on opposite cross or SL 2*ATR.
def run_m3(df, coin, trades, filtered):
    e5, e10, e55, a = ema(df.c, 5), ema(df.c, 10), ema(df.c, 55), atr(df)
    x_up = (e5 > e10) & (e5.shift() <= e10.shift())
    x_dn = (e5 < e10) & (e5.shift() >= e10.shift())
    tag = "M3_EMAxF" if filtered else "M3_EMAx"
    i, n = 60, len(df)
    while i < n - 1:
        sig = None
        if x_up[i] and (not filtered or df.c[i] > e55[i]):
            sig = "L"
        elif x_dn[i] and (not filtered or df.c[i] < e55[i]):
            sig = "S"
        if sig:
            e_px = df.o[i + 1]
            sl = e_px - 2 * a[i] if sig == "L" else e_px + 2 * a[i]
            j = i + 1
            while j < n:
                hit_sl = df.l[j] <= sl if sig == "L" else df.h[j] >= sl
                opp = x_dn[j] if sig == "L" else x_up[j]
                if hit_sl:
                    rec(trades, coin, tag, sig, df.dt[i + 1], e_px, df.dt[j], sl, "SL")
                    break
                if opp:
                    rec(trades, coin, tag, sig, df.dt[i + 1], e_px, df.dt[j],
                        df.c[j], "FLIP")
                    j -= 1  # re-evaluate the cross bar → stop-and-reverse
                    break
                j += 1
            i = j
        i += 1


# ── M4: Golden Pocket fib ladder (4H) ────────────────────────────────────────
# Pivots: 5-bar fractals, confirmed 5 bars later. Leg = pivot low → pivot high
# (>=8% magnitude). Entry ladder: half at 0.5, half at 0.618 retrace.
# SL: 0.786 level. TP1 = swing extreme (close half, SL→breakeven),
# TP2 = 1.618 extension. Invalidate leg if close beyond pivot start.
def run_m4(df, coin, trades):
    n = len(df)
    ph = df.h.rolling(11, center=True).max() == df.h
    pl = df.l.rolling(11, center=True).min() == df.l
    piv_h = [(i, df.h[i]) for i in range(5, n - 5) if ph[i]]
    piv_l = [(i, df.l[i]) for i in range(5, n - 5) if pl[i]]

    def sim(leg_dir, p_start, p_end, i_conf):
        # leg_dir "up": start=pivot low px, end=pivot high px → LONG the retrace
        rng = abs(p_end - p_start)
        if rng / p_start < 0.08:
            return None
        if leg_dir == "up":
            f50, f618, f786 = p_end - 0.5 * rng, p_end - 0.618 * rng, p_end - 0.786 * rng
            tp2 = p_end + 0.618 * rng
        else:
            f50, f618, f786 = p_end + 0.5 * rng, p_end + 0.618 * rng, p_end + 0.786 * rng
            tp2 = p_end - 0.618 * rng
        filled, avg_e, e_dt = 0, 0.0, None
        j = i_conf
        while j < n and j < i_conf + 90:
            lo_hit = df.l[j] <= f50 if leg_dir == "up" else df.h[j] >= f50
            lo_hit2 = df.l[j] <= f618 if leg_dir == "up" else df.h[j] >= f618
            inval = df.c[j] < p_start if leg_dir == "up" else df.c[j] > p_start
            if filled == 0 and lo_hit:
                avg_e, filled, e_dt = f50, 1, df.dt[j]
            if filled == 1 and lo_hit2:
                avg_e, filled = (f50 + f618) / 2, 2
            if filled:
                stop_hit = df.c[j] < f786 if leg_dir == "up" else df.c[j] > f786
                if stop_hit:  # close-based stop at 0.786
                    return (coin, "up" if leg_dir == "up" else "dn", avg_e, e_dt,
                            df.dt[j], df.c[j], "SL", 0.0)
                tp1_hit = df.h[j] >= p_end if leg_dir == "up" else df.l[j] <= p_end
                if tp1_hit:
                    # half off at TP1, runner (half size) to TP2 with BE stop.
                    # Exit px returned = avg_e so rec()'s raw term is 0; the
                    # full trade PnL (half at TP1 + half runner) rides extra_pct.
                    sgn = 1 if leg_dir == "up" else -1
                    half1 = (p_end - avg_e) / avg_e * 100 * sgn / 2

                    def _half_runner(px):
                        return (px - avg_e) / avg_e * 100 * sgn / 2
                    jj = j
                    while jj < n and jj < j + 90:
                        tp2_hit = df.h[jj] >= tp2 if leg_dir == "up" else df.l[jj] <= tp2
                        be_hit = df.l[jj] <= avg_e if leg_dir == "up" else df.h[jj] >= avg_e
                        if tp2_hit:
                            return (coin, leg_dir, avg_e, e_dt, df.dt[jj], avg_e,
                                    "TP2", half1 + _half_runner(tp2))
                        if jj > j and be_hit:
                            return (coin, leg_dir, avg_e, e_dt, df.dt[jj], avg_e,
                                    "BE", half1)
                        jj += 1
                    jj = min(jj, n - 1)
                    return (coin, leg_dir, avg_e, e_dt, df.dt[jj], avg_e,
                            "TIME", half1 + _half_runner(df.c[jj]))
            if inval and not filled:
                return None
            if inval and filled:
                return (coin, leg_dir, avg_e, e_dt, df.dt[j], df.c[j], "INVAL", 0.0)
            j += 1
        return None

    # walk pivot sequence: for each pivot high, find preceding pivot low (up leg)
    used = set()
    for (ih, pxh) in piv_h:
        lows_before = [(il, pxl) for (il, pxl) in piv_l if il < ih and ih - il <= 60]
        if not lows_before or ih in used:
            continue
        il, pxl = lows_before[-1]
        r = sim("up", pxl, pxh, ih + 5)
        if r:
            used.add(ih)
            c_, d_, ae, edt, xdt, xpx, why, extra = r
            if leg_pnl_valid(ae):
                rec(trades, coin, "M4_GoldenPocket",
                    "L" if d_ == "up" else "S", edt, ae, xdt, xpx, why,
                    extra_pct=extra)
    for (il, pxl) in piv_l:
        highs_before = [(ih, pxh) for (ih, pxh) in piv_h if ih < il and il - ih <= 60]
        if not highs_before or il in used:
            continue
        ih, pxh = highs_before[-1]
        r = sim("dn", pxh, pxl, il + 5)
        if r:
            used.add(il)
            c_, d_, ae, edt, xdt, xpx, why, extra = r
            if leg_pnl_valid(ae):
                rec(trades, coin, "M4_GoldenPocket",
                    "L" if d_ == "up" else "S", edt, ae, xdt, xpx, why,
                    extra_pct=extra)


def leg_pnl_valid(px):
    return px and px > 0


# ── Reporting ─────────────────────────────────────────────────────────────────
def summarize(trades_df, label, months):
    out = []
    for m, g in trades_df.groupby("method"):
        wins = g[g.pnl_pct > 0]
        losses = g[g.pnl_pct <= 0]
        pf = wins.pnl_pct.sum() / abs(losses.pnl_pct.sum()) if len(losses) and losses.pnl_pct.sum() != 0 else float("inf")
        mid = g.entry_dt.sort_values().iloc[len(g) // 2] if len(g) else None
        g_sorted = g.sort_values("entry_dt")
        h1 = g_sorted.iloc[: len(g) // 2]
        h2 = g_sorted.iloc[len(g) // 2:]
        def _pf(x):
            w = x[x.pnl_pct > 0].pnl_pct.sum()
            l = abs(x[x.pnl_pct <= 0].pnl_pct.sum())
            return round(w / l, 2) if l else float("inf")
        out.append(dict(method=m, n=len(g), wr=round(100 * len(wins) / len(g), 1),
                        pf=round(pf, 2), exp_pct=round(g.pnl_pct.mean(), 3),
                        mo_usd=round(g.pnl_usd.sum() / months, 0),
                        pf_h1=_pf(h1), pf_h2=_pf(h2)))
    return pd.DataFrame(out)


def main():
    all_trades = []
    ok_coins, failed = [], []
    for ci, coin in enumerate(ALL_COINS):
        try:
            d4 = fetch(coin, "4h", LOOKBACK_DAYS, 200)
            d1 = fetch(coin, "1h", LOOKBACK_DAYS, 200)
            if d4 is None or d1 is None:
                failed.append(coin)
                continue
            t = []
            run_m1(d4, coin, t)
            run_m2(d1, d4, coin, t)
            run_m3(d4, coin, t, filtered=False)
            run_m3(d4, coin, t, filtered=True)
            run_m4(d4, coin, t)
            all_trades += t
            ok_coins.append(coin)
            print(f"[{ci+1}/{len(ALL_COINS)}] {coin}: {len(t)} trades", flush=True)
        except Exception as e:
            failed.append(coin)
            print(f"[{ci+1}/{len(ALL_COINS)}] {coin}: FAILED {e}", flush=True)

    tdf = pd.DataFrame(all_trades)
    months = LOOKBACK_DAYS / 30.44
    print("\n" + "=" * 100)
    print(f"PHILAKONE METHODS — {LOOKBACK_DAYS}d, {len(ok_coins)} coins, "
          f"end {datetime.now(timezone.utc).date()}")
    print(f"Failed/skipped coins: {failed}")
    print("=" * 100)

    print("\n── POOLED (all coins) ──")
    print(summarize(tdf, "pooled", months).to_string(index=False))

    for grp_name, grp in [("ERA (Philakone's coins)", ERA_COINS),
                          ("MODERN (our universe)", MODERN_COINS)]:
        sub = tdf[tdf.coin.isin(grp)]
        if len(sub):
            print(f"\n── {grp_name} ──")
            print(summarize(sub, grp_name, months).to_string(index=False))

    print("\n── Per-coin PF by method (n>=8 only) ──")
    rows = []
    for (m, c), g in tdf.groupby(["method", "coin"]):
        if len(g) < 8:
            continue
        w = g[g.pnl_pct > 0].pnl_pct.sum()
        l = abs(g[g.pnl_pct <= 0].pnl_pct.sum())
        rows.append(dict(method=m, coin=c, n=len(g),
                         wr=round(100 * (g.pnl_pct > 0).mean(), 1),
                         pf=round(w / l, 2) if l else float("inf"),
                         usd=round(g.pnl_usd.sum(), 0)))
    pc = pd.DataFrame(rows).sort_values(["method", "pf"], ascending=[True, False])
    print(pc.to_string(index=False))

    print("\n── By side ──")
    for (m, s), g in tdf.groupby(["method", "side"]):
        w = g[g.pnl_pct > 0].pnl_pct.sum()
        l = abs(g[g.pnl_pct <= 0].pnl_pct.sum())
        pf = round(w / l, 2) if l else float("inf")
        print(f"{m:18s} {s}  n={len(g):4d}  WR={100*(g.pnl_pct>0).mean():5.1f}%  PF={pf}")

    tdf.to_csv("philakone_methods_trades.csv", index=False)
    pc.to_csv("philakone_methods_percoin.csv", index=False)
    print("\nSaved: philakone_methods_trades.csv / philakone_methods_percoin.csv")


if __name__ == "__main__":
    main()
