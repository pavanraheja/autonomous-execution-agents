"""
Alligator Trend Trader — Paper Trader
──────────────────────────────────────
v1.0  2026-03-23  Williams Alligator 6H trend direction, 2H sub-candle entry
                  Symbols : BTC / ETH / PAXG  |  Direction: LONG + SHORT
                  SL: 0.5% | Trail: +1.0% activate, 0.7% gap

v1.1  2026-03-23  SL widened to 1.0% (0.5% was stop-hunted on first candle wick)
                  Variant A : entry at close of 1st 2H sub-candle | SL 1.0%
                  Variant C : entry at close of 2nd 2H sub-candle (both must confirm direction) | SL 1.0%
                  Both variants run simultaneously — decide winner after sample builds
v1.2  2026-03-25  RR 1:2 — TP fixed at 2.0% (SL 1.0% × 2)
                  Partial exit: 75% closed at TP, 25% runner stays open with trail
                  Runner trail activates immediately at TP, gap 0.7% from peak/trough
                  PAXG removed (low ATR, -2015 API errors)
v1.3  2026-03-25  Backtest-optimised (180d, 1115 trades, BTC+ETH):
                  SL 1.0% → 1.5% | TP 2.0% → 3.0% | Exit 75% → 50% | Trail 0.7% → 0.5%
                  Result: EV +0.196%/trade | PF 1.20 | Total +218% vs old trail -13% (ETH)
v1.4  2026-03-26  Reverted to 100% exit at TP — partial exit hurts low-WR strategies
                  Backtest (180d, 1377 trades): 100% TP +103.5% vs 50% runner -448.4%
                  Root cause: 35% WR too low — halving payout flips EV negative
                  Rule: partial exit only viable when base WR >= ~40%
v2.0  2026-03-27  Full optimization (180d backtest, 10 coins tested):
                  Coins: XRP + LINK  (BTC/ETH removed — BTC 29% WR SHORT, directional mismatch)
                  Variant: C only   (double confirmation — A rejected, PF 0.95 vs C PF 1.05+)
                  Spread filter: < 2.0%  (no filter = -14%/mo | <2% = +6%/mo | <1.5% = +8.3%/mo)
                  TP: 3.0% → 3.75%  (RR 2.5×, backtest: PF 2.44, +14%/mo on XRP+LINK)
                  Conviction sizing: 2× when spread < 0.5%  (XRP+LINK ultra-tight = +11.75%/mo)
                  Expected: ~13 trades/mo | WR 49-53% | PF 2.44 | +14%/month
v2.1  2026-03-28  LIVE backtest audit — fresh 180d run:
                  LINK REMOVED: backtest shows WR 29.7% PF 1.06 (not 52%/PF2.22 as claimed in v2.0)
                  BTC SHORT added back: WR 42.9% SHORT, PF 1.36, beats LINK
                  SHORT-only filter added: LONG WR 27-33% across all coins — no edge
                  Conviction 2× disabled: 0% WR in backtest (2 trades only — too risky for live)
                  Fresh backtest: XRP+BTC SHORT, spread<2% → PF ~2.0, WR ~50%, ~$1,100/mo

Backtest results (180d, spread<2%, SHORT-only, Variant C):
  XRP  → PF 2.5  | WR 56.2% | ~7/mo  (+$821/mo on $10k notional)
  BTC  → PF 1.36 | WR 42.9% | ~5/mo  (+$312/mo on $10k notional)
  LINK → PF 1.06 | WR 29.7% | ~3/mo  (+$38/mo  — REMOVED, no edge)
  Spread sensitivity: <1.0%=55%WR | <2.0%=47%WR/$825mo | <3.0%=43%WR/$1038mo

Dashboard: http://localhost:8086
Saves:     Trade Logs/alligator_trades.json
"""

import ccxt
import json
import os
import threading
import time
import logging
import pandas as pd
from datetime import datetime, timedelta, timezone
from flask import Flask, render_template_string, request, redirect
# API keys not needed — public candle data only

PORT = 8086

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────
SYMBOLS = [
    "XRP/USDT:USDT",    # v2.1: WR 56.2% SHORT, PF 2.5  (180d backtest, spread<2%)
    "BTC/USDT:USDT",    # v2.1: WR 42.9% SHORT, PF 1.36 (180d backtest, spread<2%) — added back
    # LINK removed v2.1: backtest WR 29.7% PF 1.06 — no edge (claimed 52% was wrong)
    # SHORT-only filter below removes LONG signals — LONG WR 27-33% across all coins
]

# v2.1: Only trade SHORT direction — LONG WR 27-33% shows no edge
SHORT_ONLY = True

SL_PCT             = 1.5    # Stop loss %
TP_PCT             = 3.75   # Take profit % — RR 2.5× (v2.0: upgraded from 3.0%, backtest PF 2.44)
TP_PARTIAL_EXIT    = 1.0    # 100% exits at TP — no runner (35% base WR too low for partial exit)
TRAIL_GAP_PCT      = 0.5    # kept for compatibility

# ── v2.0: Spread filter — tight alligator = high conviction signal ──────────
SPREAD_MAX_PCT     = 2.0    # Only trade when alligator spread < 2% (no filter = -14%/mo)

# ── v2.1: Conviction sizing DISABLED — 0% WR in backtest (insufficient sample)
CONVICTION_SPREAD  = 0.5    # kept for reference — NOT active
CONVICTION_MULT    = 1.0    # flat 1× — conviction 2× disabled until 20+ samples at spread<0.5%

BASE_TRADE_SIZE    = 2_000  # Margin per trade ($)
LEVERAGE           = 5

SCAN_INTERVAL_MIN  = 10     # Signal scan: every 10 min
TRAIL_INTERVAL_MIN = 5      # Trail SL check: every 5 min
TEST_DURATION_DAYS = 30

TRADE_LOG = "/opt/trader/Trade Logs/alligator_trades.json"

# Alligator (Williams Smoothed Moving Average)
JAW_PERIOD = 13;  JAW_SHIFT   = 8
TEETH_PERIOD = 8; TEETH_SHIFT = 5
LIPS_PERIOD  = 5; LIPS_SHIFT  = 3

# ─────────────────────────────────────────
# SETUP
# ─────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

exchange = ccxt.binanceusdm({
    "enableRateLimit": True,
    "timeout": 30000,
})

app = Flask(__name__)
state = {
    "trades":         [],
    "open_trades":    [],
    "last_scan":      "Not yet run",
    "scan_count":     0,
    "start_time":     datetime.now().strftime("%Y-%m-%d %H:%M"),
    "running":        True,
    "traded_candles": {},  # coin → 6H open ts (dedup guard)
}

# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────
def fetch_candles(symbol, tf, limit=150):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, tf, limit=limit)
        df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        return df
    except Exception as e:
        log.warning(f"fetch_candles {symbol}/{tf}: {e}")
        return None


def calc_alligator(df):
    df = df.copy()
    df["jaw"]   = df["close"].ewm(alpha=1 / JAW_PERIOD,   adjust=False).mean().shift(JAW_SHIFT)
    df["teeth"] = df["close"].ewm(alpha=1 / TEETH_PERIOD, adjust=False).mean().shift(TEETH_SHIFT)
    df["lips"]  = df["close"].ewm(alpha=1 / LIPS_PERIOD,  adjust=False).mean().shift(LIPS_SHIFT)

    def _dir(r):
        if pd.isna(r["jaw"]):
            return "FLAT"
        if r["jaw"] > r["teeth"] > r["lips"]:
            return "SHORT"
        if r["lips"] > r["teeth"] > r["jaw"]:
            return "LONG"
        return "FLAT"

    df["direction"] = df.apply(_dir, axis=1)
    return df


def save_trades():
    os.makedirs(os.path.dirname(TRADE_LOG), exist_ok=True)
    with open(TRADE_LOG, "w") as f:
        json.dump(state["trades"], f, indent=2, default=str)


def load_trades():
    if os.path.exists(TRADE_LOG):
        with open(TRADE_LOG) as f:
            state["trades"] = json.load(f)
        state["open_trades"] = [t for t in state["trades"] if t["status"] == "OPEN"]
        # Rebuild dedup guard from all loaded trades so we don't re-enter same candle after restart
        for t in state["trades"]:
            sym  = t.get("symbol", "")
            var  = t.get("variant", "A")
            c6ts = t.get("c6_open_ts", "")
            if sym and c6ts:
                key = f"{sym}_{var}"
                state["traded_candles"][key] = c6ts
        log.info(f"Loaded {len(state['trades'])} trades ({len(state['open_trades'])} open). Dedup keys: {len(state['traded_candles'])}")


# ─────────────────────────────────────────
# SIGNAL CHECK
# ─────────────────────────────────────────
def check_alligator_signal(symbol, variant="A"):
    """
    Variant A: entry at close of 1st 2H sub-candle (confirmed by direction).
    Variant C: entry at close of 2nd 2H sub-candle (BOTH sub-candles must confirm direction).
    SL: 1.0% for both variants.
    """
    coin = symbol.split("/")[0]

    df6h = fetch_candles(symbol, "6h", 150)
    df2h = fetch_candles(symbol, "2h", 20)
    if df6h is None or df2h is None or len(df6h) < 30:
        return None

    df6h = calc_alligator(df6h)

    # Current forming 6H candle
    c6_open_ts = df6h.iloc[-1]["timestamp"]

    # Alligator direction from the LAST CLOSED 6H candle
    direction = df6h.iloc[-2]["direction"]
    if direction == "FLAT":
        return None

    # v2.1: SHORT-only filter — LONG WR 27-33% shows no edge across all coins
    if SHORT_ONLY and direction != "SHORT":
        log.info(f"  LONG SKIP {coin} [{variant}]: SHORT-only mode active")
        return None

    # All 2H sub-candles within the current 6H candle
    sub_df = df2h[df2h["timestamp"] >= c6_open_ts]
    if len(sub_df) == 0:
        return None

    first_sub = sub_df.iloc[0]
    now_utc   = datetime.now(timezone.utc)

    # ── Variant A: first 2H sub-candle ────────────────────────────
    if variant == "A":
        sub_close_utc = c6_open_ts.to_pydatetime() + timedelta(hours=2)
        if now_utc < sub_close_utc:
            return None  # first sub-candle not closed yet

        if direction == "SHORT" and first_sub["close"] >= first_sub["open"]:
            return None
        if direction == "LONG"  and first_sub["close"] <= first_sub["open"]:
            return None

        entry_candle = first_sub

    # ── Variant C: BOTH first AND second 2H sub-candle confirm ────
    elif variant == "C":
        sub2_close_utc = c6_open_ts.to_pydatetime() + timedelta(hours=4)
        if now_utc < sub2_close_utc:
            return None  # second sub-candle not closed yet

        if len(sub_df) < 2:
            return None

        second_sub = sub_df.iloc[1]

        # Both candles must confirm direction
        if direction == "SHORT":
            if first_sub["close"] >= first_sub["open"]:   return None  # 1st not red
            if second_sub["close"] >= second_sub["open"]: return None  # 2nd not red
        if direction == "LONG":
            if first_sub["close"] <= first_sub["open"]:   return None  # 1st not green
            if second_sub["close"] <= second_sub["open"]: return None  # 2nd not green

        entry_candle = second_sub
    else:
        return None

    entry = round(float(entry_candle["close"]), 6)

    if direction == "SHORT":
        sl_price = round(entry * (1 + SL_PCT / 100), 6)
    else:
        sl_price = round(entry * (1 - SL_PCT / 100), 6)

    # ── Alligator context at entry ─────────────────────────────────
    closed_6h    = df6h.iloc[-2]
    jaw_val      = round(float(closed_6h["jaw"]),   4)
    teeth_val    = round(float(closed_6h["teeth"]), 4)
    lips_val     = round(float(closed_6h["lips"]),  4)
    spread_pct   = round(abs(jaw_val - lips_val) / lips_val * 100, 2)
    candle_range = entry_candle["high"] - entry_candle["low"]
    body_pct     = round(abs(entry_candle["close"] - entry_candle["open"]) / candle_range * 100, 1) if candle_range > 0 else 0

    # ── v2.0: Spread filter — reject wide alligator signals ────────
    if spread_pct > SPREAD_MAX_PCT:
        log.info(f"  SPREAD SKIP {coin} [{variant}]: spread {spread_pct}% > {SPREAD_MAX_PCT}% max")
        return None

    # ── v2.0: Conviction sizing — 2× on ultra-tight spread ─────────
    if spread_pct <= CONVICTION_SPREAD:
        size_mult  = CONVICTION_MULT
        size_label = f"{CONVICTION_MULT}× (conviction — spread {spread_pct}%)"
    else:
        size_mult  = 1.0
        size_label = "1×"
    margin_usd   = int(BASE_TRADE_SIZE * size_mult)
    position_usd = margin_usd * LEVERAGE

    return {
        "id":               f"{coin}_{variant}_{datetime.now().strftime('%m%d_%H%M')}",
        "symbol":           coin,
        "full_symbol":      symbol,
        "variant":          variant,
        "direction":        direction,
        "entry":            entry,
        "sl":               sl_price,
        "sl_original":      sl_price,
        "status":           "OPEN",
        "opened_at":        datetime.now().strftime("%Y-%m-%d %H:%M"),
        "opened_at_utc":    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "closed_at":        None,
        "result":           None,
        "pnl_pct":          None,
        "exit_price":       None,
        "exit_reason":      None,
        "trail_active":     False,
        "trail_sl":         None,
        "trail_note":       "",
        "tp_hit":           False,      # True once 75% is closed at TP
        "tp_exit_price":    None,       # price at which TP was hit
        "runner_active":    False,      # True while 25% runner is still open
        "margin_usd":       margin_usd,
        "position_usd":     position_usd,
        "size_mult":        size_mult,
        "size_label":       size_label,
        "c6_open_ts":       str(c6_open_ts),
        "rule_version":     "v2.0",
        "current_price":    None,
        "unrealised_pnl":   None,
        "jaw":              jaw_val,
        "teeth":            teeth_val,
        "lips":             lips_val,
        "alligator_spread": spread_pct,
        "sub_body_pct":     body_pct,
    }


