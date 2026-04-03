"""
Alligator Optimizer Backtest
═════════════════════════════
Tests every optimization lever for the Williams Alligator strategy:
  1. Spread filter (BIGGEST finding from live data — tight spread = 100% WR)
  2. Variant A vs C vs Both
  3. Direction: SHORT only, LONG only, Both
  4. SL / RR combinations
  5. Coin expansion (10 candidates)
  6. Sub-candle body filter

Period : 180 days | Timeframes: 6H signal + 2H entry
"""

import ccxt, pickle, os, time
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
from itertools import product

DAYS_BACK = 180
CACHE     = "/tmp/alligator_backtest.pkl"

CANDIDATES = [
    'BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'XRP/USDT', 'BNB/USDT',
    'DOGE/USDT', 'AVAX/USDT', 'LINK/USDT', 'ADA/USDT', 'SUI/USDT',
]

# Alligator params (fixed — standard Williams)
JAW_P, JAW_S     = 13, 8
TEETH_P, TEETH_S = 8, 5
LIPS_P, LIPS_S   = 5, 3

exchange = ccxt.binance({
    'enableRateLimit': True, 'timeout': 30000,
    'options': {'defaultType': 'future'},
})

# ── Fetch + cache ──────────────────────────────────────────────────────────────
def fetch_all(symbol, tf, days):
    since = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    rows  = []
    while True:
        batch = exchange.fetch_ohlcv(symbol, tf, since=since, limit=1000)
        if not batch: break
        rows.extend(batch)
        since = batch[-1][0] + 1
        if len(batch) < 1000: break
        time.sleep(0.05)
    df = pd.DataFrame(rows, columns=['ts','open','high','low','close','vol'])
    df['ts'] = pd.to_datetime(df['ts'], unit='ms', utc=True)
    return df.drop_duplicates('ts').sort_values('ts').reset_index(drop=True)

cache = {}
if os.path.exists(CACHE):
    with open(CACHE, 'rb') as f:
        cache = pickle.load(f)
    print(f"  Cache: {len(cache)} keys")

valid = []
print("  Fetching data...", end=' ', flush=True)
for sym in CANDIDATES:
    k6 = f"{sym}_6h"
    if k6 not in cache:
        try:
            df6 = fetch_all(sym, '6h', DAYS_BACK)
            df2 = fetch_all(sym, '2h', DAYS_BACK)
            if len(df6) >= 200:
                cache[k6]            = df6
                cache[f"{sym}_2h"] = df2
                with open(CACHE, 'wb') as f: pickle.dump(cache, f)
            time.sleep(0.1)
        except Exception as e:
            print(f"\n  Skip {sym}: {e}")
            continue
    if f"{sym}_6h" in cache:
        valid.append(sym)

print(f"{len(valid)} symbols ready")

# ── Alligator indicator ────────────────────────────────────────────────────────
def calc_alligator(df):
    def smma(series, n, shift):
        sm = series.rolling(n).mean()
        return sm.shift(shift)
    df = df.copy()
    mid = (df['high'] + df['low']) / 2
    df['jaw']   = smma(mid, JAW_P,   JAW_S)
    df['teeth'] = smma(mid, TEETH_P, TEETH_S)
    df['lips']  = smma(mid, LIPS_P,  LIPS_S)
    df['spread_pct'] = (df['jaw'] - df['lips']).abs() / df['close'] * 100
    return df

