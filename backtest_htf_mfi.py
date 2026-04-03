#!/usr/bin/env python3
"""
HTF MFI Strategy — Comprehensive Lever Backtest
════════════════════════════════════════════════
Version: 2.0  (fixed 1H direction conflict — see KEY DESIGN NOTE below)

Strategy (matches htf_mfi_paper_trader.py):
  Direction filter : 1D + 6H + 1H must agree (price above/below EMA20, slope, ADX≥N on 1D)
  Entry            : 1H MFI fresh cross ≥ OB → SHORT; ≤ OS → LONG
                     (1H candle used as proxy for 1M MFI in live system)
  Exit             : Pure milestone trailing SL
                     Hard SL until +1.0% profit → BE; +1.5% → lock +0.8%; +2.0% → lock +1.3%
                     Every subsequent +0.5%: lock milestone − 0.7%
  Session          : 07:00–22:00 UTC
  Cooldown         : After close, wait for 1H MFI to cross back through 50

KEY DESIGN NOTE — Why 1H direction is excluded from the entry check:
  On 1H data, when MFI crosses ≥ 80 (OB), price just moved up strongly in that candle.
  This means the 1H EMA is sloping up and price > EMA → 1H direction = LONG.
  Requiring 1H direction = SHORT for a SHORT entry on 1H MFI OB cross is NEVER satisfiable.
  In the live 1M system, a 1-minute MFI spike doesn't affect the 1H EMA, so 1H direction
  can legitimately be SHORT while 1M MFI crosses OB. The backtest therefore:
    - Uses 1D + 6H for the HTF direction filter (checked at the signal candle)
    - Uses 1H MFI cross as the entry trigger (just like 1M MFI in live)
    - Optionally tests 1H direction as an ADDITIONAL filter (Lever 2 variant)

Levers:
  1. ADX threshold    : None, 15, 18, 20, 22, 25  (BTC+ETH, OB80/OS20, SL 1.0%, 1D+6H agree)
  2. TF combination   : 1D+6H only | 1D+6H+1H(prior) | 6H only | No filter
  3. MFI thresholds   : OB70/OS30, OB75/OS25, OB80/OS20, OB85/OS15
  4. Coin expansion   : BTC, ETH, SOL, LINK, XRP, BNB (various combos)
  5. SL variants      : 0.75%, 1.0%, 1.5%
  6. 6H MFI zone filter vs no filter

Data: ccxt binanceusdm (public), 90 days of 1H OHLCV
"""

import ccxt
import pandas as pd
import numpy as np
import time
import warnings
from datetime import datetime, timedelta, timezone

warnings.filterwarnings("ignore")

# ─── CONSTANTS ────────────────────────────────────────────────────────────────
LOOKBACK_DAYS   = 90
EMA_PERIOD      = 20
EMA_SLOPE_BARS  = 3
ADX_PERIOD      = 14
MFI_PERIOD      = 14
SESSION_START   = 7
SESSION_END     = 22

# Trailing SL milestones (fixed)
TRAIL_START = 1.0   # % profit → SL moves to BE
TRAIL_STEP  = 0.5   # % between milestones
TRAIL_GAP   = 0.7   # SL locked at (milestone − TRAIL_GAP)%

# Current live 6H MFI zone filter config
MFI_6H_ZONE_CURRENT = {
    "BTC": {"SHORT": None,       "LONG":  (25, 55)},
    "ETH": {"SHORT": (45, 75),   "LONG":  (25, 55)},
}

ALL_COINS = ["BTC", "ETH", "SOL", "LINK", "XRP", "BNB"]

# ─── INDICATORS ───────────────────────────────────────────────────────────────

def calc_ema(series, period=EMA_PERIOD):
    return series.ewm(span=period, adjust=False).mean()


def calc_mfi(df, period=MFI_PERIOD):
    tp  = (df["high"] + df["low"] + df["close"]) / 3
    rmf = tp * df["volume"]
    pos = rmf.where(tp > tp.shift(1), 0.0)
    neg = rmf.where(tp < tp.shift(1), 0.0)
    ps  = pos.rolling(period).sum()
    ns  = neg.rolling(period).sum().replace(0, 1e-10)
    return (100 - (100 / (1 + ps / ns))).fillna(50)


def calc_adx(df, period=ADX_PERIOD):
    hi, lo, cl = df["high"], df["low"], df["close"]
    up   = hi.diff()
    down = lo.diff().mul(-1)
    pdm  = up.where((up > down) & (up > 0), 0.0)
    mdm  = down.where((down > up) & (down > 0), 0.0)
    hl   = hi - lo
    hpc  = (hi - cl.shift(1)).abs()
    lpc  = (lo - cl.shift(1)).abs()
    tr   = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
    a    = 1 / period
    atr  = tr.ewm(alpha=a, adjust=False).mean()
    pdi  = 100 * pdm.ewm(alpha=a, adjust=False).mean() / atr.replace(0, 1e-10)
    mdi  = 100 * mdm.ewm(alpha=a, adjust=False).mean() / atr.replace(0, 1e-10)
    dx   = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, 1e-10)
    return dx.ewm(alpha=a, adjust=False).mean()


