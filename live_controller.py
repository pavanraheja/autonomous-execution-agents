#!/usr/bin/env python3
"""
AlphaGrid Live Controller — Port 8095
══════════════════════════════════════
Central live trading controller for Binance Copy Trading portfolio.

Responsibilities:
  - Receives signals from paper bots via HTTP POST
  - Capital allocation per strategy (configurable)
  - Risk guardian: max positions, drawdown kill, coin conflicts
  - Real order execution on Binance Futures
  - SL/TP placement and trailing SL updates
  - Live portfolio state tracking
  - Dashboard for monitoring

Architecture:
  Paper bots (8082/8085/8086/8087/8089) → POST /signal → Controller decides → Execute or reject
  Controller also monitors open positions and manages trailing exits.

API Keys: Read from /opt/trader/.env.live (never hardcoded)
State: /opt/trader/state/live_portfolio.json
"""

import ccxt
import json
import os
import threading
import time
import logging
import requests as http_requests
from datetime import datetime, timedelta, timezone
from flask import Flask, render_template_string, request, jsonify
from pathlib import Path

# ─── TELEGRAM ────────────────────────────────────────────────────────────────
TG_TOKEN = "8685015829:AAHxVBhQ3tqC9btYyYRf9g0axqAZD1h7DVg"
TG_CHAT = "5917469586"

def tg_send(msg):
    try:
        http_requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"}, timeout=5
        )
    except:
        pass

# ─────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────
PORT = 8095

# Read API keys from env file
def load_keys():
    env = {}
    env_path = "/opt/trader/.env.live"
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k] = v
    return env.get("BINANCE_LIVE_API_KEY", ""), env.get("BINANCE_LIVE_SECRET", "")

API_KEY, API_SECRET = load_keys()

# ── Strategy Allocations ──────────────────────────────────────────────────────
# Each strategy gets a capital budget. Controller enforces limits.
# Adjust these from the dashboard or by editing this config.
STRATEGIES = {
    "alligator": {
        "enabled":        False,     # DISABLED live 2026-07-07: real PF 0.71 (-$5.27, n=13, SUI-carried). Paper flipped LONG-only to prove the PF-1.92 LONG sleeve toward n=30.
        "display_name":   "Alligator v2.4",
        "port":           8086,
        "allocation":     100.0,     # raised 2026-05-11: signals come at $30/trade; $30×3 conc + $10 buffer (was $25 hardcoded, rejected 23/41 signals) × max_conc 3 = $21 + small buffer (was $300)
        "per_trade":      4.0,
        "per_trade_pct":  0.15,      # 2026-06-18: %-based sizing — 0.15% of equity (~$4.2 at $2.8k), auto-scales
        "leverage":       5,
        "max_concurrent": 3,
        "coins":          ["XRP","ADA","SUI","DOGE","BNB","HYPE","ORDI","ENJ"],  # XAU removed 2026-04-23: TradFi perp, blocked on Binance UAE
    },
    "htf_mfi": {
        "enabled":        True,      # PROMOTED 2026-06-18 to live-mirror (deep-review flagship candidate: PF 2.0 / WR 75% paper n=8). Micro size until live n>=30.
        "display_name":   "HTF MFI 8085 v2.1",
        "port":           8085,
        "allocation":     70.0,      # micro: ~$5.6/trade × 3 conc + buffer
        "per_trade":      5.0,       # fallback only; per_trade_pct overrides
        "per_trade_pct":  0.5,      # 0.20% of equity (~$5.6 at $2.8k)
        "leverage":       5,
        "max_concurrent": 3,
        "coins":          [],        # no coin gate (bot SYMBOLS govern); informational
    },
    "mfi_monitor": {
        "enabled":        False,      # not yet — need 15 trades on new config
        "display_name":   "MFI Monitor v2",
        "port":           8087,
        "allocation":     35.0,      # right-sized: $15 × 2 = $30 + buffer (was $150)
        "per_trade":      15.0,
        "leverage":       5,
        "max_concurrent": 2,
        "coins":          [],         # broad scan — any coin
    },
    "cascade": {
        "enabled":        False,      # not yet — need 10 paper trades
        "display_name":   "Cascade SHORT v1.5",
        "port":           8082,
        "allocation":     45.0,      # right-sized: $20 × 2 = $40 + buffer (was $100)
        "per_trade":      20.0,
        "leverage":       5,
        "max_concurrent": 2,
        "coins":          ["XRP","ARB","BTC","OP","SUI","ONT","WIF","TRUMP","ETC","CRV"],
    },
    "liq_zone": {
        "enabled":        True,      # RE-ENABLED 2026-07-12 (Pavan OK): pre-committed trip-wire met — fresh paper n=10 since 06-18 disable, WR 90%, PF 15.3 (cluster-corr; honest Mode-B read PF 2.60 since 05-30). Was disabled 06-18 on 0/7 real fills.
        "display_name":   "Liq Zone BTC v1",
        "port":           8089,
        "allocation":     240.0,     # $15 × 8 BEAR coins = $120 (worst-case basket); BULL basket is 3 coins = $45, fits same alloc
        "per_trade":      15.0,      # fallback only; per_trade_pct overrides
        "per_trade_pct":  1.0,       # 2026-07-12: %-based sizing (~$14 at $2.8k equity); 8-cluster worst case ~4% — inside the -5% daily kill switch
        "leverage":       5,
        "max_concurrent": 8,
        "coins":          ["XRP","UNI","DOT","SUI","AVAX","ADA","DOGE","ETH","BTC","LTC"],  # +BTC+LTC 2026-05-14 for BULL_COINS basket when REGIME_OVERRIDE=OFF
    },
    "tg1": {
        "enabled":        False,
        "exit_logic":    "variant_b",  # 50% partial @ 2R + BE + 3xATR trail     # paper-validated; flipped True at Pavan GO LIVE
        "display_name":   "TG-1 Institutional Candle (Bangalore signal)",
        "port":           None,      # signal source = Bangalore paper bot via POST /signal
        "allocation":     200.0,    # $200 budget Stage 0
        "per_trade":      5.0,      # Stage 0 size
        "leverage":       5,
        "max_concurrent": 3,
        "coins":          ["BTC","ETH","SOL","BNB","AVAX","LINK","ARB","OP","ATOM","NEAR","SUI","INJ","RUNE","TIA","SEI","DOGE","XRP","LTC","BCH"],
    },
    "tg2": {
        "enabled":        False,
        "exit_logic":    "variant_b",     # rare in current regime; defer until 10+ paper trades
        "display_name":   "TG-2 FVG Retest sister (Bangalore signal)",
        "port":           None,
        "allocation":     100.0,
        "per_trade":      2.5,      # 0.5x weight
        "leverage":       5,
        "max_concurrent": 2,
        "coins":          ["BTC","ETH","SOL","BNB","AVAX","LINK","ARB","OP","ATOM","NEAR","SUI","INJ","RUNE","TIA","SEI","DOGE","XRP","LTC","BCH"],
    },
    "qr_lm": {
        "enabled":        False,
        "exit_logic":    "simple",  # TP at zone, no partial/trail     # paper-validated; flipped True at Pavan GO LIVE (target ~May 5)
        "display_name":   "QR-LM Liq Zone x MFI (Bangalore signal)",
        "port":           None,
        "allocation":     200.0,
        "per_trade":      5.0,      # Stage 0 size
        "leverage":       5,
        "max_concurrent": 3,
        "coins":          ["BTC","ETH","SOL","BNB","AVAX","LINK","ARB","SUI","XRP","DOGE"],
    },
    "hl_mirror": {
        "enabled":        True,    # Pavan GO 2026-06-11 (2-whale validated cohort, 14d paper proof)
        "exit_logic":    "simple",  # real exit = bridge /close when whale closes; SL = catastrophic backstop only
        "display_name":   "HL Whale Mirror — validated cohort (Bangalore bridge)",
        "port":           None,    # signal source = hl_live_bridge.py on Bangalore
        "allocation":     100.0,   # worst case 12 slots x $5 margin + min-notional resize headroom
        "per_trade":      5.0,
        "leverage":       3,
        "max_concurrent": 12,      # bridge enforces per-whale split 8 (7c9309) + 4 (fc667a)
        "coins":          [],      # mirror follows whale coins; informational only (check_risk has no coin gate)
    },
}

# ── Risk Guardian ─────────────────────────────────────────────────────────────
# 2026-04-29: ACCOUNT_CAPITAL/RESERVE now sourced from /opt/trader/state/capital_pool.json
# (single source of truth across all live services). Falls back to hardcoded defaults if
# the pool file is unreachable so the bot can always start.
def _load_binance_capital():
    import sys as _sys
    _sys.path.insert(0, "/opt/trader/scripts")
    try:
        from capital_pool import get_platform
        # 2026-09-28: pool v2 (05-26) split "binance" into named accounts; asking for "binance"
        # silently fell back to DEFAULTS $600, which disabled the -10% equity drawdown kill.
        bn = get_platform("binance_alphagrid")
        src = bn.get("_source_path") or "(in-memory)"
        return float(bn["total"]), float(bn["reserve"]), src
    except Exception as _ex:
        print(f"[capital_pool] WARN: pool unavailable ({_ex}); using hardcoded defaults")
        return 600.0, 100.0, "HARDCODED_FALLBACK"

