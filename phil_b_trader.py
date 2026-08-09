#!/usr/bin/env python3
"""
PHIL-B (PFP-1H) — parabolic first-pullback LONG, live micro on Binance USDT-M.

Strategy (frozen = backtested spec, PF 6.94 / n=329 / 730d, philakone_challenge_2026_08_08.md):
  EVENT   daily close >= 1.8x close[t-2] AND day quote-vol >= $10M   -> coin joins watch list 7d
  ENTRY   on CLOSED 1H bars after the event: track running high H; once a bar's low <= H*(1-0.10)
          the pullback is armed with low L (L keeps updating down); enter when a bar closes above
          the prior 3 bars' high AND >= L*1.03. One trade per event.
  EXIT    SL = pullback low L (exchange-native), TP = entry + 2R (exchange-native), 48h time-stop.

This runs hourly at :01 so it acts on the bar that just closed — the same fill the backtest assumed
(next open). Entry state is RECOMPUTED from klines every run (not stored), so a missed run or a
restart cannot corrupt the signal; only "which events were already traded" is persisted.

Rails: DRY_RUN default, $25 risk/trade, max 2 concurrent, cum -10R halt, 4-SL/24h pause, halt file.
Every skipped signal is written to a shadow ledger so the cost of the concurrency cap is measurable.

Binance gotcha honoured: SL/TP are algoType=CONDITIONAL and are INVISIBLE to fetch_open_orders()
unless params={'stop': True} is passed (see 2026-06-01 v11 false-alarm post-mortem).
"""
import json, os, sys, time
from datetime import datetime, timezone

import ccxt
import requests
from dotenv import load_dotenv

load_dotenv("/opt/trader/.env.live")
load_dotenv("/opt/trader/.env.alerts")

STATE = os.getenv("PHIL_B_STATE", "/opt/trader/state/phil_b.json")   # overridable for local dry tests
HALT_FILE = STATE.replace(".json", ".HALT")

# ── frozen config ────────────────────────────────────────────────────────────
TRIG_MULT      = 1.8       # daily close vs close[t-2]
MIN_QVOL       = 10e6      # event-day quote volume
EVENT_WINDOW_H = 7 * 24    # how long an event stays tradeable
PULLBACK       = 0.10      # depth that arms the setup
RESUME_MARGIN  = 1.03      # close must be >=3% off the pullback low
TP_R           = 2.0
TIME_STOP_H    = 48
RISK_USD       = 25.0
MAX_CONCURRENT = 2
MAX_LEVERAGE   = 3
LIQ_BUFFER     = 2.0       # liquidation must sit >=2x the stop distance away (see leverage_for)
MAX_ENTRY_SLIP_R = 0.15    # skip if price ran >15% of the risk distance past the signal close
HALT_CUM_R     = -10.0
SL_BURST_N     = 4         # this many stop-outs in 24h -> pause
SL_BURST_H     = 24
PAUSE_H        = 24

DRY = "--live" not in sys.argv      # live trading requires an EXPLICIT --live flag
TEST = "--test" in sys.argv

EXCLUDE_BASES = {"USDC", "BTCDOM", "DEFI"}


def log(*a):
    print(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), *a, flush=True)


def tg(text):
    tok, chat = os.getenv("TG_BOT_TOKEN"), os.getenv("TG_CHAT_ID")
    if not tok or not chat:
        return
    tag = "[PHIL-B DRY] " if DRY else "[PHIL-B] "
    for _ in range(3):
        try:
            r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                              json={"chat_id": chat, "text": tag + text}, timeout=20)
            if r.ok:
                return
        except Exception:
            time.sleep(3)


def load_state():
    for path in (STATE, STATE + ".bak"):
        if os.path.exists(path):
            try:
                st = json.load(open(path))
                if path == STATE:
                    open(STATE + ".bak", "w").write(json.dumps(st))
                return st
            except Exception:
                continue
    return {}


def save_state(st):
    tmp = STATE + ".tmp"
    json.dump(st, open(tmp, "w"), indent=1)
    os.replace(tmp, STATE)      # atomic


