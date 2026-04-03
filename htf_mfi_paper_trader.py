"""
Multi-TF MFI Scalper — Paper Trader
─────────────────────────────────────
v1.0  2026-03-22  Initial build
v1.1  2026-03-22  Exit logic: pure milestone trailing SL (no MFI exit, no fixed TP)
v1.2  2026-03-23  Added PAXG with per-symbol MFI thresholds (PAXG: OB75/OS25)
v1.3  2026-03-23  Two new entry guards:
                  1. Price location: 6H move must be < 3% in signal direction (no chasing)
                  2. 1H MFI safe zone: SHORT blocked if 1H MFI < 40 (already oversold);
                     LONG blocked if 1H MFI > 60 (already overbought)
v1.4  2026-03-23  6H MFI zone filter — per coin/direction, backtest-validated:
                  BTC  SHORT: no zone filter (momentum SHORTs profitable unfiltered)
                  BTC  LONG : 6H MFI must be 25–55
                  ETH  SHORT: 6H MFI must be 45–75
                  ETH  LONG : 6H MFI must be 25–55
                  PAXG SHORT: DISABLED (PF < 1 in backtest regardless of filter)
                  PAXG LONG : 6H MFI must be 25–55
v1.5  2026-03-27  Direction filter relaxed — 4 changes:
                  1. ADX_MIN: 22 → 18 (current market ADX 17-20 → was blocking all signals)
                  2. Direction now uses 6H + 1H only (dropped 1D requirement — 1D lags too
                     much in fast bear markets; 6H+1H captures intraday trend better)
                  3. Added SOL/USDT:USDT (higher ATR, strong MFI signals, ~2× more signals)
                  4. Removed 6H MFI zone filters (over-fitted to small backtest sample;
                     1H MFI safe zone + 6H move guard remain as primary quality filters)
v1.6  2026-03-28  A/B test concluded + trail tightened:
                  SL-1.5% variant RETIRED — on wins both variants exit identically (trail
                  takes over at +1%). On losses SL-1.5% costs 0.5% extra. NET: +4.1% vs +3.6%.
                  Trail gap 0.7% → 0.5% — live data: avg 0.976% left on table per trail win.
                  Gap 0.5% recovers ~+0.2%/win (avg exit +2.3% → +2.5%). All 5 trail wins confirm.
                  PAXG never returning: 0W/4L across all strategies. No edge, API issues.
v1.7  2026-04-01  A/B verdict REVERSED — live data (10 paired signals) overrides v1.6 theory:
                  SL-1.5% net +6.6% vs SL-1.0% net +4.0% on identical signals.
                  Root cause: 2 trades (SOL_0328, BTC_0328) stopped at -1.0% but recovered to
                  wins at SL-1.5% (+0.8% each). Wick depth exceeded SL-1.0% but not SL-1.5%.
                  The "equal wins" assumption was wrong — wider SL survives more false wicks.
                  SL-1.5% reinstated as sole variant. SL-1.0% retired.
v1.8  2026-04-03  Regime switching — BULL_MODE flag added for Oct 2026 bull market activation:
                  BEAR mode (default): BTC/DOT/SUI/WLD/INJ — SHORT-only signals
                  BULL mode (Oct 2026): BTC/SOL/DOGE/AVAX/BNB — LONG-only signals
                  Backtest basis (Nov 2023–Dec 2024 bull period):
                    LONG PF 1.34 in bull vs SHORT PF 0.96 → run LONG-only when bull confirmed
                    DOGE PF 4.22 | SOL PF inf (5tr) | BTC PF 2.00 | AVAX PF 1.89 | BNB PF 1.67
                    SUI PF 0.58 and XRP PF 0.40 for LONG — SHORT-biased coins, do not LONG
                  Activation: set BULL_MODE = True + deploy. All other params unchanged.

Multi-timeframe direction filter → 1M MFI entry

DIRECTION (checked every 30 min):
  6H + 1H must BOTH agree:
    SHORT → close < EMA20 + EMA20 slope negative (ADX ≥ 18 on 6H)
    LONG  → close > EMA20 + EMA20 slope positive (ADX ≥ 18 on 6H)
    FLAT  → any disagreement or 6H ADX < 18 → no trades

ENTRY (scanned every 1 min, only when direction ACTIVE):
  SHORT → 1M MFI(14) fresh cross ≥ 80  → SHORT at close
  LONG  → 1M MFI(14) fresh cross ≤ 20  → LONG  at close

EXIT (pure trailing SL — ride until stopped out):
  Hard SL   : 1.0% from entry (until +1% profit reached)
  +1.0% profit → SL moves to entry (BE)
  +1.5% profit → SL locks +0.8%
  +2.0% profit → SL locks +1.3%
  +2.5% profit → SL locks +1.8%
  +3.0% profit → SL locks +2.3%
  ... every +0.5% milestone: SL = milestone − 0.7%
  No fixed TP. No MFI exit. Ride until trailing SL is hit.

RE-ENTRY:
  After trade closes → wait for MFI to cross back through 50 before next signal

SESSION:
  07:00–22:00 UTC only

Dashboard: http://localhost:8085
Saves:     Trade Logs/htf_mfi_paper_trades.json
"""

import ccxt
import json
import os
import threading
import time
import logging
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
from flask import Flask, render_template_string, request, redirect
# API keys not needed — public candle data only

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────
# ── Regime switch — flip to True when BULL CONFIRMED (monthly MFI T3, ~Oct 2026) ──
BULL_MODE = False

# BEAR mode coin set (current — SHORT-biased, backtest 180d to Apr 2026)
BEAR_SYMBOLS = [
    'BTC/USDT:USDT',   # PF 1.17 | WR 37.5% SHORT — marginal, monitoring
    'DOT/USDT:USDT',   # PF 13.00 | WR 68.8% SHORT — standout
    'SUI/USDT:USDT',   # PF 3.67  | WR 57.1% SHORT — strong
    'WLD/USDT:USDT',   # PF 2.00  | WR 53.8% SHORT — solid
    'INJ/USDT:USDT',   # PF 1.80  | WR 52.9% SHORT — solid
]
BEAR_SYMBOL_MFI = {
    "BTC":  {"ob": 80, "os": 20},
    "DOT":  {"ob": 80, "os": 20},
    "SUI":  {"ob": 80, "os": 20},
    "WLD":  {"ob": 80, "os": 20},
    "INJ":  {"ob": 80, "os": 20},
}

# BULL mode coin set (activate Oct 2026 — LONG-biased, backtest Nov 2023–Dec 2024)
BULL_SYMBOLS = [
    'BTC/USDT:USDT',   # PF 2.00 | WR 50.0% LONG — works both modes
    'SOL/USDT:USDT',   # PF inf  | WR 80.0% LONG — 5-trade sample, monitor
    'DOGE/USDT:USDT',  # PF 4.22 | WR 54.5% LONG — strong
    'AVAX/USDT:USDT',  # PF 1.89 | WR 50.0% LONG — solid
    'BNB/USDT:USDT',   # PF 1.67 | WR 66.7% LONG — viable
]
BULL_SYMBOL_MFI = {
    "BTC":  {"ob": 80, "os": 20},
    "SOL":  {"ob": 80, "os": 20},
    "DOGE": {"ob": 80, "os": 20},
    "AVAX": {"ob": 80, "os": 20},
    "BNB":  {"ob": 80, "os": 20},
}

# Active config — derives from BULL_MODE flag
SYMBOLS    = BULL_SYMBOLS    if BULL_MODE else BEAR_SYMBOLS
SYMBOL_MFI = BULL_SYMBOL_MFI if BULL_MODE else BEAR_SYMBOL_MFI
ALLOWED_DIRECTION = "LONG" if BULL_MODE else "SHORT"   # gate in check_entry()

# Direction filter
EMA_PERIOD    = 20
EMA_SLOPE_BARS = 3      # compare EMA[now] vs EMA[N bars ago] for slope
ADX_PERIOD    = 14
ADX_MIN       = 18      # v1.5: was 22 — current market ADX 17-20, was blocking all signals

# MFI
MFI_PERIOD    = 14
MFI_OB        = 80      # default (overridden per-symbol by SYMBOL_MFI)
MFI_OS        = 20      # default (overridden per-symbol by SYMBOL_MFI)

