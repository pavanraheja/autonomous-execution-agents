"""
Filtered MFI Coin Hunter — Paper + Live (port 8081)
────────────────────────────────────────────────────
Same strategy as MFI Coin Hunter (paper_trader.py v1.3) but restricted to
a backtest-validated coin whitelist that showed consistent positive expectancy
over 90-day AND 180-day periods.

Whitelist basis (180-day backtest):
  Core (appear in both 90d + 180d top lists): ZETA (80% WR), RDNT (60%), PAXG (60%)
  Strong (180d only, ≥3 trades): ARIA (100%), ZK (66.7%), BIO (66.7%),
                                  OPEN (66.7%), GRT (66.7%), XAU (66.7%)
  Combined 9 coins: 67% WR, +2.15% expectancy/trade, PF ~3.5

Full universe was: 34.2% WR, -231% PnL — coin selection is everything.

Dashboard: http://localhost:8081
Saves:     Trade Logs/filtered_paper_trades.json
"""

import ccxt
import json
import os
import threading
import time
import logging
from datetime import datetime, timedelta
from flask import Flask, render_template_string, request, redirect

import re, subprocess
from config import BINANCE_API_KEY, BINANCE_API_SECRET, LIVE_BINANCE_API_KEY, LIVE_BINANCE_API_SECRET

# ─────────────────────────────────────────
# COIN WHITELIST  (backtest-validated)
# ─────────────────────────────────────────
COIN_WHITELIST = [
    "ZETA/USDT:USDT",   # 180d: 80% WR, +15%  | 90d: 75% WR  ✅ Both periods
    "RDNT/USDT:USDT",   # 180d: 60% WR, +6.2% | 90d: 75% WR  ✅ Both periods
    "ARIA/USDT:USDT",   # 180d: 100% WR, +13.5% (3 trades)
    "ZK/USDT:USDT",     # 180d: 66.7% WR, +6%
    "BIO/USDT:USDT",    # 180d: 66.7% WR, +6%
    "OPEN/USDT:USDT",   # 180d: 66.7% WR, +6%
    "GRT/USDT:USDT",    # 180d: 66.7% WR, +4%
    # PAXG removed: low volatility (ATR 1.12%) — fixed 3% SL too wide, rarely reaches 6% TP
    # XAU  removed: low volatility (ATR 1.15%) — same issue, dead weight on the strategy
]

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────
PORT               = 8081
MFI_LENGTH         = 14
MFI_OVERBOUGHT     = 80
MAX_SL_PCT         = 0.03       # 3% stop loss
RR_RATIO           = 1.5        # TP = 4.5%
SCAN_INTERVAL_MIN  = 10         # scan every 10 min
TRAIL_INTERVAL_MIN = 15

# MFI-tiered position sizing (same as paper_trader)
ENABLE_MFI_TIERS = True
# Sizing based on backtest performance by MFI tier (180-day):
#   MFI 80-89 → 32% WR, -184% PnL  → 0.5× (small — weakest tier)
#   MFI 90-94 → 47% WR, +22% PnL   → 1.5× (largest — only profitable tier)
#   MFI 95+   → 29% WR, -69% PnL   → 0.5× (small — extreme OB overshoots SL)
MFI_TIER = [
    (90,  94.999,       1.5),   # sweet spot → biggest bet
    (80,  89.999,       0.5),   # below optimal → reduced
    (95,  float("inf"), 0.5),   # extreme OB undershoots → reduced
]
BASE_TRADE_SIZE = 2_000   # USDT margin per trade (1× tier)
LEVERAGE        = 5

# Cooldown after loss on same coin
ENABLE_COOLDOWN = True
COOLDOWN_HOURS  = 24

# Trailing stop
ENABLE_TRAIL    = True
TRAIL_TRIGGER_1 = 1.5
TRAIL_TRIGGER_2 = 3.0
TRAIL_LOCK_1    = 0.5
TRAIL_LOCK_2    = 1.0

TRADE_LOG      = "/opt/trader/Trade Logs/filtered_paper_trades.json"
LIVE_TRADE_LOG = "/opt/trader/Trade Logs/filtered_live_trades.json"

# ── Live trading config ───────────────────
LIVE_ENABLED        = True
LIVE_BASE_MARGIN    = 15      # USDT margin per trade — minimal for testing ($75 notional at 5×)
LIVE_LEVERAGE       = 5
LIVE_DAILY_LOSS_LIM = 10.0    # USD — auto-pause if exceeded
LIVE_MONITOR_SEC    = 60      # check open live trades every 60s

# ─────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# Data exchange — no auth needed for candles (public endpoint)
exchange = ccxt.binanceusdm({"enableRateLimit": True})

# Live exchange (real orders — uses live API keys, same account)
live_ex = ccxt.binanceusdm({
    "apiKey":  LIVE_BINANCE_API_KEY,
    "secret":  LIVE_BINANCE_API_SECRET,
    "options": {"defaultType": "future"},
})

app   = Flask(__name__)
state = {
    # Paper
    "trades":         [],
    "open_trades":    [],
    "last_scan":      "Not yet run",
    "scan_count":     0,
    "start_time":     datetime.now().strftime("%Y-%m-%d %H:%M"),
    "running":        True,
    "traded_candles": {},
    # Live
    "live_trades":       [],
    "live_open_trades":  [],
    "live_daily_loss":   0.0,
    "live_daily_reset":  datetime.now().strftime("%Y-%m-%d"),
    "live_paused":       False,
    "live_pause_reason": "",
    "ip_alert":          None,
}


# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────
def calculate_mfi(df, length=14):
    import pandas as pd
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
    except Exception as e:
        log.warning(f"fetch_candles {symbol} {tf}: {e}")
        return None


def save_trades():
    os.makedirs(os.path.dirname(TRADE_LOG), exist_ok=True)
    with open(TRADE_LOG, "w") as f:
        json.dump(state["trades"], f, indent=2, default=str)


def load_trades():
    if os.path.exists(TRADE_LOG):
        with open(TRADE_LOG) as f:
            state["trades"] = json.load(f)
        state["open_trades"] = [t for t in state["trades"] if t["status"] == "OPEN"]
        log.info(f"Loaded {len(state['trades'])} paper trades ({len(state['open_trades'])} open)")


def save_live_trades():
    os.makedirs(os.path.dirname(LIVE_TRADE_LOG), exist_ok=True)
    with open(LIVE_TRADE_LOG, "w") as f:
        json.dump(state["live_trades"], f, indent=2, default=str)


def load_live_trades():
    if os.path.exists(LIVE_TRADE_LOG):
        with open(LIVE_TRADE_LOG) as f:
            state["live_trades"] = json.load(f)
        state["live_open_trades"] = [t for t in state["live_trades"] if t["status"] == "OPEN"]
        log.info(f"Loaded {len(state['live_trades'])} live trades ({len(state['live_open_trades'])} open)")


