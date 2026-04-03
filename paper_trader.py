"""
Virtual Paper Trader — MFI Coin Hunter
───────────────────────────────────────
v1.0  2026-03-12  Baseline: flat sizing, no cooldown, no trailing stop
v1.1  2026-03-19  Rule 1: 24H coin cooldown after loss
                  Rule 2: MFI-tiered position sizing (0.5x / 1x / 1.5x)
                  Rule 3: Trailing stop (BE at +1.5%, lock +1.5% at +3%)
v1.2  2026-03-20  Trail check every 15 min (separate thread from 60-min signal scan)
                  Stage 1 (+1.5% profit): SL → lock +0.5% (covers fees, never a loss)
                  Stage 2 (+3.0% profit): SL → lock +1.0%
                  New result category: BE+ (closed via trail, in positive territory)
v1.3  2026-03-23  Entry aligned with backtest: close of first 2H sub-candle of 2nd
                  reversal 6H candle (was: live price on 3rd candle — up to 12H late)
v1.4  2026-03-25  RR ratio 1.5× → 2.0× (TP 4.5% → 6.0%) — backtest: +39% PnL, PF 2.00
                  Blacklisted PAXG + XAU — ATR 1.1% too low for fixed 3% SL (dead weight)
                  Strategy locked at 7 validated coins pending 30-trade milestone
v1.5  2026-03-26  Rule 4: Partial exit + runner at TP
                  50% exits at TP (6%), 50% rides with 0.5% trailing stop
                  Backtest on 16 real WIN trades: +14.71% → +17.39% (+2.68%)
                  3/16 runners extended to +14–56% after TP hit
                  Blended PnL: (0.5 × TP%) + (0.5 × runner_pnl%)
v1.6  2026-03-26  Optimization pack (backtest validated):
                  MFI threshold 80→85 | SL 3%→2.5% | Body filter 40%→50%
                  Peak-only mode (Mon–Thu only — off-peak 31% WR vs 48% peak)
                  Flat 1× sizing (MFI tiers removed — backward in live data)

Dashboard: http://localhost:8081
Saves:     Trade Logs/paper_trades.json
"""

import ccxt
import json
import os
import threading
import time
import logging
import requests as req_lib
from datetime import datetime, timedelta
from flask import Flask, render_template_string, request, redirect
# API keys not needed — public candle/ticker data only (paper trader, no order execution)

LIVE_TRADER_URL = "http://localhost:8083"   # mirror target

# ─────────────────────────────────────────
# CONFIG  (change here to revert any rule)
# ─────────────────────────────────────────
MFI_LENGTH         = 14
MFI_OVERBOUGHT     = 85       # upgraded from 80 — backtest: off-peak filter alone cut losses 43%
MAX_SL_PCT         = 0.025    # 2.5% stop loss (was 3.0% — tighter risk, RR stays 2.0×)
RR_RATIO           = 2.0      # TP = 6.0% (upgraded from 1.5× — backtest: +39% PnL, PF 2.00, lower drawdown)
MIN_VOLUME_USDT    = 5_000_000
COIN_BLACKLIST     = {"PAXG", "XAU"}   # Low ATR (~1.1%) — fixed 3% SL too wide, rarely reaches 6% TP
SYMBOL_REFRESH_H   = 6
SCAN_INTERVAL_MIN  = 10   # Signal scan: every 10 min (catch 2H candle close within ~10 min)
TRAIL_INTERVAL_MIN = 15   # Trailing stop check: every 15 min (catches fast reversals)
TEST_DURATION_DAYS = 7
BASE_TRADE_SIZE    = 2_000    # Margin per trade ($)
LEVERAGE           = 5

PEAK_DAYS_EST  = [0, 1, 2, 3]   # Mon-Thu
PEAK_HOURS_EST = (0, 23)
PEAK_ONLY      = True           # Block off-peak (Fri–Sun) — live data: 48% WR peak vs 31% off-peak

# ── RULE 1: Coin cooldown after a loss ──────────────────────────
ENABLE_COOLDOWN  = True
COOLDOWN_HOURS   = 24          # set to 0 to disable

# ── RULE 2: MFI-tiered position sizing ──────────────────────────
ENABLE_MFI_TIERS = False   # DISABLED v1.6: live data shows tiers are backward
# Live: MFI 80-89→27% WR / MFI 90-94→14% WR / MFI 95+→0% WR — higher MFI = worse performance
# Flat 1× sizing for all signals
MFI_TIER = [
    (95,  float("inf"), 1.5),
    (90,  94.999,       1.0),
    (80,  89.999,       0.5),
]

# ── RULE 3: Trailing stop for short trades ───────────────────────
ENABLE_TRAIL     = True
TRAIL_TRIGGER_1  = 1.5   # % profit → lock Stage 1
TRAIL_TRIGGER_2  = 3.0   # % profit → lock Stage 2
TRAIL_LOCK_1     = 0.5   # % profit locked at Stage 1 (covers fees — result: BE+)
TRAIL_LOCK_2     = 1.0   # % profit locked at Stage 2 (better return — result: BE+)

# ── RULE 4: Partial exit + runner at TP ──────────────────────────
# Backtest (16 real trades): +14.71% → +17.39% (+2.68%). 3/16 runners hit 14–56%.
ENABLE_RUNNER    = True
TP_PARTIAL_EXIT  = 0.50   # 50% exits at TP, 50% becomes runner
RUNNER_TRAIL_GAP = 0.5    # runner trailing gap in % (tight — backtest: 0.5% optimal)

# ── Rule changelog (for dashboard timeline) ──────────────────────
RULE_CHANGELOG = [
    {
        "version": "v1.0",
        "date":    "2026-03-12",
        "label":   "Baseline (go-live prep)",
        "color":   "#555",
        "rules": [
            "Strategy: MFI Overbought Reversal SHORT on 6H candles",
            "SL: −3% fixed  |  TP: +4.5%  (1.5× RR)",
            "Flat sizing: $10,000 notional per trade (5× leverage)",
            "No coin cooldown  |  No trailing stop",
        ],
    },
    {
        "version": "v1.1",
        "date":    "2026-03-19",
        "label":   "3-Rule Upgrade — 1-week paper test",
        "color":   "#ffd600",
        "rules": [
            "RULE 1 ✅  24H cooldown on same coin after any loss",
            "RULE 2 ✅  MFI 80-89→0.5× | MFI 90-94→1× | MFI 95+→1.5× sizing",
            "RULE 3 ✅  Trailing stop: +1.5% profit → SL to breakeven; +3% → lock +1.5%",
            "Go-live target: 2026-03-26 if performance holds",
        ],
    },
    {
        "version": "v1.2",
        "date":    "2026-03-20",
        "label":   "Trail timing fix + BE+ category",
        "color":   "#00c853",
        "rules": [
            "Trail check every 15 min (separate thread) — was 60 min; fixes Stage 2 miss",
            "Stage 1 (+1.5% profit): SL locks +0.5% profit — covers trading fees, never a loss",
            "Stage 2 (+3.0% profit): SL locks +1.0% profit — guaranteed return",
            "New result: BE+ = closed via trailing stop in positive territory (not full TP, not loss)",
            "Three categories now: WIN (+4.5% TP hit) | BE+ (trail closed, positive) | LOSS (-3% SL hit)",
        ],
    },
    {
        "version": "v1.5",
        "date":    "2026-03-26",
        "label":   "Rule 4: Partial exit + runner at TP",
        "color":   "#ff9800",
        "rules": [
            "At TP hit: 50% of position exits at 6% profit",
            "Remaining 50% rides as runner with 0.5% trailing stop",
            "Blended PnL = (50% × TP%) + (50% × runner_pnl%)",
            "Backtest on 16 real WIN trades: total PnL +14.71% → +17.39% (+2.68%)",
            "3/16 runners extended to +14–56% profit after TP: BTR +56%, BAN +24%, LIGHT +15%",
        ],
    },
    {
        "version": "v1.6",
        "date":    "2026-03-26",
        "label":   "Optimization pack — peak filter + tighter params",
        "color":   "#00bcd4",
        "rules": [
            "PEAK_ONLY ✅  Fri–Sun blocked — peak WR 48% vs off-peak 31% (live data validated)",
            "MFI ≥ 85 ✅  Threshold raised from 80 — filters low-conviction entries",
            "Body ≥ 50% ✅  c1 body ratio raised from 40% — stronger reversal candle required",
            "Flat 1× sizing ✅  MFI tiers disabled — live data showed higher MFI = worse WR",
            "SL 2.5% ✅  Tighter stop from 3.0% — reduces loss size, RR stays at 2.0×",
        ],
    },
]