def dir_series(df, adx_min=None):
    """Vectorised direction: LONG / SHORT / FLAT based on EMA20 + slope + optional ADX."""
    ema   = calc_ema(df["close"])
    price = df["close"]
    slope = ema > ema.shift(EMA_SLOPE_BARS)
    above = price > ema
    dirs  = pd.Series("FLAT", index=df.index)
    if adx_min is not None and "adx" not in df.columns:
        df = df.copy()
        df["adx"] = calc_adx(df)
    if adx_min is not None:
        ok = df["adx"] >= adx_min
        dirs[above &  slope & ok] = "LONG"
        dirs[~above & ~slope & ok] = "SHORT"
    else:
        dirs[above &  slope] = "LONG"
        dirs[~above & ~slope] = "SHORT"
    return dirs


# ─── DATA FETCHING ────────────────────────────────────────────────────────────

def fetch_df(exchange, symbol, tf, since_ms, limit=1000):
    all_c = []
    since = since_ms
    while True:
        try:
            c = exchange.fetch_ohlcv(symbol, tf, since=since, limit=limit)
        except Exception as e:
            print(f"  [WARN] {symbol} {tf}: {e}")
            break
        if not c:
            break
        all_c.extend(c)
        if len(c) < limit:
            break
        since = c[-1][0] + 1
        time.sleep(0.08)
    if not all_c:
        return pd.DataFrame()
    df = pd.DataFrame(all_c, columns=["ts","open","high","low","close","volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.drop_duplicates("ts").set_index("ts").sort_index().astype(float)


def fetch_all_data(exchange, coins):
    """Fetch 1H, 6H, 1D for all coins. Returns dict[coin][tf] = df."""
    since_1h  = int((datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS + 5)).timestamp() * 1000)
    since_htf = int((datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS + 35)).timestamp() * 1000)
    data = {}
    for coin in coins:
        sym = f"{coin}/USDT:USDT"
        print(f"  {coin} ...", end=" ", flush=True)
        d1h = fetch_df(exchange, sym, "1h",  since_1h)
        d6h = fetch_df(exchange, sym, "6h",  since_htf)
        d1d = fetch_df(exchange, sym, "1d",  since_htf)
        if d1h.empty:
            print("SKIP")
            continue
        # Pre-compute MFI on 1H, 6H
        d1h["mfi"] = calc_mfi(d1h)
        d6h["mfi"] = calc_mfi(d6h) if not d6h.empty else None
        # Pre-compute ADX on 1D
        if not d1d.empty:
            d1d["adx"] = calc_adx(d1d)
        print(f"1H:{len(d1h)}  6H:{len(d6h)}  1D:{len(d1d)}")
        data[coin] = {"1h": d1h, "6h": d6h, "1d": d1d}
    return data


# ─── DIRECTION ALIGNMENT (vectorised, aligned to 1H timestamps) ───────────────

def build_direction_arrays(coin_data, coin, adx_min):
    """
    Returns two Series aligned to 1H index:
      dir_1d_ff : 1D direction, forward-filled to 1H
      dir_6h_ff : 6H direction, forward-filled to 1H
      dir_1h_s  : 1H direction (same index as 1H)
    """
    d1h = coin_data[coin]["1h"]
    d6h = coin_data[coin]["6h"]
    d1d = coin_data[coin]["1d"]

    dir_1d = dir_series(d1d, adx_min=adx_min) if not d1d.empty else pd.Series(dtype=str)
    dir_6h = dir_series(d6h)                   if not d6h.empty else pd.Series(dtype=str)
    dir_1h = dir_series(d1h)

    dir_1d_ff = dir_1d.reindex(d1h.index, method="ffill").fillna("FLAT")
    dir_6h_ff = dir_6h.reindex(d1h.index, method="ffill").fillna("FLAT")

    return dir_1d_ff, dir_6h_ff, dir_1h


def combined_direction(dir_1d_ff, dir_6h_ff, dir_1h_s, tf_combo):
    """
    Vectorised: returns a Series of LONG/SHORT/FLAT.
    tf_combo options:
      '1D+6H'      : 1D and 6H must agree (primary — avoids 1H conflict)
      '1D+6H+1H'   : all 3 must agree (uses 1H direction from prior candle shift)
      '6H only'    : only 6H direction
      'no filter'  : any session-active candle is valid (direction = mfi cross direction)
      '2of3'       : majority of 1D,6H,1H agree
    """
    if tf_combo == "1D+6H":
        dirs = pd.Series("FLAT", index=dir_1d_ff.index)
        agree = dir_1d_ff == dir_6h_ff
        dirs[agree & (dir_1d_ff == "LONG")]  = "LONG"
        dirs[agree & (dir_1d_ff == "SHORT")] = "SHORT"
        return dirs

    elif tf_combo == "1D+6H+1H":
        # Use 1H direction shifted by 1 (prior candle) to avoid MFI-signal conflict
        dir_1h_prior = dir_1h_s.shift(1).fillna("FLAT")
        dirs = pd.Series("FLAT", index=dir_1d_ff.index)
        agree = (dir_1d_ff == dir_6h_ff) & (dir_6h_ff == dir_1h_prior)
        dirs[agree & (dir_1d_ff == "LONG")]  = "LONG"
        dirs[agree & (dir_1d_ff == "SHORT")] = "SHORT"
        return dirs

    elif tf_combo == "6H only":
        return dir_6h_ff

    elif tf_combo == "no filter":
        # Direction is simply the MFI signal direction — no HTF filter
        return pd.Series("ANY", index=dir_1d_ff.index)

    elif tf_combo == "2of3":
        # Majority of 1D, 6H, 1H (using prior 1H to avoid conflict)
        dir_1h_prior = dir_1h_s.shift(1).fillna("FLAT")
        longs  = ((dir_1d_ff == "LONG").astype(int) +
                  (dir_6h_ff == "LONG").astype(int) +
                  (dir_1h_prior == "LONG").astype(int))
        shorts = ((dir_1d_ff == "SHORT").astype(int) +
                  (dir_6h_ff == "SHORT").astype(int) +
                  (dir_1h_prior == "SHORT").astype(int))
        dirs = pd.Series("FLAT", index=dir_1d_ff.index)
        dirs[longs  >= 2] = "LONG"
        dirs[shorts >= 2] = "SHORT"
        return dirs

    return pd.Series("FLAT", index=dir_1d_ff.index)