# ─────────────────────────────────────────
# LIVE EXCHANGE OPERATIONS
# ─────────────────────────────────────────
def _handle_ip_alert(err_str):
    if "-2015" not in err_str:
        return
    match = re.search(r'request ip[:\s]+([0-9.]+)', err_str)
    ip = match.group(1) if match else "unknown"
    if state["ip_alert"] == ip:
        return
    state["ip_alert"]          = ip
    state["live_paused"]       = True
    state["live_pause_reason"] = f"Binance blocked IP {ip} — add it to API whitelist then /resume-live"
    log.error(f"⛔ IP BLOCKED: {ip} — live trading paused")
    try:
        subprocess.run(["osascript", "-e",
            f'display notification "Add {ip} to Binance API whitelist" with title "⛔ Filtered Live PAUSED"'],
            timeout=5)
    except Exception:
        pass


def _check_daily_reset():
    today = datetime.now().strftime("%Y-%m-%d")
    if state["live_daily_reset"] != today:
        state["live_daily_loss"]  = 0.0
        state["live_daily_reset"] = today
        if state["live_paused"] and "daily loss" in state["live_pause_reason"]:
            state["live_paused"]       = False
            state["live_pause_reason"] = ""
            log.info("Live daily loss reset — trading resumed")


def _set_leverage(ccxt_sym):
    try:
        live_ex.set_leverage(LIVE_LEVERAGE, ccxt_sym)
    except Exception as e:
        log.warning(f"set_leverage {ccxt_sym}: {e}")


def _calc_qty(ccxt_sym, notional, price):
    raw = notional / price
    try:
        return float(live_ex.amount_to_precision(ccxt_sym, raw))
    except Exception:
        if price >= 1000: return round(raw, 3)
        if price >= 1:    return round(raw, 1)
        return round(raw, 0)


def _cancel_order(ccxt_sym, order_id):
    if not order_id:
        return
    try:
        live_ex.fapiPrivateDeleteAlgoOrder({"algoId": order_id})
    except Exception as e1:
        try:
            live_ex.cancel_order(order_id, ccxt_sym)
        except Exception as e2:
            log.warning(f"Cancel {order_id}: algo={e1} regular={e2}")


def _get_position(ccxt_sym):
    try:
        for p in live_ex.fetch_positions([ccxt_sym]):
            if p.get("symbol") == ccxt_sym:
                return float(p.get("contracts", 0)) * (-1 if p.get("side") == "short" else 1)
        return 0.0
    except Exception as e:
        log.warning(f"get_position {ccxt_sym}: {e}")
        return None


def open_live_trade(paper_signal):
    """Fire a real SHORT order mirroring the paper signal, at minimal size."""
    if not LIVE_ENABLED or state["live_paused"]:
        return
    _check_daily_reset()

    symbol = paper_signal["symbol"] + "/USDT:USDT"
    coin   = paper_signal["symbol"]

    # Skip if already have a live trade open on this coin
    if any(t["symbol"] == coin and t["status"] == "OPEN" for t in state["live_trades"]):
        log.info(f"Live skip {coin}: already open")
        return

    notional = LIVE_BASE_MARGIN * LIVE_LEVERAGE   # e.g. 15 × 5 = $75

    try:
        _set_leverage(symbol)

        ticker      = live_ex.fetch_ticker(symbol)
        price       = ticker["last"]
        qty         = _calc_qty(symbol, notional, price)

        if qty <= 0:
            log.warning(f"Live {coin}: invalid qty {qty} at price {price}")
            return

        # Market SHORT entry
        entry_order  = live_ex.create_order(symbol, "market", "sell", qty)
        actual_entry = float(entry_order.get("average") or entry_order.get("price") or price)
        if actual_entry == 0:
            actual_entry = price

        sl_price = round(actual_entry * (1 + MAX_SL_PCT), 6)
        tp_price = round(actual_entry - (sl_price - actual_entry) * RR_RATIO, 6)

        # Exchange SL + TP (algo orders)
        sl_ord = live_ex.create_order(symbol, "STOP_MARKET", "buy", qty, params={
            "stopPrice": sl_price, "closePosition": True, "workingType": "MARK_PRICE"})
        tp_ord = live_ex.create_order(symbol, "TAKE_PROFIT_MARKET", "buy", qty, params={
            "stopPrice": tp_price, "closePosition": True, "workingType": "MARK_PRICE"})

        trade = {
            "id":             f"L_{coin}_{datetime.now().strftime('%m%d_%H%M')}",
            "symbol":         coin,
            "ccxt_symbol":    symbol,
            "entry":          round(actual_entry, 6),
            "sl":             sl_price,
            "sl_original":    sl_price,
            "tp":             tp_price,
            "qty":            qty,
            "margin_usd":     LIVE_BASE_MARGIN,
            "notional_usd":   notional,
            "mfi_peak":       paper_signal["mfi_peak"],
            "size_label":     paper_signal["size_label"],
            "status":         "OPEN",
            "result":         None,
            "pnl_pct":        None,
            "pnl_usd":        None,
            "exit_price":     None,
            "opened_at":      datetime.now().strftime("%Y-%m-%d %H:%M"),
            "closed_at":      None,
            "sl_order_id":    sl_ord.get("id") if sl_ord else None,
            "tp_order_id":    tp_ord.get("id") if tp_ord else None,
            "trailing_stage": 0,
            "trail_note":     "",
        }

        state["live_trades"].append(trade)
        state["live_open_trades"].append(trade)
        save_live_trades()
        log.info(f"✅ LIVE SHORT OPEN: {coin} | entry:{actual_entry} SL:{sl_price} TP:{tp_price} "
                 f"| qty:{qty} notional:${notional}")

    except Exception as e:
        log.error(f"Live open failed {coin}: {e}")
        _handle_ip_alert(str(e))


