"""
MFI Cascade SHORT — Expanded Coin Backtest
═══════════════════════════════════════════
Tests 25 established Binance futures coins (ETH removed)
Finds best performers by WR, frequency, and EV
Period: 180 days | SL 3% / TP 4.5% / Trail-B

Run: python3 backtest_cascade_expanded.py
"""

import ccxt, pickle, os, time
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone

# ── Coin pool — established, liquid, >180d history, excludes ETH/stables/gold ──
CANDIDATE_SYMBOLS = [
    'BTC/USDT', 'XRP/USDT', 'SOL/USDT', 'BNB/USDT', 'DOGE/USDT',
    'ADA/USDT', 'AVAX/USDT', 'DOT/USDT', 'LINK/USDT', 'SUI/USDT',
    'LTC/USDT', 'ATOM/USDT', 'NEAR/USDT', 'APT/USDT', 'ARB/USDT',
    'OP/USDT',  'INJ/USDT',  'TIA/USDT',  'HYPE/USDT','WIF/USDT',
    'ENA/USDT', 'TAO/USDT',  'FET/USDT',  'PEPE/USDT','MATIC/USDT',
]

DAYS_BACK    = 180
CACHE        = "/tmp/backtest_cascade_expanded.pkl"
MFI_PERIOD   = 14
MFI_OB       = 80
MFI_LOOKBACK = 3
SL_PCT       = 3.0
TP_PCT       = 4.5
COOLDOWN_H   = 24
TRAIL        = [(3.0, 1.5), (4.0, 2.5)]

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
        cache = pickle.load(f)
    print(f"  Cache: {len(cache)} keys loaded")

# Filter to symbols that actually exist on Binance futures
valid_symbols = []
print("  Validating symbols...", end=' ', flush=True)
for sym in CANDIDATE_SYMBOLS:
    key6 = f"{sym}_6h"
    if key6 in cache:
        valid_symbols.append(sym)
        continue
    try:
        df = fetch_all(sym, '6h', DAYS_BACK)
        if len(df) >= 200:   # need enough history
            cache[key6] = df
            df2 = fetch_all(sym, '2h', DAYS_BACK)
            cache[f"{sym}_2h"] = df2
            valid_symbols.append(sym)
            with open(CACHE, 'wb') as f: pickle.dump(cache, f)
        time.sleep(0.1)
    except Exception as e:
        pass  # symbol doesn't exist or not enough data

print(f"{len(valid_symbols)} valid")

# ── MFI ───────────────────────────────────────────────────────────────────────
def calc_mfi(df):
    tp  = (df['high'] + df['low'] + df['close']) / 3
    rmf = tp * df['vol']
    ptp = tp.shift(1)
    pos = rmf.where(tp > ptp, 0.0)
    neg = rmf.where(tp < ptp, 0.0)
    ps  = pos.rolling(MFI_PERIOD).sum()
    ns  = neg.rolling(MFI_PERIOD).sum().replace(0, np.nan)
    return (100 - (100 / (1 + ps / ns))).fillna(50)

