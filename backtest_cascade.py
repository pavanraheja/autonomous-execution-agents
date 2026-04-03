"""
MFI Cascade SHORT — Backtest
═════════════════════════════
Compares current (TP 4.5%) vs proposed (TP 6.0% / RR 1:2)
Signal: 6H MFI ≥ 80 in last 3 candles → last 6H red → first 2H sub red → SHORT
Coins : BTC, ETH, XRP, AVAX, SUI
Period: 180 days

Run: python3 backtest_cascade.py
"""

import ccxt, pickle, os, time
import pandas as np
import pandas as pd
import numpy as npy
from datetime import datetime, timedelta, timezone

SYMBOLS   = ['BTC/USDT', 'ETH/USDT', 'XRP/USDT', 'AVAX/USDT', 'SUI/USDT']
DAYS_BACK = 180
CACHE     = "/tmp/backtest_cascade_candles.pkl"

MFI_PERIOD   = 14
MFI_OB       = 80
MFI_LOOKBACK = 3
SL_PCT       = 3.0
COOLDOWN_H   = 24

SYSTEMS = [
    {"label": "Current  — TP 4.5% (RR 1:1.5)", "tp": 4.5,
     "trail": [(3.0, 1.5), (4.0, 2.5)]},
    {"label": "Proposed — TP 6.0% (RR 1:2.0)",  "tp": 6.0,
     "trail": [(3.0, 1.5), (4.0, 2.5), (5.5, 3.5)]},
]

exchange = ccxt.binance({
    'enableRateLimit': True, 'timeout': 30000,
    'options': {'defaultType': 'future'},
})

# ── Fetch + cache ─────────────────────────────────────────────────────────────
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
        import pickle; cache = pickle.load(f)
    print(f"  Cache loaded ({len(cache)} keys)")

import pickle
data = {}
for sym in SYMBOLS:
    for tf in ['6h', '2h']:
        key = f"{sym}_{tf}"
        if key not in cache:
            print(f"  Fetching {sym} {tf}...", end=' ', flush=True)
            cache[key] = fetch_all(sym, tf, DAYS_BACK)
            print(f"{len(cache[key])} bars")
            with open(CACHE, 'wb') as f: pickle.dump(cache, f)
        data[key] = cache[key]
print()

# ── MFI ───────────────────────────────────────────────────────────────────────
def calc_mfi(df, period=MFI_PERIOD):
    tp  = (df['high'] + df['low'] + df['close']) / 3
    rmf = tp * df['vol']
    ptp = tp.shift(1)
    pos = rmf.where(tp > ptp, 0.0)
    neg = rmf.where(tp < ptp, 0.0)
    ps  = pos.rolling(period).sum()
    ns  = neg.rolling(period).sum().replace(0, npy.nan)
    return (100 - (100 / (1 + ps / ns))).fillna(50)