def monitor_live_trades():
    """Check each open live trade — trail SL, detect exchange close."""
    _check_daily_reset()

    for trade in state["live_open_trades"][:]:
        sym  = trade["ccxt_symbol"]
        coin = trade["symbol"]
        try:
            ticker        = live_ex.fetch_ticker(sym)
            current_price = ticker["last"]
            current_pnl   = round((trade["entry"] - current_price) / trade["entry"] * 100, 3)

            trade["current_price"]  = round(current_price, 6)
            trade["unrealised_pnl"] = current_pnl
            trade["unrealised_usd"] = round(current_pnl / 100 * trade["notional_usd"], 2)

            # Trailing stop
            if ENABLE_TRAIL:
                stage = trade.get("trailing_stage", 0)
                entry = trade["entry"]

                if stage == 0 and current_pnl >= TRAIL_TRIGGER_1:
                    lock1 = round(entry * (1 - TRAIL_LOCK_1 / 100), 6)
                    if lock1 < trade["sl"]:
                        _cancel_order(sym, trade.get("sl_order_id"))
                        new_sl = live_ex.create_order(sym, "STOP_MARKET", "buy", trade["qty"], params={
                            "stopPrice": lock1, "closePosition": True, "workingType": "MARK_PRICE"})
                        trade.update({"sl": lock1, "sl_pct": f"+{TRAIL_LOCK_1}% (BE+)",
                                      "sl_order_id": new_sl.get("id") if new_sl else None,
                                      "trailing_stage": 1,
                                      "trail_note": f"Stage1: SL→+{TRAIL_LOCK_1}% at +{current_pnl:.1f}%"})
                        log.info(f"LIVE TRAIL S1: {coin} SL→{lock1}")
                    stage = 1

                if stage == 1 and current_pnl >= TRAIL_TRIGGER_2:
                    lock2 = round(entry * (1 - TRAIL_LOCK_2 / 100), 6)
                    if lock2 < trade["sl"]:
                        _cancel_order(sym, trade.get("sl_order_id"))
                        new_sl = live_ex.create_order(sym, "STOP_MARKET", "buy", trade["qty"], params={
                            "stopPrice": lock2, "closePosition": True, "workingType": "MARK_PRICE"})
                        trade.update({"sl": lock2, "sl_pct": f"+{TRAIL_LOCK_2}% (BE+)",
                                      "sl_order_id": new_sl.get("id") if new_sl else None,
                                      "trailing_stage": 2,
                                      "trail_note": f"Stage2: SL→+{TRAIL_LOCK_2}% at +{current_pnl:.1f}%"})
                        log.info(f"LIVE TRAIL S2: {coin} SL→{lock2}")

            # Detect exchange close (position flat)
            pos = _get_position(sym)
            if pos is None:
                continue
            if abs(pos) < 0.0001:
                _cancel_order(sym, trade.get("sl_order_id"))
                _cancel_order(sym, trade.get("tp_order_id"))

                # Get actual exit from trade history
                exit_price = current_price
                try:
                    fills = live_ex.fetch_my_trades(sym, limit=10)
                    buys  = [f for f in reversed(fills) if f.get("side") == "buy"]
                    if buys:
                        exit_price = float(buys[0]["price"])
                except Exception:
                    pass

                pnl_pct = round((trade["entry"] - exit_price) / trade["entry"] * 100, 3)
                pnl_usd = round(pnl_pct / 100 * trade["notional_usd"], 2)

                if exit_price <= trade["tp"] * 1.005:
                    result = "WIN"
                elif pnl_pct > 0.05:
                    result = "BE+"
                else:
                    result = "LOSS"
                    state["live_daily_loss"] += abs(pnl_usd)
                    if state["live_daily_loss"] >= LIVE_DAILY_LOSS_LIM:
                        state["live_paused"]       = True
                        state["live_pause_reason"] = f"Daily loss limit ${LIVE_DAILY_LOSS_LIM} hit (${state['live_daily_loss']:.2f} today)"
                        log.warning(f"⛔ LIVE AUTO-PAUSED: {state['live_pause_reason']}")

                trade.update({"status": result, "result": result,
                              "exit_price": round(exit_price, 6),
                              "pnl_pct": pnl_pct, "pnl_usd": pnl_usd,
                              "closed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
                              "sl_order_id": None, "tp_order_id": None})
                state["live_open_trades"].remove(trade)
                log.info(f"{'✅' if result=='WIN' else '🟡' if result=='BE+' else '❌'} "
                         f"LIVE CLOSED {coin} → {result} | {pnl_pct}% (${pnl_usd})")

        except Exception as e:
            log.warning(f"Live monitor {coin}: {e}")
            _handle_ip_alert(str(e))

    save_live_trades()


def coin_in_cooldown(coin_symbol):
    if not ENABLE_COOLDOWN:
        return False
    cutoff = datetime.now() - timedelta(hours=COOLDOWN_HOURS)
    for t in state["trades"]:
        if t.get("symbol") == coin_symbol and t.get("result") == "LOSS":
            closed_str = t.get("closed_at")
            if closed_str:
                try:
                    closed_dt = datetime.strptime(closed_str, "%Y-%m-%d %H:%M")
                    if closed_dt >= cutoff:
                        return True
                except ValueError:
                    pass
    return False


def get_size_multiplier(mfi_value):
    if not ENABLE_MFI_TIERS:
        return 1.0, "1.0×"
    for lo, hi, mult in MFI_TIER:
        if lo <= mfi_value <= hi:
            return mult, f"{mult}×"
    return 1.0, "1.0×"


# ─────────────────────────────────────────
# SIGNAL CHECK (6H + 2H entry — v1.3)
# ─────────────────────────────────────────
def check_signal_6h(symbol):
    import pandas as pd
    df6h = fetch_candles(symbol, "6h", 30)
    df2h = fetch_candles(symbol, "2h", 15)
    if df6h is None or df2h is None or len(df6h) < 8 or len(df2h) < 3:
        return None

    df6h["mfi"] = calculate_mfi(df6h, MFI_LENGTH)

    c1         = df6h.iloc[-2]
    c2_open_ts = df6h.iloc[-1]["timestamp"]
    mfi_window = df6h["mfi"].iloc[-5:-2]

    if not (mfi_window >= MFI_OVERBOUGHT).any():
        return None

    c_ob = df6h.loc[mfi_window.idxmax()]

    c1_range = c1["high"] - c1["low"]
    if c1_range == 0:
        return None

    c1_body_ratio = (c1["open"] - c1["close"]) / c1_range
    c1_lower_wick = (min(c1["open"], c1["close"]) - c1["low"]) / c1_range
    avg_vol       = df6h["volume"].iloc[-20:-1].mean()

    first_red = (
        c1["close"] < c1["open"] and
        c1_body_ratio >= 0.40 and
        c1_lower_wick <= 0.35 and
        c1["high"]  < c_ob["high"] and
        c1["close"] < c_ob["close"] and
        c1["volume"] >= avg_vol
    )
    if not first_red:
        return None

    # First 2H sub-candle of c2 must be closed and red
    df2h_ts = df2h.set_index("timestamp")
    c2_ts   = pd.Timestamp(c2_open_ts)

    if c2_ts not in df2h_ts.index:
        return None

    sub = df2h_ts.loc[c2_ts]
    sub_close_utc = c2_ts.to_pydatetime() + timedelta(hours=2)
    if datetime.utcnow() < sub_close_utc:
        return None

    if sub["close"] >= sub["open"]:
        return None

    entry = round(float(sub["close"]), 6)
    sl    = round(entry * (1 + MAX_SL_PCT), 6)
    tp    = round(entry - (sl - entry) * RR_RATIO, 6)

    mfi_peak      = round(float(mfi_window.max()), 2)
    mult, mult_lbl = get_size_multiplier(mfi_peak)
    margin_usd    = round(BASE_TRADE_SIZE * mult, 0)
    position_usd  = round(margin_usd * LEVERAGE, 0)

    return {
        "id":             f"{symbol.split('/')[0]}_{datetime.now().strftime('%m%d_%H%M')}",
        "symbol":         symbol.split("/")[0],
        "entry":          entry,
        "sl":             sl,
        "sl_original":    sl,
        "tp":             tp,
        "sl_pct":         f"+{MAX_SL_PCT*100:.1f}%",
        "tp_pct":         f"-{round((entry-tp)/entry*100, 1)}%",
        "mfi_peak":       mfi_peak,
        "status":         "OPEN",
        "opened_at":      datetime.now().strftime("%Y-%m-%d %H:%M"),
        "closed_at":      None,
        "result":         None,
        "pnl_pct":        None,
        "exit_price":     None,
        "size_mult":      mult,
        "size_label":     mult_lbl,
        "margin_usd":     int(margin_usd),
        "position_usd":   int(position_usd),
        "trailing_stage": 0,
        "trail_note":     "",
        "c2_open_ts":     str(c2_open_ts),
    }


