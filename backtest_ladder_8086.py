"""
8086 Alligator — Laddered-entry experiment (Philakone residue #2, final open idea)
════════════════════════════════════════════════════════════════════════════════
Signal (faithful to live trader, variant A):
  - Direction from last CLOSED 6H alligator (jaw13/8 > teeth8/5 > lips5/3 = SHORT
    stack; reverse = LONG), spread |jaw-lips|/lips < 2%
  - First 2H sub-candle of the current 6H closes in direction (red for SHORT)
  - Entry at that 2H close. One trade max per 6H candle per coin. SHORT-only
    (v2.1+ rule; SEL-LONG exceptions ignored for a clean experiment).
Exits (v2.5): SL 1.5% | trail start 3.0%, step 0.5%, gap 0.4% — simulated on 2H.

Ladder variants (the experiment): split the single market entry into rungs;
rung 2/3 are resting limits at BETTER prices, valid until the current 6H candle
closes (i.e. up to the remaining 2 sub-candles). Unfilled rungs = smaller
position. Each rung simulated as an independent mini-trade (own SL/trail from
its own fill) so rung-level PF is directly observable.
  BASE:  100% at signal close
  L1:    50% at close + 50% at +0.5% (SHORT: higher)
  L2:    50% at close + 50% at +1.0%
  L3:    34% at close + 33% at +0.5% + 33% at +1.0%
Fees 0.05%/side. $2,500 notional per FULL position (rungs pro-rata). 365d.
Gates: variant must beat BASE on PF and Mo$ (weighted); rung-2/3 fills alone
PF > rung-1 PF to justify (better entries must actually be better).
"""

import ccxt
import numpy as np
import pandas as pd
import time
from datetime import datetime, timedelta, timezone

exchange = ccxt.binanceusdm({"enableRateLimit": True, "timeout": 30000})

LOOKBACK_DAYS = 365
NOTIONAL      = 2_500
FEE_RT        = 0.10
SL_PCT        = 1.5
TRAIL_START   = 3.0
TRAIL_STEP    = 0.5
TRAIL_GAP     = 0.4
SPREAD_MAX    = 2.0

COINS = ["XRP", "ADA", "SUI", "DOGE", "DOT"]

JAW_P, JAW_S = 13, 8
TEETH_P, TEETH_S = 8, 5
LIPS_P, LIPS_S = 5, 3

VARIANTS = {
    "BASE": [(1.0, 0.0)],
    "L1":   [(0.5, 0.0), (0.5, 0.5)],
    "L2":   [(0.5, 0.0), (0.5, 1.0)],
    "L3":   [(0.34, 0.0), (0.33, 0.5), (0.33, 1.0)],
}


def fetch_df(symbol, tf, since_ms):
    rows, since = [], since_ms
    while True:
        try:
            c = exchange.fetch_ohlcv(symbol, tf, since=since, limit=1000)
        except Exception as e:
            print(f"  [{symbol} {tf}] {e}")
            break
        if not c:
            break
        rows.extend(c)
        if len(c) < 1000:
            break
        since = c[-1][0] + 1
        time.sleep(0.08)
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.drop_duplicates("ts").set_index("ts").sort_index().astype(float)


def calc_alligator(df):
    df = df.copy()
    df["jaw"] = df["close"].ewm(alpha=1 / JAW_P, adjust=False).mean().shift(JAW_S)
    df["teeth"] = df["close"].ewm(alpha=1 / TEETH_P, adjust=False).mean().shift(TEETH_S)
    df["lips"] = df["close"].ewm(alpha=1 / LIPS_P, adjust=False).mean().shift(LIPS_S)
    conds = [
        (df.jaw > df.teeth) & (df.teeth > df.lips),
        (df.lips > df.teeth) & (df.teeth > df.jaw),
    ]
    df["direction"] = np.select(conds, ["SHORT", "LONG"], default="FLAT")
    df["spread"] = (df.jaw - df.lips).abs() / df.lips * 100
    return df


def simulate_trail(entry, direction, future):
    """v2.5 exits on 2H bars: SL 1.5%, trail 3.0/0.5/0.4."""
    if future.empty:
        return 0.0
    sl = entry * (1 + SL_PCT / 100) if direction == "SHORT" else entry * (1 - SL_PCT / 100)
    peak = 0.0

    def mstone(p):
        if p < TRAIL_START:
            return sl
        m = max(int(p / TRAIL_STEP) * TRAIL_STEP, TRAIL_START)
        locked = 0.0 if m == TRAIL_START else round(m - TRAIL_GAP, 4)
        return entry * (1 - locked / 100) if direction == "SHORT" else entry * (1 + locked / 100)

    for _, row in future.iloc[:400].iterrows():
        hi, lo = row["high"], row["low"]
        if direction == "SHORT":
            cur = (entry - lo) / entry * 100
            if cur > peak:
                peak = cur
                sl = min(sl, mstone(peak))
            if hi >= sl:
                return round((entry - sl) / entry * 100, 4)
        else:
            cur = (hi - entry) / entry * 100
            if cur > peak:
                peak = cur
                sl = max(sl, mstone(peak))
            if lo <= sl:
                return round((sl - entry) / entry * 100, 4)
    last = float(future.iloc[-1]["close"])
    return round(((entry - last) if direction == "SHORT" else (last - entry)) / entry * 100, 4)


