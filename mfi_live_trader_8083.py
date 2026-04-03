"""
MFI Live Trader — Port 8083
────────────────────────────────────────────────────────────────
Real execution of the MFI Monitor (8087) strategy on Binance USDⓈ-M Futures.

Signal:  6H MFI > 80 → 3 consecutive green 6H candles → 1st red 6H candle
Entry:   15-min confirm after candle 2 opens, expires 3H
Risk:    ATR×1.5 SL (hard-capped 8%), 2:1 RR TP
Size:    $50 margin × 5× = $250 notional per trade

Features:
  - Real market entry + exchange STOP_MARKET + TAKE_PROFIT_MARKET orders
  - Auto 50% partial close at 1:1 RR (reduce-only order, SL → breakeven)
  - ATR trailing SL (cancel + replace STOP_MARKET on exchange)
  - Full-universe scan (211 Binance futures pairs, no whitelist)
  - MAX_CONCURRENT = 6

Dashboard: http://localhost:8083
Logs:      Trade Logs/live_monitor_trades.json
"""

import ccxt
import json
import os
import threading
import time
import logging
from datetime import datetime, timedelta
from flask import Flask, render_template_string, request, redirect
from config import LIVE_BINANCE_API_KEY, LIVE_BINANCE_API_SECRET

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────
PORT               = 8083
MFI_LENGTH         = 14
MFI_OB             = 80          # 6H MFI > 80 → SHORT signal
MIN_VOLUME_USDT    = 5_000_000
SYMBOL_REFRESH_H   = 6
SCAN_INTERVAL_MIN  = 30
BASE_TRADE_SIZE    = 50          # USDT margin → $250 notional at 5×
LEVERAGE           = 5
MAX_CONCURRENT     = 6           # max open live positions

ATR_PERIOD         = 14
ATR_SL_MULT        = 1.5         # SL = entry + ATR × 1.5
ATR_TP_MULT        = 4.0         # TP_ATR reference
TP_RR_RATIO        = 2.0         # TP_RR = entry − (SL dist × 2)
MIN_GREENS         = 3           # consecutive green 6H candles required
ENTRY_CONFIRM_MIN  = 15          # wait 15 min into candle 2
ENTRY_EXPIRE_MIN   = 180         # signal expires after 3H
MFI_MIN_ENTRY      = 75          # live MFI must still be ≥ 75 at entry (raised from 70 — 70-75 zone has 0% WR)
MFI_MAX_ENTRY      = 85          # skip if MFI ≥ 85 — live data: 85-97 zone has 10% WR (2026-04-01)
COOLDOWN_HOURS     = 24          # skip coin for 24H after any loss on that coin
SIGNAL_CHECK_MIN   = 10          # armed-signal check interval
MONITOR_SEC        = 60          # position monitor interval (1 min)

# ATR trailing SL (same milestones as paper)
TRAIL_BE_ATRS      = 0.5
TRAIL_LOOSE_ATRS   = 1.5
TRAIL_TIGHT_ATRS   = 3.0
TRAIL_LOOSE_MULT   = 0.9         # tightened from 1.2 — locks in ~6-8% on winning trades (2026-04-01)
TRAIL_TIGHT_MULT   = 0.8

# Partial close
PARTIAL_TP_ENABLED = True        # 50% close at 1:1 RR, SL → breakeven

TRADE_LOG = "/opt/trader/Trade Logs/live_monitor_trades.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─────────────────────────────────────────
# EXCHANGE
# ─────────────────────────────────────────
exchange = ccxt.binanceusdm({
    "apiKey":  LIVE_BINANCE_API_KEY,
    "secret":  LIVE_BINANCE_API_SECRET,
    "options": {"defaultType": "future"},
})

app   = Flask(__name__)
state = {
    "overbought":        [],
    "oversold":          [],
    "last_scan":         "Starting…",
    "next_scan_at":      None,
    "scan_count":        0,
    "scan_running":      False,
    "watchlist":         [],
    "watchlist_updated": None,
    "running":           True,
    "signals":           {},    # coin → armed signal waiting confirm
    "prev_overbought":   [],
    "trades":            [],    # all trades (open + closed)
    "open_trades":       [],    # currently open
    "paused":            False,
    "pause_reason":      "",
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

def calc_atr(df, period=14):
    import pandas as pd
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])

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

def cancel_order_safe(ccxt_sym, order_id):
    if not order_id:
        return
    try:
        exchange.fapiPrivateDeleteAlgoOrder({"algoId": order_id})
        return
    except Exception:
        pass
    try:
        exchange.cancel_order(order_id, ccxt_sym)
    except Exception as e:
        log.warning(f"Cancel order {order_id} failed: {e}")

def get_position_qty(ccxt_sym):
    """Return signed position (negative = short). None on error."""
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

