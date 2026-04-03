#!/usr/bin/env python3
"""
Liquidation Zone BTC-Signal Follower — Port 8089
─────────────────────────────────────────────────
Signal  : BTC 6H MFI reversal at liquidation zone
           SHORT: MFI was >80 → crosses <80 + red candle + within 3% of 10c high
           LONG : MFI was <20 → crosses >20 + green candle + within 2% of 40c low

Regime  : AUTO (Monthly MFI) | BEAR | BULL
  BEAR  → SHORT 9 coins: XRP, UNI, DOT, SUI, SOL, AVAX, ADA, DOGE, ETH
  BULL  → LONG  3 coins: BTC, AVAX, LTC

Paper   : $2,000 margin × 5× = $10,000 notional per coin per trade
Partial : 50% close at TP1, 50% at TP2
"""

import ccxt
import time
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
import numpy as np
import pandas as pd
from flask import Flask, render_template_string

# ─── CONFIG ──────────────────────────────────────────────────────────────────
PORT          = 8089
PAPER_MARGIN  = 2000        # USD margin per coin per trade
LEVERAGE      = 5
NOTIONAL      = PAPER_MARGIN * LEVERAGE   # $10,000

BTC_SYMBOL    = "BTC/USDT:USDT"
MFI_LENGTH    = 14
ATR_LENGTH    = 14
MFI_OB        = 80
MFI_OS        = 20

# Zone filters
LONG_SWING_WIN  = 40
SHORT_SWING_WIN = 10
LONG_ZONE_PCT   = 2.0
SHORT_ZONE_PCT  = 3.0

# Alt MFI alignment limits
LONG_MFI_MAX    = 60   # alt MFI must be below this for LONG
SHORT_MFI_MIN   = 40   # alt MFI must be above this for SHORT

# Monthly MFI regime thresholds — calibrated from 4 historical cycles
# 2019 bottom: ~19-20 | 2022 bottom: ~10-11
# Bull CONFIRMED when: monthly MFI was below 20 AND current month CLOSES HIGHER than previous
# Bear confirmed when: monthly MFI was above 65 AND starts falling (already in effect)
MONTHLY_OS_LEVEL     = 20   # monthly MFI below this = cycle bottom zone (watch closely)
MONTHLY_BULL_TRIGGER = 20   # MFI must exceed this AND be rising from below → BULL confirmed
MONTHLY_BEAR_TRIGGER = 65   # monthly MFI drops below this after being overbought → BEAR

# Regime: "AUTO", "BEAR", "BULL"
REGIME_OVERRIDE = "AUTO"

# BEAR mode coins + params
BEAR_COINS = {
    "XRP":  {"sl": 1.5, "tp1": 5.0, "tp2": 10.0},
    "UNI":  {"sl": 1.5, "tp1": 5.0, "tp2": 10.0},
    "DOT":  {"sl": 1.5, "tp1": 4.0, "tp2":  6.0},
    "SUI":  {"sl": 1.5, "tp1": 5.0, "tp2": 10.0},
    "SOL":  {"sl": 1.5, "tp1": 4.0, "tp2": 10.0},
    "AVAX": {"sl": 1.5, "tp1": 5.0, "tp2": 10.0},
    "ADA":  {"sl": 1.5, "tp1": 5.0, "tp2": 10.0},
    "DOGE": {"sl": 1.5, "tp1": 5.0, "tp2": 10.0},
    "ETH":  {"sl": 2.0, "tp1": 5.0, "tp2": 10.0},
}

# BULL mode coins + params
BULL_COINS = {
    "BTC":  {"sl": 1.5, "tp1": 5.0, "tp2": 6.0},
    "AVAX": {"sl": 1.5, "tp1": 4.0, "tp2": 6.0},
    "LTC":  {"sl": 1.5, "tp1": 4.0, "tp2": 6.0},
}

COIN_SYMBOLS = {
    "BTC":  "BTC/USDT:USDT",  "ETH":  "ETH/USDT:USDT",
    "XRP":  "XRP/USDT:USDT",  "SOL":  "SOL/USDT:USDT",
    "ADA":  "ADA/USDT:USDT",  "DOGE": "DOGE/USDT:USDT",
    "AVAX": "AVAX/USDT:USDT", "LTC":  "LTC/USDT:USDT",
    "UNI":  "UNI/USDT:USDT",  "DOT":  "DOT/USDT:USDT",
    "SUI":  "SUI/USDT:USDT",
}