def backtest_coin(coin, d6h, d2h, variant_rungs, trades):
    d6h = calc_alligator(d6h)
    cutoff = pd.Timestamp(datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS))
    idx2h = d2h.index
    for i in range(30, len(d6h) - 1):
        c6 = d6h.iloc[i]           # current 6H candle (was "forming")
        prev = d6h.iloc[i - 1]     # last CLOSED 6H → direction + spread
        if c6.name < cutoff:
            continue
        if prev["direction"] != "SHORT":          # SHORT-only
            continue
        if prev["spread"] > SPREAD_MAX:
            continue
        # first 2H sub-candle of this 6H
        sub_pos = idx2h.searchsorted(c6.name)
        if sub_pos >= len(idx2h) or idx2h[sub_pos] != c6.name:
            continue
        first = d2h.iloc[sub_pos]
        if first["close"] >= first["open"]:        # must be red for SHORT
            continue
        sig_px = float(first["close"])
        # ladder rungs: limits valid during remaining 2 sub-candles of this 6H
        window = d2h.iloc[sub_pos + 1: sub_pos + 3]
        for (frac, off_pct) in variant_rungs:
            if off_pct == 0.0:
                fill_px, fill_pos = sig_px, sub_pos
            else:
                lim = sig_px * (1 + off_pct / 100)   # SHORT: higher = better
                hit = window[window["high"] >= lim]
                if hit.empty:
                    continue
                fill_pos = idx2h.searchsorted(hit.index[0])
                fill_px = lim
            pnl = simulate_trail(fill_px, "SHORT", d2h.iloc[fill_pos + 1:]) - FEE_RT
            trades.append(dict(coin=coin, entry_ts=str(idx2h[fill_pos]),
                               rung=off_pct, frac=frac, pnl_pct=pnl,
                               pnl_usd=round(pnl / 100 * NOTIONAL * frac, 2)))


def pf_of(rows, weight=False):
    if weight:
        gw = sum(r["pnl_pct"] * r["frac"] for r in rows if r["pnl_pct"] > 0)
        gl = abs(sum(r["pnl_pct"] * r["frac"] for r in rows if r["pnl_pct"] <= 0))
    else:
        gw = sum(r["pnl_pct"] for r in rows if r["pnl_pct"] > 0)
        gl = abs(sum(r["pnl_pct"] for r in rows if r["pnl_pct"] <= 0))
    return round(gw / gl, 2) if gl > 0 else float("inf")


def main():
    since = int((datetime.now(timezone.utc)
                 - timedelta(days=LOOKBACK_DAYS + 40)).timestamp() * 1000)
    data = {}
    for coin in COINS:
        sym = f"{coin}/USDT:USDT"
        d6 = fetch_df(sym, "6h", since)
        d2 = fetch_df(sym, "2h", since)
        if d6.empty or d2.empty:
            print(f"{coin}: SKIP")
            continue
        data[coin] = (d6, d2)
        print(f"{coin}: {len(d6)} 6H / {len(d2)} 2H bars", flush=True)

    months = LOOKBACK_DAYS / 30.44
    print(f"\n8086 LADDER EXPERIMENT — SHORT-only, v2.5 exits (3.0/0.4), "
          f"{LOOKBACK_DAYS}d, {len(data)} coins")
    print("=" * 100)
    for vname, rungs in VARIANTS.items():
        trades = []
        for coin, (d6, d2) in data.items():
            backtest_coin(coin, d6, d2, rungs, trades)
        usd = sum(t["pnl_usd"] for t in trades)
        wr = 100 * sum(t["pnl_pct"] > 0 for t in trades) / len(trades) if trades else 0
        print(f"\n{vname}: signals→fills n={len(trades)} WR={wr:.1f}% "
              f"PF(w)={pf_of(trades, weight=True)} Mo$={usd/months:+.0f}")
        for off in sorted({t['rung'] for t in trades}):
            rr = [t for t in trades if t["rung"] == off]
            wr_r = 100 * sum(t["pnl_pct"] > 0 for t in rr) / len(rr)
            print(f"   rung +{off}%: n={len(rr):3d} WR={wr_r:5.1f}% PF={pf_of(rr):5.2f} "
                  f"avg={np.mean([t['pnl_pct'] for t in rr]):+.3f}% "
                  f"${sum(t['pnl_usd'] for t in rr):+8.0f}")
        if vname == "BASE":
            base_usd, base_pf = usd, pf_of(trades, weight=True)
    print("\nDone.")


if __name__ == "__main__":
    main()