# Entry guard — price location (v1.3)
MAX_6H_MOVE_PCT = 3.0   # max % price can have already moved from 6H open in signal direction

# Entry guard — 1H MFI safe zone (v1.3)
# SHORT: 1H MFI must be >= this (not already oversold — no room left to fall)
# LONG : 1H MFI must be <= this (not already overbought — no room left to rise)
MFI_1H_SHORT_MIN = 40   # SHORT blocked if 1H MFI below this
MFI_1H_LONG_MAX  = 60   # LONG  blocked if 1H MFI above this

# Entry guard — 6H MFI zone filter (v1.4 — REMOVED in v1.5)
# Zones were over-fitted to small backtest sample; removed to increase signal frequency.
# 1H MFI safe zone (guard 2) and 6H move guard (guard 1) remain as primary quality filters.
MFI_6H_ZONE = {}   # v1.5: empty = no zone restrictions applied

# Risk / Trailing SL — v1.7: SL-1.5% reinstated as sole variant (live data reversal)
# Paired 10-signal data: SL-1.5% net +6.6% vs SL-1.0% net +4.0%. SL-1.0% retired.
SL_VARIANTS  = {
    "SL-1.5%": 1.5,    # WINNER (v1.7) — survives wicks that SL-1.0% can't, identical wins
}
TRAIL_START      = 1.0   # profit % at which trailing begins (SL → BE)
TRAIL_STEP       = 0.5   # milestone increment
TRAIL_GAP        = 0.5   # v1.6: tightened 0.7% → 0.5% — live data: recovers +0.2%/win avg
# Result: +1.0%→BE | +1.5%→+1.0% | +2.0%→+1.5% | +2.5%→+2.0% | +3.0%→+2.5% ...

# Session (UTC hours)
SESSION_START    = 7
SESSION_END      = 22

# Peak hour windows (UTC) — higher volatility, larger moves expected
PEAK_SESSIONS = {
    "NY Open":     (13, 17),   # 13:00–17:00 UTC — highest volatility
    "London Open": ( 7, 10),   # 07:00–10:00 UTC — moderate
    "Asia Open":   ( 0,  4),   # 00:00–04:00 UTC — lower
}

# Sizing
BASE_TRADE_SIZE  = 500   # margin ($)
LEVERAGE         = 5     # notional = $2,500

# Scan intervals
DIRECTION_INTERVAL_MIN = 30    # how often to re-check direction
ENTRY_INTERVAL_SEC     = 60    # 1M candle = check every 60s
TEST_DURATION_DAYS     = 30

TRADE_LOG = "/opt/trader/Trade Logs/htf_mfi_paper_trades.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

exchange = ccxt.binanceusdm({
    "enableRateLimit": True,
    "timeout": 30000,
})

app = Flask(__name__)

# ── State ────────────────────────────────────────────────────────────────────
state = {
    "trades":      [],
    "open_trades": [],
    "last_scan":   "Not yet run",
    "scan_count":  0,
    "start_time":  datetime.now().strftime("%Y-%m-%d %H:%M"),
    "end_time":    (datetime.now() + timedelta(days=TEST_DURATION_DAYS)).strftime("%Y-%m-%d %H:%M"),
    "running":     True,
    "direction":   {},   # {'BTC': 'SHORT'|'LONG'|'FLAT', 'ETH': ...}
    "dir_detail":  {},   # debug info per coin
    "last_dir_check": "Not yet run",
}

# Per-symbol MFI tracking (for re-entry guard)
mfi_state = {
    sym.split("/")[0]: {
        "last_mfi":         50.0,
        "reset_needed":     False,   # True after close — wait for MFI to cross 50
        "reset_side":       None,    # 'above' or 'below' 50 needed to reset
    }
    for sym in SYMBOLS
}


# ─────────────────────────────────────────
# INDICATORS
# ─────────────────────────────────────────
def calc_ema(series, period):
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
    hi = df["high"]
    lo = df["low"]
    cl = df["close"]

    up   = hi.diff()
    down = lo.diff().mul(-1)

    plus_dm  = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)

    hl  = hi - lo
    hpc = (hi - cl.shift(1)).abs()
    lpc = (lo - cl.shift(1)).abs()
    tr  = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)

    alpha    = 1 / period
    atr      = tr.ewm(alpha=alpha, adjust=False).mean()
    plus_di  = 100 * plus_dm.ewm(alpha=alpha, adjust=False).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=alpha, adjust=False).mean() / atr

    denom = (plus_di + minus_di).replace(0, 1e-10)
    dx    = 100 * (plus_di - minus_di).abs() / denom
    adx   = dx.ewm(alpha=alpha, adjust=False).mean()
    return adx, plus_di, minus_di


# ─────────────────────────────────────────
# DATA
# ─────────────────────────────────────────
def fetch_candles(symbol, tf, limit=60):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, tf, limit=limit)
        df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        return df
    except Exception as e:
        log.warning(f"fetch {symbol} {tf}: {e}")
        return None


# ─────────────────────────────────────────
# DIRECTION CHECK (1D + 6H + 1H)
# ─────────────────────────────────────────
def _tf_direction(df, require_adx=False):
    """
    Returns ('LONG'|'SHORT'|'FLAT', detail_dict).
    """
    if df is None or len(df) < EMA_PERIOD + EMA_SLOPE_BARS + 5:
        return "FLAT", {"reason": "insufficient data"}

    df = df.copy()
    df["ema"] = calc_ema(df["close"], EMA_PERIOD)

    # Use last CLOSED candle (-2) not current live candle (-1)
    # Prevents intraday ADX/EMA flapping on partially-formed candles
    price     = df["close"].iloc[-2]
    ema_now   = df["ema"].iloc[-2]
    ema_prev  = df["ema"].iloc[-(2 + EMA_SLOPE_BARS)]
    slope_pos = ema_now > ema_prev
    above_ema = price > ema_now

    detail = {
        "price":    round(price, 4),
        "ema20":    round(ema_now, 4),
        "slope":    "up" if slope_pos else "down",
        "vs_ema":   "above" if above_ema else "below",
    }

    if require_adx:
        adx, pdi, mdi = calc_adx(df)
        adx_val = round(adx.iloc[-2], 1)  # last closed candle
        detail["adx"] = adx_val
        if adx_val < ADX_MIN:
            detail["reason"] = f"ADX {adx_val} < {ADX_MIN} (ranging)"
            return "FLAT", detail

    if above_ema and slope_pos:
        return "LONG", detail
    if not above_ema and not slope_pos:
        return "SHORT", detail

    detail["reason"] = "mixed (price vs slope disagree)"
    return "FLAT", detail


def check_direction(symbol):
    coin = symbol.split("/")[0]

    # v1.5: use 6H (with ADX) + 1H only — dropped 1D (lags too much in fast bear markets)
    df6h = fetch_candles(symbol, "6h", limit=60)
    df1h = fetch_candles(symbol, "1h", limit=60)

    dir6h, det6h = _tf_direction(df6h, require_adx=True)   # ADX check on 6H now
    dir1h, det1h = _tf_direction(df1h)

    detail = {
        "6H": {**det6h, "dir": dir6h},
        "1H": {**det1h, "dir": dir1h},
    }

    if dir6h == dir1h and dir6h != "FLAT":
        direction = dir6h
    else:
        direction = "FLAT"
        detail["reason"] = f"6H:{dir6h} 1H:{dir1h} — not aligned"

    state["direction"][coin]  = direction
    state["dir_detail"][coin] = detail
    log.info(f"Direction {coin}: {direction}  |  6H:{dir6h} 1H:{dir1h}  ADX:{det6h.get('adx','—')}")
    return direction


# ─────────────────────────────────────────
# ENTRY CHECK (1M)
# ─────────────────────────────────────────
def in_session():
    utc_hour = datetime.now(timezone.utc).hour
    return SESSION_START <= utc_hour < SESSION_END