class Trader:
    def __init__(self):
        self.ex = ccxt.binanceusdm({
            "apiKey": os.getenv("BINANCE_LIVE_API_KEY"),
            "secret": os.getenv("BINANCE_LIVE_SECRET"),
            "enableRateLimit": True,
            "timeout": 30000,
        })
        self.ex.load_markets()

    def sym(self, base):
        return f"{base}/USDT:USDT"

    def tradeable(self, base):
        """Crypto-native perp, actively trading, not scheduled for settlement/delisting."""
        s = self.sym(base)
        m = self.ex.markets.get(s)
        if not m or not m.get("active") or not m.get("swap"):
            return False
        info = m.get("info", {})
        if info.get("underlyingType") != "COIN":        # excludes tokenized equities
            return False
        if info.get("status") != "TRADING":
            return False
        dd = float(info.get("deliveryDate") or 0)       # perps sit in year 2100; near date = delisting
        if dd and dd < (time.time() + 30 * 86400) * 1000:
            return False
        return True

    def klines(self, base, tf, limit=200, since=None):
        return self.ex.fetch_ohlcv(self.sym(base), tf, since=since, limit=limit)

    def position(self, base):
        try:
            for p in self.ex.fetch_positions([self.sym(base)]):
                if abs(float(p.get("contracts") or 0)) > 0:
                    return p
        except Exception as e:
            log("position check failed", base, e)
        return None

    @staticmethod
    def leverage_for(risk_pct):
        """Keep liquidation at least LIQ_BUFFER x the stop distance away.

        Stops here are wide (median 19% of price, p90 39%) because the setup risks the whole
        pullback low. Fixed 3x would put liquidation (~33%) INSIDE the stop on wide-stop trades,
        turning a -1R stop into a -1R-plus-fees liquidation. Scale leverage to the actual stop.
        """
        return max(1, min(MAX_LEVERAGE, int(1.0 / (risk_pct * LIQ_BUFFER))))

    def open_long(self, base, sl_price, tp_price, signal_close):
        """Market entry + exchange-native reduce-only SL/TP. Returns fill dict or None."""
        s = self.sym(base)
        px = float(self.ex.fetch_ticker(s)["last"])
        if px <= sl_price:
            return {"skip": "price already at/below stop"}
        risk_dist = signal_close - sl_price
        if (px - signal_close) / risk_dist > MAX_ENTRY_SLIP_R:
            return {"skip": f"price ran {(px-signal_close)/risk_dist:.2f}R past signal"}

        amount = RISK_USD / (px - sl_price)
        amount = float(self.ex.amount_to_precision(s, amount))
        m = self.ex.market(s)
        min_amt = ((m.get("limits", {}).get("amount", {}) or {}).get("min")) or 0
        min_cost = ((m.get("limits", {}).get("cost", {}) or {}).get("min")) or 0
        if amount < min_amt or amount * px < min_cost:
            return {"skip": f"below exchange min (amt {amount} < {min_amt} or notional {amount*px:.1f} < {min_cost})"}

        lev = self.leverage_for((px - sl_price) / px)
        if DRY:
            return {"dry": True, "amount": amount, "entry": px, "lev": lev,
                    "notional": round(amount * px, 2), "margin": round(amount * px / lev, 2),
                    "sl": sl_price, "tp": tp_price}

        try:
            self.ex.set_margin_mode("isolated", s)
        except Exception as e:
            log("margin mode note", base, e)          # already isolated -> Binance errors, harmless
        try:
            self.ex.set_leverage(lev, s)
        except Exception as e:
            log("leverage note", base, e)

        order = self.ex.create_order(s, "market", "buy", amount)
        entry = float(order.get("average") or px)
        # exchange-native exits; closePosition survives our downtime
        self.ex.create_order(s, "STOP_MARKET", "sell", None, None,
                             {"stopPrice": self.ex.price_to_precision(s, sl_price),
                              "closePosition": True})
        self.ex.create_order(s, "TAKE_PROFIT_MARKET", "sell", None, None,
                             {"stopPrice": self.ex.price_to_precision(s, tp_price),
                              "closePosition": True})
        return {"amount": amount, "entry": entry, "notional": round(amount * entry, 2),
                "margin": round(amount * entry / lev, 2), "lev": lev,
                "sl": sl_price, "tp": tp_price, "order_id": order.get("id")}

    def close_market(self, base, amount):
        if DRY:
            return {"dry": True}
        s = self.sym(base)
        try:
            self.ex.cancel_all_orders(s)               # clears the CONDITIONAL SL/TP pair
        except Exception as e:
            log("cancel note", base, e)
        return self.ex.create_order(s, "market", "sell", amount, None, {"reduceOnly": True})