ACCOUNT_CAPITAL, RESERVE, _CAPITAL_SRC = _load_binance_capital()
DEPLOYABLE           = ACCOUNT_CAPITAL - RESERVE   # auto-computed
print(f"[capital_pool] binance capital: total=${ACCOUNT_CAPITAL} reserve=${RESERVE} "
      f"deployable=${DEPLOYABLE} source={_CAPITAL_SRC}")
MAX_CONCURRENT_TOTAL = 5           # max open positions across ALL strategies
MAX_SINGLE_MARGIN    = 30.0        # raised 2026-05-02 from 25 → 30 to unblock alligator probation (signals arrive at $30 margin; was hard-rejecting all post-SHRINK signals)

# Per-coin min notional (Binance USDT-M futures, queried 2026-05-03)
# Signals failing this get hard-rejected instead of placing & failing on Binance side.
COIN_MIN_NOTIONAL = {
    "BTC":  50.0,  "ETH":  20.0,  "LINK": 20.0,  "LTC": 20.0,  "BCH": 20.0,
    # All other coins: $5 (default below)
}
COIN_MIN_NOTIONAL_DEFAULT = 5.0
AUTO_RESIZE_ON_CAP   = True        # added 2026-05-02: resize margin down to MAX_SINGLE_MARGIN if signal exceeds cap (instead of hard-reject)
AUTO_RESIZE_UP_TO_MIN = True       # added 2026-05-13: bump margin UP to clear min notional (don't miss live trades from sizing)
RESIZE_UP_BUFFER     = 1.0         # extra $1 margin above the bare minimum, for price-move safety
RESIZE_MAX_FACTOR    = 2.0         # only auto-resize if margin <= cap * factor; otherwise reject as likely config error
DRAWDOWN_KILL_PCT    = 10.0        # close all if account drops this % (tightened from 15% — current losses too small to ever hit 15%)
STRATEGY_LOSS_CAP    = 50.0        # pause strategy if it loses this % of its allocation (raised from 5% — at right-sized allocations, 5% trips on first loss; 50% gives ~5 trades headroom)
STRATEGY_PAUSE_HOURS = 24          # how long to pause after hitting loss cap

# Sanity check: enabled allocations must fit within DEPLOYABLE
def _validate_allocations():
    enabled_total = sum(s["allocation"] for s in STRATEGIES.values() if s["enabled"])
    all_total     = sum(s["allocation"] for s in STRATEGIES.values())
    if enabled_total > DEPLOYABLE:
        raise RuntimeError(
            f"Enabled strategy allocations sum to ${enabled_total} > DEPLOYABLE ${DEPLOYABLE}. "
            f"Either reduce allocations or increase ACCOUNT_CAPITAL."
        )
    return enabled_total, all_total

# ── Paths ─────────────────────────────────────────────────────────────────────
STATE_FILE = Path("/opt/trader/state/live_portfolio.json")

# ─── CROSS-HOST GLOBAL HALT (2026-05-03) ────────────────────────────────────
# AlphaGrid (Contabo) polls Bangalore master dashboard for halt state.
# Cached 30s to avoid hammering. Fail-open on network error (don't halt on transient).
GLOBAL_HALT_URL = "http://139.59.68.236:8110/api/global_halt"
_GH_CACHE = {"ts": 0.0, "halted": False, "alerted": False}
_GH_TTL_SEC = 30


def _is_global_halt_active():
    now = time.time()
    if now - _GH_CACHE["ts"] < _GH_TTL_SEC:
        return _GH_CACHE["halted"]
    try:
        r = http_requests.get(GLOBAL_HALT_URL, timeout=3)
        halted = bool(r.json().get("halted", False))
        _GH_CACHE["ts"] = now
        # Edge-trigger TG alert on first detection
        if halted and not _GH_CACHE["alerted"]:
            try:
                tg_send("🛑 <b>AlphaGrid</b>: cross-host GLOBAL_HALT detected on Bangalore. Halting new entries.")
            except Exception:
                pass
            _GH_CACHE["alerted"] = True
        elif not halted and _GH_CACHE["alerted"]:
            _GH_CACHE["alerted"] = False
        _GH_CACHE["halted"] = halted
        return halted
    except Exception:
        # Fail-open: keep last-known status (default False) on network blip
        return _GH_CACHE["halted"]
LOG_FILE   = Path("/opt/trader/logs/live_controller.log")

