#!/usr/bin/env python3
"""
MFI Cascade SHORT — Paper Trader
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Strategy:  Dual-timeframe MFI reversal SHORT
           6H: MFI ≥ 80 (recent) → 1st red candle closes
               → 1st 2H sub-candle of 2nd reversal candle is red → ENTRY
Signal:    6H chart (MFI 14, lookback 3 candles)
Confirm:   2H candle (first sub-candle of forming reversal must be red)
Direction: SHORT only
SL/TP:     -3.0% / +4.5%
Trailing:  Trail-B — lock +1.5% when profit ≥ +3.0%
                   — lock +2.5% when profit ≥ +4.0%
Scan:      Every 60 min (signal check)
Monitor:   Every 15 min (trail / SL / TP check)
Port:      8082

v1.0  Coins: BTC ETH XRP AVAX SUI
v1.1  2026-03-26  Expanded to top 8 backtest-qualified coins, ETH removed
                  Backtest (180d): 48 trades | 58% WR | PF 2.13 | +10.8%/month EV
                  ETH removed: 0% WR in 180d. New: XRP,TAO,BTC,ADA,DOT,ARB,LINK,LTC
v1.2  2026-03-26  Added INJ + HYPE (top 10) + MFI-tiered sizing
                  Backtest (180d): 54 trades | 58% WR | PF 2.21 | +13.9%/month EV
                  Sizing: MFI 80-84 → 1x | MFI 85-89 → 1.5x | MFI 90+ → 2x
                  MFI 90+ bucket: 75% WR, avg +2.40%/trade (highest conviction)
                  Levers tested and rejected: runner (hurts at 4.5% TP), LONG (29% WR), MFI≥85 filter (kills frequency)
"""

import ccxt
import json
import os
import time
import threading
import numpy as np
import pandas as pd
from flask import Flask, request, jsonify
from datetime import datetime, timedelta, timezone

# ── Config ────────────────────────────────────────────────────────────────────
PORT            = 8082
STRATEGY_NAME   = "MFI Cascade SHORT"
VERSION         = "v1.2"

SYMBOLS = [
    # v1.2 — top 10 backtest-qualified coins (180d: 54 trades, 58% WR, PF 2.21)
    'XRP/USDT',   # 57% WR | PF 2.17 | +1.31%/trade | 1.3/mo
    'TAO/USDT',   # 50% WR | PF 1.58 | +0.81%/trade | 2.2/mo  ← highest frequency
    'BTC/USDT',   # 60% WR | PF 2.50 | +1.50%/trade | 1.0/mo
    'ADA/USDT',   # 60% WR | PF 2.25 | +1.50%/trade | 0.8/mo
    'DOT/USDT',   # 60% WR | PF 2.25 | +1.50%/trade | 0.8/mo
    'ARB/USDT',   # 60% WR | PF 2.25 | +1.50%/trade | 0.8/mo
    'LINK/USDT',  # 67% WR | PF 3.00 | +2.00%/trade | 0.5/mo
    'LTC/USDT',   # 67% WR | PF 3.00 | +2.00%/trade | 0.5/mo
    'INJ/USDT',   # 67% WR | PF 3.00 | +2.00%/trade | 0.5/mo
    'HYPE/USDT',  # 67% WR | PF 3.00 | +2.00%/trade | 0.5/mo
]

# MFI-tiered position sizing (v1.2) — higher MFI = stronger overbought = bigger size
# Backtest: MFI 90+ bucket hits 75% WR, avg +2.40%/trade
MFI_SIZE_TIERS = [
    (90, 2.0),   # MFI 90+ → 2× base size  (high conviction)
    (85, 1.5),   # MFI 85–89 → 1.5× base size
    (80, 1.0),   # MFI 80–84 → 1× base size (standard)
]

MFI_PERIOD      = 14
MFI_OB          = 80
MFI_LOOKBACK    = 3      # candles back MFI must have been ≥ 80

SL_PCT          = 3.0    # stop loss %
TP_PCT          = 4.5    # take profit %

# Trail-B config (backtest winner)
TRAIL_TRIGGER_1 = 3.0    # % profit to trigger stage 1
TRAIL_LOCK_1    = 1.5    # % profit locked at stage 1
TRAIL_TRIGGER_2 = 4.0    # % profit to trigger stage 2
TRAIL_LOCK_2    = 2.5    # % profit locked at stage 2

BASE_TRADE_SIZE = 2_000  # paper USDT notional per trade
LEVERAGE        = 5
SCAN_INTERVAL   = 60     # minutes
MONITOR_INTERVAL= 15     # minutes

COOLDOWN_HOURS  = 24     # after LOSS, skip same coin for 24H
MAX_CONCURRENT  = 6

TRADES_FILE = "/opt/trader/Trade Logs/cascade_trades.json"

