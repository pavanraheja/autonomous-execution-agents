"""
MFI Monitor & Manual Trader
──────────────────────────────
Scans Binance USDⓈ-M Futures every 30 min for extreme MFI readings:
  - 6H MFI > 80  → overbought (SHORT opportunity)
  - 6H MFI < 20  → oversold   (LONG opportunity)
1D MFI shown alongside for context.
TradingView perpetual link per coin.

Manual trade execution:
  - Via dashboard form or POST /api/trade (JSON)
  - Exchange-native SL/TP: STOP_MARKET + TAKE_PROFIT_MARKET algo orders
  - Optional trailing stop (same 2-stage logic as auto trader)

Dashboard: http://localhost:8087
Logs:      Trade Logs/manual_trades.json
"""

import ccxt
import json
import os
import threading
import time
import logging
from datetime import datetime, timedelta
from flask import Flask, render_template_string, request, redirect, jsonify
from config import LIVE_BINANCE_API_KEY, LIVE_BINANCE_API_SECRET

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────
MFI_LENGTH         = 14
MFI_OB             = 80          # overbought threshold → SHORT signal
MFI_OS             = 20          # oversold threshold   → LONG signal
MIN_VOLUME_USDT    = 5_000_000
SYMBOL_REFRESH_H   = 6
SCAN_INTERVAL_MIN  = 30
BASE_TRADE_SIZE    = 200         # USDT margin → $1,000 notional at 5× (paper simulation)
LEVERAGE           = 5
TRAIL_TRIGGER_1    = 1.5         # % profit → activate stage 1
TRAIL_LOCK_1       = 0.5         # % to lock at stage 1
TRAIL_TRIGGER_2    = 3.0
TRAIL_LOCK_2       = 1.0
TRAIL_INTERVAL_MIN = 1

TRADE_LOG      = "/opt/trader/Trade Logs/manual_trades.json"
PAPER_TRADE_LOG = "/opt/trader/Trade Logs/paper_monitor_trades.json"

# ── Paper trading (auto signals from OB list) ────────────
ATR_PERIOD        = 14
ATR_SL_MULT       = 1.5    # SL = entry + ATR × 1.5
ATR_TP_MULT       = 4.0    # TP_ATR = entry − ATR × 4  (backtest: better than ×3)
TP_RR_RATIO       = 2.0    # TP_RR  = entry − (SL dist × 2)
MIN_GREENS        = 3      # need 3 consecutive green candles before first red
ENTRY_CONFIRM_MIN = 15     # wait 15 min into candle 2 before checking entry
ENTRY_EXPIRE_MIN  = 180    # expire signal if >3H into candle 2
MFI_MIN_ENTRY     = 75     # live MFI must be ≥ 75 at entry (raised from 70 — 70-75 zone has 0% WR)
MFI_MAX_ENTRY     = 85     # skip if MFI ≥ 85 — live data: 85-97 zone has 10% WR (2026-04-01)
COOLDOWN_HOURS    = 24     # skip coin for 24H after any loss on that coin
SIGNAL_CHECK_MIN  = 10     # armed-signal / position monitor interval

# ── ATR trailing SL (paper trades) ───────────────────────
# SL only ever moves DOWN (tighter) for SHORTs — never loosens.
# Three milestones based on how many ATRs of profit we have:
TRAIL_BE_ATRS     = 0.5    # profit ≥ 0.5 ATR → SL moves to entry (breakeven)
TRAIL_LOOSE_ATRS  = 1.5    # profit ≥ 1.5 ATR → trail at price + 1.2× ATR
TRAIL_TIGHT_ATRS  = 3.0    # profit ≥ 3.0 ATR → trail at price + 0.8× ATR
TRAIL_LOOSE_MULT  = 0.9    # tightened from 1.2 — locks in ~6-8% on winning trades (2026-04-01)
TRAIL_TIGHT_MULT  = 0.8

# ── Partial close at 1:1 RR ─────────────────────────────
PARTIAL_TP_ENABLED = True   # auto-close 50% at 1:1 RR midpoint, keep 50% riding to 2:1

# 8087 paper-trades the full universe — same as 8083 live (no coin whitelist).
# Filter stack (MFI 75-85 window, decay guard, 1D confluence) does the curation.

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─────────────────────────────────────────
# EXCHANGE  (USDⓈ-M Futures)
# ─────────────────────────────────────────
exchange = ccxt.binanceusdm({
    "apiKey":  LIVE_BINANCE_API_KEY,
    "secret":  LIVE_BINANCE_API_SECRET,
    "options": {"defaultType": "future"},
})

app   = Flask(__name__)
state = {
    "overbought":        [],    # coins with 6H MFI > 80
    "oversold":          [],    # coins with 6H MFI < 20
    "last_scan":         "Scanning…",
    "next_scan_at":      None,
    "scan_count":        0,
    "scan_running":      False,
    "watchlist":         [],
    "watchlist_updated": None,
    "trades":            [],
    "open_trades":       [],
    "running":           True,
    # paper trading
    "paper_signals":     {},   # coin → armed signal (waiting 15-min confirm)
    "paper_trades":      [],   # all paper trades (open + closed)
    "prev_overbought":   [],   # OB list from prior scan (pattern window)
}

# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────
def calculate_mfi(df, length=14):
    tp  = (df["high"] + df["low"] + df["close"]) / 3
    rmf = tp * df["volume"]
    pos = rmf.where(tp > tp.shift(1), 0)
    neg = rmf.where(tp < tp.shift(1), 0)
    ps  = pos.rolling(length).sum()
    ns  = neg.rolling(length).sum().replace(0, 1e-10)
    return 100 - (100 / (1 + ps / ns))