# ─────────────────────────────────────────
# UPDATE OPEN TRADES (SL + trailing stop)
# ─────────────────────────────────────────
def update_open_trades():
    for trade in state["open_trades"][:]:
        symbol    = trade["full_symbol"]
        direction = trade["direction"]
        entry     = trade["entry"]
        sl_price  = trade["sl"]

        df = fetch_candles(symbol, "15m", 20)  # ~5H of 15m candles
        if df is None:
            continue

        # Only process candles from entry time onwards — prevents pre-entry wicks
        # from triggering SL (e.g. 1st sub-candle high hits SL before we entered)
        opened_utc = trade.get("opened_at_utc", "")
        if opened_utc:
            entry_time = pd.Timestamp(opened_utc, tz="UTC")
            df = df[df["timestamp"] >= entry_time]

        # Migrate old trades that predate v1.2 fields
        trade.setdefault("tp_hit",        False)
        trade.setdefault("tp_exit_price", None)
        trade.setdefault("runner_active", False)

        exit_price = None
        result     = None

        # Compute TP price once
        if direction == "SHORT":
            tp_price = round(entry * (1 - TP_PCT / 100), 6)
        else:
            tp_price = round(entry * (1 + TP_PCT / 100), 6)

        # Process each candle in order to simulate correct SL/trail sequencing
        for _, row in df.iterrows():
            h, l = row["high"], row["low"]

            if direction == "SHORT":
                # ── SL hit (full loss — only before TP) ──────────────────
                if not trade["tp_hit"] and h >= sl_price:
                    exit_price = sl_price
                    result     = "LOSS"
                    trade["exit_reason"] = f"SL hit at {sl_price} (−{SL_PCT}%)"
                    break

                # ── TP hit — 100% exit at TP ─────────────────────────────
                if not trade["tp_hit"] and l <= tp_price:
                    trade["tp_hit"]        = True
                    trade["tp_exit_price"] = tp_price
                    trade["runner_active"] = False
                    trade["trail_note"]    = f"TP hit +{TP_PCT}% | 100% closed @ {tp_price}"
                    exit_price = tp_price
                    result     = "WIN"
                    log.info(f"TP HIT: {trade['symbol']} SHORT | 100% closed @{tp_price}")
                    break

            else:  # LONG
                # ── SL hit ────────────────────────────────────────────────
                if not trade["tp_hit"] and l <= sl_price:
                    exit_price = sl_price
                    result     = "LOSS"
                    trade["exit_reason"] = f"SL hit at {sl_price} (−{SL_PCT}%)"
                    break

                # ── TP hit — 100% exit at TP ─────────────────────────────
                if not trade["tp_hit"] and h >= tp_price:
                    trade["tp_hit"]        = True
                    trade["tp_exit_price"] = tp_price
                    trade["runner_active"] = False
                    trade["trail_note"]    = f"TP hit +{TP_PCT}% | 100% closed @ {tp_price}"
                    exit_price = tp_price
                    result     = "WIN"
                    log.info(f"TP HIT: {trade['symbol']} LONG | 100% closed @{tp_price}")
                    break

        # ── Classify and close ────────────────────────────────────────────
        if exit_price is not None:
            if direction == "SHORT":
                runner_pnl = round((entry - exit_price) / entry * 100, 3)
            else:
                runner_pnl = round((exit_price - entry) / entry * 100, 3)

            if trade["tp_hit"]:
                # 100% closed at TP
                pnl    = TP_PCT
                result = "WIN"
                trade["exit_reason"] = trade.get("exit_reason") or f"TP hit +{TP_PCT}%"
            else:
                pnl = runner_pnl
                if result != "LOSS":
                    if pnl > 0.05:
                        result = "WIN"
                        trade["exit_reason"] = trade.get("exit_reason") or f"Trail closed at +{pnl:.3f}%"
                    elif pnl > -0.1:
                        result = "BE+"
                        trade["exit_reason"] = trade.get("exit_reason") or f"Trail closed near BE ({pnl:+.3f}%)"
                    else:
                        result = "LOSS"
                        trade["exit_reason"] = trade.get("exit_reason") or f"Trail reversed at {pnl:.3f}%"

            trade["status"]        = result
            trade["result"]        = result
            trade["closed_at"]     = datetime.now().strftime("%Y-%m-%d %H:%M")
            trade["exit_price"]    = exit_price
            trade["pnl_pct"]       = pnl
            trade["runner_active"] = False
            state["open_trades"].remove(trade)
            log.info(f"CLOSED: {trade['symbol']} {direction} → {result} | PnL:{pnl:+.3f}% | {trade.get('exit_reason','')}")

        else:
            # Still open — update live tracking
            if len(df) == 0:
                continue  # no post-entry candles yet, skip price update this cycle
            price = df.iloc[-1]["close"]
            trade["current_price"] = round(price, 6)
            if direction == "SHORT":
                trade["unrealised_pnl"] = round((entry - price) / entry * 100, 3)
            else:
                trade["unrealised_pnl"] = round((price - entry) / entry * 100, 3)

    save_trades()