# ─── TRAILING SL SIMULATION ───────────────────────────────────────────────────

def simulate_trailing_sl(entry_price, direction, future_df, sl_pct):
    """
    Simulate milestone trailing SL on subsequent 1H candles.
    Returns final pnl_pct (float, positive = profit).
    """
    if future_df.empty:
        return 0.0

    sl_dist  = sl_pct / 100.0
    peak_pnl = 0.0

    if direction == "LONG":
        sl_price = entry_price * (1 - sl_dist)
    else:
        sl_price = entry_price * (1 + sl_dist)

    def milestone_locked_pnl(m):
        """PnL% that is locked at milestone m%."""
        if m <= TRAIL_START:
            return 0.0    # BE
        return round(m - TRAIL_GAP, 4)

    def locked_to_sl(locked_pnl):
        if direction == "LONG":
            return entry_price * (1 + locked_pnl / 100)
        else:
            return entry_price * (1 - locked_pnl / 100)

    max_candles = min(len(future_df), 200)

    for i in range(max_candles):
        row = future_df.iloc[i]
        hi, lo, op = row["high"], row["low"], row["open"]

        if direction == "LONG":
            pnl_hi = (hi - entry_price) / entry_price * 100
        else:
            pnl_hi = (entry_price - lo) / entry_price * 100

        if pnl_hi > peak_pnl:
            peak_pnl = pnl_hi

        # Advance SL to highest milestone reached
        m = TRAIL_START
        while m <= peak_pnl:
            locked = milestone_locked_pnl(m)
            new_sl = locked_to_sl(locked)
            if direction == "LONG":
                if new_sl > sl_price:
                    sl_price = new_sl
            else:
                if new_sl < sl_price:
                    sl_price = new_sl
            m += TRAIL_STEP

        # Check SL hit
        if direction == "LONG":
            if lo <= sl_price:
                exit_p = min(sl_price, op) if op < sl_price else sl_price
                return round((exit_p - entry_price) / entry_price * 100, 4)
        else:
            if hi >= sl_price:
                exit_p = max(sl_price, op) if op > sl_price else sl_price
                return round((entry_price - exit_p) / entry_price * 100, 4)

    # Timeout exit at last close
    last_close = future_df.iloc[min(max_candles - 1, len(future_df) - 1)]["close"]
    if direction == "LONG":
        return round((last_close - entry_price) / entry_price * 100, 4)
    else:
        return round((entry_price - last_close) / entry_price * 100, 4)


# ─── CORE BACKTEST ───────────────────────────────────────────────────────────

def run_backtest_coin(coin_data, coin, adx_min, tf_combo, mfi_ob, mfi_os,
                      sl_pct, zone_filter=None):
    """
    Backtest one coin. Returns list of trade dicts.
    zone_filter: None | dict {'SHORT': (lo,hi)|None, 'LONG': (lo,hi)|None|False}
    """
    if coin not in coin_data:
        return []

    d1h = coin_data[coin]["1h"]
    d6h = coin_data[coin]["6h"]

    cutoff   = pd.Timestamp(datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS))
    d1h_bt   = d1h[d1h.index >= cutoff]
    if len(d1h_bt) < MFI_PERIOD + 5:
        return []

    # Direction arrays aligned to 1H index
    dir_1d_ff, dir_6h_ff, dir_1h_s = build_direction_arrays(coin_data, coin, adx_min)
    dir_combined = combined_direction(dir_1d_ff, dir_6h_ff, dir_1h_s, tf_combo)

    # Filter to backtest window
    dir_bt   = dir_combined[dir_combined.index >= cutoff]
    mfi_bt   = d1h_bt["mfi"]

    # Pre-compute session mask
    session_mask = pd.Series(
        (d1h_bt.index.hour >= SESSION_START) & (d1h_bt.index.hour < SESSION_END),
        index=d1h_bt.index,
    )

    # MFI cross signals (vectorised)
    ob_cross = (mfi_bt.shift(1) < mfi_ob) & (mfi_bt >= mfi_ob)
    os_cross = (mfi_bt.shift(1) > mfi_os) & (mfi_bt <= mfi_os)

    trades         = []
    in_cooldown    = False
    cooldown_side  = None   # 'above50' or 'below50'

    n = len(d1h_bt)
    for i in range(MFI_PERIOD + 2, n - 1):
        ts = d1h_bt.index[i]

        if not session_mask.iloc[i]:
            continue

        mfi_now = mfi_bt.iloc[i]

        # Cooldown: wait for MFI to return through 50 before next entry
        if in_cooldown:
            if cooldown_side == "above50" and mfi_now > 50:
                in_cooldown = False
            elif cooldown_side == "below50" and mfi_now < 50:
                in_cooldown = False
            else:
                continue

        # Check direction
        if ts not in dir_bt.index:
            continue
        direction = dir_bt.loc[ts]
        if direction == "FLAT":
            continue

        is_ob = ob_cross.iloc[i]
        is_os = os_cross.iloc[i]

        # Determine signal side
        if tf_combo == "no filter":
            is_short = is_ob
            is_long  = is_os
        else:
            is_short = is_ob and direction == "SHORT"
            is_long  = is_os and direction == "LONG"

        if not is_short and not is_long:
            continue

        side = "SHORT" if is_short else "LONG"

        # 6H MFI zone filter
        if zone_filter is not None and d6h is not None and not d6h.empty:
            zone_def = zone_filter.get(side)
            if zone_def is False:
                continue
            if zone_def is not None:
                sub6h = d6h[d6h.index <= ts]
                if len(sub6h) >= MFI_PERIOD:
                    mfi6h_val = sub6h["mfi"].iloc[-1]
                    lo_z, hi_z = zone_def
                    if not (lo_z <= mfi6h_val <= hi_z):
                        continue

        # Entry at this candle's close
        entry_price = float(d1h_bt["close"].iloc[i])

        # Simulate exit on subsequent 1H candles
        future = d1h_bt.iloc[i + 1:]
        pnl    = simulate_trailing_sl(entry_price, side, future, sl_pct)

        trades.append({
            "coin":        coin,
            "entry_ts":    ts,
            "direction":   side,
            "entry_price": entry_price,
            "pnl_pct":     pnl,
            "win":         pnl > 0,
            "mfi_entry":   round(float(mfi_now), 1),
        })

        in_cooldown   = True
        cooldown_side = "above50" if side == "SHORT" else "below50"

    return trades


