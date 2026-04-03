# Crypto Trading Bots — Systematic Strategies

Systematic crypto trading bots running live on Binance USDⓈ-M Futures. Built in Python, deployed on a VPS, monitored via Flask dashboards.

Built and operated by [Pavan Raheja](https://pavan-blog.vercel.app).

---

## Live Dashboards

| Strategy | Dashboard | Status |
|----------|-----------|--------|
| MFI Coin Hunter | [http://217.15.164.68:8083](http://217.15.164.68:8083) | Live |
| Liq Zone Trader | [http://217.15.164.68:8089](http://217.15.164.68:8089) | Live |

---

## Strategies

### MFI Coin Hunter (Port 8083 / 8087)
**Signal:** 6H MFI > 80 → 3 consecutive green candles → first red candle → SHORT entry
**Universe:** Full Binance futures scan (~220+ pairs, $5M+ volume filter)
**Risk:** ATR×1.5 SL (hard-capped 8%), 2:1 RR TP, 50% partial close at 1:1, ATR trailing stop
**Filter stack:** MFI 75–85 entry window, decay guard, 1D MFI confluence, 24H coin cooldown

### HTF MFI Scalper (Port 8085)
**Signal:** 3-layer system — 1D+6H EMA direction filter → 1H MFI extremes → ADX ≥ 18 gate
**Coins:** BTC, DOT, SUI, WLD, INJ (bear mode) / BTC, SOL, DOGE, AVAX, BNB (bull mode, Oct 2026)
**Risk:** SL 1.5%, milestone-based trailing stop (0.5% increments)
**Feature:** BULL_MODE flag — single config change flips coin universe and direction for regime switch

### Williams Alligator (Port 8086)
**Signal:** Alligator jaw/teeth/lips crossover + MFI confirmation — SHORT only in bear regime
**Coins:** XRP, ADA, ETH, SUI
**Risk:** SL 1.5%, TP 3.75% (2.5:1 RR)

### Cascade SHORT (Port 8082)
**Signal:** 6H + 2H MFI multi-timeframe confirmation — requires both timeframes overbought
**Risk:** Tiered SL/TP

### Liq Zone Trader (Port 8089)
**Signal:** Liquidity zone detection + MFI regime confirmation
**Feature:** Auto regime switch (bear/bull) based on monthly MFI

---

## Stack

- **Language:** Python 3
- **Exchange:** Binance USDⓈ-M Futures via [ccxt](https://github.com/ccxt/ccxt)
- **Dashboards:** Flask (per-port web UI with live P&L, open positions, trade log)
- **Infrastructure:** Linux VPS (systemd services, auto-restart)
- **Backtesting:** Custom Python scripts per strategy (180-day historical data)

---

## Structure

```
├── mfi_live_trader_8083.py      # MFI Coin Hunter — live execution
├── mfi_monitor.py               # MFI Monitor — paper shadow + manual trades (8087)
├── htf_mfi_paper_trader.py      # HTF MFI Scalper with BULL_MODE switch (8085)
├── alligator_paper_trader.py    # Williams Alligator (8086)
├── cascade_short.py             # Cascade SHORT (8082)
├── liq_zone_trader_8089.py      # Liq Zone Trader (8089)
├── trading_hq.py                # HQ dashboard — all ports in one view (8088)
│
├── backtest_mfi_monitor.py      # MFI strategy backtester
├── backtest_htf_mfi_expanded.py # HTF MFI — 24-coin expansion backtest
├── backtest_htf_mfi_v18.py      # HTF MFI — config comparison (A/B/C/D/E)
├── backtest_htf_mfi_bull.py     # HTF MFI — bull market LONG readiness test
├── backtest_alligator_expanded.py # Alligator — 24-coin expansion backtest
│
├── config.example.py            # API key template — copy to config.py
├── requirements.txt             # Python dependencies
├── deploy.sh                    # Server setup script (Ubuntu 22.04)
├── CHANGELOG.md                 # Strategy changes log with data basis
└── SETUP_GUIDE.md               # Full setup walkthrough
```

---

## Setup

```bash
# 1. Clone and install
git clone https://github.com/pavanraheja/crypto-trading-bots
cd crypto-trading-bots
pip install -r requirements.txt

# 2. Configure API keys
cp config.example.py config.py
# Edit config.py with your Binance API keys

# 3. Run a strategy (paper mode first)
python mfi_monitor.py         # MFI paper monitor — port 8087
python htf_mfi_paper_trader.py  # HTF MFI paper trader — port 8085
```

For VPS deployment see `SETUP_GUIDE.md`.

---

## Backtesting

Each strategy has a dedicated backtest script. Results are logged in `CHANGELOG.md` with decisions and data basis.

```bash
python backtest_mfi_monitor.py          # MFI strategy — full universe
python backtest_htf_mfi_expanded.py     # HTF MFI — 24 coins, 180 days
python backtest_alligator_expanded.py   # Alligator — 24 coins, 180 days
```

---

## Disclaimer

This is a personal project built for learning and experimentation. Not financial advice. Trading crypto futures involves significant risk of loss. Use at your own risk.