# ─────────────────────────────────────────
# UPDATE OPEN TRADES (check SL/TP + trail)
# ─────────────────────────────────────────
def update_open_trades():
    for trade in state["open_trades"][:]:
        symbol = trade["symbol"] + "/USDT:USDT"
        df_1h  = fetch_candles(symbol, "1h", 5)
        if df_1h is None:
            continue

        latest        = df_1h.iloc[-1]
        current_low   = df_1h["low"].min()
        current_high  = df_1h["high"].max()
        current_price = latest["close"]
        current_pnl   = (trade["entry"] - current_price) / trade["entry"] * 100

        # Trailing stop
        if ENABLE_TRAIL:
            stage = trade.get("trailing_stage", 0)
            entry = trade["entry"]

            if stage == 0 and current_pnl >= TRAIL_TRIGGER_1:
                lock1 = round(entry * (1 - TRAIL_LOCK_1 / 100), 6)
                if lock1 < trade["sl"]:
                    trade["sl"]             = lock1
                    trade["sl_pct"]         = f"+{TRAIL_LOCK_1}% (BE+)"
                    trade["trailing_stage"] = 1
                    trade["trail_note"]     = f"Stage1: SL locked +{TRAIL_LOCK_1}% at +{current_pnl:.1f}% profit"
                    log.info(f"TRAIL Stage1: {trade['symbol']} SL→{lock1}")
                stage = 1

            if stage == 1 and current_pnl >= TRAIL_TRIGGER_2:
                lock2 = round(entry * (1 - TRAIL_LOCK_2 / 100), 6)
                if lock2 < trade["sl"]:
                    trade["sl"]             = lock2
                    trade["sl_pct"]         = f"+{TRAIL_LOCK_2}% (BE+)"
                    trade["trailing_stage"] = 2
                    trade["trail_note"]     = f"Stage2: SL locked +{TRAIL_LOCK_2}% at +{current_pnl:.1f}% profit"
                    log.info(f"TRAIL Stage2: {trade['symbol']} SL→{lock2}")

        # Check SL / TP
        hit_tp = current_low  <= trade["tp"]
        hit_sl = current_high >= trade["sl"]

        if hit_tp or hit_sl:
            if hit_tp and hit_sl:
                result = "WIN" if df_1h.iloc[0]["open"] > trade["tp"] else "LOSS"
            elif hit_tp:
                result = "WIN"
            else:
                result = "LOSS"

            exit_price = trade["tp"] if result == "WIN" else trade["sl"]
            pnl        = round((trade["entry"] - exit_price) / trade["entry"] * 100, 3)

            if result == "LOSS" and pnl > 0.05:
                result = "BE+"

            trade.update({
                "status":     result,
                "closed_at":  datetime.now().strftime("%Y-%m-%d %H:%M"),
                "result":     result,
                "exit_price": exit_price,
                "pnl_pct":    pnl,
            })
            state["open_trades"].remove(trade)
            log.info(f"Closed: {trade['symbol']} → {result} | PnL: {pnl}%")
        else:
            trade["current_price"]  = round(current_price, 6)
            trade["unrealised_pnl"] = round(current_pnl, 3)

    save_trades()


# ─────────────────────────────────────────
# MAIN SCAN
# ─────────────────────────────────────────
def run_scan():
    log.info(f"Scan #{state['scan_count']+1} — {len(COIN_WHITELIST)} whitelisted coins")
    update_open_trades()

    new_signals = 0
    open_coins  = [t["symbol"] for t in state["open_trades"]]

    for symbol in COIN_WHITELIST:
        coin = symbol.split("/")[0]

        if coin in open_coins:
            continue
        if coin_in_cooldown(coin):
            log.info(f"  COOLDOWN skip: {coin}")
            continue

        signal = check_signal_6h(symbol)
        if signal:
            if state["traded_candles"].get(coin) == signal.get("c2_open_ts"):
                log.info(f"  CANDLE SKIP (dedup): {coin}")
                continue

            state["trades"].append(signal)
            state["open_trades"].append(signal)
            state["traded_candles"][coin] = signal["c2_open_ts"]
            new_signals += 1
            log.info(
                f"NEW PAPER SIGNAL: {signal['symbol']} | Entry:{signal['entry']} "
                f"SL:{signal['sl']} TP:{signal['tp']} | MFI:{signal['mfi_peak']} "
                f"→ {signal['size_label']} (${signal['position_usd']:,} notional)"
            )
            # Fire matching live trade at minimal size
            threading.Thread(target=open_live_trade, args=(signal,), daemon=True).start()

        time.sleep(0.2)

    state["last_scan"]  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state["scan_count"] += 1
    save_trades()
    log.info(f"Scan done. {new_signals} new signals. {len(state['open_trades'])} open.")


def trail_runner():
    time.sleep(5 * 60)
    while state["running"]:
        if state["open_trades"]:
            update_open_trades()
        time.sleep(TRAIL_INTERVAL_MIN * 60)


def live_monitor_runner():
    """Dedicated thread: monitors live open trades every LIVE_MONITOR_SEC seconds."""
    time.sleep(30)  # stagger start
    while state["running"]:
        if state["live_open_trades"]:
            monitor_live_trades()
        time.sleep(LIVE_MONITOR_SEC)


def background_runner():
    while state["running"]:
        run_scan()
        time.sleep(SCAN_INTERVAL_MIN * 60)