def get_last_fill_price(ccxt_sym, direction):
    """Find most recent closing trade fill price."""
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
        info       = exchange.fetch_balance().get("info", {})
        available  = round(float(info.get("availableBalance", 0)), 2)
        unrealised = round(float(info.get("totalUnrealizedProfit", 0)), 2)
        total_usdt = sum(
            float(a.get("walletBalance", 0))
            for a in info.get("assets", [])
            if a["asset"] in ("USDT","USDC","FDUSD","BUSD") and float(a.get("walletBalance",0)) > 0
        )
        return {"usdt": round(total_usdt,2), "available": available,
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
        log.info(f"Loaded {len(state['trades'])} live trades ({len(state['open_trades'])} open)")

def coin_in_cooldown(coin):
    """Return True if this coin had a loss closed within the last COOLDOWN_HOURS hours."""
    cutoff = datetime.now() - timedelta(hours=COOLDOWN_HOURS)
    for t in state["trades"]:
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

# ─────────────────────────────────────────
# WATCHLIST
# ─────────────────────────────────────────
_last_watchlist_refresh = None

def maybe_refresh_watchlist():
    global _last_watchlist_refresh
    now = datetime.now()
    if _last_watchlist_refresh and (now - _last_watchlist_refresh).total_seconds() < SYMBOL_REFRESH_H * 3600:
        return
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
        state["watchlist_updated"] = now.strftime("%Y-%m-%d %H:%M")
        _last_watchlist_refresh    = now
        log.info(f"Watchlist: {len(filtered)} pairs")
    except Exception as e:
        log.warning(f"Watchlist refresh failed: {e}")

# ─────────────────────────────────────────
# LIVE TRADE EXECUTION
# ─────────────────────────────────────────
def open_live_trade(coin, ccxt_sym, entry, sl, tp_rr, tp_partial, atr,
                    mfi_at_signal, mfi_at_entry):
    """Open a real SHORT position with exchange SL + TP orders.

    Two-stage design so the retry bug cannot occur:
      Stage 1 — market entry.  If this fails → return None → signal stays → safe to retry.
      Stage 2 — SL/TP orders. If this fails → log warning, trade saved without order IDs.
                               The function still returns the trade (truthy) so the signal
                               IS removed and will never be re-entered.
    """
    notional = BASE_TRADE_SIZE * LEVERAGE

    # ── ATR guard BEFORE any order ────────────────────────────────────────────
    atr_val = float(atr)
    if atr_val != atr_val or atr_val <= 0:          # NaN or zero
        log.warning(f"ABORT {coin}: invalid ATR={atr_val}")
        return None

    # ── STAGE 1: Market entry ─────────────────────────────────────────────────
    try:
        set_leverage(ccxt_sym)
        ticker = exchange.fetch_ticker(ccxt_sym)
        price  = ticker["last"]
        qty    = calc_qty(ccxt_sym, notional, price)

        if qty <= 0:
            log.warning(f"SKIP {coin}: qty=0 (position too small)")
            return None

        log.info(f"LIVE ENTRY: {coin} SHORT @ ~{price} | ${notional} notional")
        entry_order  = exchange.create_order(ccxt_sym, "market", "sell", qty)
        actual_entry = float(entry_order.get("average") or entry_order.get("price") or price)
        if actual_entry == 0:
            actual_entry = price

    except Exception as e:
        # Market order failed — no position opened, safe to leave signal for potential retry
        log.error(f"LIVE ENTRY FAILED {coin}: {e}")
        return None

    # ── Market order filled — compute SL/TP from actual fill ──────────────────
    sl_dist      = atr_val * ATR_SL_MULT
    max_sl_dist  = actual_entry * 0.08
    sl_final     = round(actual_entry + min(sl_dist, max_sl_dist), 8)
    tp_final     = round(actual_entry - (sl_final - actual_entry) * TP_RR_RATIO, 8)
    tp_partial_f = round((actual_entry + tp_final) / 2, 8)
    sl_pct       = round((sl_final     - actual_entry) / actual_entry * 100, 2)
    tp_pct       = round((actual_entry - tp_final)     / actual_entry * 100, 2)

    trade = {
        "id":                  f"LV_{coin}_{datetime.now().strftime('%m%d_%H%M')}",
        "symbol":              coin,
        "ccxt_sym":            ccxt_sym,
        "direction":           "SHORT",
        "entry":               round(actual_entry, 8),
        "sl":                  sl_final,
        "sl_original":         sl_final,
        "tp_rr":               tp_final,
        "tp_partial":          tp_partial_f,
        "atr":                 atr_val,
        "qty":                 qty,
        "notional":            notional,
        "sl_pct":              f"+{sl_pct:.2f}%",
        "tp_rr_pct":           f"-{tp_pct:.2f}%",
        "sl_order_id":         None,   # filled below if Stage 2 succeeds
        "tp_order_id":         None,
        "mfi_at_signal":       mfi_at_signal,
        "mfi_at_entry":        mfi_at_entry,
        "status":              "OPEN",
        "result":              None,
        "exit_price":          None,
        "exit_type":           None,
        "pnl_pct":             None,
        "pnl_usd":             None,
        "opened_at":           datetime.now().strftime("%Y-%m-%d %H:%M"),
        "closed_at":           None,
        "partial_closed":      False,
        "partial_close_price": None,
        "partial_close_pct":   None,
        "partial_close_usd":   None,
        "position_mult":       1.0,
        "current_price":       None,
        "unrealised_pnl":      None,
        "unrealised_usd":      None,
        "tp_progress":         None,
        "to_sl_pct":           None,
        "to_tp_pct":           None,
        "trail_stage":         None,
    }

    # ── STAGE 2: Exchange SL + TP orders ──────────────────────────────────────
    # If this fails the position is still open and tracked — monitor will show it.
    # The signal is removed regardless (function returns truthy below).
    try:
        sl_order = exchange.create_order(ccxt_sym, "STOP_MARKET", "buy", qty, params={
            "stopPrice":     sl_final,
            "closePosition": True,
            "workingType":   "MARK_PRICE",
        })
        tp_order = exchange.create_order(ccxt_sym, "TAKE_PROFIT_MARKET", "buy", qty, params={
            "stopPrice":     tp_final,
            "closePosition": True,
            "workingType":   "MARK_PRICE",
        })
        trade["sl_order_id"] = sl_order.get("id")
        trade["tp_order_id"] = tp_order.get("id")
        log.info(f"✅ LIVE SHORT OPEN: {coin} @ {actual_entry} | "
                 f"SL={sl_final}(+{sl_pct}%) TP={tp_final}(-{tp_pct}%) | "
                 f"qty={qty} SL_ID={sl_order.get('id')} TP_ID={tp_order.get('id')}")
    except Exception as e:
        log.error(f"⚠ SL/TP PLACEMENT FAILED {coin}: {e} — "
                  f"position OPEN @ {actual_entry} but UNPROTECTED. Manual SL needed!")

    # Save and return — signal is always removed after a successful market fill
    state["trades"].append(trade)
    state["open_trades"].append(trade)
    save_trades()
    return trade

# ─────────────────────────────────────────
# SIGNAL DETECTION  (same logic as 8087)
# ─────────────────────────────────────────
def check_and_arm_signals():
    """After each scan, look for 3-green-1-red pattern on OB coins."""
    all_ob_coins = {r["coin"] for r in state["overbought"]} | \
                   {r["coin"] for r in state["prev_overbought"]}
    open_coins   = {t["symbol"] for t in state["trades"] if t["status"] == "OPEN"}

    for coin in all_ob_coins:
        if coin in state["signals"] or coin in open_coins:
            continue
        ccxt_sym = coin + "/USDT:USDT"
        df6h = fetch_candles(ccxt_sym, "6h", 10)
        if df6h is None or len(df6h) < 6:
            continue

        # Candles: c[-4] c[-3] c[-2] = 3 greens; c[-1] = current (must be red so far)
        c   = df6h.iloc
        def is_green(i): return float(c[i]["close"]) > float(c[i]["open"])
        def is_red(i):   return float(c[i]["close"]) < float(c[i]["open"])

        if not (is_green(-4) and is_green(-3) and is_green(-2) and is_red(-1)):
            continue

        # Compute ATR on the 6H data
        import pandas as pd
        high  = df6h["high"]
        low   = df6h["low"]
        close = df6h["close"]
        tr    = pd.concat([high - low,
                           (high - close.shift()).abs(),
                           (low  - close.shift()).abs()], axis=1).max(axis=1)
        atr_series = tr.rolling(ATR_PERIOD).mean()
        atr_raw    = atr_series.dropna().iloc[-1] if atr_series.dropna().shape[0] > 0 else None
        if not atr_raw or atr_raw != atr_raw:   # None or NaN
            continue                             # skip coins with insufficient ATR history
        atr   = round(float(atr_raw), 8)

        # MFI at signal
        df6h["mfi"]   = calculate_mfi(df6h, MFI_LENGTH)
        mfi_at_signal = round(float(df6h["mfi"].iloc[-2]), 2)
        c2_open_price = float(df6h["open"].iloc[-1])

        # Carry recently_ob flag so entry filter can apply tighter MFI bar for decayed signals
        ob_entry    = next((r for r in state["overbought"] if r["coin"] == coin), None) or \
                      next((r for r in state["prev_overbought"] if r["coin"] == coin), None)
        recently_ob = ob_entry.get("recently_ob", False) if ob_entry else False

        state["signals"][coin] = {
            "ccxt_sym":       ccxt_sym,
            "mfi_at_signal":  mfi_at_signal,
            "atr":            atr,
            "c2_open_price":  c2_open_price,
            "recently_ob":    recently_ob,
            "armed_at":       datetime.now().strftime("%Y-%m-%d %H:%M"),
            "armed_ts":       datetime.now(),
        }
        log.info(f"⚡ SIGNAL ARMED: {coin} SHORT | MFI={mfi_at_signal} ATR={atr:.6f} recently_ob={recently_ob}")


def check_armed_entries():
    """Check armed signals for entry confirmation."""
    if not state["signals"] or state["paused"]:
        return

    open_coins  = {t["symbol"] for t in state["trades"] if t["status"] == "OPEN"}
    to_remove   = []

    for coin, sig in list(state["signals"].items()):
        if coin in open_coins:
            to_remove.append(coin)
            continue

        # Concurrent position cap
        if len(state["open_trades"]) >= MAX_CONCURRENT:
            break

        armed_ts    = sig["armed_ts"] if isinstance(sig["armed_ts"], datetime) \
                      else datetime.fromisoformat(str(sig["armed_ts"]))
        elapsed_min = (datetime.now() - armed_ts).total_seconds() / 60

        if elapsed_min < ENTRY_CONFIRM_MIN:
            continue
        if elapsed_min > ENTRY_EXPIRE_MIN:
            log.info(f"SIGNAL EXPIRED: {coin} ({elapsed_min:.0f} min)")
            to_remove.append(coin)
            continue

        ccxt_sym = sig["ccxt_sym"]
        try:
            ticker        = exchange.fetch_ticker(ccxt_sym)
            current_price = float(ticker["last"])
        except:
            continue

        # MFI at entry
        df6h = fetch_candles(ccxt_sym, "6h", 22)
        if df6h is None or len(df6h) < 16:
            continue
        df6h["mfi"] = calculate_mfi(df6h, MFI_LENGTH)
        mfi_live    = round(float(df6h["mfi"].iloc[-1]), 2)

        if mfi_live < MFI_MIN_ENTRY:
            log.info(f"ENTRY CANCELLED: {coin} — MFI {mfi_live:.1f} < {MFI_MIN_ENTRY}")
            to_remove.append(coin)
            continue

        if mfi_live >= MFI_MAX_ENTRY:
            log.info(f"ENTRY CANCELLED: {coin} — MFI {mfi_live:.1f} >= {MFI_MAX_ENTRY} (still OB, skip)")
            to_remove.append(coin)
            continue

        # Rec 2: decay guard — signal has faded too much, reversal already underway
        mfi_at_signal = sig["mfi_at_signal"]
        if isinstance(mfi_at_signal, (int, float)) and mfi_live < mfi_at_signal - 8:
            log.info(f"ENTRY CANCELLED: {coin} — MFI decayed {mfi_at_signal:.1f}→{mfi_live:.1f} (stale signal, >{8}pt drop)")
            to_remove.append(coin)
            continue

        # Rec 3: 1D MFI confluence — skip SHORT if daily is deeply oversold (coin in strong daily upswing)
        df1d_entry = fetch_candles(ccxt_sym, "1d", 20)
        if df1d_entry is not None and len(df1d_entry) >= 16:
            df1d_entry["mfi"] = calculate_mfi(df1d_entry, MFI_LENGTH)
            mfi_1d_live = round(float(df1d_entry["mfi"].iloc[-2]), 2)
            if mfi_1d_live < 45:
                log.info(f"ENTRY CANCELLED: {coin} — 1D MFI {mfi_1d_live:.1f} < 45 (daily upswing, SHORT risky)")
                to_remove.append(coin)
                continue

        # Rec 5: recently OB signals need tighter entry MFI — coin was marginally OB, apply higher bar
        if sig.get("recently_ob") and mfi_live < 77:
            log.info(f"ENTRY CANCELLED: {coin} — recently_ob signal, entry MFI {mfi_live:.1f} < 77 (too decayed)")
            to_remove.append(coin)
            continue

        if coin_in_cooldown(coin):
            log.info(f"ENTRY CANCELLED: {coin} — 24H cooldown after loss")
            to_remove.append(coin)
            continue

        # Compute SL/TP (will be recalculated at actual fill in open_live_trade)
        atr     = sig["atr"]
        entry   = current_price
        sl_raw  = entry + atr * ATR_SL_MULT
        sl_dist = min(sl_raw - entry, entry * 0.08)
        sl      = round(entry + sl_dist, 8)
        tp_rr   = round(entry - sl_dist * TP_RR_RATIO, 8)
        tp_part = round((entry + tp_rr) / 2, 8)

        result = open_live_trade(
            coin        = coin,
            ccxt_sym    = ccxt_sym,
            entry       = entry,
            sl          = sl,
            tp_rr       = tp_rr,
            tp_partial  = tp_part,
            atr         = atr,
            mfi_at_signal = sig["mfi_at_signal"],
            mfi_at_entry  = mfi_live,
        )

        if result:
            to_remove.append(coin)

    for coin in to_remove:
        state["signals"].pop(coin, None)


# ─────────────────────────────────────────
# POSITION MONITOR
# ─────────────────────────────────────────
def monitor_live_positions():
    """Monitor open positions every 1 min. Handle trail, partial close, exits."""
    trades_to_close = []
    changed = False

    for trade in state["open_trades"][:]:
        ccxt_sym  = trade["ccxt_sym"]
        coin      = trade["symbol"]

        try:
            ticker        = exchange.fetch_ticker(ccxt_sym)
            current_price = float(ticker["last"])
        except:
            continue

        # ── Check if position still exists (SL/TP may have hit on exchange)
        pos_qty = get_position_qty(ccxt_sym)
        if pos_qty is not None and abs(pos_qty) < 0.001:
            # Position is gone — determine exit
            exit_price = get_last_fill_price(ccxt_sym, "SHORT") or current_price
            pnl_pct    = round((trade["entry"] - exit_price) / trade["entry"] * 100, 3)
            pnl_usd    = round(pnl_pct / 100 * BASE_TRADE_SIZE * LEVERAGE * float(trade.get("position_mult",1.0)), 2)
            locked_usd = float(trade.get("partial_close_usd") or 0)
            total_usd  = round(pnl_usd + locked_usd, 2)

            if exit_price <= trade["tp_rr"] * 1.002:
                exit_type = "TP"
                result    = "WIN"
            elif exit_price >= trade["sl"] * 0.998:
                exit_type = "SL"
                result    = "WIN" if pnl_pct > 0.1 else ("BE+" if pnl_pct > -0.1 else "LOSS")
            else:
                exit_type = "CLOSED"
                result    = "WIN" if pnl_pct > 0.1 else ("BE+" if pnl_pct > -0.1 else "LOSS")

            trade.update({
                "status":     result,
                "result":     result,
                "exit_price": round(exit_price, 8),
                "exit_type":  exit_type,
                "pnl_pct":    pnl_pct,
                "pnl_usd":    total_usd,
                "closed_at":  datetime.now().strftime("%Y-%m-%d %H:%M"),
            })
            trades_to_close.append(trade)
            icon = "✅" if result == "WIN" else ("🟡" if result == "BE+" else "❌")
            log.info(f"{icon} LIVE CLOSED: {coin} via {exit_type} @ {exit_price} | "
                     f"PnL={pnl_pct}% (${total_usd}) [+locked ${locked_usd:.2f}]")
            changed = True
            continue

        # ── Update display fields
        pnl           = round((trade["entry"] - current_price) / trade["entry"] * 100, 3)
        position_mult = float(trade.get("position_mult", 1.0))
        trade["current_price"]  = current_price
        trade["unrealised_pnl"] = pnl
        trade["unrealised_usd"] = round(pnl / 100 * BASE_TRADE_SIZE * LEVERAGE * position_mult, 2)

        # ── ATR Trailing SL ───────────────────────────────────
        atr   = float(trade.get("atr", 0))
        entry = trade["entry"]
        if atr > 0 and pnl > 0:
            profit_atrs = (entry - current_price) / atr
            if profit_atrs >= TRAIL_TIGHT_ATRS:
                candidate_sl = round(current_price + atr * TRAIL_TIGHT_MULT, 8)
                stage        = f"Tight trail ({TRAIL_TIGHT_ATRS}ATR)"
            elif profit_atrs >= TRAIL_LOOSE_ATRS:
                candidate_sl = round(current_price + atr * TRAIL_LOOSE_MULT, 8)
                stage        = f"Loose trail ({TRAIL_LOOSE_ATRS}ATR)"
            elif profit_atrs >= TRAIL_BE_ATRS:
                candidate_sl = entry
                stage        = "Breakeven lock"
            else:
                candidate_sl = None
                stage        = None

            if candidate_sl is not None and candidate_sl < trade["sl"]:
                old_sl = trade["sl"]
                # Cancel existing SL order and place new one
                cancel_order_safe(ccxt_sym, trade.get("sl_order_id"))
                try:
                    new_sl_order = exchange.create_order(ccxt_sym, "STOP_MARKET", "buy",
                                                         trade["qty"], params={
                                                             "stopPrice":    candidate_sl,
                                                             "closePosition": True,
                                                             "workingType":  "MARK_PRICE",
                                                         })
                    trade["sl_order_id"] = new_sl_order.get("id")
                    trade["sl"]          = candidate_sl
                    trade["trail_stage"] = stage
                    changed = True
                    log.info(f"  TRAIL {coin}: SL {old_sl:.6g}→{candidate_sl:.6g} "
                             f"({stage}) | profit={profit_atrs:.2f}ATR | new_id={new_sl_order.get('id')}")
                except Exception as e:
                    log.warning(f"  TRAIL SL replace failed {coin}: {e}")
            elif stage:
                trade["trail_stage"] = stage

        # ── Auto 50% Partial Close at 1:1 RR ────────────────
        if PARTIAL_TP_ENABLED and not trade.get("partial_closed") and pnl > 0:
            tp_partial = float(trade.get("tp_partial",
                                        (trade["entry"] + trade["tp_rr"]) / 2))
            if current_price <= tp_partial:
                close_qty = trade["qty"] / 2
                min_qty   = 0.001  # rough minimum; real min checked by exchange
                if close_qty >= min_qty:
                    try:
                        close_order = exchange.create_order(
                            ccxt_sym, "market", "buy", close_qty,
                            params={"reduceOnly": True}
                        )
                        locked_price = float(close_order.get("average") or
                                             close_order.get("price") or tp_partial)
                        locked_pnl   = round((entry - locked_price) / entry * 100, 3)
                        locked_usd   = round(locked_pnl / 100 * BASE_TRADE_SIZE * LEVERAGE * 0.5, 2)

                        # Cancel old SL → place new BE stop for remaining 50%
                        cancel_order_safe(ccxt_sym, trade.get("sl_order_id"))
                        new_sl_order = exchange.create_order(ccxt_sym, "STOP_MARKET", "buy",
                                                             trade["qty"], params={
                                                                 "stopPrice":    entry,
                                                                 "closePosition": True,
                                                                 "workingType":  "MARK_PRICE",
                                                             })

                        trade["partial_closed"]      = True
                        trade["partial_close_price"] = round(locked_price, 8)
                        trade["partial_close_pct"]   = locked_pnl
                        trade["partial_close_usd"]   = locked_usd
                        trade["position_mult"]       = 0.5
                        trade["sl"]                  = entry
                        trade["sl_order_id"]         = new_sl_order.get("id")
                        trade["trail_stage"]         = "Breakeven lock (after partial close)"
                        position_mult                = 0.5
                        trade["unrealised_usd"]      = round(pnl / 100 * BASE_TRADE_SIZE * LEVERAGE * 0.5, 2)
                        changed = True
                        log.info(f"🔒 LIVE PARTIAL CLOSE {coin}: 50% @ {locked_price:.6g} "
                                 f"| locked +{locked_pnl}% (+${locked_usd}) | "
                                 f"new BE SL={entry} id={new_sl_order.get('id')}")
                    except Exception as e:
                        log.warning(f"Partial close failed {coin}: {e}")

        # ── Progress / distance metrics ─────────────────────
        sl = trade["sl"]; tp = trade["tp_rr"]
        tp_range     = abs(entry - tp)
        tp_progress  = round((entry - current_price) / tp_range * 100, 1) if tp_range else 0
        to_sl_pct    = round((sl - current_price) / current_price * 100, 2)
        to_tp_pct    = round((current_price - tp) / current_price * 100, 2)
        trade["tp_progress"] = max(0, tp_progress)
        trade["to_sl_pct"]   = to_sl_pct
        trade["to_tp_pct"]   = to_tp_pct

    # Remove closed trades from open list
    for trade in trades_to_close:
        if trade in state["open_trades"]:
            state["open_trades"].remove(trade)

    if changed:
        save_trades()


# ─────────────────────────────────────────
# SCAN
# ─────────────────────────────────────────
def run_scan():
    state["scan_running"] = True
    log.info(f"MFI scan #{state['scan_count']+1} — {len(state['watchlist'])} pairs…")
    maybe_refresh_watchlist()

    import pandas as pd
    overbought = []
    oversold   = []

    for symbol in state["watchlist"]:
        coin = symbol.split("/")[0]
        df6h = fetch_candles(symbol, "6h", 22)
        if df6h is None or len(df6h) < 16:
            time.sleep(0.15)
            continue
        df6h["mfi"] = calculate_mfi(df6h, MFI_LENGTH)
        mfi_6h      = round(float(df6h["mfi"].iloc[-2]), 2)

        # Lookback: was MFI > 80 in any of the last 3 completed candles?
        mfi_peak     = round(float(df6h["mfi"].iloc[-4:-1].max()), 2)
        recently_ob  = mfi_peak > MFI_OB and mfi_6h <= MFI_OB

        if mfi_6h > MFI_OB or mfi_6h < 20 or recently_ob:
            df1d   = fetch_candles(symbol, "1d", 20)
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

    state["prev_overbought"] = state["overbought"][:]
    state["overbought"]      = overbought
    state["oversold"]        = oversold
    state["last_scan"]       = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state["next_scan_at"]    = (datetime.now() + timedelta(minutes=SCAN_INTERVAL_MIN)).strftime("%H:%M")
    state["scan_count"]     += 1
    state["scan_running"]    = False
    log.info(f"Scan done: {len(overbought)} OB, {len(oversold)} OS")
    check_and_arm_signals()


# ─────────────────────────────────────────
# BACKGROUND THREADS
# ─────────────────────────────────────────
def scan_runner():
    run_scan()
    while state["running"]:
        time.sleep(SCAN_INTERVAL_MIN * 60)
        run_scan()

def signal_runner():
    time.sleep(90)
    while state["running"]:
        check_armed_entries()
        time.sleep(SIGNAL_CHECK_MIN * 60)

def monitor_runner():
    time.sleep(90)
    while state["running"]:
        if state["open_trades"]:
            monitor_live_positions()
        time.sleep(MONITOR_SEC)


# ─────────────────────────────────────────
# DASHBOARD HTML
# ─────────────────────────────────────────
HTML = r"""
<!DOCTYPE html>
<html>
<head>
    <title>MFI Live Trader — 8083</title>
    <meta http-equiv="refresh" content="60">
    <style>
        * { margin:0; padding:0; box-sizing:border-box; }
        body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
               background:#0d0d0d; color:#e0e0e0; padding:30px; }
        h1   { font-size:24px; color:#fff; margin-bottom:4px; }
        .sub { color:#666; font-size:13px; margin-bottom:20px; }
        .live-badge { display:inline-block; background:#1a0000; color:#ff1744;
                      border:1px solid #ff1744; border-radius:6px;
                      padding:3px 10px; font-size:11px; font-weight:700;
                      margin-left:10px; vertical-align:middle; }

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

        .paused-banner { background:#3a0000; border:1px solid #ff1744; border-radius:10px;
                         padding:14px 20px; margin-bottom:22px; color:#ff6f00; font-size:14px; }

        h2 { font-size:13px; color:#888; margin:22px 0 10px;
             text-transform:uppercase; letter-spacing:1.5px; }

        table { width:100%; border-collapse:collapse; background:#111; border-radius:10px;
                overflow:hidden; margin-bottom:28px; }
        th { background:#1a1a1a; padding:10px 14px; text-align:left; font-size:11px;
             color:#555; text-transform:uppercase; letter-spacing:1px; }
        td { padding:10px 14px; border-top:1px solid #1a1a1a; font-size:13px; }
        tr:hover td { background:#161616; }

        .coin { font-weight:700; color:#fff; font-size:14px; }
        .mfi-ob   { color:#ff1744; font-weight:700; }
        .mfi-ob2  { color:#ff6f00; font-weight:700; }
        .mfi-os   { color:#00c853; font-weight:700; }
        .mfi-os2  { color:#69f0ae; font-weight:600; }
        .mfi-mid  { color:#555; }
        .dir-short { display:inline-block; background:#1a0000; color:#ff1744;
                     border:1px solid #ff1744; border-radius:4px;
                     padding:2px 8px; font-size:11px; font-weight:700; }
        .tv-link  { color:#1e88e5; text-decoration:none; font-size:12px; }
        .tv-link:hover { text-decoration:underline; }

        .badge { display:inline-block; padding:3px 9px; border-radius:20px; font-size:11px; font-weight:600; }
        .badge-open   { background:#1e3a5f; color:#1e88e5; }
        .badge-win    { background:#1b3a2a; color:#00c853; }
        .badge-loss   { background:#3a1b1b; color:#ff1744; }
        .badge-beplus { background:#1a2a00; color:#aaff00; }

        .pnl-pos { color:#00c853; font-weight:600; }
        .pnl-neg { color:#ff1744; font-weight:600; }

        .btn-close { background:#1a0000; color:#ff5252; border:1px solid #ff1744;
                     padding:4px 10px; border-radius:6px; font-size:11px; font-weight:600; cursor:pointer; }
        .btn-close:hover { background:#ff1744; color:#fff; }
        .btn-edit  { background:#1a1a2e; color:#7c3aed; border:1px solid #7c3aed;
                     padding:4px 10px; border-radius:6px; font-size:11px; font-weight:600; cursor:pointer; }
        .btn-edit:hover  { background:#7c3aed; color:#fff; }
        .btn-add   { background:#001a00; color:#00c853; border:1px solid #00c853;
                     padding:4px 10px; border-radius:6px; font-size:11px; font-weight:600; cursor:pointer; }
        .btn-add:hover   { background:#00c853; color:#000; }

        /* Modals */
        .modal-overlay { display:none; position:fixed; inset:0; background:rgba(0,0,0,0.85);
                         z-index:1000; align-items:center; justify-content:center; }
        .modal-overlay.active { display:flex; }
        .modal { background:#1a1a1a; border:1px solid #333; border-radius:14px;
                 padding:28px; width:380px; max-width:95vw; }
        .modal h3 { color:#fff; font-size:16px; margin-bottom:16px; }
        .modal label { font-size:11px; color:#666; text-transform:uppercase;
                       letter-spacing:1px; display:block; margin-bottom:4px; margin-top:14px; }
        .modal input { width:100%; background:#111; border:1px solid #333; border-radius:8px;
                       padding:8px 12px; color:#fff; font-size:14px; }
        .modal input:focus { border-color:#7c3aed; outline:none; }
        .modal-actions { display:flex; gap:10px; margin-top:20px; }
        .modal-actions button { flex:1; padding:10px; border-radius:8px; font-size:14px;
                                font-weight:600; cursor:pointer; border:none; }
        .modal-cancel { background:#222; color:#888; }
        .modal-cancel:hover { background:#333; }
        .modal-save   { background:#7c3aed; color:#fff; }
        .modal-save:hover   { background:#9c5aff; }

        .stat-box { background:#111; border:1px solid #222; border-radius:8px;
                    padding:10px 16px; text-align:center; }
        .stat-box .label { font-size:11px; color:#555; text-transform:uppercase;
                           letter-spacing:1px; margin-bottom:4px; }
        .stat-box .value { font-size:20px; font-weight:700; color:#fff; }
        .stat-box.green .value { color:#00c853; }
        .stat-box.red   .value { color:#ff1744; }
        .stat-box.blue  .value { color:#1e88e5; }
        .stat-box.gold  .value { color:#ffd600; }

        .empty    { text-align:center; padding:30px; color:#333; font-size:13px; }
        .scanning { text-align:center; padding:20px; color:#555; font-size:13px; }
    </style>
</head>
<body>
    <h1>⚡ MFI Live Trader <span class="live-badge">LIVE</span></h1>
    <p class="sub">Binance USDⓈ-M · 6H MFI > 80 · 3 green → 1 red · $10 margin × 5× = $50/trade · Port 8083</p>

    {% if paused %}
    <div class="paused-banner">⏸ TRADING PAUSED — {{ pause_reason }}</div>
    {% endif %}

    <div class="info-bar">
        {% if scan_running %}
        <span><span class="scan-dot"></span><strong>Scanning…</strong></span>
        {% else %}
        <span>Last scan: <strong>{{ last_scan }}</strong></span>
        <span>Next scan: <strong>~{{ next_scan_at }}</strong></span>
        {% endif %}
        <span>OB (>80): <strong style="color:#ff6f00;">{{ overbought|length }}</strong></span>
        <span>OS (&lt;20): <strong style="color:#69f0ae;">{{ oversold|length }}</strong></span>
        <span>Watchlist: <strong>{{ watchlist_count }}</strong> pairs</span>
        <span>Open: <strong style="color:{% if open_trades|length >= MAX_CONCURRENT %}#ff1744{% else %}#fff{% endif %};">
            {{ open_trades|length }}/{{ MAX_CONCURRENT }}</strong></span>
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
    </div>

    <!-- ── OVERBOUGHT ────────────────────────────── -->
    <h2>🔴 Overbought — 6H MFI &gt; 80 (incl. 3-candle lookback) &nbsp;({{ overbought|length }})</h2>
    {% if scan_running %}
    <div class="scanning">⏳ Scan in progress…</div>
    {% elif overbought %}
    <table>
        <thead><tr>
            <th>Coin</th><th>6H MFI</th><th>Peak (3c)</th><th>1D MFI</th><th>Status</th><th>Scanned</th><th>Chart</th>
        </tr></thead>
        <tbody>
        {% for c in overbought %}
        <tr>
            <td class="coin">{{ c.coin }}</td>
            <td class="{{ 'mfi-ob' if c.mfi_6h >= 90 else ('mfi-ob2' if c.mfi_6h >= 80 else 'mfi-mid') }}">{{ c.mfi_6h }}</td>
            <td class="{{ 'mfi-ob' if c.mfi_peak >= 90 else 'mfi-ob2' }}">{{ c.mfi_peak }}</td>
            <td class="{{ 'mfi-ob' if c.mfi_1d and c.mfi_1d >= 80 else 'mfi-ob2' if c.mfi_1d and c.mfi_1d >= 60 else 'mfi-mid' }}">
                {{ c.mfi_1d if c.mfi_1d is not none else '—' }}
            </td>
            {% if c.recently_ob %}
            <td style="color:#ffa726;font-size:11px;font-weight:700;">↓ RECENT OB</td>
            {% else %}
            <td style="color:#ff1744;font-size:11px;font-weight:700;">LIVE OB</td>
            {% endif %}
            <td style="color:#555;font-size:11px;">{{ c.scanned_at }}</td>
            <td><a href="{{ c.tv_link }}" target="_blank" class="tv-link">📈 Chart</a></td>
        </tr>
        {% endfor %}
        </tbody>
    </table>
    {% else %}
    <div class="empty">No coins with 6H MFI &gt; 80</div>
    {% endif %}

    <!-- ── ARMED SIGNALS ────────────────────────── -->
    {% if signals %}
    <h2>⏳ Armed Signals — awaiting 15-min entry confirm ({{ signals|length }})</h2>
    <table>
        <thead><tr>
            <th>Coin</th><th>MFI @ Signal</th><th>Armed At</th>
            <th>Entry Opens At</th><th>Expires</th><th>ATR</th>
        </tr></thead>
        <tbody>
        {% for coin, s in signals.items() %}
        <tr>
            <td class="coin">{{ coin }}</td>
            <td class="{{ 'mfi-ob' if s.mfi_at_signal >= 80 else 'mfi-ob2' }}">{{ s.mfi_at_signal }}</td>
            <td style="font-size:11px;color:#555;">{{ s.armed_at }}</td>
            <td style="color:#ffd600;font-size:12px;">+15 min</td>
            <td style="color:#555;font-size:11px;">+3H max</td>
            <td style="color:#888;font-size:11px;">{{ s.atr }}</td>
        </tr>
        {% endfor %}
        </tbody>
    </table>
    {% endif %}

    <!-- ── OPEN LIVE POSITIONS ───────────────────── -->
    {% if open_trades %}
    {% set total_usd = namespace(v=0) %}
    {% for t in open_trades %}{% set total_usd.v = total_usd.v + (t.get('unrealised_usd') or 0) %}{% endfor %}
    <h2>🟣 Open Live Positions ({{ open_trades|length }})
        &nbsp;<span style="font-size:12px;font-weight:400;color:{{ '#00c853' if total_usd.v >= 0 else '#ff1744' }};">
            Unrealised: {{ '+' if total_usd.v >= 0 else '' }}${{ '%.2f'|format(total_usd.v) }}
        </span>
    </h2>
    <table>
        <thead><tr>
            <th>Coin</th>
            <th>Entry → Current</th>
            <th>SL</th>
            <th>TP (2:1)</th>
            <th>PnL % / $</th>
            <th>Progress → TP</th>
            <th>Cushion to SL</th>
            <th>Size</th>
            <th>Opened</th>
            <th>Actions</th>
        </tr></thead>
        <tbody>
        {% for t in open_trades %}
        {% set pnl    = t.get('unrealised_pnl', 0) %}
        {% set pnlusd = t.get('unrealised_usd', 0) %}
        {% set prog   = t.get('tp_progress', 0) %}
        {% set tosl   = t.get('to_sl_pct', 0) %}
        {% set totp   = t.get('to_tp_pct', 0) %}
        <tr>
            <td class="coin">{{ t.symbol }}</td>
            <td>
                <span style="color:#888;font-size:12px;">{{ t.entry }}</span>
                <span style="color:#444;font-size:11px;"> → </span>
                <strong style="color:{{ '#00c853' if pnl >= 0 else '#ff6f00' }};">
                    {{ t.get('current_price', '—') }}
                </strong>
            </td>
            <td>
                <span style="color:#ff6f00;">{{ t.sl }}</span>
                <br><span style="color:#555;font-size:10px;">{{ t.sl_pct }} from entry</span>
                {% if t.get('trail_stage') %}
                <br><span style="font-size:10px;color:#69f0ae;">⟳ {{ t.trail_stage }}</span>
                {% endif %}
            </td>
            <td>
                <span style="color:#00c853;">{{ t.tp_rr }}</span>
                <br><span style="color:#555;font-size:10px;">{{ t.tp_rr_pct }} from entry</span>
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
                    🔒 +{{ '%.2f'|format(t.partial_close_pct or 0) }}% (+${{ '%.2f'|format(t.partial_close_usd or 0) }})
                </span>
                {% endif %}
                {% if t.get('position_mult', 1.0) != 1.0 %}
                <br><span style="font-size:10px;color:#b57bee;">{{ t.get('position_mult') }}× size</span>
                {% endif %}
            </td>
            <td style="min-width:150px;">
                {% if prog >= 100 %}
                <span style="color:#ffd600;font-weight:700;font-size:12px;">✓ TP REACHED</span><br>
                {% endif %}
                <div style="background:#1a1a1a;border-radius:4px;height:6px;width:130px;margin:4px 0;">
                    <div style="background:{{ '#ffd600' if prog >= 100 else '#00c853' }};border-radius:4px;height:6px;width:{{ [prog,100]|min }}%;"></div>
                </div>
                <span style="font-size:11px;color:#{{ 'ffd600' if prog >= 100 else '888' }};">{{ '%.1f'|format(prog) }}%</span>
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
                <br><span style="font-size:10px;color:#333;">room before SL</span>
            </td>
            <td style="font-size:11px;color:#888;">
                ${{ BASE_TRADE_SIZE * LEVERAGE }}<br>
                <span style="color:#555;font-size:10px;">{{ t.get('qty', '?') }} contracts</span>
            </td>
            <td style="font-size:11px;color:#555;">{{ t.opened_at }}</td>
            <td style="white-space:nowrap;">
                <a href="https://www.tradingview.com/chart/?symbol=BINANCE:{{ t.symbol }}USDT.P&interval=240"
                   target="_blank" class="tv-link" style="margin-right:6px;font-size:15px;" title="TradingView chart">📈</a>
                <button class="btn-edit"
                        onclick="openLiveEdit('{{ t.id }}','{{ t.symbol }}','{{ t.sl }}','{{ t.tp_rr }}')"
                        title="Edit SL / TP">✎ Edit</button>
                <button class="btn-add" style="margin-left:4px;"
                        onclick="openLiveAdd('{{ t.id }}','{{ t.symbol }}',{{ t.get('position_mult', 1.0) }})"
                        title="Add to position">+ Add</button>
                <form method="POST" action="/close/{{ t.id }}" style="display:inline;margin-left:4px;"
                      onsubmit="return confirm('Close {{ t.symbol }} at market price?')">
                    <button type="submit" class="btn-close">✕ Close</button>
                </form>
            </td>
        </tr>
        {% endfor %}
        </tbody>
    </table>
    {% else %}
    <div class="empty" style="margin-bottom:28px;">No open live positions</div>
    {% endif %}

    <!-- ── CLOSED TRADES ─────────────────────────── -->
    {% set closed = trades | rejectattr("status","eq","OPEN") | list %}
    {% if closed %}
    {% set p_wins   = closed | selectattr("result","eq","WIN")  | list %}
    {% set p_losses = closed | selectattr("result","eq","LOSS") | list %}
    {% set p_wr     = (p_wins|length / closed|length * 100) | round(1) if closed else 0 %}

    {% set total_pnl = namespace(v=0) %}
    {% for t in closed %}{% set total_pnl.v = total_pnl.v + (t.pnl_usd or 0) %}{% endfor %}

    <h2>📊 Closed Live Trades ({{ closed|length }} · WR {{ p_wr }}%)</h2>
    <div style="display:flex;gap:14px;margin-bottom:14px;flex-wrap:wrap;">
        <div class="stat-box {{ 'green' if p_wr >= 50 else 'red' }}">
            <div class="label">Win Rate</div><div class="value">{{ p_wr }}%</div>
        </div>
        <div class="stat-box green">
            <div class="label">Wins</div><div class="value">{{ p_wins|length }}</div>
        </div>
        <div class="stat-box red">
            <div class="label">Losses</div><div class="value">{{ p_losses|length }}</div>
        </div>
        <div class="stat-box {{ 'green' if total_pnl.v >= 0 else 'red' }}">
            <div class="label">Total PnL $</div>
            <div class="value">{{ '+' if total_pnl.v >= 0 else '' }}${{ '%.2f'|format(total_pnl.v) }}</div>
        </div>
    </div>
    <table>
        <thead><tr>
            <th>Coin</th><th>Exit Type</th><th>Entry</th><th>Exit</th>
            <th>PnL %</th><th>PnL $</th><th>Locked Partial</th>
            <th>MFI sig→entry</th><th>ATR</th><th>Opened</th><th>Closed</th>
        </tr></thead>
        <tbody>
        {% for t in closed | reverse %}
        <tr>
            <td class="coin">{{ t.symbol }}</td>
            <td>
                {% if t.exit_type == "SL" %}
                    <span class="badge badge-loss">SL ❌</span>
                {% elif t.exit_type == "TP" %}
                    <span class="badge badge-win">TP ✅</span>
                {% elif t.exit_type == "MANUAL" %}
                    <span class="badge badge-beplus">MANUAL</span>
                {% else %}
                    <span class="badge" style="background:#222;color:#555;">{{ t.exit_type or '—' }}</span>
                {% endif %}
            </td>
            <td style="font-size:12px;">{{ t.entry }}</td>
            <td style="font-size:12px;">{{ t.exit_price or '—' }}</td>
            <td class="{{ 'pnl-pos' if t.pnl_pct and t.pnl_pct >= 0 else 'pnl-neg' }}">
                {{ ('+' if t.pnl_pct and t.pnl_pct >= 0 else '') }}{{ '%.3f'|format(t.pnl_pct) if t.pnl_pct is not none else '—' }}%
            </td>
            <td class="{{ 'pnl-pos' if t.pnl_usd and t.pnl_usd >= 0 else 'pnl-neg' }}">
                {{ ('+' if t.pnl_usd and t.pnl_usd >= 0 else '') }}${{ '%.2f'|format(t.pnl_usd) if t.pnl_usd is not none else '—' }}
            </td>
            <td style="font-size:11px;">
                {% if t.get('partial_closed') %}
                <span style="color:#ffd600;">🔒 +${{ '%.2f'|format(t.partial_close_usd or 0) }}</span>
                {% else %}—{% endif %}
            </td>
            <td style="font-size:11px;color:#888;">{{ t.mfi_at_signal }}→{{ t.mfi_at_entry }}</td>
            <td style="font-size:11px;color:#555;">{{ t.atr }}</td>
            <td style="font-size:11px;color:#555;">{{ t.opened_at }}</td>
            <td style="font-size:11px;color:#555;">{{ t.closed_at or '—' }}</td>
        </tr>
        {% endfor %}
        </tbody>
    </table>
    {% endif %}

<!-- ── EDIT MODAL ───────────────────────── -->
<div class="modal-overlay" id="editModal">
    <div class="modal">
        <h3>✎ Edit Live Trade</h3>
        <p id="editModalSub" style="color:#666;font-size:12px;margin-bottom:4px;"></p>
        <form id="editForm" method="POST">
            <label>New Stop Loss</label>
            <input type="number" name="new_sl" id="editSL" step="any" placeholder="leave blank to keep">
            <label>New Take Profit</label>
            <input type="number" name="new_tp" id="editTP" step="any" placeholder="leave blank to keep">
            <div class="modal-actions">
                <button type="button" class="modal-cancel" onclick="closeModals()">Cancel</button>
                <button type="submit" class="modal-save">Save</button>
            </div>
        </form>
    </div>
</div>

<!-- ── ADD SIZE MODAL ───────────────────── -->
<div class="modal-overlay" id="addModal">
    <div class="modal">
        <h3>+ Add to Position</h3>
        <p id="addModalSub" style="color:#666;font-size:12px;margin-bottom:16px;"></p>
        <form id="addForm" method="POST">
            <p style="font-size:12px;color:#888;margin-bottom:14px;">Current size multiplier: <strong id="addCurMult" style="color:#fff;"></strong>×</p>
            <div style="display:flex;gap:8px;flex-wrap:wrap;">
                <button type="submit" name="add_mult" value="0.5" class="btn-add" style="flex:1;padding:10px;">+0.5×</button>
                <button type="submit" name="add_mult" value="1.0" class="btn-add" style="flex:1;padding:10px;">+1×</button>
                <button type="submit" name="add_mult" value="2.0" class="btn-add" style="flex:1;padding:10px;">+2×</button>
            </div>
            <div class="modal-actions" style="margin-top:14px;">
                <button type="button" class="modal-cancel" onclick="closeModals()">Cancel</button>
            </div>
        </form>
    </div>
</div>

<script>
function openLiveEdit(id, symbol, sl, tp) {
    document.getElementById("editModalSub").textContent = symbol + " · current SL: " + sl + "  TP: " + tp;
    document.getElementById("editForm").action = "/live-edit/" + id;
    document.getElementById("editSL").value  = "";
    document.getElementById("editTP").value  = "";
    document.getElementById("editModal").classList.add("active");
}
function openLiveAdd(id, symbol, mult) {
    document.getElementById("addModalSub").textContent = symbol;
    document.getElementById("addCurMult").textContent  = mult;
    document.getElementById("addForm").action = "/live-add/" + id;
    document.getElementById("addModal").classList.add("active");
}
function closeModals() {
    document.getElementById("editModal").classList.remove("active");
    document.getElementById("addModal").classList.remove("active");
}
document.addEventListener("keydown", function(e) { if (e.key === "Escape") closeModals(); });
</script>

</body>
</html>
"""

# ─────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────
@app.route("/")
def index():
    bal           = get_balance()
    closed_trades = [t for t in state["trades"] if t["status"] != "OPEN"]
    return render_template_string(HTML,
        overbought      = state["overbought"],
        oversold        = state["oversold"],
        open_trades     = state["open_trades"],
        trades          = state["trades"],
        last_scan       = state["last_scan"],
        next_scan_at    = state["next_scan_at"] or "—",
        scan_running    = state["scan_running"],
        watchlist_count = len(state["watchlist"]),
        signals         = state["signals"],
        paused          = state["paused"],
        pause_reason    = state["pause_reason"],
        bal             = bal,
        BASE_TRADE_SIZE = BASE_TRADE_SIZE,
        LEVERAGE        = LEVERAGE,
        MAX_CONCURRENT  = MAX_CONCURRENT,
    )

@app.route("/scan", methods=["POST"])
def manual_scan():
    if not state["scan_running"]:
        threading.Thread(target=run_scan, daemon=True).start()
    return redirect("/")

@app.route("/close/<trade_id>", methods=["POST"])
def close_trade(trade_id):
    """Manually close a live position at market."""
    trade = next((t for t in state["open_trades"] if t["id"] == trade_id), None)
    if not trade:
        return redirect("/")
    ccxt_sym = trade["ccxt_sym"]
    try:
        cancel_order_safe(ccxt_sym, trade.get("sl_order_id"))
        cancel_order_safe(ccxt_sym, trade.get("tp_order_id"))

        close_order = exchange.create_order(ccxt_sym, "market", "buy",
                                            trade["qty"],
                                            params={"reduceOnly": True})
        exit_price = float(close_order.get("average") or close_order.get("price")
                           or trade.get("current_price", trade["entry"]))

        pnl_pct   = round((trade["entry"] - exit_price) / trade["entry"] * 100, 3)
        mult      = float(trade.get("position_mult", 1.0))
        pnl_usd   = round(pnl_pct / 100 * BASE_TRADE_SIZE * LEVERAGE * mult, 2)
        locked    = float(trade.get("partial_close_usd") or 0)
        total_usd = round(pnl_usd + locked, 2)
        result    = "WIN" if pnl_pct > 0.1 else ("BE+" if pnl_pct > -0.1 else "LOSS")

        trade.update({
            "status": result, "result": result,
            "exit_price": round(exit_price, 8),
            "exit_type":  "MANUAL",
            "pnl_pct":    pnl_pct,
            "pnl_usd":    total_usd,
            "closed_at":  datetime.now().strftime("%Y-%m-%d %H:%M"),
        })
        state["open_trades"].remove(trade)
        save_trades()
        log.info(f"MANUAL CLOSE: {trade['symbol']} @ {exit_price} | PnL={pnl_pct}% (${total_usd})")
    except Exception as e:
        log.error(f"Manual close failed {trade['symbol']}: {e}")
    return redirect("/")

@app.route("/live-edit/<trade_id>", methods=["POST"])
def live_edit_trade(trade_id):
    """Edit SL and/or TP on an open live trade (local tracking only — does NOT move exchange orders)."""
    trade = next((t for t in state["open_trades"] if t["id"] == trade_id), None)
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
    save_trades()
    log.info(f"LIVE EDIT: {trade['symbol']} SL={trade['sl']} TP={trade['tp_rr']}")
    return redirect("/")


@app.route("/live-add/<trade_id>", methods=["POST"])
def live_add_trade(trade_id):
    """Increase position size multiplier (for tracking PnL on manually added size)."""
    trade = next((t for t in state["open_trades"] if t["id"] == trade_id), None)
    if not trade:
        return redirect("/")
    add_mult = float(request.form.get("add_mult", 1.0))
    old_mult = float(trade.get("position_mult", 1.0))
    new_mult = round(old_mult + add_mult, 2)
    trade["position_mult"] = new_mult
    save_trades()
    log.info(f"LIVE ADD SIZE: {trade['symbol']} mult {old_mult}→{new_mult} (+{add_mult}×)")
    return redirect("/")


@app.route("/pause", methods=["POST"])
def toggle_pause():
    state["paused"]       = not state["paused"]
    state["pause_reason"] = "Manual pause" if state["paused"] else ""
    log.info(f"Trading {'PAUSED' if state['paused'] else 'RESUMED'}")
    return redirect("/")


# ─────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────
if __name__ == "__main__":
    load_trades()

    scan_t    = threading.Thread(target=scan_runner,    daemon=True)
    signal_t  = threading.Thread(target=signal_runner,  daemon=True)
    monitor_t = threading.Thread(target=monitor_runner,  daemon=True)
    scan_t.start()
    signal_t.start()
    monitor_t.start()

    print("═" * 58)
    print("  MFI Live Trader  [REAL MONEY — Binance USDⓈ-M]")
    print(f"  Dashboard: http://localhost:{PORT}")
    print(f"  Size: ${BASE_TRADE_SIZE} margin × {LEVERAGE}× = ${BASE_TRADE_SIZE*LEVERAGE} notional/trade")
    print(f"  Max concurrent: {MAX_CONCURRENT}")
    print("═" * 58)

    app.run(host="0.0.0.0", port=PORT, debug=False)