TRADE_LOG = "/Users/macbook/Downloads/Content Projects/02 - Trading Coin Hunter/Trade Logs/paper_trades.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

exchange = ccxt.binanceusdm({
    "enableRateLimit": True,
    "timeout": 30000,
})

app   = Flask(__name__)
state = {
    "trades":            [],
    "open_trades":       [],
    "last_scan":         "Not yet run",
    "scan_count":        0,
    "start_time":        datetime.now().strftime("%Y-%m-%d %H:%M"),
    "end_time":          (datetime.now() + timedelta(days=TEST_DURATION_DAYS)).strftime("%Y-%m-%d %H:%M"),
    "running":           True,
    "watchlist":         [],
    "watchlist_updated": None,
    "watchlist_count":   0,
    "traded_candles":    {},   # coin → c2_open_ts already traded (prevents same-candle re-entry)
}

# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────
def refresh_watchlist():
    try:
        log.info("Refreshing symbol list from Binance futures...")
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
        log.info(f"Watchlist: {len(filtered)} pairs pass ${MIN_VOLUME_USDT:,} volume filter (excl. {COIN_BLACKLIST}).")
    except Exception as e:
        log.warning(f"Symbol refresh failed: {e}")

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

def save_trades():
    os.makedirs(os.path.dirname(TRADE_LOG), exist_ok=True)
    with open(TRADE_LOG, "w") as f:
        json.dump(state["trades"], f, indent=2, default=str)

def load_trades():
    if os.path.exists(TRADE_LOG):
        with open(TRADE_LOG) as f:
            state["trades"] = json.load(f)
        state["open_trades"] = [t for t in state["trades"] if t["status"] == "OPEN"]

# ─────────────────────────────────────────
# RULE 1 — Coin cooldown after loss
# ─────────────────────────────────────────
def coin_in_cooldown(coin_symbol):
    """Return True if this coin had a loss in the last COOLDOWN_HOURS hours."""
    if not ENABLE_COOLDOWN or COOLDOWN_HOURS == 0:
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

# ─────────────────────────────────────────
# RULE 2 — MFI-tiered sizing
# ─────────────────────────────────────────
def get_size_multiplier(mfi_value):
    """Return (multiplier, label) based on MFI tier config."""
    if not ENABLE_MFI_TIERS:
        return 1.0, "1.0×"
    for lo, hi, mult in MFI_TIER:
        if lo <= mfi_value <= hi:
            label = f"{mult}×"
            return mult, label
    return 1.0, "1.0×"

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

    mfi_peak = round(float(mfi_window.max()), 2)
    mult, mult_label = get_size_multiplier(mfi_peak)
    margin_usd   = round(BASE_TRADE_SIZE * mult, 0)
    position_usd = round(margin_usd * LEVERAGE, 0)

    peak = is_peak_liquidity()
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
        "peak_session":   "Peak 🟢 (Mon–Thu)" if peak else "Off-Peak 🟡 (Fri–Sun)",
        "size_mult":      mult,
        "size_label":     mult_label,
        "margin_usd":     int(margin_usd),
        "position_usd":   int(position_usd),
        "rule_version":   "v1.6",
        "trailing_stage": 0,
        "trail_note":     "",
        "c2_open_ts":     str(c2_open_ts),   # tracks which 6H candle triggered this entry
        # Rule 4: partial exit + runner
        "tp_hit":             False,
        "tp_exit_price":      None,
        "runner_active":      False,
        "runner_trail_stop":  None,
    }