# ── Simulate one symbol ───────────────────────────────────────────────────────
def simulate(sym, tp_pct, trail_stages):
    df6h = data[f"{sym}_6h"].copy()
    df2h = data[f"{sym}_2h"].copy()
    df6h['mfi'] = calc_mfi(df6h)

    trades     = []
    cooldown_until = None

    for i in range(MFI_LOOKBACK + 2, len(df6h) - 1):
        now_ts = df6h.iloc[i]['ts']

        # Cooldown check
        if cooldown_until and now_ts < cooldown_until:
            continue

        # ── Signal conditions ──────────────────────────────────────
        # 1. MFI ≥ 80 in last MFI_LOOKBACK closed 6H candles (up to [-2])
        mfi_window = df6h['mfi'].iloc[i - MFI_LOOKBACK - 1 : i - 1]
        if not (mfi_window >= MFI_OB).any():
            continue

        # 2. Last closed 6H candle = iloc[i-1] must be RED
        c1 = df6h.iloc[i - 1]
        if c1['close'] >= c1['open']:
            continue

        # 3. Current forming 6H = iloc[i]; find its first 2H sub-candle
        c2_start = df6h.iloc[i]['ts']
        sub = df2h[df2h['ts'] >= c2_start].head(3)
        if len(sub) < 2:
            continue  # first 2H sub still forming (no subsequent 2H candle yet)
        first_sub = sub.iloc[0]
        # Confirm first 2H sub is closed (second 2H exists) and RED
        if first_sub['close'] >= first_sub['open']:
            continue

        # ── Entry ─────────────────────────────────────────────────
        entry    = first_sub['close']
        entry_ts = first_sub['ts']
        sl_price = entry * (1 + SL_PCT / 100)
        tp_price = entry * (1 - tp_pct / 100)

        # ── Simulate forward on 15m (approx 2H bars post-entry) ──
        future = df2h[df2h['ts'] > entry_ts].head(120)  # up to 10 days

        result     = None
        exit_price = None
        trail_sl   = sl_price
        trail_stage = 0
        peak_pnl   = 0.0

        for _, row in future.iterrows():
            h, l   = row['high'], row['low']
            mid    = (h + l) / 2
            pnl    = (entry - mid) / entry * 100

            # Update trail stages
            for stage_idx, (trigger, lock) in enumerate(trail_stages):
                if trail_stage <= stage_idx and pnl >= trigger:
                    new_sl = entry * (1 - lock / 100)
                    if new_sl < trail_sl:
                        trail_sl    = new_sl
                        trail_stage = stage_idx + 1

            peak_pnl = max(peak_pnl, pnl)

            # SL hit
            if h >= trail_sl:
                exit_price = trail_sl
                exit_pnl   = (entry - exit_price) / entry * 100
                result     = 'BE+' if (trail_stage >= 1 and exit_pnl > 0.05) else 'LOSS'
                break

            # TP hit
            if l <= tp_price:
                exit_price = tp_price
                exit_pnl   = tp_pct
                result     = 'WIN'
                break

        # Timeout — close at last price
        if exit_price is None and len(future) > 0:
            exit_price = future.iloc[-1]['close']
            exit_pnl   = (entry - exit_price) / entry * 100
            result     = 'WIN' if exit_pnl > 0.05 else ('BE+' if exit_pnl > -0.1 else 'LOSS')

        if exit_price is None:
            continue

        if result == 'LOSS':
            exit_pnl = -SL_PCT
            cooldown_until = entry_ts + pd.Timedelta(hours=COOLDOWN_H)

        trades.append({
            'symbol': sym, 'entry': entry, 'exit': exit_price,
            'entry_ts': str(entry_ts), 'result': result,
            'pnl': round(exit_pnl if result != 'LOSS' else -SL_PCT, 3),
            'peak_pnl': round(peak_pnl, 2), 'trail_stage': trail_stage,
        })

    return trades

# ── Run backtest ──────────────────────────────────────────────────────────────
print("═" * 70)
print(f"  MFI Cascade SHORT Backtest  |  {DAYS_BACK}d  |  {len(SYMBOLS)} coins")
print("═" * 70)

all_results = {}
for sys in SYSTEMS:
    label  = sys['label']
    tp_pct = sys['tp']
    trail  = sys['trail']
    all_trades = []
    for sym in SYMBOLS:
        all_trades.extend(simulate(sym, tp_pct, trail))
    all_results[label] = {'trades': all_trades, 'tp': tp_pct}

# ── Print summary ─────────────────────────────────────────────────────────────
print()
print(f"  {'System':<38}  {'Trades':>6}  {'WR':>6}  {'Total PnL':>10}  {'Avg/trade':>10}  {'PF':>6}")
print("  " + "-" * 80)

base_pnl = None
for sys in SYSTEMS:
    label  = sys['label']
    trades = all_results[label]['trades']
    wins   = [t for t in trades if t['result'] == 'WIN']
    beps   = [t for t in trades if t['result'] == 'BE+']
    losses = [t for t in trades if t['result'] == 'LOSS']
    total  = len(trades)
    wr     = len(wins) / (len(wins) + len(losses)) * 100 if (wins or losses) else 0
    total_pnl = sum(t['pnl'] for t in trades)
    avg_pnl   = total_pnl / total if total else 0
    gw = sum(t['pnl'] for t in wins) + sum(t['pnl'] for t in beps)
    gl = abs(sum(t['pnl'] for t in losses))
    pf = gw / gl if gl else 0

    if base_pnl is None:
        base_pnl = total_pnl
        flag = "  (baseline)"
    else:
        delta = total_pnl - base_pnl
        flag  = f"  (Δ {delta:+.1f}%)"

    all_results[label].update({
        'total': total, 'wins': len(wins), 'beps': len(beps),
        'losses': len(losses), 'wr': wr, 'total_pnl': total_pnl,
        'avg_pnl': avg_pnl, 'pf': pf,
    })
    print(f"  {label:<38}  {total:>6}  {wr:>5.1f}%  {total_pnl:>+9.1f}%{flag:<14}  {avg_pnl:>+9.3f}%  {pf:>6.2f}")

