"""
MFI Coin Hunter — LIVE TRADER
──────────────────────────────
Real order execution on Binance Portfolio Margin (USDⓈ-M Futures)

v1.0  2026-03-20  Launch — same MFI strategy as paper trader
                  Rules: 24H cooldown | MFI tiers | Trailing stop (15 min)
                  Daily loss limit: $9 auto-pause
v1.1  2026-03-23  Entry aligned with backtest: close of first 2H sub-candle of 2nd
                  reversal 6H candle (was: live price on 3rd candle — up to 12H late)
v1.2  2026-03-23  Fix: candle dedup — traded_candles dict prevents re-entry on same
                  6H candle (was causing SAHARA ×2, STO ×2 duplicate live trades)
                  Fix: SL/TP monitor interval 5 min → 1 min to reduce SL slippage
v1.3  2026-03-24  Add: trade comment field — flag low-liquidity/high-vol coins
                  Add: IP change detection — auto-pause + dashboard alert + macOS
                  notification when Binance rejects API key (-2015)
v1.4  2026-03-25  RR ratio 1.5× → 2.0× (TP 4.5% → 6.0%) — backtest: +39% PnL, PF 2.00
                  Blacklisted PAXG + XAU — ATR 1.1% too low for fixed 3% SL (dead weight)
                  Strategy locked at 7 validated coins pending 30-trade milestone
v1.5  2026-03-26  Optimization pack (backtest validated):
                  MFI threshold 80→85 | SL 3%→2.5% | Body filter 40%→50%
                  Peak-only mode (Mon–Thu only — off-peak 31% WR vs 48% peak)
                  Flat 1× sizing (MFI tiers removed — backward in live data)

Dashboard: http://localhost:8083
Logs:      Trade Logs/live_trades.json
"""

import ccxt
import json
import os
import re
import subprocess
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
MFI_OVERBOUGHT     = 85        # upgraded from 80 — v1.5 optimization
MAX_SL_PCT         = 0.025     # 2.5% stop loss (was 3.0% — tighter risk, RR stays 2.0×)
RR_RATIO           = 2.0       # TP = 6.0% (upgraded from 1.5× — backtest: +39% PnL, PF 2.00, lower drawdown)
MIN_VOLUME_USDT    = 5_000_000
COIN_BLACKLIST     = {"PAXG", "XAU"}   # Low ATR (~1.1%) — fixed 3% SL too wide, rarely reaches 6% TP
SYMBOL_REFRESH_H   = 6
SCAN_INTERVAL_MIN  = 60
TRAIL_INTERVAL_MIN = 1    # Check price vs SL/TP every 1 min (software-based exits)
BASE_TRADE_SIZE    = 10        # USDT margin per trade
LEVERAGE           = 5
MAX_CONCURRENT     = 6         # Max open trades at once
DAILY_LOSS_LIMIT   = 9.0       # USD — auto-pause if breached

PEAK_DAYS_EST = [0, 1, 2, 3]  # Mon-Thu
PEAK_ONLY     = True           # Block off-peak (Fri–Sun) — live data: 48% WR peak vs 31% off-peak

# ── RULE 1: Coin cooldown ────────────────
ENABLE_COOLDOWN = True
COOLDOWN_HOURS  = 24

# ── RULE 2: MFI-tiered sizing ────────────
ENABLE_MFI_TIERS = False   # DISABLED v1.5: live data shows tiers are backward (higher MFI = worse WR)
MFI_TIER = [
    (95,  float("inf"), 1.5),
    (90,  94.999,       1.0),
    (80,  89.999,       0.5),
]

# ── RULE 3: Trailing stop ────────────────
ENABLE_TRAIL    = True
TRAIL_TRIGGER_1 = 1.5
TRAIL_TRIGGER_2 = 3.0
TRAIL_LOCK_1    = 0.5   # lock +0.5% at stage 1 → result: BE+
TRAIL_LOCK_2    = 1.0   # lock +1.0% at stage 2 → result: BE+

TRADE_LOG = "/Users/macbook/Downloads/Content Projects/02 - Trading Coin Hunter/Trade Logs/live_trades.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ─────────────────────────────────────────
# EXCHANGE SETUP  (USDⓈ-M Futures)
# ─────────────────────────────────────────
exchange = ccxt.binanceusdm({
    "apiKey":  LIVE_BINANCE_API_KEY,
    "secret":  LIVE_BINANCE_API_SECRET,
    "options": {"defaultType": "future"},
})

app   = Flask(__name__)
state = {
    "trades":            [],
    "open_trades":       [],
    "last_scan":         "Not yet run",
    "scan_count":        0,
    "start_time":        datetime.now().strftime("%Y-%m-%d %H:%M"),
    "running":           True,
    "paused":            False,   # True when daily loss limit hit
    "pause_reason":      "",
    "watchlist":         [],
    "watchlist_updated": None,
    "watchlist_count":   0,
    "daily_loss":        0.0,
    "daily_reset":       datetime.now().strftime("%Y-%m-%d"),
    "traded_candles":    {},   # coin → c2_open_ts already traded (prevents same-candle re-entry)
    "ip_alert":          None, # set when Binance returns -2015 (IP not whitelisted)
}

# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────
def refresh_watchlist():
    try:
        log.info("Refreshing watchlist...")
        markets = exchange.load_markets(reload=True)
        candidates = [
            s for s, m in markets.items()
            if m.get("active") and m.get("type") == "swap" and s.endswith("/USDT:USDT")
        ]
        tickers  = exchange.fetch_tickers(candidates)
        filtered = sorted([
            s for s in candidates
            if (tickers.get(s, {}).get("quoteVolume") or 0) >= MIN_VOLUME_USDT
            and s.split("/")[0] not in COIN_BLACKLIST
        ])
        state["watchlist"]         = filtered
        state["watchlist_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        state["watchlist_count"]   = len(filtered)
        log.info(f"Watchlist: {len(filtered)} pairs (excl. {COIN_BLACKLIST})")
    except Exception as e:
        log.warning(f"Watchlist refresh failed: {e}")

def maybe_refresh_watchlist():
    if not state["watchlist"] or not state["watchlist_updated"]:
        refresh_watchlist(); return
    last = datetime.strptime(state["watchlist_updated"], "%Y-%m-%d %H:%M")
    if datetime.now() - last > timedelta(hours=SYMBOL_REFRESH_H):
        refresh_watchlist()

def is_peak_liquidity():
    from datetime import timezone
    est_now = datetime.now(timezone.utc) + timedelta(hours=-5)
    return est_now.weekday() in PEAK_DAYS_EST

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

def raw_symbol(ccxt_symbol):
    """GLM/USDT:USDT → GLMUSDT"""
    return ccxt_symbol.split("/")[0] + "USDT"

def calc_qty(ccxt_sym, notional_usdt, price):
    """Calculate order quantity rounded to exchange precision."""
    raw_qty = notional_usdt / price
    try:
        # Load markets if not already loaded (needs auth)
        if not exchange.markets:
            exchange.load_markets()
        return float(exchange.amount_to_precision(ccxt_sym, raw_qty))
    except Exception:
        # Fallback: round to reasonable precision based on price magnitude
        if price >= 1000:
            return round(raw_qty, 3)
        elif price >= 1:
            return round(raw_qty, 1)
        else:
            return round(raw_qty, 0)

def save_trades():
    os.makedirs(os.path.dirname(TRADE_LOG), exist_ok=True)
    with open(TRADE_LOG, "w") as f:
        json.dump(state["trades"], f, indent=2, default=str)

def load_trades():
    if os.path.exists(TRADE_LOG):
        with open(TRADE_LOG) as f:
            state["trades"] = json.load(f)
        state["open_trades"] = [t for t in state["trades"] if t["status"] == "OPEN"]
        # Rebuild candle dedup from loaded trades (survives restarts)
        for t in state["trades"]:
            coin    = t.get("symbol", "")
            c2ts    = t.get("c2_open_ts", "")
            if coin and c2ts:
                state["traded_candles"][coin] = c2ts
        log.info(f"Loaded {len(state['trades'])} trades ({len(state['open_trades'])} open)")

def check_daily_reset():
    today = datetime.now().strftime("%Y-%m-%d")
    if state["daily_reset"] != today:
        state["daily_loss"]  = 0.0
        state["daily_reset"] = today
        if state["paused"] and "daily loss" in state["pause_reason"]:
            state["paused"]       = False
            state["pause_reason"] = ""
            log.info("Daily loss counter reset — trading resumed")

def record_daily_loss(usd_loss):
    state["daily_loss"] += abs(usd_loss)
    if state["daily_loss"] >= DAILY_LOSS_LIMIT and not state["paused"]:
        state["paused"]       = True
        state["pause_reason"] = f"Daily loss limit ${DAILY_LOSS_LIMIT} reached (lost ${state['daily_loss']:.2f} today)"
        log.warning(f"⛔ AUTO-PAUSED: {state['pause_reason']}")

# ─────────────────────────────────────────
# IP ALERT — detect Binance -2015 errors
# ─────────────────────────────────────────
def send_mac_notification(title, message):
    """Fire a macOS notification via osascript."""
    try:
        script = f'display notification "{message}" with title "{title}" sound name "Basso"'
        subprocess.run(["osascript", "-e", script], timeout=5)
    except Exception:
        pass  # non-critical

def handle_api_error(err_str, coin=""):
    """
    Call whenever a Binance API call fails.
    Detects -2015 (IP not whitelisted), auto-pauses trading, fires alert.
    """
    if "-2015" not in err_str:
        return
    # Extract IP from Binance error message
    match = re.search(r'request ip[:\s]+([0-9.]+)', err_str)
    blocked_ip = match.group(1) if match else "unknown"

    if state["ip_alert"] == blocked_ip:
        return  # Already alerted for this IP — don't spam

    state["ip_alert"] = blocked_ip
    if not state["paused"]:
        state["paused"]       = True
        state["pause_reason"] = (
            f"Binance API key blocked — IP {blocked_ip} not whitelisted. "
            f"Go to Binance → API Management → add {blocked_ip}, then resume."
        )
        log.error(f"⛔ IP ALERT: Binance rejected IP {blocked_ip} — trading auto-paused")
        send_mac_notification(
            "⛔ Live Trader PAUSED",
            f"Binance blocked IP {blocked_ip}. Whitelist it on Binance API settings."
        )

# ─────────────────────────────────────────
# RULE 1 — Coin cooldown
# ─────────────────────────────────────────
def coin_in_cooldown(coin):
    if not ENABLE_COOLDOWN or COOLDOWN_HOURS == 0:
        return False
    cutoff = datetime.now() - timedelta(hours=COOLDOWN_HOURS)
    for t in state["trades"]:
        if t.get("symbol") == coin and t.get("result") == "LOSS":
            closed_str = t.get("closed_at")
            if closed_str:
                try:
                    if datetime.strptime(closed_str, "%Y-%m-%d %H:%M") >= cutoff:
                        return True
                except ValueError:
                    pass
    return False

# ─────────────────────────────────────────
# RULE 2 — MFI-tiered sizing
# ─────────────────────────────────────────
def get_size_multiplier(mfi_value):
    if not ENABLE_MFI_TIERS:
        return 1.0, "1.0×"
    for lo, hi, mult in MFI_TIER:
        if lo <= mfi_value <= hi:
            return mult, f"{mult}×"
    return 1.0, "1.0×"

# ─────────────────────────────────────────
# EXCHANGE OPERATIONS  (USDⓈ-M Futures)
# ─────────────────────────────────────────
def set_leverage(ccxt_sym):
    try:
        exchange.set_leverage(LEVERAGE, ccxt_sym)
        log.info(f"Leverage set {LEVERAGE}× for {ccxt_sym}")
    except Exception as e:
        log.warning(f"Leverage set failed {ccxt_sym}: {e}")

def place_entry_order(ccxt_sym, qty):
    """Market SELL to open SHORT."""
    return exchange.create_order(ccxt_sym, "market", "sell", qty)

def place_sl_order(ccxt_sym, qty, stop_price):
    """STOP_MARKET BUY to close SHORT at stop loss."""
    return exchange.create_order(ccxt_sym, "STOP_MARKET", "buy", qty, params={
        "stopPrice":   stop_price,
        "closePosition": True,
        "workingType": "MARK_PRICE",
    })

def place_tp_order(ccxt_sym, qty, take_price):
    """TAKE_PROFIT_MARKET BUY to close SHORT at take profit."""
    return exchange.create_order(ccxt_sym, "TAKE_PROFIT_MARKET", "buy", qty, params={
        "stopPrice":   take_price,
        "closePosition": True,
        "workingType": "MARK_PRICE",
    })

def close_position_at_market(ccxt_sym, qty):
    """Emergency market close."""
    return exchange.create_order(ccxt_sym, "market", "buy", qty, params={"reduceOnly": True})

def cancel_order(ccxt_sym, order_id):
    """Cancel an order — tries algo cancel first (for SL/TP), then regular cancel."""
    if not order_id:
        return
    try:
        # SL/TP orders are Binance Algo/Conditional orders — require algo delete endpoint
        exchange.fapiPrivateDeleteAlgoOrder({"algoId": order_id})
        log.info(f"Algo order {order_id} cancelled")
    except Exception as algo_err:
        # Fallback to regular cancel (for entry/other non-algo orders)
        try:
            exchange.cancel_order(order_id, ccxt_sym)
        except Exception as e:
            log.warning(f"Cancel order {order_id} failed: algo={algo_err} regular={e}")

def get_position(ccxt_sym):
    """Returns position size (negative = short, 0 = flat, None = error)."""
    try:
        positions = exchange.fetch_positions([ccxt_sym])
        for p in positions:
            if p.get("symbol") == ccxt_sym:
                return float(p.get("contracts", 0)) * (-1 if p.get("side") == "short" else 1)
        return 0.0
    except Exception as e:
        log.warning(f"Position check failed {ccxt_sym}: {e}")
        return None

def get_exit_price_from_trades(ccxt_sym):
    """Fetch actual fill price of the most recent closing trade."""
    try:
        trades = exchange.fetch_my_trades(ccxt_sym, limit=10)
        buys = [t for t in reversed(trades) if t.get("side") == "buy"]
        if buys:
            return float(buys[0]["price"])
    except Exception:
        pass
    return None

def get_account_balance():
    try:
        bal  = exchange.fetch_balance()
        info = bal.get("info", {})

        unrealised = round(float(info.get("totalUnrealizedProfit", 0)), 2)
        available  = round(float(info.get("availableBalance",      0)), 2)

        # Sum all futures wallet assets converted to USDT
        total_usdt = 0.0
        btc_qty    = 0.0
        btc_usd    = 0.0
        assets     = info.get("assets", [])
        for a in assets:
            wb = float(a.get("walletBalance", 0))
            if wb <= 0:
                continue
            asset = a["asset"]
            if asset == "USDT":
                total_usdt += wb
            elif asset == "BTC":
                btc_qty = wb
                try:
                    ticker = exchange.fetch_ticker("BTC/USDT:USDT")
                    btc_usd = round(wb * ticker["last"], 2)
                    total_usdt += btc_usd
                except Exception:
                    pass
            # other assets (FDUSD, USDC etc.) treated as ~1:1
            elif asset in ("USDC", "FDUSD", "BUSD"):
                total_usdt += wb

        return {
            "usdt":       round(total_usdt, 2),
            "available":  available,
            "unrealised": unrealised,
            "total_usd":  round(total_usdt + unrealised, 2),
            "btc":        round(btc_qty, 6),
            "btc_usd":    btc_usd,
        }
    except Exception:
        pass
    return {"usdt": 0, "available": 0, "unrealised": 0, "total_usd": 0, "btc": 0, "btc_usd": 0}

# ─────────────────────────────────────────
# SIGNAL CHECK (6H)
# ─────────────────────────────────────────
def check_signal_6h(symbol):
    import pandas as pd
    df6h = fetch_candles(symbol, "6h", 30)
    df2h = fetch_candles(symbol, "2h", 15)
    if df6h is None or df2h is None or len(df6h) < 8 or len(df2h) < 3:
        return None

    df6h["mfi"] = calculate_mfi(df6h, MFI_LENGTH)

    # ── Candle mapping (matches backtest exactly) ─────────────────
    # df6h.iloc[-1] = c2  (currently forming — 2nd reversal candle)
    # df6h.iloc[-2] = c1  (last fully closed — 1st reversal candle)
    # df6h.iloc[-5:-2]    = OB window (3 candles before c1, any had MFI ≥ 80)
    c1         = df6h.iloc[-2]
    c2_open_ts = df6h.iloc[-1]["timestamp"]

    mfi_window = df6h["mfi"].iloc[-5:-2]
    if not (mfi_window >= MFI_OVERBOUGHT).any():
        return None

    # c_ob = highest MFI candle in the window (for comparison checks)
    c_ob = df6h.loc[mfi_window.idxmax()]

    # ── c1 quality checks ─────────────────────────────────────────
    c1_range = c1["high"] - c1["low"]
    if c1_range == 0:
        return None

    c1_body_ratio = (c1["open"] - c1["close"]) / c1_range
    c1_lower_wick = (min(c1["open"], c1["close"]) - c1["low"]) / c1_range
    avg_vol       = df6h["volume"].iloc[-20:-1].mean()

    first_red = (
        c1["close"] < c1["open"] and
        c1_body_ratio >= 0.50 and   # upgraded from 0.40 — stronger reversal body
        c1_lower_wick <= 0.35 and
        c1["high"]  < c_ob["high"] and
        c1["close"] < c_ob["close"] and
        c1["volume"] >= avg_vol
    )
    if not first_red:
        return None

    # ── First 2H sub-candle of c2 ─────────────────────────────────
    df2h_ts = df2h.set_index("timestamp")
    c2_ts   = pd.Timestamp(c2_open_ts)

    if c2_ts not in df2h_ts.index:
        return None

    sub = df2h_ts.loc[c2_ts]

    # Must have closed (2H after c2 started)
    sub_close_utc = c2_ts.to_pydatetime() + timedelta(hours=2)
    if datetime.utcnow() < sub_close_utc:
        return None   # first 2H not closed yet — will catch on next scan

    # Sub-candle must confirm SHORT (close < open = red)
    if sub["close"] >= sub["open"]:
        return None

    # ── Entry at close of first 2H sub-candle ─────────────────────
    entry = round(float(sub["close"]), 6)
    sl    = round(entry * (1 + MAX_SL_PCT), 6)
    tp    = round(entry - (sl - entry) * RR_RATIO, 6)

    mfi_peak     = round(float(mfi_window.max()), 2)
    mult, mult_label = get_size_multiplier(mfi_peak)
    margin_usd   = BASE_TRADE_SIZE * mult
    notional_usd = margin_usd * LEVERAGE

    return {
        "signal_symbol": symbol,
        "entry_approx":  entry,
        "sl_approx":     sl,
        "tp_approx":     tp,
        "mfi_peak":      mfi_peak,
        "mult":          mult,
        "mult_label":    mult_label,
        "margin_usd":    margin_usd,
        "notional_usd":  notional_usd,
        "peak_session":  "Peak 🟢 (Mon–Thu)" if is_peak_liquidity() else "Off-Peak 🟡 (Fri–Sun)",
        "c2_open_ts":    str(c2_open_ts),   # tracks which 6H candle triggered this entry
    }

# ─────────────────────────────────────────
# OPEN A LIVE TRADE
# ─────────────────────────────────────────
def open_live_trade(sig):
    symbol   = sig["signal_symbol"]
    coin     = symbol.split("/")[0]
    notional = sig["notional_usd"]

    log.info(f"Opening LIVE SHORT: {coin} | MFI:{sig['mfi_peak']} | "
             f"Size:{sig['mult_label']} (${notional:.0f} notional)")

    try:
        # 1. Set leverage
        set_leverage(symbol)

        # 2. Get current price for qty calculation
        ticker      = exchange.fetch_ticker(symbol)
        entry_price = ticker["last"]
        qty         = calc_qty(symbol, notional, entry_price)

        if qty <= 0:
            log.warning(f"Invalid qty for {coin}: {qty}")
            return

        # 3. Place market entry (SHORT)
        entry_order  = place_entry_order(symbol, qty)
        actual_entry = float(entry_order.get("average") or entry_order.get("price") or entry_price)
        if actual_entry == 0:
            actual_entry = entry_price

        # 4. Calculate SL/TP from actual fill price
        sl_price = round(actual_entry * (1 + MAX_SL_PCT), 6)
        tp_price = round(actual_entry - (sl_price - actual_entry) * RR_RATIO, 6)

        # 5. Place exchange SL and TP orders (STOP_MARKET + TAKE_PROFIT_MARKET)
        sl_order = place_sl_order(symbol, qty, sl_price)
        tp_order = place_tp_order(symbol, qty, tp_price)
        sl_order_id = sl_order.get("id") if sl_order else None
        tp_order_id = tp_order.get("id") if tp_order else None
        log.info(f"  SL order {sl_order_id} @ {sl_price} | TP order {tp_order_id} @ {tp_price}")

        trade = {
            "id":             f"{coin}_{datetime.now().strftime('%m%d_%H%M')}",
            "symbol":         coin,
            "ccxt_symbol":    symbol,
            "raw_symbol":     raw_symbol(symbol),
            "entry":          round(actual_entry, 6),
            "sl":             sl_price,
            "sl_original":    sl_price,
            "tp":             tp_price,
            "sl_pct":         f"+{MAX_SL_PCT*100:.1f}%",
            "tp_pct":         f"-{round((actual_entry-tp_price)/actual_entry*100,1)}%",
            "qty":            qty,
            "mfi_peak":       sig["mfi_peak"],
            "size_mult":      sig["mult"],
            "size_label":     sig["mult_label"],
            "margin_usd":     sig["margin_usd"],
            "position_usd":   notional,
            "status":         "OPEN",
            "result":         None,
            "pnl_pct":        None,
            "pnl_usd":        None,
            "exit_price":     None,
            "opened_at":      datetime.now().strftime("%Y-%m-%d %H:%M"),
            "closed_at":      None,
            "peak_session":   sig["peak_session"],
            "c2_open_ts":     sig.get("c2_open_ts", ""),
            "trailing_stage": 0,
            "trail_note":     "",
            "entry_order_id": entry_order.get("id"),
            "sl_order_id":    sl_order_id,
            "tp_order_id":    tp_order_id,
            "rule_version":   "v1.3-live",
        }

        state["trades"].append(trade)
        state["open_trades"].append(trade)
        save_trades()
        log.info(f"✅ LIVE SHORT OPEN: {coin} | Entry:{actual_entry} SL:{sl_price} TP:{tp_price} | Exchange SL+TP orders placed")

    except Exception as e:
        log.error(f"Failed to open trade {coin}: {e}")
        handle_api_error(str(e), coin)

# ─────────────────────────────────────────
# MONITOR OPEN TRADES
# ─────────────────────────────────────────
def monitor_open_trades():
    """
    Exchange SL/TP monitoring.
    SL (STOP_MARKET) and TP (TAKE_PROFIT_MARKET) are placed on Binance — exchange
    executes them automatically. This monitor:
      1. Updates unrealised PnL display
      2. Moves SL to lock profit when trail triggers (cancels old SL, places new one)
      3. Detects when exchange closed the position (pos size = 0) and records result
    """
    for trade in state["open_trades"][:]:
        ccxt_sym = trade["ccxt_symbol"]
        coin     = trade["symbol"]

        try:
            # ── 1. Current price for display ──────────────────────
            ticker        = exchange.fetch_ticker(ccxt_sym)
            current_price = ticker["last"]
            current_pnl   = round((trade["entry"] - current_price) / trade["entry"] * 100, 3)

            trade["current_price"]  = round(current_price, 6)
            trade["unrealised_pnl"] = current_pnl
            trade["unrealised_usd"] = round(current_pnl / 100 * trade["position_usd"], 2)

            # ── 2. Trail stop — cancel old SL, place new one ──────
            if ENABLE_TRAIL:
                stage = trade.get("trailing_stage", 0)
                entry = trade["entry"]

                if stage == 0 and current_pnl >= TRAIL_TRIGGER_1:
                    lock1_price = round(entry * (1 - TRAIL_LOCK_1 / 100), 6)
                    if lock1_price < trade["sl"]:
                        cancel_order(ccxt_sym, trade.get("sl_order_id"))
                        new_sl = place_sl_order(ccxt_sym, trade["qty"], lock1_price)
                        trade["sl"]             = lock1_price
                        trade["sl_original"]    = trade.get("sl_original", trade["sl"])
                        trade["sl_pct"]         = f"+{TRAIL_LOCK_1}% (BE+)"
                        trade["sl_order_id"]    = new_sl.get("id") if new_sl else None
                        trade["trailing_stage"] = 1
                        trade["trail_note"]     = f"Stage1: SL→+{TRAIL_LOCK_1}% at +{current_pnl:.1f}%"
                        log.info(f"TRAIL S1: {coin} SL→{lock1_price} | new SL order {trade['sl_order_id']}")
                    stage = 1

                if stage == 1 and current_pnl >= TRAIL_TRIGGER_2:
                    lock2_price = round(entry * (1 - TRAIL_LOCK_2 / 100), 6)
                    if lock2_price < trade["sl"]:
                        cancel_order(ccxt_sym, trade.get("sl_order_id"))
                        new_sl = place_sl_order(ccxt_sym, trade["qty"], lock2_price)
                        trade["sl"]             = lock2_price
                        trade["sl_pct"]         = f"+{TRAIL_LOCK_2}% (BE+)"
                        trade["sl_order_id"]    = new_sl.get("id") if new_sl else None
                        trade["trailing_stage"] = 2
                        trade["trail_note"]     = f"Stage2: SL→+{TRAIL_LOCK_2}% at +{current_pnl:.1f}%"
                        log.info(f"TRAIL S2: {coin} SL→{lock2_price} | new SL order {trade['sl_order_id']}")

            # ── 3. Check if exchange closed the position ──────────
            pos_size = get_position(ccxt_sym)
            if pos_size is None:
                continue   # API error — skip this cycle, try next time

            if abs(pos_size) < 0.0001:   # position flat — SL or TP filled on exchange
                # Cancel the surviving open order (whichever didn't fill)
                cancel_order(ccxt_sym, trade.get("sl_order_id"))
                cancel_order(ccxt_sym, trade.get("tp_order_id"))

                # Get actual fill price from trade history
                exit_price = get_exit_price_from_trades(ccxt_sym) or current_price
                pnl_pct    = round((trade["entry"] - exit_price) / trade["entry"] * 100, 3)

                if exit_price <= trade["tp"] * 1.005:   # exited near or below TP
                    result = "WIN"
                elif pnl_pct > 0.05:
                    result = "BE+"
                else:
                    result = "LOSS"

                pnl_usd = round(pnl_pct / 100 * trade["position_usd"], 2)
                trade.update({
                    "status":       result,
                    "result":       result,
                    "exit_price":   round(exit_price, 6),
                    "pnl_pct":      pnl_pct,
                    "pnl_usd":      pnl_usd,
                    "closed_at":    datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "sl_order_id":  None,
                    "tp_order_id":  None,
                })
                state["open_trades"].remove(trade)

                if result == "LOSS":
                    record_daily_loss(abs(pnl_usd))

                log.info(f"{'✅' if result=='WIN' else '🟡' if result=='BE+' else '❌'} "
                         f"CLOSED {coin} → {result} | PnL:{pnl_pct}% (${pnl_usd}) | exit:{exit_price}")

        except Exception as e:
            log.warning(f"Monitor error {coin}: {e}")
            handle_api_error(str(e), coin)

    save_trades()

# ─────────────────────────────────────────
# MAIN SCAN (60 min)
# ─────────────────────────────────────────
def run_scan():
    check_daily_reset()

    if state["paused"]:
        log.info(f"⛔ PAUSED — skipping scan. Reason: {state['pause_reason']}")
        return

    # ── PEAK_ONLY: skip new entries on Fri–Sun (off-peak 31% WR vs 48% peak) ──
    if PEAK_ONLY and not is_peak_liquidity():
        state["last_scan"]  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state["scan_count"] += 1
        log.info("Off-peak day (Fri–Sun) — signal scan skipped (PEAK_ONLY=True)")
        return

    log.info(f"Scan #{state['scan_count']+1} | Open trades: {len(state['open_trades'])}")
    maybe_refresh_watchlist()

    new_signals = 0
    open_coins  = [t["symbol"] for t in state["open_trades"]]

    if len(state["open_trades"]) >= MAX_CONCURRENT:
        log.info(f"Max concurrent trades ({MAX_CONCURRENT}) reached — skipping entries")
        state["last_scan"]  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state["scan_count"] += 1
        return

    for symbol in state["watchlist"]:
        coin = symbol.split("/")[0]
        if coin in open_coins:
            continue
        if coin_in_cooldown(coin):
            continue
        if len(state["open_trades"]) >= MAX_CONCURRENT:
            break

        sig = check_signal_6h(symbol)
        if sig:
            # Dedup: skip if we already traded this 6H candle for this coin
            if state["traded_candles"].get(coin) == sig.get("c2_open_ts"):
                log.info(f"  CANDLE SKIP: {coin} — already traded c2_open_ts {sig['c2_open_ts']}")
                continue
            state["traded_candles"][coin] = sig["c2_open_ts"]   # lock this candle
            open_live_trade(sig)
            new_signals += 1
            open_coins.append(coin)

        time.sleep(0.2)

    state["last_scan"]  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state["scan_count"] += 1
    log.info(f"Scan done. {new_signals} new trades.")

# ─────────────────────────────────────────
# BACKGROUND THREADS
# ─────────────────────────────────────────
def trail_runner():
    time.sleep(5 * 60)
    while state["running"]:
        if state["open_trades"] and not state["paused"]:
            log.info(f"Trail check: {len(state['open_trades'])} open...")
            monitor_open_trades()
        time.sleep(TRAIL_INTERVAL_MIN * 60)

def background_runner():
    while state["running"]:
        run_scan()
        time.sleep(SCAN_INTERVAL_MIN * 60)
    log.info("Live trader stopped.")

# ─────────────────────────────────────────
# DASHBOARD HTML
# ─────────────────────────────────────────
HTML = r"""
<!DOCTYPE html>
<html>
<head>
    <title>MFI Coin Hunter — LIVE</title>
    <meta http-equiv="refresh" content="60">
    <style>
        * { margin:0; padding:0; box-sizing:border-box; }
        body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
               background:#0d0d0d; color:#e0e0e0; padding:30px; }
        h1   { font-size:24px; color:#fff; margin-bottom:4px; }
        .sub { color:#666; font-size:13px; margin-bottom:20px; }

        .live-banner { background:#1a0000; border:1px solid #ff1744; border-radius:10px;
                       padding:12px 20px; margin-bottom:20px; display:flex;
                       align-items:center; gap:12px; }
        .live-dot { width:10px; height:10px; background:#ff1744; border-radius:50%;
                    animation:pulse 1s infinite; flex-shrink:0; }
        .live-banner p { font-size:13px; color:#ff6f6f; }
        .live-banner strong { color:#fff; }

        .paused-banner { background:#1a1a00; border:1px solid #ffd600; border-radius:10px;
                         padding:12px 20px; margin-bottom:20px; }
        .paused-banner p { font-size:13px; color:#ffd600; }

        .stats { display:flex; gap:14px; margin-bottom:22px; flex-wrap:wrap; }
        .stat-box { background:#1a1a1a; border:1px solid #2a2a2a; border-radius:10px;
                    padding:14px 20px; min-width:130px; }
        .stat-box .label { font-size:11px; color:#555; text-transform:uppercase; letter-spacing:1px; }
        .stat-box .value { font-size:20px; font-weight:700; color:#fff; margin-top:4px; }
        .green .value { color:#00c853; }
        .red   .value { color:#ff1744; }
        .blue  .value { color:#1e88e5; }
        .gold  .value { color:#ffd600; }
        .beplus .value { color:#aaff00; }

        h2 { font-size:15px; color:#aaa; margin:24px 0 12px; text-transform:uppercase; letter-spacing:1px; }

        table { width:100%; border-collapse:collapse; background:#1a1a1a;
                border-radius:10px; overflow:hidden; margin-bottom:28px; }
        th { background:#222; padding:11px 14px; text-align:left; font-size:11px;
             color:#666; text-transform:uppercase; letter-spacing:1px; }
        td { padding:11px 14px; border-top:1px solid #222; font-size:13px; }
        tr:hover td { background:#1e1e1e; }

        .coin    { font-weight:700; color:#fff; font-size:15px; }
        .sl-val  { color:#ff6f00; }
        .tp-val  { color:#00c853; }
        .pnl-pos { color:#00c853; font-weight:600; }
        .pnl-neg { color:#ff1744; font-weight:600; }
        .usd-pos { color:#00c853; font-weight:600; }
        .usd-neg { color:#ff1744; font-weight:600; }
        .tv-link { color:#1e88e5; text-decoration:none; font-size:12px; }

        .badge { display:inline-block; padding:3px 9px; border-radius:20px; font-size:11px; font-weight:600; }
        .badge-open   { background:#1e3a5f; color:#1e88e5; }
        .badge-win    { background:#1b3a2a; color:#00c853; }
        .badge-loss   { background:#3a1b1b; color:#ff1744; }
        .badge-beplus { background:#1a2a00; color:#aaff00; }
        .badge-manual { background:#2a2a00; color:#ffd600; }

        .size-badge { display:inline-block; padding:2px 7px; border-radius:5px; font-size:11px; font-weight:700; }
        .size-half  { background:#1a1a00; color:#ffd600; border:1px solid #555; }
        .size-full  { background:#001a00; color:#00c853; border:1px solid #00c853; }
        .size-over  { background:#1a0040; color:#b57bee; border:1px solid #b57bee; }

        .trail-badge { display:inline-block; padding:2px 7px; border-radius:5px; font-size:10px; font-weight:600; }
        .trail-0 { color:#555; }
        .trail-1 { background:#001a0d; color:#00c853; border:1px solid #00c853; }
        .trail-2 { background:#004d1a; color:#00ff88; border:1px solid #00ff88; }

        .daily-bar-wrap { background:#111; border-radius:6px; height:6px; width:120px;
                          display:inline-block; vertical-align:middle; margin-left:8px; }
        .daily-bar { height:6px; border-radius:6px; background:#ff1744; }

        .running-dot { display:inline-block; width:8px; height:8px; background:#00c853;
                       border-radius:50%; margin-right:6px; animation:pulse 1.5s infinite; }
        @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }

        .ip-alert-banner { background:#1a0a00; border:2px solid #ff6f00; border-radius:10px;
                           padding:14px 20px; margin-bottom:20px; }
        .ip-alert-banner p { font-size:13px; color:#ff9800; line-height:1.7; }
        .ip-alert-banner strong { color:#fff; }
        .ip-alert-banner code { background:#2a1500; color:#ffcc02; padding:2px 8px;
                                border-radius:4px; font-size:13px; }

        .comment-tag { display:inline-block; background:#2a1800; color:#ff9800;
                       border:1px solid #ff6f00; border-radius:4px; padding:2px 7px;
                       font-size:10px; font-weight:600; margin-right:4px; }
        .btn-comment { background:transparent; color:#444; border:1px solid #333;
                       padding:3px 7px; border-radius:5px; font-size:10px; cursor:pointer; }
        .btn-comment:hover { color:#ff9800; border-color:#ff6f00; }
        .empty { text-align:center; padding:40px; color:#444; }

        .btn-close { background:#1a0000; color:#ff5252; border:1px solid #ff1744;
                     padding:4px 10px; border-radius:6px; font-size:11px; font-weight:600; cursor:pointer; }
        .btn-close:hover { background:#ff1744; color:#fff; }
        .btn-edit  { background:#001a2a; color:#40c4ff; border:1px solid #1e88e5;
                     padding:4px 10px; border-radius:6px; font-size:11px; font-weight:600;
                     cursor:pointer; margin-right:4px; }
        .btn-edit:hover { background:#1e88e5; color:#fff; }

        /* Edit modal */
        .modal-overlay { display:none; position:fixed; inset:0; background:rgba(0,0,0,0.75);
                         z-index:1000; align-items:center; justify-content:center; }
        .modal-overlay.active { display:flex; }
        .modal { background:#1a1a1a; border:1px solid #333; border-radius:14px; padding:28px 32px; width:360px; }
        .modal h3 { font-size:14px; color:#fff; margin-bottom:6px; }
        .modal .warn { font-size:11px; color:#ff6f00; margin-bottom:16px; }
        .modal label { font-size:11px; color:#666; text-transform:uppercase; letter-spacing:1px;
                       display:block; margin-bottom:4px; margin-top:14px; }
        .modal input { width:100%; background:#111; border:1px solid #333; border-radius:8px;
                       color:#fff; font-size:14px; padding:8px 12px; outline:none; }
        .modal input:focus { border-color:#1e88e5; }
        .modal-hint { font-size:10px; color:#555; margin-top:4px; }
        .modal-actions { display:flex; gap:10px; margin-top:20px; }
        .modal-actions button { flex:1; padding:9px; border-radius:8px; font-size:13px;
                                font-weight:600; cursor:pointer; border:none; }
        .btn-save   { background:#1e88e5; color:#fff; }
        .btn-cancel { background:#222; color:#aaa; }
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
    </style>
</head>
<body>
    <h1>
        {% if running and not paused %}<span class="running-dot"></span>{% endif %}
        MFI Coin Hunter
        <span style="color:#ff1744;font-size:18px;">— LIVE TRADING</span>
        <span style="color:#555;font-size:13px;margin-left:10px;">Portfolio Margin · 5× Leverage</span>
    </h1>
    <p class="sub">Started: {{ start_time }} · Signal scan: 60min · Trail check: 15min · Last scan: {{ last_scan }}</p>

    {% if ip_alert %}
    <div class="ip-alert-banner">
        <p>🚨 <strong>BINANCE IP BLOCKED</strong> — API key rejected for IP <code>{{ ip_alert }}</code></p>
        <p>Go to <strong>Binance → API Management → your live key → IP restriction</strong> and add <code>{{ ip_alert }}</code></p>
        <p style="margin-top:8px;">
            <form method="POST" action="/clear-ip-alert" style="display:inline;">
                <button type="submit" style="background:#ff6f00;color:#fff;border:none;padding:5px 14px;border-radius:6px;font-size:12px;font-weight:600;cursor:pointer;">
                    ✓ Whitelisted — Clear Alert & Resume
                </button>
            </form>
        </p>
    </div>
    {% endif %}

    {% if paused %}
    <div class="paused-banner">
        <p>⛔ <strong>AUTO-PAUSED</strong> — {{ pause_reason }}</p>
        <p style="margin-top:4px;font-size:12px;color:#888;">Trading will resume tomorrow when daily loss counter resets.</p>
    </div>
    {% else %}
    <div class="live-banner">
        <div class="live-dot"></div>
        <p>
        <strong>LIVE</strong> — Binance USDⓈ-M Futures · Exchange SL/TP orders (STOP_MARKET + TAKE_PROFIT_MARKET) ·
        Wallet: <strong>${{ bal.total_usd }}</strong>
        <span style="color:#555;font-size:11px;">
            (${{ "%.2f"|format(bal.usdt - bal.btc_usd) }} USDT{% if bal.btc > 0 %} + {{ bal.btc }} BTC ≈ ${{ bal.btc_usd }}{% endif %})
            · Available: ${{ bal.available }} · Unrealised: {{ '+' if bal.unrealised >= 0 else '' }}${{ "%.2f"|format(bal.unrealised) }}
        </span>
        &nbsp;·&nbsp; Daily loss: <strong>${{ daily_loss }}</strong> / ${{ daily_limit }}
        <span class="daily-bar-wrap"><span class="daily-bar" style="width:{{ [daily_loss/daily_limit*100,100]|min }}%;"></span></span>
        </p>
    </div>
    {% endif %}

    {% if insights %}
    <div class="insights-box">
        <h3>🧠 Learning Insights — Milestone {{ insights.milestone }} Trades (Live)</h3>
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
    </div>
    {% endif %}

    <!-- Stats -->
    <div class="stats">
        <div class="stat-box gold">
            <div class="label">Portfolio Value</div>
            <div class="value">${{ bal.total_usd }}</div>
        </div>
        <div class="stat-box blue">
            <div class="label">Wallet Balance</div>
            <div class="value">${{ bal.usdt }}</div>
        </div>
        <div class="stat-box blue">
            <div class="label">Available Margin</div>
            <div class="value">${{ bal.available }}</div>
        </div>
        <div class="stat-box blue">
            <div class="label">Open Trades</div>
            <div class="value">{{ open_trades|length }} / {{ max_concurrent }}</div>
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
        <div class="stat-box {{ 'green' if win_rate >= 60 else 'red' }}">
            <div class="label">Win+BE+ Rate</div>
            <div class="value">{{ win_rate }}%</div>
        </div>
        <div class="stat-box {{ 'green' if profit_factor >= 2 else 'blue' }}">
            <div class="label">Profit Factor</div>
            <div class="value">{{ profit_factor }}</div>
        </div>
        <div class="stat-box {{ 'green' if realised_usd >= 0 else 'red' }}">
            <div class="label">Realised PnL $</div>
            <div class="value">{{ '+' if realised_usd >= 0 else '' }}${{ "%.2f"|format(realised_usd) }}</div>
        </div>
        <div class="stat-box {{ 'green' if unrealised_usd >= 0 else 'red' }}">
            <div class="label">Unrealised $</div>
            <div class="value">{{ '+' if unrealised_usd >= 0 else '' }}${{ "%.2f"|format(unrealised_usd) }}</div>
        </div>
        <div class="stat-box">
            <div class="label">Scans</div>
            <div class="value">{{ scan_count }}</div>
        </div>
    </div>

    {% if open_trades %}
    <h2>🔴 Live Open Positions ({{ open_trades|length }})</h2>
    <table>
        <thead><tr>
            <th>Coin</th><th>Session</th><th>Entry</th>
            <th>Stop Loss</th><th>Take Profit</th>
            <th>Size</th><th>Notional</th>
            <th>MFI</th><th>Trail</th>
            <th>PnL %</th><th>PnL $</th>
            <th>Opened</th><th>Chart</th><th>Actions</th>
        </tr></thead>
        <tbody>
        {% for t in open_trades %}
        <tr>
            <td class="coin">{{ t.symbol }}</td>
            <td style="font-size:11px;">{{ t.peak_session or '—' }}</td>
            <td>{{ t.entry }}</td>
            <td class="sl-val">
                {{ t.sl }}
                <br><small style="color:#555;">{{ t.sl_pct }}</small>
            </td>
            <td class="tp-val">{{ t.tp }} <small>({{ t.tp_pct }})</small></td>
            <td>
                {% set mult = t.size_mult or 1.0 %}
                {% if mult >= 1.5 %}<span class="size-badge size-over">1.5×</span>
                {% elif mult <= 0.5 %}<span class="size-badge size-half">0.5×</span>
                {% else %}<span class="size-badge size-full">1.0×</span>{% endif %}
            </td>
            <td style="color:#ffd600;">${{ "%.0f"|format(t.position_usd or 50) }}</td>
            <td style="color:#ff1744;">{{ t.mfi_peak }}</td>
            <td>
                {% set stage = t.trailing_stage or 0 %}
                {% if stage == 2 %}<span class="trail-badge trail-2">🔒 +1%</span>
                {% elif stage == 1 %}<span class="trail-badge trail-1">✅ +0.5%</span>
                {% else %}<span class="trail-badge trail-0">—</span>{% endif %}
            </td>
            <td class="{{ 'pnl-pos' if (t.unrealised_pnl or 0) >= 0 else 'pnl-neg' }}">
                {{ '+' if (t.unrealised_pnl or 0) >= 0 else '' }}{{ t.unrealised_pnl or '—' }}%
            </td>
            <td class="{{ 'usd-pos' if (t.unrealised_usd or 0) >= 0 else 'usd-neg' }}">
                {{ '+' if (t.unrealised_usd or 0) >= 0 else '' }}${{ "%.2f"|format(t.unrealised_usd or 0) }}
            </td>
            <td style="color:#555;font-size:11px;">{{ t.opened_at }}</td>
            <td><a class="tv-link" href="https://www.tradingview.com/chart/?symbol=BINANCE:{{ t.symbol }}USDT.P&interval=360" target="_blank">6H →</a></td>
            <td style="white-space:nowrap;">
                <button class="btn-edit"
                        onclick="openEdit('{{ t.id }}','{{ t.symbol }}','{{ t.sl }}','{{ t.tp }}')">
                    Edit
                </button>
                <form method="POST" action="/close/{{ t.id }}" style="display:inline;margin:0;">
                    <button class="btn-close" type="submit"
                            onclick="return confirm('Close {{ t.symbol }} at market price? This places a real market order.')">
                        Close
                    </button>
                </form>
            </td>
        </tr>
        {% endfor %}
        </tbody>
    </table>
    {% endif %}

    {% if closed_trades %}
    <h2>📋 Closed Trades ({{ closed_trades|length }})</h2>
    <table>
        <thead><tr>
            <th>Coin</th><th>Result</th><th>Size</th>
            <th>Entry</th><th>Exit</th>
            <th>PnL %</th><th>PnL $</th>
            <th>Trail Note</th><th>Notes / Blacklist</th><th>Opened</th><th>Closed</th>
        </tr></thead>
        <tbody>
        {% for t in closed_trades|reverse %}
        <tr>
            <td class="coin">{{ t.symbol }}</td>
            <td>
                <span class="badge badge-{{ 'beplus' if t.result == 'BE+' else ('manual' if t.result == 'MANUAL' else t.result|lower) }}">
                    {{ t.result }}
                </span>
            </td>
            <td>
                {% set mult = t.size_mult or 1.0 %}
                {% if mult >= 1.5 %}<span class="size-badge size-over">1.5×</span>
                {% elif mult <= 0.5 %}<span class="size-badge size-half">0.5×</span>
                {% else %}<span class="size-badge size-full">1×</span>{% endif %}
            </td>
            <td>{{ t.entry }}</td>
            <td>{{ t.exit_price }}</td>
            <td class="{{ 'pnl-pos' if (t.pnl_pct or 0) >= 0 else 'pnl-neg' }}">
                {{ '+' if (t.pnl_pct or 0) >= 0 else '' }}{{ t.pnl_pct }}%
            </td>
            <td class="{{ 'usd-pos' if (t.pnl_usd or 0) >= 0 else 'usd-neg' }}">
                {{ '+' if (t.pnl_usd or 0) >= 0 else '' }}${{ "%.2f"|format(t.pnl_usd or 0) }}
            </td>
            <td style="font-size:11px;color:#555;">{{ t.trail_note or '—' }}</td>
            <td>
                {% if t.comment %}
                    <span class="comment-tag">{{ t.comment }}</span>
                {% endif %}
                <button class="btn-comment"
                        onclick="addComment('{{ t.id }}', '{{ t.symbol }}', '{{ t.comment or '' }}')">
                    {{ '✏️' if t.comment else '+ note' }}
                </button>
            </td>
            <td style="color:#555;font-size:11px;">{{ t.opened_at }}</td>
            <td style="color:#555;font-size:11px;">{{ t.closed_at }}</td>
        </tr>
        {% endfor %}
        </tbody>
    </table>
    {% endif %}

    {% if not open_trades and not closed_trades %}
    <div class="empty">⏳ Scanning for first live signal... Refreshes every 60 seconds.</div>
    {% endif %}

    <!-- Edit SL/TP Modal -->
    <div class="modal-overlay" id="editModal">
        <div class="modal">
            <h3 id="modalTitle">Edit SL / TP</h3>
            <p class="warn">⚠️ Cancels the live Binance order and places a new one at your price.</p>
            <form method="POST" id="editForm" action="">
                <label>Stop Loss (price)</label>
                <input type="number" name="new_sl" id="modalSL" step="any" min="0.000001" required>
                <div class="modal-hint">Must be ABOVE entry for shorts.</div>
                <label>Take Profit (price)</label>
                <input type="number" name="new_tp" id="modalTP" step="any" min="0.000001" required>
                <div class="modal-hint">Must be BELOW entry for shorts.</div>
                <div class="modal-actions">
                    <button type="submit" class="btn-save">Save & Replace Orders</button>
                    <button type="button" class="btn-cancel" onclick="closeEdit()">Cancel</button>
                </div>
            </form>
        </div>
    </div>
    <script>
        function openEdit(id, label, sl, tp) {
            document.getElementById('modalTitle').textContent = 'Edit SL / TP — ' + label;
            document.getElementById('modalSL').value = sl;
            document.getElementById('modalTP').value = tp;
            document.getElementById('editForm').action = '/edit/' + id;
            document.getElementById('editModal').classList.add('active');
        }
        function closeEdit() { document.getElementById('editModal').classList.remove('active'); }
        document.getElementById('editModal').addEventListener('click', function(e) {
            if (e.target === this) closeEdit();
        });

        function addComment(id, symbol, current) {
            var suggestions = [
                '⚠️ Low liquidity',
                '🚫 Blacklist — SL slippage',
                '🚫 Blacklist — High volatility',
                '📌 Watch — review at 20 trades',
                '✅ Good entry',
            ];
            var hint = 'Suggestions:\n' + suggestions.map((s,i) => (i+1)+'. '+s).join('\n') + '\n\nOr type your own:';
            var note = prompt(symbol + ' — ' + hint, current || '');
            if (note === null) return;
            fetch('/comment/' + id, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({comment: note.trim()})
            }).then(function(r) { if (r.ok) location.reload(); });
        }
    </script>
</body>
</html>
"""

# ─────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────
@app.route("/edit/<trade_id>", methods=["POST"])
def edit_trade(trade_id):
    """Cancel live SL/TP orders and replace at new prices."""
    trade = next((t for t in state["open_trades"] if t["id"] == trade_id), None)
    if not trade:
        return "Not found", 404
    try:
        new_sl = float(request.form.get("new_sl", 0))
        new_tp = float(request.form.get("new_tp", 0))
        if new_sl <= 0 or new_tp <= 0:
            return "Invalid values", 400

        ccxt_sym = trade["ccxt_symbol"]

        # Cancel existing exchange orders
        cancel_order(ccxt_sym, trade.get("sl_order_id"))
        cancel_order(ccxt_sym, trade.get("tp_order_id"))

        # Place new orders at updated prices
        sl_order = place_sl_order(ccxt_sym, trade["qty"], new_sl)
        tp_order = place_tp_order(ccxt_sym, trade["qty"], new_tp)

        trade["sl"]         = round(new_sl, 6)
        trade["tp"]         = round(new_tp, 6)
        trade["sl_pct"]     = f"+{round(abs(trade['entry']-new_sl)/trade['entry']*100,1)}% (manual)"
        trade["tp_pct"]     = f"-{round(abs(trade['entry']-new_tp)/trade['entry']*100,1)}% (manual)"
        trade["sl_order_id"] = sl_order.get("id") if sl_order else None
        trade["tp_order_id"] = tp_order.get("id") if tp_order else None
        save_trades()
        log.info(f"EDIT: {trade['symbol']} | SL→{new_sl} ({trade['sl_order_id']}) TP→{new_tp} ({trade['tp_order_id']})")
    except Exception as e:
        log.error(f"Edit failed: {e}")
    return redirect("/")

@app.route("/close/<trade_id>", methods=["POST"])
def close_trade(trade_id):
    trade = next((t for t in state["open_trades"] if t["id"] == trade_id), None)
    if not trade:
        return "Not found", 404
    try:
        ccxt_sym = trade["ccxt_symbol"]
        # Cancel exchange SL and TP orders first
        cancel_order(ccxt_sym, trade.get("sl_order_id"))
        cancel_order(ccxt_sym, trade.get("tp_order_id"))
        # Market close
        close_order = close_position_at_market(ccxt_sym, trade["qty"])
        ticker     = exchange.fetch_ticker(trade["ccxt_symbol"])
        exit_price = ticker["last"]
        pnl_pct    = round((trade["entry"] - exit_price) / trade["entry"] * 100, 3)
        pnl_usd    = round(pnl_pct / 100 * trade["position_usd"], 2)
        result     = "MANUAL"

        trade.update({
            "status":     "MANUAL",
            "result":     result,
            "exit_price": round(exit_price, 6),
            "pnl_pct":    pnl_pct,
            "pnl_usd":    pnl_usd,
            "closed_at":  datetime.now().strftime("%Y-%m-%d %H:%M"),
        })
        state["open_trades"].remove(trade)
        if pnl_usd < 0:
            record_daily_loss(abs(pnl_usd))
        save_trades()
        log.info(f"MANUAL CLOSE: {trade['symbol']} | PnL:{pnl_pct}% (${pnl_usd})")
    except Exception as e:
        log.error(f"Manual close failed: {e}")
    return redirect("/")

@app.route("/comment/<trade_id>", methods=["POST"])
def save_comment(trade_id):
    """Save analyst comment/note against a trade (e.g. blacklist flag)."""
    trade = next((t for t in state["trades"] if t.get("id") == trade_id), None)
    if not trade:
        return jsonify({"error": "not found"}), 404
    data    = request.get_json(silent=True) or {}
    comment = data.get("comment", "").strip()
    if comment:
        trade["comment"] = comment
    elif "comment" in trade:
        del trade["comment"]
    save_trades()
    log.info(f"COMMENT: {trade.get('symbol')} [{trade_id}] → '{comment}'")
    return jsonify({"ok": True})

@app.route("/clear-ip-alert", methods=["POST"])
def clear_ip_alert():
    """Clear the IP alert banner and resume trading after whitelisting."""
    state["ip_alert"] = None
    if state["paused"] and "IP" in state.get("pause_reason", ""):
        state["paused"]       = False
        state["pause_reason"] = ""
        log.info("IP alert cleared — trading resumed")
    return redirect("/")

@app.route("/pause", methods=["POST"])
def pause():
    state["paused"]       = True
    state["pause_reason"] = "Manually paused by user"
    return redirect("/")

@app.route("/resume", methods=["POST"])
def resume():
    state["paused"]       = False
    state["pause_reason"] = ""
    return redirect("/")

@app.route("/mirror", methods=["POST"])
def mirror():
    """
    Called by paper trader the instant it opens a new trade.
    Executes the same position on live immediately — no scan delay.
    """
    data   = request.get_json(silent=True) or {}
    symbol = data.get("symbol")   # e.g. "SAHARA/USDT"
    if not symbol:
        return jsonify({"status": "error", "msg": "symbol required"}), 400

    coin = symbol.split("/")[0]
    log.info(f"🔄 MIRROR request: {coin}")

    if state["paused"]:
        log.info(f"  Mirror skipped — paused: {state['pause_reason']}")
        return jsonify({"status": "skipped", "msg": "live trader paused"}), 200

    if len(state["open_trades"]) >= MAX_CONCURRENT:
        log.info(f"  Mirror skipped — max concurrent ({MAX_CONCURRENT}) reached")
        return jsonify({"status": "skipped", "msg": "max concurrent reached"}), 200

    if coin in [t["symbol"] for t in state["open_trades"]]:
        log.info(f"  Mirror skipped — {coin} already open")
        return jsonify({"status": "skipped", "msg": f"{coin} already open"}), 200

    if coin_in_cooldown(coin):
        log.info(f"  Mirror skipped — {coin} in 24H cooldown")
        return jsonify({"status": "skipped", "msg": f"{coin} in cooldown"}), 200

    # Build signal using MFI/sizing from paper trader's data
    mfi_val      = data.get("mfi_peak", 80)
    mult, mlabel = get_size_multiplier(mfi_val)
    margin_usd   = BASE_TRADE_SIZE * mult
    notional     = margin_usd * LEVERAGE

    sig = {
        "signal_symbol": symbol,
        "mfi_peak":      mfi_val,
        "mult":          mult,
        "mult_label":    mlabel,
        "margin_usd":    margin_usd,
        "notional_usd":  notional,
        "peak_session":  "Peak 🟢 (Mon–Thu)" if is_peak_liquidity() else "Off-Peak 🟡 (Fri–Sun)",
        "entry_approx":  data.get("entry", 0),
        "sl_approx":     0,
        "tp_approx":     0,
    }

    # Execute in background so HTTP response returns immediately
    threading.Thread(target=open_live_trade, args=(sig,), daemon=True).start()
    log.info(f"  Mirror queued: {coin} MFI:{mfi_val} {mlabel} (${notional:.0f} notional)")
    return jsonify({"status": "ok", "msg": f"mirror queued for {coin}"}), 200

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

    gross_win  = sum(t.get("pnl_usd", 0) or 0 for t in wins + bes)
    gross_loss = sum(abs(t.get("pnl_usd", 0) or 0) for t in losses)
    pf = round(gross_win / gross_loss, 2) if gross_loss > 0 else None

    avg_win  = round(sum(t.get("pnl_pct", 0) or 0 for t in wins)   / len(wins),   2) if wins   else 0
    avg_loss = round(sum(t.get("pnl_pct", 0) or 0 for t in losses)  / len(losses), 2) if losses else 0

    # Coin-level repeat losses
    loss_coins = {}
    for t in losses:
        c = t.get("symbol", "?")
        loss_coins[c] = loss_coins.get(c, 0) + 1
    repeat_losers = [c for c, cnt in loss_coins.items() if cnt >= 2]

    recs = []
    if win_rate >= 70:
        recs.append(("green",  f"Win+BE rate {win_rate}% — strategy solid on live. Consider scaling size."))
    elif win_rate >= 50:
        recs.append(("yellow", f"Win+BE rate {win_rate}% — acceptable. Monitor closely before scaling."))
    else:
        recs.append(("red",    f"Win+BE rate {win_rate}% — below 50% on live. Do not scale capital yet."))

    if pf and pf >= 2.0:
        recs.append(("green",  f"Profit factor {pf} — strong risk/reward on real trades."))
    elif pf and pf < 1.0:
        recs.append(("red",    f"Profit factor {pf} — losing money overall. Check entry/SL alignment with paper trader."))

    if wins and losses and abs(avg_loss) > avg_win:
        recs.append(("yellow", f"Avg win {avg_win}% < avg loss {abs(avg_loss)}% — entry timing slippage may be hurting live vs paper."))
    elif wins:
        recs.append(("green",  f"Avg win {avg_win}% vs avg loss {abs(avg_loss)}% — good asymmetry on live."))

    if repeat_losers:
        recs.append(("red",    f"Repeat losing coins: {', '.join(repeat_losers)} — cooldown not catching these; review paper trader."))

    if len(closed) > milestone:
        recs.append(("yellow", f"Next milestone: {milestone + 3} trades — come back to review then."))

    return {"milestone": milestone, "total": n, "win_rate": win_rate, "pf": pf,
            "avg_win": avg_win, "avg_loss": avg_loss, "recs": recs}


@app.route("/")
def index():
    closed  = [t for t in state["trades"] if t["status"] not in ("OPEN",)]
    wins    = len([t for t in closed if t.get("result") == "WIN"])
    beplus  = len([t for t in closed if t.get("result") == "BE+"])
    losses  = len([t for t in closed if t.get("result") == "LOSS"])
    decided = wins + beplus + losses
    wr      = round((wins + beplus) / decided * 100, 1) if decided > 0 else 0

    gross_win  = sum(t.get("pnl_usd", 0) or 0 for t in closed if t.get("result") in ("WIN", "BE+"))
    gross_loss = sum(abs(t.get("pnl_usd", 0) or 0) for t in closed if t.get("result") == "LOSS")
    pf         = round(gross_win / gross_loss, 2) if gross_loss > 0 else (round(gross_win, 2) if gross_win > 0 else 0)

    realised_usd   = round(gross_win - gross_loss, 2)
    unrealised_usd = round(sum(t.get("unrealised_usd", 0) or 0 for t in state["open_trades"]), 2)

    raw_bal = get_account_balance()
    # Make accessible as object in template
    class Bal:
        pass
    bal = Bal()
    bal.usdt       = raw_bal["usdt"]
    bal.available  = raw_bal["available"]
    bal.unrealised = raw_bal["unrealised"]
    bal.total_usd  = raw_bal["total_usd"]
    bal.btc        = raw_bal["btc"]
    bal.btc_usd    = raw_bal["btc_usd"]

    insights = generate_insights(closed)

    return render_template_string(HTML,
        open_trades    = state["open_trades"],
        closed_trades  = closed,
        wins           = wins,
        beplus         = beplus,
        losses         = losses,
        win_rate       = wr,
        profit_factor  = pf,
        realised_usd   = realised_usd,
        unrealised_usd = unrealised_usd,
        insights       = insights,
        bal            = bal,
        max_concurrent = MAX_CONCURRENT,
        scan_count     = state["scan_count"],
        last_scan      = state["last_scan"],
        start_time     = state["start_time"],
        running        = state["running"],
        paused         = state["paused"],
        pause_reason   = state["pause_reason"],
        daily_loss     = round(state["daily_loss"], 2),
        daily_limit    = DAILY_LOSS_LIMIT,
        ip_alert       = state["ip_alert"],
    )

# ─────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────
if __name__ == "__main__":
    load_trades()

    # scan_thread disabled — live trader receives all signals via /mirror from paper_trader.
    # Running its own scan in parallel caused duplicate positions on the same coin.
    trail_thread = threading.Thread(target=trail_runner)
    trail_thread.daemon = True
    trail_thread.start()

    print("\n" + "═"*60)
    print("  MFI Coin Hunter — LIVE TRADER  v1.5")
    print("  ⚠️  REAL MONEY — Binance Portfolio Margin")
    print("  Open: 👉  http://localhost:8083")
    print(f"  Base margin:   ${BASE_TRADE_SIZE} per trade")
    print(f"  Leverage:      {LEVERAGE}×  (${BASE_TRADE_SIZE * LEVERAGE} notional)")
    print(f"  Max trades:    {MAX_CONCURRENT}")
    print(f"  Daily limit:   ${DAILY_LOSS_LIMIT} auto-pause")
    print(f"  Signals:       mirror-only (via paper_trader on :8081)")
    print(f"  Trail check:   every {TRAIL_INTERVAL_MIN} min")
    print("═"*60 + "\n")

    app.run(host="0.0.0.0", port=8083, debug=False)