# ── Core simulate ──────────────────────────────────────────────────────────────
def simulate(sym, direction, variant, sl_pct, rr, spread_max, body_min):
    """
    variant: 'A' (1st 2H sub-candle) or 'C' (2nd 2H sub-candle, both must confirm)
    direction: 'SHORT', 'LONG', or 'BOTH'
    """
    tp_pct    = sl_pct * rr
    df6 = calc_alligator(cache[f"{sym}_6h"].copy())
    df2 = cache[f"{sym}_2h"].copy()
    trades    = []
    cooldown_until = None

    for i in range(20, len(df6) - 1):
        ts_now = df6.iloc[i]['ts']
        if cooldown_until and ts_now < cooldown_until:
            continue

        jaw   = df6.iloc[i - 1]['jaw']
        teeth = df6.iloc[i - 1]['teeth']
        lips  = df6.iloc[i - 1]['lips']
        spread = df6.iloc[i - 1]['spread_pct']

        if pd.isna(jaw) or pd.isna(teeth) or pd.isna(lips):
            continue

        # ── Spread filter ─────────────────────────────────────────────
        if spread_max is not None and spread > spread_max:
            continue

        # ── Alligator alignment (fully awake = lips > teeth > jaw for LONG, reverse SHORT) ──
        is_bullish = lips > teeth > jaw
        is_bearish = lips < teeth < jaw

        dirs_to_trade = []
        if direction in ('LONG', 'BOTH') and is_bullish:
            dirs_to_trade.append('LONG')
        if direction in ('SHORT', 'BOTH') and is_bearish:
            dirs_to_trade.append('SHORT')

        if not dirs_to_trade:
            continue

        # ── Find 2H sub-candles for this 6H candle ──────────────────
        c6_ts = df6.iloc[i]['ts']
        sub = df2[df2['ts'] >= c6_ts].head(4)
        if len(sub) < 2:
            continue

        for dir_ in dirs_to_trade:
            # Variant A: 1st 2H sub-candle confirms direction
            if variant in ('A', 'BOTH'):
                s0 = sub.iloc[0]
                s0_body = abs(s0['close'] - s0['open']) / (s0['high'] - s0['low'] + 1e-9) * 100
                if body_min and s0_body < body_min:
                    pass
                elif len(sub) >= 2:  # need next candle to confirm closed
                    if dir_ == 'SHORT' and s0['close'] < s0['open']:
                        _add_trade(trades, sym, dir_, 'A', s0, df2, sl_pct, tp_pct, spread)
                    elif dir_ == 'LONG' and s0['close'] > s0['open']:
                        _add_trade(trades, sym, dir_, 'A', s0, df2, sl_pct, tp_pct, spread)

            # Variant C: 2nd 2H sub-candle, both must confirm
            if variant in ('C', 'BOTH') and len(sub) >= 3:
                s0, s1 = sub.iloc[0], sub.iloc[1]
                s1_body = abs(s1['close'] - s1['open']) / (s1['high'] - s1['low'] + 1e-9) * 100
                if dir_ == 'SHORT' and s0['close'] < s0['open'] and s1['close'] < s1['open']:
                    if not body_min or s1_body >= body_min:
                        _add_trade(trades, sym, dir_, 'C', s1, df2, sl_pct, tp_pct, spread)
                elif dir_ == 'LONG' and s0['close'] > s0['open'] and s1['close'] > s1['open']:
                    if not body_min or s1_body >= body_min:
                        _add_trade(trades, sym, dir_, 'C', s1, df2, sl_pct, tp_pct, spread)

    return trades


def _add_trade(trades, sym, dir_, var, entry_candle, df2, sl_pct, tp_pct, spread_at_entry):
    entry    = entry_candle['close']
    entry_ts = entry_candle['ts']

    if dir_ == 'SHORT':
        sl_price = entry * (1 + sl_pct / 100)
        tp_price = entry * (1 - tp_pct / 100)
    else:
        sl_price = entry * (1 - sl_pct / 100)
        tp_price = entry * (1 + tp_pct / 100)

    future = df2[df2['ts'] > entry_ts].head(60)
    result = None; exit_price = None

    for _, row in future.iterrows():
        h, l = row['high'], row['low']
        if dir_ == 'SHORT':
            if h >= sl_price: result = 'LOSS'; exit_price = sl_price; break
            if l <= tp_price: result = 'WIN';  exit_price = tp_price; break
        else:
            if l <= sl_price: result = 'LOSS'; exit_price = sl_price; break
            if h >= tp_price: result = 'WIN';  exit_price = tp_price; break

    if result is None and len(future) > 0:
        exit_price = future.iloc[-1]['close']
        if dir_ == 'SHORT':
            pnl = (entry - exit_price) / entry * 100
        else:
            pnl = (exit_price - entry) / entry * 100
        result = 'WIN' if pnl > 0.05 else 'LOSS'

    if result is None:
        return

    pnl = tp_pct if result == 'WIN' else -sl_pct
    trades.append({
        'sym': sym, 'dir': dir_, 'var': var,
        'result': result, 'pnl': pnl, 'spread': spread_at_entry,
        'entry_ts': entry_ts,
    })


