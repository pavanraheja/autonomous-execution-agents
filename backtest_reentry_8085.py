"""
8085 HTF MFI — Re-entry-after-stop experiment (Philakone residue: "cut faster,
re-enter faster")
════════════════════════════════════════════════════════════════════════════════
Baseline = live v2.0 config exactly:
  Direction 1D+6H agree | ADX>=18 on 1D | 1H MFI(14) fresh cross OB80/OS20
  SL 1.5% | trail start 1.5%, step 0.5%, gap 0.5% | session 07-22 UTC
  cooldown: MFI back through 50 | $2,500/trade | 1H proxy (conservative counts)

Rule change tested: when a trade dies at the INITIAL stop (never reached trail
start), allow a re-entry within the next N bars — bypassing the through-50
cooldown for that window only — if direction still agrees and:
  R1 "hold":    MFI is still beyond the threshold (signal still active), N=12h, max 1
  R2 "recross": MFI freshly crosses the threshold again,            N=12h, max 1
  R3 "recross": same as R2 but N=24h, max 2 chained re-entries

Period 365d, coins = live 8085 set. Gates: variant beats BASE on PF and Mo$
without degrading WR >5pts; re-entry trades alone must be PF>=1.0 to justify.
"""

import ccxt
import pandas as pd
import numpy as np
import time
from datetime import datetime, timedelta, timezone

exchange = ccxt.binanceusdm({"enableRateLimit": True, "timeout": 30000})

LOOKBACK_DAYS  = 365
EMA_PERIOD     = 20
EMA_SLOPE_BARS = 3
ADX_PERIOD     = 14
ADX_MIN        = 18
MFI_PERIOD     = 14
MFI_OB         = 80
MFI_OS         = 20
SL_PCT         = 1.5
TRAIL_START    = 1.5          # v2.0 (deployed 2026-04-12)
TRAIL_STEP     = 0.5
TRAIL_GAP      = 0.5
SESSION_START  = 7
SESSION_END    = 22
NOTIONAL       = 2_500

COINS = ["DOT", "SUI", "WLD", "INJ", "PERP", "IMX", "VET", "GMX"]

VARIANTS = {
    "BASE": None,
    "R1_hold_12h_x1":    dict(mode="hold",    n_bars=12, max_re=1),
    "R2_recross_12h_x1": dict(mode="recross", n_bars=12, max_re=1),
    "R3_recross_24h_x2": dict(mode="recross", n_bars=24, max_re=2),
}


def calc_ema(s):
    return s.ewm(span=EMA_PERIOD, adjust=False).mean()


def calc_mfi(df):
    tp = (df["high"] + df["low"] + df["close"]) / 3
    rmf = tp * df["volume"]
    pos = rmf.where(tp > tp.shift(1), 0.0)
    neg = rmf.where(tp < tp.shift(1), 0.0)
    ps = pos.rolling(MFI_PERIOD).sum()
    ns = neg.rolling(MFI_PERIOD).sum().replace(0, 1e-10)
    return (100 - (100 / (1 + ps / ns))).fillna(50)


def calc_adx(df):
    hi, lo, cl = df["high"], df["low"], df["close"]
    up, down = hi.diff(), lo.diff().mul(-1)
    pdm = up.where((up > down) & (up > 0), 0.0)
    mdm = down.where((down > up) & (down > 0), 0.0)
    tr = pd.concat([hi - lo, (hi - cl.shift(1)).abs(),
                    (lo - cl.shift(1)).abs()], axis=1).max(axis=1)
    a = 1 / ADX_PERIOD
    atr = tr.ewm(alpha=a, adjust=False).mean().replace(0, 1e-10)
    pdi = 100 * pdm.ewm(alpha=a, adjust=False).mean() / atr
    mdi = 100 * mdm.ewm(alpha=a, adjust=False).mean() / atr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, 1e-10)
    return dx.ewm(alpha=a, adjust=False).mean()


def dir_series(df, adx_min=None):
    ema = calc_ema(df["close"])
    slope = ema > ema.shift(EMA_SLOPE_BARS)
    above = df["close"] > ema
    dirs = pd.Series("FLAT", index=df.index)
    if adx_min is not None:
        ok = calc_adx(df) >= adx_min
        dirs[above & slope & ok] = "LONG"
        dirs[~above & ~slope & ok] = "SHORT"
    else:
        dirs[above & slope] = "LONG"
        dirs[~above & ~slope] = "SHORT"
    return dirs