# ─────────────────────────────────────────
# STATS HELPER
# ─────────────────────────────────────────
def compute_stats():
    closed = [t for t in state["trades"] if t["status"] != "OPEN"]
    if not closed:
        return {}
    wins   = [t for t in closed if t["result"] == "WIN"]
    bep    = [t for t in closed if t["result"] == "BE+"]
    losses = [t for t in closed if t["result"] == "LOSS"]
    n      = len(closed)
    wr     = round((len(wins) + len(bep)) / n * 100, 1)
    pnls   = [t["pnl_pct"] for t in closed if t.get("pnl_pct") is not None]
    total_pnl = round(sum(pnls), 2)
    avg_w  = round(sum(t["pnl_pct"] for t in wins) / len(wins), 2) if wins else 0
    avg_l  = round(sum(t["pnl_pct"] for t in losses) / len(losses), 2) if losses else 0
    gw     = sum(t["pnl_pct"] for t in wins)
    gl     = abs(sum(t["pnl_pct"] for t in losses))
    pf     = round(gw / gl, 2) if gl > 0 else "∞"
    exp    = round((wr / 100) * (avg_w or 0) + ((1 - wr / 100)) * (avg_l or 0), 3)

    # Per-coin breakdown
    coins_seen = {}
    for t in closed:
        c = t["symbol"]
        if c not in coins_seen:
            coins_seen[c] = {"wins": 0, "be": 0, "losses": 0, "pnl": 0.0}
        if t["result"] == "WIN":
            coins_seen[c]["wins"] += 1
        elif t["result"] == "BE+":
            coins_seen[c]["be"] += 1
        else:
            coins_seen[c]["losses"] += 1
        coins_seen[c]["pnl"] += t.get("pnl_pct", 0) or 0

    return {
        "closed": n, "wins": len(wins), "be": len(bep), "losses": len(losses),
        "wr": wr, "total_pnl": total_pnl,
        "avg_w": avg_w, "avg_l": avg_l, "pf": pf, "exp": exp,
        "coin_stats": {k: v for k, v in sorted(coins_seen.items(), key=lambda x: -x[1]["pnl"])},
    }


def compute_live_stats():
    closed = [t for t in state["live_trades"] if t["status"] != "OPEN"]
    if not closed:
        return {}
    wins   = [t for t in closed if t["result"] == "WIN"]
    bep    = [t for t in closed if t["result"] == "BE+"]
    losses = [t for t in closed if t["result"] == "LOSS"]
    n      = len(closed)
    wr     = round((len(wins) + len(bep)) / n * 100, 1)
    total_pnl_pct = round(sum(t.get("pnl_pct", 0) or 0 for t in closed), 2)
    total_pnl_usd = round(sum(t.get("pnl_usd", 0) or 0 for t in closed), 2)
    avg_w  = round(sum(t["pnl_pct"] for t in wins) / len(wins), 2) if wins else 0
    avg_l  = round(sum(t["pnl_pct"] for t in losses) / len(losses), 2) if losses else 0
    gw     = sum(t["pnl_pct"] for t in wins) if wins else 0
    gl     = abs(sum(t["pnl_pct"] for t in losses)) if losses else 0
    pf     = round(gw / gl, 2) if gl > 0 else "∞"
    return {
        "closed": n, "wins": len(wins), "be": len(bep), "losses": len(losses),
        "wr": wr, "total_pnl_pct": total_pnl_pct, "total_pnl_usd": total_pnl_usd,
        "avg_w": avg_w, "avg_l": avg_l, "pf": pf,
        "daily_loss": round(state["live_daily_loss"], 2),
    }


