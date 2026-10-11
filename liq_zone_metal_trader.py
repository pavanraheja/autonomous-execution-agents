#!/usr/bin/env python3
"""
Liq-Zone METAL micro trader — ONE config-driven codebase, N isolated instances (QR Lab).

Runs the validated ChartPrime liquidation-zone strategy (same signal core as live 8089) on a
single configurable metal perp, at micro size, self-managing the FULL exit (partial TP1 + ATR
trail + TP2 + native catastrophic SL — the trail IS the edge). Each instance is isolated:
own port / state / trade-log / systemd unit. Trades ONLY its own symbol; sl_watcher +
live_controller are (symbol,side)-scoped and ignore it.

Instances (env-configured):
  PAXG (gold)   : LZ_COIN=PAXG LZ_SYMBOL=PAXG/USDT:USDT LZ_TFS=6h,3h LZ_DIRS=LONG,SHORT LZ_PORT=8113
  XAG  (silver) : LZ_COIN=XAG  LZ_SYMBOL=XAG/USDT:USDT  LZ_TFS=4h    LZ_DIRS=SHORT      LZ_PORT=8114
Defaults reproduce the PAXG instance exactly.

Backtests (liq_zone_paxg / xag _2026_08_30): ~7-17mo, one regime each. Gold-scaled TPs REJECTED
on both metals (trail-riding edge). PAXG best ungated (long=gold beta); XAG best 4H-gated
(short = genuine timing alpha, beats random-short +2.30 vs -0.17%/trade in a -15.4% downtrend).
Micro = learn real slippage. Promotion gate: n>=30 forward AND PF>=1.5.
"""
import ccxt, time, json, logging, threading, os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import numpy as np
import pandas as pd
from flask import Flask, jsonify

# ─── INSTANCE CONFIG (env-driven; defaults = PAXG) ────────────────────────────
COIN          = os.environ.get("LZ_COIN", "PAXG")
SYMBOL        = os.environ.get("LZ_SYMBOL", "PAXG/USDT:USDT")
TIMEFRAMES    = [t.strip() for t in os.environ.get("LZ_TFS", "6h,3h").split(",") if t.strip()]
DIRECTIONS    = [d.strip().upper() for d in os.environ.get("LZ_DIRS", "LONG,SHORT").split(",") if d.strip()]
PORT          = int(os.environ.get("LZ_PORT", "8113"))
NOTIONAL      = float(os.environ.get("LZ_NOTIONAL", "70"))
DRY_RUN       = os.environ.get("LZ_DRY", "true").lower() == "true"   # SAFE DEFAULT: shadow unless LZ_DRY=false
ACCOUNT       = os.environ.get("LZ_ACCOUNT", "BINANCE_LIVE")
VENUE         = os.environ.get("LZ_VENUE", "binance").lower()        # binance | bybit
LEVERAGE      = int(os.environ.get("LZ_LEVERAGE", "5"))
# per-coin SL/TP (standard config — gold-scaled rejected on both metals)
CFG           = {"sl": float(os.environ.get("LZ_SL", "1.5")),
                 "tp1": float(os.environ.get("LZ_TP1", "5.0")),
                 "tp2": float(os.environ.get("LZ_TP2", "10.0"))}

BTC_SYMBOL    = "BTC/USDT:USDT"
MFI_LENGTH, ATR_LENGTH, TRAIL_ATR_MULT = 14, 14, 1.0
MFI_OB, MFI_OS, MFI_OB_DEEP = 80, 20, 82
PIVOT_LENGTH = 10
LIQ_LEVELS_SHORT, LIQ_LEVELS_LONG = [100, 50, 25], [100, 25]
PIVOT_PROXIMITY, PIVOT_NEAR_PCT = 1.0, 2.0
LONG_MFI_MAX, SHORT_MFI_MIN = 60, 40
COST_PCT, COOLDOWN_HOURS = 0.20, 24
SCAN_INTERVAL_MIN, MONITOR_INTERVAL_MIN = 30, 15