def check_entry(symbol):
    """Returns trade dict if signal fires, else None."""
    coin      = symbol.split("/")[0]
    direction = state["direction"].get(coin, "FLAT")

    if direction == "FLAT" or not in_session():
        return None

    # Regime gate — only trade in the allowed direction for current market mode
    if direction != ALLOWED_DIRECTION:
        return None

    # Skip if any variant is still open for this coin
    open_variants = [t.get("variant") for t in state["open_trades"] if t["symbol"] == coin]
    if len(open_variants) >= len(SL_VARIANTS):
        return None

    df = fetch_candles(symbol, "1m", limit=30)
    if df is None or len(df) < MFI_PERIOD + 2:
        return None

    df["mfi"] = calc_mfi(df)

    mfi_now  = float(df["mfi"].iloc[-2])   # last closed 1M candle
    mfi_prev = float(df["mfi"].iloc[-3])   # one before that
    ms       = mfi_state[coin]

    # Reset guard: after close, wait for MFI to cross back through 50
    if ms["reset_needed"]:
        side = ms["reset_side"]
        if side == "above" and mfi_now > 50:
            ms["reset_needed"] = False
            log.info(f"  {coin} MFI reset (crossed above 50 after SHORT close)")
        elif side == "below" and mfi_now < 50:
            ms["reset_needed"] = False
            log.info(f"  {coin} MFI reset (crossed below 50 after LONG close)")
        else:
            ms["last_mfi"] = mfi_now
            return None

    ms["last_mfi"] = mfi_now
    entry = round(float(df["close"].iloc[-2]), 6)

    # Per-symbol MFI thresholds
    ob = SYMBOL_MFI.get(coin, {"ob": MFI_OB, "os": MFI_OS})["ob"]
    os = SYMBOL_MFI.get(coin, {"ob": MFI_OB, "os": MFI_OS})["os"]

    # ── Guard 1: Price location — don't chase an already-extended 6H move ────
    df6h = fetch_candles(symbol, "6h", limit=3)
    if df6h is not None and len(df6h) >= 2:
        c6h_open = float(df6h.iloc[-1]["open"])   # current open 6H candle
        move_pct = (entry - c6h_open) / c6h_open * 100  # + = up, − = down
        if direction == "SHORT" and move_pct < -MAX_6H_MOVE_PCT:
            log.info(f"  SKIP {coin} SHORT: 6H already down {move_pct:.1f}% (>{MAX_6H_MOVE_PCT}% threshold)")
            return None
        if direction == "LONG" and move_pct > MAX_6H_MOVE_PCT:
            log.info(f"  SKIP {coin} LONG: 6H already up {move_pct:.1f}% (>{MAX_6H_MOVE_PCT}% threshold)")
            return None

    # ── Guard 2: 1H MFI safe zone — block signals when HTF is at extreme ─────
    df1h = fetch_candles(symbol, "1h", limit=20)
    if df1h is not None and len(df1h) >= MFI_PERIOD + 2:
        df1h["mfi"] = calc_mfi(df1h)
        mfi_1h = float(df1h["mfi"].iloc[-2])
        if direction == "SHORT" and mfi_1h < MFI_1H_SHORT_MIN:
            log.info(f"  SKIP {coin} SHORT: 1H MFI {mfi_1h:.1f} < {MFI_1H_SHORT_MIN} (oversold on 1H, no room to fall)")
            return None
        if direction == "LONG" and mfi_1h > MFI_1H_LONG_MAX:
            log.info(f"  SKIP {coin} LONG: 1H MFI {mfi_1h:.1f} > {MFI_1H_LONG_MAX} (overbought on 1H, no room to rise)")
            return None

    # ── Guard 3: 6H MFI zone — per coin/direction, backtest-validated ─────────
    zone_cfg = MFI_6H_ZONE.get(coin, {})
    zone     = zone_cfg.get(direction)
    if zone is False:
        log.info(f"  SKIP {coin} {direction}: disabled for this coin/direction (backtest PF < 1)")
        return None
    if zone is not None:
        df6h_z = fetch_candles(symbol, "6h", limit=20)
        if df6h_z is not None and len(df6h_z) >= MFI_PERIOD + 2:
            df6h_z["mfi"] = calc_mfi(df6h_z)
            mfi_6h = float(df6h_z["mfi"].iloc[-2])  # last closed 6H candle
            lo, hi = zone
            if not (lo <= mfi_6h <= hi):
                log.info(f"  SKIP {coin} {direction}: 6H MFI {mfi_6h:.1f} outside zone {lo}–{hi}")
                return None

    # SHORT: MFI fresh cross above threshold
    if direction == "SHORT" and mfi_prev < ob <= mfi_now:
        return [
            _build_trade(coin, "SHORT", entry, round(entry * (1 + sl_pct / 100), 6), mfi_now, vname, sl_pct)
            for vname, sl_pct in SL_VARIANTS.items()
        ]

    # LONG: MFI fresh cross below threshold
    if direction == "LONG" and mfi_prev > os >= mfi_now:
        return [
            _build_trade(coin, "LONG", entry, round(entry * (1 - sl_pct / 100), 6), mfi_now, vname, sl_pct)
            for vname, sl_pct in SL_VARIANTS.items()
        ]

    return None


def _calc_trail_lock(peak_pct):
    """
    Returns the SL lock % based on peak profit milestone.
    +1.0% → 0.0% (BE) | +1.5% → +0.8% | +2.0% → +1.3% | every +0.5% after locks milestone−0.7%
    """
    if peak_pct < TRAIL_START:
        return None
    milestone = int(peak_pct / TRAIL_STEP) * TRAIL_STEP   # floor to nearest 0.5
    milestone = max(milestone, TRAIL_START)
    if milestone == TRAIL_START:
        return 0.0   # BE
    return round(milestone - TRAIL_GAP, 2)


def _get_session_label():
    h = datetime.now(timezone.utc).hour
    for name, (start, end) in PEAK_SESSIONS.items():
        if start <= h < end:
            return name
    return "Off-Peak"


def _build_trade(coin, direction, entry, sl, mfi_entry, variant, sl_pct):
    sign = "+" if direction == "SHORT" else "-"
    return {
        "id":             f"{coin}_{variant}_{datetime.now().strftime('%m%d_%H%M%S')}",
        "symbol":         coin,
        "variant":        variant,
        "session":        _get_session_label(),
        "direction":      direction,
        "entry":          entry,
        "sl":             sl,
        "sl_original":    sl,
        "sl_pct":         f"{sign}{sl_pct}%",
        "mfi_entry":      round(mfi_entry, 1),
        "status":         "OPEN",
        "opened_at":      datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "closed_at":      None,
        "result":         None,
        "pnl_pct":        None,
        "exit_price":     None,
        "exit_reason":    None,
        "position_usd":   BASE_TRADE_SIZE * LEVERAGE,
        "peak_pct":       0.0,
        "trail_lock_pct": None,
        "trail_note":     "",
        "current_price":  entry,
        "unrealised_pnl": 0.0,
        "direction_at_entry": state["direction"].get(coin, "?"),
    }