# ── Exchange ──────────────────────────────────────────────────────────────────
exchange = ccxt.binance({
    'enableRateLimit': True,
    'timeout': 30000,
    'options': {'defaultType': 'future'},
})

# ── State ─────────────────────────────────────────────────────────────────────
state = {
    'open_trades':   [],
    'closed_trades': [],
    'scan_count':    0,
    'last_scan':     None,
    'last_monitor':  None,
    'log':           [],
}

state_lock = threading.Lock()


# ── Persistence ───────────────────────────────────────────────────────────────
def load_trades():
    if os.path.exists(TRADES_FILE):
        try:
            with open(TRADES_FILE) as f:
                data = json.load(f)
            state['closed_trades'] = data.get('closed', [])
            state['open_trades']   = data.get('open', [])
            log_event(f"Loaded {len(state['closed_trades'])} closed, {len(state['open_trades'])} open trades")
        except Exception as e:
            log_event(f"Load error: {e}")

def save_trades():
    try:
        os.makedirs(os.path.dirname(TRADES_FILE), exist_ok=True)
        with open(TRADES_FILE, 'w') as f:
            json.dump({'open': state['open_trades'], 'closed': state['closed_trades']}, f, indent=2, default=str)
    except Exception as e:
        log_event(f"Save error: {e}")


# ── Logging ───────────────────────────────────────────────────────────────────
def log_event(msg):
    ts  = datetime.now().strftime('%H:%M:%S')
    entry = f"[{ts}] {msg}"
    state['log'].append(entry)
    if len(state['log']) > 200:
        state['log'] = state['log'][-200:]
    print(entry)


# ── Market Data ───────────────────────────────────────────────────────────────
def fetch_ohlcv(symbol, timeframe, limit=100):
    bars = exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
    df   = pd.DataFrame(bars, columns=['ts','open','high','low','close','volume'])
    df['ts'] = pd.to_datetime(df['ts'], unit='ms', utc=True)
    return df

def calc_mfi(df, period=MFI_PERIOD):
    tp      = (df['high'] + df['low'] + df['close']) / 3
    rmf     = tp * df['volume']
    prev_tp = tp.shift(1)
    pos     = rmf.where(tp > prev_tp, 0.0)
    neg     = rmf.where(tp < prev_tp, 0.0)
    ps      = pos.rolling(period).sum()
    ns      = neg.rolling(period).sum().replace(0, np.nan)
    return (100 - (100 / (1 + ps / ns))).fillna(50)

def get_price(symbol):
    ticker = exchange.fetch_ticker(symbol)
    return ticker['last']


# ── Signal Detection ──────────────────────────────────────────────────────────
def check_signal(symbol):
    """
    Returns entry_price if SHORT signal detected, else None.
    Signal:
      1. MFI ≥ 80 within last MFI_LOOKBACK 6H candles
      2. Most recent closed 6H candle is RED (reversal candle 1)
      3. First 2H sub-candle of the current (forming) 6H candle is RED
    Entry: current market price (paper trade)
    """
    try:
        df6h = fetch_ohlcv(symbol, '6h', limit=MFI_LOOKBACK + 10)
        df6h['mfi'] = calc_mfi(df6h)

        # Last fully closed 6H candle = iloc[-2], signal window ends there
        # MFI window: last MFI_LOOKBACK candles up to and including [-2]
        mfi_window = df6h['mfi'].iloc[-(MFI_LOOKBACK + 1):-1]
        was_ob = (mfi_window >= MFI_OB).any()
        if not was_ob:
            return None

        # First reversal candle: [-2] must be red (closed)
        c1 = df6h.iloc[-2]
        if c1['close'] >= c1['open']:
            return None

        # Second reversal candle: current forming 6H = [-1]
        # Check first 2H sub-candle of this 6H candle
        df2h = fetch_ohlcv(symbol, '2h', limit=10)
        # The 6H candle start time
        c2_start = df6h.iloc[-1]['ts']
        # Find the 2H candle that starts at or after c2_start
        sub_candles = df2h[df2h['ts'] >= c2_start]
        if sub_candles.empty:
            return None
        first_sub = sub_candles.iloc[0]
        # Must be a closed 2H candle (not currently forming) — next 2H candle must exist
        if len(sub_candles) < 1:
            return None
        # The first 2H candle of the new 6H must be red and already closed
        # It's closed if there's a 2H candle that started after it
        sub_after = df2h[df2h['ts'] > first_sub['ts']]
        if sub_after.empty:
            return None  # first 2H sub-candle still forming
        if first_sub['close'] >= first_sub['open']:
            return None  # not red

        # All conditions met — return (price, mfi_peak) for tiered sizing
        price    = get_price(symbol)
        mfi_peak = round(float(mfi_window.max()), 2)
        return price, mfi_peak

    except Exception as e:
        log_event(f"Signal check error {symbol}: {e}")
        return None