def run_backtest(coin_data, coins, adx_min, tf_combo, mfi_ob, mfi_os,
                 sl_pct, zone_filter_map=None):
    """Run backtest over multiple coins."""
    all_trades = []
    for coin in coins:
        if coin not in coin_data:
            continue
        zf = zone_filter_map.get(coin) if zone_filter_map else None
        t  = run_backtest_coin(coin_data, coin, adx_min, tf_combo,
                               mfi_ob, mfi_os, sl_pct, zf)
        all_trades.extend(t)
    return all_trades


# ─── STATS ───────────────────────────────────────────────────────────────────

def compute_stats(trades, period_days=LOOKBACK_DAYS):
    if not trades:
        return {
            "n": 0, "wr": 0.0, "pf": 0.0, "avg_pnl": 0.0,
            "monthly_trades": 0.0, "monthly_return": 0.0, "total_pnl": 0.0,
        }
    df  = pd.DataFrame(trades)
    n   = len(df)
    wr  = df["win"].mean() * 100
    gw  = df[df["win"]]["pnl_pct"].sum()
    gl  = df[~df["win"]]["pnl_pct"].abs().sum()
    pf  = gw / gl if gl > 0 else float("inf") if gw > 0 else 0.0
    avg = df["pnl_pct"].mean()
    mo  = period_days / 30.0
    return {
        "n":               n,
        "wr":              wr,
        "pf":              pf,
        "avg_pnl":         avg,
        "monthly_trades":  n / mo,
        "monthly_return":  df["pnl_pct"].sum() / mo,
        "total_pnl":       df["pnl_pct"].sum(),
    }


def print_lever_table(title, rows, current_marker=None):
    print(f"\n{'═'*94}")
    print(f"  {title}")
    print(f"{'═'*94}")
    hdr = (f"  {'Config':<34} {'Trades':>7} {'WR%':>7} {'PF':>7} "
           f"{'Avg PnL%':>10} {'Mo.Trades':>10} {'Mo.Return%':>12}")
    print(hdr)
    print("  " + "─" * 90)

    # Find best by monthly return (at least 3 trades, finite PF)
    best_lbl, best_val = None, -9999
    for lbl, s in rows:
        if s["n"] >= 3 and s["monthly_return"] > best_val:
            best_val = s["monthly_return"]
            best_lbl = lbl

    for lbl, s in rows:
        pf_str = f"{s['pf']:.2f}" if s["pf"] != float("inf") else "∞"
        notes  = []
        if lbl == current_marker:
            notes.append("◄ CURRENT")
        if lbl == best_lbl and s["n"] >= 3:
            notes.append("★ BEST")
        note_str = "  ".join(notes)
        line = (f"  {lbl:<34} {s['n']:>7} {s['wr']:>6.1f}% {pf_str:>7} "
                f"{s['avg_pnl']:>+9.3f}% {s['monthly_trades']:>9.1f} "
                f"{s['monthly_return']:>+11.2f}%  {note_str}")
        if "★ BEST" in note_str:
            print(f"\033[1m{line}\033[0m")
        elif "◄ CURRENT" in note_str:
            print(f"\033[33m{line}\033[0m")
        else:
            print(line)
    print()


def _coin_dir_breakdown(trades, label):
    if not trades:
        print(f"  {label}: (no trades)\n")
        return
    df = pd.DataFrame(trades)
    print(f"  {label}:")
    for (coin, direction), grp in df.groupby(["coin", "direction"]):
        n   = len(grp)
        wr  = grp["win"].mean() * 100
        pnl = grp["pnl_pct"].sum()
        gw  = grp[grp["win"]]["pnl_pct"].sum()
        gl  = grp[~grp["win"]]["pnl_pct"].abs().sum()
        pf  = f"{gw/gl:.2f}" if gl > 0 else "∞"
        print(f"    {coin:<6} {direction:<6}  n={n:>4}  WR={wr:>5.1f}%  "
              f"PF={pf:>6}  TotalPnL={pnl:>+7.2f}%")
    print()