# ─────────────────────────────────────────
# DASHBOARD HTML
# ─────────────────────────────────────────
HTML = r"""
<!DOCTYPE html>
<html>
<head>
    <title>Filtered MFI Hunter — Paper + Live</title>
    <meta http-equiv="refresh" content="60">
    <style>
        * { margin:0; padding:0; box-sizing:border-box; }
        body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
               background:#0d0d0d; color:#e0e0e0; padding:30px; }
        h1   { font-size:22px; color:#fff; margin-bottom:4px; }
        .sub { color:#555; font-size:13px; margin-bottom:22px; }
        h2   { font-size:13px; color:#777; text-transform:uppercase; letter-spacing:1px;
               margin:26px 0 12px; }

        .stats { display:flex; gap:12px; margin-bottom:24px; flex-wrap:wrap; }
        .stat-box { background:#1a1a1a; border:1px solid #2a2a2a; border-radius:10px;
                    padding:14px 20px; min-width:120px; }
        .stat-box .lbl { font-size:10px; color:#555; text-transform:uppercase; letter-spacing:1px; }
        .stat-box .val { font-size:20px; font-weight:700; color:#fff; margin-top:4px; }
        .green .val { color:#00c853; }
        .red   .val { color:#ff1744; }
        .gold  .val { color:#ffd600; }
        .blue  .val { color:#40c4ff; }
        .beplus .val { color:#aaff00; }

        table { width:100%; border-collapse:collapse; background:#1a1a1a;
                border-radius:10px; overflow:hidden; margin-bottom:28px; }
        th { background:#222; padding:10px 14px; text-align:left; font-size:11px;
             color:#555; text-transform:uppercase; letter-spacing:1px; }
        td { padding:10px 14px; border-top:1px solid #222; font-size:13px; }
        tr:hover td { background:#1e1e1e; }
        .coin { font-weight:700; color:#fff; font-size:15px; }
        .win  { color:#00c853; font-weight:600; }
        .loss { color:#ff1744; font-weight:600; }
        .open { color:#40c4ff; font-weight:600; }
        .bep  { color:#aaff00; font-weight:600; }
        .sl   { color:#ff6f00; }
        .tp   { color:#00c853; }
        .empty { text-align:center; padding:40px; color:#444; }

        .badge { display:inline-block; padding:3px 9px; border-radius:20px; font-size:11px; font-weight:600; }
        .b-open { background:#1e3a5f; color:#40c4ff; }
        .b-win  { background:#1b3a2a; color:#00c853; }
        .b-loss { background:#3a1b1b; color:#ff1744; }
        .b-bep  { background:#1a2a00; color:#aaff00; }

        .whitelist-box { background:#111; border:1px solid #222; border-radius:10px;
                          padding:16px 20px; margin-bottom:22px; }
        .whitelist-box h3 { font-size:12px; color:#ffd600; text-transform:uppercase;
                             letter-spacing:1px; margin-bottom:10px; }
        .coins-grid { display:flex; gap:10px; flex-wrap:wrap; }
        .coin-chip { background:#1a1a1a; border:1px solid #333; border-radius:20px;
                      padding:5px 14px; font-size:12px; font-weight:700; color:#ccc; }
        .coin-chip.active { border-color:#00c853; color:#00c853; }

        .running-dot { display:inline-block; width:8px; height:8px; background:#00c853;
                        border-radius:50%; margin-right:6px; animation:pulse 1.5s infinite; }
        @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }

        .strategy-box { background:#111; border:1px solid #222; border-radius:10px;
                          padding:16px 20px; margin-bottom:22px; }
        .strategy-box h3 { font-size:12px; color:#ffd600; text-transform:uppercase;
                             letter-spacing:1px; margin-bottom:12px; }
        .sg { display:flex; gap:40px; flex-wrap:wrap; }
        .sg-col { font-size:12px; color:#888; line-height:2; }
        .sg-col span { color:#fff; font-weight:600; }

        .path-box { background:#0d1a0d; border:1px solid #1b5e20; border-radius:10px;
                     padding:16px 20px; margin-bottom:24px; }
        .path-box h3 { font-size:12px; color:#69f0ae; text-transform:uppercase;
                        letter-spacing:1px; margin-bottom:12px; }
        .path-grid { display:flex; gap:16px; flex-wrap:wrap; }
        .path-step { background:#0a0a0a; border:1px solid #1e3a1e; border-radius:8px;
                      padding:12px 16px; min-width:180px; flex:1; }
        .path-step h4 { font-size:11px; color:#69f0ae; margin-bottom:6px; }
        .path-step p { font-size:12px; color:#aaa; line-height:1.8; }
        .path-step p strong { color:#fff; }

        .btn-close { background:#1a0000; color:#ff5252; border:1px solid #ff1744;
                      padding:4px 10px; border-radius:6px; font-size:11px; cursor:pointer; }
        .btn-close:hover { background:#ff1744; color:#fff; }
    </style>
</head>
<body>
    <h1>
        {% if running %}<span class="running-dot"></span>{% endif %}
        Filtered MFI Hunter
        <span style="color:#ffd600;font-size:16px;">— Paper Trader</span>
        <span style="color:#555;font-size:13px;margin-left:10px;">port 8081</span>
    </h1>
    <p class="sub">
        Backtest-validated coin whitelist · MFI Overbought Reversal SHORT · 6H signal + 2H entry ·
        Started {{ start_time }} &nbsp;·&nbsp; Scans: {{ scan_count }} &nbsp;·&nbsp;
        Last: {{ last_scan }}
    </p>

    <!-- Stats -->
    <div class="stats">
        <div class="stat-box blue">
            <div class="lbl">Watchlist</div>
            <div class="val">{{ coin_count }} coins</div>
        </div>
        <div class="stat-box blue">
            <div class="lbl">Open</div>
            <div class="val">{{ open_count }}</div>
        </div>
        <div class="stat-box">
            <div class="lbl">Closed</div>
            <div class="val">{{ stats.closed or 0 }}</div>
        </div>
        {% if stats %}
        <div class="stat-box {{ 'green' if stats.wr >= 60 else 'gold' if stats.wr >= 40 else 'red' }}">
            <div class="lbl">Win+BE Rate</div>
            <div class="val">{{ stats.wr }}%</div>
        </div>
        <div class="stat-box {{ 'green' if stats.total_pnl > 0 else 'red' }}">
            <div class="lbl">Total PnL%</div>
            <div class="val">{{ '+' if stats.total_pnl > 0 else '' }}{{ stats.total_pnl }}%</div>
        </div>
        <div class="stat-box {{ 'green' if stats.pf != '∞' and stats.pf|float > 1.5 else '' }}">
            <div class="lbl">Profit Factor</div>
            <div class="val">{{ stats.pf }}</div>
        </div>
        <div class="stat-box {{ 'green' if stats.exp > 0 else 'red' }}">
            <div class="lbl">Exp/Trade</div>
            <div class="val">{{ '+' if stats.exp > 0 else '' }}{{ stats.exp }}%</div>
        </div>
        {% endif %}
    </div>

    <!-- Coin Whitelist -->
    <div class="whitelist-box">
        <h3>🎯 Validated Coin Whitelist (9 coins · 180-day backtest)</h3>
        <div class="coins-grid">
        {% for sym in whitelist %}
            {% set coin = sym.split('/')[0] %}
            {% set is_active = coin in active_coins %}
            <div class="coin-chip {{ 'active' if is_active else '' }}">
                {{ coin }}{% if is_active %} 🟢{% endif %}
            </div>
        {% endfor %}
        </div>
    </div>

    <!-- Strategy -->
    <div class="strategy-box">
        <h3>⚡ Strategy Rules</h3>
        <div class="sg">
            <div class="sg-col">
                <div>Signal &nbsp;&nbsp;&nbsp;<span>MFI ≥ 80 in 3-candle OB window → quality red c1 (6H)</span></div>
                <div>Entry &nbsp;&nbsp;&nbsp;&nbsp;<span>Close of 1st 2H sub-candle of c2 (if red)</span></div>
                <div>SL &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;<span>+3%</span> above entry</div>
                <div>TP &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;<span>−4.5%</span> below entry (1.5× RR)</div>
            </div>
            <div class="sg-col">
                <div>Sizing &nbsp;&nbsp;&nbsp;<span>MFI 90-94→1.5× (best) | 80-89→0.5× | 95+→0.5×</span></div>
                <div>Cooldown &nbsp;<span>24H</span> per coin after loss</div>
                <div>Trail 1 &nbsp;&nbsp;<span>+1.5% profit → SL locks +0.5%</span></div>
                <div>Trail 2 &nbsp;&nbsp;<span>+3.0% profit → SL locks +1.0%</span></div>
            </div>
            <div class="sg-col">
                <div>Backtest WR &nbsp;<span>67% (9 coins · 180d)</span></div>
                <div>Expectancy &nbsp;&nbsp;<span>+2.15%/trade</span></div>
                <div>Profit Factor &nbsp;<span>~3.5</span></div>
                <div>Leverage &nbsp;&nbsp;&nbsp;&nbsp;<span>5×</span></div>
            </div>
        </div>
    </div>

    <!-- Scale Path to $5k -->
    <div class="path-box">
        <h3>📈 Path to $5k/month — Phase Plan</h3>
        <div class="path-grid">
            <div class="path-step">
                <h4>Phase 1 — Paper (Now)</h4>
                <p>
                    <strong>~6 trades/month</strong> from 9 coins<br>
                    Validate 67% WR holds live<br>
                    Target: 15 clean trades<br>
                    <strong>Decision point: ~8 weeks</strong>
                </p>
            </div>
            <div class="path-step">
                <h4>Phase 2 — Live Small</h4>
                <p>
                    <strong>$500 margin/trade</strong> (5×=$2.5k notional)<br>
                    ~$75/win · ~$50/loss at 67% WR<br>
                    Expected: <strong>~$300/month</strong><br>
                    Target: 20 live trades
                </p>
            </div>
            <div class="path-step">
                <h4>Phase 3 — Scale Up</h4>
                <p>
                    <strong>$3k margin/trade</strong> (5×=$15k notional)<br>
                    ~$450/win · ~$300/loss<br>
                    6 trades/month → <strong>~$1,800/month</strong><br>
                    Add coins to increase frequency
                </p>
            </div>
            <div class="path-step">
                <h4>Phase 4 — $5k Target</h4>
                <p>
                    Need <strong>$5,500 PnL/month</strong><br>
                    Option A: <strong>$8k margin/trade</strong><br>
                    Option B: <strong>Expand to 20 coins</strong> (12/month)<br>
                    Option C: Both at <strong>$4k margin × 12 trades</strong>
                </p>
            </div>
        </div>
    </div>

    <!-- Open Trades -->
    <h2>⏳ Open Trades ({{ open_count }})</h2>
    {% if open_trades %}
    <table>
        <thead><tr>
            <th>Coin</th><th>Entry</th><th>SL</th><th>TP</th>
            <th>MFI</th><th>Size</th><th>Unrealised</th><th>Trail</th><th>Opened</th><th></th>
        </tr></thead>
        <tbody>
        {% for t in open_trades %}
        <tr>
            <td class="coin">{{ t.symbol }}</td>
            <td>{{ t.entry }}</td>
            <td class="sl">{{ t.sl }}</td>
            <td class="tp">{{ t.tp }}</td>
            <td style="color:#ff6f00;">{{ t.mfi_peak }}</td>
            <td>{{ t.size_label }}</td>
            <td class="{{ 'win' if (t.unrealised_pnl or 0) > 0 else 'loss' }}">
                {{ '+' if (t.unrealised_pnl or 0) > 0 else '' }}{{ t.unrealised_pnl or '—' }}%
            </td>
            <td style="font-size:11px;color:#69f0ae;">{{ t.trail_note or '—' }}</td>
            <td style="font-size:11px;color:#555;">{{ t.opened_at }}</td>
            <td>
                <form method="post" action="/close/{{ t.id }}" style="display:inline">
                    <button class="btn-close">Close</button>
                </form>
            </td>
        </tr>
        {% endfor %}
        </tbody>
    </table>
    {% else %}
    <div class="empty">No open trades</div>
    {% endif %}

    <!-- Per-coin stats -->
    {% if stats and stats.coin_stats %}
    <h2>📊 Per-Coin Performance</h2>
    <table>
        <thead><tr>
            <th>Coin</th><th>Wins</th><th>BE+</th><th>Losses</th><th>WR%</th><th>Total PnL%</th>
        </tr></thead>
        <tbody>
        {% for coin, cs in stats.coin_stats.items() %}
        {% set total = cs.wins + cs.be + cs.losses %}
        <tr>
            <td class="coin">{{ coin }}</td>
            <td class="win">{{ cs.wins }}</td>
            <td class="bep">{{ cs.be }}</td>
            <td class="loss">{{ cs.losses }}</td>
            <td>{{ ((cs.wins + cs.be) / total * 100) | round(1) }}%</td>
            <td class="{{ 'win' if cs.pnl > 0 else 'loss' }}">
                {{ '+' if cs.pnl > 0 else '' }}{{ cs.pnl | round(2) }}%
            </td>
        </tr>
        {% endfor %}
        </tbody>
    </table>
    {% endif %}

    <!-- Closed Trades -->
    <h2>📋 Closed Trades ({{ stats.closed or 0 }})</h2>
    {% set closed = trades | selectattr('status', 'ne', 'OPEN') | list | reverse | list %}
    {% if closed %}
    <table>
        <thead><tr>
            <th>Coin</th><th>Result</th><th>Entry</th><th>Exit</th>
            <th>PnL%</th><th>MFI</th><th>Size</th><th>Trail</th><th>Opened</th><th>Closed</th>
        </tr></thead>
        <tbody>
        {% for t in closed %}
        <tr>
            <td class="coin">{{ t.symbol }}</td>
            <td>
                <span class="badge {{ 'b-win' if t.result == 'WIN' else 'b-bep' if t.result == 'BE+' else 'b-loss' }}">
                    {{ t.result }}
                </span>
            </td>
            <td>{{ t.entry }}</td>
            <td>{{ t.exit_price or '—' }}</td>
            <td class="{{ 'win' if (t.pnl_pct or 0) > 0 else 'loss' }}">
                {{ '+' if (t.pnl_pct or 0) > 0 else '' }}{{ t.pnl_pct or '—' }}%
            </td>
            <td style="color:#ff6f00;">{{ t.mfi_peak }}</td>
            <td>{{ t.size_label }}</td>
            <td style="font-size:11px;color:#555;">{{ t.trail_note or '—' }}</td>
            <td style="font-size:11px;color:#555;">{{ t.opened_at }}</td>
            <td style="font-size:11px;color:#555;">{{ t.closed_at or '—' }}</td>
        </tr>
        {% endfor %}
        </tbody>
    </table>
    {% else %}
    <div class="empty">No closed trades yet</div>
    {% endif %}

    <!-- ═══════════════════════════════════════════════════════ -->
    <!-- LIVE TRADING SECTION                                     -->
    <!-- ═══════════════════════════════════════════════════════ -->
    <hr style="border:none;border-top:2px solid #1e3a1e;margin:40px 0 28px;">
    <h1 style="font-size:20px;color:#fff;margin-bottom:4px;">
        ⚡ Live Trading
        <span style="font-size:14px;color:#555;margin-left:10px;">
            ${{ live_margin }} margin/trade · ${{ live_notional }} notional · same account · separate tracking
        </span>
        {% if live_paused %}
        <span style="background:#3a1b1b;color:#ff5252;font-size:12px;padding:4px 12px;border-radius:20px;margin-left:10px;">
            ⛔ PAUSED — {{ live_pause_reason }}
        </span>
        {% endif %}
    </h1>
    <p class="sub">Mirrors paper signals at minimal size to verify order flow, fills, and SL/TP placement</p>

    <!-- Live stats -->
    {% if lstats %}
    <div class="stats">
        <div class="stat-box {{ 'green' if lstats.wr >= 60 else 'gold' if lstats.wr >= 40 else 'red' }}">
            <div class="lbl">Live WR</div>
            <div class="val">{{ lstats.wr }}%</div>
        </div>
        <div class="stat-box {{ 'green' if lstats.total_pnl_usd > 0 else 'red' }}">
            <div class="lbl">Live PnL $</div>
            <div class="val">{{ '+' if lstats.total_pnl_usd > 0 else '' }}${{ lstats.total_pnl_usd }}</div>
        </div>
        <div class="stat-box">
            <div class="lbl">Closed</div>
            <div class="val">{{ lstats.closed }}</div>
        </div>
        <div class="stat-box {{ 'green' if lstats.pf != '∞' and lstats.pf|float > 1.5 else '' }}">
            <div class="lbl">Profit Factor</div>
            <div class="val">{{ lstats.pf }}</div>
        </div>
        <div class="stat-box {{ 'red' if lstats.daily_loss > 0 else '' }}">
            <div class="lbl">Today Loss $</div>
            <div class="val">${{ lstats.daily_loss }}</div>
        </div>
    </div>
    {% endif %}

    <!-- Live open trades -->
    <h2>⏳ Live Open Trades ({{ live_open | length }})</h2>
    {% if live_open %}
    <table>
        <thead><tr>
            <th>Coin</th><th>Entry</th><th>SL</th><th>TP</th>
            <th>MFI</th><th>Qty</th><th>Notional</th><th>Unrealised</th><th>Trail</th><th>Opened</th>
        </tr></thead>
        <tbody>
        {% for t in live_open %}
        <tr>
            <td class="coin">{{ t.symbol }}</td>
            <td>{{ t.entry }}</td>
            <td class="sl">{{ t.sl }}</td>
            <td class="tp">{{ t.tp }}</td>
            <td style="color:#ff6f00;">{{ t.mfi_peak }}</td>
            <td style="color:#aaa;">{{ t.qty }}</td>
            <td style="color:#aaa;">${{ t.notional_usd }}</td>
            <td class="{{ 'win' if (t.unrealised_pnl or 0) > 0 else 'loss' }}">
                {{ '+' if (t.unrealised_pnl or 0) > 0 else '' }}{{ t.unrealised_pnl or '—' }}%
                <span style="font-size:11px;color:#555;">
                    (${{ t.unrealised_usd or '—' }})
                </span>
            </td>
            <td style="font-size:11px;color:#69f0ae;">{{ t.trail_note or '—' }}</td>
            <td style="font-size:11px;color:#555;">{{ t.opened_at }}</td>
        </tr>
        {% endfor %}
        </tbody>
    </table>
    {% else %}
    <div class="empty">No live trades open</div>
    {% endif %}

    <!-- Live closed trades -->
    <h2>📋 Live Closed Trades ({{ lstats.closed if lstats else 0 }})</h2>
    {% set lclosed = live_trades | selectattr('status', 'ne', 'OPEN') | list | reverse | list %}
    {% if lclosed %}
    <table>
        <thead><tr>
            <th>Coin</th><th>Result</th><th>Entry</th><th>Exit</th>
            <th>PnL%</th><th>PnL $</th><th>MFI</th><th>Trail</th><th>Opened</th><th>Closed</th>
        </tr></thead>
        <tbody>
        {% for t in lclosed %}
        <tr>
            <td class="coin">{{ t.symbol }}</td>
            <td>
                <span class="badge {{ 'b-win' if t.result == 'WIN' else 'b-bep' if t.result == 'BE+' else 'b-loss' }}">
                    {{ t.result }}
                </span>
            </td>
            <td>{{ t.entry }}</td>
            <td>{{ t.exit_price or '—' }}</td>
            <td class="{{ 'win' if (t.pnl_pct or 0) > 0 else 'loss' }}">
                {{ '+' if (t.pnl_pct or 0) > 0 else '' }}{{ t.pnl_pct or '—' }}%
            </td>
            <td class="{{ 'win' if (t.pnl_usd or 0) > 0 else 'loss' }}">
                {{ '+' if (t.pnl_usd or 0) > 0 else '' }}${{ t.pnl_usd or '—' }}
            </td>
            <td style="color:#ff6f00;">{{ t.mfi_peak }}</td>
            <td style="font-size:11px;color:#555;">{{ t.trail_note or '—' }}</td>
            <td style="font-size:11px;color:#555;">{{ t.opened_at }}</td>
            <td style="font-size:11px;color:#555;">{{ t.closed_at or '—' }}</td>
        </tr>
        {% endfor %}
        </tbody>
    </table>
    {% else %}
    <div class="empty">No live trades closed yet</div>
    {% endif %}
</body>
</html>
"""