# ── Cooldown Check ────────────────────────────────────────────────────────────
def in_cooldown(symbol):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=COOLDOWN_HOURS)
    for t in state['closed_trades']:
        if t.get('symbol') == symbol and t.get('result') == 'LOSS':
            ts_str = t.get('exit_ts') or t.get('entry_ts', '')
            try:
                ts = datetime.fromisoformat(str(ts_str))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts > cutoff:
                    return True
            except:
                pass
    return False

def already_open(symbol):
    return any(t['symbol'] == symbol for t in state['open_trades'])


# ── Open Trade ────────────────────────────────────────────────────────────────
def get_size_mult(mfi_peak):
    """Return size multiplier and label based on MFI peak (tiered sizing)."""
    for threshold, mult in sorted(MFI_SIZE_TIERS, reverse=True):
        if mfi_peak >= threshold:
            return mult, f'{mult}× (MFI {mfi_peak:.0f})'
    return 1.0, '1×'

def open_trade(symbol, entry, mfi_peak=80.0):
    sl_price   = round(entry * (1 + SL_PCT / 100), 8)
    tp_price   = round(entry * (1 - TP_PCT / 100), 8)
    mult, mult_label = get_size_mult(mfi_peak)
    size_usd   = round(BASE_TRADE_SIZE * mult, 0)
    trade = {
        'id':           len(state['closed_trades']) + len(state['open_trades']) + 1,
        'symbol':       symbol,
        'direction':    'SHORT',
        'entry':        entry,
        'sl':           sl_price,
        'tp':           tp_price,
        'sl_pct':       f'-{SL_PCT}%',
        'trail_stage':  0,
        'trail_note':   '—',
        'entry_ts':     datetime.now(timezone.utc).isoformat(),
        'mfi_peak':     mfi_peak,
        'size_mult':    mult,
        'size_label':   mult_label,
        'size_usd':     size_usd,
    }
    state['open_trades'].append(trade)
    save_trades()
    log_event(f"OPEN SHORT {symbol} @ {entry:.6f}  SL:{sl_price:.6f}  TP:{tp_price:.6f}")


# ── Monitor Trades ────────────────────────────────────────────────────────────
def monitor_trades():
    if not state['open_trades']:
        return

    to_close = []
    now_ts = datetime.now(timezone.utc).isoformat()

    for trade in state['open_trades']:
        sym   = trade['symbol']
        entry = trade['entry']
        try:
            price = get_price(sym)
        except Exception as e:
            log_event(f"Price error {sym}: {e}")
            continue

        pnl_pct = (entry - price) / entry * 100   # positive = profit for SHORT
        sl      = trade['sl']
        tp      = trade['tp']
        stage   = trade.get('trail_stage', 0)

        # ── Trailing stop update ──────────────────────────────────
        if stage == 0 and pnl_pct >= TRAIL_TRIGGER_1:
            new_sl = round(entry * (1 - TRAIL_LOCK_1 / 100), 8)
            if new_sl < sl:
                trade['sl']         = new_sl
                trade['sl_pct']     = f'+{TRAIL_LOCK_1}% locked (BE+)'
                trade['trail_stage']= 1
                trade['trail_note'] = f'Stage1: SL→+{TRAIL_LOCK_1}% at +{pnl_pct:.1f}%'
                log_event(f"TRAIL Stage1 {sym}: SL→{new_sl:.6f} (+{TRAIL_LOCK_1}% lock) pnl={pnl_pct:.2f}%")
                stage = 1

        if stage == 1 and pnl_pct >= TRAIL_TRIGGER_2:
            new_sl = round(entry * (1 - TRAIL_LOCK_2 / 100), 8)
            if new_sl < trade['sl']:
                trade['sl']         = new_sl
                trade['sl_pct']     = f'+{TRAIL_LOCK_2}% locked'
                trade['trail_stage']= 2
                trade['trail_note'] = f'Stage2: SL→+{TRAIL_LOCK_2}% at +{pnl_pct:.1f}%'
                log_event(f"TRAIL Stage2 {sym}: SL→{new_sl:.6f} (+{TRAIL_LOCK_2}% lock) pnl={pnl_pct:.2f}%")

        sl = trade['sl']  # updated

        # ── Exit check ───────────────────────────────────────────
        result = None
        exit_price = price

        if price >= sl:
            exit_price = sl
            exit_pnl   = (entry - sl) / entry * 100
            result     = 'BE+' if (trade.get('trail_stage', 0) >= 1 and exit_pnl > 0.05) else 'LOSS'
        elif price <= tp:
            exit_price = tp
            exit_pnl   = (entry - tp) / entry * 100
            result     = 'WIN'

        if result:
            exit_pnl_usd = round((exit_price - entry) / entry * trade.get('size_usd', BASE_TRADE_SIZE) * -1, 2)  # -1 because SHORT
            closed = {
                **trade,
                'result':    result,
                'exit':      exit_price,
                'exit_pnl':  round(exit_pnl if result != 'LOSS' or trade.get('trail_stage',0)>=1 else -SL_PCT, 2),
                'exit_pnl_usd': exit_pnl_usd,
                'exit_ts':   now_ts,
                'peak_pnl':  round(pnl_pct, 2),
            }
            to_close.append((trade, closed))
            log_event(f"CLOSE {result} {sym} @ {exit_price:.6f}  pnl={closed['exit_pnl']:+.2f}%  ${exit_pnl_usd:+.2f}")

    for trade, closed in to_close:
        state['open_trades'].remove(trade)
        state['closed_trades'].append(closed)

    if to_close:
        save_trades()