# ─────────────────────────────────────────
# LEARNING INSIGHTS
# ─────────────────────────────────────────
def generate_insights():
    closed = [t for t in state["trades"] if t.get("status") not in ("OPEN", None)]
    n = len(closed)
    if n < 1:
        return None

    milestones = [1, 3, 6, 9, 12, 18, 24, 30]
    next_milestone = next((m for m in milestones if m > n), None)

    wins   = [t for t in closed if t.get("result") == "WIN"]
    losses = [t for t in closed if t.get("result") == "LOSS"]
    beplus = [t for t in closed if t.get("result") == "BE+"]

    wr = len(wins) / n * 100 if n else 0
    win_pnls  = [t.get("pnl_pct", 0) or 0 for t in wins]
    loss_pnls = [t.get("pnl_pct", 0) or 0 for t in losses]
    gross_win  = sum(win_pnls)
    gross_loss = abs(sum(loss_pnls))
    pf    = round(gross_win / gross_loss, 2) if gross_loss > 0 else 99.0
    avg_w = round(sum(win_pnls)  / len(wins),   3) if wins   else 0
    avg_l = round(sum(loss_pnls) / len(losses), 3) if losses else 0

    # Per-coin breakdown
    coins = {}
    for t in closed:
        c = t.get("symbol", "?")
        if c not in coins:
            coins[c] = {"W": 0, "L": 0, "BE": 0}
        r = t.get("result", "")
        if r == "WIN":  coins[c]["W"]  += 1
        elif r == "LOSS": coins[c]["L"] += 1
        elif r == "BE+":  coins[c]["BE"] += 1

    # Per-direction breakdown
    dirs = {"LONG": {"W": 0, "L": 0, "BE": 0}, "SHORT": {"W": 0, "L": 0, "BE": 0}}
    for t in closed:
        d = t.get("direction", "")
        r = t.get("result", "")
        if d in dirs:
            if r == "WIN":  dirs[d]["W"]  += 1
            elif r == "LOSS": dirs[d]["L"] += 1
            elif r == "BE+":  dirs[d]["BE"] += 1

    # Per-variant breakdown
    variants = {"A": {"W": 0, "L": 0, "BE": 0, "pnl": []}, "C": {"W": 0, "L": 0, "BE": 0, "pnl": []}}
    for t in closed:
        v = t.get("variant", "A")
        r = t.get("result", "")
        if v in variants:
            if r == "WIN":    variants[v]["W"]  += 1
            elif r == "LOSS": variants[v]["L"]  += 1
            elif r == "BE+":  variants[v]["BE"] += 1
            variants[v]["pnl"].append(t.get("pnl_pct", 0) or 0)

    recs = []

    # Overall PF recommendation
    if pf < 1.0:
        recs.append(("red", f"Profit factor {pf} is below 1.0 — strategy losing at current settings. "
                            "Consider tightening to 2H only on stronger Alligator spread."))
    elif pf >= 1.5:
        recs.append(("green", f"Profit factor {pf} — solid edge confirmed. Trail is working. "
                              "Consider slightly widening trail gap to let winners run further."))
    else:
        recs.append(("yellow", f"Profit factor {pf} — marginal edge. Monitor 5 more trades before changes."))

    # Win rate
    if wr < 40:
        recs.append(("yellow", f"Win rate {wr:.1f}% — losing more trades than winning (expected with 0.5% SL). "
                               "PF matters more — check that avg wins are > avg losses."))
    elif wr > 60:
        recs.append(("green", f"Win rate {wr:.1f}% — direction accuracy is strong."))

    # Avg win vs avg loss
    if wins and losses and avg_w < abs(avg_l):
        recs.append(("red", f"Avg win ({avg_w:+.2f}%) smaller than avg loss ({avg_l:+.2f}%) — "
                            "trail not capturing enough. Consider reducing TRAIL_GAP from 0.7% to 0.5%."))

    # Per-coin analysis
    for coin, stats in coins.items():
        total_c = stats["W"] + stats["L"] + stats["BE"]
        if total_c >= 3:
            coin_wr = stats["W"] / total_c * 100
            if coin_wr < 30:
                recs.append(("red", f"{coin}: {coin_wr:.0f}% WR over {total_c} trades — "
                                   "underperforming, consider pausing this symbol."))
            elif coin_wr > 65:
                recs.append(("green", f"{coin}: {coin_wr:.0f}% WR over {total_c} trades — strongest performer."))

    # Per-direction analysis
    for d, stats in dirs.items():
        total_d = stats["W"] + stats["L"] + stats["BE"]
        if total_d >= 3:
            dir_wr = stats["W"] / total_d * 100
            if dir_wr < 30:
                recs.append(("red", f"{d} trades: {dir_wr:.0f}% WR over {total_d} — "
                                   "Alligator may be better at one direction in current market regime."))

    # Variant A vs C comparison
    for v, vstats in variants.items():
        total_v = vstats["W"] + vstats["L"] + vstats["BE"]
        if total_v >= 3:
            v_wr = vstats["W"] / total_v * 100
            v_gw  = sum(p for p in vstats["pnl"] if p > 0)
            v_gl  = abs(sum(p for p in vstats["pnl"] if p < 0))
            v_pf  = round(v_gw / v_gl, 2) if v_gl > 0 else 99.0
            label = "1st sub-candle" if v == "A" else "2nd sub-candle (confirmed)"
            color = "green" if v_pf >= 1.2 else ("yellow" if v_pf >= 1.0 else "red")
            recs.append((color, f"Variant {v} ({label}): {total_v} trades | WR {v_wr:.0f}% | PF {v_pf}"))

    if not recs:
        recs.append(("yellow", f"No strong patterns yet at {n} trades — check again at next milestone."))

    return {
        "n": n, "wr": round(wr, 1), "pf": pf,
        "wins": len(wins), "losses": len(losses), "beplus": len(beplus),
        "avg_w": avg_w, "avg_l": avg_l,
        "coins": coins, "dirs": dirs,
        "variants": variants,
        "recs": recs,
        "next_milestone": next_milestone,
        "preliminary": n < 3,
    }


