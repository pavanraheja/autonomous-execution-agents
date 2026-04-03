"""
MFI Scanner — Web Dashboard
────────────────────────────
Run this and open http://localhost:5000 in your browser.
Shows all Binance Futures coins that hit MFI > 80 + red candle reversal.
Auto-scans every 6 hours. Manual scan button also available.

Requirements:
    pip install ccxt pandas flask

Run:
    python dashboard.py
"""

import ccxt
import pandas as pd
import threading
import time
import logging
from datetime import datetime
from flask import Flask, render_template_string
from config import (
    BINANCE_API_KEY, BINANCE_API_SECRET,
    MFI_LENGTH, MFI_OVERBOUGHT, TIMEFRAME, CANDLE_LIMIT
)

# ─────────────────────────────────────────
# SETUP
# ─────────────────────────────────────────
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

exchange = ccxt.binanceusdm({
    "apiKey": BINANCE_API_KEY,
    "secret": BINANCE_API_SECRET,
})

MIN_VOLUME_USDT = 5_000_000   # $5M min 24h volume — filters micro caps
MAX_SL_PCT      = 0.03        # 3% max stop loss cap

# Shared state
state = {
    "signals":      [],
    "last_scan":    "Not yet run",
    "total_scanned": 0,
    "scanning":     False,
}

# ─────────────────────────────────────────
# MFI CALCULATION
# ─────────────────────────────────────────
def calculate_mfi(df, length=14):
    typical_price  = (df["high"] + df["low"] + df["close"]) / 3
    raw_money_flow = typical_price * df["volume"]
    positive_flow  = raw_money_flow.where(typical_price > typical_price.shift(1), 0)
    negative_flow  = raw_money_flow.where(typical_price < typical_price.shift(1), 0)
    pos_sum        = positive_flow.rolling(window=length).sum()
    neg_sum        = negative_flow.rolling(window=length).sum().replace(0, 1e-10)
    mfi            = 100 - (100 / (1 + pos_sum / neg_sum))
    return mfi

# ─────────────────────────────────────────
# FETCH + CHECK
# ─────────────────────────────────────────
def fetch_candles(symbol):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=CANDLE_LIMIT)
        df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        return df
    except Exception as e:
        log.warning(f"Failed {symbol}: {e}")
        return None

def check_signal(df, symbol):
    df["mfi"] = calculate_mfi(df, MFI_LENGTH)

    # Candle roles:
    # c_ob   = overbought candle (MFI >= 80)
    # c1     = first red candle — must be FULLY & PROPERLY formed
    # c2     = second candle — closed, confirming direction
    # c_live = currently forming candle — should be turning red
    c_ob   = df.iloc[-4]
    c1     = df.iloc[-3]
    c2     = df.iloc[-2]
    c_live = df.iloc[-1]

    # ── 1. MFI overbought + declining ────────────────────────
    mfi_overbought = c_ob["mfi"] >= MFI_OVERBOUGHT
    mfi_declining  = c1["mfi"] < c_ob["mfi"]   # MFI must be falling from peak

    # ── 2. First red candle — PROPERLY FORMED ────────────────
    c1_range = c1["high"] - c1["low"]
    if c1_range == 0:
        return None

    c1_body        = c1["open"] - c1["close"]
    c1_body_ratio  = c1_body / c1_range
    c1_lower_wick  = (min(c1["open"], c1["close"]) - c1["low"]) / c1_range
    avg_volume     = df["volume"].iloc[-20:-1].mean()

    c1_is_red          = c1["close"] < c1["open"]
    c1_proper_body     = c1_body_ratio >= 0.40       # Decent body, not doji
    c1_no_buy_pressure = c1_lower_wick <= 0.35       # No strong buyers defending the low
    c1_lower_high      = c1["high"] < c_ob["high"]   # Lower high = reversal confirmed
    c1_lower_close     = c1["close"] < c_ob["close"] # Closes below overbought candle
    c1_vol_confirm     = c1["volume"] >= avg_volume * 1.0  # At or above average volume

    first_red_valid = (
        c1_is_red and c1_proper_body and
        c1_no_buy_pressure and c1_lower_high and
        c1_lower_close and c1_vol_confirm
    )

    # ── 3. Second candle — confirming, controlled move ───────
    c2_range = c2["high"] - c2["low"]
    if c2_range == 0:
        return None

    c2_body_ratio  = abs(c2["open"] - c2["close"]) / c2_range

    c2_is_red      = c2["close"] < c2["open"]
    c2_lower_high  = c2["high"] < c1["high"]         # Continues lower highs
    c2_lower_close = c2["close"] < c1["close"]       # Each close lower
    c2_controlled  = c2_body_ratio <= 0.80           # Not a panic dump — controlled

    second_candle_valid = (
        c2_is_red and c2_lower_high and
        c2_lower_close and c2_controlled
    )

    # ── 4. Live candle still heading down ────────────────────
    live_still_red = c_live["close"] < c_live["open"]
    live_lower     = c_live["close"] < c2["close"]

    # ── 5. Consistent downtrend across all candles ───────────
    consistent_downtrend = (
        c_ob["close"] > c1["close"] > c2["close"]
    )

    # ── FINAL SIGNAL ─────────────────────────────────────────
    signal = (
        mfi_overbought and mfi_declining and
        first_red_valid and second_candle_valid and
        consistent_downtrend
    )

    if signal:
        score = 0
        if c1_body_ratio >= 0.65: score += 20
        if c1_lower_wick <= 0.15: score += 15
        if c1_vol_confirm:        score += 20
        if mfi_declining:         score += 20
        if live_still_red:        score += 15
        if live_lower:            score += 10

        entry = round(c_live["close"], 6)
        sl    = round(min(entry * (1 + MAX_SL_PCT), entry * 1.03), 6)
        tp    = round(entry - (sl - entry) * 1.5, 6)

        return {
            "symbol":        symbol.replace("/USDT:USDT", "").replace(":USDT", ""),
            "full_symbol":   symbol,
            "mfi":           round(c_ob["mfi"], 2),
            "mfi_now":       round(c2["mfi"], 2),
            "price":         entry,
            "sl":            sl,
            "tp":            tp,
            "sl_pct":        "3.0%",
            "tp_pct":        f"{round(((entry - tp) / entry) * 100, 1)}%",
            "c1_body_pct":   f"{round(c1_body_ratio * 100)}%",
            "volume_vs_avg": f"{round(c1['volume'] / avg_volume, 1)}x",
            "score":         score,
            "confidence":    "High 🟢" if score >= 65 else "Medium 🟡",
            "live_red":      "Yes 🔴" if live_still_red else "Forming",
            "time":          c2["timestamp"].strftime("%Y-%m-%d %H:%M"),
        }
    return None