# ─────────────────────────────────────────
# UPDATE OPEN TRADES (1H check + trailing)
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

        # Unrealised PnL (positive = winning short)
        current_pnl = (trade["entry"] - current_price) / trade["entry"] * 100

        # ── RULE 3: Update trailing stop (SHORT trades) ────────────
        if ENABLE_TRAIL:
            stage = trade.get("trailing_stage", 0)
            entry = trade["entry"]

            # Check both stages in sequence — catches fast movers in same update cycle
            if stage == 0 and current_pnl >= TRAIL_TRIGGER_1:
                # Stage 1: lock +0.5% profit (covers fees — categorised as BE+ if hit)
                # For SHORT: SL = entry × (1 - lock%) → price that gives +0.5% if closed there
                lock1_price = round(entry * (1 - TRAIL_LOCK_1 / 100), 6)
                if lock1_price < trade["sl"]:   # only tighten, never loosen
                    trade["sl"]             = lock1_price
                    trade["sl_pct"]         = f"+{TRAIL_LOCK_1}% (BE+)"
                    trade["trailing_stage"] = 1
                    trade["trail_note"]     = f"Stage1: SL→+{TRAIL_LOCK_1}% lock at +{current_pnl:.1f}% profit"
                    log.info(f"TRAIL Stage1: {trade['symbol']} SL locks +{TRAIL_LOCK_1}% ({lock1_price})")
                stage = 1  # allow stage 2 check in same update

            if stage == 1 and current_pnl >= TRAIL_TRIGGER_2:
                # Stage 2: lock +1.0% profit (guaranteed return — still BE+)
                lock2_price = round(entry * (1 - TRAIL_LOCK_2 / 100), 6)
                if lock2_price < trade["sl"]:   # only tighten
                    trade["sl"]             = lock2_price
                    trade["sl_pct"]         = f"+{TRAIL_LOCK_2}% (BE+)"
                    trade["trailing_stage"] = 2
                    trade["trail_note"]     = f"Stage2: SL→+{TRAIL_LOCK_2}% lock at +{current_pnl:.1f}% profit"
                    log.info(f"TRAIL Stage2: {trade['symbol']} SL locks +{TRAIL_LOCK_2}% ({lock2_price})")

        # ── Migrate old trades: add Rule 4 fields if missing ──────
        trade.setdefault("tp_hit", False)
        trade.setdefault("tp_exit_price", None)
        trade.setdefault("runner_active", False)
        trade.setdefault("runner_trail_stop", None)

        # ── RULE 4: Runner trail stop check (after TP already hit) ─
        if ENABLE_RUNNER and trade.get("runner_active"):
            runner_trail = trade["runner_trail_stop"]
            # Runner closes if price rises back above trail stop (SHORT)
            if current_high >= runner_trail:
                runner_exit = runner_trail
                tp_exit     = trade["tp_exit_price"]
                entry       = trade["entry"]
                tp_pnl      = (entry - tp_exit) / entry * 100
                runner_pnl  = (tp_exit - runner_exit) / tp_exit * 100
                blended_pnl = round(TP_PARTIAL_EXIT * tp_pnl + (1 - TP_PARTIAL_EXIT) * runner_pnl, 3)

                result = "WIN" if blended_pnl > 0 else "BE+"
                trade["status"]     = result
                trade["closed_at"]  = datetime.now().strftime("%Y-%m-%d %H:%M")
                trade["result"]     = result
                trade["exit_price"] = round(runner_exit, 6)
                trade["pnl_pct"]    = blended_pnl
                trade["trail_note"] = f"Runner closed: 50%@TP({tp_pnl:+.2f}%) + 50%@trail({runner_pnl:+.2f}%) = blended {blended_pnl:+.2f}%"

                state["open_trades"].remove(trade)
                log.info(f"RUNNER closed: {trade['symbol']} → {result} | Blended PnL: {blended_pnl}%")
            else:
                # Tighten runner trail stop if price extended lower
                best_price = min(current_low, trade.get("current_price", trade["tp"]))
                new_trail  = round(best_price * (1 + RUNNER_TRAIL_GAP / 100), 6)
                if new_trail < runner_trail:
                    trade["runner_trail_stop"] = new_trail
                trade["current_price"]  = round(current_price, 6)
                trade["unrealised_pnl"] = round(current_pnl, 3)
            continue  # runner logic handles this trade, skip SL/TP check below

        # ── Check SL / TP hit ──────────────────────────────────────
        hit_tp = current_low  <= trade["tp"]
        hit_sl = current_high >= trade["sl"]

        if hit_tp or hit_sl:
            if hit_tp and hit_sl:
                result = "WIN" if df_1h.iloc[0]["open"] > trade["tp"] else "LOSS"
            elif hit_tp:
                result = "WIN"
            else:
                result = "LOSS"  # will be reclassified below if SL was trailed into profit

            exit_price = trade["tp"] if result == "WIN" else trade["sl"]
            pnl        = round((trade["entry"] - exit_price) / trade["entry"] * 100, 3)

            # Reclassify: if SL was trailed and exit is in positive territory → BE+
            if result == "LOSS" and pnl > 0.05:
                result = "BE+"

            # ── RULE 4: TP hit → partial exit, activate runner ────
            if ENABLE_RUNNER and result == "WIN" and not trade.get("tp_hit"):
                tp_price = trade["tp"]
                trade["tp_hit"]            = True
                trade["tp_exit_price"]     = tp_price
                trade["runner_active"]     = True
                trade["runner_trail_stop"] = round(tp_price * (1 + RUNNER_TRAIL_GAP / 100), 6)
                trade["trail_note"]        = f"TP hit — 50% exited at {tp_price}, runner active (trail {RUNNER_TRAIL_GAP}%)"
                log.info(f"TP HIT (50% out): {trade['symbol']} @ {tp_price} | runner trail set @ {trade['runner_trail_stop']}")
                # Trade stays open — runner continues
            else:
                trade["status"]     = result
                trade["closed_at"]  = datetime.now().strftime("%Y-%m-%d %H:%M")
                trade["result"]     = result
                trade["exit_price"] = exit_price
                trade["pnl_pct"]    = pnl

                state["open_trades"].remove(trade)
                log.info(f"Trade closed: {trade['symbol']} → {result} | PnL: {pnl}%")

        else:
            trade["current_price"]  = round(current_price, 6)
            trade["unrealised_pnl"] = round(current_pnl, 3)

    save_trades()

# ─────────────────────────────────────────
# MIRROR TO LIVE TRADER
# ─────────────────────────────────────────
def _mirror_to_live(symbol, mfi_peak, entry):
    """Fire-and-forget: tell live trader to open the same position immediately."""
    try:
        resp = req_lib.post(
            f"{LIVE_TRADER_URL}/mirror",
            json={"symbol": symbol, "mfi_peak": mfi_peak, "entry": entry},
            timeout=8
        )
        result = resp.json()
        log.info(f"  Mirror → live: {symbol} | {result.get('status')} — {result.get('msg','')}")
    except Exception as e:
        log.warning(f"  Mirror failed for {symbol}: {e}")

# ─────────────────────────────────────────
# MAIN SCAN
# ─────────────────────────────────────────
def run_scan():
    log.info(f"Scan #{state['scan_count']+1} started...")
    maybe_refresh_watchlist()
    update_open_trades()

    # ── PEAK_ONLY: skip signal scan on Fri–Sun (off-peak 31% WR vs 48% peak) ─
    if PEAK_ONLY and not is_peak_liquidity():
        state["last_scan"]  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        state["scan_count"] += 1
        log.info("Off-peak day (Fri–Sun) — signal scan skipped (PEAK_ONLY=True)")
        return

    new_signals = 0
    for symbol in state["watchlist"]:
        coin = symbol.split("/")[0]

        # Skip if already have open trade on this coin
        if coin in [t["symbol"] for t in state["open_trades"]]:
            continue

        # RULE 1: Skip if coin is in cooldown after a loss
        if coin_in_cooldown(coin):
            log.info(f"  COOLDOWN skip: {coin} — loss within last {COOLDOWN_HOURS}H")
            continue

        signal = check_signal_6h(symbol)
        if signal:
            # Dedup: skip if we already traded this exact 6H candle for this coin
            if state["traded_candles"].get(coin) == signal.get("c2_open_ts"):
                log.info(f"  CANDLE SKIP: {coin} — already traded c2_open_ts {signal['c2_open_ts']}")
                continue

            state["trades"].append(signal)
            state["open_trades"].append(signal)
            state["traded_candles"][coin] = signal["c2_open_ts"]   # lock this candle
            new_signals += 1
            log.info(
                f"NEW ORDER: {signal['symbol']} | Entry:{signal['entry']} "
                f"SL:{signal['sl']} TP:{signal['tp']} | "
                f"MFI:{signal['mfi_peak']} → {signal['size_label']} "
                f"(${signal['position_usd']:,} notional)"
            )
            # Mirror to live trader instantly — no scan delay
            threading.Thread(
                target=_mirror_to_live,
                args=(symbol, signal["mfi_peak"], signal["entry"]),
                daemon=True
            ).start()

        time.sleep(0.2)

    state["last_scan"]  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state["scan_count"] += 1
    save_trades()
    log.info(f"Scan done. {new_signals} new orders. {len(state['open_trades'])} open.")

def trail_runner():
    """Dedicated thread: checks trailing stops every 15 min independently of signal scan."""
    end_time = datetime.now() + timedelta(days=TEST_DURATION_DAYS)
    # Stagger start by 5 min so it doesn't overlap with initial scan
    time.sleep(5 * 60)
    while datetime.now() < end_time and state["running"]:
        if state["open_trades"]:
            log.info(f"Trail check: {len(state['open_trades'])} open trades...")
            update_open_trades()
        time.sleep(TRAIL_INTERVAL_MIN * 60)

def background_runner():
    """Main scan thread: checks for new signals every 60 min."""
    end_time = datetime.now() + timedelta(days=TEST_DURATION_DAYS)
    while datetime.now() < end_time and state["running"]:
        run_scan()
        time.sleep(SCAN_INTERVAL_MIN * 60)
    state["running"] = False
    log.info("Paper trading test complete.")