_c = COIN.lower()
STATE_FILE = Path(f"/opt/trader/state/liq_zone_{_c}.json")
TRADE_LOG  = Path(f"/opt/trader/Trade Logs/liq_zone_{_c}_trades.json")
LOG_FILE   = Path(f"/opt/trader/logs/{_c}_{PORT}.log")

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()])
log = logging.getLogger(_c)

state = {"open_trade": None, "closed_trades": [], "cooldown_until": None,
         "btc_mfi": {}, "coin_price": None, "last_scan": None, "dry_run": DRY_RUN}

# ─── ALERTS (Telegram, reuse /opt/trader/.env.alerts) ─────────────────────────
def tg(msg):
    try:
        import urllib.parse, urllib.request
        bot = os.environ.get("TG_BOT_TOKEN", ""); chat = os.environ.get("TG_CHAT_ID", "")
        if not bot or not chat:
            for line in open("/opt/trader/.env.alerts"):
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1); v = v.strip().strip('"').strip("'")
                    if k.strip() == "TG_BOT_TOKEN" and not bot: bot = v
                    elif k.strip() == "TG_CHAT_ID" and not chat: chat = v
        if not bot or not chat:
            return
        data = urllib.parse.urlencode({"chat_id": chat, "text": msg, "parse_mode": "HTML"}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{bot}/sendMessage", data=data, timeout=8).read()
    except Exception as e:
        log.info("tg note: %s", str(e)[:60])

# ─── KEYS / EXCHANGE ──────────────────────────────────────────────────────────
def _load_keys():
    d = {}
    for line in open("/opt/trader/.env.live"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1); d[k.strip()] = v.strip().strip('"').strip("'")
    if VENUE == "bybit":                       # note: Bybit uses _API_SECRET, Binance uses _SECRET
        return d.get("BYBIT_API_KEY", ""), d.get("BYBIT_API_SECRET", "")
    return d.get(ACCOUNT + "_API_KEY", ""), d.get(ACCOUNT + "_SECRET", "")

_ex = None
def get_exchange():
    global _ex
    if _ex is None:
        k, s = _load_keys()
        if VENUE == "bybit":
            _ex = ccxt.bybit({"apiKey": k, "secret": s, "enableRateLimit": True,
                              "options": {"defaultType": "swap"}})
        else:
            _ex = ccxt.binance({"apiKey": k, "secret": s,
                                "options": {"defaultType": "future"}, "enableRateLimit": True})
        _ex.load_markets()
    return _ex

# ─── INDICATORS (verbatim from live 8089) ─────────────────────────────────────
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

def find_pivot_highs(df, length=PIVOT_LENGTH):
    highs = df["high"].values; pivots = []
    for i in range(length, len(df) - length):
        if highs[i] == highs[i - length:i + length + 1].max():
            pivots.append((i, float(highs[i])))
    return pivots

def find_pivot_lows(df, length=PIVOT_LENGTH):
    lows = df["low"].values; pivots = []
    for i in range(length, len(df) - length):
        if lows[i] == lows[i - length:i + length + 1].min():
            pivots.append((i, float(lows[i])))
    return pivots

def check_pivot_zone(price, pivots, idx, direction="SHORT"):
    recent_pivot = None
    for pi, pv in reversed(pivots):
        if pi < idx:
            recent_pivot = pv; break
    if recent_pivot is None:
        return False, None
    levels = LIQ_LEVELS_SHORT if direction == "SHORT" else LIQ_LEVELS_LONG
    for lev in levels:
        liq_pct = 1.0 / lev
        for liq_price in [recent_pivot * (1 + liq_pct), recent_pivot * (1 - liq_pct)]:
            proximity = abs(price - liq_price) / price * 100
            if proximity <= PIVOT_PROXIMITY:
                return True, {"method": "chartprime_liq", "pivot": round(recent_pivot, 2),
                              "leverage": str(lev) + "x", "liq_price": round(liq_price, 2),
                              "proximity": round(proximity, 2)}
    pivot_dist = abs(price - recent_pivot) / price * 100
    if pivot_dist <= PIVOT_NEAR_PCT:
        return True, {"method": "chartprime_pivot", "pivot": round(recent_pivot, 2),
                      "distance": round(pivot_dist, 2)}
    return False, None

# ─── CANDLES (1h→3h resample for the synthetic 3h clock) ──────────────────────
def fetch_candles(ex, symbol, timeframe, limit=200):
    try:
        if timeframe == "3h":
            raw = ex.fetch_ohlcv(symbol, "1h", limit=min(limit * 3 + 10, 1000))
            df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
            df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True); df.set_index("ts", inplace=True)
            return df.resample("3h").agg({"open": "first", "high": "max", "low": "min",
                                          "close": "last", "volume": "sum"}).dropna()
        raw = ex.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(raw, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True); df.set_index("ts", inplace=True)
        return df
    except Exception as e:
        log.error("fetch %s %s: %s", symbol, timeframe, str(e)[:80]); return None

