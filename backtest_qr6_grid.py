"""
QR-6 v0.1 — BTC Grid Bot backtest (structural mean reversion)
════════════════════════════════════════════════════════════════
Spec (agreed 2026-07-03):
- Range: rolling 30d low/high computed at (re)arm time
- Grids: 40 / 60 evenly-spaced levels, NEUTRAL mode
  (at arm: buy inventory to back one sell order at every level above price;
   cash backs one buy order at every level below price; $2,500/N per level)
- Fill model: 5m candles; buy level fills if low<=level, sell fills if
  high>=level; fills at the level price (limit order, maker)
- Hard stop: 5m CLOSE 2% beyond either boundary → flatten all at that close
  (taker), wait 3 days, re-arm with fresh 30d range
- Range policies: A = break-triggered re-arm only (baseline)
                  B = weekly re-center (flatten+re-arm every 7d, maker-ish
                      cost still applied as taker on flatten)
- Fee configs: SPOT 0.075% (BNB discount) | FUTURES 0.02% maker / 0.045% taker
- Capital $2,500. IS = year 1, OOS = year 2 of a 730d window.
Gates: net>0 in IS AND OOS; maxDD < 1.5x annual profit;
       worst single break loss < 3 months of grid income.
"""

import ccxt
import numpy as np
import pandas as pd
import time
from datetime import datetime, timezone

EX = ccxt.binanceusdm({"enableRateLimit": True, "timeout": 30000})

DAYS     = 730
CAPITAL  = 2_500.0
BREAK_PCT = 0.02          # close 2% beyond boundary → stop
REARM_DAYS = 3
BAR_MS   = 300_000        # 5m

FEES = {"SPOT": {"maker": 0.00075, "taker": 0.00075},
        "FUT":  {"maker": 0.00020, "taker": 0.00045}}


def fetch_5m(days):
    since = EX.milliseconds() - days * 86_400_000 - 30 * 86_400_000  # +30d warmup
    rows = []
    now = EX.milliseconds()
    while since < now - BAR_MS:
        b = EX.fetch_ohlcv("BTC/USDT:USDT", "5m", since=since, limit=1000)
        if not b:
            break
        rows += b
        since = b[-1][0] + 1
        time.sleep(0.1)
    df = pd.DataFrame(rows, columns=["ts", "o", "h", "l", "c", "v"])
    df = df.drop_duplicates("ts").reset_index(drop=True)
    df["dt"] = pd.to_datetime(df.ts, unit="ms", utc=True)
    print(f"fetched {len(df)} 5m bars: {df.dt.iloc[0]} → {df.dt.iloc[-1]}", flush=True)
    return df


def run_grid(df, n_grids, fee_key, policy, warmup_bars):
    """Event-driven grid sim. Returns per-bar equity + event log."""
    mk, tk = FEES[fee_key]["maker"], FEES[fee_key]["taker"]
    o, h, l, c = df.o.values, df.h.values, df.l.values, df.c.values
    ts = df.ts.values
    n = len(df)
    bars_30d = 30 * 288
    bars_7d = 7 * 288
    bars_rearm = REARM_DAYS * 288

    cash = CAPITAL
    qty = 0.0
    levels = None
    has_sell = has_buy = None
    lo_b = hi_b = None
    armed = False
    arm_bar = -1
    wait_until = -1

    nonlocal_eq_arm = [CAPITAL]
    grid_profit = 0.0          # realized buy→sell cycle profit
    n_cycles = 0
    n_breaks = 0
    break_losses = []          # equity change on each break flatten
    equity = np.full(n, np.nan)
    events = []

    def mark(i):
        return cash + qty * c[i]

    def flatten(i, why):
        nonlocal cash, qty, armed, n_breaks
        eq_before = mark(i)
        if qty > 0:
            cash += qty * c[i] * (1 - tk)
        elif qty < 0:
            cash += qty * c[i] * (1 + tk)
        qty = 0.0
        armed = False
        events.append((str(df.dt[i]), why, round(eq_before, 2)))

    def arm(i):
        nonlocal levels, has_sell, has_buy, lo_b, hi_b, armed, arm_bar, cash, qty
        lo_b = l[i - bars_30d:i].min()
        hi_b = h[i - bars_30d:i].max()
        if hi_b / lo_b - 1 < 0.02:      # degenerate range
            return False
        levels = np.linspace(lo_b, hi_b, n_grids + 1)
        px = c[i]
        has_sell = levels > px * 1.001
        has_buy = levels < px * 0.999
        per_level = CAPITAL / (n_grids + 1)
        need_qty = has_sell.sum() * per_level / px
        cost = need_qty * px * (1 + tk)
        if cost > cash:                  # scale to available cash
            need_qty = cash * 0.98 / (px * (1 + tk)) * has_sell.sum() / max(has_sell.sum(), 1)
        cash -= need_qty * px * (1 + tk)
        qty += need_qty
        armed = True
        arm_bar = i
        nonlocal_eq_arm[0] = mark(i)
        events.append((str(df.dt[i]), f"ARM {lo_b:.0f}-{hi_b:.0f}", round(mark(i), 2)))
        return True

    i = warmup_bars
    arm(i)
    per_level = CAPITAL / (n_grids + 1)

    while i < n:
        if not armed:
            equity[i] = mark(i)
            if i >= wait_until:
                arm(i)
            i += 1
            continue

        # fills this bar (limit orders at levels, maker fee)
        for j in range(len(levels)):
            lv = levels[j]
            if has_buy[j] and l[i] <= lv:
                q = per_level / lv
                cost = q * lv * (1 + mk)
                if cash >= cost:            # no free borrowing
                    cash -= cost
                    qty += q
                    has_buy[j] = False
                    if j + 1 < len(levels):
                        has_sell[j + 1] = True
            if has_sell[j] and h[i] >= lv:
                q = per_level / lv
                cash += q * lv * (1 - mk)
                qty -= q
                has_sell[j] = False
                if j - 1 >= 0:
                    has_buy[j - 1] = True
                step = (hi_b - lo_b) / n_grids
                grid_profit += per_level * (step / lv) - 2 * per_level * mk
                n_cycles += 1

        # break check on close
        if c[i] > hi_b * (1 + BREAK_PCT) or c[i] < lo_b * (1 - BREAK_PCT):
            flatten(i, "BREAK")
            n_breaks += 1
            break_losses.append(mark(i) - nonlocal_eq_arm[0])
            wait_until = i + bars_rearm
        elif policy == "B" and i - arm_bar >= bars_7d:
            flatten(i, "RECENTER")
            wait_until = i          # immediate re-arm next bar
        equity[i] = mark(i)
        i += 1

    eq = pd.Series(equity, index=df.dt).ffill()
    return eq, dict(grid_profit=round(grid_profit, 2), cycles=n_cycles,
                    breaks=n_breaks,
                    worst_break=round(min(break_losses), 2) if break_losses else 0.0,
                    events=events)