# ─────────────────────────────────────────
# SCAN
# ─────────────────────────────────────────
def run_scan():
    log.info(f"=== Alligator scan #{state['scan_count'] + 1} ===")
    update_open_trades()
    new_signals = 0

    for symbol in SYMBOLS:
        coin = symbol.split("/")[0]

        # v2.0: Variant C only — double confirmation outperforms A (PF 1.05 vs 0.95)
        for variant in ["C"]:
            dedup_key = f"{coin}_{variant}"

            # Skip if already have open trade for this coin+variant
            open_keys = [f"{t['symbol']}_{t.get('variant','A')}" for t in state["open_trades"]]
            if dedup_key in open_keys:
                continue

            signal = check_alligator_signal(symbol, variant)
            if not signal:
                continue

            # Dedup: same 6H candle already traded for this variant
            if state["traded_candles"].get(dedup_key) == signal["c6_open_ts"]:
                log.info(f"  CANDLE SKIP {coin} [{variant}]: already entered c6_open_ts={signal['c6_open_ts']}")
                continue

            state["trades"].append(signal)
            state["open_trades"].append(signal)
            state["traded_candles"][dedup_key] = signal["c6_open_ts"]
            new_signals += 1
            log.info(
                f"NEW SIGNAL [{variant}]: {signal['symbol']} {signal['direction']} | "
                f"Entry:{signal['entry']} SL:{signal['sl']} TP:{round(signal['entry']*(1-TP_PCT/100),6) if signal['direction']=='SHORT' else round(signal['entry']*(1+TP_PCT/100),6)} | "
                f"Spread:{signal['alligator_spread']}% | Size:{signal['size_label']} | "
                f"${signal['position_usd']:,} notional"
            )
            time.sleep(0.2)

    state["last_scan"]  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state["scan_count"] += 1
    save_trades()
    log.info(f"Scan done. {new_signals} new signal(s). {len(state['open_trades'])} open.")


def background_runner():
    end_time = datetime.now() + timedelta(days=TEST_DURATION_DAYS)
    while datetime.now() < end_time and state["running"]:
        run_scan()
        time.sleep(SCAN_INTERVAL_MIN * 60)
    state["running"] = False
    log.info("Test period complete.")


def trail_runner():
    end_time = datetime.now() + timedelta(days=TEST_DURATION_DAYS)
    time.sleep(90)   # stagger start so it doesn't overlap initial scan
    while datetime.now() < end_time and state["running"]:
        if state["open_trades"]:
            log.info(f"Trail check: {len(state['open_trades'])} open trade(s)...")
            update_open_trades()
        time.sleep(TRAIL_INTERVAL_MIN * 60)