# ─── SIGNAL (BTC MFI reversal at liq zone) ────────────────────────────────────
def detect_signal(ex, tf):
    df = fetch_candles(ex, BTC_SYMBOL, tf, limit=80)
    if df is None or len(df) < 50:
        return None
    df["mfi"] = calc_mfi(df, MFI_LENGTH); df["atr"] = calc_atr(df, ATR_LENGTH)
    mfi_vals = df["mfi"].dropna().values
    if len(mfi_vals) < 10:
        return None
    cur_mfi = float(mfi_vals[-2]); prev_mfis = mfi_vals[-7:-2]
    cand = df.iloc[-2]; entry = float(cand["close"]); atr = float(df["atr"].dropna().iloc[-2])
    state["btc_mfi"][tf] = round(cur_mfi, 1)
    green = cand["close"] > cand["open"]; red = cand["close"] < cand["open"]
    i = len(df) - 2; ph = find_pivot_highs(df); pl = find_pivot_lows(df)
    ts = df.index[-2].strftime("%Y-%m-%dT%H:%M+00:00")
    if np.any(prev_mfis > MFI_OB_DEEP) and cur_mfi < MFI_OB and red:
        ok, zi = check_pivot_zone(entry, ph, i, "SHORT")
        if ok:
            return {"direction": "SHORT", "entry": entry, "atr": atr, "btc_mfi": round(cur_mfi, 1),
                    "tf": tf, "ts": ts, "zone": zi}
    if np.any(prev_mfis < MFI_OS) and cur_mfi > MFI_OS and green:
        ok, zi = check_pivot_zone(entry, pl, i, "LONG")
        if ok:
            return {"direction": "LONG", "entry": entry, "atr": atr, "btc_mfi": round(cur_mfi, 1),
                    "tf": tf, "ts": ts, "zone": zi}
    return None

# ─── EXECUTOR (DRY_RUN-gated, single-symbol, reduceOnly exits) ────────────────
def _qty(ex, notional, price):
    return float(ex.amount_to_precision(SYMBOL, notional / price))