# ── event scan (daily bars) ──────────────────────────────────────────────────
def scan_events(t):
    """Return {BASE: trigger_ms} for coins that went parabolic on a CLOSED daily bar."""
    events = {}
    try:
        tick = requests.get("https://fapi.binance.com/fapi/v1/ticker/24hr", timeout=25).json()
    except Exception as e:
        log("ticker fetch failed", e)
        return events
    cands = [x["symbol"][:-4] for x in tick
             if x["symbol"].endswith("USDT") and float(x.get("quoteVolume") or 0) >= MIN_QVOL]
    for base in cands:
        if base in EXCLUDE_BASES or not t.tradeable(base):
            continue
        try:
            k = t.klines(base, "1d", limit=5)
        except Exception:
            continue
        if len(k) < 4:
            continue
        kc = k[:-1]                                    # closed daily bars only
        last = kc[-1]
        c, c2 = float(last[4]), float(kc[-3][4])
        qvol = float(last[4]) * float(last[5])
        if c2 > 0 and c >= TRIG_MULT * c2 and qvol >= MIN_QVOL:
            events[base] = int(last[0])
    return events


# ── entry logic (1H bars, recomputed each run) ───────────────────────────────
def entry_signal(t, base, trigger_ms):
    """Replay the backtested pullback logic on closed 1H bars. Returns (close, sl) or None."""
    k = t.klines(base, "1h", limit=400, since=trigger_ms)
    kc = [b for b in k[:-1] if b[0] > trigger_ms]      # closed bars strictly after the event bar
    if len(kc) < 5:
        return None
    horizon = trigger_ms + EVENT_WINDOW_H * 3600_000
    H, L, armed = 0.0, None, False
    for i, b in enumerate(kc):
        ts, o, h, l, c = b[0], float(b[1]), float(b[2]), float(b[3]), float(b[4])
        if ts > horizon:
            return None
        H = max(H, h)
        if not armed:
            if l <= H * (1 - PULLBACK):
                armed, L = True, l
            continue
        L = min(L, l)
        prior_high = max(float(x[2]) for x in kc[max(0, i - 3):i]) if i else h
        if c > prior_high and c >= L * RESUME_MARGIN:
            # only act if this is the bar that JUST closed (else the setup already passed)
            if i == len(kc) - 1:
                return c, L
            return None
    return None


