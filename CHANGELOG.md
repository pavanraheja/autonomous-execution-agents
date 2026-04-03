# Agent Trader — Strategy Changelog

This file tracks all strategy changes, filters, and improvements with dates and reasoning.
Used to monitor learning progress and ensure decisions are data-driven.

---

## 2026-04-01

### [8085] HTF MFI Scalper — SL-1.0% Retired, SL-1.5% Reinstated (v1.7)
**Change:** `SL_VARIANTS` switched from `{"SL-1.0%": 1.0}` to `{"SL-1.5%": 1.5}`. Now running single variant at 1.5% SL.
**Data (10 paired signals):**
- SL-1.5% net: **+6.6%** | WR: **80%** | PF: **6.40**
- SL-1.0% net: **+4.0%** | WR: **60%** | PF: **2.20**
**Root cause of reversal:** v1.6 assumed "on wins both variants exit identically." This was wrong. SOL_0328 and BTC_0328 both dipped past -1.0% but recovered. SL-1.0% was stopped out (-1.0% each). SL-1.5% survived the wick and closed at +0.8% each. 2 flipped trades = +3.6% swing in SL-1.5%'s favor.
**On true stops (PAXG_0323):** SL-1.5% did cost 0.5% more (-1.5% vs -1.0%). But 1 extra loss × 0.5% = -0.5% vs 2 rescued trades × 1.8% = +3.6%. Data clearly favors wider SL.
**Impact:** Running one variant halves trade count in logs, doubles clarity. Next A/B test: session filter (London Open vs Off-Peak).

---

### [8083 + 8087] 5-Filter Strategy Upgrade — Data-Driven (15 trades)
**Goal:** Improve WR from 33% raw → 57%+ by eliminating known bad-entry patterns.

**Rec 1 — MFI_MIN_ENTRY raised 70 → 75 (both bots)**
Data: 70-75 zone at entry = 0% WR (NOM -0.55%, OPN -8.46% both in this zone). No wins ever came from below 75. Blocks stale decayed signals.

**Rec 2 — Entry decay guard: cancel if entry MFI < signal MFI - 8 (both bots)**
Data: OPN signal=86.58, entry=73.95 (12.6pt decay) = -8.46% loss. When MFI decays >8pts from signal to entry, the reversal is already in progress or signal has faded — entering is chasing.

**Rec 3 — 1D MFI confluence: cancel if 1D MFI < 45 (both bots)**
Logic: If daily MFI is deeply oversold (<45), the coin is in a strong daily upswing. Shorting into a daily recovery risks getting caught in a sustained bounce. Filter ensures we're not shorting against the macro timeframe.

**Rec 4 — Trail SL Stage 2 tightened: TRAIL_LOOSE_MULT 1.2 → 0.9 (both bots)**
Data: All 5 wins in paper data were manually closed at 5-6%. Automated TP at 12-16% was never hit. Stage 2 trail (at 1.5 ATR profit) now trails price + 0.9×ATR instead of 1.2×ATR — locks in ~6-8% automatically on winning trades without needing manual intervention.

**Rec 5 — Recently OB / borderline signal guard: entry MFI < 77 blocked (both bots)**
Data: STG signal=79.92, NOM signal=71.12 — both below or barely above OB threshold, both losses. When signal was borderline (<78), require entry MFI ≥ 77. For 8083 lookback signals, same rule via recently_ob flag.

**Expected impact (retroactive):** Reduces bad-entry trades from 9 to ~4, improves WR from 33% → ~57%, avg PnL/trade from -1.9% → +2.1%.
**Review trigger:** 10 live trades → assess, 30 live trades → scale size.

---

### [8083] 3-Candle Lookback Window Added to OB Scan
**Change:** `run_scan()` now checks if MFI peaked > 80 in any of the last 3 completed 6H candles, not just the most recent one.
**Reason:** USUAL was caught by 8087 (which uses full lookback) but missed by 8083 — USUAL's MFI had decayed to 73.39 by the time the reversal pattern formed, so 8083 never added it to the watchlist. 8087 had armed it because it uses historical candles.
**Logic:** If `max(mfi[-4:-1]) > 80` AND current MFI ≤ 80 → mark as `recently_ob = True`, include in overbought list so `check_and_arm_signals()` can pick up the reversal pattern.
**Dashboard:** OB table now shows Peak MFI (3-candle window) and Status column: `LIVE OB` (red) or `↓ RECENT OB` (amber). Live OB coins sort first.
**Impact:** 8083 can now arm signals for coins that peaked OB and decayed — same signal coverage as 8087.

---

## 2026-04-01

### [SERVER] Firewall — ports 8081-8088 locked to user IP only
**Change:** UFW rule updated — 8081-8088 blocked from public internet, allowed only from 80.227.66.230.
**Reason:** Internet bots scanning daily. Dashboards accessed via SSH tunnel only.

### [8083] Live trade size scaled $10 → $50 margin ($50 → $250 notional)
**Change:** BASE_TRADE_SIZE = 10 → 50. Each trade now $250 notional at 5× leverage.
**Reason:** $50 notional = noise. Need real PnL data to validate strategy. $250 is still conservative.