def fetch_candles(symbol, tf, limit):
    import pandas as pd
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, tf, limit=limit)
        df    = pd.DataFrame(ohlcv, columns=["timestamp","open","high","low","close","volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        return df
    except:
        return None

def calc_qty(ccxt_sym, notional_usdt, price):
    raw = notional_usdt / price
    try:
        if not exchange.markets:
            exchange.load_markets()
        return float(exchange.amount_to_precision(ccxt_sym, raw))
    except:
        if price >= 1000: return round(raw, 3)
        elif price >= 1:  return round(raw, 1)
        else:             return round(raw, 0)

def set_leverage(ccxt_sym):
    try:
        exchange.set_leverage(LEVERAGE, ccxt_sym)
    except Exception as e:
        log.warning(f"Leverage set failed {ccxt_sym}: {e}")

def cancel_order(ccxt_sym, order_id):
    if not order_id:
        return
    try:
        exchange.fapiPrivateDeleteAlgoOrder({"algoId": order_id})
    except Exception as algo_err:
        try:
            exchange.cancel_order(order_id, ccxt_sym)
        except Exception as e:
            log.warning(f"Cancel {order_id} failed: algo={algo_err} regular={e}")

def get_position(ccxt_sym):
    try:
        positions = exchange.fetch_positions([ccxt_sym])
        for p in positions:
            if p.get("symbol") == ccxt_sym:
                contracts = float(p.get("contracts", 0) or 0)
                return -contracts if p.get("side") == "short" else contracts
        return 0.0
    except Exception as e:
        log.warning(f"Position check {ccxt_sym}: {e}")
        return None

def get_exit_price(ccxt_sym, direction):
    try:
        trades     = exchange.fetch_my_trades(ccxt_sym, limit=10)
        close_side = "buy" if direction == "SHORT" else "sell"
        closing    = [t for t in reversed(trades) if t.get("side") == close_side]
        if closing:
            return float(closing[0]["price"])
    except:
        pass
    return None

def get_balance():
    try:
        info      = exchange.fetch_balance().get("info", {})
        available = round(float(info.get("availableBalance", 0)), 2)
        unrealised = round(float(info.get("totalUnrealizedProfit", 0)), 2)
        total_usdt = sum(
            float(a.get("walletBalance", 0))
            for a in info.get("assets", [])
            if a["asset"] in ("USDT","USDC","FDUSD","BUSD") and float(a.get("walletBalance", 0)) > 0
        )
        return {"usdt": round(total_usdt, 2), "available": available,
                "unrealised": unrealised, "total_usd": round(total_usdt + unrealised, 2)}
    except:
        return {"usdt": 0, "available": 0, "unrealised": 0, "total_usd": 0}

def save_trades():
    os.makedirs(os.path.dirname(TRADE_LOG), exist_ok=True)
    with open(TRADE_LOG, "w") as f:
        json.dump(state["trades"], f, indent=2, default=str)

def load_trades():
    if os.path.exists(TRADE_LOG):
        with open(TRADE_LOG) as f:
            state["trades"] = json.load(f)
        state["open_trades"] = [t for t in state["trades"] if t["status"] == "OPEN"]
        log.info(f"Loaded {len(state['trades'])} manual trades ({len(state['open_trades'])} open)")

def refresh_watchlist():
    try:
        markets   = exchange.load_markets(reload=True)
        candidates = [
            s for s, m in markets.items()
            if m.get("active") and m.get("type") == "swap" and s.endswith("/USDT:USDT")
        ]
        tickers  = exchange.fetch_tickers(candidates)
        filtered = sorted([
            s for s in candidates
            if (tickers.get(s, {}).get("quoteVolume") or 0) >= MIN_VOLUME_USDT
        ])
        state["watchlist"]         = filtered
        state["watchlist_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        log.info(f"Watchlist: {len(filtered)} pairs")
    except Exception as e:
        log.warning(f"Watchlist refresh failed: {e}")

def maybe_refresh_watchlist():
    if not state["watchlist"] or not state["watchlist_updated"]:
        refresh_watchlist(); return
    last = datetime.strptime(state["watchlist_updated"], "%Y-%m-%d %H:%M")
    if datetime.now() - last > timedelta(hours=SYMBOL_REFRESH_H):
        refresh_watchlist()

# ─────────────────────────────────────────
# PAPER TRADING — ATR + PATTERN ENGINE
# ─────────────────────────────────────────
def calculate_atr(df, period=14):
    import pandas as pd
    high, low, prev_close = df["high"], df["low"], df["close"].shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def save_paper_trades():
    os.makedirs(os.path.dirname(PAPER_TRADE_LOG), exist_ok=True)
    with open(PAPER_TRADE_LOG, "w") as f:
        json.dump(state["paper_trades"], f, indent=2, default=str)

def load_paper_trades():
    if os.path.exists(PAPER_TRADE_LOG):
        with open(PAPER_TRADE_LOG) as f:
            state["paper_trades"] = json.load(f)
        log.info(f"Loaded {len(state['paper_trades'])} paper monitor trades")

def coin_in_cooldown(coin):
    """Return True if this coin had a loss closed within the last COOLDOWN_HOURS hours."""
    cutoff = datetime.now() - timedelta(hours=COOLDOWN_HOURS)
    for t in state["paper_trades"]:
        if t.get("symbol") == coin and t.get("result") == "LOSS":
            closed_str = t.get("closed_at", "")
            if closed_str:
                try:
                    closed_dt = datetime.strptime(str(closed_str)[:16], "%Y-%m-%d %H:%M")
                    if closed_dt >= cutoff:
                        return True
                except ValueError:
                    pass
    return False

def detect_pattern(symbol, coin):
    """
    Look for: [GREEN×3][RED×1] on last 4 closed 6H candles.
    MFI must have been ≥ 80 in the green window.
    Returns signal dict or None.
    """
    import pandas as pd
    df = fetch_candles(symbol, "6h", 28)
    if df is None or len(df) < 10:
        return None

    df["mfi"] = calculate_mfi(df, MFI_LENGTH)
    df["atr"] = calculate_atr(df, ATR_PERIOD)

    # iloc[-1] = current forming candle (c2), iloc[-2] = last closed (c1)
    c1 = df.iloc[-2]   # must be RED
    g1 = df.iloc[-3]   # must be GREEN
    g2 = df.iloc[-4]   # must be GREEN
    g3 = df.iloc[-5]   # must be GREEN

    if c1["close"] >= c1["open"]:
        return None  # c1 not red
    if not (g1["close"] > g1["open"] and g2["close"] > g2["open"] and g3["close"] > g3["open"]):
        return None  # 3 greens not present

    # MFI must have been ≥ OB threshold in the 3-green window
    mfi_green_window = df["mfi"].iloc[-5:-2]
    if mfi_green_window.max() < MFI_OB:
        return None  # was never overbought in that window

    # MFI on first red candle must be ≥ MFI_MIN_ENTRY (75) — below this, signal is too weak to arm
    if float(df["mfi"].iloc[-2]) < MFI_MIN_ENTRY:
        return None

    atr_val       = float(df["atr"].iloc[-2])
    c2_ts         = df.iloc[-1]["timestamp"]
    c2_open_price = float(df.iloc[-1]["open"])
    c2_open_time  = c2_ts.to_pydatetime().replace(tzinfo=None)

    # Don't arm if c2 is already too old (>ENTRY_EXPIRE_MIN since it opened)
    elapsed = (datetime.utcnow() - c2_open_time).total_seconds() / 60
    if elapsed > ENTRY_EXPIRE_MIN:
        return None

    return {
        "coin":          coin,
        "ccxt_sym":      symbol,
        "c2_open_price": c2_open_price,
        "c2_open_time":  c2_open_time,
        "atr":           round(atr_val, 8),
        "mfi_at_signal": round(float(df["mfi"].iloc[-2]), 2),
        "armed_at":      datetime.now().strftime("%Y-%m-%d %H:%M"),
    }

def check_and_arm_signals():
    """
    After each scan: check current + previous OB coins for the 3G+1R pattern.
    Arms new signals; skips coins already armed or in open paper trade.
    """
    candidates = {c["coin"]: c for c in state["overbought"]}
    for c in state["prev_overbought"]:
        if c["coin"] not in candidates:
            candidates[c["coin"]] = c

    open_coins = {t["symbol"] for t in state["paper_trades"] if t["status"] == "OPEN"}

    for coin, info in candidates.items():
        if coin in state["paper_signals"] or coin in open_coins:
            continue
        sig = detect_pattern(info["ccxt_sym"], coin)
        if sig:
            sig["recently_ob"] = info.get("recently_ob", False)  # carry flag from scan → entry guard
            state["paper_signals"][coin] = sig
            log.info(f"📡 PAPER ARMED: {coin} | c2_open={sig['c2_open_price']} "
                     f"ATR={sig['atr']:.6f} MFI={sig['mfi_at_signal']} recently_ob={sig['recently_ob']}")

def check_armed_entries():
    """
    Every SIGNAL_CHECK_MIN: for each armed signal check if entry conditions met:
      - ≥ 15 min since c2 opened
      - < 3H since c2 opened (not expired)
      - Current price < c2_open_price (candle 2 still red)
      - Live MFI ≥ 70
    """
    to_remove = []
    open_coins = {t["symbol"] for t in state["paper_trades"] if t["status"] == "OPEN"}

    for coin, sig in list(state["paper_signals"].items()):
        if coin in open_coins:
            to_remove.append(coin); continue

        c2_open_time = sig["c2_open_time"]
        if isinstance(c2_open_time, str):
            c2_open_time = datetime.strptime(c2_open_time[:19], "%Y-%m-%d %H:%M:%S")

        elapsed_min = (datetime.utcnow() - c2_open_time).total_seconds() / 60

        if elapsed_min < ENTRY_CONFIRM_MIN:
            continue  # too early

        if elapsed_min > ENTRY_EXPIRE_MIN:
            log.info(f"PAPER EXPIRED: {coin} ({elapsed_min:.0f} min elapsed)")
            to_remove.append(coin); continue

        # Fetch current state
        symbol = sig["ccxt_sym"]
        df = fetch_candles(symbol, "6h", 20)
        if df is None:
            continue

        df["mfi"] = calculate_mfi(df, MFI_LENGTH)

        try:
            current_price = exchange.fetch_ticker(symbol)["last"]
        except:
            current_price = float(df.iloc[-1]["close"])

        mfi_live      = float(df["mfi"].iloc[-1])
        c2_open_price = sig["c2_open_price"]

        if current_price >= c2_open_price:
            log.info(f"PAPER CANCELLED: {coin} — candle 2 went green")
            to_remove.append(coin); continue

        if mfi_live < MFI_MIN_ENTRY:
            log.info(f"PAPER CANCELLED: {coin} — MFI {mfi_live:.1f} < {MFI_MIN_ENTRY}")
            to_remove.append(coin); continue

        if mfi_live >= MFI_MAX_ENTRY:
            log.info(f"PAPER CANCELLED: {coin} — MFI {mfi_live:.1f} >= {MFI_MAX_ENTRY} (still OB, skip)")
            to_remove.append(coin)
            continue

        # Rec 2: decay guard — signal has faded too much since c1 candle, reversal already underway
        mfi_at_signal = sig.get("mfi_at_signal")
        if isinstance(mfi_at_signal, (int, float)) and mfi_live < mfi_at_signal - 8:
            log.info(f"PAPER CANCELLED: {coin} — MFI decayed {mfi_at_signal:.1f}→{mfi_live:.1f} (stale signal, >8pt drop)")
            to_remove.append(coin)
            continue

        # Rec 3: 1D MFI confluence — skip SHORT if daily is deeply oversold (coin in strong daily upswing)
        df1d_entry = fetch_candles(symbol, "1d", 20)
        if df1d_entry is not None and len(df1d_entry) >= 16:
            df1d_entry["mfi"] = calculate_mfi(df1d_entry, MFI_LENGTH)
            mfi_1d_live = round(float(df1d_entry["mfi"].iloc[-2]), 2)
            if mfi_1d_live < 45:
                log.info(f"PAPER CANCELLED: {coin} — 1D MFI {mfi_1d_live:.1f} < 45 (daily upswing, SHORT risky)")
                to_remove.append(coin)
                continue

        # Rec 5: borderline signal guard — recently_ob signals (peaked OB then decayed) need tighter entry
        # Mirrors 8083 exactly: recently_ob flag set in scan, same condition at entry
        if sig.get("recently_ob") and mfi_live < 77:
            log.info(f"PAPER CANCELLED: {coin} — recently_ob signal, entry MFI {mfi_live:.1f} < 77 (too decayed)")
            to_remove.append(coin)
            continue

        if coin_in_cooldown(coin):
            log.info(f"PAPER CANCELLED: {coin} — 24H cooldown after loss")
            to_remove.append(coin)
            continue

        # All clear → enter paper trade
        atr    = sig["atr"]
        entry  = round(current_price, 8)
        sl_raw  = entry + atr * ATR_SL_MULT
        sl_dist = sl_raw - entry
        # Cap SL at 8% regardless of ATR — prevents micro-cap blowups (e.g. ONT -13%)
        max_sl_dist = entry * 0.08
        sl     = round(entry + min(sl_dist, max_sl_dist), 8)
        tp_rr  = round(entry - (sl - entry) * TP_RR_RATIO, 8)
        tp_atr = round(entry - atr * ATR_TP_MULT, 8)

        sl_pct     = round((sl - entry) / entry * 100, 2)
        tp_rr_pct  = round((entry - tp_rr)  / entry * 100, 2)
        tp_atr_pct = round((entry - tp_atr) / entry * 100, 2)

        trade = {
            "id":             f"PM_{coin}_{datetime.now().strftime('%m%d_%H%M')}",
            "symbol":         coin,
            "direction":      "SHORT",
            "entry":          entry,
            "sl":             sl,
            "tp_rr":          tp_rr,
            "tp_atr":         tp_atr,
            "atr":            atr,
            "sl_pct":         f"+{sl_pct:.2f}%",
            "tp_rr_pct":      f"-{tp_rr_pct:.2f}%",
            "tp_atr_pct":     f"-{tp_atr_pct:.2f}%",
            "mfi_at_signal":  sig["mfi_at_signal"],
            "mfi_at_entry":   round(mfi_live, 2),
            "c2_open_price":  c2_open_price,
            "elapsed_min":    round(elapsed_min, 0),
            "status":         "OPEN",
            "result":         None,
            "exit_price":     None,
            "exit_type":      None,
            "tp_rr_hit":      False,
            "tp_atr_hit":     False,
            "pnl_pct":        None,
            "opened_at":      datetime.now().strftime("%Y-%m-%d %H:%M"),
            "closed_at":      None,
            # ── Partial close / position sizing ──────────────────
            "tp_partial":         round((entry + tp_rr) / 2, 8),  # 1:1 level
            "partial_closed":     False,
            "partial_close_price": None,
            "partial_close_pct":  None,
            "partial_close_usd":  None,
            "position_mult":      1.0,
        }

        state["paper_trades"].append(trade)
        save_paper_trades()
        to_remove.append(coin)
        log.info(f"📝 PAPER ENTRY: {coin} SHORT @ {entry} | "
                 f"SL={sl}(+{sl_pct}%) TP_RR={tp_rr}(-{tp_rr_pct}%) TP_ATR={tp_atr}(-{tp_atr_pct}%) | "
                 f"ATR={atr:.6f} elapsed={elapsed_min:.0f}min")

    for coin in to_remove:
        state["paper_signals"].pop(coin, None)

def monitor_paper_positions():
    """Check open paper trades for SL/TP hits using live ticker price."""
    open_trades = [t for t in state["paper_trades"] if t["status"] == "OPEN"]
    if not open_trades:
        return

    changed = False
    for trade in open_trades:
        symbol = trade["symbol"] + "/USDT:USDT"
        try:
            current_price = exchange.fetch_ticker(symbol)["last"]
        except:
            continue

        pnl           = round((trade["entry"] - current_price) / trade["entry"] * 100, 3)
        position_mult = float(trade.get("position_mult", 1.0))
        trade["current_price"]  = current_price
        trade["unrealised_pnl"] = pnl
        trade["unrealised_usd"] = round(pnl / 100 * BASE_TRADE_SIZE * LEVERAGE * position_mult, 2)

        # ── ATR Trailing SL ───────────────────────────────────
        # Only for SHORT trades. SL can only decrease (tighten). Never loosens.
        atr    = float(trade.get("atr", 0))
        entry  = trade["entry"]
        if atr > 0 and pnl > 0:   # only trail when in profit
            profit_atrs = (entry - current_price) / atr

            if profit_atrs >= TRAIL_TIGHT_ATRS:
                candidate_sl = round(current_price + atr * TRAIL_TIGHT_MULT, 8)
                stage        = f"Tight trail (+{TRAIL_TIGHT_ATRS}ATR) — SL={atr * TRAIL_TIGHT_MULT:.4g}× ATR above price"
            elif profit_atrs >= TRAIL_LOOSE_ATRS:
                candidate_sl = round(current_price + atr * TRAIL_LOOSE_MULT, 8)
                stage        = f"Loose trail (+{TRAIL_LOOSE_ATRS}ATR) — SL={atr * TRAIL_LOOSE_MULT:.4g}× ATR above price"
            elif profit_atrs >= TRAIL_BE_ATRS:
                candidate_sl = entry   # breakeven
                stage        = "Breakeven lock"
            else:
                candidate_sl = None
                stage        = None

            if candidate_sl is not None and candidate_sl < trade["sl"]:
                old_sl = trade["sl"]
                trade["sl"]         = candidate_sl
                trade["trail_stage"] = stage
                if old_sl != candidate_sl:
                    log.info(f"  TRAIL {trade['symbol']}: SL {old_sl:.6g} → {candidate_sl:.6g} ({stage}) | profit={profit_atrs:.2f}ATR")
            elif stage:
                trade["trail_stage"] = stage   # keep label even if SL already tighter

        # ── Auto 50% Partial Close at 1:1 RR ─────────────────
        if PARTIAL_TP_ENABLED and not trade.get("partial_closed") and pnl > 0:
            tp_partial = trade.get("tp_partial")
            if tp_partial is None:
                # compute dynamically for trades loaded from JSON without this field
                tp_partial = round((trade["entry"] + trade["tp_rr"]) / 2, 8)
                trade["tp_partial"] = tp_partial
            if current_price <= tp_partial:
                locked_pnl = round((trade["entry"] - tp_partial) / trade["entry"] * 100, 3)
                locked_usd = round(locked_pnl / 100 * BASE_TRADE_SIZE * LEVERAGE * 0.5, 2)
                trade["partial_closed"]      = True
                trade["partial_close_price"] = tp_partial
                trade["partial_close_pct"]   = locked_pnl
                trade["partial_close_usd"]   = locked_usd
                trade["position_mult"]       = 0.5
                position_mult                = 0.5
                # Move SL to breakeven so the remaining 50% risks nothing
                if trade["sl"] > trade["entry"]:
                    trade["sl"]          = trade["entry"]
                    trade["trail_stage"] = "Breakeven lock (after partial close)"
                changed = True
                log.info(f"🔒 PARTIAL CLOSE {trade['symbol']}: 50% locked @ {tp_partial} "
                         f"| +{locked_pnl}% (+${locked_usd}) | 50% still open → TP_RR={trade['tp_rr']}")
                # Recompute unrealised_usd with new position_mult = 0.5
                trade["unrealised_usd"] = round(pnl / 100 * BASE_TRADE_SIZE * LEVERAGE * 0.5, 2)

        # Progress / distance metrics for dashboard display
        entry = trade["entry"]; sl = trade["sl"]; tp = trade["tp_rr"]
        tp_range   = abs(entry - tp)
        tp_progress = round((entry - current_price) / tp_range * 100, 1) if tp_range else 0
        to_sl_pct  = round((sl - current_price) / current_price * 100, 2)   # remaining cushion to SL
        to_tp_pct  = round((current_price - tp) / current_price * 100, 2)   # remaining drop needed to TP
        trade["tp_progress"]  = max(0, tp_progress)   # 0–100% how far toward TP
        trade["to_sl_pct"]    = to_sl_pct             # + means SL is above (safe); drops to 0 = danger
        trade["to_tp_pct"]    = to_tp_pct             # how much more price needs to fall

        sl_hit     = current_price >= trade["sl"]
        tp_rr_hit  = current_price <= trade["tp_rr"]
        tp_atr_hit = current_price <= trade["tp_atr"]

        # Record hits (both independently)
        if tp_rr_hit:  trade["tp_rr_hit"]  = True
        if tp_atr_hit: trade["tp_atr_hit"] = True

        # Exit logic: SL hard stop, then first TP reached
        if sl_hit:
            exit_pnl = round((trade["entry"] - current_price) / trade["entry"] * 100, 3)
            # Trail SL may have moved above entry — positive exit = BE+, not LOSS
            result = "BE+" if exit_pnl > 0.05 else "LOSS"
            status = result
            icon   = "🟡" if result == "BE+" else "❌"
            trade.update({"status": status, "result": result, "exit_price": current_price,
                          "exit_type": "SL", "pnl_pct": exit_pnl,
                          "closed_at": datetime.now().strftime("%Y-%m-%d %H:%M")})
            changed = True
            log.info(f"{icon} PAPER {result}: {trade['symbol']} SL @ {current_price} | {exit_pnl}%")

        elif tp_rr_hit and tp_atr_hit:
            exit_pnl = round((trade["entry"] - current_price) / trade["entry"] * 100, 3)
            trade.update({"status": "WIN", "result": "WIN_BOTH", "exit_price": current_price,
                          "exit_type": "TP_BOTH", "pnl_pct": exit_pnl,
                          "closed_at": datetime.now().strftime("%Y-%m-%d %H:%M")})
            changed = True
            log.info(f"✅ PAPER WIN (BOTH): {trade['symbol']} @ {current_price} | {exit_pnl}%")

        elif tp_rr_hit:
            exit_pnl = round((trade["entry"] - current_price) / trade["entry"] * 100, 3)
            trade.update({"status": "WIN", "result": "WIN_RR", "exit_price": current_price,
                          "exit_type": "TP_RR", "pnl_pct": exit_pnl,
                          "closed_at": datetime.now().strftime("%Y-%m-%d %H:%M")})
            changed = True
            log.info(f"✅ PAPER WIN (TP_RR): {trade['symbol']} @ {current_price} | {exit_pnl}%")

        elif tp_atr_hit:
            exit_pnl = round((trade["entry"] - current_price) / trade["entry"] * 100, 3)
            trade.update({"status": "WIN", "result": "WIN_ATR", "exit_price": current_price,
                          "exit_type": "TP_ATR", "pnl_pct": exit_pnl,
                          "closed_at": datetime.now().strftime("%Y-%m-%d %H:%M")})
            changed = True
            log.info(f"✅ PAPER WIN (TP_ATR): {trade['symbol']} @ {current_price} | {exit_pnl}%")

    if changed:
        save_paper_trades()

def signal_monitor_runner():
    time.sleep(90)  # let startup complete
    while state["running"]:
        check_armed_entries()
        monitor_paper_positions()
        time.sleep(SIGNAL_CHECK_MIN * 60)


# ─────────────────────────────────────────
# SCAN
# ─────────────────────────────────────────
def run_scan():
    state["scan_running"] = True
    log.info(f"MFI scan #{state['scan_count']+1} — {len(state['watchlist'])} pairs…")
    maybe_refresh_watchlist()

    overbought = []
    oversold   = []

    for symbol in state["watchlist"]:
        coin = symbol.split("/")[0]

        # 6H MFI — use last fully closed candle (iloc[-2])
        df6h = fetch_candles(symbol, "6h", 22)
        if df6h is None or len(df6h) < 16:
            time.sleep(0.15)
            continue
        df6h["mfi"] = calculate_mfi(df6h, MFI_LENGTH)
        mfi_6h      = round(float(df6h["mfi"].iloc[-2]), 2)

        # 3-candle lookback — mirrors 8083: catches coins that peaked OB then decayed (e.g. USUAL)
        mfi_peak    = round(float(df6h["mfi"].iloc[-4:-1].max()), 2)
        recently_ob = mfi_peak > MFI_OB and mfi_6h <= MFI_OB

        if mfi_6h < MFI_OS or mfi_6h > MFI_OB or recently_ob:
            # 1D MFI
            df1d  = fetch_candles(symbol, "1d", 20)
            mfi_1d = None
            if df1d is not None and len(df1d) >= 16:
                df1d["mfi"] = calculate_mfi(df1d, MFI_LENGTH)
                mfi_1d      = round(float(df1d["mfi"].iloc[-2]), 2)

            rec = {
                "coin":        coin,
                "ccxt_sym":    symbol,
                "mfi_6h":      mfi_6h,
                "mfi_peak":    mfi_peak,
                "recently_ob": recently_ob,
                "mfi_1d":      mfi_1d,
                "tv_link":     f"https://www.tradingview.com/chart/?symbol=BINANCE:{coin}USDT.P",
                "scanned_at":  datetime.now().strftime("%H:%M"),
                "direction":   "SHORT" if (mfi_6h >= MFI_OB or recently_ob) else "LONG",
            }
            if mfi_6h >= MFI_OB or recently_ob:
                overbought.append(rec)
            else:
                oversold.append(rec)

        time.sleep(0.15)

    overbought.sort(key=lambda x: (x["recently_ob"], -x["mfi_6h"]))
    oversold.sort(key=lambda x: x["mfi_6h"])

    state["prev_overbought"] = state["overbought"][:]   # save before overwriting
    state["overbought"]      = overbought
    state["oversold"]        = oversold
    state["last_scan"]       = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state["next_scan_at"]    = (datetime.now() + timedelta(minutes=SCAN_INTERVAL_MIN)).strftime("%H:%M")
    state["scan_count"]     += 1
    state["scan_running"]    = False
    log.info(f"Scan done: {len(overbought)} overbought, {len(oversold)} oversold")

    # Check for paper trade patterns on current + previous OB coins
    check_and_arm_signals()

# ─────────────────────────────────────────
# TRADE EXECUTION
# ─────────────────────────────────────────
def open_manual_trade(coin, ccxt_sym, direction, sl_price, tp_price, size_mult, use_trail, notes=""):
    sl_price  = float(sl_price)
    tp_price  = float(tp_price)
    size_mult = float(size_mult)
    notional  = BASE_TRADE_SIZE * size_mult * LEVERAGE

    log.info(f"Manual {direction}: {coin} | SL={sl_price} TP={tp_price} | ${notional:.0f} notional")

    try:
        set_leverage(ccxt_sym)

        ticker       = exchange.fetch_ticker(ccxt_sym)
        price        = ticker["last"]
        qty          = calc_qty(ccxt_sym, notional, price)

        if qty <= 0:
            return {"ok": False, "msg": f"qty=0 for {coin} (position too small for exchange minimum)"}

        # Entry
        entry_side   = "sell" if direction == "SHORT" else "buy"
        entry_order  = exchange.create_order(ccxt_sym, "market", entry_side, qty)
        actual_entry = float(entry_order.get("average") or entry_order.get("price") or price)
        if actual_entry == 0:
            actual_entry = price

        # Exchange SL + TP (algo orders)
        close_side = "buy" if direction == "SHORT" else "sell"
        sl_order = exchange.create_order(ccxt_sym, "STOP_MARKET", close_side, qty, params={
            "stopPrice": sl_price, "closePosition": True, "workingType": "MARK_PRICE",
        })
        tp_order = exchange.create_order(ccxt_sym, "TAKE_PROFIT_MARKET", close_side, qty, params={
            "stopPrice": tp_price, "closePosition": True, "workingType": "MARK_PRICE",
        })
        sl_id = sl_order.get("id") if sl_order else None
        tp_id = tp_order.get("id") if tp_order else None

        # PnL reference %
        if direction == "SHORT":
            sl_pct = round((sl_price - actual_entry) / actual_entry * 100, 2)
            tp_pct = round((actual_entry - tp_price) / actual_entry * 100, 2)
        else:
            sl_pct = round((actual_entry - sl_price) / actual_entry * 100, 2)
            tp_pct = round((tp_price - actual_entry) / actual_entry * 100, 2)

        trade = {
            "id":             f"{coin}_{datetime.now().strftime('%m%d_%H%M')}",
            "symbol":         coin,
            "ccxt_symbol":    ccxt_sym,
            "direction":      direction,
            "entry":          round(actual_entry, 6),
            "sl":             sl_price,
            "sl_original":    sl_price,
            "tp":             tp_price,
            "sl_pct":         f"{sl_pct:.2f}%",
            "tp_pct":         f"{tp_pct:.2f}%",
            "qty":            qty,
            "size_mult":      size_mult,
            "position_usd":   notional,
            "use_trail":      use_trail,
            "trailing_stage": 0,
            "trail_note":     "",
            "notes":          notes,
            "status":         "OPEN",
            "result":         None,
            "pnl_pct":        None,
            "pnl_usd":        None,
            "exit_price":     None,
            "opened_at":      datetime.now().strftime("%Y-%m-%d %H:%M"),
            "closed_at":      None,
            "entry_order_id": entry_order.get("id"),
            "sl_order_id":    sl_id,
            "tp_order_id":    tp_id,
            "rule_version":   "manual-v1.0",
        }

        state["trades"].append(trade)
        state["open_trades"].append(trade)
        save_trades()
        log.info(f"✅ {direction} OPEN: {coin} | entry={actual_entry} SL={sl_price}({sl_pct:.1f}%) TP={tp_price}({tp_pct:.1f}%) | algoSL={sl_id} algoTP={tp_id}")
        return {"ok": True, "trade": trade}

    except Exception as e:
        log.error(f"Trade failed {coin}: {e}")
        return {"ok": False, "msg": str(e)}

# ─────────────────────────────────────────
# MONITOR OPEN TRADES
# ─────────────────────────────────────────
def monitor_open_trades():
    for trade in state["open_trades"][:]:
        ccxt_sym  = trade["ccxt_symbol"]
        coin      = trade["symbol"]
        direction = trade["direction"]

        try:
            ticker        = exchange.fetch_ticker(ccxt_sym)
            current_price = ticker["last"]

            if direction == "SHORT":
                current_pnl = round((trade["entry"] - current_price) / trade["entry"] * 100, 3)
            else:
                current_pnl = round((current_price - trade["entry"]) / trade["entry"] * 100, 3)

            trade["current_price"]  = round(current_price, 6)
            trade["unrealised_pnl"] = current_pnl
            trade["unrealised_usd"] = round(current_pnl / 100 * trade["position_usd"], 2)

            # ── Trail stop ─────────────────────────────────────────
            if trade.get("use_trail"):
                stage = trade.get("trailing_stage", 0)
                entry = trade["entry"]

                if stage == 0 and current_pnl >= TRAIL_TRIGGER_1:
                    if direction == "SHORT":
                        lock = round(entry * (1 - TRAIL_LOCK_1 / 100), 6)
                        valid = lock < trade["sl"]
                    else:
                        lock = round(entry * (1 + TRAIL_LOCK_1 / 100), 6)
                        valid = lock > trade["sl"]
                    if valid:
                        cancel_order(ccxt_sym, trade.get("sl_order_id"))
                        close_side = "buy" if direction == "SHORT" else "sell"
                        new_sl = exchange.create_order(ccxt_sym, "STOP_MARKET", close_side, trade["qty"], params={
                            "stopPrice": lock, "closePosition": True, "workingType": "MARK_PRICE"
                        })
                        trade.update({"sl": lock, "sl_order_id": new_sl.get("id") if new_sl else None,
                                      "trailing_stage": 1, "trail_note": f"S1→{lock}"})
                        log.info(f"TRAIL S1: {coin} SL→{lock}")
                    stage = 1

                if stage == 1 and current_pnl >= TRAIL_TRIGGER_2:
                    if direction == "SHORT":
                        lock = round(entry * (1 - TRAIL_LOCK_2 / 100), 6)
                        valid = lock < trade["sl"]
                    else:
                        lock = round(entry * (1 + TRAIL_LOCK_2 / 100), 6)
                        valid = lock > trade["sl"]
                    if valid:
                        cancel_order(ccxt_sym, trade.get("sl_order_id"))
                        close_side = "buy" if direction == "SHORT" else "sell"
                        new_sl = exchange.create_order(ccxt_sym, "STOP_MARKET", close_side, trade["qty"], params={
                            "stopPrice": lock, "closePosition": True, "workingType": "MARK_PRICE"
                        })
                        trade.update({"sl": lock, "sl_order_id": new_sl.get("id") if new_sl else None,
                                      "trailing_stage": 2, "trail_note": f"S2→{lock}"})
                        log.info(f"TRAIL S2: {coin} SL→{lock}")

            # ── Check if exchange closed position ──────────────────
            pos_size = get_position(ccxt_sym)
            if pos_size is None:
                continue

            if abs(pos_size) < 0.0001:
                cancel_order(ccxt_sym, trade.get("sl_order_id"))
                cancel_order(ccxt_sym, trade.get("tp_order_id"))

                exit_price = get_exit_price(ccxt_sym, direction) or current_price

                if direction == "SHORT":
                    pnl_pct = round((trade["entry"] - exit_price) / trade["entry"] * 100, 3)
                    is_win  = exit_price <= trade["tp"] * 1.005
                else:
                    pnl_pct = round((exit_price - trade["entry"]) / trade["entry"] * 100, 3)
                    is_win  = exit_price >= trade["tp"] * 0.995

                result  = "WIN" if is_win else ("BE+" if pnl_pct > 0.05 else "LOSS")
                pnl_usd = round(pnl_pct / 100 * trade["position_usd"], 2)

                trade.update({
                    "status": result, "result": result,
                    "exit_price": round(exit_price, 6),
                    "pnl_pct": pnl_pct, "pnl_usd": pnl_usd,
                    "closed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "sl_order_id": None, "tp_order_id": None,
                })
                state["open_trades"].remove(trade)
                icon = "✅" if result == "WIN" else ("🟡" if result == "BE+" else "❌")
                log.info(f"{icon} CLOSED {coin} {direction} → {result} | {pnl_pct}% (${pnl_usd})")

        except Exception as e:
            log.warning(f"Monitor error {coin}: {e}")

    save_trades()

# ─────────────────────────────────────────
# BACKGROUND THREADS
# ─────────────────────────────────────────
def scan_runner():
    run_scan()
    while state["running"]:
        time.sleep(SCAN_INTERVAL_MIN * 60)
        run_scan()

def trail_runner():
    time.sleep(60)
    while state["running"]:
        if state["open_trades"]:
            monitor_open_trades()
        time.sleep(TRAIL_INTERVAL_MIN * 60)

# ─────────────────────────────────────────
# DASHBOARD HTML
# ─────────────────────────────────────────
HTML = r"""
<!DOCTYPE html>
<html>
<head>
    <title>MFI Monitor</title>
    <meta http-equiv="refresh" content="120">
    <style>
        * { margin:0; padding:0; box-sizing:border-box; }
        body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
               background:#0d0d0d; color:#e0e0e0; padding:30px; }
        h1   { font-size:24px; color:#fff; margin-bottom:4px; }
        .sub { color:#666; font-size:13px; margin-bottom:20px; }

        .info-bar { background:#111; border:1px solid #2a2a2a; border-radius:10px;
                    padding:12px 20px; margin-bottom:22px; display:flex;
                    gap:28px; flex-wrap:wrap; align-items:center; }
        .info-bar span { font-size:13px; color:#888; }
        .info-bar strong { color:#fff; }
        .scan-dot { display:inline-block; width:8px; height:8px; border-radius:50%;
                    background:#ffd600; margin-right:6px; animation:pulse 1s infinite; }
        @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }

        .bal-bar { background:#111827; border:1px solid #1f2937; border-radius:10px;
                   padding:12px 20px; margin-bottom:22px; font-size:13px; color:#9ca3af; }
        .bal-bar strong { color:#fff; font-size:15px; }

        h2 { font-size:13px; color:#888; margin:22px 0 10px;
             text-transform:uppercase; letter-spacing:1.5px; }

        table { width:100%; border-collapse:collapse; background:#111; border-radius:10px;
                overflow:hidden; margin-bottom:28px; }
        th { background:#1a1a1a; padding:10px 14px; text-align:left; font-size:11px;
             color:#555; text-transform:uppercase; letter-spacing:1px; }
        td { padding:10px 14px; border-top:1px solid #1a1a1a; font-size:13px; }
        tr:hover td { background:#161616; }

        .coin { font-weight:700; color:#fff; font-size:14px; }

        /* MFI color bands */
        .mfi-ob   { color:#ff1744; font-weight:700; }
        .mfi-ob2  { color:#ff6f00; font-weight:700; }
        .mfi-os   { color:#00c853; font-weight:700; }
        .mfi-os2  { color:#69f0ae; font-weight:600; }
        .mfi-mid  { color:#555; }

        .dir-short { display:inline-block; background:#1a0000; color:#ff1744;
                     border:1px solid #ff1744; border-radius:4px;
                     padding:2px 8px; font-size:11px; font-weight:700; }
        .dir-long  { display:inline-block; background:#001a00; color:#00c853;
                     border:1px solid #00c853; border-radius:4px;
                     padding:2px 8px; font-size:11px; font-weight:700; }

        .tv-link { color:#1e88e5; text-decoration:none; font-size:12px; }
        .tv-link:hover { text-decoration:underline; }

        .btn-trade { background:#1a0040; color:#b57bee; border:1px solid #7c3aed;
                     padding:4px 12px; border-radius:6px; font-size:11px;
                     font-weight:700; cursor:pointer; }
        .btn-trade:hover { background:#7c3aed; color:#fff; }
        .btn-close { background:#1a0000; color:#ff5252; border:1px solid #ff1744;
                     padding:4px 10px; border-radius:6px; font-size:11px; font-weight:600; cursor:pointer; }
        .btn-close:hover { background:#ff1744; color:#fff; }
        .btn-add  { background:#001a00; color:#00c853; border:1px solid #00c853;
                    padding:4px 10px; border-radius:6px; font-size:11px; font-weight:600; cursor:pointer; }
        .btn-add:hover { background:#00c853; color:#000; }

        .badge { display:inline-block; padding:3px 9px; border-radius:20px; font-size:11px; font-weight:600; }
        .badge-open   { background:#1e3a5f; color:#1e88e5; }
        .badge-win    { background:#1b3a2a; color:#00c853; }
        .badge-loss   { background:#3a1b1b; color:#ff1744; }
        .badge-beplus { background:#1a2a00; color:#aaff00; }

        .pnl-pos { color:#00c853; font-weight:600; }
        .pnl-neg { color:#ff1744; font-weight:600; }

        .trail-badge { font-size:10px; color:#69f0ae; margin-left:4px; }

        .empty { text-align:center; padding:30px; color:#333; font-size:13px; }
        .scanning { text-align:center; padding:20px; color:#555; font-size:13px; }

        /* Trade modal */
        .modal-overlay { display:none; position:fixed; inset:0; background:rgba(0,0,0,0.8);
                         z-index:1000; align-items:center; justify-content:center; }
        .modal-overlay.active { display:flex; }
        .modal { background:#1a1a1a; border:1px solid #333; border-radius:14px;
                 padding:28px 32px; width:420px; max-width:95vw; }
        .modal h3 { color:#fff; font-size:16px; margin-bottom:4px; }
        .modal .sub2 { color:#666; font-size:12px; margin-bottom:20px; }
        .modal label { font-size:11px; color:#666; text-transform:uppercase;
                       letter-spacing:1px; display:block; margin-bottom:4px; margin-top:14px; }
        .modal input, .modal select, .modal textarea {
            width:100%; background:#111; border:1px solid #333; border-radius:8px;
            color:#fff; font-size:14px; padding:8px 12px; outline:none; }
        .modal input:focus, .modal select:focus { border-color:#7c3aed; }
        .modal textarea { resize:vertical; height:60px; font-family:inherit; }
        .hint { font-size:10px; color:#444; margin-top:3px; }
        .modal-row { display:flex; gap:12px; }
        .modal-row > div { flex:1; }
        .trail-row { display:flex; align-items:center; gap:10px; margin-top:14px; }
        .trail-row label { margin:0; font-size:12px; color:#aaa; text-transform:none; letter-spacing:0; }
        .trail-row input[type=checkbox] { width:auto; }
        .modal-actions { display:flex; gap:10px; margin-top:22px; }
        .modal-actions button { flex:1; padding:10px; border-radius:8px; font-size:14px;
                                font-weight:700; cursor:pointer; border:none; }
        .btn-confirm { background:#7c3aed; color:#fff; }
        .btn-confirm:hover { background:#6d28d9; }
        .btn-cancel  { background:#222; color:#888; }
        .dir-toggle { display:flex; gap:8px; margin-top:4px; }
        .dir-toggle label { margin:0; padding:7px 18px; border-radius:7px; cursor:pointer;
                            font-size:13px; font-weight:700; border:1px solid #333;
                            text-transform:none; letter-spacing:0; color:#888; }
        .dir-toggle input[type=radio] { display:none; }
        .dir-toggle input[type=radio]:checked + label.short-lbl { background:#1a0000; color:#ff1744; border-color:#ff1744; }
        .dir-toggle input[type=radio]:checked + label.long-lbl  { background:#001a00; color:#00c853; border-color:#00c853; }
    </style>
</head>
<body>
    <h1>📡 MFI Monitor</h1>
    <p class="sub">Binance USDⓈ-M Futures · 6H MFI extremes · 30 min scan · Port 8087</p>

    <div class="info-bar">
        {% if scan_running %}
        <span><span class="scan-dot"></span><strong>Scanning…</strong></span>
        {% else %}
        <span>Last scan: <strong>{{ last_scan }}</strong></span>
        <span>Next scan: <strong>~{{ next_scan_at }}</strong></span>
        {% endif %}
        <span>Overbought (>80): <strong style="color:#ff6f00;">{{ overbought|length }}</strong></span>
        <span>Oversold (&lt;20): <strong style="color:#69f0ae;">{{ oversold|length }}</strong></span>
        <span>Watchlist: <strong>{{ watchlist_count }}</strong> pairs</span>
        <form method="POST" action="/scan" style="margin:0;">
            <button type="submit" style="background:#1a1a2e;color:#7c3aed;border:1px solid #7c3aed;
                padding:4px 12px;border-radius:6px;font-size:11px;font-weight:700;cursor:pointer;">
                ↻ Scan Now
            </button>
        </form>
    </div>

    <div class="bal-bar">
        💰 Wallet: <strong>${{ bal.total_usd }}</strong>
        &nbsp;·&nbsp; Available: <strong>${{ bal.available }}</strong>
        &nbsp;·&nbsp; Unrealised: <strong style="color:{{ '#00c853' if bal.unrealised >= 0 else '#ff1744' }};">
            {{ '+' if bal.unrealised >= 0 else '' }}${{ "%.2f"|format(bal.unrealised) }}
        </strong>
        &nbsp;·&nbsp; Open manual trades: <strong>{{ open_trades|length }}</strong>
    </div>

    <!-- ── OVERBOUGHT ──────────────────────────────────── -->
    <h2>🔴 Overbought — 6H MFI &gt; 80 &nbsp;({{ overbought|length }} coins)</h2>
    {% if scan_running %}
    <div class="scanning">⏳ Scan in progress…</div>
    {% elif overbought %}
    <table>
        <thead><tr>
            <th>Coin</th>
            <th>Direction</th>
            <th>6H MFI</th>
            <th>1D MFI</th>
            <th>Scanned</th>
            <th>Chart</th>
            <th>Trade</th>
        </tr></thead>
        <tbody>
        {% for c in overbought %}
        <tr>
            <td class="coin">{{ c.coin }}</td>
            <td><span class="dir-short">SHORT</span></td>
            <td class="{{ 'mfi-ob' if c.mfi_6h >= 90 else 'mfi-ob2' }}">{{ c.mfi_6h }}</td>
            <td class="{{ 'mfi-ob' if c.mfi_1d and c.mfi_1d >= 80 else 'mfi-ob2' if c.mfi_1d and c.mfi_1d >= 60 else 'mfi-mid' }}">
                {{ c.mfi_1d if c.mfi_1d is not none else '—' }}
            </td>
            <td style="color:#555;font-size:11px;">{{ c.scanned_at }}</td>
            <td><a href="{{ c.tv_link }}" target="_blank" class="tv-link">📈 Chart</a></td>
            <td>
                <button class="btn-trade"
                    onclick="openModal('{{ c.coin }}','{{ c.ccxt_sym }}','SHORT')">
                    Trade ↗
                </button>
            </td>
        </tr>
        {% endfor %}
        </tbody>
    </table>
    {% else %}
    <div class="empty">No coins with 6H MFI &gt; 80 right now</div>
    {% endif %}

    <!-- ── OVERSOLD ───────────────────────────────────── -->
    <h2>🟢 Oversold — 6H MFI &lt; 20 &nbsp;({{ oversold|length }} coins)</h2>
    {% if scan_running %}
    <div class="scanning">⏳ Scan in progress…</div>
    {% elif oversold %}
    <table>
        <thead><tr>
            <th>Coin</th>
            <th>Direction</th>
            <th>6H MFI</th>
            <th>1D MFI</th>
            <th>Scanned</th>
            <th>Chart</th>
            <th>Trade</th>
        </tr></thead>
        <tbody>
        {% for c in oversold %}
        <tr>
            <td class="coin">{{ c.coin }}</td>
            <td><span class="dir-long">LONG</span></td>
            <td class="{{ 'mfi-os' if c.mfi_6h <= 10 else 'mfi-os2' }}">{{ c.mfi_6h }}</td>
            <td class="{{ 'mfi-os' if c.mfi_1d and c.mfi_1d <= 20 else 'mfi-os2' if c.mfi_1d and c.mfi_1d <= 40 else 'mfi-mid' }}">
                {{ c.mfi_1d if c.mfi_1d is not none else '—' }}
            </td>
            <td style="color:#555;font-size:11px;">{{ c.scanned_at }}</td>
            <td><a href="{{ c.tv_link }}" target="_blank" class="tv-link">📈 Chart</a></td>
            <td>
                <button class="btn-trade"
                    onclick="openModal('{{ c.coin }}','{{ c.ccxt_sym }}','LONG')">
                    Trade ↗
                </button>
            </td>
        </tr>
        {% endfor %}
        </tbody>
    </table>
    {% else %}
    <div class="empty">No coins with 6H MFI &lt; 20 right now</div>
    {% endif %}

    <!-- ── OPEN POSITIONS ─────────────────────────────── -->
    {% if open_trades %}
    <h2>🟣 Open Manual Positions ({{ open_trades|length }})</h2>
    <table>
        <thead><tr>
            <th>Coin</th><th>Dir</th><th>Entry</th>
            <th>Stop Loss</th><th>Take Profit</th>
            <th>Size</th><th>Trail</th>
            <th>PnL %</th><th>PnL $</th>
            <th>Opened</th><th>Notes</th><th>Close</th>
        </tr></thead>
        <tbody>
        {% for t in open_trades %}
        <tr>
            <td class="coin">{{ t.symbol }}</td>
            <td>
                {% if t.direction == 'SHORT' %}<span class="dir-short">SHORT</span>
                {% else %}<span class="dir-long">LONG</span>{% endif %}
            </td>
            <td>{{ t.entry }}</td>
            <td style="color:#ff6f00;">{{ t.sl }}
                <span style="color:#444;font-size:10px;">({{ t.sl_pct }})</span>
            </td>
            <td style="color:#00c853;">{{ t.tp }}
                <span style="color:#444;font-size:10px;">({{ t.tp_pct }})</span>
            </td>
            <td style="color:#888;">{{ t.size_mult }}× (${{ t.position_usd|int }})</td>
            <td>
                {% if t.use_trail %}
                    <span style="color:#69f0ae;font-size:11px;">
                        ✓ S{{ t.trailing_stage }}
                        {% if t.trail_note %}<br><span style="color:#555;font-size:10px;">{{ t.trail_note }}</span>{% endif %}
                    </span>
                {% else %}
                    <span style="color:#333;font-size:11px;">—</span>
                {% endif %}
            </td>
            <td class="{{ 'pnl-pos' if t.get('unrealised_pnl', 0) >= 0 else 'pnl-neg' }}">
                {{ ('+' if t.get('unrealised_pnl', 0) >= 0 else '') + '%.3f'|format(t.get('unrealised_pnl', 0)) + '%' }}
            </td>
            <td class="{{ 'pnl-pos' if t.get('unrealised_usd', 0) >= 0 else 'pnl-neg' }}">
                {{ ('+' if t.get('unrealised_usd', 0) >= 0 else '') }}${{ "%.2f"|format(t.get('unrealised_usd', 0)) }}
            </td>
            <td style="font-size:11px;color:#555;">{{ t.opened_at }}</td>
            <td style="font-size:11px;color:#777;">{{ t.notes or '—' }}</td>
            <td>
                <form method="POST" action="/close/{{ t.id }}" style="display:inline;">
                    <button type="submit" class="btn-close">✕ Close</button>
                </form>
            </td>
        </tr>
        {% endfor %}
        </tbody>
    </table>
    {% endif %}

    <!-- ── CLOSED TRADES ──────────────────────────────── -->
    {% if closed_trades %}
    <h2>📋 Closed Manual Trades ({{ closed_trades|length }})</h2>
    <table>
        <thead><tr>
            <th>Coin</th><th>Dir</th><th>Result</th>
            <th>Entry</th><th>Exit</th><th>PnL %</th><th>PnL $</th>
            <th>Opened</th><th>Closed</th><th>Notes</th>
        </tr></thead>
        <tbody>
        {% for t in closed_trades|reverse %}
        <tr>
            <td class="coin">{{ t.symbol }}</td>
            <td>
                {% if t.direction == 'SHORT' %}<span class="dir-short">SHORT</span>
                {% else %}<span class="dir-long">LONG</span>{% endif %}
            </td>
            <td>
                {% if t.result == 'WIN' %}<span class="badge badge-win">WIN ✅</span>
                {% elif t.result == 'BE+' %}<span class="badge badge-beplus">BE+ 🟡</span>
                {% elif t.result == 'LOSS' %}<span class="badge badge-loss">LOSS ❌</span>
                {% else %}<span class="badge" style="background:#222;color:#555;">{{ t.result }}</span>
                {% endif %}
            </td>
            <td>{{ t.entry }}</td>
            <td>{{ t.exit_price or '—' }}</td>
            <td class="{{ 'pnl-pos' if t.pnl_pct and t.pnl_pct >= 0 else 'pnl-neg' }}">
                {{ ('+' if t.pnl_pct and t.pnl_pct >= 0 else '') }}{{ '%.3f'|format(t.pnl_pct) if t.pnl_pct is not none else '—' }}%
            </td>
            <td class="{{ 'pnl-pos' if t.pnl_usd and t.pnl_usd >= 0 else 'pnl-neg' }}">
                {{ ('+' if t.pnl_usd and t.pnl_usd >= 0 else '') }}${{ '%.2f'|format(t.pnl_usd) if t.pnl_usd is not none else '—' }}
            </td>
            <td style="font-size:11px;color:#555;">{{ t.opened_at }}</td>
            <td style="font-size:11px;color:#555;">{{ t.closed_at or '—' }}</td>
            <td style="font-size:11px;color:#777;">{{ t.notes or '—' }}</td>
        </tr>
        {% endfor %}
        </tbody>
    </table>
    {% endif %}

    <!-- ── PAPER TRADING ─────────────────────────────── -->
    <hr style="border:none;border-top:1px solid #1a1a1a;margin:32px 0;">
    <h1 style="font-size:20px;color:#fff;margin-bottom:4px;">📝 Paper Trading — Auto Signals</h1>
    <p class="sub">3 greens → 1 red on 6H · ATR-based SL/TP · 15-min entry confirmation · No real orders</p>

    {% if paper_signals %}
    <h2>⏳ Armed Signals — waiting 15-min entry confirm ({{ paper_signals|length }})</h2>
    <table>
        <thead><tr>
            <th>Coin</th><th>MFI @ Signal</th><th>C2 Opened</th>
            <th>Entry Opens At</th><th>Expires At</th><th>ATR</th><th>Status</th>
        </tr></thead>
        <tbody>
        {% for coin, s in paper_signals.items() %}
        <tr>
            <td class="coin">{{ coin }}</td>
            <td class="{{ 'mfi-ob' if s.mfi_at_signal >= 80 else 'mfi-ob2' }}">{{ s.mfi_at_signal }}</td>
            <td style="font-size:11px;color:#555;">{{ s.armed_at }}</td>
            <td style="color:#ffd600;font-size:12px;">+15 min</td>
            <td style="color:#555;font-size:11px;">+3H max</td>
            <td style="color:#888;font-size:11px;">{{ s.atr }}</td>
            <td><span style="color:#ffd600;font-size:11px;">⏳ Confirming…</span></td>
        </tr>
        {% endfor %}
        </tbody>
    </table>
    {% endif %}

    {% set open_paper = paper_trades | selectattr("status","eq","OPEN") | list %}
    {% set closed_paper = paper_trades | rejectattr("status","eq","OPEN") | list %}

    {% if open_paper %}
    {% set open_total_usd = namespace(v=0) %}
    {% for t in open_paper %}{% set open_total_usd.v = open_total_usd.v + (t.get('unrealised_usd') or 0) %}{% endfor %}
    <h2>🟣 Open Paper Positions ({{ open_paper|length }}) &nbsp;
        <span style="font-size:12px;font-weight:400;color:{{ '#00c853' if open_total_usd.v >= 0 else '#ff1744' }};">
            Total unrealised: {{ '+' if open_total_usd.v >= 0 else '' }}${{ '%.2f'|format(open_total_usd.v) }}
        </span>
    </h2>
    <table>
        <thead><tr>
            <th>Coin</th><th>Entry → Current</th>
            <th>SL (max risk)</th><th>TP_RR (2:1 target)</th>
            <th>MFI sig</th>
            <th>PnL % / $</th>
            <th>Progress → TP</th>
            <th>Cushion to SL</th>
            <th>Opened</th>
            <th>Chart</th>
            <th>Actions</th>
        </tr></thead>
        <tbody>
        {% for t in open_paper %}
        {% set prog  = t.get('tp_progress', 0) %}
        {% set tosl  = t.get('to_sl_pct', 0) %}
        {% set totp  = t.get('to_tp_pct', 0) %}
        {% set pnl   = t.get('unrealised_pnl', 0) %}
        {% set pnlusd= t.get('unrealised_usd', 0) %}
        <tr>
            <td class="coin">{{ t.symbol }}</td>
            <td>
                <span style="color:#888;font-size:12px;">{{ t.entry }}</span>
                <span style="color:#444;font-size:11px;"> → </span>
                <strong style="color:{{ '#00c853' if pnl >= 0 else '#ff6f00' }};">{{ t.get('current_price', '—') }}</strong>
            </td>
            <td>
                <span style="color:#ff6f00;">{{ t.sl }}</span>
                <br><span style="color:#555;font-size:10px;">{{ t.sl_pct }} from entry</span>
            </td>
            <td>
                <span style="color:#00c853;">{{ t.tp_rr }}</span>
                <br><span style="color:#555;font-size:10px;">{{ t.tp_rr_pct }} from entry</span>
            </td>
            <td style="font-size:11px;color:#888;">
                {{ t.mfi_at_signal }}<br>
                <span style="color:#555;font-size:10px;">entry {{ t.mfi_at_entry }}</span>
            </td>
            <td>
                <span class="{{ 'pnl-pos' if pnl >= 0 else 'pnl-neg' }}" style="font-size:15px;font-weight:700;">
                    {{ '+' if pnl >= 0 else '' }}{{ '%.2f'|format(pnl) }}%
                </span><br>
                <span class="{{ 'pnl-pos' if pnlusd >= 0 else 'pnl-neg' }}" style="font-size:12px;">
                    {{ '+' if pnlusd >= 0 else '' }}${{ '%.2f'|format(pnlusd) }}
                </span>
                {% if t.get('partial_closed') %}
                <br><span style="font-size:10px;color:#ffd600;">
                    🔒 Partial: +{{ '%.2f'|format(t.partial_close_pct or 0) }}% (+${{ '%.2f'|format(t.partial_close_usd or 0) }})
                </span>
                {% endif %}
                {% if t.get('position_mult', 1.0) != 1.0 %}
                <br><span style="font-size:10px;color:#b57bee;">{{ t.get('position_mult', 1.0) }}× size</span>
                {% endif %}
            </td>
            <td style="min-width:160px;">
                {% if prog >= 100 %}
                    <span style="color:#ffd600;font-weight:700;font-size:12px;">✓ TP REACHED</span><br>
                {% endif %}
                <div style="background:#1a1a1a;border-radius:4px;height:6px;width:140px;margin:4px 0;">
                    <div style="background:{{ '#ffd600' if prog >= 100 else '#00c853' }};border-radius:4px;height:6px;width:{{ [prog,100]|min }}%;"></div>
                </div>
                <span style="font-size:11px;color:#{{ 'ffd600' if prog >= 100 else '888' }};">{{ '%.1f'|format(prog) }}% of way</span>
                <span style="font-size:10px;color:#555;"> · {{ '%.1f'|format(totp) }}% left</span>
            </td>
            <td>
                {% if tosl <= 3.0 %}
                    <span style="color:#ff1744;font-weight:700;font-size:13px;">⚠ {{ '%.1f'|format(tosl) }}%</span>
                {% elif tosl <= 6.0 %}
                    <span style="color:#ff6f00;font-weight:600;">{{ '%.1f'|format(tosl) }}%</span>
                {% else %}
                    <span style="color:#555;">{{ '%.1f'|format(tosl) }}%</span>
                {% endif %}
                <br><span style="font-size:10px;color:#333;">room before SL hit</span>
                {% if t.get('trail_stage') %}
                <br><span style="font-size:10px;color:#69f0ae;">⟳ {{ t.trail_stage }}</span>
                {% endif %}
            </td>
            <td style="font-size:11px;color:#555;">{{ t.opened_at }}</td>
            <td>
                <a href="https://www.tradingview.com/chart/?symbol=BINANCE:{{ t.symbol }}USDT.P&interval=240"
                   target="_blank" class="tv-link" style="font-size:13px;">📈</a>
            </td>
            <td style="white-space:nowrap;">
                <form method="POST" action="/paper-close/{{ t.id }}" style="display:inline;">
                    <button type="submit" class="btn-close" title="Close at market price">✕ Close</button>
                </form>
                <button class="btn-trade" style="margin-left:4px;font-size:10px;padding:3px 8px;"
                    onclick="openPaperEdit('{{ t.id }}','{{ t.sl }}','{{ t.tp_rr }}')"
                    title="Edit SL / TP">✎ Edit</button>
                <button class="btn-add" style="margin-left:4px;font-size:10px;padding:3px 8px;"
                    onclick="openPaperAdd('{{ t.id }}', {{ t.get('position_mult', 1.0) }})"
                    title="Add position size">➕ Add</button>
            </td>
        </tr>
        {% endfor %}
        </tbody>
    </table>
    {% endif %}

    {% if closed_paper %}
    {% set p_wins   = closed_paper | selectattr("status","eq","WIN")  | list %}
    {% set p_losses = closed_paper | selectattr("status","eq","LOSS") | list %}
    {% set p_wr     = (p_wins|length / closed_paper|length * 100) | round(1) if closed_paper else 0 %}

    {# ── Compute PnL stats via namespace ── #}
    {% set ns = namespace(win_pnl=0, loss_pnl=0, total_pnl=0) %}
    {% for t in p_wins %}
        {% set ns.win_pnl = ns.win_pnl + (t.pnl_pct or 0) %}
    {% endfor %}
    {% for t in p_losses %}
        {% set ns.loss_pnl = ns.loss_pnl + (t.pnl_pct or 0) %}
    {% endfor %}
    {% for t in closed_paper %}
        {% set ns.total_pnl = ns.total_pnl + (t.pnl_pct or 0) / 100 * BASE_TRADE_SIZE * LEVERAGE %}
    {% endfor %}

    {% set avg_win  = (ns.win_pnl  / p_wins|length)   if p_wins   else 0 %}
    {% set avg_loss = (ns.loss_pnl / p_losses|length)  if p_losses else 0 %}
    {% set pf_num   = ns.win_pnl if ns.win_pnl > 0 else 0 %}
    {% set pf_den   = ns.loss_pnl * -1 if ns.loss_pnl < 0 else 0.001 %}

    <h2>📊 Closed Paper Trades ({{ closed_paper|length }} · WR {{ p_wr }}%)</h2>
    <div style="display:flex;gap:12px;margin-bottom:16px;flex-wrap:wrap;">
        <div class="stat-box {{ 'green' if p_wr >= 50 else 'red' }}" style="min-width:90px;">
            <div class="label">Win Rate</div>
            <div class="value">{{ p_wr }}%</div>
        </div>
        <div class="stat-box green">
            <div class="label">Wins</div>
            <div class="value">{{ p_wins|length }}</div>
        </div>
        <div class="stat-box red">
            <div class="label">Losses</div>
            <div class="value">{{ p_losses|length }}</div>
        </div>
        <div class="stat-box {{ 'green' if avg_win > 0 else 'red' }}">
            <div class="label">Avg Win</div>
            <div class="value">+{{ '%.2f'|format(avg_win) }}%</div>
        </div>
        <div class="stat-box red">
            <div class="label">Avg Loss</div>
            <div class="value">{{ '%.2f'|format(avg_loss) }}%</div>
        </div>
        <div class="stat-box {{ 'green' if pf_num / pf_den >= 1.0 else 'red' }}">
            <div class="label">Profit Factor</div>
            <div class="value">{{ '%.2f'|format(pf_num / pf_den) }}</div>
        </div>
        <div class="stat-box {{ 'green' if ns.total_pnl >= 0 else 'red' }}">
            <div class="label">Total PnL $</div>
            <div class="value">{{ '+' if ns.total_pnl >= 0 else '' }}${{ '%.0f'|format(ns.total_pnl) }}</div>
        </div>
        <div class="stat-box blue">
            <div class="label">RR Ratio</div>
            <div class="value">{{ '%.1f'|format(avg_win / (avg_loss * -1)) if avg_loss < 0 else '—' }}:1</div>
        </div>
    </div>
    <table>
        <thead><tr>
            <th>Coin</th><th>Chart</th><th>Exit</th><th>Entry</th><th>Exit Price</th>
            <th>PnL %</th><th>PnL $</th><th>RR achieved</th>
            <th>MFI sig→entry</th><th>Opened</th><th>Closed</th>
        </tr></thead>
        <tbody>
        {% for t in closed_paper | reverse %}
        {% set cpnl = t.pnl_pct or 0 %}
        {% set cpnl_usd = cpnl / 100 * BASE_TRADE_SIZE * LEVERAGE %}
        <tr>
            <td class="coin">{{ t.symbol }}</td>
            <td>
                <a href="https://www.tradingview.com/chart/?symbol=BINANCE:{{ t.symbol }}USDT.P&interval=240"
                   target="_blank" class="tv-link" style="font-size:13px;">📈</a>
            </td>
            <td>
                {% if t.exit_type == "SL" %}
                    <span class="badge badge-loss">SL ❌</span>
                {% elif t.exit_type in ("TP_BOTH","TP_RR","TP_ATR") %}
                    <span class="badge badge-win">TP ✅</span>
                {% elif t.exit_type == "MANUAL" %}
                    <span class="badge badge-beplus">MANUAL</span>
                {% else %}
                    <span class="badge" style="background:#222;color:#888;">{{ t.exit_type or '—' }}</span>
                {% endif %}
            </td>
            <td style="font-size:12px;color:#888;">{{ t.entry }}</td>
            <td style="font-size:12px;color:#aaa;">{{ t.exit_price or "—" }}</td>
            <td class="{{ 'pnl-pos' if cpnl >= 0 else 'pnl-neg' }}" style="font-weight:700;">
                {{ '+' if cpnl >= 0 else '' }}{{ '%.2f'|format(cpnl) }}%
            </td>
            <td class="{{ 'pnl-pos' if cpnl_usd >= 0 else 'pnl-neg' }}" style="font-weight:700;">
                {{ '+' if cpnl_usd >= 0 else '' }}${{ '%.2f'|format(cpnl_usd) }}
            </td>
            <td style="font-size:11px;">
                {% set sl_dist = (t.sl_original - t.entry) / t.entry * 100 if t.get('sl_original') else none %}
                {% if sl_dist and sl_dist > 0 and cpnl != 0 %}
                    <span style="color:{{ '#00c853' if cpnl > 0 else '#ff1744' }};">
                        {{ '%.1f'|format(cpnl / sl_dist) }}R
                    </span>
                {% else %}—{% endif %}
            </td>
            <td style="font-size:11px;color:#888;">{{ t.mfi_at_signal }}→{{ t.mfi_at_entry }}</td>
            <td style="font-size:11px;color:#555;">{{ t.opened_at }}</td>
            <td style="font-size:11px;color:#555;">{{ t.closed_at or "—" }}</td>
        </tr>
        {% endfor %}
        </tbody>
    </table>
    {% endif %}

    <!-- ── TRADE MODAL ────────────────────────────────── -->
    <div class="modal-overlay" id="tradeModal">
        <div class="modal">
            <h3 id="modalTitle">Open Manual Trade</h3>
            <p class="sub2" id="modalSub">Exchange SL/TP · USDⓈ-M Futures</p>
            <form method="POST" action="/trade">
                <input type="hidden" name="coin"     id="modalCoin">
                <input type="hidden" name="ccxt_sym" id="modalSym">

                <label>Direction</label>
                <div class="dir-toggle">
                    <input type="radio" name="direction" id="dirShort" value="SHORT">
                    <label for="dirShort" class="short-lbl">SHORT ↓</label>
                    <input type="radio" name="direction" id="dirLong"  value="LONG">
                    <label for="dirLong"  class="long-lbl">LONG ↑</label>
                </div>

                <div class="modal-row">
                    <div>
                        <label>Stop Loss Price</label>
                        <input type="number" name="sl" id="modalSL" step="any" required placeholder="0.00000">
                        <p class="hint">SHORT: above entry &nbsp;|&nbsp; LONG: below entry</p>
                    </div>
                    <div>
                        <label>Take Profit Price</label>
                        <input type="number" name="tp" id="modalTP" step="any" required placeholder="0.00000">
                        <p class="hint">SHORT: below entry &nbsp;|&nbsp; LONG: above entry</p>
                    </div>
                </div>

                <label>Size Multiplier</label>
                <select name="size_mult">
                    <option value="0.5">0.5× — $25 notional ($5 margin)</option>
                    <option value="1.0" selected>1.0× — $50 notional ($10 margin)</option>
                    <option value="1.5">1.5× — $75 notional ($15 margin)</option>
                    <option value="2.0">2.0× — $100 notional ($20 margin)</option>
                </select>

                <div class="trail-row">
                    <input type="checkbox" name="use_trail" id="useTrail" value="1">
                    <label for="useTrail">Use trailing stop (+1.5% → lock 0.5% · +3% → lock 1%)</label>
                </div>

                <label>Notes (optional)</label>
                <textarea name="notes" id="modalNotes" placeholder="e.g. HTF confluence, strong 1D MFI…"></textarea>

                <div class="modal-actions">
                    <button type="submit" class="btn-confirm">⚡ Execute Trade</button>
                    <button type="button" class="btn-cancel" onclick="closeModal()">Cancel</button>
                </div>
            </form>
        </div>
    </div>

    <script>
    function openModal(coin, sym, direction) {
        document.getElementById("modalCoin").value  = coin;
        document.getElementById("modalSym").value   = sym;
        document.getElementById("modalTitle").textContent = "Trade — " + coin + " / USDT";
        document.getElementById("modalNotes").value = "";
        document.getElementById("modalSL").value    = "";
        document.getElementById("modalTP").value    = "";

        if (direction === "SHORT") {
            document.getElementById("dirShort").checked = true;
        } else {
            document.getElementById("dirLong").checked = true;
        }
        document.getElementById("tradeModal").classList.add("active");
    }
    function closeModal() {
        document.getElementById("tradeModal").classList.remove("active");
    }
    document.getElementById("tradeModal").addEventListener("click", function(e) {
        if (e.target === this) closeModal();
    });

    // ── Paper Edit Modal ─────────────────────────────────────
    function openPaperEdit(id, sl, tp) {
        document.getElementById("peTradeId").value = id;
        document.getElementById("peForm").action  = "/paper-edit/" + id;
        document.getElementById("peSL").value     = sl;
        document.getElementById("peTP").value     = tp;
        document.getElementById("paperEditModal").classList.add("active");
    }
    function closePaperEdit() {
        document.getElementById("paperEditModal").classList.remove("active");
    }
    document.getElementById("paperEditModal").addEventListener("click", function(e) {
        if (e.target === this) closePaperEdit();
    });

    // ── Paper Add Size Modal ──────────────────────────────────
    function openPaperAdd(id, currentMult) {
        document.getElementById("paForm").action        = "/paper-add/" + id;
        document.getElementById("paCurrentMult").textContent = currentMult;
        document.getElementById("paperAddModal").classList.add("active");
    }
    function closePaperAdd() {
        document.getElementById("paperAddModal").classList.remove("active");
    }
    document.getElementById("paperAddModal").addEventListener("click", function(e) {
        if (e.target === this) closePaperAdd();
    });
    </script>

    <!-- Paper Add Size Modal -->
    <div class="modal-overlay" id="paperAddModal">
        <div class="modal" style="width:320px;">
            <h3 style="color:#00c853;">➕ Add Position Size</h3>
            <p class="sub2">Current: <span id="paCurrentMult">1.0</span>× · Add more to ride harder</p>
            <form method="POST" id="paForm">
                <label>Add multiplier</label>
                <div style="display:flex;gap:8px;margin-top:8px;">
                    <button type="submit" name="add_mult" value="0.5"
                        style="flex:1;background:#1a3a1a;border:1px solid #00c853;color:#00c853;
                               padding:10px;border-radius:8px;font-size:14px;font-weight:700;cursor:pointer;">
                        +0.5×
                    </button>
                    <button type="submit" name="add_mult" value="1.0"
                        style="flex:1;background:#7c3aed;border:none;color:#fff;
                               padding:10px;border-radius:8px;font-size:14px;font-weight:700;cursor:pointer;">
                        +1.0×
                    </button>
                    <button type="submit" name="add_mult" value="2.0"
                        style="flex:1;background:#1a0a2e;border:1px solid #b57bee;color:#b57bee;
                               padding:10px;border-radius:8px;font-size:14px;font-weight:700;cursor:pointer;">
                        +2.0×
                    </button>
                </div>
                <p class="hint" style="margin-top:10px;">Each +1× adds ${{ BASE_TRADE_SIZE * LEVERAGE }} notional to the position</p>
                <div class="modal-actions">
                    <button type="button" class="btn-cancel" onclick="closePaperAdd()">Cancel</button>
                </div>
            </form>
        </div>
    </div>

    <!-- Paper Edit Modal -->
    <div class="modal-overlay" id="paperEditModal">
        <div class="modal" style="width:340px;">
            <h3 style="color:#b57bee;">✎ Edit Paper Trade</h3>
            <p class="sub2">Adjust Stop Loss or Take Profit price</p>
            <form method="POST" id="peForm">
                <input type="hidden" id="peTradeId" name="trade_id">
                <label>New Stop Loss Price</label>
                <input type="number" id="peSL" name="new_sl" step="any" placeholder="Leave blank to keep current">
                <p class="hint">SHORT: SL must be above entry price</p>
                <label style="margin-top:14px;">New TP_RR Price</label>
                <input type="number" id="peTP" name="new_tp" step="any" placeholder="Leave blank to keep current">
                <p class="hint">SHORT: TP must be below entry price</p>
                <div class="modal-actions">
                    <button type="submit" class="btn-confirm">Save Changes</button>
                    <button type="button" class="btn-cancel" onclick="closePaperEdit()">Cancel</button>
                </div>
            </form>
        </div>
    </div>
</body>
</html>
"""

# ─────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────
@app.route("/")
def index():
    bal          = get_balance()
    closed_trades = [t for t in state["trades"] if t["status"] != "OPEN"]
    return render_template_string(HTML,
        overbought      = state["overbought"],
        oversold        = state["oversold"],
        open_trades     = state["open_trades"],
        closed_trades   = closed_trades,
        last_scan       = state["last_scan"],
        next_scan_at    = state["next_scan_at"] or "—",
        scan_running    = state["scan_running"],
        watchlist_count = len(state["watchlist"]),
        bal             = bal,
        paper_signals   = state["paper_signals"],
        paper_trades    = state["paper_trades"],
        BASE_TRADE_SIZE = BASE_TRADE_SIZE,
        LEVERAGE        = LEVERAGE,
    )

@app.route("/scan", methods=["POST"])
def manual_scan():
    """Trigger an immediate scan."""
    if not state["scan_running"]:
        threading.Thread(target=run_scan, daemon=True).start()
    return redirect("/")

@app.route("/trade", methods=["POST"])
def trade():
    """Execute manual trade from dashboard form."""
    coin      = request.form.get("coin", "").upper().strip()
    ccxt_sym  = request.form.get("ccxt_sym") or (coin + "/USDT:USDT")
    direction = request.form.get("direction", "SHORT").upper()
    sl        = request.form.get("sl")
    tp        = request.form.get("tp")
    size_mult = float(request.form.get("size_mult", 1.0))
    use_trail = request.form.get("use_trail") == "1"
    notes     = request.form.get("notes", "")

    if not coin or not sl or not tp:
        return "coin, sl, tp required", 400

    result = open_manual_trade(coin, ccxt_sym, direction, sl, tp, size_mult, use_trail, notes)
    if not result["ok"]:
        return f"Trade failed: {result['msg']}", 500
    return redirect("/")

@app.route("/api/trade", methods=["POST"])
def api_trade():
    """
    JSON endpoint for manual trade via API / Claude.
    Body: { coin, direction, sl, tp, size_mult, use_trail, notes }
    """
    data      = request.get_json(silent=True) or {}
    coin      = data.get("coin", "").upper().strip()
    direction = data.get("direction", "SHORT").upper()
    sl        = data.get("sl")
    tp        = data.get("tp")
    size_mult = float(data.get("size_mult", 1.0))
    use_trail = bool(data.get("use_trail", False))
    notes     = data.get("notes", "")
    ccxt_sym  = coin + "/USDT:USDT"

    if not coin or sl is None or tp is None:
        return jsonify({"ok": False, "msg": "coin, sl, tp required"}), 400

    result = open_manual_trade(coin, ccxt_sym, direction, sl, tp, size_mult, use_trail, notes)
    return jsonify(result), (200 if result["ok"] else 500)

@app.route("/close/<trade_id>", methods=["POST"])
def close_trade(trade_id):
    """Manually close an open position at market."""
    trade = next((t for t in state["open_trades"] if t["id"] == trade_id), None)
    if not trade:
        return redirect("/")
    try:
        cancel_order(trade["ccxt_symbol"], trade.get("sl_order_id"))
        cancel_order(trade["ccxt_symbol"], trade.get("tp_order_id"))

        close_side  = "buy" if trade["direction"] == "SHORT" else "sell"
        close_order = exchange.create_order(trade["ccxt_symbol"], "market", close_side,
                                            trade["qty"], params={"reduceOnly": True})
        exit_price  = float(close_order.get("average") or close_order.get("price")
                            or trade.get("current_price", trade["entry"]))

        if trade["direction"] == "SHORT":
            pnl_pct = round((trade["entry"] - exit_price) / trade["entry"] * 100, 3)
        else:
            pnl_pct = round((exit_price - trade["entry"]) / trade["entry"] * 100, 3)

        result  = "WIN" if pnl_pct > 0.1 else ("BE+" if pnl_pct > -0.1 else "LOSS")
        pnl_usd = round(pnl_pct / 100 * trade["position_usd"], 2)

        trade.update({
            "status": result, "result": result,
            "exit_price": round(exit_price, 6),
            "pnl_pct": pnl_pct, "pnl_usd": pnl_usd,
            "closed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "sl_order_id": None, "tp_order_id": None,
        })
        state["open_trades"].remove(trade)
        save_trades()
        log.info(f"MANUAL CLOSE: {trade['symbol']} {trade['direction']} | PnL:{pnl_pct}% (${pnl_usd})")
    except Exception as e:
        log.error(f"Manual close failed: {e}")
    return redirect("/")

@app.route("/paper-close/<trade_id>", methods=["POST"])
def paper_close_trade(trade_id):
    """Manually close a paper trade at current market price."""
    trade = next((t for t in state["paper_trades"] if t["id"] == trade_id and t["status"] == "OPEN"), None)
    if not trade:
        return redirect("/")
    symbol = trade["symbol"] + "/USDT:USDT"
    try:
        exit_price = exchange.fetch_ticker(symbol)["last"]
    except:
        exit_price = trade.get("current_price", trade["entry"])
    pnl_pct = round((trade["entry"] - exit_price) / trade["entry"] * 100, 3)
    pnl_usd = round(pnl_pct / 100 * BASE_TRADE_SIZE * LEVERAGE, 2)
    result  = "WIN" if pnl_pct > 0.1 else ("BE+" if pnl_pct > -0.1 else "LOSS")
    trade.update({
        "status": result, "result": result,
        "exit_price": round(exit_price, 8),
        "exit_type": "MANUAL",
        "pnl_pct": pnl_pct,
        "closed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    save_paper_trades()
    log.info(f"PAPER MANUAL CLOSE: {trade['symbol']} | PnL:{pnl_pct}% (${pnl_usd})")
    return redirect("/")


@app.route("/paper-edit/<trade_id>", methods=["POST"])
def paper_edit_trade(trade_id):
    """Edit SL and/or TP on an open paper trade."""
    trade = next((t for t in state["paper_trades"] if t["id"] == trade_id and t["status"] == "OPEN"), None)
    if not trade:
        return redirect("/")
    new_sl = request.form.get("new_sl", "").strip()
    new_tp = request.form.get("new_tp", "").strip()
    if new_sl:
        try:
            trade["sl"] = float(new_sl)
            sl_dist = abs(trade["sl"] - trade["entry"]) / trade["entry"] * 100
            trade["sl_pct"] = f"+{sl_dist:.2f}%"
        except ValueError:
            pass
    if new_tp:
        try:
            trade["tp_rr"] = float(new_tp)
            tp_dist = abs(trade["tp_rr"] - trade["entry"]) / trade["entry"] * 100
            trade["tp_rr_pct"] = f"-{tp_dist:.2f}%"
        except ValueError:
            pass
    save_paper_trades()
    log.info(f"PAPER EDIT: {trade['symbol']} SL={trade['sl']} TP={trade['tp_rr']}")
    return redirect("/")


@app.route("/paper-add/<trade_id>", methods=["POST"])
def paper_add_trade(trade_id):
    """Increase position size multiplier on an open paper trade."""
    trade = next((t for t in state["paper_trades"] if t["id"] == trade_id and t["status"] == "OPEN"), None)
    if not trade:
        return redirect("/")
    add_mult = float(request.form.get("add_mult", 1.0))
    old_mult = float(trade.get("position_mult", 1.0))
    new_mult = round(old_mult + add_mult, 2)
    trade["position_mult"] = new_mult
    save_paper_trades()
    log.info(f"PAPER ADD SIZE: {trade['symbol']} mult {old_mult}→{new_mult} (+{add_mult}×)")
    return redirect("/")


# ─────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────
if __name__ == "__main__":
    load_trades()
    load_paper_trades()

    scan_thread   = threading.Thread(target=scan_runner,          daemon=True)
    trail_thread  = threading.Thread(target=trail_runner,         daemon=True)
    signal_thread = threading.Thread(target=signal_monitor_runner, daemon=True)
    scan_thread.start()
    trail_thread.start()
    signal_thread.start()

    print("═" * 55)
    print("  MFI Monitor & Manual Trader")
    print(f"  Dashboard: http://localhost:8087")
    print(f"  Scan:      every {SCAN_INTERVAL_MIN} min (6H MFI extremes)")
    print(f"  Thresholds: OB > {MFI_OB}  |  OS < {MFI_OS}")
    print(f"  API trade: POST http://localhost:8087/api/trade")
    print("═" * 55 + "\n")

    app.run(host="0.0.0.0", port=8087, debug=False)