def place_entry(ex, direction, qty, sl_price):
    if DRY_RUN:
        log.info("[DRY_RUN] ENTRY %s %s %.6f @mkt SL=%.4f", direction, SYMBOL, qty, sl_price)
        return {"dry_run": True}
    try:
        ex.set_leverage(LEVERAGE, SYMBOL)
    except Exception as e:
        log.info("set_leverage note: %s", str(e)[:60])
    side = "buy" if direction == "LONG" else "sell"
    if VENUE == "bybit":
        order = ex.create_order(SYMBOL, "market", side, qty, params={"positionIdx": 0})
        try:                                    # Bybit: SL attached to position (tpslMode Full)
            ex.privatePostV5PositionTradingStop({
                "category": "linear", "symbol": ex.market(SYMBOL)["id"], "positionIdx": 0,
                "stopLoss": ex.price_to_precision(SYMBOL, sl_price), "tpslMode": "Full"})
        except Exception as e:
            log.error("NATIVE SL FAILED (position open w/o backstop!): %s", str(e)[:100])
        return order
    order = ex.create_order(SYMBOL, "market", side, qty)
    sl_side = "sell" if direction == "LONG" else "buy"
    try:
        ex.create_order(SYMBOL, "STOP_MARKET", sl_side, qty, None,
                        {"stopPrice": ex.price_to_precision(SYMBOL, sl_price), "reduceOnly": True})
    except Exception as e:
        log.error("NATIVE SL FAILED (position open w/o backstop!): %s", str(e)[:100])
    return order

def reduce(ex, direction, qty, tag):
    if DRY_RUN:
        log.info("[DRY_RUN] %s reduce %s %.6f", tag, SYMBOL, qty); return {"dry_run": True}
    side = "sell" if direction == "LONG" else "buy"
    params = {"reduceOnly": True}
    if VENUE == "bybit":
        params["positionIdx"] = 0
    return ex.create_order(SYMBOL, "market", side, qty, None, params)

def cancel_stops(ex):
    if DRY_RUN:
        return
    try:
        ex.cancel_all_orders(SYMBOL)
    except Exception as e:
        log.info("cancel_stops note: %s", str(e)[:60])

# ─── STATE ────────────────────────────────────────────────────────────────────
def load_state():
    if STATE_FILE.exists():
        try:
            state.update(json.load(open(STATE_FILE)))
        except Exception:
            pass
    state["dry_run"] = DRY_RUN

def save_state():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    json.dump(state, open(STATE_FILE, "w"), indent=2, default=str)
    try:
        TRADE_LOG.parent.mkdir(parents=True, exist_ok=True)
        json.dump(state["closed_trades"], open(TRADE_LOG, "w"), indent=2, default=str)
    except Exception:
        pass

def in_cooldown():
    cu = state.get("cooldown_until")
    if not cu:
        return False
    try:
        return datetime.now(timezone.utc) < datetime.fromisoformat(cu)
    except Exception:
        return False