# ── Signal Scanner Thread ─────────────────────────────────────────────────────
def signal_scanner():
    log_event(f"{STRATEGY_NAME} scanner started — {SCAN_INTERVAL}min scans")
    while True:
        try:
            with state_lock:
                state['scan_count'] += 1
                state['last_scan']   = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            log_event(f"Scan #{state['scan_count']} started ({len(state['open_trades'])}/{MAX_CONCURRENT} open)")

            for sym in SYMBOLS:
                with state_lock:
                    if len(state['open_trades']) >= MAX_CONCURRENT:
                        break
                    if already_open(sym) or in_cooldown(sym):
                        continue

                result = check_signal(sym)
                if result:
                    entry, mfi_peak = result
                    with state_lock:
                        open_trade(sym, entry, mfi_peak)
                time.sleep(1)

        except Exception as e:
            log_event(f"Scanner error: {e}")

        time.sleep(SCAN_INTERVAL * 60)


# ── Monitor Thread ────────────────────────────────────────────────────────────
def monitor_thread():
    log_event(f"Monitor thread started — {MONITOR_INTERVAL}min checks")
    time.sleep(60)  # initial delay
    while True:
        try:
            with state_lock:
                state['last_monitor'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                monitor_trades()
        except Exception as e:
            log_event(f"Monitor error: {e}")
        time.sleep(MONITOR_INTERVAL * 60)


# ── Stats Helper ──────────────────────────────────────────────────────────────
def compute_stats():
    closed = state['closed_trades']
    wins   = [t for t in closed if t.get('result') == 'WIN']
    bep    = [t for t in closed if t.get('result') == 'BE+']
    losses = [t for t in closed if t.get('result') == 'LOSS']
    total  = len(closed)

    win_pct = len(wins) / total * 100 if total else 0
    wb_pct  = (len(wins) + len(bep)) / total * 100 if total else 0

    gross_win  = sum(t.get('exit_pnl', 0) for t in wins) + sum(t.get('exit_pnl', 0) for t in bep)
    gross_loss = sum(abs(t.get('exit_pnl', 0)) for t in losses)
    pf         = gross_win / gross_loss if gross_loss > 0 else 0

    realised_usd = sum(t.get('exit_pnl_usd', 0) for t in closed)

    unrealised = 0.0
    for t in state['open_trades']:
        try:
            price = get_price(t['symbol'])
            unrealised += (t['entry'] - price) / t['entry'] * t['size_usd']
        except:
            pass

    return {
        'total': total, 'wins': len(wins), 'bep': len(bep), 'losses': len(losses),
        'win_pct': round(win_pct, 1), 'wb_pct': round(wb_pct, 1),
        'pf': round(pf, 2), 'realised_usd': round(realised_usd, 2),
        'unrealised_usd': round(unrealised, 2),
    }


# ── Flask Dashboard ───────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route('/')
def dashboard():
    with state_lock:
        st   = compute_stats()
        open_t   = list(reversed(state['open_trades']))
        closed_t = list(reversed(state['closed_trades']))
        log  = list(reversed(state['log'][-50:]))
        scan_count   = state['scan_count']
        last_scan    = state['last_scan'] or '—'
        last_monitor = state['last_monitor'] or '—'
        open_count   = len(state['open_trades'])

    # Unrealised for each open trade (inline)
    open_rows = ''
    for t in open_t:
        try:
            price = get_price(t['symbol'])
            pnl_pct = (t['entry'] - price) / t['entry'] * 100
            pnl_usd = pnl_pct / 100 * t['size_usd']
            stage   = t.get('trail_stage', 0)
            stage_badge = {0:'<span class="badge grey">—</span>',
                           1:f'<span class="badge teal">+{TRAIL_LOCK_1}% locked</span>',
                           2:f'<span class="badge gold">+{TRAIL_LOCK_2}% locked</span>'}.get(stage,'')
            color = 'green' if pnl_pct > 0 else 'red'
            open_rows += f"""
            <tr>
              <td>{t['symbol'].replace('/USDT','')}</td>
              <td><span class="dir-short">SHORT</span></td>
              <td>{t['entry']:.6g}</td>
              <td>{price:.6g}</td>
              <td class="{color}">{pnl_pct:+.2f}%</td>
              <td class="{color}">${pnl_usd:+.2f}</td>
              <td>{t['sl']:.6g} <small>({t['sl_pct']})</small></td>
              <td>{t['tp']:.6g}</td>
              <td>{stage_badge}</td>
              <td><small>{str(t.get('entry_ts',''))[:16]}</small></td>
            </tr>"""
        except:
            open_rows += f"<tr><td colspan='10'>{t['symbol']} — price error</td></tr>"

    closed_rows = ''
    for t in closed_t[:60]:
        result  = t.get('result','—')
        pnl     = t.get('exit_pnl', 0)
        pnl_usd = t.get('exit_pnl_usd', 0)
        color   = {'WIN':'green','BE+':'teal','LOSS':'red'}.get(result,'')
        badge   = {'WIN':'<span class="badge green">WIN ✅</span>',
                   'BE+':'<span class="badge teal">BE+ 🟡</span>',
                   'LOSS':'<span class="badge red">LOSS ❌</span>'}.get(result, result)
        stage_badge = {0:'—',1:f'+{TRAIL_LOCK_1}%',2:f'+{TRAIL_LOCK_2}%'}.get(t.get('trail_stage',0),'—')
        closed_rows += f"""
        <tr>
          <td>{t['symbol'].replace('/USDT','')}</td>
          <td>{t.get('entry',0):.6g}</td>
          <td>{t.get('exit',0):.6g}</td>
          <td class="{color}">{pnl:+.2f}%</td>
          <td class="{color}">${pnl_usd:+.2f}</td>
          <td>{badge}</td>
          <td><small>{stage_badge}</small></td>
          <td><small>{str(t.get('entry_ts',''))[:16]}</small></td>
          <td><small>{str(t.get('exit_ts',''))[:16]}</small></td>
        </tr>"""

    log_html = ''.join(f'<div class="log-line">{l}</div>' for l in log)

    pf_color = 'green' if st['pf'] >= 1.0 else 'red'

    # ── Goal tracker computations ─────────────────────────────────────────────
    try:
        start_dt  = datetime.strptime(state['start_time'], '%Y-%m-%d %H:%M')
        g_days    = max((datetime.now() - start_dt).days, 1)
    except Exception:
        g_days = 1
    g_total     = st['total']
    g_pace      = round(g_total / g_days * 30, 1)           # trades/month at current rate
    g_avg_pnl   = round(st['realised_usd'] / g_total, 2) if g_total else 0   # avg $ per trade
    g_wr        = round(st['wins'] / (st['wins'] + st['losses']) * 100, 1) if (st['wins'] + st['losses']) > 0 else 0
    g_monthly_usd = round(g_avg_pnl * g_pace, 2)            # est. monthly USD
    # EV as % — based on average BASE_TRADE_SIZE per trade
    avg_trade_size = BASE_TRADE_SIZE
    g_avg_pnl_pct  = round(g_avg_pnl / avg_trade_size * 100, 2) if avg_trade_size else 0
    g_monthly_ev   = round(g_avg_pnl_pct * g_pace / 100, 2)   # monthly EV as fraction of capital

    # Color helpers
    def _c(val, good, ok): return 'cg' if val >= good else ('cy' if val >= ok else 'cr')
    wr_col  = _c(g_wr, 55, 40)
    pf_col  = _c(st['pf'], 2.0, 1.1)
    pace_col = _c(g_pace, 9, 5)
    ev_sign = '+' if g_monthly_usd >= 0 else ''
    ev_col  = _c(g_monthly_usd, 1, 0)

    # Bar fill: clamp 0-100
    wr_fill   = min(g_wr / 58 * 100, 100)
    pf_fill   = min(st['pf'] / 2.21 * 100, 100)
    pace_fill = min(g_pace / 9 * 100, 100)
    ev_fill   = min(g_monthly_usd / 13.9 * 100, 100) if g_monthly_usd > 0 else 0
    wr_bar_col   = '#34d399' if g_wr >= 55 else ('#fbbf24' if g_wr >= 40 else '#f87171')
    pf_bar_col   = '#34d399' if st['pf'] >= 2.0 else ('#fbbf24' if st['pf'] >= 1.1 else '#f87171')
    pace_bar_col = '#34d399' if g_pace >= 9 else ('#fbbf24' if g_pace >= 5 else '#f87171')
    ev_bar_col   = '#34d399' if g_monthly_usd > 0 else '#f87171'

    return f"""<!DOCTYPE html>
<html>
<head>
  <title>MFI Cascade SHORT</title>
  <meta http-equiv="refresh" content="120">
  <style>
    * {{ margin:0; padding:0; box-sizing:border-box; }}
    body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
            background:#0a0a14; color:#e0e0e0; padding:28px; }}
    h1   {{ font-size:22px; color:#fff; margin-bottom:3px; letter-spacing:.5px; }}
    .sub {{ color:#555; font-size:12px; margin-bottom:24px; }}

    /* Accent: purple/violet theme — distinct from paper trader green */
    .accent {{ color:#a78bfa; }}

    .banner {{
      display:inline-block; background:#1e1b4b; color:#a78bfa;
      border:1px solid #4c1d95; border-radius:6px;
      padding:6px 14px; font-size:12px; font-weight:600;
      margin-bottom:20px; letter-spacing:.5px;
    }}

    .cards {{ display:flex; flex-wrap:wrap; gap:14px; margin-bottom:28px; }}
    .card  {{ background:#111122; border:1px solid #1e1e3f;
              border-radius:10px; padding:16px 22px; min-width:130px; }}
    .card .val {{ font-size:26px; font-weight:700; color:#a78bfa; }}
    .card .lbl {{ font-size:11px; color:#666; margin-top:3px; }}
    .card .val.green {{ color:#34d399; }}
    .card .val.red   {{ color:#f87171; }}
    .card .val.teal  {{ color:#5eead4; }}
    .card .val.gold  {{ color:#fbbf24; }}

    /* Rules pills */
    .rules {{ display:flex; gap:8px; flex-wrap:wrap; margin-bottom:24px; }}
    .pill  {{ font-size:11px; padding:4px 10px; border-radius:20px;
              border:1px solid #333; color:#aaa; background:#111; }}
    .pill.on {{ background:#1e1b4b; border-color:#4c1d95; color:#a78bfa; }}

    table  {{ width:100%; border-collapse:collapse; font-size:12px;
              margin-bottom:28px; }}
    th     {{ background:#0d0d1f; color:#666; text-align:left;
              padding:8px 10px; border-bottom:1px solid #1e1e3f; font-weight:500; }}
    td     {{ padding:7px 10px; border-bottom:1px solid #141428; }}
    tr:hover td {{ background:#111122; }}
    .green {{ color:#34d399; }}
    .red   {{ color:#f87171; }}
    .teal  {{ color:#5eead4; }}
    .gold  {{ color:#fbbf24; }}

    .badge {{ font-size:10px; padding:2px 8px; border-radius:10px;
              font-weight:600; }}
    .badge.green {{ background:#064e3b; color:#34d399; }}
    .badge.teal  {{ background:#0d3d3a; color:#5eead4; }}
    .badge.gold  {{ background:#3d2f0d; color:#fbbf24; }}
    .badge.red   {{ background:#450a0a; color:#f87171; }}
    .badge.grey  {{ background:#1a1a1a; color:#555; }}

    .dir-short {{ background:#450a0a; color:#f87171;
                  padding:2px 7px; border-radius:4px; font-size:10px;
                  font-weight:600; }}

    h2 {{ font-size:14px; color:#888; margin-bottom:10px;
           letter-spacing:.5px; text-transform:uppercase; }}

    .log-box  {{ background:#0d0d1a; border:1px solid #1e1e3f;
                 border-radius:8px; padding:12px; max-height:220px;
                 overflow-y:auto; font-family:monospace; font-size:11px;
                 color:#666; }}
    .log-line {{ margin-bottom:3px; }}

    .footer {{ margin-top:20px; font-size:11px; color:#333; }}

    /* Goal Tracker */
    .goal-box {{
      background:#0d0d1f; border:1px solid #3730a3; border-radius:12px;
      padding:18px 22px; margin-bottom:28px;
    }}
    .goal-box-title {{
      font-size:12px; color:#a78bfa; text-transform:uppercase;
      letter-spacing:1.2px; font-weight:700; margin-bottom:14px;
    }}
    .goal-grid {{ display:flex; flex-wrap:wrap; gap:12px; margin-bottom:16px; }}
    .gc {{ background:#111122; border:1px solid #1e1e3f; border-radius:8px;
            padding:12px 16px; min-width:130px; flex:1; }}
    .gc .gc-label {{ font-size:10px; color:#555; text-transform:uppercase;
                     letter-spacing:1px; margin-bottom:5px; }}
    .gc .gc-val   {{ font-size:22px; font-weight:700; }}
    .gc .gc-sub   {{ font-size:11px; color:#444; margin-top:3px; }}
    .gc .gc-sub em {{ color:#777; font-style:normal; }}
    .cv {{ color:#a78bfa; }} .cg {{ color:#34d399; }}
    .cy {{ color:#fbbf24; }} .cr {{ color:#f87171; }}
    .goal-bars {{ margin-bottom:12px; }}
    .gb-row {{ margin-bottom:9px; }}
    .gb-label {{ display:flex; justify-content:space-between;
                  font-size:11px; color:#555; margin-bottom:3px; }}
    .gb-label b {{ color:#aaa; }}
    .gb-track {{ height:6px; background:#1a1a2e; border-radius:3px; overflow:hidden; }}
    .gb-fill  {{ height:100%; border-radius:3px; }}
    .goal-footer {{ font-size:11px; color:#444; border-top:1px solid #1e1e3f;
                    padding-top:10px; display:flex; gap:20px; flex-wrap:wrap; }}
    .goal-footer span {{ color:#777; }}
    .goal-footer strong {{ color:#aaa; }}
  </style>
</head>
<body>
  <h1>MFI Cascade SHORT <span class="accent">{VERSION}</span></h1>
  <p class="sub">Dual-Timeframe MFI Reversal · 6H signal + 2H confirmation · SHORT only · Paper Trading</p>

  <div class="banner">⚡ CASCADE — SL/TP monitored every {MONITOR_INTERVAL} min · Signals scanned every {SCAN_INTERVAL} min</div>

  <!-- Rules Pills -->
  <div class="rules">
    <span class="pill on">SHORT Only</span>
    <span class="pill on">6H MFI ≥ 80 Signal</span>
    <span class="pill on">2H Candle Confirm</span>
    <span class="pill on">SL -{SL_PCT}% / TP +{TP_PCT}%</span>
    <span class="pill on">Trail-B: +1.5% lock @ +3% / +2.5% lock @ +4%</span>
    <span class="pill on">24H Cooldown</span>
    <span class="pill on">Max {MAX_CONCURRENT} Concurrent</span>
  </div>

  <!-- Scorecards -->
  <div class="cards">
    <div class="card">
      <div class="val accent">{open_count}/{MAX_CONCURRENT}</div>
      <div class="lbl">Open Trades</div>
    </div>
    <div class="card">
      <div class="val green">{st['wins']}</div>
      <div class="lbl">Wins ✅</div>
    </div>
    <div class="card">
      <div class="val teal">{st['bep']}</div>
      <div class="lbl">BE+ 🟡</div>
    </div>
    <div class="card">
      <div class="val red">{st['losses']}</div>
      <div class="lbl">Losses ❌</div>
    </div>
    <div class="card">
      <div class="val">{st['wb_pct']}%</div>
      <div class="lbl">Win+BE+ Rate</div>
    </div>
    <div class="card">
      <div class="val {pf_color}">{st['pf']}</div>
      <div class="lbl">Profit Factor</div>
    </div>
    <div class="card">
      <div class="val {'green' if st['realised_usd']>=0 else 'red'}">${st['realised_usd']:+.2f}</div>
      <div class="lbl">Realised PnL</div>
    </div>
    <div class="card">
      <div class="val {'green' if st['unrealised_usd']>=0 else 'red'}">${st['unrealised_usd']:+.2f}</div>
      <div class="lbl">Unrealised PnL</div>
    </div>
    <div class="card">
      <div class="val accent">{scan_count}</div>
      <div class="lbl">Scans Done</div>
    </div>
  </div>

  <!-- Goal Tracker -->
  <div class="goal-box">
    <div class="goal-box-title">🎯 Goal Tracker — Cascade SHORT (Backtest Benchmarks)</div>
    <div class="goal-grid">
      <div class="gc">
        <div class="gc-label">Monthly EV Target</div>
        <div class="gc-val cy">~$13.9</div>
        <div class="gc-sub">backtest @$2k/trade &nbsp;|&nbsp; <em>+13.9% on capital</em></div>
      </div>
      <div class="gc">
        <div class="gc-label">Win Rate</div>
        <div class="gc-val {wr_col}">{g_wr}%</div>
        <div class="gc-sub">Target: <em>≥ 58%</em> &nbsp;|&nbsp; Min: <em>40%</em></div>
      </div>
      <div class="gc">
        <div class="gc-label">Profit Factor</div>
        <div class="gc-val {pf_col}">{st['pf']}</div>
        <div class="gc-sub">Target: <em>≥ 2.21</em> &nbsp;|&nbsp; Min: <em>1.1</em></div>
      </div>
      <div class="gc">
        <div class="gc-label">Monthly Pace</div>
        <div class="gc-val {pace_col}">{g_pace}</div>
        <div class="gc-sub">trades/month &nbsp;|&nbsp; Target: <em>~9/mo</em></div>
      </div>
      <div class="gc">
        <div class="gc-label">Est. Monthly P&amp;L</div>
        <div class="gc-val {ev_col}">{ev_sign}${g_monthly_usd:.2f}</div>
        <div class="gc-sub">at current pace &nbsp;|&nbsp; Target: <em>+$13.9 @$2k/trade</em></div>
      </div>
    </div>

    <div class="goal-bars">
      <div class="gb-row">
        <div class="gb-label"><div>Win Rate &nbsp;<b>{g_wr}%</b></div><div>Backtest: 58%</div></div>
        <div class="gb-track"><div class="gb-fill" style="width:{wr_fill:.0f}%;background:{wr_bar_col};"></div></div>
      </div>
      <div class="gb-row">
        <div class="gb-label"><div>Profit Factor &nbsp;<b>{st['pf']}</b></div><div>Backtest: 2.21</div></div>
        <div class="gb-track"><div class="gb-fill" style="width:{pf_fill:.0f}%;background:{pf_bar_col};"></div></div>
      </div>
      <div class="gb-row">
        <div class="gb-label"><div>Trades/Month &nbsp;<b>{g_pace}</b></div><div>Backtest: ~9/mo</div></div>
        <div class="gb-track"><div class="gb-fill" style="width:{pace_fill:.0f}%;background:{pace_bar_col};"></div></div>
      </div>
      <div class="gb-row">
        <div class="gb-label"><div>Monthly P&amp;L &nbsp;<b>{ev_sign}${g_monthly_usd:.2f}</b></div><div>Target: +$13.9</div></div>
        <div class="gb-track"><div class="gb-fill" style="width:{ev_fill:.0f}%;background:{ev_bar_col};"></div></div>
      </div>
    </div>

    <div class="goal-footer">
      <div>{g_days}d running · {g_total} closed trades · {g_total/g_days*30:.1f}/mo pace</div>
      <div>Coins: <strong>{', '.join(s.replace('/USDT','') for s in SYMBOLS)}</strong></div>
      <div>SL {SL_PCT}% · TP {TP_PCT}% · RR 1:{TP_PCT/SL_PCT:.1f} · Trail-B</div>
      <div>Sizing: MFI 80-84→1× / 85-89→1.5× / 90+→2× base</div>
    </div>
  </div>

  <!-- Open Trades -->
  <h2>Open Trades ({open_count})</h2>
  <table>
    <tr>
      <th>Coin</th><th>Dir</th><th>Entry</th><th>Price</th>
      <th>PnL %</th><th>PnL $</th><th>SL</th><th>TP</th>
      <th>Trail</th><th>Opened</th>
    </tr>
    {'<tr><td colspan="10" style="color:#333;text-align:center;padding:20px">Waiting for first signal...</td></tr>' if not open_t else open_rows}
  </table>

  <!-- Closed Trades -->
  <h2>Closed Trades ({st['total']})</h2>
  <table>
    <tr>
      <th>Coin</th><th>Entry</th><th>Exit</th>
      <th>PnL %</th><th>PnL $</th><th>Result</th>
      <th>Trail Stage</th><th>Opened</th><th>Closed</th>
    </tr>
    {'<tr><td colspan="9" style="color:#333;text-align:center;padding:20px">No closed trades yet</td></tr>' if not closed_t else closed_rows}
  </table>

  <!-- Log -->
  <h2>Event Log</h2>
  <div class="log-box">
    {log_html if log_html else '<div class="log-line">No events yet.</div>'}
  </div>

  <div class="footer">
    Last scan: {last_scan} &nbsp;·&nbsp;
    Last monitor: {last_monitor} &nbsp;·&nbsp;
    Auto-refresh: 2 min &nbsp;·&nbsp;
    Coins: {', '.join(s.replace('/USDT','') for s in SYMBOLS)}
  </div>
</body>
</html>"""


@app.route('/api/status')
def api_status():
    with state_lock:
        return jsonify({
            'open':   len(state['open_trades']),
            'closed': len(state['closed_trades']),
            'scans':  state['scan_count'],
        })


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print(f"\n{'═'*55}")
    print(f"  {STRATEGY_NAME} {VERSION} — Paper Trader")
    print(f"{'═'*55}")
    print(f"  Signal:   6H MFI ≥ {MFI_OB} → red candle → 2H confirm")
    print(f"  Trail-B:  Lock +{TRAIL_LOCK_1}% @ +{TRAIL_TRIGGER_1}% / +{TRAIL_LOCK_2}% @ +{TRAIL_TRIGGER_2}%")
    print(f"  SL/TP:    -{SL_PCT}% / +{TP_PCT}%")
    print(f"  Coins:    {', '.join(s.replace('/USDT','') for s in SYMBOLS)}")
    print(f"  Dashboard: http://localhost:{PORT}")
    print(f"{'═'*55}\n")

    load_trades()

    scanner_t = threading.Thread(target=signal_scanner, daemon=True, name="scanner")
    monitor_t = threading.Thread(target=monitor_thread,  daemon=True, name="monitor")
    scanner_t.start()
    monitor_t.start()

    threads = {"scanner": scanner_t, "monitor": monitor_t}

    def thread_watchdog():
        while True:
            time.sleep(60)
            if not threads["scanner"].is_alive():
                log_event("Scanner thread died — restarting")
                threads["scanner"] = threading.Thread(target=signal_scanner, daemon=True, name="scanner")
                threads["scanner"].start()
            if not threads["monitor"].is_alive():
                log_event("Monitor thread died — restarting")
                threads["monitor"] = threading.Thread(target=monitor_thread, daemon=True, name="monitor")
                threads["monitor"].start()

    threading.Thread(target=thread_watchdog, daemon=True, name="watchdog").start()

    app.run(host='0.0.0.0', port=PORT, debug=False)