# ─────────────────────────────────────────
# HTML DASHBOARD
# ─────────────────────────────────────────
HTML = r"""
<!DOCTYPE html>
<html>
<head>
    <title>Alligator Trend Trader</title>
    <meta http-equiv="refresh" content="120">
    <style>
        * { margin:0; padding:0; box-sizing:border-box; }
        body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
               background:#0d0d0d; color:#e0e0e0; padding:30px; }
        h1   { font-size:24px; color:#fff; margin-bottom:4px; }
        .sub { color:#666; font-size:13px; margin-bottom:22px; }

        .stats { display:flex; gap:14px; margin-bottom:24px; flex-wrap:wrap; }
        .stat-box { background:#1a1a1a; border:1px solid #2a2a2a; border-radius:10px;
                    padding:14px 20px; min-width:120px; }
        .stat-box .label { font-size:11px; color:#555; text-transform:uppercase; letter-spacing:1px; }
        .stat-box .value { font-size:20px; font-weight:700; color:#fff; margin-top:4px; }
        .green  .value { color:#00c853; }
        .red    .value { color:#ff1744; }
        .blue   .value { color:#1e88e5; }
        .gold   .value { color:#ffd600; }
        .purple .value { color:#ce93d8; }
        .beplus .value { color:#aaff00; }

        h2 { font-size:14px; color:#888; margin:26px 0 10px;
             text-transform:uppercase; letter-spacing:1px; }

        table { width:100%; border-collapse:collapse; background:#1a1a1a;
                border-radius:10px; overflow:hidden; margin-bottom:28px; }
        th { background:#222; padding:11px 14px; text-align:left; font-size:11px;
             color:#555; text-transform:uppercase; letter-spacing:1px; }
        td { padding:11px 14px; border-top:1px solid #1e1e1e; font-size:13px; }
        tr:hover td { background:#1f1f1f; }

        .coin    { font-weight:700; color:#fff; font-size:15px; }
        .long    { color:#00c853; font-weight:700; }
        .short   { color:#ff5252; font-weight:700; }
        .win     { color:#00c853; font-weight:600; }
        .loss    { color:#ff1744; font-weight:600; }
        .beplus-t { color:#aaff00; font-weight:600; }
        .open    { color:#1e88e5; font-weight:600; }
        .sl-val  { color:#ff6f00; }
        .pnl-pos { color:#00c853; font-weight:600; }
        .pnl-neg { color:#ff1744; font-weight:600; }
        .pnl-neu { color:#aaa; font-weight:600; }

        .badge { display:inline-block; padding:3px 9px; border-radius:20px; font-size:11px; font-weight:600; }
        .badge-open   { background:#1e3a5f; color:#1e88e5; }
        .badge-win    { background:#1b3a2a; color:#00c853; }
        .badge-loss   { background:#3a1b1b; color:#ff1744; }
        .badge-be     { background:#1a2a00; color:#aaff00; }
        .badge-long   { background:#0d2a0d; color:#00c853; border:1px solid #00c853; }
        .badge-short  { background:#2a0d0d; color:#ff5252; border:1px solid #ff5252; }
        .badge-va     { background:#0d1a2a; color:#40c4ff; border:1px solid #1e88e5; font-size:10px; padding:2px 7px; }
        .badge-vc     { background:#1a0d2a; color:#ce93d8; border:1px solid #9c27b0; font-size:10px; padding:2px 7px; }

        .trail-on  { color:#aaff00; font-size:11px; font-weight:600; }
        .trail-off { color:#333; font-size:11px; }
        .tv-link   { color:#1e88e5; text-decoration:none; font-size:12px; }

        .strategy-box { background:#141414; border:1px solid #252525; border-radius:12px;
                        padding:16px 22px; margin-bottom:22px; }
        .strategy-box h3 { font-size:12px; color:#ffd600; text-transform:uppercase;
                           letter-spacing:1px; margin-bottom:10px; }
        .sg-grid { display:flex; gap:32px; flex-wrap:wrap; }
        .sg { font-size:12px; color:#888; line-height:2.1; }
        .sg span { color:#fff; font-weight:600; }

        .btn-close { background:#1a0000; color:#ff5252; border:1px solid #ff1744;
                     padding:4px 10px; border-radius:6px; font-size:11px; cursor:pointer; }
        .btn-close:hover { background:#ff1744; color:#fff; }
        .btn-scan  { background:#001a2a; color:#40c4ff; border:1px solid #1e88e5;
                     padding:8px 18px; border-radius:8px; font-size:13px; cursor:pointer; margin-bottom:18px; }
        .btn-scan:hover { background:#1e88e5; color:#fff; }

        .running-dot { display:inline-block; width:8px; height:8px; background:#00c853;
                       border-radius:50%; margin-right:6px; animation:pulse 1.5s infinite; }
        @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
        .empty { text-align:center; padding:40px; color:#444; font-size:14px; }

        /* ── Learning Insights ─────────────────────── */
        .insights-box { background:#0d1a0d; border:1px solid #1b5e20; border-radius:10px;
                        padding:18px 22px; margin-bottom:24px; }
        .insights-box h3 { color:#69f0ae; font-size:15px; margin-bottom:6px; }
        .insights-sub { font-size:12px; color:#555; margin-bottom:14px; }
        .insights-meta { display:flex; gap:28px; margin-bottom:14px; flex-wrap:wrap; }
        .insights-meta .im { font-size:12px; color:#888; }
        .insights-meta .im strong { color:#e0e0e0; }
        .rec { display:flex; gap:10px; align-items:flex-start; margin-bottom:10px; }
        .rec-dot { width:8px; height:8px; border-radius:50%; margin-top:4px; flex-shrink:0; }
        .rec-dot.green  { background:#00c853; }
        .rec-dot.yellow { background:#ffd600; }
        .rec-dot.red    { background:#ff1744; }
        .rec-text { font-size:13px; color:#bbb; line-height:1.55; }
        .ig-grid { display:flex; gap:16px; margin-top:14px; flex-wrap:wrap; }
        .ig-box  { background:#0a140a; border:1px solid #1e2e1e; border-radius:8px;
                   padding:12px 16px; min-width:150px; flex:1; }
        .ig-box h4 { font-size:11px; color:#555; margin-bottom:8px;
                     text-transform:uppercase; letter-spacing:1px; }
        .ig-row { display:flex; justify-content:space-between; font-size:12px;
                  color:#777; padding:3px 0; border-bottom:1px solid #0f0f0f; }
        .ig-row:last-child { border-bottom:none; }
        .ig-row span { color:#ccc; font-weight:600; }
        .pending-box { background:#0d0d14; border:1px solid #2a2a3a; border-radius:10px;
                       padding:18px 22px; margin-bottom:24px; color:#555; font-size:13px;
                       text-align:center; }
    </style>
</head>
<body>
    <h1>
        {% if running %}<span class="running-dot"></span>{% endif %}
        Alligator Trend Trader
        <span style="color:#ffd600;font-size:18px;">— Paper Trader</span>
        <span style="color:#555;font-size:14px;margin-left:12px;">v1.0</span>
    </h1>
    <p class="sub">
        Binance USDT Futures · BTC / ETH / PAXG ·
        Started {{ start_time }} ·
        Last scan: {{ last_scan }} (scan #{{ scan_count }})
    </p>

    <!-- Strategy Summary -->
    <div class="strategy-box">
        <h3>Williams Alligator — 6H Trend · 2H Sub-candle Entry · A/B Variant Test</h3>
        <div class="sg-grid">
            <div class="sg">
                Direction<br>
                Variant A<br>
                Variant C<br>
                Stop Loss<br>
                Trail
            </div>
            <div class="sg">
                <span>Alligator on 6H (Jaw/Teeth/Lips) — LONG or SHORT</span><br>
                <span style="color:#40c4ff;">Entry at close of 1st 2H sub-candle confirming direction</span><br>
                <span style="color:#ce93d8;">Entry at close of 2nd 2H sub-candle (BOTH must confirm)</span><br>
                <span>{{ sl_pct }}% fixed (v1.1 — widened from 0.5%)</span><br>
                <span>+{{ trail_start }}% activate · {{ trail_gap }}% gap from peak/trough</span>
            </div>
            <div class="sg">
                Scan interval<br>
                Trail check<br>
                Position size<br>
                Backtest PF<br>
                &nbsp;
            </div>
            <div class="sg">
                <span>Every {{ scan_min }} min</span><br>
                <span>Every {{ trail_min }} min</span><br>
                <span>${{ "{:,}".format(margin_usd) }} margin · {{ leverage }}× = ${{ "{:,}".format(position_usd) }} notional</span><br>
                <span>BTC:1.09 · ETH:1.53 · PAXG:1.56 (90-day backtest)</span><br>
                &nbsp;
            </div>
        </div>
    </div>

    <!-- Stats Row -->
    <div class="stats">
        <div class="stat-box blue">
            <div class="label">Total Trades</div>
            <div class="value">{{ total }}</div>
        </div>
        <div class="stat-box" style="background:#1a1a1a;">
            <div class="label">Open</div>
            <div class="value" style="color:#1e88e5;">{{ open_count }}</div>
        </div>
        <div class="stat-box green">
            <div class="label">Wins</div>
            <div class="value">{{ wins }}</div>
        </div>
        <div class="stat-box red">
            <div class="label">Losses</div>
            <div class="value">{{ losses }}</div>
        </div>
        <div class="stat-box beplus">
            <div class="label">BE+</div>
            <div class="value">{{ beplus }}</div>
        </div>
        <div class="stat-box gold">
            <div class="label">Win Rate</div>
            <div class="value">{{ wr }}%</div>
        </div>
        <div class="stat-box {% if pf >= 1.5 %}green{% elif pf >= 1.0 %}gold{% else %}red{% endif %}">
            <div class="label">Profit Factor</div>
            <div class="value">{{ pf }}</div>
        </div>
        <div class="stat-box purple">
            <div class="label">Long / Short</div>
            <div class="value">{{ longs }} / {{ shorts }}</div>
        </div>
    </div>

    <form action="/manual_scan" method="post" style="display:inline;">
        <button type="submit" class="btn-scan">⚡ Run Scan Now</button>
    </form>

    <!-- Open Trades -->
    <h2>Open Positions ({{ open_count }})</h2>
    {% if open_trades %}
    <table>
        <tr>
            <th>Coin</th>
            <th>Variant</th>
            <th>Dir</th>
            <th>Entry</th>
            <th>SL</th>
            <th>Trail SL</th>
            <th>Current</th>
            <th>Unrealised PnL</th>
            <th>Entry Context</th>
            <th>Opened</th>
            <th>TradingView</th>
            <th>Action</th>
        </tr>
        {% for t in open_trades %}
        <tr>
            <td class="coin">{{ t.symbol }}</td>
            <td>
                <span class="badge {% if t.variant=='C' %}badge-vc{% else %}badge-va{% endif %}">
                    {{ t.variant or 'A' }}
                </span>
            </td>
            <td>
                <span class="badge {% if t.direction=='LONG' %}badge-long{% else %}badge-short{% endif %}">
                    {{ t.direction }}
                </span>
            </td>
            <td>{{ t.entry }}</td>
            <td class="sl-val">{{ t.sl }}</td>
            <td>
                {% if t.trail_active %}
                    <span class="trail-on">{{ t.trail_sl }} 🔒</span>
                {% else %}
                    <span class="trail-off">—</span>
                {% endif %}
            </td>
            <td>{{ t.current_price or '—' }}</td>
            <td>
                {% if t.unrealised_pnl is not none %}
                    <span class="{% if t.unrealised_pnl > 0 %}pnl-pos{% elif t.unrealised_pnl < 0 %}pnl-neg{% else %}pnl-neu{% endif %}">
                        {{ '%+.3f' % t.unrealised_pnl }}%
                    </span>
                {% else %}—{% endif %}
            </td>
            <td style="font-size:11px;color:#666;line-height:1.7;">
                {% if t.alligator_spread is defined %}
                    Spread: <span style="color:#ffd600;">{{ t.alligator_spread }}%</span><br>
                    Body: <span style="color:#aaa;">{{ t.sub_body_pct }}%</span>
                {% else %}—{% endif %}
            </td>
            <td style="color:#666;font-size:12px;">{{ t.opened_at }}</td>
            <td>
                <a class="tv-link" href="https://www.tradingview.com/chart/?symbol=BINANCE:{{ t.symbol }}USDT.P&interval=240" target="_blank">chart ↗</a>
            </td>
            <td>
                <form action="/close/{{ t.id }}" method="post" style="display:inline;">
                    <button type="submit" class="btn-close">Close</button>
                </form>
            </td>
        </tr>
        {% endfor %}
    </table>
    {% else %}
    <div class="empty">No open positions</div>
    {% endif %}

    <!-- Learning Insights -->
    {% if insights %}
    <div class="insights-box">
        <h3>Learning Insights
            {% if insights.preliminary %}
            <span style="font-size:12px;color:#555;font-weight:400;margin-left:8px;">
                (preliminary · {{ insights.n }} trade{{ 's' if insights.n > 1 else '' }} — stronger signal at 3+)
            </span>
            {% else %}
            <span style="font-size:12px;color:#555;font-weight:400;margin-left:8px;">
                {{ insights.n }} closed trades
                {% if insights.next_milestone %} · next review at {{ insights.next_milestone }}{% endif %}
            </span>
            {% endif %}
        </h3>
        <div class="insights-meta">
            <div class="im">Closed: <strong>{{ insights.n }}</strong></div>
            <div class="im">Wins: <strong style="color:#00c853;">{{ insights.wins }}</strong></div>
            <div class="im">Losses: <strong style="color:#ff1744;">{{ insights.losses }}</strong></div>
            <div class="im">BE+: <strong style="color:#aaff00;">{{ insights.beplus }}</strong></div>
            <div class="im">Win Rate: <strong>{{ insights.wr }}%</strong></div>
            <div class="im">Profit Factor: <strong>{{ insights.pf }}</strong></div>
            <div class="im">Avg Win: <strong>{{ '%+.3f' % insights.avg_w }}%</strong></div>
            <div class="im">Avg Loss: <strong>{{ '%+.3f' % insights.avg_l }}%</strong></div>
        </div>
        {% for color, text in insights.recs %}
        <div class="rec">
            <div class="rec-dot {{ color }}"></div>
            <div class="rec-text">{{ text }}</div>
        </div>
        {% endfor %}
        <div class="ig-grid">
            <div class="ig-box">
                <h4>By Coin</h4>
                {% for coin, s in insights.coins.items() %}
                <div class="ig-row">
                    {{ coin }}
                    <span>{{ s.W }}W / {{ s.L }}L / {{ s.BE }}BE</span>
                </div>
                {% endfor %}
            </div>
            <div class="ig-box">
                <h4>By Direction</h4>
                {% for d, s in insights.dirs.items() %}
                <div class="ig-row">
                    {{ d }}
                    <span>{{ s.W }}W / {{ s.L }}L / {{ s.BE }}BE</span>
                </div>
                {% endfor %}
            </div>
            <div class="ig-box">
                <h4>Variant A vs C</h4>
                {% for v, s in insights.variants.items() %}
                {% set total_v = s.W + s.L + s.BE %}
                <div class="ig-row">
                    <span style="color:{% if v=='C' %}#ce93d8{% else %}#40c4ff{% endif %};">{{ v }}</span>
                    <span>
                        {% if total_v > 0 %}
                            {{ s.W }}W/{{ s.L }}L · {{ (s.W/total_v*100)|round|int }}% WR
                        {% else %}—{% endif %}
                    </span>
                </div>
                {% endfor %}
            </div>
        </div>
    </div>
    {% else %}
    <div class="pending-box">
        Learning Insights will appear after the first trade closes
    </div>
    {% endif %}

    <!-- Trade History -->
    <h2>Trade History ({{ closed_count }} closed)</h2>
    {% if closed_trades %}
    <table>
        <tr>
            <th>ID</th>
            <th>Coin</th>
            <th>Dir</th>
            <th>Entry</th>
            <th>Exit</th>
            <th>SL</th>
            <th>PnL %</th>
            <th>PnL $</th>
            <th>Variant</th>
            <th>Result</th>
            <th>Exit Reason</th>
            <th>Entry Context</th>
            <th>Opened</th>
            <th>Closed</th>
        </tr>
        {% for t in closed_trades | reverse %}
        <tr>
            <td style="color:#444;font-size:11px;">{{ t.id }}</td>
            <td class="coin">{{ t.symbol }}</td>
            <td>
                <span class="badge {% if t.direction=='LONG' %}badge-long{% else %}badge-short{% endif %}">
                    {{ t.direction }}
                </span>
            </td>
            <td>{{ t.entry }}</td>
            <td>{{ t.exit_price or '—' }}</td>
            <td class="sl-val">{{ t.sl_original }}</td>
            <td>
                {% if t.pnl_pct is not none %}
                    <span class="{% if t.pnl_pct > 0 %}pnl-pos{% elif t.pnl_pct < 0 %}pnl-neg{% else %}pnl-neu{% endif %}">
                        {{ '%+.3f' % t.pnl_pct }}%
                    </span>
                {% else %}—{% endif %}
            </td>
            <td>
                {% if t.pnl_pct is not none %}
                    {% set usd = t.position_usd * t.pnl_pct / 100 %}
                    <span class="{% if usd > 0 %}pnl-pos{% elif usd < 0 %}pnl-neg{% else %}pnl-neu{% endif %}" style="font-size:12px;">
                        ${{ '%+.2f' % usd }}
                    </span>
                {% else %}—{% endif %}
            </td>
            <td>
                {% if t.result == 'WIN' %}
                    <span class="badge badge-win">WIN</span>
                {% elif t.result == 'BE+' %}
                    <span class="badge badge-be">BE+</span>
                {% elif t.result == 'LOSS' %}
                    <span class="badge badge-loss">LOSS</span>
                {% else %}
                    <span class="badge">{{ t.result }}</span>
                {% endif %}
            </td>
            <td>
                <span class="badge {% if t.variant=='C' %}badge-vc{% else %}badge-va{% endif %}">
                    {{ t.variant or 'A' }}
                </span>
            </td>
            <td style="font-size:11px;color:#777;max-width:180px;">
                {{ t.exit_reason or ('Trail 🔒' if t.trail_active else '—') }}
            </td>
            <td style="font-size:11px;color:#666;line-height:1.7;">
                {% if t.alligator_spread is defined %}
                    Spread: <span style="color:#ffd600;">{{ t.alligator_spread }}%</span><br>
                    Body: <span style="color:#aaa;">{{ t.sub_body_pct }}%</span>
                {% else %}—{% endif %}
            </td>
            <td style="color:#555;font-size:12px;">{{ t.opened_at }}</td>
            <td style="color:#555;font-size:12px;">{{ t.closed_at or '—' }}</td>
        </tr>
        {% endfor %}
    </table>
    {% else %}
    <div class="empty">No closed trades yet</div>
    {% endif %}

</body>
</html>
"""