# ─────────────────────────────────────────
# SCANNER
# ─────────────────────────────────────────
def run_scan():
    if state["scanning"]:
        log.info("Scan already in progress, skipping.")
        return

    state["scanning"] = True
    state["signals"]  = []
    log.info("Scan started...")

    try:
        markets  = exchange.load_markets()
        tickers  = exchange.fetch_tickers()
        usdt_futures = [
            s for s, m in markets.items()
            if m.get("quote") == "USDT"
            and m.get("active")
            and not m.get("expiry")
            and (tickers.get(s, {}).get("quoteVolume") or 0) >= MIN_VOLUME_USDT
        ]

        state["total_scanned"] = len(usdt_futures)
        log.info(f"Scanning {len(usdt_futures)} coins...")

        for symbol in usdt_futures:
            df = fetch_candles(symbol)
            if df is None or len(df) < MFI_LENGTH + 5:
                continue
            signal = check_signal(df, symbol)
            if signal:
                state["signals"].append(signal)
                log.info(f"Signal: {signal['symbol']} | Score: {signal['score']} | MFI: {signal['mfi']} → {signal['mfi_now']} | Conf: {signal['confidence']}")
            time.sleep(0.1)

        state["last_scan"] = datetime.now().strftime("%d %b %Y — %H:%M:%S")
        log.info(f"Scan complete. {len(state['signals'])} signals found.")

    except Exception as e:
        log.error(f"Scan error: {e}")
    finally:
        state["scanning"] = False

def background_scheduler():
    while True:
        run_scan()
        time.sleep(6 * 60 * 60)  # Every 6 hours