# ─── ENTER (single-position, first-signal-wins; DIRECTIONS filter = gate) ─────
def try_enter(ex):
    if state.get("open_trade") or in_cooldown():
        return
    for tf in TIMEFRAMES:
        sig = detect_signal(ex, tf)
        if not sig:
            continue
        direction = sig["direction"]
        if direction not in DIRECTIONS:          # gate: e.g. XAG = SHORT-only
            log.info("%s %s: %s signal skipped (DIRECTIONS=%s)", COIN, tf, direction, DIRECTIONS)
            continue
        df = fetch_candles(ex, SYMBOL, tf, limit=40)
        if df is None or len(df) < 20:
            continue
        df["mfi"] = calc_mfi(df, MFI_LENGTH); df["atr"] = calc_atr(df, ATR_LENGTH)
        coin_mfi = float(df["mfi"].dropna().iloc[-2]); coin_atr = float(df["atr"].dropna().iloc[-2])
        price = float(df["close"].iloc[-2]); state["coin_price"] = round(price, 4)
        if np.isnan(coin_mfi) or np.isnan(coin_atr) or coin_atr == 0:
            continue
        if direction == "LONG" and coin_mfi > LONG_MFI_MAX:
            log.info("%s %s: MFI %.1f > %d — skip", COIN, tf, coin_mfi, LONG_MFI_MAX); continue
        if direction == "SHORT" and coin_mfi < SHORT_MFI_MIN:
            log.info("%s %s: MFI %.1f < %d — skip", COIN, tf, coin_mfi, SHORT_MFI_MIN); continue
        sl_dist = coin_atr * CFG["sl"]
        if direction == "LONG":
            sl = price - sl_dist; tp1 = price * (1 + CFG["tp1"] / 100); tp2 = price * (1 + CFG["tp2"] / 100)
        else:
            sl = price + sl_dist; tp1 = price * (1 - CFG["tp1"] / 100); tp2 = price * (1 - CFG["tp2"] / 100)
        qty = _qty(ex, NOTIONAL, price)
        place_entry(ex, direction, qty, sl)
        trade = {"coin": COIN, "symbol": SYMBOL, "direction": direction, "entry_tf": tf,
                 "entry": round(price, 6), "qty": qty, "notional": NOTIONAL,
                 "sl": round(sl, 6), "tp1": round(tp1, 6), "tp2": round(tp2, 6),
                 "tp1_pct": CFG["tp1"], "tp2_pct": CFG["tp2"], "atr": round(coin_atr, 6),
                 "partial_done": False, "partial_pnl": 0.0, "trail_active": False, "trail_sl": None,
                 "peak_pnl": 0.0, "btc_mfi": sig["btc_mfi"], "coin_mfi": round(coin_mfi, 1),
                 "zone": sig["zone"], "opened_at": datetime.now(timezone.utc).isoformat(),
                 "gate_would_allow": direction == "SHORT", "dry_run": DRY_RUN, "status": "OPEN"}
        state["open_trade"] = trade; save_state()
        log.info("ENTER %s %s @ %.4f | tf=%s SL=%.4f TP1=%.4f TP2=%.4f qty=%.6f%s",
                 direction, COIN, price, tf, sl, tp1, tp2, qty, " [DRY]" if DRY_RUN else " [LIVE]")
        tg(f"🟡 <b>{COIN} liq-zone {'[LIVE]' if not DRY_RUN else '[DRY]'} ENTER</b>\n"
           f"{direction} @ {price:.4f} · tf={tf} · ${NOTIONAL:.0f}\n"
           f"SL {sl:.4f} · TP1 {tp1:.4f} · TP2 {tp2:.4f}\nBTC MFI {sig['btc_mfi']} · zone {sig['zone'].get('method')}")
        return

# ─── MONITOR (real partial + ATR trail + TP2 + SL) ────────────────────────────
def record_close(reason, price, pnl_gross):
    t = state["open_trade"]
    pnl = round(pnl_gross - COST_PCT, 3)
    closed = {**t, "exit_price": round(price, 6), "exit_reason": reason,
              "final_pnl": pnl, "final_pnl_gross": round(pnl_gross, 3),
              "pnl_usd": round(pnl / 100 * NOTIONAL, 2),
              "closed_at": datetime.now(timezone.utc).isoformat(), "status": "CLOSED"}
    state["closed_trades"].append(closed); state["open_trade"] = None
    if reason == "SL":
        state["cooldown_until"] = (datetime.now(timezone.utc) + timedelta(hours=COOLDOWN_HOURS)).isoformat()
    save_state()
    log.info("CLOSED %s [%s] %+.2f%% ($%+.2f)%s", COIN, reason, pnl, closed["pnl_usd"],
             " [DRY]" if DRY_RUN else " [LIVE]")
    emoji = "🟢" if pnl > 0 else "🔴"
    tg(f"{emoji} <b>{COIN} liq-zone {'[LIVE]' if not DRY_RUN else '[DRY]'} CLOSE [{reason}]</b>\n"
       f"{t['direction']} · {pnl:+.2f}% (${closed['pnl_usd']:+.2f}) · tf={t['entry_tf']}\n"
       f"closed {len(state['closed_trades'])} total")