def report(eq, stats, label, warmup_bars):
    eq = eq.iloc[warmup_bars:]
    total = eq.iloc[-1] - CAPITAL
    mid = len(eq) // 2
    is_pnl = eq.iloc[mid] - eq.iloc[0]
    oos_pnl = eq.iloc[-1] - eq.iloc[mid]
    dd = (eq - eq.cummax()).min()
    months = DAYS / 30.44
    mo_grid = stats["grid_profit"] / months
    g1 = is_pnl > 0 and oos_pnl > 0
    g2 = abs(dd) < 1.5 * (total / 2) if total > 0 else False
    g3 = abs(stats["worst_break"]) < 3 * mo_grid if mo_grid > 0 else False
    print(f"\n{label}")
    print(f"  total ${total:>8.0f} | IS ${is_pnl:>7.0f} | OOS ${oos_pnl:>7.0f} | "
          f"ret {total/CAPITAL*100:5.1f}% ({total/CAPITAL*100/months*12:5.1f}%/yr)")
    print(f"  grid profit ${stats['grid_profit']:>8.0f} ({stats['cycles']} cycles, "
          f"${mo_grid:.0f}/mo) | inventory/breaks ${total-stats['grid_profit']:>8.0f}")
    print(f"  maxDD ${dd:>7.0f} | breaks {stats['breaks']} | worst break ${stats['worst_break']:>7.0f}")
    print(f"  GATES: IS+OOS>0 {'PASS' if g1 else 'FAIL'} | DD<1.5x/yr "
          f"{'PASS' if g2 else 'FAIL'} | break<3mo {'PASS' if g3 else 'FAIL'}")
    return dict(label=label, total=round(total), is_pnl=round(is_pnl),
                oos=round(oos_pnl), dd=round(dd), **{k: v for k, v in stats.items()
                                                     if k != "events"},
                gates=f"{g1}/{g2}/{g3}")


def main():
    df = fetch_5m(DAYS)
    warmup = 30 * 288
    rows = []
    for n_grids in (40, 60):
        for fee in ("SPOT", "FUT"):
            for pol in ("A", "B"):
                eq, st = run_grid(df, n_grids, fee, pol, warmup)
                rows.append(report(eq, st, f"QR6 {n_grids}g {fee} policy{pol}", warmup))
    pd.DataFrame(rows).to_csv("qr6_grid_results.csv", index=False)
    # buy & hold reference
    px = df.c.iloc[warmup:]
    bh = (px.iloc[-1] / px.iloc[0] - 1) * 100
    print(f"\nReference: BTC buy&hold over window {bh:+.1f}% "
          f"({px.iloc[0]:.0f} → {px.iloc[-1]:.0f})")
    print("Saved: qr6_grid_results.csv")


if __name__ == "__main__":
    main()