# ─────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────
@app.route("/")
def dashboard():
    closed  = [t for t in state["trades"] if t.get("status") != "OPEN"]
    wins    = [t for t in closed if t.get("result") == "WIN"]
    losses  = [t for t in closed if t.get("result") == "LOSS"]
    beplus  = [t for t in closed if t.get("result") == "BE+"]
    longs   = [t for t in state["trades"] if t.get("direction") == "LONG"]
    shorts  = [t for t in state["trades"] if t.get("direction") == "SHORT"]
    n_closed = len(closed)
    wr = round(len(wins) / n_closed * 100, 1) if n_closed > 0 else 0.0
    gw = sum(t.get("pnl_pct", 0) or 0 for t in wins)
    gl = abs(sum(t.get("pnl_pct", 0) or 0 for t in losses))
    pf = round(gw / gl, 2) if gl > 0 else (99.0 if gw > 0 else 0.0)

    return render_template_string(
        HTML,
        running      = state["running"],
        start_time   = state["start_time"],
        last_scan    = state["last_scan"],
        scan_count   = state["scan_count"],
        sl_pct       = SL_PCT,
        trail_start  = TP_PCT,
        trail_gap    = TRAIL_GAP_PCT,
        scan_min     = SCAN_INTERVAL_MIN,
        trail_min    = TRAIL_INTERVAL_MIN,
        margin_usd   = BASE_TRADE_SIZE,
        leverage     = LEVERAGE,
        position_usd = int(BASE_TRADE_SIZE * LEVERAGE),
        total        = len(state["trades"]),
        open_count   = len(state["open_trades"]),
        closed_count = n_closed,
        wins         = len(wins),
        losses       = len(losses),
        beplus       = len(beplus),
        longs        = len(longs),
        shorts       = len(shorts),
        wr           = wr,
        pf           = pf,
        open_trades  = state["open_trades"],
        closed_trades = closed,
        insights     = generate_insights(),
    )