# ─────────────────────────────────────────
# HTML DASHBOARD
# ─────────────────────────────────────────
HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>MFI Coin Hunter</title>
    <meta http-equiv="refresh" content="60">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
               background: #0d0d0d; color: #e0e0e0; padding: 30px; }

        h1  { font-size: 26px; color: #fff; margin-bottom: 4px; }
        .sub { color: #888; font-size: 13px; margin-bottom: 30px; }

        .stats { display: flex; gap: 20px; margin-bottom: 30px; flex-wrap: wrap; }
        .stat-box { background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 10px;
                    padding: 16px 24px; min-width: 160px; }
        .stat-box .label { font-size: 11px; color: #666; text-transform: uppercase; letter-spacing: 1px; }
        .stat-box .value { font-size: 22px; font-weight: 700; color: #fff; margin-top: 4px; }
        .stat-box.green .value { color: #00c853; }
        .stat-box.red .value   { color: #ff1744; }

        .scan-btn { background: #1e88e5; color: #fff; border: none; padding: 10px 24px;
                    border-radius: 8px; font-size: 14px; cursor: pointer; margin-bottom: 30px; }
        .scan-btn:hover { background: #1565c0; }
        .scanning-badge { background: #ff6f00; color: #fff; padding: 4px 12px;
                          border-radius: 20px; font-size: 12px; margin-left: 12px; }

        table { width: 100%; border-collapse: collapse; background: #1a1a1a;
                border-radius: 12px; overflow: hidden; }
        th { background: #222; padding: 14px 18px; text-align: left; font-size: 12px;
             color: #888; text-transform: uppercase; letter-spacing: 1px; }
        td { padding: 14px 18px; border-top: 1px solid #2a2a2a; font-size: 14px; }
        tr:hover td { background: #222; }

        .coin    { font-weight: 700; color: #fff; font-size: 16px; }
        .strong  { color: #ff1744; font-weight: 600; }
        .early   { color: #ff6f00; font-weight: 600; }
        .mfi-val { color: #ff1744; font-weight: 700; }
        .price   { color: #aaa; }
        .time    { color: #555; font-size: 12px; }

        .no-signals { text-align: center; padding: 60px; color: #444; font-size: 16px; }
        .tv-link { color: #1e88e5; text-decoration: none; font-size: 12px; }
        .tv-link:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <h1>🔴 MFI Coin Hunter</h1>
    <p class="sub">Binance USDT Futures · 6H Chart · MFI > {{ mfi_level }} + Red Candle Reversal</p>

    <div class="stats">
        <div class="stat-box red">
            <div class="label">Signals Found</div>
            <div class="value">{{ signals | length }}</div>
        </div>
        <div class="stat-box">
            <div class="label">Coins Scanned</div>
            <div class="value">{{ total_scanned }}</div>
        </div>
        <div class="stat-box">
            <div class="label">Last Scan</div>
            <div class="value" style="font-size:13px; margin-top:6px;">{{ last_scan }}</div>
        </div>
        <div class="stat-box">
            <div class="label">Next Scan</div>
            <div class="value" style="font-size:13px; margin-top:6px;">Every 6 Hours</div>
        </div>
    </div>

    <form method="post" action="/scan" style="display:inline;">
        <button class="scan-btn" type="submit">⚡ Scan Now</button>
        {% if scanning %}<span class="scanning-badge">Scanning...</span>{% endif %}
    </form>

    {% if signals %}
    <p style="color:#666; font-size:12px; margin-bottom:12px;">
        Sorted by confidence score. All signals passed: MFI overbought → declining → properly formed red candle → volume spike → consistent downtrend.
    </p>
    <table>
        <thead>
            <tr>
                <th>Coin</th>
                <th>Confidence</th>
                <th>Score</th>
                <th>MFI Peak→Now</th>
                <th>Entry Price</th>
                <th>Stop Loss</th>
                <th>Take Profit</th>
                <th>Body</th>
                <th>Volume</th>
                <th>Live</th>
                <th>Time</th>
                <th>Chart</th>
            </tr>
        </thead>
        <tbody>
            {% for s in signals | sort(attribute='score', reverse=True) %}
            <tr>
                <td class="coin">{{ s.symbol }}</td>
                <td class="{{ 'strong' if 'High' in s.confidence else 'early' }}">{{ s.confidence }}</td>
                <td class="mfi-val">{{ s.score }}/100</td>
                <td class="mfi-val">{{ s.mfi }} → {{ s.mfi_now }}</td>
                <td class="price">{{ s.price }}</td>
                <td style="color:#ff6f00;">{{ s.sl }} <span style="color:#555;font-size:11px;">(+{{ s.sl_pct }})</span></td>
                <td style="color:#00c853;">{{ s.tp }} <span style="color:#555;font-size:11px;">(-{{ s.tp_pct }})</span></td>
                <td>{{ s.c1_body_pct }}</td>
                <td>{{ s.volume_vs_avg }}</td>
                <td>{{ s.live_red }}</td>
                <td class="time">{{ s.time }}</td>
                <td>
                    <a class="tv-link"
                       href="https://www.tradingview.com/chart/?symbol=BINANCE:{{ s.symbol }}USDT.P&interval=360"
                       target="_blank">View →</a>
                </td>
            </tr>
            {% endfor %}
        </tbody>
    </table>
    {% else %}
    <div class="no-signals">
        {{ '🔄 Scanning in progress...' if scanning else '✅ No signals right now. Page refreshes every 60 seconds.' }}
    </div>
    {% endif %}
</body>
</html>
"""

# ─────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────
@app.route("/")
def index():
    return render_template_string(HTML,
        signals       = state["signals"],
        last_scan     = state["last_scan"],
        total_scanned = state["total_scanned"],
        scanning      = state["scanning"],
        mfi_level     = MFI_OVERBOUGHT,
    )

@app.route("/scan", methods=["POST"])
def manual_scan():
    thread = threading.Thread(target=run_scan)
    thread.daemon = True
    thread.start()
    time.sleep(1)
    from flask import redirect
    return redirect("/")

# ─────────────────────────────────────────
# START
# ─────────────────────────────────────────
if __name__ == "__main__":
    # Start background scanner
    scanner_thread = threading.Thread(target=background_scheduler)
    scanner_thread.daemon = True
    scanner_thread.start()

    print("\n" + "═"*50)
    print("  MFI Coin Hunter is running!")
    print("  Open this in your browser:")
    print("  👉  http://localhost:8080")
    print("═"*50 + "\n")

    app.run(host="0.0.0.0", port=8080, debug=False)