def fetch_df(symbol, tf, since_ms):
    all_c, since = [], since_ms
    while True:
        try:
            c = exchange.fetch_ohlcv(symbol, tf, since=since, limit=1000)
        except Exception as e:
            print(f"    [{symbol} {tf}] {e}")
            break
        if not c:
            break
        all_c.extend(c)
        if len(c) < 1000:
            break
        since = c[-1][0] + 1
        time.sleep(0.08)
    if not all_c:
        return pd.DataFrame()
    df = pd.DataFrame(all_c, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.drop_duplicates("ts").set_index("ts").sort_index().astype(float)


def simulate_trail(entry, direction, future_df):
    """Returns (pnl_pct, bars_used, initial_sl_hit)."""
    if future_df.empty:
        return 0.0, 0, False
    sl_price = entry * (1 + SL_PCT / 100) if direction == "SHORT" else entry * (1 - SL_PCT / 100)
    peak = 0.0

    def milestone_sl(peak_pct):
        if peak_pct < TRAIL_START:
            return sl_price
        m = int(peak_pct / TRAIL_STEP) * TRAIL_STEP
        m = max(m, TRAIL_START)
        locked = 0.0 if m == TRAIL_START else round(m - TRAIL_GAP, 4)
        return entry * (1 - locked / 100) if direction == "SHORT" else entry * (1 + locked / 100)

    k = 0
    for _, row in future_df.iloc[:200].iterrows():
        k += 1
        hi, lo = row["high"], row["low"]
        if direction == "SHORT":
            curr = (entry - lo) / entry * 100
            if curr > peak:
                peak = curr
                sl_price = min(sl_price, milestone_sl(peak))
            if hi >= sl_price:
                pnl = round((entry - sl_price) / entry * 100, 4)
                return pnl, k, peak < TRAIL_START
        else:
            curr = (hi - entry) / entry * 100
            if curr > peak:
                peak = curr
                sl_price = max(sl_price, milestone_sl(peak))
            if lo <= sl_price:
                pnl = round((sl_price - entry) / entry * 100, 4)
                return pnl, k, peak < TRAIL_START
    last = float(future_df.iloc[-1]["close"])
    pnl = round(((entry - last) if direction == "SHORT" else (last - entry)) / entry * 100, 4)
    return pnl, k, False


def backtest_coin(coin, d1h, d6h, d1d, variant):
    cutoff = pd.Timestamp(datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS))
    d1h_bt = d1h[d1h.index >= cutoff]
    if len(d1h_bt) < MFI_PERIOD + 5:
        return []

    dir_1d = dir_series(d1d, adx_min=ADX_MIN) if not d1d.empty else pd.Series("FLAT", index=d1d.index)
    dir_6h = dir_series(d6h) if not d6h.empty else pd.Series("FLAT", index=d6h.index)
    dir_1d_ff = dir_1d.reindex(d1h_bt.index, method="ffill").fillna("FLAT")
    dir_6h_ff = dir_6h.reindex(d1h_bt.index, method="ffill").fillna("FLAT")
    dir_combined = pd.Series("FLAT", index=d1h_bt.index)
    agree = dir_1d_ff == dir_6h_ff
    dir_combined[agree & (dir_1d_ff == "LONG")] = "LONG"
    dir_combined[agree & (dir_1d_ff == "SHORT")] = "SHORT"

    d1h_bt = d1h_bt.copy()
    d1h_bt["mfi"] = calc_mfi(d1h_bt)
    mfi = d1h_bt["mfi"]
    ob_cross = (mfi.shift(1) < MFI_OB) & (mfi >= MFI_OB)
    os_cross = (mfi.shift(1) > MFI_OS) & (mfi <= MFI_OS)
    session = (d1h_bt.index.hour >= SESSION_START) & (d1h_bt.index.hour < SESSION_END)
    closes = d1h_bt["close"]

    def take(i, side, tag, trades):
        entry = float(closes.iloc[i])
        pnl, used, init_sl = simulate_trail(entry, side, d1h_bt.iloc[i + 1:])
        trades.append(dict(coin=coin, entry_ts=d1h_bt.index[i], direction=side,
                           tag=tag, pnl_pct=pnl,
                           pnl_usd=round(pnl / 100 * NOTIONAL, 2),
                           win=pnl > 0.05))
        return i + used, init_sl        # exit bar index (approx), initial-SL flag

    trades = []
    in_cooldown = False
    cool_side = None

    for i in range(MFI_PERIOD + 2, len(d1h_bt) - 1):
        if not session[i]:
            continue
        mfi_now = float(mfi.iloc[i])
        if in_cooldown:
            if cool_side == "above50" and mfi_now > 50:
                in_cooldown = False
            elif cool_side == "below50" and mfi_now < 50:
                in_cooldown = False
            else:
                continue
        direction = dir_combined.iloc[i]
        if direction == "FLAT":
            continue
        is_short = bool(ob_cross.iloc[i]) and direction == "SHORT"
        is_long = bool(os_cross.iloc[i]) and direction == "LONG"
        if not is_short and not is_long:
            continue
        side = "SHORT" if is_short else "LONG"
        exit_i, init_sl = take(i, side, "ORIG", trades)

        # ── re-entry chain ────────────────────────────────────────────────
        if variant is not None:
            re_left = variant["max_re"]
            while init_sl and re_left > 0 and exit_i < len(d1h_bt) - 2:
                found = None
                for j in range(exit_i + 1,
                               min(exit_i + 1 + variant["n_bars"], len(d1h_bt) - 1)):
                    if not session[j] or dir_combined.iloc[j] != side:
                        continue
                    m = float(mfi.iloc[j])
                    if variant["mode"] == "hold":
                        ok = m >= MFI_OB if side == "SHORT" else m <= MFI_OS
                    else:
                        ok = bool(ob_cross.iloc[j]) if side == "SHORT" else bool(os_cross.iloc[j])
                    if ok:
                        found = j
                        break
                if found is None:
                    break
                exit_i, init_sl = take(found, side, "RE", trades)
                re_left -= 1

        in_cooldown = True
        cool_side = "above50" if side == "SHORT" else "below50"

    return trades