# ── Per-coin breakdown ────────────────────────────────────────────────────────
print()
print(f"  {'':38}  PER COIN BREAKDOWN (Proposed 6.0% TP)")
print(f"  {'Coin':<8}  {'Trades':>6}  {'WR':>6}  {'BE+':>5}  {'Total PnL':>10}  {'Avg/trade':>10}  {'PF':>6}")
print("  " + "-" * 65)

prop_label  = SYSTEMS[1]['label']
prop_trades = all_results[prop_label]['trades']

for sym in SYMBOLS:
    coin = sym.replace('/USDT','')
    t_sub  = [t for t in prop_trades if t['symbol'] == sym]
    wins   = [t for t in t_sub if t['result'] == 'WIN']
    beps   = [t for t in t_sub if t['result'] == 'BE+']
    losses = [t for t in t_sub if t['result'] == 'LOSS']
    if not t_sub: continue
    wr     = len(wins) / (len(wins) + len(losses)) * 100 if (wins or losses) else 0
    tp_sub = round(sum(t['pnl'] for t in t_sub), 1)
    avg    = tp_sub / len(t_sub)
    gw     = sum(t['pnl'] for t in wins) + sum(t['pnl'] for t in beps)
    gl     = abs(sum(t['pnl'] for t in losses))
    pf     = gw / gl if gl else 0
    print(f"  {coin:<8}  {len(t_sub):>6}  {wr:>5.1f}%  {len(beps):>5}  {tp_sub:>+9.1f}%  {avg:>+9.3f}%  {pf:>6.2f}")

# ── Trail stage analysis ──────────────────────────────────────────────────────
print()
print(f"  TRAIL STAGE REACHED (Proposed 6.0%)")
total_p = len(prop_trades)
for stage in [0, 1, 2, 3]:
    count = sum(1 for t in prop_trades if t['trail_stage'] == stage)
    pct   = count / total_p * 100 if total_p else 0
    label = {0: 'No trail triggered', 1: 'Stage1 (+1.5% lock @ +3%)',
             2: 'Stage2 (+2.5% lock @ +4%)', 3: 'Stage3 (+3.5% lock @ +5.5%)'}.get(stage, f'Stage{stage}')
    print(f"  {label:<35} {count:>5} trades  ({pct:.1f}%)")

# ── Verdict ───────────────────────────────────────────────────────────────────
curr_r = all_results[SYSTEMS[0]['label']]
prop_r = all_results[SYSTEMS[1]['label']]
delta  = prop_r['total_pnl'] - curr_r['total_pnl']

print()
print("═" * 70)
print("  VERDICT")
print("═" * 70)
print(f"  Current  (4.5% TP): {curr_r['total_pnl']:>+7.1f}%  |  WR {curr_r['wr']:.1f}%  |  PF {curr_r['pf']:.2f}  |  EV {curr_r['avg_pnl']:+.3f}%/trade")
print(f"  Proposed (6.0% TP): {prop_r['total_pnl']:>+7.1f}%  |  WR {prop_r['wr']:.1f}%  |  PF {prop_r['pf']:.2f}  |  EV {prop_r['avg_pnl']:+.3f}%/trade")
print(f"  Delta: {delta:+.1f}%")
print()
if delta > 5:
    print("  APPLY — 6% TP clearly outperforms. Update to RR 1:2.")
elif delta > 0:
    print("  APPLY — Marginal gain. 6% TP worth the switch.")
elif delta > -5:
    print("  BORDERLINE — Negligible difference. Either works.")
else:
    print("  KEEP CURRENT — 4.5% TP outperforms. Do not change.")
print("═" * 70)