def monitor(ex):
    t = state.get("open_trade")
    if not t:
        return
    # LIVE reconcile: the native SL (or any exchange-side close) may have closed the position
    # independently of the software monitor. Detect flat-on-exchange and record it, else the
    # software exit logic fires reduceOnly orders against a non-existent position (-2022 spam)
    # and the single-position guard stays stuck.
    if not DRY_RUN:
        try:
            rp = [p for p in ex.fetch_positions([SYMBOL]) if float(p.get("contracts") or 0) != 0]
        except Exception as e:
            rp = None; log.info("reconcile positions note: %s", str(e)[:60])
        if rp is not None and len(rp) == 0:
            exit_px = t["sl"]; close_ms = None
            try:
                fills = ex.fetch_my_trades(SYMBOL, limit=5)
                if fills:
                    exit_px = float(fills[-1]["price"]); close_ms = fills[-1].get("timestamp")
            except Exception:
                pass
            d = t["direction"]; e = t["entry"]
            gross = (exit_px - e) / e * 100 if d == "LONG" else (e - exit_px) / e * 100
            if t.get("partial_done"):
                gross = t["partial_pnl"] + gross * 0.5
            cancel_stops(ex)
            log.info("%s RECONCILE: position closed on exchange (native SL) @ %.4f", COIN, exit_px)
            record_close("SL", exit_px, gross)
            if close_ms:                        # cooldown from actual close time, not reconcile time
                state["cooldown_until"] = (datetime.fromtimestamp(close_ms / 1000, timezone.utc)
                                           + timedelta(hours=COOLDOWN_HOURS)).isoformat()
                save_state()
            return
    df = fetch_candles(ex, SYMBOL, t["entry_tf"], limit=40)
    if df is None or len(df) < 20:
        return
    df["atr"] = calc_atr(df, ATR_LENGTH)
    coin_atr = float(df["atr"].dropna().iloc[-2]) if len(df["atr"].dropna()) >= 2 else t["atr"]
    high = float(df["high"].iloc[-2]); low = float(df["low"].iloc[-2]); price = float(df["close"].iloc[-2])
    state["coin_price"] = round(price, 4)
    d = t["direction"]; e = t["entry"]; qty = t["qty"]
    cur_pnl = (price - e) / e * 100 if d == "LONG" else (e - price) / e * 100
    t["peak_pnl"] = round(max(t.get("peak_pnl", 0), cur_pnl), 3)
    if d == "LONG":
        if low <= t["sl"]:
            pnl = t["partial_pnl"] + (t["sl"] - e) / e * 100 * (0.5 if t["partial_done"] else 1.0)
            reduce(ex, d, qty * (0.5 if t["partial_done"] else 1.0), "SL"); cancel_stops(ex)
            record_close("SL", t["sl"], pnl); return
        if not t["partial_done"] and high >= t["tp1"]:
            t["partial_done"] = True; t["partial_pnl"] = round(t["tp1_pct"] * 0.5, 3)
            t["trail_active"] = True; t["trail_sl"] = round(price - coin_atr * TRAIL_ATR_MULT, 6)
            reduce(ex, d, qty * 0.5, "TP1_PARTIAL")
            log.info("%s TP1 hit @ %.4f — 50%% closed, trail @ %.4f", COIN, t["tp1"], t["trail_sl"]); save_state()
        if t.get("trail_active") and t["trail_sl"] is not None:
            nt = round(price - coin_atr * TRAIL_ATR_MULT, 6)
            if nt > t["trail_sl"]:
                t["trail_sl"] = nt
            if low <= t["trail_sl"]:
                pnl = t["partial_pnl"] + (t["trail_sl"] - e) / e * 100 * 0.5
                reduce(ex, d, qty * 0.5, "TRAIL"); cancel_stops(ex)
                record_close("TRAIL", t["trail_sl"], pnl); return
        elif high >= t["tp2"]:
            pnl = t["partial_pnl"] + t["tp2_pct"] * 0.5
            reduce(ex, d, qty * 0.5, "TP2"); cancel_stops(ex)
            record_close("TP2", t["tp2"], pnl); return
    else:
        if high >= t["sl"]:
            pnl = t["partial_pnl"] + (e - t["sl"]) / e * 100 * (0.5 if t["partial_done"] else 1.0)
            reduce(ex, d, qty * (0.5 if t["partial_done"] else 1.0), "SL"); cancel_stops(ex)
            record_close("SL", t["sl"], pnl); return
        if not t["partial_done"] and low <= t["tp1"]:
            t["partial_done"] = True; t["partial_pnl"] = round(t["tp1_pct"] * 0.5, 3)
            t["trail_active"] = True; t["trail_sl"] = round(price + coin_atr * TRAIL_ATR_MULT, 6)
            reduce(ex, d, qty * 0.5, "TP1_PARTIAL")
            log.info("%s TP1 hit @ %.4f — 50%% closed, trail @ %.4f", COIN, t["tp1"], t["trail_sl"]); save_state()
        if t.get("trail_active") and t["trail_sl"] is not None:
            nt = round(price + coin_atr * TRAIL_ATR_MULT, 6)
            if nt < t["trail_sl"]:
                t["trail_sl"] = nt
            if high >= t["trail_sl"]:
                pnl = t["partial_pnl"] + (e - t["trail_sl"]) / e * 100 * 0.5
                reduce(ex, d, qty * 0.5, "TRAIL"); cancel_stops(ex)
                record_close("TRAIL", t["trail_sl"], pnl); return
        elif low <= t["tp2"]:
            pnl = t["partial_pnl"] + t["tp2_pct"] * 0.5
            reduce(ex, d, qty * 0.5, "TP2"); cancel_stops(ex)
            record_close("TP2", t["tp2"], pnl); return
    save_state()