def pf_of(rows):
    gw = sum(t["pnl_pct"] for t in rows if t["win"])
    gl = abs(sum(t["pnl_pct"] for t in rows if not t["win"]))
    return round(gw / gl, 2) if gl > 0 else float("inf")


def main():
    since_1h = int((datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS + 5)).timestamp() * 1000)
    since_htf = int((datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS + 35)).timestamp() * 1000)
    data = {}
    for coin in COINS:
        sym = f"{coin}/USDT:USDT"
        d1h = fetch_df(sym, "1h", since_1h)
        d6h = fetch_df(sym, "6h", since_htf)
        d1d = fetch_df(sym, "1d", since_htf)
        if d1h.empty:
            print(f"{coin}: SKIP")
            continue
        data[coin] = (d1h, d6h, d1d)
        print(f"{coin}: {len(d1h)} 1H bars", flush=True)

    months = LOOKBACK_DAYS / 30.44
    print(f"\n8085 RE-ENTRY EXPERIMENT — v2.0 config, {LOOKBACK_DAYS}d, "
          f"{len(data)} coins, 1H proxy")
    print("=" * 96)
    all_rows = {}
    for vname, vcfg in VARIANTS.items():
        rows = []
        for coin, (a, b, c) in data.items():
            rows += backtest_coin(coin, a, b, c, vcfg)
        all_rows[vname] = rows
        wr = 100 * sum(t["win"] for t in rows) / len(rows) if rows else 0
        usd = sum(t["pnl_usd"] for t in rows)
        re_rows = [t for t in rows if t["tag"] == "RE"]
        re_wr = 100 * sum(t["win"] for t in re_rows) / len(re_rows) if re_rows else 0
        print(f"{vname:20s} n={len(rows):4d} WR={wr:5.1f}% PF={pf_of(rows):5.2f} "
              f"Mo$={usd/months:+7.0f} | RE-only: n={len(re_rows):3d} "
              f"WR={re_wr:5.1f}% PF={pf_of(re_rows) if re_rows else 0:5.2f} "
              f"${sum(t['pnl_usd'] for t in re_rows):+8.0f}")

    # per-coin for best variant vs base
    print("\nPer-coin (BASE → R2):")
    for coin in data:
        b = [t for t in all_rows["BASE"] if t["coin"] == coin]
        r = [t for t in all_rows["R2_recross_12h_x1"] if t["coin"] == coin]
        if b:
            print(f"  {coin:5s} BASE n={len(b):3d} PF={pf_of(b):5.2f} "
                  f"${sum(t['pnl_usd'] for t in b):+8.0f} | "
                  f"R2 n={len(r):3d} PF={pf_of(r):5.2f} ${sum(t['pnl_usd'] for t in r):+8.0f}")

    pd.DataFrame([t for v, rows in all_rows.items()
                  for t in [dict(variant=v, **r) for r in rows]]
                 ).to_csv("reentry_8085_trades.csv", index=False)
    print("\nSaved: reentry_8085_trades.csv")


if __name__ == "__main__":
    main()