# ─────────────────────────────────────────
# TRADE MANAGEMENT (1M check)
# ─────────────────────────────────────────
def update_open_trades():
    for trade in state["open_trades"][:]:
        symbol    = trade["symbol"] + "/USDT:USDT"
        df        = fetch_candles(symbol, "1m", limit=20)
        if df is None or len(df) < 3:
            continue

        price     = float(df["close"].iloc[-1])
        lo_candle = float(df["low"].min())
        hi_candle = float(df["high"].max())
        direction = trade["direction"]
        entry     = trade["entry"]

        if direction == "LONG":
            pnl = (price - entry) / entry * 100
        else:
            pnl = (entry - price) / entry * 100

        # Track peak profit
        peak = max(trade.get("peak_pct", 0.0), pnl)
        trade["peak_pct"] = round(peak, 3)

        # ── Milestone trailing SL ──────────────────────────────────
        lock_pct = _calc_trail_lock(peak)
        if lock_pct is not None:
            if direction == "LONG":
                new_sl = round(entry * (1 + lock_pct / 100), 6)
                if new_sl > trade["sl"]:
                    old_lock = trade.get("trail_lock_pct")
                    trade["sl"]            = new_sl
                    trade["trail_lock_pct"] = lock_pct
                    if old_lock != lock_pct:
                        trade["trail_note"] = f"SL → +{lock_pct}% lock (peak +{peak:.2f}%)"
                        log.info(f"  TRAIL {trade['symbol']} LONG: SL locked +{lock_pct}% at peak +{peak:.2f}%")
            else:
                new_sl = round(entry * (1 - lock_pct / 100), 6)
                if new_sl < trade["sl"]:
                    old_lock = trade.get("trail_lock_pct")
                    trade["sl"]             = new_sl
                    trade["trail_lock_pct"] = lock_pct
                    if old_lock != lock_pct:
                        trade["trail_note"] = f"SL → +{lock_pct}% lock (peak +{peak:.2f}%)"
                        log.info(f"  TRAIL {trade['symbol']} SHORT: SL locked +{lock_pct}% at peak +{peak:.2f}%")

        # ── Check SL hit ───────────────────────────────────────────
        hit_sl = (hi_candle >= trade["sl"]) if direction == "SHORT" else (lo_candle <= trade["sl"])

        if hit_sl:
            exit_price = trade["sl"]
            if direction == "LONG":
                pnl_out = round((exit_price - entry) / entry * 100, 3)
            else:
                pnl_out = round((entry - exit_price) / entry * 100, 3)

            lock = trade.get("trail_lock_pct")
            if lock is not None and lock > 0:
                result = "WIN"
            elif lock == 0.0:
                result = "BE+"
            else:
                result = "LOSS"

            trade["status"]         = result
            trade["result"]         = result
            trade["closed_at"]      = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            trade["exit_price"]     = round(exit_price, 6)
            trade["pnl_pct"]        = pnl_out
            trade["exit_reason"]    = f"Trail SL (locked +{lock}%)" if lock is not None else "Hard SL"
            state["open_trades"].remove(trade)

            # Only set reset guard when the last variant for this coin closes
            remaining = [t for t in state["open_trades"] if t["symbol"] == trade["symbol"]]
            if not remaining:
                ms = mfi_state[trade["symbol"]]
                ms["reset_needed"] = True
                ms["reset_side"]   = "above" if direction == "SHORT" else "below"

            log.info(
                f"CLOSED {trade['symbol']} {direction} → {result} | "
                f"PnL:{pnl_out:.3f}% | Peak:+{peak:.2f}% | {trade['exit_reason']}"
            )
        else:
            trade["current_price"]  = round(price, 6)
            trade["unrealised_pnl"] = round(pnl, 3)

    save_trades()