# ── Simulate one symbol ───────────────────────────────────────────────────────
def simulate(sym):
    df6h = cache[f"{sym}_6h"].copy()
    df2h = cache[f"{sym}_2h"].copy()
    df6h['mfi'] = calc_mfi(df6h)
    trades = []
    cooldown_until = None

    for i in range(MFI_LOOKBACK + 2, len(df6h) - 1):
        now_ts = df6h.iloc[i]['ts']
        if cooldown_until and now_ts < cooldown_until:
            continue

        mfi_w = df6h['mfi'].iloc[i - MFI_LOOKBACK - 1 : i - 1]
        if not (mfi_w >= MFI_OB).any():
            continue

        c1 = df6h.iloc[i - 1]
        if c1['close'] >= c1['open']:
            continue

        c2_start = df6h.iloc[i]['ts']
        sub = df2h[df2h['ts'] >= c2_start].head(3)
        if len(sub) < 2:
            continue
        first_sub = sub.iloc[0]
        if first_sub['close'] >= first_sub['open']:
            continue

        entry    = first_sub['close']
        entry_ts = first_sub['ts']
        sl_price = entry * (1 + SL_PCT / 100)
        tp_price = entry * (1 - TP_PCT / 100)

        future = df2h[df2h['ts'] > entry_ts].head(120)
        result = None; exit_price = None
        trail_sl = sl_price; trail_stage = 0; peak_pnl = 0.0

        for _, row in future.iterrows():
            h, l = row['high'], row['low']
            mid  = (h + l) / 2
            pnl  = (entry - mid) / entry * 100
            for si, (trig, lock) in enumerate(TRAIL):
                if trail_stage <= si and pnl >= trig:
                    new_sl = entry * (1 - lock / 100)
                    if new_sl < trail_sl:
                        trail_sl = new_sl; trail_stage = si + 1
            peak_pnl = max(peak_pnl, pnl)
            if h >= trail_sl:
                exit_price = trail_sl
                ep = (entry - exit_price) / entry * 100
                result = 'BE+' if (trail_stage >= 1 and ep > 0.05) else 'LOSS'
                break
            if l <= tp_price:
                exit_price = tp_price; result = 'WIN'; break

        if exit_price is None and len(future) > 0:
            exit_price = future.iloc[-1]['close']
            ep = (entry - exit_price) / entry * 100
            result = 'WIN' if ep > 0.05 else ('BE+' if ep > -0.1 else 'LOSS')
        if exit_price is None:
            continue

        ep_final = TP_PCT if result == 'WIN' else \
                   ((entry - exit_price) / entry * 100 if result == 'BE+' else -SL_PCT)
        if result == 'LOSS':
            cooldown_until = entry_ts + pd.Timedelta(hours=COOLDOWN_H)

        trades.append({
            'symbol': sym, 'result': result,
            'pnl': round(ep_final, 3), 'peak_pnl': round(peak_pnl, 2),
            'trail_stage': trail_stage, 'entry_ts': entry_ts,
        })

    return trades

# ── Run all symbols ───────────────────────────────────────────────────────────
print()
print("  Simulating...", end=' ', flush=True)
coin_results = {}
for sym in valid_symbols:
    coin_results[sym] = simulate(sym)
print("done")

# ── Per-coin stats ────────────────────────────────────────────────────────────
months = 180 / 30
stats  = []
for sym, trades in coin_results.items():
    if not trades:
        continue
    coin  = sym.replace('/USDT', '')
    wins  = [t for t in trades if t['result'] == 'WIN']
    beps  = [t for t in trades if t['result'] == 'BE+']
    losses= [t for t in trades if t['result'] == 'LOSS']
    total = len(trades)
    wr    = len(wins) / (len(wins) + len(losses)) * 100 if (wins or losses) else 0
    pos_r = (len(wins) + len(beps)) / total * 100
    total_pnl = sum(t['pnl'] for t in trades)
    avg_pnl   = total_pnl / total
    gw = sum(t['pnl'] for t in wins) + sum(t['pnl'] for t in beps)
    gl = abs(sum(t['pnl'] for t in losses))
    pf = gw / gl if gl else (99 if gw > 0 else 0)
    per_mo = total / months

    stats.append({
        'coin': coin, 'sym': sym, 'total': total, 'wins': len(wins),
        'beps': len(beps), 'losses': len(losses),
        'wr': wr, 'pos_rate': pos_r, 'pf': pf,
        'total_pnl': total_pnl, 'avg_pnl': avg_pnl, 'per_mo': per_mo,
    })

# Sort by EV score: avg_pnl × frequency
for s in stats:
    s['ev_score'] = s['avg_pnl'] * s['per_mo']  # monthly EV contribution

stats.sort(key=lambda x: x['ev_score'], reverse=True)

# ── Print full coin ranking ───────────────────────────────────────────────────
print()
print("═" * 80)
print(f"  COIN RANKING — EV Score = avg_pnl × monthly_frequency")
print(f"  {'Coin':<7} {'Trades':>6} {'Per Mo':>7} {'WR':>6} {'BE+':>5} {'PF':>6} {'Avg/tr':>8} {'Total':>8}  {'EV/mo':>7}  {'Verdict'}")
print("  " + "-" * 78)

PASS_WR  = 40    # minimum WR %
PASS_PF  = 1.1   # minimum profit factor
PASS_AVG = 0.0   # minimum avg pnl per trade

qualified = []
for s in stats:
    verdict = "✓ PASS" if (s['wr'] >= PASS_WR and s['pf'] >= PASS_PF and s['avg_pnl'] > PASS_AVG and s['total'] >= 3) else "✗"
    if verdict == "✓ PASS":
        qualified.append(s)
    flag = " ←" if verdict == "✓ PASS" else ""
    print(f"  {s['coin']:<7} {s['total']:>6} {s['per_mo']:>6.1f}/mo {s['wr']:>5.0f}%  {s['beps']:>4}  {s['pf']:>5.2f}  {s['avg_pnl']:>+7.2f}%  {s['total_pnl']:>+7.1f}%  {s['ev_score']:>+6.2f}%{flag}")