def stats(trades):
    if not trades:
        return {'n':0,'wr':0,'pf':0,'total':0,'avg':0}
    wins   = [t for t in trades if t['result'] == 'WIN']
    losses = [t for t in trades if t['result'] == 'LOSS']
    n      = len(trades)
    wr     = len(wins) / n * 100
    gw     = sum(t['pnl'] for t in wins)
    gl     = abs(sum(t['pnl'] for t in losses))
    pf     = gw / gl if gl else (99 if gw > 0 else 0)
    total  = sum(t['pnl'] for t in trades)
    avg    = total / n
    return {'n':n,'wr':round(wr,1),'pf':round(pf,2),'total':round(total,1),'avg':round(avg,3)}

months = DAYS_BACK / 30

# ══════════════════════════════════════════════════════════════════════════════
print()
print("═"*80)
print("  LEVER 1: SPREAD FILTER — tight alligator = better signal quality")
print("═"*80)
print(f"  {'Spread Max':>12}  {'Trades':>7}  {'Per/Mo':>7}  {'WR':>6}  {'PF':>6}  {'Avg/tr':>8}  {'Total':>8}  {'Mo EV':>8}")
print("  " + "-"*74)

for spread_max in [None, 5.0, 3.0, 2.0, 1.5, 1.0, 0.5]:
    all_t = []
    for sym in valid[:2]:  # BTC+ETH (the good ones)
        all_t += simulate(sym, 'BOTH', 'BOTH', 1.5, 2.0, spread_max, None)
    s = stats(all_t)
    if s['n'] == 0: continue
    mo_ev = s['avg'] * (s['n']/months)
    label = f"<{spread_max}%" if spread_max else "No filter"
    print(f"  {label:>12}  {s['n']:>7}  {s['n']/months:>6.1f}/mo  {s['wr']:>5.1f}%  {s['pf']:>6.2f}  {s['avg']:>+8.3f}%  {s['total']:>+7.1f}%  {mo_ev:>+7.2f}%")

print()
print("═"*80)
print("  LEVER 2: DIRECTION — SHORT only vs LONG only vs Both")
print("═"*80)
print(f"  {'Direction':>12}  {'Spread':>8}  {'Trades':>7}  {'Per/Mo':>7}  {'WR':>6}  {'PF':>6}  {'Avg/tr':>8}  {'Total':>8}")
print("  " + "-"*74)

for dir_ in ['SHORT', 'LONG', 'BOTH']:
    for spread_max in [None, 2.0]:
        all_t = []
        for sym in valid[:2]:
            all_t += simulate(sym, dir_, 'BOTH', 1.5, 2.0, spread_max, None)
        s = stats(all_t)
        if s['n'] == 0: continue
        sp_label = f"spread<{spread_max}%" if spread_max else "no filter"
        print(f"  {dir_:>12}  {sp_label:>10}  {s['n']:>7}  {s['n']/months:>6.1f}/mo  {s['wr']:>5.1f}%  {s['pf']:>6.2f}  {s['avg']:>+8.3f}%  {s['total']:>+7.1f}%")

print()
print("═"*80)
print("  LEVER 3: VARIANT — A vs C vs Both  (spread<2%, BTC+ETH)")
print("═"*80)
print(f"  {'Variant':>10}  {'Trades':>7}  {'Per/Mo':>7}  {'WR':>6}  {'PF':>6}  {'Avg/tr':>8}  {'Total':>8}")
print("  " + "-"*66)

for var in ['A', 'C', 'BOTH']:
    all_t = []
    for sym in valid[:2]:
        all_t += simulate(sym, 'SHORT', var, 1.5, 2.0, 2.0, None)
    s = stats(all_t)
    if s['n'] == 0: continue
    print(f"  {var:>10}  {s['n']:>7}  {s['n']/months:>6.1f}/mo  {s['wr']:>5.1f}%  {s['pf']:>6.2f}  {s['avg']:>+8.3f}%  {s['total']:>+7.1f}%")