# ─── LOOPS ────────────────────────────────────────────────────────────────────
def signal_loop():
    while True:
        try:
            state["last_scan"] = datetime.now(timezone.utc).isoformat()
            try_enter(get_exchange())
        except Exception as e:
            log.error("signal_loop: %s", str(e)[:120])
        time.sleep(SCAN_INTERVAL_MIN * 60)

def monitor_loop():
    while True:
        try:
            monitor(get_exchange())
        except Exception as e:
            log.error("monitor_loop: %s", str(e)[:120])
        time.sleep(MONITOR_INTERVAL_MIN * 60)

# ─── STATUS API ───────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route("/status")
def status():
    closed = state["closed_trades"]
    wins = [c for c in closed if (c.get("final_pnl") or 0) > 0]
    losses = [c for c in closed if (c.get("final_pnl") or 0) <= 0]
    gp = sum((c.get("final_pnl") or 0) / 100 * NOTIONAL for c in wins)
    gl = abs(sum((c.get("final_pnl") or 0) / 100 * NOTIONAL for c in losses))
    return jsonify({"strategy": f"liq_zone_{_c}", "coin": COIN, "dry_run": DRY_RUN, "account": ACCOUNT,
                    "venue": VENUE, "symbol": SYMBOL, "notional": NOTIONAL, "timeframes": TIMEFRAMES, "directions": DIRECTIONS,
                    "tps": CFG, "open_trade": state.get("open_trade"), "n_closed": len(closed),
                    "wins": len(wins), "losses": len(losses),
                    "wr": round(len(wins) / len(closed) * 100, 1) if closed else None,
                    "pf": round(gp / gl, 2) if gl > 0 else None, "net_usd": round(gp - gl, 2),
                    "btc_mfi": state.get("btc_mfi"), "coin_price": state.get("coin_price"),
                    "last_scan": state.get("last_scan"), "cooldown_until": state.get("cooldown_until")})

if __name__ == "__main__":
    load_state()
    log.info("Liq-Zone %s trader | DRY_RUN=%s | acct=%s | TFs=%s | DIRS=%s | notional=$%s | port=%s",
             COIN, DRY_RUN, ACCOUNT, TIMEFRAMES, DIRECTIONS, NOTIONAL, PORT)
    threading.Thread(target=signal_loop, daemon=True).start()
    threading.Thread(target=monitor_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
