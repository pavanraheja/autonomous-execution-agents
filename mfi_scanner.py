"""
Binance Futures MFI Scanner
────────────────────────────
Scans ALL Binance USDT Perpetual Futures coins on 6H chart.
Fires Telegram alert when:
  - MFI was above 80 (overbought)
  - 1 or 2 red candles have formed (reversal starting)

Requirements:
    pip install ccxt pandas requests schedule

Run:
    python mfi_scanner.py
"""

import ccxt
import pandas as pd
import requests
import schedule
import time
import logging
from datetime import datetime
from config import (
    BINANCE_API_KEY, BINANCE_API_SECRET,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    MFI_LENGTH, MFI_OVERBOUGHT, TIMEFRAME, CANDLE_LIMIT
)

# ─────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("scanner.log")
    ]
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────
# BINANCE CONNECTION
# ─────────────────────────────────────────
exchange = ccxt.binance({
    "apiKey": BINANCE_API_KEY,
    "secret": BINANCE_API_SECRET,
    "options": {"defaultType": "future"},  # Futures only
})

# ─────────────────────────────────────────
# TELEGRAM ALERT
# ─────────────────────────────────────────
def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, data=payload, timeout=10)
        if response.status_code == 200:
            log.info(f"Telegram alert sent.")
        else:
            log.warning(f"Telegram error: {response.text}")
    except Exception as e:
        log.error(f"Telegram failed: {e}")

# ─────────────────────────────────────────
# MFI CALCULATION
# ─────────────────────────────────────────
def calculate_mfi(df: pd.DataFrame, length: int = 14) -> pd.Series:
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    raw_money_flow = typical_price * df["volume"]

    positive_flow = raw_money_flow.where(typical_price > typical_price.shift(1), 0)
    negative_flow = raw_money_flow.where(typical_price < typical_price.shift(1), 0)

    pos_sum = positive_flow.rolling(window=length).sum()
    neg_sum = negative_flow.rolling(window=length).sum()

    # Avoid division by zero
    neg_sum = neg_sum.replace(0, 1e-10)

    money_flow_ratio = pos_sum / neg_sum
    mfi = 100 - (100 / (1 + money_flow_ratio))
    return mfi

# ─────────────────────────────────────────
# FETCH CANDLES
# ─────────────────────────────────────────
def fetch_candles(symbol: str) -> pd.DataFrame | None:
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=CANDLE_LIMIT)
        df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        return df
    except Exception as e:
        log.warning(f"Failed to fetch {symbol}: {e}")
        return None

# ─────────────────────────────────────────
# CHECK CONDITIONS
# ─────────────────────────────────────────
def check_signal(df: pd.DataFrame, symbol: str) -> dict | None:
    df["mfi"] = calculate_mfi(df, MFI_LENGTH)
    df["is_red"] = df["close"] < df["open"]

    # Get last 3 completed candles (index -1 is latest closed)
    c0 = df.iloc[-1]   # Latest candle
    c1 = df.iloc[-2]   # 1 candle ago
    c2 = df.iloc[-3]   # 2 candles ago

    mfi_now  = c0["mfi"]
    mfi_1ago = c1["mfi"]
    mfi_2ago = c2["mfi"]

    red_now  = c0["is_red"]
    red_1ago = c1["is_red"]

    # SIGNAL 1: MFI overbought 1 candle ago + current red candle
    signal_1 = mfi_1ago >= MFI_OVERBOUGHT and red_now

    # SIGNAL 2: MFI overbought 2 candles ago + 2 consecutive red candles
    signal_2 = mfi_2ago >= MFI_OVERBOUGHT and red_1ago and red_now

    if signal_1 or signal_2:
        return {
            "symbol":      symbol,
            "signal_type": "2 Red Candles" if signal_2 else "1 Red Candle",
            "mfi":         round(mfi_1ago if signal_1 else mfi_2ago, 2),
            "price":       round(c0["close"], 6),
            "time":        c0["timestamp"].strftime("%Y-%m-%d %H:%M"),
        }
    return None

# ─────────────────────────────────────────
# MAIN SCAN
# ─────────────────────────────────────────
def run_scan():
    log.info("═" * 50)
    log.info(f"Scan started — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    log.info("═" * 50)

    # Get all USDT perpetual futures
    markets = exchange.load_markets()
    usdt_futures = [
        s for s, m in markets.items()
        if m.get("quote") == "USDT"
        and m.get("type") == "future"
        and m.get("active")
        and not m.get("expiry")   # Perpetuals only (no dated futures)
    ]

    log.info(f"Scanning {len(usdt_futures)} Binance USDT Perpetual Futures...")

    signals_found = []

    for symbol in usdt_futures:
        df = fetch_candles(symbol)
        if df is None or len(df) < MFI_LENGTH + 5:
            continue

        signal = check_signal(df, symbol)
        if signal:
            signals_found.append(signal)
            log.info(f"SIGNAL: {signal}")

        time.sleep(0.1)  # Rate limit protection

    # Send Telegram alerts
    if signals_found:
        header = f"🚨 *MFI Reversal Scanner — {datetime.now().strftime('%d %b %Y %H:%M')}*\n"
        header += f"_Binance Futures | 6H Chart | MFI > {MFI_OVERBOUGHT}_\n"
        header += "─" * 30 + "\n\n"

        body = ""
        for s in signals_found:
            body += f"🔴 *{s['symbol']}*\n"
            body += f"   Signal: {s['signal_type']}\n"
            body += f"   MFI: {s['mfi']}\n"
            body += f"   Price: {s['price']}\n"
            body += f"   Time: {s['time']}\n\n"

        send_telegram(header + body)
        log.info(f"Total signals found: {len(signals_found)}")
    else:
        log.info("No signals found this scan.")

    log.info("Scan complete.\n")

# ─────────────────────────────────────────
# SCHEDULER — Every 6 Hours
# ─────────────────────────────────────────
if __name__ == "__main__":
    log.info("MFI Scanner started. Running every 6 hours.")
    send_telegram("✅ *MFI Scanner is live!*\nScanning Binance Futures every 6H for MFI reversals.")

    # Run immediately on start
    run_scan()

    # Then every 6 hours
    schedule.every(6).hours.do(run_scan)

    while True:
        schedule.run_pending()
        time.sleep(60)