# ─── LEVER FUNCTIONS ──────────────────────────────────────────────────────────

def lever1_adx(coin_data):
    print("\n" + "█"*94)
    print("  LEVER 1: ADX Threshold")
    print("  BTC+ETH | OB80/OS20 | SL 1.0% | 1D+6H direction agree")
    print("█"*94)
    coins = [c for c in ["BTC", "ETH"] if c in coin_data]
    rows  = []
    for adx_val in [None, 15, 18, 20, 22, 25]:
        lbl    = "ADX: no filter" if adx_val is None else f"ADX ≥ {adx_val}"
        trades = run_backtest(coin_data, coins, adx_min=adx_val,
                              tf_combo="1D+6H", mfi_ob=80, mfi_os=20, sl_pct=1.0)
        rows.append((lbl, compute_stats(trades)))
    print_lever_table("LEVER 1 — ADX Threshold Sweep", rows, current_marker="ADX ≥ 22")
    return rows


def lever2_tf_combo(coin_data):
    print("█"*94)
    print("  LEVER 2: TF Combination")
    print("  BTC+ETH | ADX_MIN=20 | OB80/OS20 | SL 1.0%")
    print("█"*94)
    coins = [c for c in ["BTC", "ETH"] if c in coin_data]
    rows  = []
    combos = [
        ("1D+6H agree (primary)",          "1D+6H"),
        ("1D+6H+1H agree (prior 1H dir)",  "1D+6H+1H"),
        ("2/3 majority (prior 1H dir)",    "2of3"),
        ("6H only",                        "6H only"),
        ("No direction filter",            "no filter"),
    ]
    for lbl, combo in combos:
        trades = run_backtest(coin_data, coins, adx_min=20,
                              tf_combo=combo, mfi_ob=80, mfi_os=20, sl_pct=1.0)
        rows.append((lbl, compute_stats(trades)))
    print_lever_table("LEVER 2 — TF Combination Sweep", rows,
                      current_marker="1D+6H+1H agree (prior 1H dir)")
    return rows


def lever3_mfi(coin_data, best_adx):
    print("█"*94)
    print(f"  LEVER 3: MFI Thresholds")
    print(f"  BTC+ETH | ADX={best_adx} | 1D+6H | SL 1.0%")
    print("█"*94)
    coins = [c for c in ["BTC", "ETH"] if c in coin_data]
    rows  = []
    for ob, os_ in [(70, 30), (75, 25), (80, 20), (85, 15)]:
        lbl    = f"OB{ob}/OS{os_}"
        trades = run_backtest(coin_data, coins, adx_min=best_adx,
                              tf_combo="1D+6H", mfi_ob=ob, mfi_os=os_, sl_pct=1.0)
        rows.append((lbl, compute_stats(trades)))
    print_lever_table("LEVER 3 — MFI Threshold Sweep", rows, current_marker="OB80/OS20")
    return rows


def lever4_coins(coin_data, best_adx, best_ob, best_os):
    print("█"*94)
    print(f"  LEVER 4: Coin Expansion")
    print(f"  ADX≥{best_adx} | OB{best_ob}/OS{best_os} | 1D+6H | SL 1.0%")
    print("█"*94)
    available = [c for c in ALL_COINS if c in coin_data]
    coin_sets = [
        ("BTC only",                           ["BTC"]),
        ("ETH only",                           ["ETH"]),
        ("BTC+ETH  (current)",                 ["BTC", "ETH"]),
        ("BTC+ETH+SOL",                        ["BTC", "ETH", "SOL"]),
        ("BTC+ETH+SOL+LINK",                   ["BTC", "ETH", "SOL", "LINK"]),
        ("BTC+ETH+SOL+XRP",                    ["BTC", "ETH", "SOL", "XRP"]),
        ("All 6 (BTC ETH SOL LINK XRP BNB)",   ALL_COINS),
    ]
    rows = []
    for lbl, coins in coin_sets:
        active = [c for c in coins if c in available]
        if not active:
            continue
        trades = run_backtest(coin_data, active, adx_min=best_adx,
                              tf_combo="1D+6H", mfi_ob=best_ob,
                              mfi_os=best_os, sl_pct=1.0)
        rows.append((lbl, compute_stats(trades)))
    print_lever_table("LEVER 4 — Coin Expansion Sweep", rows,
                      current_marker="BTC+ETH  (current)")
    return rows


def lever5_sl(coin_data, best_adx, best_ob, best_os, best_coins):
    print("█"*94)
    print(f"  LEVER 5: SL Variants")
    print(f"  ADX≥{best_adx} | OB{best_ob}/OS{best_os} | 1D+6H | coins: {best_coins}")
    print("█"*94)
    available = [c for c in best_coins if c in coin_data]
    rows = []
    for sl in [0.75, 1.0, 1.5]:
        lbl    = f"SL {sl:.2f}%"
        trades = run_backtest(coin_data, available, adx_min=best_adx,
                              tf_combo="1D+6H", mfi_ob=best_ob,
                              mfi_os=best_os, sl_pct=sl)
        rows.append((lbl, compute_stats(trades)))
    print_lever_table("LEVER 5 — SL Variant Sweep", rows, current_marker="SL 1.00%")
    return rows