# ─────────────────────────────────────────
# WORKERS
# ─────────────────────────────────────────
def direction_worker():
    """Runs every 60 min — updates direction for each symbol."""
    end_time = datetime.now() + timedelta(days=TEST_DURATION_DAYS)
    while datetime.now() < end_time and state["running"]:
        log.info("Direction check running...")
        for symbol in SYMBOLS:
            try:
                check_direction(symbol)
                time.sleep(0.5)
            except Exception as e:
                log.warning(f"Direction check error {symbol}: {e}")
        state["last_dir_check"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        time.sleep(DIRECTION_INTERVAL_MIN * 60)


def entry_worker():
    """Runs every 60s — checks 1M MFI for entries and manages open trades."""
    end_time = datetime.now() + timedelta(days=TEST_DURATION_DAYS)
    # Wait for first direction check to complete
    time.sleep(15)
    while datetime.now() < end_time and state["running"]:
        state["scan_count"] += 1
        state["last_scan"]   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Update open trades first
        if state["open_trades"]:
            update_open_trades()

        # Check for new entries
        if in_session():
            for symbol in SYMBOLS:
                try:
                    signals = check_entry(symbol)
                    if signals:
                        for sig in signals:
                            state["trades"].append(sig)
                            state["open_trades"].append(sig)
                            log.info(
                                f"NEW TRADE [{sig['variant']}]: {sig['symbol']} {sig['direction']} | "
                                f"Entry:{sig['entry']}  SL:{sig['sl']} ({sig['sl_pct']}) | "
                                f"MFI:{sig['mfi_entry']}"
                            )
                        save_trades()
                    time.sleep(0.3)
                except Exception as e:
                    log.warning(f"Entry check error {symbol}: {e}")
        else:
            log.info(f"Outside session (07:00–22:00 UTC) — skipping entry scan")

        time.sleep(ENTRY_INTERVAL_SEC)

    state["running"] = False
    log.info("HTF MFI paper trading complete.")


# ─────────────────────────────────────────
# PERSISTENCE
# ─────────────────────────────────────────
def save_trades():
    os.makedirs(os.path.dirname(TRADE_LOG), exist_ok=True)
    with open(TRADE_LOG, "w") as f:
        json.dump(state["trades"], f, indent=2, default=str)


def load_trades():
    if os.path.exists(TRADE_LOG):
        with open(TRADE_LOG) as f:
            state["trades"] = json.load(f)
        state["open_trades"] = [t for t in state["trades"] if t["status"] == "OPEN"]
        log.info(f"Loaded {len(state['trades'])} trades ({len(state['open_trades'])} open)")


# ─────────────────────────────────────────
# HTML DASHBOARD
# ─────────────────────────────────────────
HTML = r"""
<!DOCTYPE html>
<html>
<head>
    <title>Multi-TF MFI Scalper — Paper Trader</title>
    <meta http-equiv="refresh" content="60">
    <style>
        * { margin:0; padding:0; box-sizing:border-box; }
        body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
               background:#0d0d0d; color:#e0e0e0; padding:30px; }
        h1   { font-size:24px; color:#fff; margin-bottom:4px; }
        .sub { color:#666; font-size:13px; margin-bottom:22px; }

        .stats { display:flex; gap:14px; margin-bottom:24px; flex-wrap:wrap; }
        .stat-box { background:#1a1a1a; border:1px solid #2a2a2a; border-radius:10px;
                    padding:14px 20px; min-width:130px; }
        .stat-box .label { font-size:11px; color:#555; text-transform:uppercase; letter-spacing:1px; }
        .stat-box .value { font-size:20px; font-weight:700; color:#fff; margin-top:4px; }
        .green .value { color:#00c853; }
        .red   .value { color:#ff1744; }
        .blue  .value { color:#1e88e5; }
        .gold  .value { color:#ffd600; }
        .beplus .value { color:#aaff00; }

        h2 { font-size:15px; color:#aaa; margin:26px 0 12px; text-transform:uppercase; letter-spacing:1px; }

        /* Direction panel */
        .dir-panel { display:flex; gap:16px; margin-bottom:24px; flex-wrap:wrap; }
        .dir-card  { background:#141414; border:1px solid #2a2a2a; border-radius:12px;
                     padding:16px 22px; flex:1; min-width:280px; }
        .dir-card h3 { font-size:13px; color:#aaa; margin-bottom:12px; text-transform:uppercase; letter-spacing:1px; }
        .dir-badge-lg { display:inline-block; padding:6px 18px; border-radius:8px; font-size:16px;
                        font-weight:700; margin-bottom:12px; }
        .dir-long-lg  { background:#0a2a10; color:#00c853; border:1px solid #00c853; }
        .dir-short-lg { background:#2a1000; color:#ff6f00; border:1px solid #ff6f00; }
        .dir-flat-lg  { background:#1a1a1a; color:#555;   border:1px solid #333; }
        .dir-row { font-size:12px; color:#666; line-height:2; }
        .dir-row span { color:#ccc; font-weight:600; margin-left:6px; }
        .dir-tf { font-size:10px; color:#444; text-transform:uppercase; letter-spacing:1px; margin-right:6px; }
        .dir-agree { color:#00c853; font-weight:700; }
        .dir-disagree { color:#ff1744; font-weight:700; }

        table { width:100%; border-collapse:collapse; background:#1a1a1a;
                border-radius:10px; overflow:hidden; margin-bottom:28px; }
        th { background:#222; padding:11px 14px; text-align:left; font-size:11px;
             color:#666; text-transform:uppercase; letter-spacing:1px; }
        td { padding:11px 14px; border-top:1px solid #222; font-size:13px; }
        tr:hover td { background:#1e1e1e; }

        .coin    { font-weight:700; color:#fff; font-size:15px; }
        .pnl-pos { color:#00c853; font-weight:600; }
        .pnl-neg { color:#ff1744; font-weight:600; }
        .usd-pos { color:#00c853; font-weight:600; font-size:12px; }
        .usd-neg { color:#ff1744; font-weight:600; font-size:12px; }
        .sl-val  { color:#ff6f00; }
        .mfi-ob  { color:#ff1744; font-weight:700; }
        .mfi-os  { color:#00c853; font-weight:700; }
        .mfi-mid { color:#aaa; }
        .tv-link { color:#1e88e5; text-decoration:none; font-size:12px; }

        .badge { display:inline-block; padding:3px 9px; border-radius:20px; font-size:11px; font-weight:600; }
        .badge-open   { background:#1e3a5f; color:#1e88e5; }
        .badge-win    { background:#1b3a2a; color:#00c853; }
        .badge-loss   { background:#3a1b1b; color:#ff1744; }
        .badge-manual { background:#2a2a00; color:#ffd600; }
        .badge-be\+   { background:#1a2a00; color:#aaff00; }

        .dir-badge { display:inline-block; padding:3px 9px; border-radius:6px; font-size:11px; font-weight:700; }
        .dir-long  { background:#0a2a10; color:#00c853; border:1px solid #00c853; }
        .dir-short { background:#2a1000; color:#ff6f00; border:1px solid #ff6f00; }
        .dir-flat  { background:#1a1a1a; color:#555; border:1px solid #333; }

        .trail-badge { display:inline-block; padding:2px 7px; border-radius:5px; font-size:10px; font-weight:600; }
        .trail-0 { color:#555; }
        .trail-1 { background:#001a0d; color:#00c853; border:1px solid #00c853; }

        .btn-close { background:#1a0000; color:#ff5252; border:1px solid #ff1744;
                     padding:4px 10px; border-radius:6px; font-size:11px; font-weight:600; cursor:pointer; }
        .btn-close:hover { background:#ff1744; color:#fff; }

        .running-dot { display:inline-block; width:8px; height:8px; background:#00c853;
                       border-radius:50%; margin-right:6px; animation:pulse 1.5s infinite; }
        @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
        .empty { text-align:center; padding:40px; color:#444; }
        .session-badge { display:inline-block; padding:3px 10px; border-radius:12px; font-size:11px; font-weight:600; }
        .session-on  { background:#001a0d; color:#00c853; border:1px solid #00c853; }
        .session-off { background:#1a1a1a; color:#555;   border:1px solid #333; }

        /* ── A/B Variant panel ───────────────────────── */
        .ab-panel { display:flex; gap:16px; margin-bottom:24px; flex-wrap:wrap; }
        .ab-card  { background:#141414; border:1px solid #2a2a2a; border-radius:12px;
                    padding:16px 22px; flex:1; min-width:220px; }
        .ab-card h3 { font-size:12px; color:#888; text-transform:uppercase; letter-spacing:1px; margin-bottom:12px; }
        .ab-row { display:flex; justify-content:space-between; font-size:12px; color:#666; line-height:2.2; border-bottom:1px solid #1e1e1e; }
        .ab-row:last-child { border-bottom:none; }
        .ab-val { color:#fff; font-weight:600; }
        .ab-win  { color:#00c853; font-weight:700; }
        .ab-loss { color:#ff1744; font-weight:700; }
        .ab-pf-good { color:#00c853; font-weight:700; }
        .ab-pf-bad  { color:#ff1744; font-weight:700; }
        .variant-badge { display:inline-block; padding:2px 8px; border-radius:5px; font-size:10px; font-weight:700; }
        .variant-a { background:#001a2a; color:#40c4ff; border:1px solid #1e88e5; }
        .variant-b { background:#1a0040; color:#b57bee; border:1px solid #7c4dff; }
        .sess-badge { display:inline-block; padding:2px 7px; border-radius:5px; font-size:10px; font-weight:600; }
        .sess-ny     { background:#1a0a00; color:#ff9800; border:1px solid #ff9800; }
        .sess-london { background:#001a0d; color:#00c853; border:1px solid #00c853; }
        .sess-asia   { background:#00001a; color:#40c4ff; border:1px solid #1e88e5; }
        .sess-off    { background:#1a1a1a; color:#555;   border:1px solid #333; }
        .insights-box { background:#0d1a0d; border:1px solid #1b5e20; border-radius:10px;
                        padding:18px 22px; margin-bottom:24px; }
        .insights-box h3 { color:#69f0ae; font-size:15px; margin-bottom:14px; }
        .insights-meta { display:flex; gap:24px; margin-bottom:14px; flex-wrap:wrap; }
        .insights-meta span { font-size:12px; color:#aaa; }
        .insights-meta strong { color:#fff; }
        .rec { display:flex; gap:10px; align-items:flex-start; margin-bottom:8px; font-size:13px; }
        .rec-dot { width:8px; height:8px; border-radius:50%; margin-top:4px; flex-shrink:0; }
        .rec-dot.green  { background:#00c853; }
        .rec-dot.yellow { background:#ffd600; }
        .rec-dot.red    { background:#ff1744; }
        .rec-text { color:#ccc; line-height:1.5; }
        .insights-grid { display:flex; gap:20px; margin-top:14px; flex-wrap:wrap; }
        .ig-box { background:#0a0a0a; border:1px solid #1e2a1e; border-radius:8px;
                  padding:12px 16px; min-width:160px; flex:1; }
        .ig-box h4 { font-size:11px; color:#555; margin-bottom:8px; text-transform:uppercase; letter-spacing:1px; }
        .ig-row { display:flex; justify-content:space-between; font-size:12px;
                  color:#aaa; padding:3px 0; border-bottom:1px solid #111; }
        .ig-row:last-child { border-bottom:none; }
        .ig-row span { color:#e0e0e0; font-weight:600; }
    </style>
</head>
<body>
    <h1>
        {% if running %}<span class="running-dot"></span>{% endif %}
        Multi-TF MFI Scalper <span style="color:#ffd600;font-size:18px;">— Paper Trader</span>
        <span style="color:#555;font-size:14px;margin-left:12px;">v1.5</span>
    </h1>
    <p class="sub">
        Binance USDT Futures · BTC · ETH · SOL · Direction: 6H+1H EMA20+ADX≥18 · Entry: 1M MFI ·
        {{ start_time }} → {{ end_time }} &nbsp;·&nbsp;
        <span class="session-badge {{ 'session-on' if in_session else 'session-off' }}">
            {{ '🟢 Session Active' if in_session else '⚪ Off Session' }}
        </span>
        &nbsp;·&nbsp; <span style="color:#555;">Scan #{{ scan_count }}</span>
    </p>

    <!-- Direction Cards -->
    <div class="dir-panel">
        {% for coin, detail in dir_detail.items() %}
        <div class="dir-card">
            <h3>{{ coin }} — Direction</h3>
            {% set d = direction.get(coin, 'FLAT') %}
            {% if d == 'LONG' %}
                <span class="dir-badge-lg dir-long-lg">↑ LONG</span>
            {% elif d == 'SHORT' %}
                <span class="dir-badge-lg dir-short-lg">↓ SHORT</span>
            {% else %}
                <span class="dir-badge-lg dir-flat-lg">— FLAT</span>
            {% endif %}
            <div class="dir-row">
                {% for tf in ['6H','1H'] %}
                {% set td = detail.get(tf, {}) %}
                <div>
                    <span class="dir-tf">{{ tf }}</span>
                    <span class="{{ 'dir-agree' if td.get('dir') == d and d != 'FLAT' else 'dir-disagree' }}">{{ td.get('dir','—') }}</span>
                    &nbsp; price {{ td.get('vs_ema','—') }} EMA20
                    &nbsp; slope {{ td.get('slope','—') }}
                    {% if td.get('adx') %}&nbsp; ADX {{ td.get('adx') }}{% endif %}
                </div>
                {% endfor %}
                {% if detail.get('reason') %}
                <div style="color:#444;font-size:11px;margin-top:4px;">{{ detail.get('reason') }}</div>
                {% endif %}
            </div>
        </div>
        {% endfor %}
    </div>

    <!-- A/B Variant Comparison -->
    <div class="ab-panel">
        {% for vname, vstats in variant_stats.items() %}
        <div class="ab-card">
            <h3>
                <span class="variant-badge {{ 'variant-a' if loop.index == 1 else 'variant-b' }}">{{ vname }}</span>
                &nbsp; SL Variant Results
            </h3>
            <div class="ab-row"><span>Trades closed</span><span class="ab-val">{{ vstats.total }}</span></div>
            <div class="ab-row">
                <span>W / BE+ / L</span>
                <span>
                    <span class="ab-win">{{ vstats.wins }}</span> /
                    <span style="color:#aaff00;">{{ vstats.beplus }}</span> /
                    <span class="ab-loss">{{ vstats.losses }}</span>
                </span>
            </div>
            <div class="ab-row"><span>Win+BE+ Rate</span><span class="ab-val {{ 'ab-win' if vstats.wr >= 50 else 'ab-loss' }}">{{ vstats.wr }}%</span></div>
            <div class="ab-row"><span>Profit Factor</span><span class="ab-val {{ 'ab-pf-good' if vstats.pf >= 1 else 'ab-pf-bad' }}">{{ vstats.pf }}</span></div>
            <div class="ab-row"><span>Net PnL $</span><span class="ab-val {{ 'ab-win' if vstats.net >= 0 else 'ab-loss' }}">{{ '+' if vstats.net >= 0 else '' }}${{ "{:,.0f}".format(vstats.net) }}</span></div>
            <div class="ab-row"><span>Avg PnL %</span><span class="ab-val {{ 'ab-win' if vstats.avg_pnl >= 0 else 'ab-loss' }}">{{ '+' if vstats.avg_pnl >= 0 else '' }}{{ vstats.avg_pnl }}%</span></div>
        </div>
        {% endfor %}

        <!-- Session breakdown -->
        <div class="ab-card">
            <h3>Session Breakdown</h3>
            {% for sname, sstats in session_stats.items() %}
            <div class="ab-row">
                <span>
                    {% if 'NY' in sname %}<span class="sess-badge sess-ny">{{ sname }}</span>
                    {% elif 'London' in sname %}<span class="sess-badge sess-london">{{ sname }}</span>
                    {% elif 'Asia' in sname %}<span class="sess-badge sess-asia">{{ sname }}</span>
                    {% else %}<span class="sess-badge sess-off">{{ sname }}</span>{% endif %}
                </span>
                <span class="ab-val">
                    {{ sstats.total }} trades &nbsp;
                    <span class="{{ 'ab-win' if sstats.wr >= 50 else 'ab-loss' }}">{{ sstats.wr }}% W+BE+</span>
                </span>
            </div>
            {% endfor %}
            {% if not session_stats %}
            <div style="color:#444;font-size:12px;padding-top:8px;">No closed trades yet</div>
            {% endif %}
        </div>
    </div>

    {% if insights %}
    <div class="insights-box">
        <h3>🧠 Learning Insights — Milestone {{ insights.milestone }} Trades (HTF MFI)</h3>
        <div class="insights-meta">
            <span>Trades: <strong>{{ insights.total }}</strong></span>
            <span>Win+BE rate: <strong>{{ insights.win_rate }}%</strong></span>
            <span>Profit factor: <strong>{{ insights.pf if insights.pf else 'n/a' }}</strong></span>
            <span>Avg win: <strong>+{{ insights.avg_win }}%</strong></span>
            <span>Avg loss: <strong>{{ insights.avg_loss }}%</strong></span>
        </div>
        {% for color, text in insights.recs %}
        <div class="rec">
            <div class="rec-dot {{ color }}"></div>
            <div class="rec-text">{{ text }}</div>
        </div>
        {% endfor %}
        <div class="insights-grid">
            <div class="ig-box">
                <h4>A/B Variant Win Rate</h4>
                {% for vname, data in insights.variant_insights.items() %}
                {% if data[1] > 0 %}
                <div class="ig-row">{{ vname }} ({{ data[1] }} trades)<span>{{ data[0] }}%</span></div>
                {% endif %}
                {% endfor %}
            </div>
            <div class="ig-box">
                <h4>Session Breakdown</h4>
                {% for sname, s in insights.session_stats.items() %}
                {% set total = s.w + s.be + s.l %}
                {% if total > 0 %}
                <div class="ig-row">
                    {{ sname }}
                    <span>{{ s.w }}W {{ s.be }}BE {{ s.l }}L
                    ({{ ((s.w + s.be) / total * 100) | round | int }}%)</span>
                </div>
                {% endif %}
                {% endfor %}
            </div>
        </div>
    </div>
    {% endif %}

    <!-- Stats -->
    <div class="stats">
        <div class="stat-box blue">
            <div class="label">Open</div>
            <div class="value">{{ open_trades | length }}</div>
        </div>
        <div class="stat-box green">
            <div class="label">Wins ✅</div>
            <div class="value">{{ wins }}</div>
        </div>
        <div class="stat-box beplus">
            <div class="label">BE+ 🟡</div>
            <div class="value">{{ beplus }}</div>
        </div>
        <div class="stat-box red">
            <div class="label">Losses ❌</div>
            <div class="value">{{ losses }}</div>
        </div>
        <div class="stat-box {{ 'green' if win_rate >= 50 else 'red' }}">
            <div class="label">Win+BE+ %</div>
            <div class="value">{{ win_rate }}%</div>
        </div>
        <div class="stat-box {{ 'green' if profit_factor >= 1 else 'red' }}">
            <div class="label">Profit Factor</div>
            <div class="value">{{ profit_factor }}</div>
        </div>
        <div class="stat-box {{ 'green' if total_pnl_usd >= 0 else 'red' }}">
            <div class="label">Realised PnL $</div>
            <div class="value">{{ '+' if total_pnl_usd >= 0 else '' }}${{ "{:,.0f}".format(total_pnl_usd) }}</div>
        </div>
        <div class="stat-box {{ 'green' if unrealised_usd >= 0 else 'red' }}">
            <div class="label">Unrealised $</div>
            <div class="value">{{ '+' if unrealised_usd >= 0 else '' }}${{ "{:,.0f}".format(unrealised_usd) }}</div>
        </div>
        <div class="stat-box">
            <div class="label">Last Dir Check</div>
            <div class="value" style="font-size:10px;margin-top:8px;">{{ last_dir_check }}</div>
        </div>
        <div class="stat-box">
            <div class="label">Last Entry Scan</div>
            <div class="value" style="font-size:10px;margin-top:8px;">{{ last_scan }}</div>
        </div>
    </div>

    {% if open_trades %}
    <h2>🔵 Open Trades ({{ open_trades | length }})</h2>
    <table>
        <thead><tr>
            <th>Coin</th><th>Variant</th><th>Session</th><th>Dir</th><th>Entry</th><th>Current SL</th>
            <th>SL Locked</th><th>Peak %</th><th>Entry MFI</th>
            <th>Unrealised %</th><th>USD PnL</th>
            <th>Opened</th><th>Chart</th><th>Action</th>
        </tr></thead>
        <tbody>
        {% for t in open_trades %}
        <tr>
            <td class="coin">{{ t.symbol }}</td>
            <td><span class="variant-badge {{ 'variant-a' if t.variant == 'SL-1.0%' else 'variant-b' }}">{{ t.variant }}</span></td>
            <td>
                {% set s = t.session or 'Off-Peak' %}
                {% if 'NY' in s %}<span class="sess-badge sess-ny">{{ s }}</span>
                {% elif 'London' in s %}<span class="sess-badge sess-london">{{ s }}</span>
                {% elif 'Asia' in s %}<span class="sess-badge sess-asia">{{ s }}</span>
                {% else %}<span class="sess-badge sess-off">{{ s }}</span>{% endif %}
            </td>
            <td>
                {% if t.direction == 'LONG' %}
                    <span class="dir-badge dir-long">↑ LONG</span>
                {% else %}
                    <span class="dir-badge dir-short">↓ SHORT</span>
                {% endif %}
            </td>
            <td>{{ t.entry }}</td>
            <td class="sl-val">{{ t.sl }}</td>
            <td>
                {% set lock = t.trail_lock_pct %}
                {% if lock is none %}
                    <span class="trail-badge trail-0">Hard −{{ sl_pct }}%</span>
                {% elif lock == 0 %}
                    <span class="trail-badge trail-1">✅ BE</span>
                {% else %}
                    <span class="trail-badge trail-2">🔒 +{{ lock }}%</span>
                {% endif %}
            </td>
            <td class="pnl-pos">+{{ t.peak_pct or '0.0' }}%</td>
            <td class="{{ 'mfi-ob' if t.mfi_entry >= 75 else ('mfi-os' if t.mfi_entry <= 25 else 'mfi-mid') }}">
                {{ t.mfi_entry }}
            </td>
            <td class="{{ 'pnl-pos' if (t.unrealised_pnl or 0) >= 0 else 'pnl-neg' }}">
                {{ '+' if (t.unrealised_pnl or 0) >= 0 else '' }}{{ t.unrealised_pnl or '—' }}%
            </td>
            <td class="{{ 'usd-pos' if (t.unrealised_pnl or 0) >= 0 else 'usd-neg' }}">
                {{ '+' if (t.unrealised_pnl or 0) >= 0 else '' }}${{ "{:,.0f}".format((t.unrealised_pnl or 0) / 100 * (t.position_usd or (base_size * leverage))) }}
            </td>
            <td style="color:#555;font-size:11px;">{{ t.opened_at }}</td>
            <td>
                <a class="tv-link" href="https://www.tradingview.com/chart/?symbol=BINANCE:{{ t.symbol }}USDT.P&interval=1" target="_blank">1M →</a>
            </td>
            <td>
                <form method="POST" action="/close/{{ t.id }}" style="display:inline;margin:0;">
                    <button class="btn-close" type="submit" onclick="return confirm('Close {{ t.symbol }}?')">Close</button>
                </form>
            </td>
        </tr>
        {% endfor %}
        </tbody>
    </table>
    {% endif %}

    {% if closed_trades %}
    <h2>📋 Closed Trades ({{ closed_trades | length }})</h2>
    <table>
        <thead><tr>
            <th>Coin</th><th>Variant</th><th>Session</th><th>Dir</th><th>Result</th>
            <th>Entry</th><th>Exit</th><th>PnL %</th><th>PnL $</th><th>Peak %</th>
            <th>MFI Entry</th><th>SL Lock</th><th>Exit Reason</th>
            <th>Opened</th><th>Closed</th>
        </tr></thead>
        <tbody>
        {% for t in closed_trades | reverse %}
        <tr>
            <td class="coin">{{ t.symbol }}</td>
            <td><span class="variant-badge {{ 'variant-a' if t.variant == 'SL-1.0%' else 'variant-b' }}">{{ t.variant or '—' }}</span></td>
            <td>
                {% set s = t.session or 'Off-Peak' %}
                {% if 'NY' in s %}<span class="sess-badge sess-ny">{{ s }}</span>
                {% elif 'London' in s %}<span class="sess-badge sess-london">{{ s }}</span>
                {% elif 'Asia' in s %}<span class="sess-badge sess-asia">{{ s }}</span>
                {% else %}<span class="sess-badge sess-off">{{ s }}</span>{% endif %}
            </td>
            <td>
                {% if t.direction == 'LONG' %}
                    <span class="dir-badge dir-long">↑ L</span>
                {% else %}
                    <span class="dir-badge dir-short">↓ S</span>
                {% endif %}
            </td>
            <td><span class="badge badge-{{ 'manual' if t.result == 'MANUAL' else t.result | lower | replace('+','plus') }}">{{ t.result }}</span></td>
            <td>{{ t.entry }}</td>
            <td>{{ t.exit_price }}</td>
            <td class="{{ 'pnl-pos' if (t.pnl_pct or 0) >= 0 else 'pnl-neg' }}">
                {{ '+' if (t.pnl_pct or 0) >= 0 else '' }}{{ t.pnl_pct }}%
            </td>
            <td class="{{ 'usd-pos' if (t.pnl_pct or 0) >= 0 else 'usd-neg' }}">
                {{ '+' if (t.pnl_pct or 0) >= 0 else '' }}${{ "{:,.0f}".format((t.pnl_pct or 0) / 100 * (t.position_usd or (base_size * leverage))) }}
            </td>
            <td class="pnl-pos">+{{ t.peak_pct or '0.0' }}%</td>
            <td class="{{ 'mfi-ob' if (t.mfi_entry or 50) >= 75 else ('mfi-os' if (t.mfi_entry or 50) <= 25 else 'mfi-mid') }}">
                {{ t.mfi_entry or '—' }}
            </td>
            <td style="font-size:11px;color:#00c853;">
                {% if t.trail_lock_pct is not none %}+{{ t.trail_lock_pct }}%{% else %}none{% endif %}
            </td>
            <td style="font-size:11px;color:#aaa;">{{ t.exit_reason or '—' }}</td>
            <td style="color:#555;font-size:11px;">{{ t.opened_at }}</td>
            <td style="color:#555;font-size:11px;">{{ t.closed_at }}</td>
        </tr>
        {% endfor %}
        </tbody>
    </table>
    {% endif %}

    {% if not open_trades and not closed_trades %}
    <div class="empty">⏳ Waiting for first HTF-aligned MFI signal... Refreshes every 60s.</div>
    {% endif %}

</body>
</html>
"""


# ─────────────────────────────────────────
# FLASK ROUTES
# ─────────────────────────────────────────
@app.route("/close/<trade_id>", methods=["POST"])
def close_trade(trade_id):
    trade = next((t for t in state["open_trades"] if t["id"] == trade_id), None)
    if not trade:
        return "Trade not found", 404
    try:
        symbol     = trade["symbol"] + "/USDT:USDT"
        ticker     = exchange.fetch_ticker(symbol)
        exit_price = round(ticker["last"], 6)
        direction  = trade["direction"]
        if direction == "LONG":
            pnl = round((exit_price - trade["entry"]) / trade["entry"] * 100, 3)
        else:
            pnl = round((trade["entry"] - exit_price) / trade["entry"] * 100, 3)
        result = "WIN" if pnl > 0.1 else ("BE+" if pnl >= 0 else "LOSS")
        trade.update({
            "status":      "MANUAL",
            "result":      result,
            "closed_at":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "exit_price":  exit_price,
            "pnl_pct":     pnl,
            "exit_reason": "MANUAL",
        })
        state["open_trades"].remove(trade)
        save_trades()
        log.info(f"MANUAL CLOSE: {trade['symbol']} | Exit:{exit_price} | PnL:{pnl}% → {result}")
    except Exception as e:
        log.warning(f"Manual close failed: {e}")
    return redirect("/")


def generate_insights(closed):
    n = len(closed)
    if n < 3:
        return None
    milestone = (n // 3) * 3
    wins   = [t for t in closed if t.get("result") == "WIN"]
    bes    = [t for t in closed if t.get("result") == "BE+"]
    losses = [t for t in closed if t.get("result") == "LOSS"]
    decided = len(wins) + len(bes) + len(losses)
    win_rate = round((len(wins) + len(bes)) / decided * 100, 1) if decided else 0

    pos_usd = BASE_TRADE_SIZE * LEVERAGE
    gross_win  = sum((t.get("pnl_pct") or 0) / 100 * pos_usd for t in wins + bes)
    gross_loss = sum(abs(t.get("pnl_pct") or 0) / 100 * pos_usd for t in losses)
    pf = round(gross_win / gross_loss, 2) if gross_loss > 0 else None

    avg_win  = round(sum(t.get("pnl_pct", 0) or 0 for t in wins)   / len(wins),   2) if wins   else 0
    avg_loss = round(sum(t.get("pnl_pct", 0) or 0 for t in losses)  / len(losses), 2) if losses else 0

    # A/B variant comparison
    def variant_wr(vname):
        vt = [t for t in closed if t.get("variant") == vname]
        if not vt: return None, 0
        pos = sum(1 for t in vt if t.get("result") in ("WIN", "BE+"))
        return round(pos / len(vt) * 100), len(vt)

    variant_insights = {v: variant_wr(v) for v in SL_VARIANTS}

    # Session breakdown
    session_stats = {}
    for t in closed:
        s = t.get("session") or "Off-Peak"
        if s not in session_stats:
            session_stats[s] = {"w": 0, "be": 0, "l": 0}
        r = t.get("result", "")
        if r == "WIN":   session_stats[s]["w"]  += 1
        elif r == "BE+": session_stats[s]["be"] += 1
        else:            session_stats[s]["l"]  += 1

    # MFI level at entry
    mfi_low  = [t for t in closed if 80 <= (t.get("mfi_entry") or 0) < 85]
    mfi_high = [t for t in closed if (t.get("mfi_entry") or 0) >= 85]
    def mfi_wr(bucket):
        if not bucket: return None
        return round(sum(1 for t in bucket if t.get("result") in ("WIN","BE+")) / len(bucket) * 100)

    recs = []
    if win_rate >= 70:
        recs.append(("green",  f"Win+BE rate {win_rate}% — HTF filter working well."))
    elif win_rate >= 50:
        recs.append(("yellow", f"Win+BE rate {win_rate}% — acceptable. Direction filter may need tightening."))
    else:
        recs.append(("red",    f"Win+BE rate {win_rate}% — below 50%. ADX threshold or MFI level may need adjusting."))

    if pf and pf >= 2.0:
        recs.append(("green",  f"Profit factor {pf} — trailing SL riding moves well."))
    elif pf and pf < 1.0:
        recs.append(("red",    f"Profit factor {pf} — losses outweighing wins. Trail may be too tight."))

    # A/B recommendation
    vnames = list(SL_VARIANTS.keys())
    if len(vnames) == 2:
        wr_a, n_a = variant_insights[vnames[0]]
        wr_b, n_b = variant_insights[vnames[1]]
        if wr_a is not None and wr_b is not None and n_a >= 3 and n_b >= 3:
            if wr_a > wr_b + 10:
                recs.append(("green",  f"{vnames[0]} win rate {wr_a}% vs {vnames[1]} {wr_b}% — tighter SL winning. Consider standardising to {vnames[0]}."))
            elif wr_b > wr_a + 10:
                recs.append(("green",  f"{vnames[1]} win rate {wr_b}% vs {vnames[0]} {wr_a}% — wider SL winning. Consider standardising to {vnames[1]}."))
            else:
                recs.append(("yellow", f"A/B variants similar ({vnames[0]}: {wr_a}% vs {vnames[1]}: {wr_b}%) — keep testing."))

    # Session insights
    for sname, s in session_stats.items():
        total = s["w"] + s["be"] + s["l"]
        if total < 2: continue
        swr = round((s["w"] + s["be"]) / total * 100)
        if swr == 0:
            recs.append(("red",    f"{sname}: 0% win rate ({total} trades) — avoid this session."))
        elif swr >= 80:
            recs.append(("green",  f"{sname}: {swr}% win rate — best session, prioritise it."))

    # MFI level
    wr_low  = mfi_wr(mfi_low)
    wr_high = mfi_wr(mfi_high)
    if wr_low is not None and wr_high is not None and wr_high > wr_low + 20:
        recs.append(("yellow", f"MFI 85+ entries win rate {wr_high}% vs MFI 80-85 {wr_low}% — consider raising 1M MFI threshold."))

    return {
        "milestone": milestone, "total": n, "win_rate": win_rate, "pf": pf,
        "avg_win": avg_win, "avg_loss": avg_loss, "recs": recs,
        "session_stats": session_stats, "variant_insights": variant_insights,
    }


@app.route("/")
def index():
    from datetime import timezone as tz
    utc_h      = datetime.now(tz.utc).hour
    session_on = SESSION_START <= utc_h < SESSION_END

    closed  = [t for t in state["trades"] if t["status"] != "OPEN"]
    wins    = len([t for t in closed if t["result"] == "WIN"])
    beplus  = len([t for t in closed if t["result"] == "BE+"])
    losses  = len([t for t in closed if t["result"] == "LOSS"])
    decided = wins + beplus + losses

    wr = round((wins + beplus) / decided * 100, 1) if decided > 0 else 0

    pos_usd    = BASE_TRADE_SIZE * LEVERAGE
    gross_win  = sum((t["pnl_pct"] or 0) / 100 * pos_usd for t in closed if t.get("result") in ("WIN", "BE+") and t.get("pnl_pct"))
    gross_loss = sum(abs(t["pnl_pct"] or 0) / 100 * pos_usd for t in closed if t.get("result") == "LOSS" and t.get("pnl_pct"))
    pf         = round(gross_win / gross_loss, 2) if gross_loss > 0 else (round(gross_win, 2) if gross_win > 0 else 0)

    total_pnl_usd  = round(gross_win - gross_loss, 2)

    # ── Variant A/B stats ──────────────────────────────────────────
    def _vstats(trades):
        w  = [t for t in trades if t.get("result") == "WIN"]
        b  = [t for t in trades if t.get("result") == "BE+"]
        l  = [t for t in trades if t.get("result") == "LOSS"]
        n  = len(trades)
        gw = sum((t["pnl_pct"] or 0) / 100 * pos_usd for t in w + b)
        gl = sum(abs(t["pnl_pct"] or 0) / 100 * pos_usd for t in l)
        return {
            "total":   n,
            "wins":    len(w),
            "beplus":  len(b),
            "losses":  len(l),
            "wr":      round((len(w) + len(b)) / n * 100, 1) if n else 0,
            "pf":      round(gw / gl, 2) if gl > 0 else (round(gw, 2) if gw > 0 else 0),
            "net":     round(gw - gl, 2),
            "avg_pnl": round(sum(t["pnl_pct"] or 0 for t in trades) / n, 3) if n else 0,
        }

    variant_stats = {
        vname: _vstats([t for t in closed if t.get("variant") == vname])
        for vname in SL_VARIANTS
    }

    # ── Session stats (across all variants) ───────────────────────
    all_sessions = list(PEAK_SESSIONS.keys()) + ["Off-Peak"]
    session_stats = {}
    for sname in all_sessions:
        st = [t for t in closed if (t.get("session") or "Off-Peak") == sname]
        if st:
            sw = len([t for t in st if t.get("result") in ("WIN", "BE+")])
            session_stats[sname] = {
                "total": len(st),
                "wr":    round(sw / len(st) * 100, 1),
            }

    unrealised_usd = round(sum(
        (t.get("unrealised_pnl") or 0) / 100 * pos_usd
        for t in state["open_trades"]
    ), 2)

    insights = generate_insights(closed)

    return render_template_string(HTML,
        open_trades    = state["open_trades"],
        closed_trades  = closed,
        wins           = wins,
        beplus         = beplus,
        losses         = losses,
        win_rate       = wr,
        profit_factor  = pf,
        total_pnl_usd  = total_pnl_usd,
        unrealised_usd = unrealised_usd,
        base_size      = BASE_TRADE_SIZE,
        leverage       = LEVERAGE,
        sl_pct         = list(SL_VARIANTS.values())[0],
        scan_count     = state["scan_count"],
        last_scan      = state["last_scan"],
        last_dir_check = state["last_dir_check"],
        start_time     = state["start_time"],
        end_time       = state["end_time"],
        running        = state["running"],
        direction      = state["direction"],
        dir_detail     = state["dir_detail"],
        in_session     = session_on,
        variant_stats  = variant_stats,
        session_stats  = session_stats,
        insights       = insights,
    )


# ─────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────
if __name__ == "__main__":
    load_trades()

    dir_thread   = threading.Thread(target=direction_worker, daemon=True)
    entry_thread = threading.Thread(target=entry_worker,    daemon=True)
    dir_thread.start()
    entry_thread.start()

    mode_label = "🐂 BULL MODE — LONG only" if BULL_MODE else "🐻 BEAR MODE — SHORT only"
    print("\n" + "═" * 62)
    print("  Multi-TF MFI Scalper — Paper Trader  v1.8")
    print(f"  Regime: {mode_label}")
    print("  Open: 👉  http://localhost:8085")
    sym_labels = " | ".join(f"{s.split('/')[0]} OB:{v['ob']}/OS:{v['os']}" for s, v in zip(SYMBOLS, [SYMBOL_MFI[s.split('/')[0]] for s in SYMBOLS]))
    print(f"  Symbols:  {sym_labels}")
    print(f"  Direction filter:  6H + 1H  EMA{EMA_PERIOD} + ADX{ADX_PERIOD} ≥ {ADX_MIN}")
    print(f"  Entry:    1M MFI{MFI_PERIOD} — {ALLOWED_DIRECTION} signals only")
    print(f"  Exit:     Pure trailing SL — no fixed TP, no MFI exit")
    sl_labels = " | ".join(f"{v}%" for v in SL_VARIANTS.values())
    print(f"  SL:       {sl_labels} → +{TRAIL_START}% profit: BE → every +{TRAIL_STEP}% milestone: SL = milestone−{TRAIL_GAP}%")
    print(f"  Session:  {SESSION_START}:00–{SESSION_END}:00 UTC")
    print(f"  Direction scan: every {DIRECTION_INTERVAL_MIN} min")
    print(f"  Entry scan:     every {ENTRY_INTERVAL_SEC}s")
    print(f"  Running until: {state['end_time']}")
    print("═" * 62 + "\n")

    app.run(host="0.0.0.0", port=8085, debug=False)