# ── Logging ───────────────────────────────────────────────────────────────────
os.makedirs(LOG_FILE.parent, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger("AlphaGrid")

# ── Exchange ──────────────────────────────────────────────────────────────────
exchange = ccxt.binanceusdm({
    "apiKey":          API_KEY,
    "secret":          API_SECRET,
    "enableRateLimit": True,
    "timeout":         30000,
})

app = Flask(__name__)


# ─────────────────────────────────────────
# STATE
# ─────────────────────────────────────────
state = {
    "positions":       [],          # open live positions
    "closed":          [],          # closed trades (recent 100)
    "strategy_pnl":    {},          # cumulative PnL per strategy
    "strategy_paused": {},          # {strategy: pause_until_ts}
    "kill_switch":     False,       # True = all trading halted
    "started_at":      datetime.now(timezone.utc).isoformat(),
    "total_trades":    0,
    "total_pnl":       0.0,
    "last_signal":     None,
    "last_rejection":  None,
    "signals_received":0,
    "signals_executed":0,
    "signals_rejected":0,
}


def save_state():
    os.makedirs(STATE_FILE.parent, exist_ok=True)
    _tmp = str(STATE_FILE) + ".tmp"
    with open(_tmp, "w") as f:
        json.dump(state, f, indent=2, default=str)
        f.flush(); os.fsync(f.fileno())
    os.replace(_tmp, STATE_FILE)


def load_state():
    global state
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                saved = json.load(f)
            # Merge — keep new keys, update saved ones
            for k in saved:
                if k in state:
                    state[k] = saved[k]
            log.info(f"State loaded: {len(state['positions'])} open, {len(state['closed'])} closed")
        except Exception as e:
            log.warning(f"Failed to load state: {e}")


# ─────────────────────────────────────────
# RISK GUARDIAN
# ─────────────────────────────────────────
def check_risk(strategy, coin, direction, margin):
    """
    Returns (allowed: bool, reason: str).
    Checks all risk rules before allowing a trade.
    """
    cfg = STRATEGIES.get(strategy)
    if not cfg:
        return False, f"Unknown strategy: {strategy}"

    if not cfg["enabled"]:
        return False, f"Strategy '{strategy}' is disabled"

    if state["kill_switch"]:
        return False, "Kill switch active — all trading halted"

    # 2026-05-03 — Cross-host global halt (Bangalore-side flag). 30s cache.
    if _is_global_halt_active():
        return False, "Global halt flag set on Bangalore — cross-strategy halt active"

    # Strategy paused?
    pause_until = state["strategy_paused"].get(strategy)
    if pause_until:
        if datetime.now(timezone.utc).isoformat() < pause_until:
            return False, f"Strategy paused until {pause_until}"
        else:
            del state["strategy_paused"][strategy]

    # Max concurrent total
    if len(state["positions"]) >= MAX_CONCURRENT_TOTAL:
        return False, f"Max concurrent positions reached ({MAX_CONCURRENT_TOTAL})"

    # Max concurrent per strategy
    strat_positions = [p for p in state["positions"] if p["strategy"] == strategy]
    if len(strat_positions) >= cfg["max_concurrent"]:
        return False, f"Strategy max concurrent reached ({cfg['max_concurrent']})"

    # Margin cap
    if margin > MAX_SINGLE_MARGIN:
        return False, f"Margin ${margin} exceeds max ${MAX_SINGLE_MARGIN}"

    # Min notional check — Binance USDT-M futures rejects orders below per-coin min
    coin_min = COIN_MIN_NOTIONAL.get(coin, COIN_MIN_NOTIONAL_DEFAULT)
    notional = margin * cfg["leverage"]
    if notional < coin_min:
        return False, (f"Notional ${notional:.2f} (margin ${margin} × {cfg['leverage']}x) "
                       f"below {coin} min ${coin_min}. Skip or raise per_trade.")

    # Capital allocation check
    used = sum(p["margin"] for p in state["positions"] if p["strategy"] == strategy)
    if used + margin > cfg["allocation"]:
        return False, f"Capital exhausted: ${used}+${margin} > ${cfg['allocation']} allocated"

    # Coin conflict: same coin, opposite direction from another strategy
    for p in state["positions"]:
        if p["coin"] == coin and p["direction"] != direction:
            return False, f"Coin conflict: {coin} already {p['direction']} by {p['strategy']}"

    # Duplicate: same coin+direction+strategy already open
    for p in state["positions"]:
        if p["coin"] == coin and p["direction"] == direction and p["strategy"] == strategy:
            return False, f"Duplicate: {coin} {direction} already open for {strategy}"

    # Account drawdown check
    if state["total_pnl"] <= -(ACCOUNT_CAPITAL * DRAWDOWN_KILL_PCT / 100):
        state["kill_switch"] = True
        save_state()
        return False, f"DRAWDOWN KILL: PnL ${state['total_pnl']:.2f} breached -{DRAWDOWN_KILL_PCT}%"

    # Per-strategy loss cap
    strat_pnl = state["strategy_pnl"].get(strategy, 0.0)
    loss_limit = -(cfg["allocation"] * STRATEGY_LOSS_CAP / 100)
    if strat_pnl <= loss_limit:
        pause_until = (datetime.now(timezone.utc) + timedelta(hours=STRATEGY_PAUSE_HOURS)).isoformat()
        state["strategy_paused"][strategy] = pause_until
        save_state()
        return False, f"Strategy loss cap: {strategy} PnL ${strat_pnl:.2f} breached ${loss_limit:.2f}. Paused {STRATEGY_PAUSE_HOURS}h."

    return True, "OK"


# ─────────────────────────────────────────
# ORDER EXECUTOR
# ─────────────────────────────────────────
import time as _time
ALGO_VERIFY_WAITS = (0.3, 0.5, 0.8, 1.2, 1.5)   # ~4.3s total before giving up

def place_algo_stop(symbol, side, trigger_price, order_type):
    """
    Place a conditional SL/TP via the Binance Algo Order API and VERIFY it exists.
    FIX 2026-07-25: since Binance's mandatory 2025-12-09 migration, conditional orders
    (STOP_MARKET/TAKE_PROFIT_MARKET) MUST go through POST /fapi/v1/algoOrder, not the old
    /fapi/v1/order (which returns -4120, or silently returns a phantom-looking algoId).
    The old code logged "SL SET" on any response without verifying -> positions ran
    unprotected while state claimed a stop. Now we place AND query-back before trusting it.
    order_type: "STOP_MARKET" or "TAKE_PROFIT_MARKET".  Returns algoId (str) or None.
    """
    market_id = exchange.market(symbol)["id"]  # "XRP/USDT:USDT" -> "XRPUSDT"
    trig = exchange.price_to_precision(symbol, trigger_price)
    try:
        r = exchange.fapiPrivatePostAlgoOrder({
            "algoType": "CONDITIONAL", "symbol": market_id, "side": side.upper(),
            "type": order_type, "triggerPrice": trig,
            "closePosition": "true", "workingType": "MARK_PRICE",
        })
        algo_id = r.get("algoId")
        if not algo_id:
            log.error(f"  ALGO {order_type} no algoId in response: {r}")
            return None
    except Exception as e:
        log.error(f"  ALGO {order_type} FAILED: {e}")
        return None
    # Verify it actually rests as NEW. 2026-09-28: Binance registers algo orders
    # asynchronously — querying ~170ms after placement returns -2013 "Order does not
    # exist", which made EVERY entry since 07-30 emergency-close. Poll with backoff,
    # then fall back to the open-algo-orders list, before declaring it missing.
    for attempt, wait in enumerate(ALGO_VERIFY_WAITS):
        _time.sleep(wait)
        try:
            q = exchange.fapiPrivateGetAlgoOrder({"algoId": algo_id})
        except Exception as e:
            if "-2013" in str(e):
                continue                      # not registered yet — wait and retry
            log.error(f"  ALGO {order_type} verify error: {e}")
            continue
        status = q.get("algoStatus") or q.get("status")
        if status in ("NEW", "WORKING"):
            if attempt:
                log.info(f"  ALGO {order_type} verified on attempt {attempt + 1}")
            return algo_id
        log.error(f"  ALGO {order_type} placed but status={status} (algoId={algo_id})")
        return None
    try:
        rows = exchange.fapiPrivateGetOpenAlgoOrders({"symbol": market_id})
        rows = rows if isinstance(rows, list) else rows.get("orders", [])
        if any(str(o.get("algoId")) == str(algo_id) for o in rows):
            log.info(f"  ALGO {order_type} found in openAlgoOrders after query retries")
            return algo_id
    except Exception as e:
        log.error(f"  ALGO {order_type} openAlgoOrders check failed: {e}")
    log.error(f"  ALGO {order_type} never confirmed (algoId={algo_id}) — cancelling it")
    try:
        exchange.fapiPrivateDeleteAlgoOrder({"algoId": algo_id})
    except Exception:
        pass
    return None


import time as _t_coid, re as _re_coid
STRAT_CODE = {"liq_zone":"LZ","htf_mfi":"HM","mfi_long":"ML","dvol":"DV","paxg":"PX","xag":"XG"}
def _coid(strategy, coin, suffix=""):
    code = STRAT_CODE.get(strategy, "XX")
    base = _re_coid.sub(r"[^A-Z0-9]", "", str(coin).split("/")[0].upper())[:6]
    cid = f"AG-{code}-{base}-{int(_t_coid.time()*1000)%10000000}{suffix}"
    return cid[:36]


def execute_entry(strategy, coin, direction, margin, sl_price, tp_price=None, atr_at_signal=None):
    """
    Place a market order + SL (+ optional TP) on Binance.
    Returns position dict or None on failure.
    """
    cfg = STRATEGIES[strategy]
    symbol = f"{coin}/USDT:USDT"
    notional = margin * cfg["leverage"]
    side = "sell" if direction == "SHORT" else "buy"

    try:
        # Get current price for quantity calculation
        ticker = exchange.fetch_ticker(symbol)
        price = ticker["last"]
        qty = round(notional / price, 6)

        # Adjust quantity precision per symbol
        market = exchange.market(symbol)
        qty = exchange.amount_to_precision(symbol, qty)

        log.info(f"EXECUTING: {strategy} {coin} {direction} | margin=${margin} notional=${notional} qty={qty} @ ~{price}")

        # Stage 1: Market order
        try:
            order = exchange.create_order(
                symbol=symbol,
                type="market",
                side=side,
                amount=float(qty),
                params={"newClientOrderId": _coid(strategy, coin)},
            )
            # FIX 2026-07-15: Binance returns average=None on immediate market-order response;
            # dict.get(k, fallback) does NOT fall back on existing-but-None keys -> float(None)
            # crashed AFTER the fill -> orphaned unmanaged position (first htf_mfi live fire, SUI 07-14).
            entry_price = float(order.get("average") or order.get("price") or price)
            order_id = order.get("id", "unknown")
            log.info(f"  ENTRY FILLED: {order_id} @ {entry_price}")
        except Exception as e:
            log.error(f"  ENTRY FAILED: {e}")
            return None

        # Stage 2: SL via VERIFIED algo order (separate try/except — critical rule)
        sl_side = "buy" if direction == "SHORT" else "sell"
        sl_order_id = place_algo_stop(symbol, sl_side, sl_price, "STOP_MARKET")
        if sl_order_id:
            log.info(f"  SL SET (verified algo): {sl_order_id} @ {sl_price}")
        else:
            # placement failed OR did not verify → position is unprotected; close it.
            # (Software SL watcher is a backstop, but entry policy = never hold unverified.)
            log.error(f"  SL UNVERIFIED — POSITION UNPROTECTED, closing immediately")
            tg_send(f"🚨 <b>[AG] SL UNVERIFIED</b>\n{coin} {direction} — closing to stay safe")
            try:
                exchange.create_order(symbol=symbol, type="market", side=sl_side, amount=float(qty),
                                      params={"reduceOnly": True})
                log.info(f"  EMERGENCY CLOSE completed")
                tg_send(f"🔒 <b>[AG] Emergency close done</b> — {coin} safe")
            except Exception as e2:
                log.error(f"  EMERGENCY CLOSE ALSO FAILED: {e2} — MANUAL INTERVENTION NEEDED")
                tg_send(f"🚨🚨🚨 <b>[AG] EMERGENCY CLOSE FAILED</b>\n{coin} {direction} UNPROTECTED\nMANUAL INTERVENTION NEEDED\n{e2}")
            return None

        # Stage 3: TP via verified algo order (optional)
        tp_order_id = None
        if tp_price:
            tp_side = "buy" if direction == "SHORT" else "sell"
            tp_order_id = place_algo_stop(symbol, tp_side, tp_price, "TAKE_PROFIT_MARKET")
            if tp_order_id:
                log.info(f"  TP SET (verified algo): {tp_order_id} @ {tp_price}")
            else:
                log.warning(f"  TP unverified (non-critical) @ {tp_price}")

        # Build position record
        position = {
            "id":           f"{strategy}_{coin}_{datetime.now(timezone.utc).strftime('%m%d_%H%M%S')}",
            "strategy":     strategy,
            "coin":         coin,
            "symbol":       symbol,
            "direction":    direction,
            "entry_price":  entry_price,
            "qty":          float(qty),
            "margin":       margin,
            "notional":     notional,
            "sl_price":     sl_price,
            "tp_price":     tp_price,
            "order_id":     order_id,
            "sl_order_id":  sl_order_id,
            "tp_order_id":  tp_order_id,
            "opened_at":    datetime.now(timezone.utc).isoformat(),
            "status":       "OPEN",
            "pnl":          0.0,
            # Variant B exit state (only meaningful when exit_logic="variant_b")
            "exit_logic":   cfg.get("exit_logic", "simple"),
            "atr_at_signal": atr_at_signal,
            "qty_remaining": float(qty),  # decremented after partial
            "partial_taken": False,
            "be_active":     False,
            "peak_price":    entry_price,  # peak in favorable direction (for trail)
            "target_2R":     (entry_price + 2 * abs(entry_price - sl_price)
                              if direction == "LONG"
                              else entry_price - 2 * abs(entry_price - sl_price)),
            "peak_pnl":     0.0,
            "trail_stage":  0,
        }

        state["positions"].append(position)
        state["total_trades"] += 1
        state["signals_executed"] += 1
        save_state()

        log.info(f"  POSITION OPENED: {position['id']}")
        tg_send(
            f"📈 <b>[AG] {direction} {coin}</b>\n"
            f"Strategy: {strategy}\n"
            f"Entry: ${entry_price:.4f} | Margin: ${margin}\n"
            f"SL: ${sl_price} | Qty: {qty}\n"
            f"Total PnL: ${state['total_pnl']:+.2f}"
        )
        return position

    except Exception as e:
        log.error(f"EXECUTION ERROR: {e}")
        tg_send(f"🚨 <b>[AG] EXEC FAILED</b>\n{strategy} {coin} {direction}\n{e}")
        return None


def execute_close(position, reason="manual"):
    """Close a live position."""
    symbol = position["symbol"]
    direction = position["direction"]
    qty = position["qty"]
    close_side = "buy" if direction == "SHORT" else "sell"

    try:
        order = exchange.create_order(
            symbol=symbol,
            type="market",
            side=close_side,
            amount=qty,
            params={"reduceOnly": True, "newClientOrderId": _coid(position.get("strategy","manual"), position.get("symbol","X/USDT").split("/")[0], "-X")},
        )
        exit_price = float(order.get("average", order.get("price", 0)))

        # Calculate PnL
        if direction == "SHORT":
            pnl_pct = (position["entry_price"] - exit_price) / position["entry_price"] * 100
        else:
            pnl_pct = (exit_price - position["entry_price"]) / position["entry_price"] * 100

        pnl_usd = round(pnl_pct / 100 * position["notional"], 2)

        position["exit_price"] = exit_price
        position["pnl_pct"] = round(pnl_pct, 4)
        position["pnl_usd"] = pnl_usd
        position["closed_at"] = datetime.now(timezone.utc).isoformat()
        position["exit_reason"] = reason
        position["status"] = "CLOSED"

        # Cancel SL/TP orders
        for oid_key in ["sl_order_id", "tp_order_id"]:
            oid = position.get(oid_key)
            if oid:
                try:
                    exchange.cancel_order(oid, symbol)
                except:
                    pass

        # Move to closed
        state["positions"] = [p for p in state["positions"] if p["id"] != position["id"]]
        state["closed"].append(position)
        if len(state["closed"]) > 100:
            state["closed"] = state["closed"][-100:]

        # Update PnL tracking
        state["total_pnl"] = round(state["total_pnl"] + pnl_usd, 2)
        strat = position["strategy"]
        state["strategy_pnl"][strat] = round(state["strategy_pnl"].get(strat, 0) + pnl_usd, 2)

        save_state()
        log.info(f"CLOSED: {position['id']} | {reason} | PnL: {pnl_pct:+.2f}% (${pnl_usd:+.2f})")
        emoji = "✅" if pnl_usd >= 0 else "❌"
        tg_send(
            f"{emoji} <b>[AG] CLOSED {position['coin']}</b>\n"
            f"Strategy: {position['strategy']} | {position['direction']}\n"
            f"Entry: ${position['entry_price']:.4f} → Exit: ${exit_price:.4f}\n"
            f"PnL: {pnl_pct:+.2f}% (${pnl_usd:+.2f})\n"
            f"Reason: {reason}\n"
            f"Total PnL: ${state['total_pnl']:+.2f}"
        )
        return True

    except Exception as e:
        log.error(f"CLOSE ERROR for {position['id']}: {e}")
        tg_send(f"🚨 <b>[AG] CLOSE FAILED</b>\n{position['id']}\n{e}")
        return False


def update_sl(position, new_sl_price):
    """Update the SL order for a position (trailing)."""
    symbol = position["symbol"]
    old_sl_id = position.get("sl_order_id")

    try:
        # Cancel old algo SL (FIX 2026-07-25: algo orders cancel via DELETE /fapi/v1/algoOrder,
        # not the regular cancel_order endpoint)
        if old_sl_id:
            try:
                exchange.fapiPrivateDeleteAlgoOrder({"algoId": old_sl_id})
            except Exception:
                pass

        # Place new SL via verified algo order (same path as entry)
        sl_side = "buy" if position["direction"] == "SHORT" else "sell"
        new_id = place_algo_stop(symbol, sl_side, new_sl_price, "STOP_MARKET")
        if not new_id:
            log.error(f"  SL UPDATE UNVERIFIED: {position['id']} → {new_sl_price} (old SL cancelled!)")
            return False
        position["sl_price"] = new_sl_price
        position["sl_order_id"] = new_id
        save_state()
        log.info(f"  SL UPDATED (verified algo): {position['id']} → {new_sl_price}")
        return True
    except Exception as e:
        log.error(f"  SL UPDATE FAILED: {position['id']}: {e}")
        return False


# ─────────────────────────────────────────
# POSITION MONITOR (hardened for live)
# ─────────────────────────────────────────
MONITOR_INTERVAL     = 30    # seconds between checks (live = aggressive)
RECONCILE_INTERVAL   = 300   # full exchange reconciliation every 5 min
HEARTBEAT_TIMEOUT    = 3600  # alert if no signal from any bot in 1 hour
STALE_POSITION_HOURS = 48    # flag positions open > 48 hours

_last_reconcile = 0
_last_signal_ts = {}   # {strategy: timestamp}



def manage_variant_b(pos: dict, current_price: float):
    """Apply Variant B exit logic to one position.
    Returns True if any change was made (partial taken or SL updated).
    """
    if pos.get("exit_logic") != "variant_b":
        return False
    if pos.get("status") != "OPEN":
        return False

    direction = pos["direction"]
    is_long = direction == "LONG"

    # Update peak in favorable direction
    if is_long and current_price > pos.get("peak_price", pos["entry_price"]):
        pos["peak_price"] = current_price
    elif not is_long and current_price < pos.get("peak_price", pos["entry_price"]):
        pos["peak_price"] = current_price

    # Step 1: partial close at 2R
    if not pos.get("partial_taken"):
        target_2R = pos.get("target_2R")
        hit_target = (is_long and current_price >= target_2R) or \
                     (not is_long and current_price <= target_2R)
        if hit_target:
            try:
                close_side = "buy" if not is_long else "sell"
                partial_qty_raw = pos["qty"] * 0.5
                partial_qty = exchange.amount_to_precision(pos["symbol"], partial_qty_raw)
                order = exchange.create_order(
                    symbol=pos["symbol"], type="market", side=close_side,
                    amount=float(partial_qty),
                    params={"reduceOnly": True},
                )
                fill_price = float(order.get("average", order.get("price", current_price)))
                # Track partial PnL
                if is_long:
                    partial_pnl = (fill_price - pos["entry_price"]) * float(partial_qty)
                else:
                    partial_pnl = (pos["entry_price"] - fill_price) * float(partial_qty)
                pos["partial_taken"] = True
                pos["partial_fill_price"] = fill_price
                pos["partial_pnl_usd"] = round(partial_pnl, 4)
                pos["qty_remaining"] = pos["qty"] - float(partial_qty)
                # Move SL to BE (entry price)
                update_sl(pos, pos["entry_price"])
                pos["be_active"] = True
                log.info(f"  PARTIAL @ 2R: {pos['id']} closed {partial_qty} @ {fill_price}, SL→BE")
                tg_send(f"💰 <b>[AG] PARTIAL 2R</b> {pos['coin']} {direction}\n"
                        f"closed {partial_qty} @ {fill_price}\n"
                        f"partial PnL: ${partial_pnl:+.2f}\n"
                        f"SL moved to BE (${pos['entry_price']:.4f})")
                return True
            except Exception as e:
                log.error(f"  PARTIAL FAILED: {pos['id']}: {e}")
                tg_send(f"⚠️ <b>[AG] PARTIAL FAILED</b> {pos['coin']}: {e}")
                return False

    # Step 2: chandelier ATR trail (after partial)
    if pos.get("partial_taken") and pos.get("atr_at_signal"):
        atr = float(pos["atr_at_signal"])
        peak = pos.get("peak_price", pos["entry_price"])
        if is_long:
            new_trail = peak - 3 * atr
            cur_sl = pos.get("sl_price", 0)
            if new_trail > cur_sl:
                if update_sl(pos, new_trail):
                    log.info(f"  TRAIL UPDATED: {pos['id']} SL → {new_trail:.4f} (peak {peak}, ATR {atr})")
                    return True
        else:
            new_trail = peak + 3 * atr
            cur_sl = pos.get("sl_price", float("inf"))
            if new_trail < cur_sl:
                if update_sl(pos, new_trail):
                    log.info(f"  TRAIL UPDATED: {pos['id']} SL → {new_trail:.4f} (peak {peak}, ATR {atr})")
                    return True

    return False


def monitor_positions():
    """
    Hardened live monitor — runs every 30s.

    Safety layers:
    1. Price tracking + unrealised PnL on every tick
    2. Exchange reconciliation — detect if SL/TP filled by Binance
    3. Orphan detection — positions on exchange not in our state
    4. Stale position alert — open > 48h without trail movement
    5. Account drawdown check — kill switch if breached
    6. Heartbeat — alert if paper bot stops sending signals
    """
    global _last_reconcile

    while True:
        try:
            now = time.time()

            # ── 1. Update unrealised PnL for all positions ──────────────
            if state["positions"]:
                for pos in list(state["positions"]):
                    try:
                        ticker = exchange.fetch_ticker(pos["symbol"])
                        price = ticker["last"]

                        if pos["direction"] == "SHORT":
                            pnl_pct = (pos["entry_price"] - price) / pos["entry_price"] * 100
                        else:
                            pnl_pct = (price - pos["entry_price"]) / pos["entry_price"] * 100

                        pos["pnl"] = round(pnl_pct, 4)
                        pos["current_price"] = price
                        pos["last_checked"] = datetime.now(timezone.utc).isoformat()

                        if pnl_pct > pos.get("peak_pnl", 0):
                            pos["peak_pnl"] = round(pnl_pct, 4)

                        # Variant B exit management (TG-1, TG-2)
                        try:
                            manage_variant_b(pos, price)
                        except Exception as e:
                            log.warning(f"manage_variant_b error {pos.get('id','?')}: {e}")

                        # ── Stale position warning ──────────────────────
                        opened = pos.get("opened_at", "")
                        if opened:
                            try:
                                age_hours = (datetime.now(timezone.utc) - datetime.fromisoformat(opened)).total_seconds() / 3600
                                if age_hours > STALE_POSITION_HOURS:
                                    pos["stale_warning"] = f"Open {age_hours:.0f}h — check manually"
                                    if not pos.get("stale_logged"):
                                        log.warning(f"STALE: {pos['id']} open {age_hours:.0f}h — review needed")
                                        pos["stale_logged"] = True
                            except:
                                pass

                        time.sleep(0.3)
                    except Exception as e:
                        log.warning(f"Monitor price error {pos.get('id','?')}: {e}")

                save_state()

            # ── 2. Exchange reconciliation (every 5 min) ────────────────
            if now - _last_reconcile > RECONCILE_INTERVAL:
                _last_reconcile = now
                try:
                    reconcile_with_exchange()
                except Exception as e:
                    log.warning(f"Reconciliation error: {e}")

            # ── 3. Account drawdown check ───────────────────────────────
            try:
                balance = exchange.fetch_balance()
                usdt_total = float(balance.get("USDT", {}).get("total", ACCOUNT_CAPITAL))
                unrealised = float(balance.get("USDT", {}).get("used", 0))
                equity = usdt_total
                drawdown_pct = (ACCOUNT_CAPITAL - equity) / ACCOUNT_CAPITAL * 100

                state["account_equity"] = round(equity, 2)
                state["account_drawdown_pct"] = round(drawdown_pct, 2)

                if drawdown_pct >= DRAWDOWN_KILL_PCT and not state["kill_switch"]:
                    log.error(f"DRAWDOWN KILL TRIGGERED: equity=${equity:.2f}, drawdown={drawdown_pct:.1f}%")
                    state["kill_switch"] = True
                    for pos in list(state["positions"]):
                        execute_close(pos, reason=f"DRAWDOWN KILL ({drawdown_pct:.1f}%)")
                    save_state()
            except Exception as e:
                log.warning(f"Balance check error: {e}")

        except Exception as e:
            log.warning(f"Monitor loop error: {e}")

        time.sleep(MONITOR_INTERVAL)


def reconcile_with_exchange():
    """
    Compare our state with actual Binance positions.
    Detect: positions closed by exchange (SL/TP hit), orphan positions.

    2026-06-07 fix: positions are now matched by (symbol, side) tuple rather than
    symbol alone. Prior bug caused phantom positions when a manual macro position
    coexisted with a strategy position on the same symbol — reconcile would see
    "BTC still on exchange" and assume the strategy LONG was open, even after it
    closed. Manual macro SHORT (Pavan's) would never get reconciled because the
    strategy's BTC LONG occupied the symbol slot. Now we distinguish LONG vs SHORT.
    Orphans (positions on exchange we didn't open) are deduped so the manual macro
    doesn't spam the log every 5 min.
    """
    try:
        exchange_positions = exchange.fetch_positions()
        active_keys = {}   # keyed by (symbol, side_upper) to distinguish LONG vs SHORT
        for ep in exchange_positions:
            contracts = abs(float(ep.get("contracts", 0)))
            if contracts > 0:
                sym = ep.get("symbol", "")
                side = (ep.get("side") or "").upper()  # normalize to LONG/SHORT
                active_keys[(sym, side)] = {
                    "contracts": contracts,
                    "side": side,
                    "unrealizedPnl": float(ep.get("unrealizedPnl", 0)),
                    "entryPrice": float(ep.get("entryPrice", 0)),
                }

        # Check our positions against exchange (match by symbol + side)
        for pos in list(state["positions"]):
            sym = pos["symbol"]
            pos_side = (pos.get("direction") or "").upper()
            if (sym, pos_side) not in active_keys:
                # Position was closed by exchange (SL or TP hit)
                log.info(f"RECONCILE: {pos['id']} ({sym} {pos_side}) no longer on exchange — closed by SL/TP")

                # Get the actual exit price from recent trades
                try:
                    trades = exchange.fetch_my_trades(sym, limit=5)
                    if trades:
                        exit_price = float(trades[-1]["price"])
                    else:
                        exit_price = pos.get("current_price", pos["entry_price"])
                except:
                    exit_price = pos.get("current_price", pos["entry_price"])

                # Calculate PnL
                if pos["direction"] == "SHORT":
                    pnl_pct = (pos["entry_price"] - exit_price) / pos["entry_price"] * 100
                else:
                    pnl_pct = (exit_price - pos["entry_price"]) / pos["entry_price"] * 100

                pnl_usd = round(pnl_pct / 100 * pos["notional"], 2)

                pos["exit_price"] = exit_price
                pos["pnl_pct"] = round(pnl_pct, 4)
                pos["pnl_usd"] = pnl_usd
                pos["closed_at"] = datetime.now(timezone.utc).isoformat()
                pos["exit_reason"] = "SL/TP (exchange reconcile)"
                pos["status"] = "CLOSED"

                state["positions"] = [p for p in state["positions"] if p["id"] != pos["id"]]
                state["closed"].append(pos)
                if len(state["closed"]) > 100:
                    state["closed"] = state["closed"][-100:]

                state["total_pnl"] = round(state["total_pnl"] + pnl_usd, 2)
                strat = pos["strategy"]
                state["strategy_pnl"][strat] = round(state["strategy_pnl"].get(strat, 0) + pnl_usd, 2)

                log.info(f"  → Reconciled close: PnL {pnl_pct:+.2f}% (${pnl_usd:+.2f})")

        # Check for orphans: positions on exchange NOT in our state, by (sym, side).
        # Deduped so a known manual macro position doesn't spam the log/state every 5 min.
        our_keys = {(p["symbol"], (p.get("direction") or "").upper()) for p in state["positions"]}
        if "orphans" not in state:
            state["orphans"] = []
        known_orphan_keys = {(o.get("symbol"), (o.get("side") or "").upper()) for o in state["orphans"]}
        for (sym, side), info in active_keys.items():
            if (sym, side) in our_keys:
                continue
            if (sym, side) in known_orphan_keys:
                continue  # already logged — skip to avoid noise from manual macros
            log.warning(f"ORPHAN DETECTED: {sym} {side} {info['contracts']} contracts on exchange but NOT in controller state")
            state["orphans"].append({
                "symbol": sym,
                "side": side,
                "contracts": info["contracts"],
                "detected_at": datetime.now(timezone.utc).isoformat(),
            })

        save_state()

    except Exception as e:
        log.warning(f"Reconcile error: {e}")


# ─────────────────────────────────────────
# API ENDPOINTS
# ─────────────────────────────────────────
@app.route("/signal", methods=["POST"])
def receive_signal():
    """
    Paper bots POST signals here.
    Expected JSON:
    {
        "strategy": "alligator",
        "coin": "XRP",
        "direction": "SHORT",
        "entry_price": 0.5432,
        "sl_price": 0.5513,
        "tp_price": null,
        "margin": 30
    }
    """
    data = request.json
    if not data:
        return jsonify({"status": "error", "msg": "No JSON body"}), 400

    strategy  = data.get("strategy", "")
    coin      = data.get("coin", "")
    direction = data.get("direction", "")
    sl_price  = data.get("sl_price")
    tp_price  = data.get("tp_price")
    margin    = data.get("margin", STRATEGIES.get(strategy, {}).get("per_trade", 30))
    # %-based sizing (2026-06-18): if strategy defines per_trade_pct, size off live equity
    # so risk-per-trade stays a constant % as the account grows. account_equity is cached
    # every 30s by the monitor loop; falls back to ACCOUNT_CAPITAL if unset. Downstream
    # min-notional resize + MAX_SINGLE_MARGIN clamp still apply.
    _pct = STRATEGIES.get(strategy, {}).get("per_trade_pct")
    if _pct:
        _eq = state.get("account_equity") or ACCOUNT_CAPITAL
        margin = round(_eq * _pct / 100, 2)
    atr_at_signal = data.get("atr_at_signal")  # required for variant_b trail

    state["signals_received"] += 1
    state["last_signal"] = {
        "strategy": strategy, "coin": coin, "direction": direction,
        "ts": datetime.now(timezone.utc).isoformat(),
    }

    # ── Signal dedup guard (prevents double entry on bot restart) ─────
    dedup_key = f"{strategy}_{coin}_{direction}"
    last_entry = state.get("_last_entry_ts", {})
    now_ts = datetime.now(timezone.utc)
    if dedup_key in last_entry:
        try:
            last_ts = datetime.fromisoformat(last_entry[dedup_key])
            if (now_ts - last_ts).total_seconds() < 300:  # 5 min dedup window
                state["signals_rejected"] += 1
                save_state()
                log.info(f"DEDUP REJECT: {dedup_key} — fired {(now_ts - last_ts).total_seconds():.0f}s ago")
                return jsonify({"status": "rejected", "reason": "dedup — too soon after last entry"}), 200
        except:
            pass
    if "_last_entry_ts" not in state:
        state["_last_entry_ts"] = {}
    state["_last_entry_ts"][dedup_key] = now_ts.isoformat()

    # Auto-resize: if signal margin exceeds cap by ≤ RESIZE_MAX_FACTOR, shrink to cap
    # Prevents the 2026-04-30 SHRINK halt bug class (cap rejecting signals instead of resizing)
    original_margin = margin
    if AUTO_RESIZE_ON_CAP and margin > MAX_SINGLE_MARGIN:
        if margin <= MAX_SINGLE_MARGIN * RESIZE_MAX_FACTOR:
            log.warning(f"RESIZE-DOWN: {strategy} {coin} {direction} — ${margin} → ${MAX_SINGLE_MARGIN} (cap)")
            margin = MAX_SINGLE_MARGIN
            state["resize_events"] = state.get("resize_events", 0) + 1
            state["last_resize"] = {"strategy": strategy, "coin": coin, "direction": "down",
                                       "original_margin": original_margin,
                                       "resized_margin": margin,
                                       "ts": datetime.now(timezone.utc).isoformat()}
        # if margin > MAX * factor, fall through to check_risk which will reject

    # Auto-resize UP: if signal margin × leverage < coin min notional, bump UP so the order clears Binance min.
    # Added 2026-05-13: prevents missing live trades when per-coin min notional > planned notional.
    # Especially relevant for ETH/BTC/LINK/LTC/BCH which have higher mins.
    if AUTO_RESIZE_UP_TO_MIN:
        cfg_for_resize = STRATEGIES.get(strategy, {})
        lev = cfg_for_resize.get("leverage", 5)
        coin_min_check = COIN_MIN_NOTIONAL.get(coin, COIN_MIN_NOTIONAL_DEFAULT)
        if margin * lev < coin_min_check:
            import math as _math
            needed = _math.ceil(coin_min_check / lev) + RESIZE_UP_BUFFER
            if needed <= MAX_SINGLE_MARGIN:
                log.warning(
                    f"RESIZE-UP: {strategy} {coin} {direction} — ${margin} → ${needed} "
                    f"(to clear {coin} min notional ${coin_min_check})"
                )
                margin = needed
                state["resize_events"] = state.get("resize_events", 0) + 1
                state["last_resize"] = {"strategy": strategy, "coin": coin, "direction": "up",
                                            "original_margin": original_margin,
                                            "resized_margin": margin,
                                            "reason": f"min_notional ${coin_min_check}",
                                            "ts": datetime.now(timezone.utc).isoformat()}
            # else: fall through to check_risk which will reject (needed > MAX_SINGLE_MARGIN is rare config error)

    # Risk check
    allowed, reason = check_risk(strategy, coin, direction, margin)
    if not allowed:
        state["signals_rejected"] += 1
        state["last_rejection"] = {"strategy": strategy, "coin": coin, "reason": reason,
                                    "ts": datetime.now(timezone.utc).isoformat()}
        save_state()
        log.info(f"REJECTED: {strategy} {coin} {direction} — {reason}")
        return jsonify({"status": "rejected", "reason": reason}), 200

    # Execute
    pos = execute_entry(strategy, coin, direction, margin, sl_price, tp_price, atr_at_signal)
    if pos:
        return jsonify({"status": "executed", "position_id": pos["id"]}), 200
    else:
        return jsonify({"status": "error", "msg": "Execution failed"}), 500


@app.route("/close", methods=["POST"])
def close_position():
    """Close a specific position. JSON: {"position_id": "..."} or {"coin": "XRP", "strategy": "alligator"}"""
    data = request.json
    pos_id = data.get("position_id")
    coin = data.get("coin")
    strategy = data.get("strategy")

    target = None
    for p in state["positions"]:
        if pos_id and p["id"] == pos_id:
            target = p; break
        if coin and strategy and p["coin"] == coin and p["strategy"] == strategy:
            target = p; break

    if not target:
        return jsonify({"status": "error", "msg": "Position not found"}), 404

    success = execute_close(target, reason=data.get("reason", "manual"))
    return jsonify({"status": "closed" if success else "error"}), 200


@app.route("/update_sl", methods=["POST"])
def api_update_sl():
    """Update SL for a position. JSON: {"position_id": "...", "new_sl": 0.55}"""
    data = request.json
    pos_id = data.get("position_id")
    new_sl = data.get("new_sl")

    target = None
    for p in state["positions"]:
        if p["id"] == pos_id:
            target = p; break

    if not target:
        return jsonify({"status": "error", "msg": "Position not found"}), 404

    success = update_sl(target, new_sl)
    return jsonify({"status": "updated" if success else "error"}), 200


@app.route("/kill", methods=["POST"])
def kill_switch():
    """Emergency: close all positions and halt trading."""
    log.warning("KILL SWITCH ACTIVATED")
    tg_send(f"🚨🚨 <b>[AG] KILL SWITCH</b>\nClosing {len(state['positions'])} positions\nTotal PnL: ${state['total_pnl']:+.2f}")
    state["kill_switch"] = True
    for pos in list(state["positions"]):
        execute_close(pos, reason="KILL SWITCH")
    save_state()
    return jsonify({"status": "killed", "positions_closed": len(state["positions"])}), 200


@app.route("/config", methods=["GET", "POST"])
def config_endpoint():
    """GET: return current config. POST: update strategy allocation/enabled."""
    if request.method == "GET":
        return jsonify({
            "strategies": STRATEGIES,
            "account_capital": ACCOUNT_CAPITAL,
            "reserve": RESERVE,
            "max_concurrent": MAX_CONCURRENT_TOTAL,
            "drawdown_kill_pct": DRAWDOWN_KILL_PCT,
        })

    data = request.json
    strat = data.get("strategy")
    if strat and strat in STRATEGIES:
        if "enabled" in data:
            STRATEGIES[strat]["enabled"] = data["enabled"]
        if "allocation" in data:
            STRATEGIES[strat]["allocation"] = float(data["allocation"])
        if "per_trade" in data:
            STRATEGIES[strat]["per_trade"] = float(data["per_trade"])
        if "max_concurrent" in data:
            STRATEGIES[strat]["max_concurrent"] = int(data["max_concurrent"])
        log.info(f"CONFIG UPDATED: {strat} → {data}")
        return jsonify({"status": "updated", "strategy": STRATEGIES[strat]})

    if "kill_switch" in data:
        state["kill_switch"] = data["kill_switch"]
        save_state()
        return jsonify({"status": "kill_switch", "value": state["kill_switch"]})

    return jsonify({"status": "error", "msg": "Unknown config key"}), 400


@app.route("/status")
def status_json():
    """Full state as JSON — for programmatic access."""
    return jsonify({
        "positions":        state["positions"],
        "closed_recent":    state["closed"][-10:],
        "strategy_pnl":     state["strategy_pnl"],
        "total_pnl":        state["total_pnl"],
        "total_trades":     state["total_trades"],
        "signals_received": state["signals_received"],
        "signals_executed": state["signals_executed"],
        "signals_rejected": state["signals_rejected"],
        "kill_switch":      state["kill_switch"],
        "strategy_paused":  state["strategy_paused"],
        "last_signal":      state["last_signal"],
        "last_rejection":   state["last_rejection"],
    })


# ─────────────────────────────────────────
# DASHBOARD
# ─────────────────────────────────────────
DASHBOARD_HTML = """
<!DOCTYPE html>
<html><head>
<title>AlphaGrid Live Controller</title>
<meta http-equiv="refresh" content="30">
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:-apple-system,BlinkMacSystemFont,monospace; background:#0a0a0a; color:#e0e0e0; padding:20px; }
h1 { color:#a78bfa; margin-bottom:4px; font-size:22px; }
.subtitle { color:#666; font-size:12px; margin-bottom:20px; }
.stats-row { display:flex; gap:16px; margin-bottom:20px; flex-wrap:wrap; }
.stat-card { background:#111; border:1px solid #222; border-radius:8px; padding:14px 18px; min-width:140px; }
.stat-label { font-size:10px; color:#666; text-transform:uppercase; letter-spacing:1px; }
.stat-value { font-size:24px; font-weight:700; margin-top:4px; }
.green { color:#00c853; } .red { color:#ff1744; } .yellow { color:#ffd600; } .purple { color:#a78bfa; }
.kill-active { background:#2a0000; border-color:#ff1744; }
table { width:100%; border-collapse:collapse; margin-top:12px; }
th { background:#1a1a1a; color:#888; font-size:10px; text-transform:uppercase; letter-spacing:1px; padding:8px 12px; text-align:left; }
td { padding:8px 12px; border-bottom:1px solid #1a1a1a; font-size:13px; }
.section { margin-top:24px; }
.section-title { color:#a78bfa; font-size:14px; font-weight:600; margin-bottom:8px; }
.badge { display:inline-block; padding:2px 8px; border-radius:4px; font-size:11px; font-weight:600; }
.badge-on { background:#0a2a10; color:#00c853; border:1px solid #00c853; }
.badge-off { background:#1a1a1a; color:#555; border:1px solid #333; }
.badge-paused { background:#2a2a00; color:#ffd600; border:1px solid #ffd600; }
.btn { padding:6px 14px; border-radius:4px; border:none; cursor:pointer; font-size:12px; font-weight:600; }
.btn-kill { background:#ff1744; color:#fff; }
.btn-kill:hover { background:#d50000; }
</style>
</head><body>

<h1>⚡ AlphaGrid Live Controller</h1>
<div class="subtitle">Binance Copy Trading Portfolio · Port {{ port }} · Auto-refresh 30s</div>

{% if state.kill_switch %}
<div class="stat-card kill-active" style="margin-bottom:16px;">
  <div class="stat-label">⚠️ KILL SWITCH ACTIVE</div>
  <div class="stat-value red">ALL TRADING HALTED</div>
</div>
{% endif %}

<div class="stats-row">
  <div class="stat-card">
    <div class="stat-label">Account P&L</div>
    <div class="stat-value {{ 'green' if state.total_pnl >= 0 else 'red' }}">
      ${{ "%.2f"|format(state.total_pnl) }}
    </div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Open Positions</div>
    <div class="stat-value purple">{{ state.positions|length }}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Total Trades</div>
    <div class="stat-value">{{ state.total_trades }}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Signals</div>
    <div class="stat-value">{{ state.signals_executed }}/{{ state.signals_received }}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Rejected</div>
    <div class="stat-value yellow">{{ state.signals_rejected }}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Capital ({{ days_str }})</div>
    <div class="stat-value">${{ "%.0f"|format(account_capital) }}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Avg Deployed</div>
    <div class="stat-value">${{ "%.0f"|format(acct_deployed) }} <span style="color:#888;font-size:13px;">({{ acct_util }}%)</span></div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Monthly ROI · Alloc</div>
    <div class="stat-value {{ 'green' if roi_pct > 0 else ('red' if roi_pct < 0 else '') }}">{{ monthly_str }}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Monthly ROI · Deployed</div>
    <div class="stat-value {{ 'green' if acct_dep_roi > 0 else ('red' if acct_dep_roi < 0 else '') }}">{{ acct_dep_monthly }}</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Annual IRR · Deployed</div>
    <div class="stat-value {{ 'green' if acct_dep_roi > 0 else ('red' if acct_dep_roi < 0 else '') }}">{{ acct_dep_irr }}</div>
  </div>
</div>

<div class="section">
  <div class="section-title">Strategy Allocation</div>
  <table>
    <tr><th>Strategy</th><th>Status</th><th>Alloc</th><th>Per Trade</th><th>Max</th><th>P&L</th><th>Avg Dep</th><th>Util %</th><th>ROI Alloc</th><th>ROI Deployed</th><th>Monthly Dep</th><th>Annual IRR Dep</th><th>n / Days</th><th>Open</th></tr>
    {% for key, s in strategies.items() %}
    {% set m = strat_metrics.get(key, {}) %}
    {% set s_pnl = state.strategy_pnl.get(key, 0) %}
    <tr>
      <td><strong>{{ s.display_name }}</strong></td>
      <td>
        {% if state.strategy_paused.get(key) %}
          <span class="badge badge-paused">PAUSED</span>
        {% elif s.enabled %}
          <span class="badge badge-on">LIVE</span>
        {% else %}
          <span class="badge badge-off">OFF</span>
        {% endif %}
      </td>
      <td>${{ "%.0f"|format(s.allocation) }}</td>
      <td>${{ "%.0f"|format(s.per_trade) }} × {{ s.leverage }}×</td>
      <td>{{ s.max_concurrent }}</td>
      <td class="{{ 'green' if s_pnl > 0 else ('red' if s_pnl < 0 else '') }}">
        ${{ "%.2f"|format(s_pnl) }}
      </td>
      <td>${{ "%.2f"|format(m.get('deployed_usd', 0)) }}</td>
      <td style="color:{{ '#888' if m.get('util_pct', 0) < 1 else ('#ffb300' if m.get('util_pct', 0) < 30 else '#00c853') }};">
        {{ "%.1f"|format(m.get('util_pct', 0)) }}%
      </td>
      <td class="{{ 'green' if m.get('alloc_roi', 0) > 0 else ('red' if m.get('alloc_roi', 0) < 0 else '') }}">
        {{ "%+.1f"|format(m.get('alloc_roi', 0)) }}%
      </td>
      <td class="{{ 'green' if m.get('dep_roi', 0) > 0 else ('red' if m.get('dep_roi', 0) < 0 else '') }}">
        {{ "%+.1f"|format(m.get('dep_roi', 0)) }}%
      </td>
      <td class="{{ 'green' if m.get('dep_roi', 0) > 0 else ('red' if m.get('dep_roi', 0) < 0 else '') }}">
        {{ m.get('dep_monthly', '—') }}
      </td>
      <td class="{{ 'green' if m.get('dep_roi', 0) > 0 else ('red' if m.get('dep_roi', 0) < 0 else '') }}">
        {{ m.get('dep_irr', '—') }}
      </td>
      <td style="color:#888;">{{ m.get('n_closed', 0) }}T / {{ m.get('days_str', '0.0d') }}</td>
      <td>{{ state.positions|selectattr('strategy','equalto',key)|list|length }}</td>
    </tr>
    {% endfor %}
  </table>
</div>

{% if state.positions %}
<div class="section">
  <div class="section-title">Open Positions</div>
  <table>
    <tr><th>ID</th><th>Strategy</th><th>Coin</th><th>Dir</th><th>Entry</th><th>Current</th><th>SL</th><th>PnL</th><th>Opened</th></tr>
    {% for p in state.positions %}
    <tr>
      <td>{{ p.id }}</td>
      <td>{{ p.strategy }}</td>
      <td><strong>{{ p.coin }}</strong></td>
      <td>{{ p.direction }}</td>
      <td>{{ p.entry_price }}</td>
      <td>{{ p.get('current_price', '—') }}</td>
      <td>{{ p.sl_price }}</td>
      <td class="{{ 'green' if p.pnl >= 0 else 'red' }}">{{ "%.2f"|format(p.pnl) }}%</td>
      <td>{{ p.opened_at[:16] }}</td>
    </tr>
    {% endfor %}
  </table>
</div>
{% endif %}

{% if state.closed %}
<div class="section">
  <div class="section-title">Recent Closed Trades (last 10)</div>
  <table>
    <tr><th>ID</th><th>Strategy</th><th>Coin</th><th>Dir</th><th>PnL</th><th>$</th><th>Exit</th><th>Closed</th></tr>
    {% for p in state.closed[-10:]|reverse %}
    <tr>
      <td>{{ p.id }}</td>
      <td>{{ p.strategy }}</td>
      <td><strong>{{ p.coin }}</strong></td>
      <td>{{ p.direction }}</td>
      <td class="{{ 'green' if p.get('pnl_pct',0) >= 0 else 'red' }}">{{ "%.2f"|format(p.get('pnl_pct',0)) }}%</td>
      <td class="{{ 'green' if p.get('pnl_usd',0) >= 0 else 'red' }}">${{ "%.2f"|format(p.get('pnl_usd',0)) }}</td>
      <td>{{ p.get('exit_reason','—') }}</td>
      <td>{{ p.get('closed_at','—')[:16] }}</td>
    </tr>
    {% endfor %}
  </table>
</div>
{% endif %}

{% if state.last_rejection %}
<div class="section">
  <div class="section-title">Last Rejection</div>
  <div style="background:#1a1a00; border:1px solid #333; padding:10px; border-radius:6px; font-size:12px;">
    {{ state.last_rejection.strategy }} / {{ state.last_rejection.coin }} — <span class="yellow">{{ state.last_rejection.reason }}</span>
    <span style="color:#555;"> · {{ state.last_rejection.ts[:16] }}</span>
  </div>
</div>
{% endif %}

<div class="section" style="margin-top:30px;">
  <div style="color:#333; font-size:11px;">
    Started: {{ state.started_at[:16] }} ·
    <a href="/status" style="color:#555;">JSON API</a> ·
    <a href="/config" style="color:#555;">Config API</a>
  </div>
</div>

</body></html>
"""

def _roi_metrics(pnl, capital, days):
    if capital <= 0:
        return {"roi_pct": 0.0, "monthly_str": "—", "irr_str": "—", "days_str": "0.0d"}
    roi_pct = pnl / capital * 100
    if days < 1.0:
        return {"roi_pct": round(roi_pct, 1), "monthly_str": "n<1d", "irr_str": "n<1d", "days_str": f"{days:.1f}d"}
    monthly_roi = roi_pct * (30.0 / days)
    base = 1 + pnl / capital
    try:
        annual_irr = ((base ** (365.0 / days)) - 1) * 100 if base > 0 else -100.0
    except (OverflowError, ZeroDivisionError):
        annual_irr = 0.0
    return {
        "roi_pct": round(roi_pct, 1),
        "monthly_str": f"{monthly_roi:+.1f}%",
        "irr_str": f"{annual_irr:+.0f}%",
        "days_str": f"{days:.1f}d",
    }


def _parse_iso(ts):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _earliest_open_days(trades):
    parsed = [_parse_iso(t.get("opened_at")) for t in trades]
    parsed = [p for p in parsed if p]
    if not parsed:
        return 0.0
    return max((datetime.now(timezone.utc) - min(parsed)).total_seconds() / 86400, 0.0)


def _time_weighted_deployed(closed_trades, open_trades):
    """Time-weighted avg deployed margin: Σ (margin × duration_days) / span_days.
    Returns (avg_deployed_usd, span_days, util_pct_of_alloc)."""
    now = datetime.now(timezone.utc)
    total_md = 0.0  # margin-days
    earliest = None
    for t in closed_trades:
        o = _parse_iso(t.get("opened_at"))
        c = _parse_iso(t.get("closed_at"))
        if o and c:
            total_md += float(t.get("margin", 0)) * (c - o).total_seconds() / 86400
            if not earliest or o < earliest:
                earliest = o
    for p in open_trades:
        o = _parse_iso(p.get("opened_at"))
        if o:
            total_md += float(p.get("margin", 0)) * (now - o).total_seconds() / 86400
            if not earliest or o < earliest:
                earliest = o
    if not earliest:
        return 0.0, 0.0
    span = max((now - earliest).total_seconds() / 86400, 0.0)
    avg_dep = total_md / span if span > 0 else 0.0
    return avg_dep, span


@app.route("/")
def dashboard():
    pnl = float(state.get("total_pnl", 0.0))
    capital = ACCOUNT_CAPITAL
    try:
        started = datetime.fromisoformat(state["started_at"].replace("Z", "+00:00"))
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        days = max((datetime.now(timezone.utc) - started).total_seconds() / 86400, 0.0)
    except Exception:
        days = 0.0
    acct = _roi_metrics(pnl, capital, days)

    closed_all = list(state.get("closed", []))
    open_all = list(state.get("positions", []))

    # Account-level ROI on deployed
    acct_dep, acct_span = _time_weighted_deployed(closed_all, open_all)
    acct_dep_metrics = _roi_metrics(pnl, acct_dep, acct_span) if acct_dep > 0 else {
        "roi_pct": 0.0, "monthly_str": "—", "irr_str": "—", "days_str": "0.0d"
    }
    acct_util = (acct_dep / capital * 100) if capital > 0 else 0.0

    strat_metrics = {}
    for key, cfg in STRATEGIES.items():
        s_pnl = float(state["strategy_pnl"].get(key, 0.0))
        s_alloc = float(cfg.get("allocation", 0.0))
        s_closed = [t for t in closed_all if t.get("strategy") == key]
        s_open   = [t for t in open_all   if t.get("strategy") == key]
        s_days = _earliest_open_days(s_closed + s_open)
        m_alloc = _roi_metrics(s_pnl, s_alloc, s_days)
        s_dep, s_span = _time_weighted_deployed(s_closed, s_open)
        m_dep = _roi_metrics(s_pnl, s_dep, s_span) if s_dep > 0 else {
            "roi_pct": 0.0, "monthly_str": "—", "irr_str": "—", "days_str": "0.0d"
        }
        util = (s_dep / s_alloc * 100) if s_alloc > 0 else 0.0
        strat_metrics[key] = {
            "alloc_roi": m_alloc["roi_pct"],
            "alloc_monthly": m_alloc["monthly_str"],
            "alloc_irr": m_alloc["irr_str"],
            "days_str": m_alloc["days_str"],
            "deployed_usd": round(s_dep, 2),
            "util_pct": round(util, 1),
            "dep_roi": m_dep["roi_pct"],
            "dep_monthly": m_dep["monthly_str"],
            "dep_irr": m_dep["irr_str"],
            "n_closed": len(s_closed),
        }

    return render_template_string(
        DASHBOARD_HTML,
        state=state,
        strategies=STRATEGIES,
        port=PORT,
        account_capital=ACCOUNT_CAPITAL,
        deployable=DEPLOYABLE,
        reserve=RESERVE,
        roi_pct=acct["roi_pct"],
        monthly_str=acct["monthly_str"],
        irr_str=acct["irr_str"],
        days_str=acct["days_str"],
        acct_deployed=round(acct_dep, 2),
        acct_util=round(acct_util, 1),
        acct_dep_roi=acct_dep_metrics["roi_pct"],
        acct_dep_monthly=acct_dep_metrics["monthly_str"],
        acct_dep_irr=acct_dep_metrics["irr_str"],
        strat_metrics=strat_metrics,
    )


# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────
def main():
    load_state()

    if not API_KEY or not API_SECRET:
        log.error("API keys not found in /opt/trader/.env.live — cannot start")
        return

    # Verify connection
    try:
        balance = exchange.fetch_balance()
        usdt = balance.get("USDT", {})
        log.info(f"Binance connected: USDT total=${usdt.get('total', 0)} free=${usdt.get('free', 0)}")
    except Exception as e:
        log.error(f"Binance connection failed: {e}")
        return

    enabled_alloc, all_alloc = _validate_allocations()
    log.info("=" * 60)
    log.info("AlphaGrid Live Controller starting")
    log.info(f"  Port:        {PORT}")
    log.info(f"  Capital:     ${ACCOUNT_CAPITAL}")
    log.info(f"  Reserve:     ${RESERVE} (deployable ${DEPLOYABLE})")
    log.info(f"  Strategies:  {[k for k,v in STRATEGIES.items() if v['enabled']]}")
    log.info(f"  Allocations: enabled ${enabled_alloc} / all ${all_alloc} of ${DEPLOYABLE}")
    log.info(f"  Max pos:     {MAX_CONCURRENT_TOTAL}")
    log.info(f"  Kill at:     -{DRAWDOWN_KILL_PCT}%")
    log.info("=" * 60)

    # Start position monitor thread
    monitor_thread = threading.Thread(target=monitor_positions, daemon=True)
    monitor_thread.start()

    # Start Flask
    app.run(host="0.0.0.0", port=PORT, debug=False)


if __name__ == "__main__":
    main()