def lever6_zone(coin_data, best_adx, best_ob, best_os, best_coins, best_sl):
    print("█"*94)
    print(f"  LEVER 6: 6H MFI Zone Filter")
    print(f"  ADX≥{best_adx} | OB{best_ob}/OS{best_os} | 1D+6H | SL {best_sl}% | coins: {best_coins}")
    print("█"*94)
    available = [c for c in best_coins if c in coin_data]
    rows = []

    trades_no = run_backtest(coin_data, available, adx_min=best_adx,
                             tf_combo="1D+6H", mfi_ob=best_ob, mfi_os=best_os,
                             sl_pct=best_sl, zone_filter_map=None)
    rows.append(("No zone filter", compute_stats(trades_no)))

    trades_z = run_backtest(coin_data, available, adx_min=best_adx,
                            tf_combo="1D+6H", mfi_ob=best_ob, mfi_os=best_os,
                            sl_pct=best_sl, zone_filter_map=MFI_6H_ZONE_CURRENT)
    rows.append(("Current zones (BTC/ETH specific)", compute_stats(trades_z)))

    print_lever_table("LEVER 6 — 6H MFI Zone Filter", rows,
                      current_marker="Current zones (BTC/ETH specific)")

    _coin_dir_breakdown(trades_no, "No zone filter — per coin/direction")
    _coin_dir_breakdown(trades_z,  "Current zones — per coin/direction")
    return rows, trades_no, trades_z


# ─── BEST CONFIG EXTRACTION ───────────────────────────────────────────────────

def best_row(rows):
    """Return (label, stats) with best monthly_return among rows with ≥3 trades."""
    candidates = [(lbl, s) for lbl, s in rows if s["n"] >= 3]
    if not candidates:
        return None, None
    return max(candidates, key=lambda x: x[1]["monthly_return"])


def parse_adx(lbl):
    if lbl is None or "no filter" in lbl:
        return None
    try:
        return int(lbl.split("≥")[-1].strip())
    except Exception:
        return 20


def parse_mfi(lbl):
    ob, os_ = 80, 20
    if lbl:
        try:
            parts = lbl.strip().split()[0].split("/")
            ob  = int(parts[0].replace("OB", ""))
            os_ = int(parts[1].replace("OS", ""))
        except Exception:
            pass
    return ob, os_


def parse_coins(lbl):
    coin_map = {
        "BTC only":                          ["BTC"],
        "ETH only":                          ["ETH"],
        "BTC+ETH  (current)":                ["BTC", "ETH"],
        "BTC+ETH+SOL":                       ["BTC", "ETH", "SOL"],
        "BTC+ETH+SOL+LINK":                  ["BTC", "ETH", "SOL", "LINK"],
        "BTC+ETH+SOL+XRP":                   ["BTC", "ETH", "SOL", "XRP"],
        "All 6 (BTC ETH SOL LINK XRP BNB)":  ALL_COINS,
    }
    for k, v in coin_map.items():
        if k in (lbl or ""):
            return v
    return ["BTC", "ETH"]