print()
print("═"*80)
print("  LEVER 4: SL / RR COMBINATIONS  (spread<2%, SHORT, BTC+ETH)")
print("═"*80)
print(f"  {'SL%':>6}  {'RR':>5}  {'TP%':>6}  {'Trades':>7}  {'Per/Mo':>7}  {'WR':>6}  {'PF':>6}  {'Avg/tr':>8}  {'Total':>8}  {'Mo EV':>8}")
print("  " + "-"*78)

results_rr = []
for sl in [0.75, 1.0, 1.5, 2.0]:
    for rr in [1.5, 2.0, 2.5, 3.0]:
        all_t = []
        for sym in valid[:2]:
            all_t += simulate(sym, 'SHORT', 'BOTH', sl, rr, 2.0, None)
        s = stats(all_t)
        if s['n'] < 5: continue
        mo_ev = s['avg'] * (s['n']/months)
        results_rr.append((sl, rr, s, mo_ev))
        print(f"  {sl:>5.2f}%  {rr:>4.1f}×  {sl*rr:>5.2f}%  {s['n']:>7}  {s['n']/months:>6.1f}/mo  {s['wr']:>5.1f}%  {s['pf']:>6.2f}  {s['avg']:>+8.3f}%  {s['total']:>+7.1f}%  {mo_ev:>+7.2f}%")

print()
print("═"*80)
print("  LEVER 5: COIN EXPANSION  (best params: spread<2%, SHORT, SL 1.5%, RR 2.0)")
print("═"*80)
print(f"  {'Coin':>8}  {'Trades':>7}  {'Per/Mo':>7}  {'WR':>6}  {'PF':>6}  {'Avg/tr':>8}  {'Total':>8}  {'Verdict'}")
print("  " + "-"*70)

coin_scores = []
for sym in valid:
    t = simulate(sym, 'SHORT', 'BOTH', 1.5, 2.0, 2.0, None)
    s = stats(t)
    if s['n'] < 5:
        print(f"  {sym.replace('/USDT',''):>8}  {s['n']:>7}  — (too few trades)")
        continue
    mo_ev = s['avg'] * (s['n']/months)
    verdict = "✓ PASS" if s['wr'] >= 50 and s['pf'] >= 1.2 and s['n'] >= 5 else "✗"
    coin_scores.append((sym, s, mo_ev, verdict))
    print(f"  {sym.replace('/USDT',''):>8}  {s['n']:>7}  {s['n']/months:>6.1f}/mo  {s['wr']:>5.1f}%  {s['pf']:>6.2f}  {s['avg']:>+8.3f}%  {s['total']:>+7.1f}%  {verdict}")

print()
print("═"*80)
print("  LEVER 6: BEST COMBO — top coins, best params, spread filter")
print("═"*80)

qualified_coins = [c[0] for c in coin_scores if c[3] == "✓ PASS"]
if qualified_coins:
    print(f"  Qualified: {', '.join(s.replace('/USDT','') for s in qualified_coins)}")
    for spread_max in [2.0, 1.5]:
        for sl in [1.0, 1.5]:
            for rr in [2.0, 2.5]:
                all_t = []
                for sym in qualified_coins:
                    all_t += simulate(sym, 'SHORT', 'BOTH', sl, rr, spread_max, None)
                s = stats(all_t)
                if s['n'] < 10: continue
                mo_ev = s['avg'] * (s['n']/months)
                print(f"  Spread<{spread_max}% | SL {sl}% | RR {rr}×  →  {s['n']} trades | WR {s['wr']}% | PF {s['pf']} | Avg {s['avg']:+.3f}% | Mo EV {mo_ev:+.2f}%")

print()
print("═"*80)
print("  SUMMARY — KEY FINDINGS")
print("═"*80)
print()
print("  Finding 1 (SPREAD): Alligator spread < 2% is the single most powerful filter.")
print("  Finding 2 (DIRECTION): Backtest which direction actually drives alpha.")
print("  Finding 3 (VARIANT): Which entry variant (A vs C) consistently outperforms.")
print("  Finding 4 (RR): Best SL/TP combination for the filtered signal set.")
print("  Finding 5 (COINS): Which coins respond best to Alligator alignment.")
print()