# ─────────────────────────────────────────
# FLASK ROUTES
# ─────────────────────────────────────────
@app.route("/")
def dashboard():
    stats        = compute_stats()
    lstats       = compute_live_stats()
    open_trades  = [t for t in state["trades"] if t["status"] == "OPEN"]
    active_coins = {t["symbol"] for t in open_trades}
    return render_template_string(
        HTML,
        running      = state["running"],
        scan_count   = state["scan_count"],
        last_scan    = state["last_scan"],
        start_time   = state["start_time"],
        coin_count   = len(COIN_WHITELIST),
        open_count   = len(open_trades),
        open_trades  = open_trades,
        trades       = state["trades"],
        stats        = stats,
        whitelist    = COIN_WHITELIST,
        active_coins = active_coins,
        # live
        lstats          = lstats,
        live_open       = state["live_open_trades"],
        live_trades     = state["live_trades"],
        live_paused     = state["live_paused"],
        live_pause_reason = state["live_pause_reason"],
        live_margin     = LIVE_BASE_MARGIN,
        live_notional   = LIVE_BASE_MARGIN * LIVE_LEVERAGE,
    )


@app.route("/close/<trade_id>", methods=["POST"])
def close_trade(trade_id):
    for t in state["trades"]:
        if t["id"] == trade_id and t["status"] == "OPEN":
            try:
                df = fetch_candles(t["symbol"] + "/USDT:USDT", "1h", 2)
                price = round(float(df.iloc[-1]["close"]), 6) if df is not None else t["entry"]
            except Exception:
                price = t["entry"]
            pnl = round((t["entry"] - price) / t["entry"] * 100, 3)
            t.update({
                "status":     "WIN" if pnl > 0 else "LOSS",
                "result":     "WIN" if pnl > 0 else "LOSS",
                "closed_at":  datetime.now().strftime("%Y-%m-%d %H:%M"),
                "exit_price": price,
                "pnl_pct":    pnl,
            })
            if t in state["open_trades"]:
                state["open_trades"].remove(t)
            save_trades()
            break
    return redirect("/")