def parse_sl(lbl):
    try:
        return float(lbl.split("SL")[1].split("%")[0].strip())
    except Exception:
        return 1.0


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    print("=" * 94)
    print("  HTF MFI Strategy — Comprehensive Lever Backtest  (v2.0)")
    print(f"  Period : last {LOOKBACK_DAYS} days  |  Coins: {ALL_COINS}")
    print(f"  Data   : ccxt binanceusdm (public, no API key needed)")
    print(f"  Proxy  : 1H MFI cross = entry trigger (live uses 1M MFI)")
    print(f"  Direction filter: 1D+6H agreement (1H excluded — see KEY DESIGN NOTE)")
    print(f"  Exit   : Milestone trailing SL | Session: {SESSION_START}:00–{SESSION_END}:00 UTC")
    print("=" * 94)

    exchange = ccxt.binanceusdm({"enableRateLimit": True})
    try:
        exchange.load_markets()
    except Exception as e:
        print(f"[ERROR] Failed to load markets: {e}")
        return

    print(f"\nFetching data for all 6 coins ...")
    coin_data = fetch_all_data(exchange, ALL_COINS)

    if not coin_data:
        print("[ERROR] No data fetched.")
        return

    available = list(coin_data.keys())
    print(f"\nData ready: {available}")
    print()

    # ── LEVER 1 ──────────────────────────────────────────────────────────────
    l1_rows = lever1_adx(coin_data)
    best_adx_lbl, _ = best_row(l1_rows)
    best_adx = parse_adx(best_adx_lbl)
    print(f"  → Lever 1 best: {best_adx_lbl}  → using ADX={best_adx} for levers 3+")

    # ── LEVER 2 ──────────────────────────────────────────────────────────────
    l2_rows = lever2_tf_combo(coin_data)

    # ── LEVER 3 ──────────────────────────────────────────────────────────────
    l3_rows = lever3_mfi(coin_data, best_adx)
    best_mfi_lbl, _ = best_row(l3_rows)
    best_ob, best_os = parse_mfi(best_mfi_lbl)
    print(f"  → Lever 3 best: {best_mfi_lbl}  → using OB{best_ob}/OS{best_os}")

    # ── LEVER 4 ──────────────────────────────────────────────────────────────
    l4_rows = lever4_coins(coin_data, best_adx, best_ob, best_os)
    best_coins_lbl, _ = best_row(l4_rows)
    best_coins = parse_coins(best_coins_lbl)
    best_coins = [c for c in best_coins if c in available]
    if not best_coins:
        best_coins = [c for c in ["BTC", "ETH"] if c in available]
    print(f"  → Lever 4 best: {best_coins_lbl}  → using coins: {best_coins}")

    # ── LEVER 5 ──────────────────────────────────────────────────────────────
    l5_rows = lever5_sl(coin_data, best_adx, best_ob, best_os, best_coins)
    best_sl_lbl, _ = best_row(l5_rows)
    best_sl = parse_sl(best_sl_lbl) if best_sl_lbl else 1.0
    print(f"  → Lever 5 best: {best_sl_lbl}  → using SL={best_sl}%")

    # ── LEVER 6 ──────────────────────────────────────────────────────────────
    l6_rows, trades_no_zone, trades_zone = lever6_zone(
        coin_data, best_adx, best_ob, best_os, best_coins, best_sl)

    # ── FINAL SUMMARY ────────────────────────────────────────────────────────
    print("\n" + "═"*94)
    print("  FINAL SUMMARY — OPTIMAL SETTINGS vs CURRENT LIVE CONFIG")
    print("═"*94)
    print(f"  Current live : ADX≥22 | 1D+6H+1H 3/3 | OB80/OS20 | BTC+ETH | SL 1.0%")
    print(f"                 BTC-LONG zone 25-55 | ETH-SHORT zone 45-75 | ETH-LONG zone 25-55")
    print()
    print(f"  Lever 1  Best ADX threshold : {best_adx_lbl}")
    b2, _ = best_row(l2_rows)
    print(f"  Lever 2  Best TF combo      : {b2}")
    print(f"  Lever 3  Best MFI levels    : {best_mfi_lbl}")
    print(f"  Lever 4  Best coin set      : {best_coins_lbl}")
    print(f"  Lever 5  Best SL            : {best_sl_lbl}")
    b6, _ = best_row(l6_rows)
    print(f"  Lever 6  Best zone config   : {b6}")
    print()

    # Optimal config performance
    best_zone_lbl, _ = best_row(l6_rows)
    use_zone = MFI_6H_ZONE_CURRENT if (best_zone_lbl and "zone" in best_zone_lbl.lower() and "no" not in best_zone_lbl.lower()) else None
    optimal_trades = run_backtest(
        coin_data, best_coins,
        adx_min=best_adx, tf_combo="1D+6H",
        mfi_ob=best_ob, mfi_os=best_os, sl_pct=best_sl,
        zone_filter_map=use_zone,
    )
    current_trades = run_backtest(
        coin_data, [c for c in ["BTC", "ETH"] if c in available],
        adx_min=22, tf_combo="1D+6H",
        mfi_ob=80, mfi_os=20, sl_pct=1.0,
        zone_filter_map=MFI_6H_ZONE_CURRENT,
    )

    opt_s = compute_stats(optimal_trades)
    cur_s = compute_stats(current_trades)

    print(f"  {'Config':<28}  {'Trades':>7}  {'WR%':>7}  {'PF':>7}  {'Mo.Trades':>10}  {'Mo.Return%':>12}")
    print("  " + "─"*80)

    def pf_str(s): return f"{s['pf']:.2f}" if s["pf"] != float("inf") else "∞"
    print(f"  {'Current live config':<28}  {cur_s['n']:>7}  {cur_s['wr']:>6.1f}%  {pf_str(cur_s):>7}  "
          f"{cur_s['monthly_trades']:>9.1f}  {cur_s['monthly_return']:>+11.2f}%")
    print(f"  {'Optimal config':<28}  {opt_s['n']:>7}  {opt_s['wr']:>6.1f}%  {pf_str(opt_s):>7}  "
          f"{opt_s['monthly_trades']:>9.1f}  {opt_s['monthly_return']:>+11.2f}%")
    print("═"*94)

    # ── DIAGNOSTICS ──────────────────────────────────────────────────────────
    print_diagnostics(coin_data, available)

    # ── TRADE LIST (primary 1D+6H, no ADX, OB80/OS20, BTC+ETH, SL1%) ────────
    primary_trades = run_backtest(
        coin_data, [c for c in ["BTC", "ETH"] if c in available],
        adx_min=None, tf_combo="1D+6H",
        mfi_ob=80, mfi_os=20, sl_pct=1.0,
        zone_filter_map=None,
    )
    print_trade_list(primary_trades, "Primary Config — 1D+6H | No ADX | OB80/OS20 | BTC+ETH | SL 1.0%")

    print("\nDone.")