### [8087] BE+ result classification fixed for trailed SL exits
**Change:** SL hit with exit_pnl > 0.05% now records as BE+, not LOSS.
**Reason:** Trail SL moves above entry — closing at that SL is a win, not a loss. Was showing 23% WR when real profitable rate is 37%.

### [8083 + 8087] MFI_MAX_ENTRY lowered 97 → 85 + 24H coin cooldown added
**Change 1:** MFI_MAX_ENTRY = 85 (was 97) on both live trader and monitor.
**Data (30 paper trades):** MFI 80-85 at entry = 67% WR. MFI 85-90 = 22% WR. MFI 90-95 = 0% WR.
**Impact:** Blocks the 85-97 zone which had ~10% WR. Keeps only the 80-85 sweet spot.

**Change 2:** COOLDOWN_HOURS = 24 added to both 8083 and 8087.
**Data:** ARC re-entered with no cooldown → -1.08% then -8.08% = -9.16% net from one coin.
**Logic:** Same as paper_trader.py Rule 1 — skip coin for 24H after any loss.

**Bug fix (8083):** MFI_MAX_ENTRY check was dead code (unreachable after continue). Now fixed.
**Synced:** Both scripts now have identical entry filters — MFI_MIN=70, MFI_MAX=85, COOLDOWN=24H.

---

## 2026-03-28

### [8087 + 8083] MFI Max Entry Filter — MFI_MAX_ENTRY = 97
**Change:** Added upper MFI cap — skip any trade where MFI at entry confirmation ≥ 97.
**Reason:** Data analysis on 17 paper trades showed:
- MFI 80–85 zone: 60% WR ✅
- MFI 85–95 zone: 50% WR
- MFI 95–99+ zone: **25% WR** ❌ (ONT -13.31%, ESPORTS -0.58%, 1000RATS -1.48%)
- Extreme MFI = momentum still running, reversal not yet triggered
**Impact (backtested on 12 closed trades):**
- WR: 42% → 56%
- Profit Factor: 1.37 → 6.55
- Removed: 3 trades (all losses) — zero winning trades removed
**Threshold tested:** 97 chosen over 95 (95 removes MON WIN +6.25% with no WR gain)

---

## 2026-03-28

### [8083] Retry Bug Fix — Two-Stage Entry
**Change:** Split open_live_trade() into Stage 1 (market order) + Stage 2 (SL/TP orders).
**Reason:** Original code had one try/except block. If market order succeeded but SL/TP
failed, function returned None → signal stayed in state → re-entered every 10 min.
**Impact:** ESPORTS entered ~8 times ($377 notional vs $50 target). STO entered ~7 times.
Both positions manually recovered with SL/TP placed. STO manually closed at -$8.19.
**Fix:** Market fill now always returns truthy → signal always removed after fill.
SL/TP failure logs WARNING but does not block trade recording.

---

## 2026-03-27

### [ALL PORTS] Migrated to Contabo VPS — 217.15.164.68
**Change:** All 8 bots moved from local Mac to 24/7 cloud server.
**Spec:** Ubuntu 24.04, 4 vCPU, 8GB RAM, Singapore
**Setup:** systemd services (auto-restart on reboot), UFW firewall, daily backup to Mac
**Reason:** Eliminate laptop dependency — bots now run 24/7 independently
**Binance IP:** 217.15.164.68 whitelisted

---

## 2026-03-27

### [8083] NaN ATR Bug Fix
**Change:** Added dropna() guard on ATR series — skip coin if insufficient candle history.
**Reason:** iloc[-1] on rolling mean returns NaN for new coins with <14 candles.
NaN ATR → NaN SL/TP → Binance order rejection → retry bug triggered.
**Code:** atr_raw = atr_series.dropna().iloc[-1] if atr_series.dropna().shape[0] > 0 else None

---

## 2026-03-27

### [8083] Recovered 4 Unprotected Live Positions
**Change:** Manual recovery — placed STOP_MARKET + TAKE_PROFIT_MARKET on 4 open positions.
**Positions recovered:** STO (SL +8%), MAGMA (SL +8%), 1000RATS (SL +6.8%), ESPORTS (SL +8%)
**Cause:** NaN ATR bug + retry bug combined — entries fired without valid SL/TP orders.

---

## 2026-03-26

### [8087] Partial Close at 1:1 RR — PARTIAL_TP_ENABLED = True
**Change:** Auto-close 50% of position when price reaches midpoint between entry and TP.
**Reason:** Locks in profit on winning trades, converts near-misses into small wins.
**Evidence (from 8087 paper):** APR — would have been -loss, partial saved it to +1.4%.
MAGMA, KNC, KAVA all locked $40-48 each before remaining position continues to full TP.

---

## 2026-03-26

### [8087 + 8083] ATR Trailing SL — 3-Stage Trail
**Change:** Trail SL in 3 stages based on profit in ATR multiples.
- Stage 1 (0.5 ATR profit): SL → breakeven
- Stage 2 (1.5 ATR profit): SL trails at price + 1.2× ATR (loose)
- Stage 3 (3.0 ATR profit): SL trails at price + 0.8× ATR (tight)
**Reason:** Protects profits on strong moves without exiting too early.