# ─────────────────────────────────────────
# HTML DASHBOARD
# ─────────────────────────────────────────
HTML = r"""
<!DOCTYPE html>
<html>
<head>
    <title>MFI Coin Hunter — Paper Trader</title>
    <meta http-equiv="refresh" content="120">
    <style>
        * { margin:0; padding:0; box-sizing:border-box; }
        body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
               background:#0d0d0d; color:#e0e0e0; padding:30px; }
        h1   { font-size:24px; color:#fff; margin-bottom:4px; }
        .sub { color:#666; font-size:13px; margin-bottom:22px; }

        /* ── Stat boxes ─────────────────────────────── */
        .stats { display:flex; gap:14px; margin-bottom:24px; flex-wrap:wrap; }
        .stat-box { background:#1a1a1a; border:1px solid #2a2a2a; border-radius:10px;
                    padding:14px 20px; min-width:130px; }
        .stat-box .label { font-size:11px; color:#555; text-transform:uppercase; letter-spacing:1px; }
        .stat-box .value { font-size:20px; font-weight:700; color:#fff; margin-top:4px; }
        .green .value { color:#00c853; }
        .red   .value { color:#ff1744; }
        .blue  .value { color:#1e88e5; }
        .gold  .value { color:#ffd600; }

        h2 { font-size:15px; color:#aaa; margin:26px 0 12px; text-transform:uppercase; letter-spacing:1px; }

        /* ── Tables ─────────────────────────────────── */
        table { width:100%; border-collapse:collapse; background:#1a1a1a;
                border-radius:10px; overflow:hidden; margin-bottom:28px; }
        th { background:#222; padding:11px 14px; text-align:left; font-size:11px;
             color:#666; text-transform:uppercase; letter-spacing:1px; }
        td { padding:11px 14px; border-top:1px solid #222; font-size:13px; }
        tr:hover td { background:#1e1e1e; }

        .coin    { font-weight:700; color:#fff; font-size:15px; }
        .win     { color:#00c853; font-weight:600; }
        .loss    { color:#ff1744; font-weight:600; }
        .open    { color:#1e88e5; font-weight:600; }
        .sl-val  { color:#ff6f00; }
        .tp-val  { color:#00c853; }
        .mfi     { color:#ff1744; }
        .pnl-pos { color:#00c853; font-weight:600; }
        .pnl-neg { color:#ff1744; font-weight:600; }
        .usd-pos { color:#00c853; font-weight:600; font-size:12px; }
        .usd-neg { color:#ff1744; font-weight:600; font-size:12px; }
        .tv-link { color:#1e88e5; text-decoration:none; font-size:12px; }

        /* ── Badges ─────────────────────────────────── */
        .badge { display:inline-block; padding:3px 9px; border-radius:20px; font-size:11px; font-weight:600; }
        .badge-open   { background:#1e3a5f; color:#1e88e5; }
        .badge-win    { background:#1b3a2a; color:#00c853; }
        .badge-loss   { background:#3a1b1b; color:#ff1744; }
        .badge-manual { background:#2a2a00; color:#ffd600; }
        .badge-be\+   { background:#1a2a00; color:#aaff00; }
        .beplus .value { color:#aaff00; }

        .size-badge { display:inline-block; padding:2px 7px; border-radius:5px; font-size:11px; font-weight:700; }
        .size-half  { background:#1a1a00; color:#ffd600; border:1px solid #555; }
        .size-full  { background:#001a00; color:#00c853; border:1px solid #00c853; }
        .size-over  { background:#1a0040; color:#b57bee; border:1px solid #b57bee; }

        .trail-badge { display:inline-block; padding:2px 7px; border-radius:5px; font-size:10px; font-weight:600; }
        .trail-0 { color:#555; }
        .trail-1 { background:#001a0d; color:#00c853; border:1px solid #00c853; }
        .trail-2 { background:#004d1a; color:#00ff88; border:1px solid #00ff88; }

        .lev-badge { display:inline-block; background:#1a1f00; color:#ffd600; border:1px solid #ffd600;
                     padding:2px 7px; border-radius:5px; font-size:11px; font-weight:700; margin-left:5px; }

        /* ── Strategy box ───────────────────────────── */
        .strategy-box { background:#141414; border:1px solid #2a2a2a; border-radius:12px;
                        padding:16px 22px; margin-bottom:22px; }
        .strategy-box h3 { font-size:12px; color:#ffd600; text-transform:uppercase; letter-spacing:1px; margin-bottom:10px; }
        .strategy-grid { display:flex; gap:28px; flex-wrap:wrap; }
        .sg { font-size:12px; color:#aaa; line-height:2; }
        .sg span { color:#fff; font-weight:600; }

        /* ── Rule Changelog Timeline ────────────────── */
        .changelog { background:#0f0f0f; border:1px solid #252525; border-radius:12px;
                     padding:18px 22px; margin-bottom:24px; }
        .changelog-header { display:flex; align-items:center; gap:12px; margin-bottom:14px;
                            cursor:pointer; user-select:none; }
        .changelog-header h3 { font-size:12px; color:#888; text-transform:uppercase; letter-spacing:1.2px; }
        .changelog-header .toggle-icon { color:#555; font-size:12px; transition:transform 0.2s; }
        .changelog-body { display:flex; flex-direction:column; gap:0; }
        .cl-entry { display:flex; gap:0; }
        .cl-line  { display:flex; flex-direction:column; align-items:center; width:40px; flex-shrink:0; }
        .cl-dot   { width:12px; height:12px; border-radius:50%; flex-shrink:0; margin-top:3px; }
        .cl-stem  { width:2px; flex:1; background:#222; margin-top:4px; }
        .cl-content { padding:0 0 20px 14px; flex:1; }
        .cl-ver   { font-size:11px; font-weight:700; letter-spacing:1px; }
        .cl-date  { font-size:11px; color:#555; margin-left:8px; }
        .cl-label { font-size:13px; color:#ccc; font-weight:600; margin:3px 0 6px; }
        .cl-rules { list-style:none; padding:0; }
        .cl-rules li { font-size:12px; color:#777; line-height:1.8; }
        .cl-rules li::before { content:"• "; color:#444; }
        .cl-entry.active .cl-rules li { color:#aaa; }
        .cl-entry.active .cl-label   { color:#fff; }

        /* ── Buttons ────────────────────────────────── */
        .btn-close { background:#1a0000; color:#ff5252; border:1px solid #ff1744;
                     padding:4px 10px; border-radius:6px; font-size:11px; font-weight:600; cursor:pointer; }
        .btn-close:hover { background:#ff1744; color:#fff; }
        .btn-edit  { background:#001a2a; color:#40c4ff; border:1px solid #1e88e5;
                     padding:4px 10px; border-radius:6px; font-size:11px; font-weight:600; cursor:pointer; margin-right:4px; }
        .btn-edit:hover { background:#1e88e5; color:#fff; }

        /* ── Modal ──────────────────────────────────── */
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

        /* ── Learning Insights ───────────────────────── */
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

        /* ── Rule pills ──────────────────────────────── */
        .rule-row { display:flex; gap:10px; margin-bottom:22px; flex-wrap:wrap; }
        .rule-pill { display:flex; align-items:center; gap:6px; background:#111; border:1px solid #2a2a2a;
                     border-radius:20px; padding:6px 14px; font-size:12px; }
        .rule-pill.on  { border-color:#00c853; }
        .rule-pill.off { border-color:#333; color:#444; }
        .pill-icon { font-size:13px; }
        .pill-label { color:#aaa; }
        .rule-pill.on .pill-label { color:#ccc; }

        /* ── Goal Tracker ────────────────────────────── */
        .goal-box { background:#0a1a10; border:1px solid #1b5e20; border-radius:12px;
                    padding:18px 22px; margin-bottom:22px; }
        .goal-box-title { font-size:12px; color:#69f0ae; text-transform:uppercase;
                          letter-spacing:1.2px; font-weight:700; margin-bottom:14px; }
        .goal-grid { display:flex; gap:14px; flex-wrap:wrap; margin-bottom:16px; }
        .goal-card { background:#0d1f12; border:1px solid #1e3a25; border-radius:8px;
                     padding:12px 16px; min-width:140px; flex:1; }
        .goal-card .gc-label { font-size:10px; color:#555; text-transform:uppercase;
                               letter-spacing:1px; margin-bottom:6px; }
        .goal-card .gc-current { font-size:22px; font-weight:700; }
        .goal-card .gc-target  { font-size:11px; color:#555; margin-top:3px; }
        .goal-card .gc-target em { color:#aaa; font-style:normal; }
        .gc-green  { color:#00c853; }
        .gc-yellow { color:#ffd600; }
        .gc-red    { color:#ff1744; }
        .gc-grey   { color:#555; }

        .goal-bar-row { margin-bottom:10px; }
        .goal-bar-label { display:flex; justify-content:space-between;
                          font-size:11px; color:#666; margin-bottom:4px; }
        .goal-bar-label span { color:#aaa; font-weight:600; }
        .goal-bar-track { height:6px; background:#1a1a1a; border-radius:3px; overflow:hidden; }
        .goal-bar-fill  { height:100%; border-radius:3px; transition:width 0.5s; }
        .goal-status { font-size:12px; color:#555; margin-top:12px; border-top:1px solid #1a2a1a;
                       padding-top:10px; display:flex; gap:20px; flex-wrap:wrap; }
        .goal-status span { color:#aaa; }
        .goal-status strong { color:#fff; }
    </style>
</head>
<body>
    <h1>
        {% if running %}<span class="running-dot"></span>{% endif %}
        MFI Coin Hunter <span style="color:#ffd600;font-size:18px;">— Paper Trader</span>
        <span style="color:#555;font-size:14px;margin-left:12px;">v1.2 rules active</span>
    </h1>
    <p class="sub">
        Binance USDT Futures · {{ watchlist_count }} Liquid Pairs (≥${{ min_vol_m }}M vol) ·
        {{ start_time }} → {{ end_time }} &nbsp;·&nbsp;
        <span style="color:#ffd600;">Go-live: 2026-03-26</span>
    </p>

    <!-- Active Rules Indicator -->
    <div class="rule-row">
        <div class="rule-pill {{ 'on' if rule1_on else 'off' }}">
            <span class="pill-icon">{{ '🟢' if rule1_on else '⚪' }}</span>
            <span class="pill-label">Rule 1: 24H Cooldown ({{ cooldown_h }}H)</span>
        </div>
        <div class="rule-pill {{ 'on' if rule2_on else 'off' }}">
            <span class="pill-icon">{{ '🟢' if rule2_on else '⚪' }}</span>
            <span class="pill-label">Rule 2: MFI Sizing (0.5× / 1× / 1.5×)</span>
        </div>
        <div class="rule-pill {{ 'on' if rule3_on else 'off' }}">
            <span class="pill-icon">{{ '🟢' if rule3_on else '⚪' }}</span>
            <span class="pill-label">Rule 3: Trail — every {{ trail_interval }}min | +1.5%→lock+0.5% | +3%→lock+1.0% | result: BE+</span>
        </div>
    </div>

    <!-- Goal Tracker -->
    <div class="goal-box">
        <div class="goal-box-title">🎯 Goal Tracker — MFI Coin Hunter (v1.6)</div>
        <div class="goal-grid">
            <div class="goal-card">
                <div class="gc-label">Monthly ROI Target</div>
                <div class="gc-current gc-yellow">3 – 8%</div>
                <div class="gc-target">per month on capital<br><em>market-condition dependent</em></div>
            </div>
            <div class="goal-card">
                <div class="gc-label">Current Win Rate</div>
                <div class="gc-current {{ 'gc-green' if win_rate >= 45 else ('gc-yellow' if win_rate >= 35 else 'gc-red') }}">{{ win_rate }}%</div>
                <div class="gc-target">Target: <em>≥ 45%</em> &nbsp;|&nbsp; Min: <em>35%</em></div>
            </div>
            <div class="goal-card">
                <div class="gc-label">Profit Factor</div>
                <div class="gc-current {{ 'gc-green' if profit_factor >= 1.5 else ('gc-yellow' if profit_factor >= 1.0 else 'gc-red') }}">{{ profit_factor if profit_factor else '—' }}</div>
                <div class="gc-target">Target: <em>≥ 1.5</em> &nbsp;|&nbsp; Min: <em>≥ 1.0</em></div>
            </div>
            <div class="goal-card">
                <div class="gc-label">Monthly Pace</div>
                <div class="gc-current gc-yellow">{{ monthly_pace }}</div>
                <div class="gc-target">trades/month at current rate<br><em>{{ days_run }}d running · {{ closed_total }} closed</em></div>
            </div>
            <div class="goal-card">
                <div class="gc-label">Est. Monthly EV</div>
                {% if monthly_ev > 0 %}
                <div class="gc-current gc-green">+{{ monthly_ev }}%</div>
                {% elif monthly_ev < 0 %}
                <div class="gc-current gc-red">{{ monthly_ev }}%</div>
                {% else %}
                <div class="gc-current gc-grey">—</div>
                {% endif %}
                <div class="gc-target">avg/trade × pace &nbsp;|&nbsp; Goal: <em>+3–8%</em></div>
            </div>
        </div>

        <!-- Progress bars -->
        <div class="goal-bar-row">
            <div class="goal-bar-label">
                <div>Win Rate &nbsp;<span>{{ win_rate }}%</span></div>
                <div>Target 45%</div>
            </div>
            <div class="goal-bar-track">
                <div class="goal-bar-fill" style="width:{{ [win_rate/45*100, 100]|min }}%; background:{{ '#00c853' if win_rate >= 45 else ('#ffd600' if win_rate >= 35 else '#ff1744') }};"></div>
            </div>
        </div>
        <div class="goal-bar-row">
            <div class="goal-bar-label">
                <div>Profit Factor &nbsp;<span>{{ profit_factor }}</span></div>
                <div>Target 1.5</div>
            </div>
            <div class="goal-bar-track">
                <div class="goal-bar-fill" style="width:{{ [profit_factor/1.5*100, 100]|min if profit_factor else 0 }}%; background:{{ '#00c853' if profit_factor >= 1.5 else ('#ffd600' if profit_factor >= 1.0 else '#ff1744') }};"></div>
            </div>
        </div>
        <div class="goal-bar-row">
            <div class="goal-bar-label">
                <div>Monthly EV &nbsp;<span>{{ monthly_ev }}%</span></div>
                <div>Target +3–8%</div>
            </div>
            <div class="goal-bar-track">
                <div class="goal-bar-fill" style="width:{{ [monthly_ev/8*100, 100]|min if monthly_ev > 0 else 0 }}%; background:{{ '#00c853' if monthly_ev >= 3 else ('#ffd600' if monthly_ev > 0 else '#ff1744') }};"></div>
            </div>
        </div>

        <div class="goal-status">
            <div>Capital/trade: <strong>${{ "{:,}".format(base_size) }} margin · ${{ "{:,}".format(base_size * leverage) }} notional ({{ leverage }}×)</strong></div>
            <div>Filters active: <strong>MFI ≥ 85 · SL 2.5% · Body ≥ 50% · Peak only (Mon–Thu)</strong></div>
            <div>Strategy ceiling: <strong>~5–8%/month in bearish conditions · Lower in neutral markets</strong></div>
        </div>
    </div>

    <!-- Strategy Summary -->
    <div class="strategy-box">
        <h3>⚡ Strategy: MFI Overbought Reversal (6H) — v1.1 Rules</h3>
        <div class="strategy-grid">
            <div class="sg">
                <div>Direction &nbsp;&nbsp;<span>SHORT only</span></div>
                <div>Signal &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;<span>MFI &gt; 80 → 2 quality red candles (6H)</span></div>
                <div>SL &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;<span>+3%</span> above entry</div>
                <div>TP &nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;<span>−4.5%</span> below entry (1.5× RR)</div>
            </div>
            <div class="sg">
                <div>R1 Cooldown &nbsp;<span>{{ cooldown_h }}H</span> after any loss on same coin</div>
                <div>R2 Low tier &nbsp;&nbsp;<span>MFI 80-89 → 0.5× (${{ "{:,}".format(base_size * 5 // 2) }})</span></div>
                <div>R2 Mid tier &nbsp;&nbsp;<span>MFI 90-94 → 1.0× (${{ "{:,}".format(base_size * 5) }})</span></div>
                <div>R2 High tier &nbsp;<span>MFI 95+  → 1.5× (${{ "{:,}".format(base_size * 5 * 3 // 2) }})</span></div>
            </div>
            <div class="sg">
                <div>R3 Stage 1 &nbsp;&nbsp;<span>+1.5% profit → SL → BE+0.1% buffer</span></div>
                <div>R3 Stage 2 &nbsp;&nbsp;<span>+3.0% profit → SL → Lock +1.5%</span></div>
                <div>Trail check &nbsp;<span>every {{ trail_interval }} min</span> (signal scan: 60 min)</div>
                <div>Leverage &nbsp;&nbsp;&nbsp;&nbsp;<span>5×</span><span class="lev-badge">5×</span></div>
                <div>Backtest WR &nbsp;<span>68–71%</span></div>
            </div>
        </div>
    </div>

    <!-- Rule Changelog Timeline -->
    <div class="changelog">
        <div class="changelog-header" onclick="toggleChangelog()">
            <h3>📋 Rule Changelog / Version History</h3>
            <span class="toggle-icon" id="chev">▼ expand</span>
        </div>
        <div class="changelog-body" id="changelogBody" style="display:none;">
        {% for entry in changelog %}
            <div class="cl-entry {{ 'active' if loop.last else '' }}">
                <div class="cl-line">
                    <div class="cl-dot" style="background:{{ entry.color }};"></div>
                    {% if not loop.last %}<div class="cl-stem"></div>{% endif %}
                </div>
                <div class="cl-content">
                    <div>
                        <span class="cl-ver" style="color:{{ entry.color }};">{{ entry.version }}</span>
                        <span class="cl-date">{{ entry.date }}</span>
                    </div>
                    <div class="cl-label">{{ entry.label }}</div>
                    <ul class="cl-rules">
                        {% for rule in entry.rules %}
                        <li>{{ rule }}</li>
                        {% endfor %}
                    </ul>
                </div>
            </div>
        {% endfor %}
        </div>
    </div>

    <!-- Learning Insights -->
    {% if insights %}
    <div class="insights-box">
        <h3>🧠 Learning Insights — Milestone {{ insights.milestone }} Trades</h3>
        <div class="insights-meta">
            <span>Trades analysed: <strong>{{ insights.total }}</strong></span>
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
                <h4>Session Breakdown</h4>
                {% for sname, s in insights.session_stats.items() %}
                {% set total = s.w + s.be + s.l %}
                {% if total > 0 %}
                <div class="ig-row">
                    {{ sname }}
                    <span>{{ s.w }}W {{ s.be }}BE {{ s.l }}L
                    ({{ ((s.w + s.be) / total * 100) | round | int }}%)</span>
                </div>
                {% endif %}
                {% endfor %}
            </div>
            <div class="ig-box">
                <h4>MFI Bucket Win Rate</h4>
                {% for bucket, data in insights.mfi_buckets.items() %}
                {% if data[0] > 0 %}
                <div class="ig-row">
                    MFI {{ bucket }} ({{ data[0] }} trades)
                    <span>{{ data[1] | int if data[1] else '—' }}%</span>
                </div>
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
        <div class="stat-box {{ 'green' if win_rate >= 60 else 'red' }}">
            <div class="label">Win+BE+ Rate</div>
            <div class="value">{{ win_rate }}%</div>
        </div>
        <div class="stat-box {{ 'green' if profit_factor >= 2 else ('blue' if profit_factor >= 1 else 'red') }}">
            <div class="label">Profit Factor</div>
            <div class="value">{{ profit_factor }}</div>
        </div>
        <div class="stat-box {{ 'green' if total_pnl_usd >= 0 else 'red' }}">
            <div class="label">Realised PnL $</div>
            <div class="value">{{ '+' if total_pnl_usd >= 0 else '' }}${{ "${:,.0f}".format(total_pnl_usd) }}</div>
        </div>
        <div class="stat-box {{ 'green' if unrealised_usd >= 0 else 'red' }}">
            <div class="label">Unrealised PnL $</div>
            <div class="value">{{ '+' if unrealised_usd >= 0 else '' }}${{ "${:,.0f}".format(unrealised_usd) }}</div>
        </div>
        <div class="stat-box {{ 'green' if (total_pnl_usd + unrealised_usd) >= 0 else 'red' }}">
            <div class="label">Total PnL $</div>
            <div class="value">{{ '+' if (total_pnl_usd + unrealised_usd) >= 0 else '' }}${{ "${:,.0f}".format(total_pnl_usd + unrealised_usd) }}</div>
        </div>
        <div class="stat-box blue">
            <div class="label">Pairs Scanning</div>
            <div class="value">{{ watchlist_count }}</div>
        </div>
        <div class="stat-box">
            <div class="label">Last Scan</div>
            <div class="value" style="font-size:12px;margin-top:6px;">{{ last_scan }}</div>
        </div>
    </div>

    {% if open_trades %}
    <h2>🔵 Open Virtual Orders ({{ open_trades | length }})</h2>
    <table>
        <thead><tr>
            <th>Coin</th><th>Session</th><th>Entry</th>
            <th>Stop Loss</th><th>Take Profit</th>
            <th>Size Tier</th><th>Position $</th>
            <th>MFI</th><th>Trailing</th>
            <th>Unrealised %</th><th>USD PnL</th>
            <th>Opened</th><th>Chart</th><th>Action</th>
        </tr></thead>
        <tbody>
        {% for t in open_trades %}
        <tr>
            <td class="coin">{{ t.symbol }}</td>
            <td style="font-size:12px;">{{ t.peak_session or '—' }}</td>
            <td>{{ t.entry }}</td>
            <td class="sl-val">
                {{ t.sl }}
                <br><small style="color:#555;">{{ t.sl_pct }}</small>
            </td>
            <td class="tp-val">{{ t.tp }} <small>({{ t.tp_pct }})</small></td>
            <td>
                {% set mult = t.size_mult or 1.0 %}
                {% if mult >= 1.5 %}
                    <span class="size-badge size-over">1.5×</span>
                {% elif mult <= 0.5 %}
                    <span class="size-badge size-half">0.5×</span>
                {% else %}
                    <span class="size-badge size-full">1.0×</span>
                {% endif %}
            </td>
            <td style="color:#ffd600;">${{ "{:,}".format(t.position_usd or (base_size * leverage)) }}</td>
            <td class="mfi">{{ t.mfi_peak }}</td>
            <td>
                {% set stage = t.trailing_stage or 0 %}
                {% if stage == 2 %}
                    <span class="trail-badge trail-2">🔒 +1.5%</span>
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
                {% set pos = t.position_usd or (base_size * leverage) %}
                {{ '+' if (t.unrealised_pnl or 0) >= 0 else '' }}${{ "${:,.0f}".format((t.unrealised_pnl or 0) / 100 * pos) }}
            </td>
            <td style="color:#555;font-size:12px;">{{ t.opened_at }}</td>
            <td><a class="tv-link" href="https://www.tradingview.com/chart/?symbol=BINANCE:{{ t.symbol }}USDT.P&interval=360" target="_blank">6H →</a></td>
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
                <div class="modal-hint">Current SL shown above. Enter new price level.</div>
                <label>Take Profit (price)</label>
                <input type="number" name="new_tp" id="modalTP" step="any" min="0.000001" required>
                <div class="modal-hint">Current TP shown above. Enter new price level.</div>
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
        function toggleChangelog() {
            var body = document.getElementById('changelogBody');
            var chev = document.getElementById('chev');
            if (body.style.display === 'none') {
                body.style.display = 'flex'; body.style.flexDirection = 'column';
                chev.textContent = '▲ collapse';
            } else {
                body.style.display = 'none';
                chev.textContent = '▼ expand';
            }
        }
    </script>

    {% if closed_trades %}
    <h2>📋 Closed Trades ({{ closed_trades | length }})</h2>
    <table>
        <thead><tr>
            <th>Coin</th><th>Result</th><th>Session</th><th>Ver</th>
            <th>Size</th><th>Entry</th><th>Exit</th>
            <th>PnL %</th><th>PnL $</th>
            <th>Trail Note</th><th>Opened</th><th>Closed</th>
        </tr></thead>
        <tbody>
        {% for t in closed_trades | reverse %}
        <tr>
            <td class="coin">{{ t.symbol }}</td>
            <td><span class="badge badge-{{ 'manual' if t.result == 'MANUAL' else t.result | lower | replace('+','plus') }}">{{ t.result }}</span></td>
            <td style="font-size:11px;">{{ t.peak_session or '—' }}</td>
            <td style="font-size:11px;color:#555;">{{ t.rule_version or 'v1.0' }}</td>
            <td>
                {% set mult = t.size_mult or 1.0 %}
                {% if mult >= 1.5 %}
                    <span class="size-badge size-over">1.5×</span>
                {% elif mult <= 0.5 %}
                    <span class="size-badge size-half">0.5×</span>
                {% else %}
                    <span class="size-badge size-full">1×</span>
                {% endif %}
            </td>
            <td>{{ t.entry }}</td>
            <td>{{ t.exit_price }}</td>
            <td class="{{ 'pnl-pos' if t.pnl_pct >= 0 else 'pnl-neg' }}">
                {{ '+' if t.pnl_pct >= 0 else '' }}{{ t.pnl_pct }}%
            </td>
            <td class="{{ 'usd-pos' if t.pnl_pct >= 0 else 'usd-neg' }}">
                {% set pos = t.position_usd or (base_size * leverage) %}
                {{ '+' if t.pnl_pct >= 0 else '' }}${{ "${:,.0f}".format(t.pnl_pct / 100 * pos) }}
            </td>
            <td style="font-size:11px;color:#555;">{{ t.trail_note or '—' }}</td>
            <td style="color:#555;font-size:11px;">{{ t.opened_at }}</td>
            <td style="color:#555;font-size:11px;">{{ t.closed_at }}</td>
        </tr>
        {% endfor %}
        </tbody>
    </table>
    {% endif %}

    {% if not open_trades and not closed_trades %}
    <div class="empty">⏳ Waiting for first signal... Refreshes every 2 min.</div>
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
        trade["sl_pct"] = f"+{round(abs(trade['entry']-new_sl)/trade['entry']*100,1)}% (manual)"
        trade["tp_pct"] = f"-{round(abs(trade['entry']-new_tp)/trade['entry']*100,1)}% (manual)"
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
        pnl        = round((trade["entry"] - exit_price) / trade["entry"] * 100, 3)
        result     = "WIN" if pnl > 1.0 else "LOSS"
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
    """Generate learning recommendations at 3/6/9/12... trade milestones."""
    n = len(closed)
    if n < 3:
        return None

    milestone = (n // 3) * 3   # 3, 6, 9, 12 ...

    wins   = [t for t in closed if t.get("result") == "WIN"]
    bes    = [t for t in closed if t.get("result") == "BE+"]
    losses = [t for t in closed if t.get("result") == "LOSS"]
    decided = len(wins) + len(bes) + len(losses)
    win_rate = round((len(wins) + len(bes)) / decided * 100, 1) if decided else 0

    gross_win  = sum(t.get("pnl_pct", 0) or 0 for t in wins + bes)
    gross_loss = sum(abs(t.get("pnl_pct", 0) or 0) for t in losses)
    pf = round(gross_win / gross_loss, 2) if gross_loss > 0 else None

    # Session breakdown
    session_stats = {}
    for t in closed:
        s = t.get("peak_session") or "Off-Peak"
        if s not in session_stats:
            session_stats[s] = {"w": 0, "l": 0, "be": 0}
        r = t.get("result", "")
        if r == "WIN":   session_stats[s]["w"]  += 1
        elif r == "BE+": session_stats[s]["be"] += 1
        else:            session_stats[s]["l"]  += 1

    # MFI bucket breakdown
    def mfi_wr(bucket):
        if not bucket: return None
        pos = sum(1 for t in bucket if t.get("result") in ("WIN","BE+"))
        return round(pos / len(bucket) * 100, 0)

    mfi_low  = [t for t in closed if 80 <= (t.get("mfi_peak") or 0) < 85]
    mfi_mid  = [t for t in closed if 85 <= (t.get("mfi_peak") or 0) < 92]
    mfi_high = [t for t in closed if (t.get("mfi_peak") or 0) >= 92]

    # Coin repeat losses
    loss_coins = {}
    for t in losses:
        c = t.get("symbol", "?")
        loss_coins[c] = loss_coins.get(c, 0) + 1
    repeat_losers = [c for c, cnt in loss_coins.items() if cnt >= 2]

    # Avg win / avg loss %
    avg_win  = round(sum(t.get("pnl_pct",0) or 0 for t in wins)  / len(wins),  2) if wins   else 0
    avg_loss = round(sum(t.get("pnl_pct",0) or 0 for t in losses) / len(losses),2) if losses else 0

    # Build recommendations
    recs = []

    if win_rate >= 70:
        recs.append(("green", f"Win+BE rate {win_rate}% — strategy solid. Consider scaling position size."))
    elif win_rate >= 50:
        recs.append(("yellow", f"Win+BE rate {win_rate}% — acceptable. Watch for pattern improvements below."))
    else:
        recs.append(("red", f"Win+BE rate {win_rate}% — below 50%. Avoid adding real capital yet."))

    if pf and pf >= 2.0:
        recs.append(("green", f"Profit factor {pf} — excellent. Strategy paying well per risk unit."))
    elif pf and pf < 1.0:
        recs.append(("red", f"Profit factor {pf} — losing money overall. Check SL/TP ratio."))

    # Session insights
    for sname, s in session_stats.items():
        total = s["w"] + s["be"] + s["l"]
        if total < 2: continue
        swr = round((s["w"] + s["be"]) / total * 100)
        if swr == 0:
            recs.append(("red",    f"{sname}: 0% win rate ({total} trades) — consider avoiding this session."))
        elif swr >= 80:
            recs.append(("green",  f"{sname}: {swr}% win rate ({total} trades) — best session, prioritise it."))
        elif swr <= 35:
            recs.append(("yellow", f"{sname}: {swr}% win rate ({total} trades) — weak session, be cautious."))

    # MFI threshold insights
    wr_low  = mfi_wr(mfi_low)
    wr_mid  = mfi_wr(mfi_mid)
    wr_high = mfi_wr(mfi_high)
    if wr_low is not None and wr_high is not None and wr_high > wr_low + 20:
        recs.append(("yellow", f"MFI 92+ win rate {wr_high:.0f}% vs MFI 80-85 win rate {wr_low:.0f}% — consider raising entry threshold to MFI 85+."))
    if wr_low is not None and wr_low >= 70:
        recs.append(("green", f"MFI 80-85 still performing ({wr_low:.0f}%) — keep threshold at 80."))

    # Repeat losers
    if repeat_losers:
        recs.append(("red", f"Repeat losing coins: {', '.join(repeat_losers)} — consider blacklisting or extending cooldown."))

    # Win vs loss size
    if wins and losses and abs(avg_loss) > avg_win:
        recs.append(("yellow", f"Avg win {avg_win}% < avg loss {abs(avg_loss)}% — trailing SL may be too tight; consider widening trail start."))
    elif wins and avg_win > 0:
        recs.append(("green", f"Avg win {avg_win}% vs avg loss {abs(avg_loss)}% — good asymmetry."))

    return {
        "milestone":      milestone,
        "total":          n,
        "win_rate":       win_rate,
        "pf":             pf,
        "recs":           recs,
        "session_stats":  session_stats,
        "mfi_buckets":    {"80-85": (len(mfi_low), wr_low), "85-92": (len(mfi_mid), wr_mid), "92+": (len(mfi_high), wr_high)},
        "avg_win":        avg_win,
        "avg_loss":       avg_loss,
    }


@app.route("/")
def index():
    closed  = [t for t in state["trades"] if t["status"] != "OPEN"]
    wins    = len([t for t in closed if t["result"] == "WIN"])
    beplus  = len([t for t in closed if t["result"] == "BE+"])
    losses  = len([t for t in closed if t["result"] == "LOSS"])
    decided = wins + beplus + losses
    # Win rate: WIN + BE+ both count as positive outcomes
    wr      = round((wins + beplus) / decided * 100, 1) if decided > 0 else 0

    # Goal tracker computations
    try:
        start_dt  = datetime.strptime(state["start_time"], "%Y-%m-%d %H:%M")
        days_run  = max((datetime.now() - start_dt).days, 1)
    except Exception:
        days_run  = 1
    monthly_pace = round(len(closed) / days_run * 30, 1) if days_run > 0 else 0
    avg_pnl_pct  = round(sum(t.get("pnl_pct") or 0 for t in closed) / len(closed), 3) if closed else 0
    monthly_ev   = round(avg_pnl_pct * monthly_pace / 100, 2)  # as % of notional value

    # Profit factor (using variable position sizes)
    gross_win  = sum(
        t["pnl_pct"] / 100 * (t.get("position_usd") or BASE_TRADE_SIZE * LEVERAGE)
        for t in closed if t.get("result") in ("WIN", "BE+") and t.get("pnl_pct")
    )
    gross_loss = sum(
        abs(t["pnl_pct"]) / 100 * (t.get("position_usd") or BASE_TRADE_SIZE * LEVERAGE)
        for t in closed if t.get("result") == "LOSS" and t.get("pnl_pct")
    )
    pf = round(gross_win / gross_loss, 2) if gross_loss > 0 else (round(gross_win, 2) if gross_win > 0 else 0)

    total_pnl_usd = round(gross_win - gross_loss, 2)
    unrealised_usd = round(sum(
        (t.get("unrealised_pnl") or 0) / 100 * (t.get("position_usd") or BASE_TRADE_SIZE * LEVERAGE)
        for t in state["open_trades"]
    ), 2)

    insights = generate_insights(closed)

    return render_template_string(HTML,
        open_trades    = state["open_trades"],
        closed_trades  = closed,
        insights       = insights,
        wins           = wins,
        beplus         = beplus,
        losses         = losses,
        win_rate       = wr,
        profit_factor  = pf,
        total_pnl_usd  = total_pnl_usd,
        unrealised_usd = unrealised_usd,
        base_size      = BASE_TRADE_SIZE,
        leverage       = LEVERAGE,
        scan_count     = state["scan_count"],
        last_scan      = state["last_scan"],
        start_time     = state["start_time"],
        end_time       = state["end_time"],
        running        = state["running"],
        watchlist_count= state["watchlist_count"],
        min_vol_m      = MIN_VOLUME_USDT // 1_000_000,
        # Rule status
        rule1_on       = ENABLE_COOLDOWN,
        rule2_on       = ENABLE_MFI_TIERS,
        rule3_on       = ENABLE_TRAIL,
        cooldown_h     = COOLDOWN_HOURS,
        trail_interval = TRAIL_INTERVAL_MIN,
        changelog      = RULE_CHANGELOG,
        # Goal tracker
        days_run       = days_run,
        monthly_pace   = monthly_pace,
        monthly_ev     = monthly_ev,
        closed_total   = len(closed),
    )

# ─────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────
if __name__ == "__main__":
    load_trades()

    scan_thread  = threading.Thread(target=background_runner)
    trail_thread = threading.Thread(target=trail_runner)
    scan_thread.daemon  = True
    trail_thread.daemon = True
    scan_thread.start()
    trail_thread.start()

    print("\n" + "═"*58)
    print("  MFI Coin Hunter — Paper Trader  v1.6")
    print("  Open: 👉  http://localhost:8081")
    print(f"  Running until: {state['end_time']}")
    print(f"  Signal scan:   every {SCAN_INTERVAL_MIN} min")
    print(f"  Trail check:   every {TRAIL_INTERVAL_MIN} min (separate thread)")
    print(f"  Stage 1 lock:  +{TRAIL_LOCK_1}% profit (BE+)")
    print(f"  Stage 2 lock:  +{TRAIL_LOCK_2}% profit (BE+)")
    print(f"  Rule 4:        50% exits at TP, 50% runner ({RUNNER_TRAIL_GAP}% trail)")
    print("═"*58 + "\n")

    app.run(host="0.0.0.0", port=8081, debug=False)