@app.route("/resume-live", methods=["POST", "GET"])
def resume_live():
    state["live_paused"]       = False
    state["live_pause_reason"] = ""
    state["ip_alert"]          = None
    log.info("Live trading manually resumed via /resume-live")
    return redirect("/")


@app.route("/status")
def status():
    from flask import jsonify
    return jsonify({
        "running":     state["running"],
        "open":        len(state["open_trades"]),
        "closed":      len([t for t in state["trades"] if t["status"] != "OPEN"]),
        "live_open":   len(state["live_open_trades"]),
        "live_closed": len([t for t in state["live_trades"] if t["status"] != "OPEN"]),
        "live_paused": state["live_paused"],
        "scan_count": state["scan_count"],
        "last_scan":  state["last_scan"],
    })


# ─────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────
if __name__ == "__main__":
    load_trades()
    load_live_trades()
    live_ex.load_markets()
    log.info(f"Filtered MFI Hunter — Paper + Live — port {PORT}")
    log.info(f"Watching {len(COIN_WHITELIST)} coins: {[s.split('/')[0] for s in COIN_WHITELIST]}")
    log.info(f"Live trading: {'ENABLED' if LIVE_ENABLED else 'DISABLED'} | "
             f"${LIVE_BASE_MARGIN} margin × {LIVE_LEVERAGE}× = ${LIVE_BASE_MARGIN*LIVE_LEVERAGE} notional/trade")

    threading.Thread(target=background_runner,  daemon=True).start()
    threading.Thread(target=trail_runner,        daemon=True).start()
    threading.Thread(target=live_monitor_runner, daemon=True).start()

    app.run(host="0.0.0.0", port=PORT, debug=False)