STATE_FILE = Path("/opt/trader/state/liq_zone_8089.json")
LOG_FILE   = Path("/opt/trader/logs/8089.log")

SCAN_INTERVAL_MIN    = 30    # BTC signal check every 30 min
MONITOR_INTERVAL_MIN = 15    # position monitoring every 15 min
REGIME_CHECK_HOURS   = 6     # monthly MFI regime check every 6H
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger(__name__)

app   = Flask(__name__)
state = {}
lock  = threading.Lock()


# ─── EXCHANGE ────────────────────────────────────────────────────────────────
def get_exchange():
    try:
        from config import BINANCE_API_KEY, BINANCE_SECRET_KEY
        return ccxt.binance({
            "apiKey": BINANCE_API_KEY, "secret": BINANCE_SECRET_KEY,
            "options": {"defaultType": "future"}, "enableRateLimit": True,
        })
    except Exception:
        return ccxt.binance({"options": {"defaultType": "future"}, "enableRateLimit": True})


# ─── INDICATORS ──────────────────────────────────────────────────────────────
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


def fetch_candles(exchange, symbol, timeframe, limit=200):
    try:
        raw = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
        df  = pd.DataFrame(raw, columns=["ts","open","high","low","close","volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
        df.set_index("ts", inplace=True)
        return df
    except Exception as e:
        log.warning(f"fetch_candles({symbol} {timeframe}): {e}")
        return None


# ─── STATE ───────────────────────────────────────────────────────────────────
def load_state():
    global state
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            state = json.load(f)
    else:
        state = {
            "regime":          "BEAR",     # current active regime
            "monthly_mfi":     None,        # last monthly MFI value
            "btc_mfi":         None,        # last BTC 6H MFI
            "btc_price":       None,
            "last_signal":     None,        # last BTC signal dict
            "last_scan":       None,
            "last_regime_check": None,
            "open_trades":     {},          # coin -> trade dict
            "closed_trades":   [],
            "signal_log":      [],          # history of BTC signals
        }
    save_state()


def save_state():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


# ─── REGIME DETECTION ────────────────────────────────────────────────────────
def detect_regime(exchange):
    """
    Auto-detect regime from BTC Monthly MFI.

    BEAR → BULL switch (precise 4-cycle calibration):
      Step 1 — Bottom confirmed: monthly MFI drops below 20
               (2019 cycle: ~19-20 | 2022 cycle: ~10-11)
      Step 2 — Bull confirmed:   the NEXT monthly close is HIGHER than the bottom
               (Jan 2023: MFI went 10.95 → 14.55, price $15k → $20k)
      Both steps required — avoids false triggers mid-bear.

    BULL → BEAR switch:
      Monthly MFI falls below 65 after having been above 65.
      (Already triggered Oct 2025 — we are correctly in BEAR now.)

    Current projection (Apr 2026): MFI 41.52, declining ~7/month.
    Estimated bottom zone: Aug–Sep 2026 (~15-20). Watch for higher monthly close after.
    """
    if REGIME_OVERRIDE != "AUTO":
        state["regime"] = REGIME_OVERRIDE
        return REGIME_OVERRIDE

    df = fetch_candles(exchange, BTC_SYMBOL, "1M", limit=36)
    if df is None or len(df) < 16:
        log.warning("Monthly candles unavailable — keeping current regime")
        return state.get("regime", "BEAR")

    df["mfi"] = calc_mfi(df, MFI_LENGTH)
    mfi_clean  = df["mfi"].dropna()
    mfi_vals   = mfi_clean.values

    current   = round(float(mfi_vals[-2]), 2)   # last CLOSED month
    previous  = round(float(mfi_vals[-3]), 2)   # month before that
    prev_6    = mfi_vals[-8:-2]                  # 6 months before current

    state["monthly_mfi"]      = current
    state["monthly_mfi_prev"] = previous
    state["monthly_mfi_proj"] = round(current + (current - previous), 1)  # naive 1-month projection

    log.info(f"Monthly MFI: prev={previous}  current={current}  proj={state['monthly_mfi_proj']}")

    current_regime = state.get("regime", "BEAR")

    # ── BULL confirmation: bottom < 20 then higher monthly close ──────────────
    # previous month was below 20 (the bottom) AND current month closed higher
    bottom_confirmed = previous < MONTHLY_OS_LEVEL
    higher_close     = current > previous

    if bottom_confirmed and higher_close:
        if current_regime != "BULL":
            log.info(
                f"REGIME SWITCH → BULL | Monthly MFI: {previous} → {current} "
                f"(bottom was {previous:.1f} < 20, now HIGHER CLOSE confirmed)"
            )
        state["regime"] = "BULL"
        state["bull_trigger_mfi"]   = current
        state["bull_trigger_price"] = state.get("btc_price")

    # ── BEAR confirmation: was overbought, now falling below trigger ──────────
    elif np.any(prev_6 > 65) and current < MONTHLY_BEAR_TRIGGER and current_regime != "BEAR":
        log.info(f"REGIME SWITCH → BEAR | Monthly MFI fell to {current} from overbought")
        state["regime"] = "BEAR"

    # ── Early warning flags ───────────────────────────────────────────────────
    state["monthly_near_bottom"] = current < 25        # getting close — watch closely
    state["monthly_at_bottom"]   = current < MONTHLY_OS_LEVEL   # below 20 = potential bottom

    log.info(f"Regime: {state['regime']} | Near bottom: {state['monthly_near_bottom']} | At bottom: {state['monthly_at_bottom']}")
    save_state()
    return state["regime"]


# ─── BTC SIGNAL DETECTION ────────────────────────────────────────────────────
def check_btc_signal(exchange):
    """
    Detect BTC 6H MFI reversal at zone.
    Returns signal dict or None.
    """
    df = fetch_candles(exchange, BTC_SYMBOL, "6h", limit=60)
    if df is None or len(df) < 50:
        return None

    df["mfi"] = calc_mfi(df, MFI_LENGTH)
    df["atr"] = calc_atr(df, ATR_LENGTH)
    mfi_vals   = df["mfi"].dropna().values
    atr_vals   = df["atr"].dropna().values

    if len(mfi_vals) < 10 or len(atr_vals) < 5:
        return None

    cur_mfi   = float(mfi_vals[-2])     # last CLOSED candle
    prev_mfis = mfi_vals[-7:-2]          # 5 candles before
    cur_candle = df.iloc[-2]
    entry      = float(cur_candle["close"])
    atr        = float(df["atr"].dropna().iloc[-2])

    state["btc_mfi"]   = round(cur_mfi, 1)
    state["btc_price"] = round(entry, 2)

    green = cur_candle["close"] > cur_candle["open"]
    red   = cur_candle["close"] < cur_candle["open"]

    highs = df["high"].values
    lows  = df["low"].values
    i     = len(df) - 2   # index of last closed candle

    # SHORT signal: MFI was >80, crossed back below 80, red candle, near bounce high
    if np.any(prev_mfis > MFI_OB) and cur_mfi < MFI_OB and red:
        recent_high = highs[max(0, i - SHORT_SWING_WIN):i+1].max()
        dist = (recent_high - entry) / entry * 100
        if dist <= SHORT_ZONE_PCT:
            sig = {"direction":"SHORT","entry":entry,"atr":atr,
                   "btc_mfi":round(cur_mfi,1),"dist_from_zone":round(dist,2),
                   "ts":str(df.index[-2]),"zone_type":"bounce_high","recent_high":round(recent_high,2)}
            log.info(f"BTC SHORT signal | MFI {cur_mfi:.1f} | entry {entry} | dist {dist:.2f}% from high")
            return sig

    # LONG signal: MFI was <20, crossed back above 20, green candle, near swing low
    if np.any(prev_mfis < MFI_OS) and cur_mfi > MFI_OS and green:
        swing_low = lows[max(0, i - LONG_SWING_WIN):i].min()
        dist = (entry - swing_low) / entry * 100
        if dist <= LONG_ZONE_PCT:
            sig = {"direction":"LONG","entry":entry,"atr":atr,
                   "btc_mfi":round(cur_mfi,1),"dist_from_zone":round(dist,2),
                   "ts":str(df.index[-2]),"zone_type":"swing_low","swing_low":round(swing_low,2)}
            log.info(f"BTC LONG signal | MFI {cur_mfi:.1f} | entry {entry} | dist {dist:.2f}% from low")
            return sig

    return None


# ─── ENTER PAPER TRADES ──────────────────────────────────────────────────────
def enter_paper_trades(exchange, btc_signal):
    """
    Fire paper trades on all coins in the active regime list.
    Skip coins that already have an open trade.
    """
    regime = state.get("regime", "BEAR")
    direction = btc_signal["direction"]

    # Regime / direction consistency check
    if regime == "BEAR" and direction != "SHORT":
        log.info("Regime=BEAR but signal=LONG — skipping")
        return
    if regime == "BULL" and direction != "LONG":
        log.info("Regime=BULL but signal=SHORT — skipping")
        return

    coin_configs = BEAR_COINS if regime == "BEAR" else BULL_COINS
    entered = []

    for coin, cfg in coin_configs.items():
        if coin in state["open_trades"]:
            log.info(f"  {coin}: already open — skip")
            continue

        symbol = COIN_SYMBOLS.get(coin)
        if not symbol:
            continue

        try:
            df = fetch_candles(exchange, symbol, "6h", limit=30)
            if df is None or len(df) < 20:
                continue

            df["mfi"] = calc_mfi(df, MFI_LENGTH)
            df["atr"] = calc_atr(df, ATR_LENGTH)
            coin_mfi = float(df["mfi"].dropna().iloc[-2])
            coin_atr = float(df["atr"].dropna().iloc[-2])
            price    = float(df["close"].iloc[-2])

            if np.isnan(coin_mfi) or np.isnan(coin_atr) or coin_atr == 0:
                log.info(f"  {coin}: NaN MFI/ATR — skip")
                continue

            # MFI alignment check
            if direction == "LONG"  and coin_mfi > LONG_MFI_MAX:
                log.info(f"  {coin}: MFI {coin_mfi:.1f} > {LONG_MFI_MAX} (overbought) — skip")
                continue
            if direction == "SHORT" and coin_mfi < SHORT_MFI_MIN:
                log.info(f"  {coin}: MFI {coin_mfi:.1f} < {SHORT_MFI_MIN} (oversold) — skip")
                continue

            sl_dist = coin_atr * cfg["sl"]
            if direction == "LONG":
                sl   = price - sl_dist
                tp1  = price * (1 + cfg["tp1"] / 100)
                tp2  = price * (1 + cfg["tp2"] / 100)
                size = NOTIONAL / price   # coin quantity
            else:
                sl   = price + sl_dist
                tp1  = price * (1 - cfg["tp1"] / 100)
                tp2  = price * (1 - cfg["tp2"] / 100)
                size = NOTIONAL / price

            trade = {
                "coin":          coin,
                "symbol":        symbol,
                "direction":     direction,
                "entry":         round(price, 6),
                "size":          round(size, 6),
                "notional":      NOTIONAL,
                "margin":        PAPER_MARGIN,
                "sl":            round(sl, 6),
                "tp1":           round(tp1, 6),
                "tp2":           round(tp2, 6),
                "tp1_pct":       cfg["tp1"],
                "tp2_pct":       cfg["tp2"],
                "sl_pct":        round(sl_dist / price * 100, 2),
                "coin_mfi":      round(coin_mfi, 1),
                "atr":           round(coin_atr, 6),
                "partial_done":  False,
                "partial_pnl":   0.0,
                "regime":        regime,
                "btc_signal_ts": btc_signal["ts"],
                "btc_mfi":       btc_signal["btc_mfi"],
                "entered_at":    datetime.now().strftime("%Y-%m-%d %H:%M"),
                "status":        "OPEN",
                "pnl":           0.0,
                "peak_pnl":      0.0,
            }
            state["open_trades"][coin] = trade
            entered.append(coin)
            log.info(f"  ✓ {coin} {direction} @ {price:.4f} | SL {sl:.4f} ({sl_dist/price*100:.2f}%) | TP1 {tp1:.4f} | TP2 {tp2:.4f} | MFI {coin_mfi:.1f}")
            time.sleep(0.2)

        except Exception as e:
            log.error(f"  {coin} entry error: {e}")

    if entered:
        log.info(f"Entered {len(entered)} paper trades: {entered}")
        state["signal_log"].append({
            "ts":        btc_signal["ts"],
            "direction": direction,
            "regime":    regime,
            "btc_mfi":   btc_signal["btc_mfi"],
            "coins":     entered,
        })
        save_state()


# ─── MONITOR POSITIONS ───────────────────────────────────────────────────────
def monitor_positions(exchange):
    """Check all open trades against current prices."""
    if not state["open_trades"]:
        return

    to_close = []

    for coin, trade in state["open_trades"].items():
        try:
            df = fetch_candles(exchange, trade["symbol"], "6h", limit=5)
            if df is None:
                continue

            # Use last CLOSED candle high/low for fill simulation
            high  = float(df["high"].iloc[-2])
            low   = float(df["low"].iloc[-2])
            price = float(df["close"].iloc[-2])
            d     = trade["direction"]

            if d == "LONG":
                cur_pnl = (price - trade["entry"]) / trade["entry"] * 100
            else:
                cur_pnl = (trade["entry"] - price) / trade["entry"] * 100

            trade["pnl"]      = round(cur_pnl, 3)
            trade["peak_pnl"] = round(max(trade.get("peak_pnl", 0), cur_pnl), 3)

            if d == "LONG":
                # SL check (use low)
                if low <= trade["sl"]:
                    pnl = trade["partial_pnl"] + (trade["sl"] - trade["entry"]) / trade["entry"] * 100 * (0.5 if trade["partial_done"] else 1.0)
                    close_trade(coin, "SL", trade["sl"], round(pnl, 3))
                    to_close.append(coin); continue

                # TP1 partial (use high)
                if not trade["partial_done"] and high >= trade["tp1"]:
                    trade["partial_done"] = True
                    trade["partial_pnl"]  = round(trade["tp1_pct"] * 0.5, 3)   # 50% at TP1
                    log.info(f"  {coin} TP1 HIT @ {trade['tp1']:.4f} (+{trade['tp1_pct']}% on 50%) — trailing 50%")

                # TP2 full (use high)
                if high >= trade["tp2"]:
                    pnl = trade["partial_pnl"] + trade["tp2_pct"] * 0.5
                    close_trade(coin, "TP2", trade["tp2"], round(pnl, 3))
                    to_close.append(coin); continue

            else:  # SHORT
                if high >= trade["sl"]:
                    pnl = trade["partial_pnl"] + (trade["entry"] - trade["sl"]) / trade["entry"] * 100 * (0.5 if trade["partial_done"] else 1.0)
                    close_trade(coin, "SL", trade["sl"], round(pnl, 3))
                    to_close.append(coin); continue

                if not trade["partial_done"] and low <= trade["tp1"]:
                    trade["partial_done"] = True
                    trade["partial_pnl"]  = round(trade["tp1_pct"] * 0.5, 3)
                    log.info(f"  {coin} TP1 HIT @ {trade['tp1']:.4f} (+{trade['tp1_pct']}% on 50%) — trailing 50%")

                if low <= trade["tp2"]:
                    pnl = trade["partial_pnl"] + trade["tp2_pct"] * 0.5
                    close_trade(coin, "TP2", trade["tp2"], round(pnl, 3))
                    to_close.append(coin); continue

            time.sleep(0.15)

        except Exception as e:
            log.error(f"monitor {coin}: {e}")

    for coin in to_close:
        state["open_trades"].pop(coin, None)

    if to_close or state["open_trades"]:
        save_state()


def close_trade(coin, reason, price, pnl):
    trade = state["open_trades"].get(coin, {})
    closed = {**trade, "exit_price": round(price, 6), "exit_reason": reason,
              "final_pnl": pnl, "closed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
              "status": "CLOSED"}
    state["closed_trades"].append(closed)
    pnl_usd = round(pnl / 100 * NOTIONAL, 2)
    log.info(f"  CLOSED {coin} [{reason}] PnL: {pnl:+.2f}% (${pnl_usd:+.2f})")


# ─── MAIN LOOPS ──────────────────────────────────────────────────────────────
def signal_loop():
    """BTC signal detection — runs every SCAN_INTERVAL_MIN."""
    exchange = get_exchange()
    log.info("Signal loop started")

    while True:
        try:
            with lock:
                state["last_scan"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                sig = check_btc_signal(exchange)
                if sig:
                    state["last_signal"] = sig
                    enter_paper_trades(exchange, sig)
                    save_state()
        except Exception as e:
            log.error(f"signal_loop error: {e}")
        time.sleep(SCAN_INTERVAL_MIN * 60)


def monitor_loop():
    """Position monitor — runs every MONITOR_INTERVAL_MIN."""
    exchange = get_exchange()
    log.info("Monitor loop started")
    time.sleep(60)   # let signal loop initialise first

    while True:
        try:
            with lock:
                monitor_positions(exchange)
        except Exception as e:
            log.error(f"monitor_loop error: {e}")
        time.sleep(MONITOR_INTERVAL_MIN * 60)


def regime_loop():
    """Monthly MFI regime check — runs every REGIME_CHECK_HOURS."""
    exchange = get_exchange()
    log.info("Regime loop started")
    time.sleep(30)

    while True:
        try:
            with lock:
                regime = detect_regime(exchange)
                state["last_regime_check"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                save_state()
        except Exception as e:
            log.error(f"regime_loop error: {e}")
        time.sleep(REGIME_CHECK_HOURS * 3600)


# ─── DASHBOARD ───────────────────────────────────────────────────────────────
TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <meta http-equiv="refresh" content="60">
  <title>8089 — Liq Zone Trader</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { background: #0d0d0d; color: #e0e0e0; font-family: 'Courier New', monospace; font-size: 13px; padding: 20px; }
    h1 { color: #fff; font-size: 18px; margin-bottom: 4px; }
    .subtitle { color: #666; font-size: 11px; margin-bottom: 20px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 12px; margin-bottom: 24px; }
    .card { background: #161616; border: 1px solid #2a2a2a; border-radius: 6px; padding: 14px; }
    .card .label { color: #888; font-size: 10px; text-transform: uppercase; margin-bottom: 4px; }
    .card .value { font-size: 20px; font-weight: bold; }
    .bull  { color: #00e676; }
    .bear  { color: #ff5252; }
    .muted { color: #888; }
    table { width: 100%; border-collapse: collapse; margin-bottom: 24px; }
    th { background: #1a1a1a; color: #888; font-size: 10px; text-transform: uppercase; padding: 8px 10px; text-align: left; }
    td { padding: 8px 10px; border-bottom: 1px solid #1e1e1e; }
    tr:hover td { background: #181818; }
    .win  { color: #00e676; }
    .loss { color: #ff5252; }
    .open { color: #ffd740; }
    .partial { color: #40c4ff; }
    .section { color: #888; font-size: 10px; text-transform: uppercase; letter-spacing: 1px; margin: 20px 0 8px; }
    .regime-bull { background: #0a2a0a; border-color: #00e676; }
    .regime-bear { background: #2a0a0a; border-color: #ff5252; }
    .tag { display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 10px; font-weight: bold; }
    .tag-bull { background: #00e676; color: #000; }
    .tag-bear { background: #ff5252; color: #000; }
    .tag-auto { background: #ffd740; color: #000; }
  </style>
</head>
<body>
  <h1>⚡ Liquidation Zone Trader — Port 8089</h1>
  <div class="subtitle">BTC 6H MFI signal → multi-coin follower | Paper ${{ notional }}/trade | Refresh 60s</div>

  <!-- Status Cards -->
  <div class="grid">
    <div class="card {{ 'regime-bull' if regime == 'BULL' else 'regime-bear' }}">
      <div class="label">Regime</div>
      <div class="value">
        <span class="tag {{ 'tag-bull' if regime == 'BULL' else 'tag-bear' }}">{{ regime }}</span>
        {{ '→ LONGS' if regime == 'BULL' else '→ SHORTS' }}
      </div>
    </div>
    <div class="card {{ 'regime-bull' if monthly_at_bottom else '' }}">
      <div class="label">Monthly MFI{% if monthly_near_bottom %} ⚠️{% endif %}</div>
      <div class="value {{ 'bull' if monthly_mfi and monthly_mfi < 20 else 'bear' if monthly_mfi and monthly_mfi > 65 else '' }}">
        {{ monthly_mfi if monthly_mfi else '—' }}
        {% if monthly_at_bottom %}<span style="font-size:11px;color:#00e676"> ◄◄ BOTTOM ZONE — watch for higher close!</span>
        {% elif monthly_near_bottom %}<span style="font-size:11px;color:#ffd740"> ← approaching bottom (~20)</span>{% endif %}
      </div>
      {% if monthly_mfi_proj %}<div class="label" style="margin-top:4px">Proj next month: {{ monthly_mfi_proj }}</div>{% endif %}
    </div>
    <div class="card">
      <div class="label">BTC 6H MFI</div>
      <div class="value {{ 'bear' if btc_mfi and btc_mfi > 70 else 'bull' if btc_mfi and btc_mfi < 30 else '' }}">
        {{ btc_mfi if btc_mfi else '—' }}
      </div>
    </div>
    <div class="card">
      <div class="label">BTC Price</div>
      <div class="value">${{ '{:,.0f}'.format(btc_price) if btc_price else '—' }}</div>
    </div>
    <div class="card">
      <div class="label">Open Trades</div>
      <div class="value open">{{ open_count }}</div>
    </div>
    <div class="card">
      <div class="label">Total Closed</div>
      <div class="value">{{ closed_count }}</div>
    </div>
    <div class="card">
      <div class="label">Win Rate</div>
      <div class="value {{ 'bull' if wr >= 50 else 'bear' if wr < 35 else '' }}">{{ wr }}%</div>
    </div>
    <div class="card">
      <div class="label">Net PnL</div>
      <div class="value {{ 'bull' if net_pnl > 0 else 'bear' }}">{{ '{:+.1f}'.format(net_pnl) }}%</div>
    </div>
  </div>

  <!-- Last BTC Signal -->
  {% if last_signal %}
  <div class="section">Last BTC Signal</div>
  <table>
    <tr>
      <th>Time</th><th>Direction</th><th>BTC Entry</th><th>BTC MFI</th><th>Zone Type</th><th>Dist from Zone</th><th>Coins Entered</th>
    </tr>
    <tr>
      <td>{{ last_signal.ts }}</td>
      <td class="{{ 'bull' if last_signal.direction == 'LONG' else 'bear' }}">{{ last_signal.direction }}</td>
      <td>${{ last_signal.entry }}</td>
      <td>{{ last_signal.btc_mfi }}</td>
      <td>{{ last_signal.get('zone_type','—') }}</td>
      <td>{{ last_signal.dist_from_zone }}%</td>
      <td>{{ ', '.join(last_signal_coins) if last_signal_coins else '—' }}</td>
    </tr>
  </table>
  {% endif %}

  <!-- Open Positions -->
  <div class="section">Open Positions ({{ open_count }})</div>
  {% if open_trades %}
  <table>
    <tr>
      <th>Coin</th><th>Dir</th><th>Entry</th><th>SL</th><th>TP1</th><th>TP2</th>
      <th>Current PnL%</th><th>Peak%</th><th>MFI@Entry</th><th>Partial</th><th>Since</th>
    </tr>
    {% for coin, t in open_trades.items() %}
    <tr>
      <td><b>{{ coin }}</b></td>
      <td class="{{ 'bull' if t.direction == 'LONG' else 'bear' }}">{{ t.direction }}</td>
      <td>{{ t.entry }}</td>
      <td class="loss">{{ t.sl }}</td>
      <td class="partial">{{ t.tp1 }}</td>
      <td class="win">{{ t.tp2 }}</td>
      <td class="{{ 'win' if t.pnl > 0 else 'loss' }}">{{ '{:+.2f}'.format(t.pnl) }}%</td>
      <td class="win">{{ '{:+.2f}'.format(t.peak_pnl) }}%</td>
      <td>{{ t.coin_mfi }}</td>
      <td class="{{ 'partial' if t.partial_done else 'muted' }}">{{ '✓' if t.partial_done else '—' }}</td>
      <td>{{ t.entered_at }}</td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <p class="muted" style="margin-bottom:20px">No open positions</p>
  {% endif %}

  <!-- Signal History -->
  <div class="section">Signal History ({{ signal_log|length }})</div>
  {% if signal_log %}
  <table>
    <tr><th>Time</th><th>Dir</th><th>Regime</th><th>BTC MFI</th><th>Coins</th></tr>
    {% for s in signal_log[-10:]|reverse %}
    <tr>
      <td>{{ s.ts }}</td>
      <td class="{{ 'bull' if s.direction == 'LONG' else 'bear' }}">{{ s.direction }}</td>
      <td>{{ s.regime }}</td>
      <td>{{ s.btc_mfi }}</td>
      <td>{{ ', '.join(s.coins) }}</td>
    </tr>
    {% endfor %}
  </table>
  {% endif %}

  <!-- Closed Trades -->
  <div class="section">Closed Trades ({{ closed_count }}) — Last 20</div>
  {% if closed_trades %}
  <table>
    <tr>
      <th>Coin</th><th>Dir</th><th>Entry</th><th>Exit</th><th>Reason</th>
      <th>PnL%</th><th>PnL $</th><th>Regime</th><th>Opened</th><th>Closed</th>
    </tr>
    {% for t in closed_trades[-20:]|reverse %}
    <tr>
      <td><b>{{ t.coin }}</b></td>
      <td class="{{ 'bull' if t.direction == 'LONG' else 'bear' }}">{{ t.direction }}</td>
      <td>{{ t.entry }}</td>
      <td>{{ t.exit_price }}</td>
      <td class="{{ 'win' if t.exit_reason == 'TP2' else 'partial' if t.exit_reason == 'TP1' else 'loss' }}">{{ t.exit_reason }}</td>
      <td class="{{ 'win' if t.final_pnl > 0 else 'loss' }}">{{ '{:+.2f}'.format(t.final_pnl) }}%</td>
      <td class="{{ 'win' if t.final_pnl > 0 else 'loss' }}">${{ '{:+.0f}'.format(t.final_pnl / 100 * notional) }}</td>
      <td>{{ t.regime }}</td>
      <td>{{ t.entered_at }}</td>
      <td>{{ t.closed_at }}</td>
    </tr>
    {% endfor %}
  </table>
  {% endif %}

  <!-- Per-Coin Summary -->
  <div class="section">Per-Coin Summary</div>
  <table>
    <tr><th>Coin</th><th>Trades</th><th>Wins</th><th>Losses</th><th>WR%</th><th>Net PnL%</th><th>Net PnL $</th></tr>
    {% for row in coin_summary %}
    <tr>
      <td><b>{{ row.coin }}</b></td>
      <td>{{ row.n }}</td>
      <td class="win">{{ row.wins }}</td>
      <td class="loss">{{ row.losses }}</td>
      <td class="{{ 'win' if row.wr >= 50 else 'loss' if row.wr < 35 else '' }}">{{ row.wr }}%</td>
      <td class="{{ 'win' if row.net > 0 else 'loss' }}">{{ '{:+.1f}'.format(row.net) }}%</td>
      <td class="{{ 'win' if row.net > 0 else 'loss' }}">${{ '{:+.0f}'.format(row.net / 100 * notional) }}</td>
    </tr>
    {% endfor %}
  </table>

  <div class="muted" style="margin-top:20px;font-size:10px">
    Last scan: {{ last_scan }} | Last regime check: {{ last_regime_check }} | Override: {{ regime_override }}
  </div>
</body>
</html>
"""


@app.route("/")
def dashboard():
    with lock:
        closed = state.get("closed_trades", [])
        open_t = state.get("open_trades", {})
        sig    = state.get("last_signal")

        # WR + net
        wins   = [t for t in closed if t.get("final_pnl", 0) > 0]
        losses = [t for t in closed if t.get("final_pnl", 0) <= 0]
        wr     = round(len(wins) / len(closed) * 100, 1) if closed else 0
        net    = round(sum(t.get("final_pnl", 0) for t in closed), 2)

        # Per-coin summary
        from collections import defaultdict
        coin_data = defaultdict(lambda: {"n":0,"wins":0,"losses":0,"net":0.0})
        for t in closed:
            c = t.get("coin","?")
            coin_data[c]["n"]    += 1
            coin_data[c]["net"]  += t.get("final_pnl", 0)
            if t.get("final_pnl", 0) > 0: coin_data[c]["wins"]   += 1
            else:                           coin_data[c]["losses"] += 1
        coin_summary = []
        for c, d in sorted(coin_data.items()):
            d["coin"] = c
            d["wr"]   = round(d["wins"]/d["n"]*100, 1) if d["n"] else 0
            d["net"]  = round(d["net"], 2)
            coin_summary.append(d)

        # Last signal coins
        last_signal_coins = []
        if sig and state.get("signal_log"):
            last_log = state["signal_log"][-1] if state["signal_log"] else {}
            last_signal_coins = last_log.get("coins", [])

        return render_template_string(
            TEMPLATE,
            regime              = state.get("regime", "BEAR"),
            monthly_mfi         = state.get("monthly_mfi"),
            monthly_mfi_proj    = state.get("monthly_mfi_proj"),
            monthly_near_bottom = state.get("monthly_near_bottom", False),
            monthly_at_bottom   = state.get("monthly_at_bottom", False),
            btc_mfi             = state.get("btc_mfi"),
            btc_price           = state.get("btc_price"),
            open_count          = len(open_t),
            closed_count        = len(closed),
            wr                  = wr,
            net_pnl             = net,
            open_trades         = open_t,
            closed_trades       = closed,
            last_signal         = sig,
            last_signal_coins   = last_signal_coins,
            signal_log          = state.get("signal_log", []),
            coin_summary        = coin_summary,
            last_scan           = state.get("last_scan", "—"),
            last_regime_check   = state.get("last_regime_check", "—"),
            regime_override     = REGIME_OVERRIDE,
            notional            = NOTIONAL,
        )


# ─── ENTRY POINT ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    load_state()

    # Run initial regime detection
    try:
        ex = get_exchange()
        detect_regime(ex)
    except Exception as e:
        log.warning(f"Initial regime detection failed: {e}")

    threading.Thread(target=signal_loop,  daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()
    threading.Thread(target=regime_loop,  daemon=True).start()

    log.info(f"Starting Liq Zone Trader on port {PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
