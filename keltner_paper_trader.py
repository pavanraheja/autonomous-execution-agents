"""
Keltner Channel Breakout — Paper Trader
────────────────────────────────────────
v1.0  2026-03-22  Baseline
v1.1  2026-03-28  Coins: +SOL/BNB/AVAX (backtest PF 1.05→1.26, 5× expectancy). Fixed BE classification bug (pnl_out >= 0.0).

Strategy: 1H Keltner Channel Breakout + Volume confirmation
  LONG  → close > upper band + volume > 1.5× avg + bull candle
  SHORT → close < lower band + volume > 1.5× avg + bear candle

Entry:   Close of confirming 1H candle
Track:   1H candles for SL/TP/trailing
SL:      1.0%  |  TP: 2.0%  (2:1 RR)

Backtest result (90 days, 1H, BTC+ETH):
  BTC: PF 1.25, 45.5% WR
  ETH: PF 1.26, 47.5% WR

Dashboard: http://localhost:8084
Saves:     Trade Logs/keltner_paper_trades.json
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
from config import BINANCE_API_KEY, BINANCE_API_SECRET

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────
SYMBOLS = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT', 'BNB/USDT:USDT', 'AVAX/USDT:USDT']

# Keltner Channel parameters
KC_EMA_PERIOD  = 20
KC_ATR_PERIOD  = 10
KC_MULTIPLIER  = 1.5

# Volume filter
VOL_MULTIPLIER = 1.5    # candle volume must exceed 1.5× 20-bar average

# Risk management (from backtest optimal)
SL_PCT         = 1.0    # 1% stop loss
TP_PCT         = 2.0    # 2% take profit  (2:1 RR)

# Trailing stop
ENABLE_TRAIL    = True
TRAIL_TRIGGER_1 = 1.0   # % profit → lock Stage 1
TRAIL_TRIGGER_2 = 1.5   # % profit → lock Stage 2
TRAIL_LOCK_1    = 0.0   # lock breakeven (0% — covers BE, not profit)
TRAIL_LOCK_2    = 0.5   # lock +0.5% profit at Stage 2

# Position sizing
BASE_TRADE_SIZE = 500   # margin per trade ($)
LEVERAGE        = 5     # notional = 500 × 5 = $2,500

# Scan timing
SCAN_INTERVAL_MIN  = 60   # 1H candles — scan once per hour
TRAIL_INTERVAL_MIN = 15   # trail check every 15 min
TEST_DURATION_DAYS = 30   # run for 30 days

TRADE_LOG = "/Users/macbook/Downloads/Content Projects/02 - Trading Coin Hunter/Trade Logs/keltner_paper_trades.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

exchange = ccxt.binanceusdm({
    "enableRateLimit": True,
    "timeout": 30000,
})

app   = Flask(__name__)
state = {
    "trades":      [],
    "open_trades": [],
    "last_scan":   "Not yet run",
    "scan_count":  0,
    "start_time":  datetime.now().strftime("%Y-%m-%d %H:%M"),
    "end_time":    (datetime.now() + timedelta(days=TEST_DURATION_DAYS)).strftime("%Y-%m-%d %H:%M"),
    "running":     True,
}


# ─────────────────────────────────────────
# DATA
# ─────────────────────────────────────────
def fetch_candles(symbol, tf, limit=100):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, tf, limit=limit)
        df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        return df
    except Exception as e:
        log.warning(f"fetch_candles {symbol} {tf}: {e}")
        return None


# ─────────────────────────────────────────
# KELTNER CHANNEL
# ─────────────────────────────────────────
def calc_keltner(df):
    """Returns df with ema, atr, upper, lower columns."""
    df = df.copy()
    df["ema"] = df["close"].ewm(span=KC_EMA_PERIOD, adjust=False).mean()

    # True Range
    hl  = df["high"] - df["low"]
    hpc = (df["high"] - df["close"].shift(1)).abs()
    lpc = (df["low"]  - df["close"].shift(1)).abs()
    df["tr"]  = pd.concat([hl, hpc, lpc], axis=1).max(axis=1)
    df["atr"] = df["tr"].rolling(KC_ATR_PERIOD).mean()

    df["upper"] = df["ema"] + KC_MULTIPLIER * df["atr"]
    df["lower"] = df["ema"] - KC_MULTIPLIER * df["atr"]
    return df


# ─────────────────────────────────────────
# SIGNAL CHECK (1H)
# ─────────────────────────────────────────
def check_signal(symbol):
    df = fetch_candles(symbol, "1h", limit=60)
    if df is None or len(df) < KC_EMA_PERIOD + KC_ATR_PERIOD + 5:
        return None

    df = calc_keltner(df)

    # Use second-to-last candle (last closed candle — last row is current open)
    c  = df.iloc[-2]
    vol_avg = df["volume"].iloc[-22:-2].mean()   # 20-bar avg excluding signal candle

    if vol_avg == 0 or pd.isna(c["upper"]) or pd.isna(c["lower"]):
        return None

    coin   = symbol.split("/")[0]
    label  = f"{coin}_{datetime.now().strftime('%m%d_%H%M')}"
    entry  = round(float(c["close"]), 6)

    # LONG breakout: close above upper band, bull candle, high volume
    if (c["close"] > c["upper"] and
        c["close"] > c["open"] and
        c["volume"] > VOL_MULTIPLIER * vol_avg):

        sl = round(entry * (1 - SL_PCT / 100), 6)
        tp = round(entry * (1 + TP_PCT / 100), 6)
        return _build_trade(label, coin, "LONG", entry, sl, tp,
                            round(float(c["upper"]), 4), round(float(c["lower"]), 4),
                            round(float(c["volume"]), 0), round(vol_avg, 0))

    # SHORT breakout: close below lower band, bear candle, high volume
    if (c["close"] < c["lower"] and
        c["close"] < c["open"] and
        c["volume"] > VOL_MULTIPLIER * vol_avg):

        sl = round(entry * (1 + SL_PCT / 100), 6)
        tp = round(entry * (1 - TP_PCT / 100), 6)
        return _build_trade(label, coin, "SHORT", entry, sl, tp,
                            round(float(c["upper"]), 4), round(float(c["lower"]), 4),
                            round(float(c["volume"]), 0), round(vol_avg, 0))

    return None


def _build_trade(label, coin, direction, entry, sl, tp, upper, lower, vol, vol_avg):
    sl_pct_label = f"-{SL_PCT}%" if direction == "LONG" else f"+{SL_PCT}%"
    tp_pct_label = f"+{TP_PCT}%" if direction == "LONG" else f"-{TP_PCT}%"
    return {
        "id":             label,
        "symbol":         coin,
        "direction":      direction,
        "entry":          entry,
        "sl":             sl,
        "sl_original":    sl,
        "tp":             tp,
        "sl_pct":         sl_pct_label,
        "tp_pct":         tp_pct_label,
        "kc_upper":       upper,
        "kc_lower":       lower,
        "signal_vol":     int(vol),
        "avg_vol":        int(vol_avg),
        "vol_ratio":      round(vol / vol_avg, 2) if vol_avg > 0 else 0,
        "status":         "OPEN",
        "opened_at":      datetime.now().strftime("%Y-%m-%d %H:%M"),
        "opened_at_utc":  datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "closed_at":      None,
        "result":         None,
        "pnl_pct":        None,
        "exit_price":     None,
        "position_usd":   BASE_TRADE_SIZE * LEVERAGE,
        "trailing_stage": 0,
        "trail_note":     "",
        "current_price":  entry,
        "unrealised_pnl": 0.0,
    }


# ─────────────────────────────────────────
# TRADE MANAGEMENT
# ─────────────────────────────────────────
def update_open_trades():
    for trade in state["open_trades"][:]:
        symbol = trade["symbol"] + "/USDT:USDT"
        df     = fetch_candles(symbol, "1h", limit=10)
        if df is None:
            continue

        # Filter to candles opened AFTER trade entry — prevents pre-entry wicks
        # from triggering SL/TP on the very first monitor cycle
        opened_utc = trade.get("opened_at_utc", "")
        if opened_utc:
            entry_ts = pd.Timestamp(opened_utc, tz="UTC")
            df_post  = df[df["timestamp"] >= entry_ts]
        else:
            df_post  = df  # fallback for old trades without utc field

        if df_post.empty:
            # Trade just opened, entry candle not yet closed — skip this cycle
            latest = df.iloc[-1]
            trade["current_price"]  = round(float(latest["close"]), 6)
            trade["unrealised_pnl"] = 0.0
            continue

        latest = df_post.iloc[-1]
        h, l   = df_post["high"].max(), df_post["low"].min()
        price  = float(latest["close"])

        direction = trade["direction"]
        entry     = trade["entry"]

        if direction == "LONG":
            pnl = (price - entry) / entry * 100
        else:
            pnl = (entry - price) / entry * 100

        # ── Trailing stop ──────────────────────────────────────────
        if ENABLE_TRAIL:
            stage = trade.get("trailing_stage", 0)

            if stage == 0 and pnl >= TRAIL_TRIGGER_1:
                if direction == "LONG":
                    new_sl = round(entry * (1 + TRAIL_LOCK_1 / 100), 6) if TRAIL_LOCK_1 > 0 else entry
                else:
                    new_sl = round(entry * (1 - TRAIL_LOCK_1 / 100), 6) if TRAIL_LOCK_1 > 0 else entry

                if direction == "LONG" and new_sl > trade["sl"]:
                    trade["sl"] = new_sl
                    trade["trailing_stage"] = 1
                    trade["trail_note"] = f"Stage1: SL→BE at +{pnl:.1f}% profit"
                    log.info(f"TRAIL Stage1 LONG: {trade['symbol']} SL→{new_sl}")
                elif direction == "SHORT" and new_sl < trade["sl"]:
                    trade["sl"] = new_sl
                    trade["trailing_stage"] = 1
                    trade["trail_note"] = f"Stage1: SL→BE at +{pnl:.1f}% profit"
                    log.info(f"TRAIL Stage1 SHORT: {trade['symbol']} SL→{new_sl}")
                stage = trade.get("trailing_stage", 0)

            if stage == 1 and pnl >= TRAIL_TRIGGER_2:
                if direction == "LONG":
                    new_sl = round(entry * (1 + TRAIL_LOCK_2 / 100), 6)
                    if new_sl > trade["sl"]:
                        trade["sl"] = new_sl
                        trade["trailing_stage"] = 2
                        trade["trail_note"] = f"Stage2: SL→+{TRAIL_LOCK_2}% at +{pnl:.1f}% profit"
                        log.info(f"TRAIL Stage2 LONG: {trade['symbol']} SL→{new_sl}")
                else:
                    new_sl = round(entry * (1 - TRAIL_LOCK_2 / 100), 6)
                    if new_sl < trade["sl"]:
                        trade["sl"] = new_sl
                        trade["trailing_stage"] = 2
                        trade["trail_note"] = f"Stage2: SL→+{TRAIL_LOCK_2}% at +{pnl:.1f}% profit"
                        log.info(f"TRAIL Stage2 SHORT: {trade['symbol']} SL→{new_sl}")

        # ── Check SL / TP ──────────────────────────────────────────
        if direction == "LONG":
            hit_tp = h >= trade["tp"]
            hit_sl = l <= trade["sl"]
        else:
            hit_tp = l <= trade["tp"]
            hit_sl = h >= trade["sl"]

        if hit_tp and hit_sl:
            # Whichever opens closer to is likely first
            hit_sl = abs(latest["open"] - trade["sl"]) <= abs(latest["open"] - trade["tp"])
            hit_tp = not hit_sl

        if hit_tp:
            exit_price = trade["tp"]
            pnl_out    = round(TP_PCT, 3)
            result     = "WIN"
        elif hit_sl:
            exit_price = trade["sl"]
            if direction == "LONG":
                pnl_out = round((exit_price - entry) / entry * 100, 3)
            else:
                pnl_out = round((entry - exit_price) / entry * 100, 3)
            result = "BE+" if (trade.get("trailing_stage", 0) >= 1 and pnl_out >= 0.0) else "LOSS"
        else:
            trade["current_price"]  = round(price, 6)
            trade["unrealised_pnl"] = round(pnl, 3)
            continue

        trade["status"]     = result
        trade["result"]     = result
        trade["closed_at"]  = datetime.now().strftime("%Y-%m-%d %H:%M")
        trade["exit_price"] = round(exit_price, 6)
        trade["pnl_pct"]    = pnl_out
        state["open_trades"].remove(trade)
        log.info(f"Closed: {trade['symbol']} {direction} → {result} | PnL: {pnl_out}%")

    save_trades()


# ─────────────────────────────────────────
# MAIN SCAN
# ─────────────────────────────────────────
def run_scan():
    log.info(f"Keltner scan #{state['scan_count'] + 1}...")
    update_open_trades()

    new_sigs = 0
    for symbol in SYMBOLS:
        coin = symbol.split("/")[0]

        # Skip if already have an open trade on this coin
        if coin in [t["symbol"] for t in state["open_trades"]]:
            log.info(f"  Skip {coin} — trade already open")
            continue

        signal = check_signal(symbol)
        if signal:
            state["trades"].append(signal)
            state["open_trades"].append(signal)
            new_sigs += 1
            log.info(
                f"NEW SIGNAL: {signal['symbol']} {signal['direction']} | "
                f"Entry:{signal['entry']} SL:{signal['sl']} TP:{signal['tp']} | "
                f"Vol ratio:{signal['vol_ratio']}×"
            )
        time.sleep(0.3)

    state["last_scan"]  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state["scan_count"] += 1
    save_trades()
    log.info(f"Scan done. {new_sigs} new signals. {len(state['open_trades'])} open.")


def trail_runner():
    end_time = datetime.now() + timedelta(days=TEST_DURATION_DAYS)
    time.sleep(5 * 60)
    while datetime.now() < end_time and state["running"]:
        if state["open_trades"]:
            log.info(f"Trail check: {len(state['open_trades'])} open trades...")
            update_open_trades()
        time.sleep(TRAIL_INTERVAL_MIN * 60)


def background_runner():
    end_time = datetime.now() + timedelta(days=TEST_DURATION_DAYS)
    while datetime.now() < end_time and state["running"]:
        run_scan()
        time.sleep(SCAN_INTERVAL_MIN * 60)
    state["running"] = False
    log.info("Keltner paper trading test complete.")


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
    <title>Keltner Breakout — Paper Trader</title>
    <meta http-equiv="refresh" content="120">
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

        table { width:100%; border-collapse:collapse; background:#1a1a1a;
                border-radius:10px; overflow:hidden; margin-bottom:28px; }
        th { background:#222; padding:11px 14px; text-align:left; font-size:11px;
             color:#666; text-transform:uppercase; letter-spacing:1px; }
        td { padding:11px 14px; border-top:1px solid #222; font-size:13px; }
        tr:hover td { background:#1e1e1e; }

        .coin    { font-weight:700; color:#fff; font-size:15px; }
        .long    { color:#00c853; font-weight:700; }
        .short   { color:#ff6f00; font-weight:700; }
        .win     { color:#00c853; font-weight:600; }
        .loss    { color:#ff1744; font-weight:600; }
        .open    { color:#1e88e5; font-weight:600; }
        .sl-val  { color:#ff6f00; }
        .tp-val  { color:#00c853; }
        .pnl-pos { color:#00c853; font-weight:600; }
        .pnl-neg { color:#ff1744; font-weight:600; }
        .usd-pos { color:#00c853; font-weight:600; font-size:12px; }
        .usd-neg { color:#ff1744; font-weight:600; font-size:12px; }
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

        .trail-badge { display:inline-block; padding:2px 7px; border-radius:5px; font-size:10px; font-weight:600; }
        .trail-0 { color:#555; }
        .trail-1 { background:#001a0d; color:#00c853; border:1px solid #00c853; }
        .trail-2 { background:#004d1a; color:#00ff88; border:1px solid #00ff88; }

        .strategy-box { background:#141414; border:1px solid #2a2a2a; border-radius:12px;
                        padding:16px 22px; margin-bottom:22px; }
        .strategy-box h3 { font-size:12px; color:#ffd600; text-transform:uppercase; letter-spacing:1px; margin-bottom:10px; }
        .strategy-grid { display:flex; gap:28px; flex-wrap:wrap; }
        .sg { font-size:12px; color:#aaa; line-height:2; }
        .sg span { color:#fff; font-weight:600; }

        .btn-close { background:#1a0000; color:#ff5252; border:1px solid #ff1744;
                     padding:4px 10px; border-radius:6px; font-size:11px; font-weight:600; cursor:pointer; }
        .btn-close:hover { background:#ff1744; color:#fff; }
        .btn-edit  { background:#001a2a; color:#40c4ff; border:1px solid #1e88e5;
                     padding:4px 10px; border-radius:6px; font-size:11px; font-weight:600; cursor:pointer; margin-right:4px; }
        .btn-edit:hover { background:#1e88e5; color:#fff; }

        .modal-overlay { display:none; position:fixed; inset:0; background:rgba(0,0,0,0.75);
                         z-index:1000; align-items:center; justify-content:center; }
        .modal-overlay.active { display:flex; }
        .modal { background:#1a1a1a; border:1px solid #333; border-radius:14px; padding:28px 32px; width:340px; }
        .modal h3 { font-size:14px; color:#fff; margin-bottom:16px; }
        .modal label { font-size:11px; color:#666; text-transform:uppercase; letter-spacing:1px;
                       display:block; margin-bottom:4px; margin-top:14px; }
        .modal input { width:100%; background:#111; border:1px solid #333; border-radius:8px;
                       color:#fff; font-size:14px; padding:8px 12px; outline:none; }
        .modal input:focus { border-color:#1e88e5; }
        .modal-hint { font-size:10px; color:#555; margin-top:4px; }
        .modal-actions { display:flex; gap:10px; margin-top:20px; }
        .modal-actions button { flex:1; padding:9px; border-radius:8px; font-size:13px; font-weight:600; cursor:pointer; border:none; }
        .btn-save   { background:#1e88e5; color:#fff; }
        .btn-cancel { background:#222; color:#aaa; }

        .running-dot { display:inline-block; width:8px; height:8px; background:#00c853;
                       border-radius:50%; margin-right:6px; animation:pulse 1.5s infinite; }
        @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
        .empty { text-align:center; padding:40px; color:#444; }
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
        Keltner Breakout <span style="color:#ffd600;font-size:18px;">— Paper Trader</span>
        <span style="color:#555;font-size:14px;margin-left:12px;">v1.0</span>
    </h1>
    <p class="sub">
        Binance USDT Futures · BTC + ETH + SOL + BNB + AVAX · 1H candles ·
        {{ start_time }} → {{ end_time }} &nbsp;·&nbsp;
        <span style="color:#555;">Scan #{{ scan_count }}</span>
    </p>

    <!-- Strategy Summary -->
    <div class="strategy-box">
        <h3>⚡ Strategy: Keltner Channel Breakout (1H) — Both Directions</h3>
        <div class="strategy-grid">
            <div class="sg">
                <div>Direction &nbsp;&nbsp;<span>LONG &amp; SHORT</span></div>
                <div>Timeframe &nbsp;&nbsp;<span>1H candles</span></div>
                <div>KC EMA &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;<span>{{ kc_ema }}  period</span></div>
                <div>KC ATR &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;<span>{{ kc_atr }} period  ×{{ kc_mult }}</span></div>
                <div>Vol Filter &nbsp;&nbsp;<span>&gt;{{ vol_mult }}× 20-bar avg</span></div>
            </div>
            <div class="sg">
                <div>LONG signal &nbsp;<span>close &gt; upper band + bull candle + volume</span></div>
                <div>SHORT signal<span> close &lt; lower band + bear candle + volume</span></div>
                <div>SL &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;<span>{{ sl_pct }}%</span> from entry</div>
                <div>TP &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;<span>{{ tp_pct }}%</span> from entry (2:1 RR)</div>
            </div>
            <div class="sg">
                <div>Trail Stage1 &nbsp;<span>+{{ trail_t1 }}% profit → SL → BE</span></div>
                <div>Trail Stage2 &nbsp;<span>+{{ trail_t2 }}% profit → SL → lock +{{ trail_l2 }}%</span></div>
                <div>Trail check &nbsp;<span>every {{ trail_interval }} min</span></div>
                <div>Position &nbsp;&nbsp;&nbsp;&nbsp;<span>${{ "{:,}".format(base_size) }} margin × {{ leverage }}× = ${{ "{:,}".format(base_size * leverage) }}</span></div>
                <div>Backtest WR &nbsp;<span>45–48%  |  PF 1.25–1.26</span></div>
            </div>
        </div>
    </div>

    {% if insights %}
    <div class="insights-box">
        <h3>🧠 Learning Insights — Milestone {{ insights.milestone }} Trades (Keltner)</h3>
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
                <h4>Direction Breakdown</h4>
                {% for side, data in insights.sides.items() %}
                {% if data[0] > 0 %}
                <div class="ig-row">{{ side }} ({{ data[0] }} trades)<span>{{ data[1] }}%</span></div>
                {% endif %}
                {% endfor %}
            </div>
            <div class="ig-box">
                <h4>Coin Breakdown</h4>
                {% for coin, data in insights.coins.items() %}
                {% if data[0] > 0 %}
                <div class="ig-row">{{ coin }} ({{ data[0] }} trades)<span>{{ data[1] }}%</span></div>
                {% endif %}
                {% endfor %}
            </div>
        </div>
    </div>
    {% endif %}

    <!-- Stats -->
    <div class="stats">
        <div class="stat-box blue">
            <div class="label">Open Trades</div>
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
            <div class="label">Win+BE+ Rate</div>
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
            <div class="label">Last Scan</div>
            <div class="value" style="font-size:11px;margin-top:6px;">{{ last_scan }}</div>
        </div>
    </div>

    {% if open_trades %}
    <h2>🔵 Open Trades ({{ open_trades | length }})</h2>
    <table>
        <thead><tr>
            <th>Coin</th><th>Dir</th><th>Entry</th>
            <th>Stop Loss</th><th>Take Profit</th>
            <th>KC Upper</th><th>KC Lower</th>
            <th>Vol Ratio</th><th>Trailing</th>
            <th>Unrealised %</th><th>USD PnL</th>
            <th>Opened</th><th>Chart</th><th>Action</th>
        </tr></thead>
        <tbody>
        {% for t in open_trades %}
        <tr>
            <td class="coin">{{ t.symbol }}</td>
            <td>
                {% if t.direction == 'LONG' %}
                    <span class="dir-badge dir-long">↑ LONG</span>
                {% else %}
                    <span class="dir-badge dir-short">↓ SHORT</span>
                {% endif %}
            </td>
            <td>{{ t.entry }}</td>
            <td class="sl-val">
                {{ t.sl }}
                <br><small style="color:#555;">{{ t.sl_pct }}</small>
            </td>
            <td class="tp-val">{{ t.tp }} <small>({{ t.tp_pct }})</small></td>
            <td style="color:#aaa;font-size:12px;">{{ t.kc_upper or '—' }}</td>
            <td style="color:#aaa;font-size:12px;">{{ t.kc_lower or '—' }}</td>
            <td style="color:#ffd600;font-size:12px;">{{ t.vol_ratio or '—' }}×</td>
            <td>
                {% set stage = t.trailing_stage or 0 %}
                {% if stage == 2 %}
                    <span class="trail-badge trail-2">🔒 +0.5%</span>
                {% elif stage == 1 %}
                    <span class="trail-badge trail-1">✅ BE</span>
                {% else %}
                    <span class="trail-badge trail-0">—</span>
                {% endif %}
            </td>
            <td class="{{ 'pnl-pos' if (t.unrealised_pnl or 0) >= 0 else 'pnl-neg' }}">
                {{ '+' if (t.unrealised_pnl or 0) >= 0 else '' }}{{ t.unrealised_pnl or '—' }}%
            </td>
            <td class="{{ 'usd-pos' if (t.unrealised_pnl or 0) >= 0 else 'usd-neg' }}">
                {{ '+' if (t.unrealised_pnl or 0) >= 0 else '' }}${{ "{:,.0f}".format((t.unrealised_pnl or 0) / 100 * (t.position_usd or (base_size * leverage))) }}
            </td>
            <td style="color:#555;font-size:12px;">{{ t.opened_at }}</td>
            <td>
                <a class="tv-link" href="https://www.tradingview.com/chart/?symbol=BINANCE:{{ t.symbol }}USDT.P&interval=60" target="_blank">1H →</a>
            </td>
            <td style="white-space:nowrap;">
                <button class="btn-edit" onclick="openEdit('{{ t.id }}','{{ t.symbol }}','{{ t.sl }}','{{ t.tp }}')">Edit</button>
                <form method="POST" action="/close/{{ t.id }}" style="display:inline;margin:0;">
                    <button class="btn-close" type="submit" onclick="return confirm('Close {{ t.symbol }}?')">Close</button>
                </form>
            </td>
        </tr>
        {% endfor %}
        </tbody>
    </table>
    {% endif %}

    <!-- Edit SL/TP Modal -->
    <div class="modal-overlay" id="editModal">
        <div class="modal">
            <h3 id="modalTitle">Edit SL / TP</h3>
            <form method="POST" id="editForm" action="">
                <label>Stop Loss (price)</label>
                <input type="number" name="new_sl" id="modalSL" step="any" min="0.000001" required>
                <div class="modal-hint">Enter new stop loss price.</div>
                <label>Take Profit (price)</label>
                <input type="number" name="new_tp" id="modalTP" step="any" min="0.000001" required>
                <div class="modal-hint">Enter new take profit price.</div>
                <div class="modal-actions">
                    <button type="submit" class="btn-save">Save</button>
                    <button type="button" class="btn-cancel" onclick="closeEdit()">Cancel</button>
                </div>
            </form>
        </div>
    </div>
    <script>
        function openEdit(id,label,sl,tp) {
            document.getElementById('modalTitle').textContent='Edit SL/TP — '+label;
            document.getElementById('modalSL').value=sl;
            document.getElementById('modalTP').value=tp;
            document.getElementById('editForm').action='/edit/'+id;
            document.getElementById('editModal').classList.add('active');
        }
        function closeEdit() { document.getElementById('editModal').classList.remove('active'); }
        document.getElementById('editModal').addEventListener('click',function(e){ if(e.target===this)closeEdit(); });
    </script>

    {% if closed_trades %}
    <h2>📋 Closed Trades ({{ closed_trades | length }})</h2>
    <table>
        <thead><tr>
            <th>Coin</th><th>Dir</th><th>Result</th>
            <th>Entry</th><th>Exit</th>
            <th>PnL %</th><th>PnL $</th>
            <th>Vol Ratio</th><th>Trail Note</th>
            <th>Opened</th><th>Closed</th>
        </tr></thead>
        <tbody>
        {% for t in closed_trades | reverse %}
        <tr>
            <td class="coin">{{ t.symbol }}</td>
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
            <td style="color:#ffd600;font-size:12px;">{{ t.vol_ratio or '—' }}×</td>
            <td style="font-size:11px;color:#555;">{{ t.trail_note or '—' }}</td>
            <td style="color:#555;font-size:11px;">{{ t.opened_at }}</td>
            <td style="color:#555;font-size:11px;">{{ t.closed_at }}</td>
        </tr>
        {% endfor %}
        </tbody>
    </table>
    {% endif %}

    {% if not open_trades and not closed_trades %}
    <div class="empty">⏳ Waiting for first Keltner breakout signal... Refreshes every 2 min.</div>
    {% endif %}

</body>
</html>
"""