def main():
    st = load_state()
    st.setdefault("events", {})
    st.setdefault("traded", {})
    st.setdefault("open", {})
    st.setdefault("ledger", [])
    st.setdefault("shadow", [])
    st.setdefault("paused_until", 0)

    t = Trader()

    if TEST:
        bal = t.ex.fetch_balance().get("USDT", {})
        tg(f"connectivity test ok. USDT free {bal.get('free')}, mode={'DRY' if DRY else 'LIVE'}")
        log("TEST ok. balance:", bal.get("free"), "| DRY:", DRY)
        return

    cum_r = sum(x["R"] for x in st["ledger"])
    halted = os.path.exists(HALT_FILE) or cum_r <= HALT_CUM_R
    now_ms = int(time.time() * 1000)
    paused = now_ms < st.get("paused_until", 0)

    # ── 1. manage open positions (time-stop + reconcile) ────────────────────
    for base in list(st["open"].keys()):
        oc = st["open"][base]
        pos = t.position(base) if not DRY else None
        age_h = (now_ms - oc["entry_ms"]) / 3600_000

        if not DRY and pos is None:
            # exchange-native SL or TP already closed it — classify by last price
            try:
                px = float(t.ex.fetch_ticker(t.sym(base))["last"])
            except Exception:
                px = oc["entry"]
            out = "TP" if px >= oc["tp"] * 0.995 else "SL"
            r = TP_R if out == "TP" else -1.0
            st["ledger"].append({"base": base, "out": out, "R": r, "entry": oc["entry"],
                                 "exit_est": px, "hold_h": round(age_h, 1),
                                 "slip_pct": oc.get("slip_pct"), "regime": oc.get("regime"),
                                 "closed_at": now_ms})
            tg(f"{base} closed {out} ({r:+.1f}R est). hold {age_h:.0f}h. cum {cum_r + r:+.1f}R")
            del st["open"][base]
            continue

        if age_h >= TIME_STOP_H:
            amt = abs(float(pos.get("contracts"))) if pos else oc["amount"]
            try:
                px = float(t.ex.fetch_ticker(t.sym(base))["last"])
                t.close_market(base, amt)
                r = (px - oc["entry"]) / (oc["entry"] - oc["sl"])
                st["ledger"].append({"base": base, "out": "TIME", "R": round(r, 2),
                                     "entry": oc["entry"], "exit_est": px,
                                     "hold_h": round(age_h, 1), "slip_pct": oc.get("slip_pct"),
                                     "regime": oc.get("regime"), "closed_at": now_ms})
                tg(f"{base} TIME-STOP closed at {px:g} ({r:+.1f}R). 48h elapsed.")
                del st["open"][base]
            except Exception as e:
                log("time-stop close failed", base, e)
                tg(f"WARNING {base} time-stop close FAILED: {e}. Manual check needed.")

    # ── 2. refresh event universe once per day ──────────────────────────────
    if now_ms - st.get("events_refreshed", 0) > 12 * 3600_000:
        found = scan_events(t)
        for base, ms in found.items():
            if base not in st["events"]:
                st["events"][base] = ms
                tg(f"EVENT: {base} went parabolic (>={TRIG_MULT}x in 2d). Watching 1H for the first pullback.")
        st["events_refreshed"] = now_ms
    # expire old events
    for base in list(st["events"]):
        if now_ms - st["events"][base] > (EVENT_WINDOW_H + 2) * 3600_000:
            del st["events"][base]

    # ── 3. hunt entries on the bar that just closed ─────────────────────────
    fired = []
    for base, trigger_ms in sorted(st["events"].items()):
        key = f"{base}:{trigger_ms}"
        if key in st["traded"] or base in st["open"]:
            continue
        try:
            sig = entry_signal(t, base, trigger_ms)
        except Exception as e:
            log("signal check failed", base, e)
            continue
        if not sig:
            continue
        close, sl = sig
        tp = close + TP_R * (close - sl)

        # rails: every block still records the signal so we can measure what it cost
        block = None
        if halted:
            block = f"HALTED (cum {cum_r:+.1f}R)"
        elif paused:
            block = "PAUSED (SL burst)"
        elif len(st["open"]) >= MAX_CONCURRENT:
            block = f"concurrency cap {MAX_CONCURRENT}"
        elif not t.tradeable(base):
            block = "not tradeable (delist/status)"
        elif t.position(base):
            block = "position already open on symbol"

        if block:
            st["shadow"].append({"base": base, "ts": now_ms, "close": close, "sl": sl,
                                 "tp": tp, "blocked_by": block})
            log("SHADOW", base, block)
            continue

        res = t.open_long(base, sl, tp, close)
        if res.get("skip"):
            st["shadow"].append({"base": base, "ts": now_ms, "close": close, "sl": sl,
                                 "tp": tp, "blocked_by": res["skip"]})
            log("SKIP", base, res["skip"])
            continue

        entry = res["entry"]
        slip = 100 * (entry / close - 1)
        st["traded"][key] = now_ms
        st["open"][base] = {"entry": entry, "sl": sl, "tp": tp, "amount": res["amount"],
                            "entry_ms": now_ms, "signal_close": close, "slip_pct": round(slip, 3),
                            "trigger_ms": trigger_ms}
        fired.append(base)
        tg(f"LONG {base} @ {entry:g} (signal {close:g}, slip {slip:+.2f}%). "
           f"SL {sl:g} ({100*(entry-sl)/entry:.1f}%), TP {tp:g} (2R). "
           f"Risk ${RISK_USD:.0f}, notional ${res['notional']}. Open {len(st['open'])}/{MAX_CONCURRENT}.")
        log("ENTRY", base, res)

    # ── 4. SL-burst pause ───────────────────────────────────────────────────
    recent_sl = [x for x in st["ledger"]
                 if x["out"] == "SL" and now_ms - x["closed_at"] < SL_BURST_H * 3600_000]
    if len(recent_sl) >= SL_BURST_N and not paused:
        st["paused_until"] = now_ms + PAUSE_H * 3600_000
        tg(f"AUTO-PAUSE {PAUSE_H}h: {len(recent_sl)} stop-outs in {SL_BURST_H}h.")

    if halted and st["ledger"] and cum_r <= HALT_CUM_R:
        tg(f"HALT: cumulative {cum_r:+.1f}R hit the {HALT_CUM_R}R floor. No new entries until reviewed.")

    st["last_run"] = {"ts": now_ms, "events": len(st["events"]), "open": list(st["open"]),
                      "fired": fired, "cum_R": round(cum_r, 2), "n_closed": len(st["ledger"]),
                      "dry": DRY, "halted": halted, "paused": paused}
    save_state(st)
    log("run:", json.dumps(st["last_run"]))


if __name__ == "__main__":
    main()
