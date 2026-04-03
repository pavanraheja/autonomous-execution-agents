# Setup Guide — MFI Binance Futures Scanner

## Step 1 — Install Python Dependencies
Open Terminal and run:
```bash
pip install ccxt pandas requests schedule
```

---

## Step 2 — Get Binance API Key (Read Only)
1. Login to Binance → Profile → API Management
2. Create new API key
3. **Enable:** Read Info only
4. **Disable:** Trading, Withdrawals (not needed)
5. Copy API Key + Secret → paste into `config.py`

---

## Step 3 — Create Telegram Bot (2 mins)
1. Open Telegram → search `@BotFather`
2. Send: `/newbot`
3. Follow steps → copy the **Bot Token** → paste into `config.py`
4. Open Telegram → search `@userinfobot`
5. Send any message → copy your **Chat ID** → paste into `config.py`
6. Start your bot by searching its name and pressing Start

---

## Step 4 — Run the Scanner
```bash
cd "/Users/macbook/Downloads/Content Projects/02 - Trading Coin Hunter/Scripts/Python"
python mfi_scanner.py
```

---

## What Happens
- Scans ALL Binance USDT Perpetual Futures
- Checks 6H candles every 6 hours
- Sends Telegram alert when:
  - MFI was above 80 AND
  - 1 or 2 red candles have formed

## Sample Telegram Alert
```
🚨 MFI Reversal Scanner — 11 Mar 2026 06:00
Binance Futures | 6H Chart | MFI > 80
──────────────────────────────

🔴 SOLUSDT
   Signal: 2 Red Candles
   MFI: 83.4
   Price: 142.50
   Time: 2026-03-11 06:00

🔴 BNBUSDT
   Signal: 1 Red Candle
   MFI: 81.7
   Price: 412.30
   Time: 2026-03-11 06:00
```

---

## Keep It Running 24/7
To keep the scanner running even when you close Terminal:
```bash
nohup python mfi_scanner.py &
```
Or use a cloud server (AWS/DigitalOcean) for always-on scanning.