def print_diagnostics(coin_data, available):
    """Print ADX history and signal density analysis for BTC as reference."""
    print("\n" + "═"*94)
    print("  DIAGNOSTICS — ADX History & Signal Density (BTC as reference)")
    print("═"*94)

    if "BTC" not in coin_data:
        print("  BTC data not available.")
        return

    d1d = coin_data["BTC"]["1d"].copy()
    d1d["adx"] = calc_adx(d1d)

    cutoff = pd.Timestamp(datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS))
    d1d_bt = d1d[d1d.index >= cutoff].copy()

    print(f"\n  BTC Daily ADX(14) — Last {LOOKBACK_DAYS} days")
    print(f"  {'Date':<12} {'Close':>10} {'ADX':>7}  {'Threshold?'}")
    for ts, row in d1d_bt.iterrows():
        adx_val = row["adx"]
        flags = []
        for thresh in [15, 18, 20, 22, 25]:
            if adx_val >= thresh:
                flags.append(f"≥{thresh}")
        flag_str = " ".join(flags) if flags else "below all"
        print(f"  {str(ts)[:10]:<12} {row['close']:>10.0f} {adx_val:>7.1f}  {flag_str}")

    print()
    # Days above each threshold
    print("  Days above ADX thresholds (of last 90 days):")
    for thresh in [None, 15, 18, 20, 22, 25]:
        if thresh is None:
            n = len(d1d_bt)
            print(f"    No filter: {n:>3} days ({n/len(d1d_bt)*100:.0f}%)")
        else:
            n = (d1d_bt["adx"] >= thresh).sum()
            print(f"    ADX ≥ {thresh:>2}: {n:>3} days ({n/len(d1d_bt)*100:.0f}%)")
    print()

    # Signal density per coin
    print("  Signal density — raw MFI crosses (in session, 90-day window):")
    print(f"  {'Coin':<6} {'OB80':>6} {'OS20':>6} {'OB75':>6} {'OS25':>6} {'OB70':>6} {'OS30':>6}")
    for coin in available:
        d1h = coin_data[coin]["1h"]
        bt_mask = d1h.index >= cutoff
        sm = pd.Series((d1h.index.hour >= SESSION_START) & (d1h.index.hour < SESSION_END), index=d1h.index)
        mfi = d1h["mfi"]
        mask = bt_mask & sm
        ob80 = ((mfi.shift(1) < 80) & (mfi >= 80) & mask).sum()
        os20 = ((mfi.shift(1) > 20) & (mfi <= 20) & mask).sum()
        ob75 = ((mfi.shift(1) < 75) & (mfi >= 75) & mask).sum()
        os25 = ((mfi.shift(1) > 25) & (mfi <= 25) & mask).sum()
        ob70 = ((mfi.shift(1) < 70) & (mfi >= 70) & mask).sum()
        os30 = ((mfi.shift(1) > 30) & (mfi <= 30) & mask).sum()
        print(f"  {coin:<6} {ob80:>6} {os20:>6} {ob75:>6} {os25:>6} {ob70:>6} {os30:>6}")
    print()

    # Direction availability
    print("  Direction availability (1D+6H agree, no ADX filter) per coin:")
    print(f"  {'Coin':<6} {'1D LONG':>8} {'1D SHORT':>9} {'6H LONG':>8} {'6H SHORT':>9} {'Agree L':>8} {'Agree S':>9}")
    for coin in available:
        d1h = coin_data[coin]["1h"]
        d6h = coin_data[coin]["6h"]
        d1d_c = coin_data[coin]["1d"]
        bt_mask = d1h.index >= cutoff
        d1d_dir = dir_series(d1d_c).reindex(d1h.index, method="ffill").fillna("FLAT")
        d6h_dir = dir_series(d6h).reindex(d1h.index, method="ffill").fillna("FLAT")
        m = bt_mask
        d1l = (d1d_dir[m] == "LONG").sum()
        d1s = (d1d_dir[m] == "SHORT").sum()
        d6l = (d6h_dir[m] == "LONG").sum()
        d6s = (d6h_dir[m] == "SHORT").sum()
        agl = ((d1d_dir[m] == "LONG")  & (d6h_dir[m] == "LONG")).sum()
        ags = ((d1d_dir[m] == "SHORT") & (d6h_dir[m] == "SHORT")).sum()
        print(f"  {coin:<6} {d1l:>8} {d1s:>9} {d6l:>8} {d6s:>9} {agl:>8} {ags:>9}  "
              f"(of {m.sum()} 1H candles)")
    print()


def print_trade_list(trades, label):
    """Print all individual trades."""
    if not trades:
        print(f"\n  {label}: No trades found.")
        return
    df = pd.DataFrame(trades).sort_values("entry_ts")
    s  = compute_stats(trades)
    print(f"\n{'═'*94}")
    print(f"  TRADE LIST: {label}")
    print(f"  Trades: {s['n']}  WR: {s['wr']:.1f}%  PF: {s['pf']:.2f}  "
          f"Avg: {s['avg_pnl']:+.3f}%  Total: {s['total_pnl']:+.2f}%")
    print(f"{'═'*94}")
    print(f"  {'#':>3}  {'Coin':<5} {'Date':<12} {'Time':>5} {'Dir':<6} {'Entry':>10} "
          f"{'MFI':>6} {'PnL%':>8}  Result")
    print("  " + "─"*82)
    for i, (_, row) in enumerate(df.iterrows(), 1):
        ts_str = str(row["entry_ts"])
        date   = ts_str[:10]
        hhmm   = ts_str[11:16]
        result = "WIN " if row["win"] else "LOSS"
        pnl    = row["pnl_pct"]
        marker = "✓" if row["win"] else "✗"
        print(f"  {i:>3}  {row['coin']:<5} {date:<12} {hhmm:>5} {row['direction']:<6} "
              f"{row['entry_price']:>10.2f} {row['mfi_entry']:>6.1f} {pnl:>+7.3f}%  {result} {marker}")
    print()


if __name__ == "__main__":
    main()