# ─────────────────────────────────────────
# FLASK ROUTES
# ─────────────────────────────────────────
@app.route("/edit/<trade_id>", methods=["POST"])
def edit_trade(trade_id):
    trade = next((t for t in state["open_trades"] if t["id"] == trade_id), None)
    if not trade:
        return "Trade not found", 404
    try:
        new_sl = float(request.form.get("new_sl", 0))
        new_tp = float(request.form.get("new_tp", 0))
        if new_sl <= 0 or new_tp <= 0:
            return "Invalid values", 400
        trade["sl"]     = round(new_sl, 6)
        trade["tp"]     = round(new_tp, 6)
        trade["sl_pct"] = f"{round(abs(trade['entry']-new_sl)/trade['entry']*100,1)}% (manual)"
        trade["tp_pct"] = f"{round(abs(trade['entry']-new_tp)/trade['entry']*100,1)}% (manual)"
        save_trades()
        log.info(f"EDIT: {trade['symbol']} | SL:{new_sl} TP:{new_tp}")
    except (ValueError, TypeError) as e:
        log.warning(f"Edit failed: {e}")
    return redirect("/")


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
        result = "WIN" if pnl > 0.5 else "LOSS"
        trade.update({
            "status":     "MANUAL",
            "result":     result,
            "closed_at":  datetime.now().strftime("%Y-%m-%d %H:%M"),
            "exit_price": exit_price,
            "pnl_pct":    pnl,
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

    # LONG vs SHORT breakdown
    longs  = [t for t in closed if t.get("direction") == "LONG"]
    shorts = [t for t in closed if t.get("direction") == "SHORT"]
    def side_wr(trades):
        if not trades: return None
        pos = sum(1 for t in trades if t.get("result") in ("WIN", "BE+"))
        return round(pos / len(trades) * 100)
    long_wr  = side_wr(longs)
    short_wr = side_wr(shorts)

    # BTC vs ETH
    btc = [t for t in closed if "BTC" in t.get("symbol", "")]
    eth = [t for t in closed if "ETH" in t.get("symbol", "")]
    btc_wr = side_wr(btc)
    eth_wr = side_wr(eth)

    recs = []
    if win_rate >= 60:
        recs.append(("green",  f"Win+BE rate {win_rate}% — Keltner performing above backtest baseline (45-47%)."))
    elif win_rate >= 45:
        recs.append(("yellow", f"Win+BE rate {win_rate}% — in line with backtest. Profit factor is what matters."))
    else:
        recs.append(("red",    f"Win+BE rate {win_rate}% — below backtest baseline. Consider pausing Keltner."))

    if pf and pf >= 1.25:
        recs.append(("green",  f"Profit factor {pf} — meets or beats backtest PF of 1.25-1.26."))
    elif pf and pf < 1.0:
        recs.append(("red",    f"Profit factor {pf} — below 1.0. Strategy losing money. Review SL/TP."))

    if long_wr is not None and short_wr is not None:
        if long_wr > short_wr + 20:
            recs.append(("yellow", f"LONG win rate {long_wr}% >> SHORT {short_wr}% — consider LONG-only filter."))
        elif short_wr > long_wr + 20:
            recs.append(("yellow", f"SHORT win rate {short_wr}% >> LONG {long_wr}% — consider SHORT-only filter."))
        else:
            recs.append(("green",  f"LONG {long_wr}% / SHORT {short_wr}% — balanced, both directions working."))

    if btc_wr is not None and eth_wr is not None:
        if abs(btc_wr - eth_wr) > 25:
            better = "BTC" if btc_wr > eth_wr else "ETH"
            recs.append(("yellow", f"{better} significantly outperforming — consider sizing up {better}."))

    if wins and losses and abs(avg_loss) > avg_win * 1.5:
        recs.append(("red", f"Avg win {avg_win}% vs avg loss {abs(avg_loss)}% — RR breaking down. SL too tight or TP too far."))

    return {
        "milestone": milestone, "total": n, "win_rate": win_rate, "pf": pf,
        "avg_win": avg_win, "avg_loss": avg_loss, "recs": recs,
        "sides": {"LONG": (len(longs), long_wr), "SHORT": (len(shorts), short_wr)},
        "coins": {"BTC": (len(btc), btc_wr), "ETH": (len(eth), eth_wr)},
    }


@app.route("/")
def index():
    closed = [t for t in state["trades"] if t["status"] != "OPEN"]
    wins   = len([t for t in closed if t["result"] == "WIN"])
    beplus = len([t for t in closed if t["result"] == "BE+"])
    losses = len([t for t in closed if t["result"] == "LOSS"])
    decided = wins + beplus + losses

    wr = round((wins + beplus) / decided * 100, 1) if decided > 0 else 0

    pos_usd = BASE_TRADE_SIZE * LEVERAGE
    gross_win  = sum(
        (t["pnl_pct"] or 0) / 100 * (t.get("position_usd") or pos_usd)
        for t in closed if t.get("result") in ("WIN", "BE+") and t.get("pnl_pct")
    )
    gross_loss = sum(
        abs(t["pnl_pct"] or 0) / 100 * (t.get("position_usd") or pos_usd)
        for t in closed if t.get("result") == "LOSS" and t.get("pnl_pct")
    )
    pf = round(gross_win / gross_loss, 2) if gross_loss > 0 else (round(gross_win, 2) if gross_win > 0 else 0)
    total_pnl_usd = round(gross_win - gross_loss, 2)

    unrealised_usd = round(sum(
        (t.get("unrealised_pnl") or 0) / 100 * (t.get("position_usd") or pos_usd)
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
        insights       = insights,
        base_size      = BASE_TRADE_SIZE,
        leverage       = LEVERAGE,
        scan_count     = state["scan_count"],
        last_scan      = state["last_scan"],
        start_time     = state["start_time"],
        end_time       = state["end_time"],
        running        = state["running"],
        kc_ema         = KC_EMA_PERIOD,
        kc_atr         = KC_ATR_PERIOD,
        kc_mult        = KC_MULTIPLIER,
        vol_mult       = VOL_MULTIPLIER,
        sl_pct         = SL_PCT,
        tp_pct         = TP_PCT,
        trail_t1       = TRAIL_TRIGGER_1,
        trail_t2       = TRAIL_TRIGGER_2,
        trail_l2       = TRAIL_LOCK_2,
        trail_interval = TRAIL_INTERVAL_MIN,
    )


# ─────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────
if __name__ == "__main__":
    load_trades()

    scan_thread  = threading.Thread(target=background_runner, daemon=True)
    trail_thread = threading.Thread(target=trail_runner, daemon=True)
    scan_thread.start()
    trail_thread.start()

    print("\n" + "═" * 58)
    print("  Keltner Breakout — Paper Trader  v1.0")
    print("  Open: 👉  http://localhost:8084")
    print(f"  Symbols:  BTC ETH SOL BNB AVAX (1H candles)")
    print(f"  KC:       EMA{KC_EMA_PERIOD} + ATR{KC_ATR_PERIOD} × {KC_MULTIPLIER}  |  Vol >{VOL_MULTIPLIER}× avg")
    print(f"  SL: {SL_PCT}%  |  TP: {TP_PCT}%  |  Pos: ${BASE_TRADE_SIZE * LEVERAGE:,} notional")
    print(f"  Running until: {state['end_time']}")
    print(f"  Signal scan:   every {SCAN_INTERVAL_MIN} min")
    print(f"  Trail check:   every {TRAIL_INTERVAL_MIN} min")
    print("═" * 58 + "\n")

    app.run(host="0.0.0.0", port=8084, debug=False)