# ── Recommended coin list ─────────────────────────────────────────────────────
print()
print("═" * 80)
print("  RECOMMENDED COIN LIST (qualified: WR≥40%, PF≥1.1, ≥3 trades)")
print("═" * 80)
if qualified:
    rec_coins = [s['coin'] for s in qualified]
    print(f"  Coins ({len(rec_coins)}): {', '.join(rec_coins)}")
    print()

    # Combined portfolio stats
    all_trades = []
    for s in qualified:
        all_trades.extend(coin_results[s['sym']])

    all_trades.sort(key=lambda x: x['entry_ts'])
    pw  = [t for t in all_trades if t['result'] == 'WIN']
    pb  = [t for t in all_trades if t['result'] == 'BE+']
    pl  = [t for t in all_trades if t['result'] == 'LOSS']
    pt  = len(all_trades)
    pwr = len(pw) / (len(pw) + len(pl)) * 100 if (pw or pl) else 0
    ptotal = sum(t['pnl'] for t in all_trades)
    pavg   = ptotal / pt
    gw_p   = sum(t['pnl'] for t in pw) + sum(t['pnl'] for t in pb)
    gl_p   = abs(sum(t['pnl'] for t in pl))
    ppf    = gw_p / gl_p if gl_p else 0
    permo  = pt / months

    print(f"  Portfolio (recommended coins only):")
    print(f"  Total trades 180d : {pt}  (~{permo:.1f}/month)")
    print(f"  Win rate          : {pwr:.0f}%")
    print(f"  Positive rate     : {(len(pw)+len(pb))/pt*100:.0f}% (WIN+BE+)")
    print(f"  Profit factor     : {ppf:.2f}")
    print(f"  Total PnL 180d    : {ptotal:+.1f}%")
    print(f"  Avg PnL/trade     : {pavg:+.3f}%")
    print(f"  Monthly EV        : ~{pavg*permo:+.1f}%  (${pavg/100*2000*permo:+.0f}/mo on $2k/trade)")

    # Original (current) portfolio for comparison
    orig_syms = ['BTC/USDT','ETH/USDT','XRP/USDT','AVAX/USDT','SUI/USDT']
    orig_trades = []
    for sym in orig_syms:
        if sym in coin_results:
            orig_trades.extend(coin_results[sym])
    ot = len(orig_trades); ow = sum(1 for t in orig_trades if t['result']=='WIN')
    ol = sum(1 for t in orig_trades if t['result']=='LOSS')
    orig_pnl = sum(t['pnl'] for t in orig_trades)

    print()
    print(f"  vs Original (BTC+ETH+XRP+AVAX+SUI):")
    print(f"  Total trades: {ot} | WR: {ow/(ow+ol)*100:.0f}% | Total PnL: {orig_pnl:+.1f}% | Monthly: ~{ot/months:.1f} trades")
    print()

    # No-ETH comparison
    no_eth = [t for t in orig_trades if t['symbol'] != 'ETH/USDT']
    ne = len(no_eth); nw = sum(1 for t in no_eth if t['result']=='WIN')
    nl = sum(1 for t in no_eth if t['result']=='LOSS')
    ne_pnl = sum(t['pnl'] for t in no_eth)
    print(f"  vs No-ETH (BTC+XRP+AVAX+SUI):")
    print(f"  Total trades: {ne} | WR: {nw/(nw+nl)*100:.0f}% | Total PnL: {ne_pnl:+.1f}% | Monthly: ~{ne/months:.1f} trades")

print()
print("═" * 80)
print("  FREQUENCY BOOST: trades/month with different coin counts")
print("═" * 80)
for n in [4, 6, 8, 10, len(qualified)]:
    if n > len(qualified): break
    top_n = qualified[:n]
    n_trades = sum(s['total'] for s in top_n)
    n_pnl    = sum(coin_results[s['sym']][i]['pnl'] for s in top_n for i in range(len(coin_results[s['sym']])))
    n_avg    = n_pnl / n_trades if n_trades else 0
    n_mo     = n_trades / months
    print(f"  Top {n:2d} coins: ~{n_mo:.1f} trades/month  |  Monthly EV: ~{n_avg*n_mo:+.1f}%  |  Coins: {', '.join(s['coin'] for s in top_n)}")