@app.route("/close/<trade_id>", methods=["POST"])
def close_trade(trade_id):
    for t in state["open_trades"][:]:
        if t["id"] == trade_id:
            df = fetch_candles(t["full_symbol"], "1m", 3)
            exit_price = round(float(df.iloc[-1]["close"]), 6) if df is not None else t["entry"]
            entry = t["entry"]
            if t["direction"] == "SHORT":
                pnl = round((entry - exit_price) / entry * 100, 3)
            else:
                pnl = round((exit_price - entry) / entry * 100, 3)
            result = "WIN" if pnl > 0.05 else ("BE+" if pnl > -0.1 else "LOSS")
            t["status"]     = result
            t["result"]     = result
            t["closed_at"]  = datetime.now().strftime("%Y-%m-%d %H:%M")
            t["exit_price"] = exit_price
            t["pnl_pct"]    = pnl
            state["open_trades"].remove(t)
            save_trades()
            log.info(f"MANUAL CLOSE: {t['symbol']} {t['direction']} → {result} | PnL:{pnl:+.3f}%")
            break
    return redirect("/")


@app.route("/manual_scan", methods=["POST"])
def manual_scan():
    threading.Thread(target=run_scan, daemon=True).start()
    return redirect("/")


@app.route("/api/state")
def api_state():
    from flask import jsonify
    return jsonify({
        "open":   len(state["open_trades"]),
        "total":  len(state["trades"]),
        "scan":   state["last_scan"],
        "running": state["running"],
    })


# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────
if __name__ == "__main__":
    log.info("=" * 62)
    log.info("  Alligator Trend Trader — Paper Trader  v2.0")
    log.info(f"  Coins   : {', '.join(s.split('/')[0] for s in SYMBOLS)}")
    log.info(f"  SL/TP   : {SL_PCT}% / {TP_PCT}% (RR {TP_PCT/SL_PCT:.1f}×) | 100% exit at TP")
    log.info(f"  Spread  : < {SPREAD_MAX_PCT}% filter | Conviction 2× when < {CONVICTION_SPREAD}%")
    log.info(f"  Variant : C only (double 2H confirmation)")
    log.info(f"  Expect  : ~13 trades/mo | WR 49-53% | PF 2.44 | +14%/mo")
    log.info(f"  Scan    : every {SCAN_INTERVAL_MIN} min  |  Trail: every {TRAIL_INTERVAL_MIN} min")
    log.info(f"  Dashboard: http://localhost:{PORT}")
    log.info("=" * 62)

    load_trades()

    scan_thread  = threading.Thread(target=background_runner, daemon=True)
    trail_thread = threading.Thread(target=trail_runner,      daemon=True)
    scan_thread.start()
    trail_thread.start()

    app.run(host="0.0.0.0", port=PORT, debug=False)
