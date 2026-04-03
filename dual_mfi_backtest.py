#!/usr/bin/env python3
"""
Dual-Timeframe MFI Reversal — Backtest v2
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Signal (6H):  MFI ≥ 80 within last 3 candles → 1st red candle closes
              → 1st 2H sub-candle of 2nd reversal candle is red → SHORT
              Mirror for LONG (MFI ≤ 20 → green candles)

Entry:        Close of confirming 2H candle
Trade track:  1H candles for SL/TP/trailing

Tests 9 combinations:
  3 SL/TP variants  × 3 trailing configs
"""

import ccxt
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
import time

# ── Assets ───────────────────────────────────────────────────────────────────
SYMBOLS = ['BTC/USDT:USDT', 'ETH/USDT:USDT', 'XRP/USDT:USDT', 'AVAX/USDT:USDT', 'SUI/USDT:USDT']
SYM_LABELS = {s: s.replace('/USDT:USDT','') for s in SYMBOLS}

MFI_PERIOD   = 14
MFI_OB       = 80
MFI_OS       = 20
MFI_LOOKBACK = 3
BACKTEST_DAYS = 730

# ── SL/TP Variants ───────────────────────────────────────────────────────────
SL_TP = {
    'Base': {'sl': 3.0,   'tp': 4.5},
    '+25%': {'sl': 3.75,  'tp': 5.625},
    '+40%': {'sl': 4.2,   'tp': 6.3},
}

# ── Trailing Stop Configs ─────────────────────────────────────────────────────
# Format: (trigger1, lock1, trigger2, lock2)
# Current: small locks → kills PF because BE+ avg ~+0.5% vs -3% loss
# Trail-A: medium locks → meaningful BE+ exits
# Trail-B: large locks → BE+ only triggers on strong moves
TRAIL_CONFIGS = {
    'Current  (lock +0.5%@+1.5% / +1.0%@+3.0%)': (1.5, 0.5, 3.0, 1.0),
    'Trail-A  (lock +1.0%@+2.0% / +2.0%@+3.5%)': (2.0, 1.0, 3.5, 2.0),
    'Trail-B  (lock +1.5%@+3.0% / +2.5%@+4.0%)': (3.0, 1.5, 4.0, 2.5),
}

exchange = ccxt.binanceusdm({'enableRateLimit': True})


# ── Data Fetch ───────────────────────────────────────────────────────────────
def fetch_ohlcv(symbol, timeframe, days=BACKTEST_DAYS):
    end_ms   = int(datetime.now(timezone.utc).timestamp() * 1000)
    since_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    all_rows = []
    current  = since_ms
    while current < end_ms:
        try:
            batch = exchange.fetch_ohlcv(symbol, timeframe, since=current, limit=1000)
        except Exception as e:
            print(f"    fetch error: {e}")
            break
        if not batch:
            break
        all_rows.extend(batch)
        last_ts = batch[-1][0]
        if last_ts >= end_ms or len(batch) < 10:
            break
        current = last_ts + 1
        time.sleep(0.1)
    df = pd.DataFrame(all_rows, columns=['ts','open','high','low','close','volume'])
    df['ts'] = pd.to_datetime(df['ts'], unit='ms', utc=True)
    df = df.drop_duplicates('ts').sort_values('ts').reset_index(drop=True)
    return df


# ── MFI ──────────────────────────────────────────────────────────────────────
def calc_mfi(df, period=MFI_PERIOD):
    tp      = (df['high'] + df['low'] + df['close']) / 3
    rmf     = tp * df['volume']
    prev_tp = tp.shift(1)
    pos     = rmf.where(tp > prev_tp, 0.0)
    neg     = rmf.where(tp < prev_tp, 0.0)
    pos_sum = pos.rolling(period).sum()
    neg_sum = neg.rolling(period).sum().replace(0, np.nan)
    return (100 - (100 / (1 + pos_sum / neg_sum))).fillna(50)


# ── Signal Detection ─────────────────────────────────────────────────────────
def find_signals(df6h, df2h):
    df6h = df6h.copy()
    df6h['mfi'] = calc_mfi(df6h)
    df2h_idx = df2h.set_index('ts')

    signals = []
    n = len(df6h)

    for i in range(MFI_LOOKBACK + 1, n - 2):
        mfi_win  = df6h['mfi'].iloc[i - MFI_LOOKBACK: i + 1]
        c1       = df6h.iloc[i + 1]
        c2_start = df6h.iloc[i + 2]['ts']

        if c2_start not in df2h_idx.index:
            continue
        sub          = df2h_idx.loc[c2_start]
        entry_price  = float(sub['close'])
        entry_ts     = c2_start

        # SHORT
        if (mfi_win >= MFI_OB).any() and c1['close'] < c1['open'] and sub['close'] < sub['open']:
            signals.append({'direction': 'SHORT', 'ts_signal': df6h.iloc[i]['ts'],
                            'entry_price': entry_price, 'entry_ts': entry_ts})
        # LONG
        if (mfi_win <= MFI_OS).any() and c1['close'] > c1['open'] and sub['close'] > sub['open']:
            signals.append({'direction': 'LONG',  'ts_signal': df6h.iloc[i]['ts'],
                            'entry_price': entry_price, 'entry_ts': entry_ts})

    return signals


# ── Trade Simulation ─────────────────────────────────────────────────────────
def simulate_trade(direction, entry, sl_pct, tp_pct, trail_cfg, df1h, entry_ts):
    t1, l1, t2, l2 = trail_cfg

    if direction == 'SHORT':
        sl_price = entry * (1 + sl_pct / 100)
        tp_price = entry * (1 - tp_pct / 100)
    else:
        sl_price = entry * (1 - sl_pct / 100)
        tp_price = entry * (1 + tp_pct / 100)

    trail_stage = 0
    current_sl  = sl_price
    peak_pct    = 0.0

    future = df1h[df1h['ts'] > entry_ts].reset_index(drop=True)

    for _, row in future.iterrows():
        h, l, o, c = row['high'], row['low'], row['open'], row['close']

        pnl = (entry - c) / entry * 100 if direction == 'SHORT' else (c - entry) / entry * 100
        peak_pct = max(peak_pct, pnl)

        # Stage 1
        if trail_stage == 0 and pnl >= t1:
            new_sl = entry * (1 - l1/100) if direction == 'SHORT' else entry * (1 + l1/100)
            better = new_sl < current_sl if direction == 'SHORT' else new_sl > current_sl
            if better:
                current_sl = new_sl
            trail_stage = 1

        # Stage 2
        if trail_stage == 1 and pnl >= t2:
            new_sl = entry * (1 - l2/100) if direction == 'SHORT' else entry * (1 + l2/100)
            better = new_sl < current_sl if direction == 'SHORT' else new_sl > current_sl
            if better:
                current_sl = new_sl
            trail_stage = 2

        # Check hits
        sl_hit = h >= current_sl if direction == 'SHORT' else l <= current_sl
        tp_hit = l <= tp_price  if direction == 'SHORT' else h >= tp_price

        if sl_hit and tp_hit:
            sl_hit = abs(o - current_sl) <= abs(o - tp_price)

        if tp_hit and not sl_hit:
            actual = (entry - tp_price)/entry*100 if direction == 'SHORT' else (tp_price - entry)/entry*100
            return 'WIN', actual, row['ts'], peak_pct, trail_stage

        if sl_hit:
            exit_pct = (entry - current_sl)/entry*100 if direction == 'SHORT' else (current_sl - entry)/entry*100
            result   = 'BE+' if (trail_stage >= 1 and exit_pct > 0.05) else 'LOSS'
            return result, exit_pct, row['ts'], peak_pct, trail_stage

    final = (entry - future.iloc[-1]['close'])/entry*100 if direction == 'SHORT' else (future.iloc[-1]['close'] - entry)/entry*100
    return 'OPEN', final if len(future) else 0, None, peak_pct, trail_stage


# ── Run ───────────────────────────────────────────────────────────────────────
def run_backtest():
    print()
    print("═" * 65)
    print("  Dual-Timeframe MFI Reversal — Backtest v2")
    print("═" * 65)

    all_trades = []

    for sym in SYMBOLS:
        print(f"\n▶ {SYM_LABELS[sym]} — fetching data...", flush=True)
        try:
            df6h = fetch_ohlcv(sym, '6h')
            df2h = fetch_ohlcv(sym, '2h')
            df1h = fetch_ohlcv(sym, '1h')
        except Exception as e:
            print(f"  ✗ {e}")
            continue

        print(f"  6H:{len(df6h)} | 2H:{len(df2h)} | 1H:{len(df1h)}")
        signals = find_signals(df6h, df2h)
        print(f"  Signals: {len(signals)}  ({sum(1 for s in signals if s['direction']=='SHORT')} SHORT / {sum(1 for s in signals if s['direction']=='LONG')} LONG)")

        for sig in signals:
            for tname, tcfg in TRAIL_CONFIGS.items():
                for vname, vp in SL_TP.items():
                    res, epct, ets, ppct, tstg = simulate_trade(
                        sig['direction'], sig['entry_price'],
                        vp['sl'], vp['tp'], tcfg, df1h, sig['entry_ts']
                    )
                    all_trades.append({
                        'symbol':    sym,
                        'direction': sig['direction'],
                        'entry_ts':  sig['entry_ts'],
                        'trail':     tname,
                        'variant':   vname,
                        'sl_pct':    vp['sl'],
                        'tp_pct':    vp['tp'],
                        'result':    res,
                        'exit_pct':  round(epct, 3),
                        'peak_pct':  round(ppct, 3),
                        'trail_stg': tstg,
                        'exit_ts':   ets,
                    })

    return pd.DataFrame(all_trades)


# ── Report ────────────────────────────────────────────────────────────────────
def report(df):
    if df.empty:
        print("\nNo trades.")
        return

    print()
    print("═" * 65)
    print("  RESULTS — grouped by Trailing Config")
    print("═" * 65)

    for tname, tcfg in TRAIL_CONFIGS.items():
        t1, l1, t2, l2 = tcfg
        print(f"\n{'━'*65}")
        print(f"  TRAIL: {tname.strip()}")
        print(f"{'━'*65}")

        tdf = df[df['trail'] == tname]

        for vname in SL_TP:
            vdf    = tdf[tdf['variant'] == vname]
            closed = vdf[vdf['result'] != 'OPEN']
            if closed.empty:
                continue

            wins = closed[closed['result'] == 'WIN']
            bep  = closed[closed['result'] == 'BE+']
            loss = closed[closed['result'] == 'LOSS']
            n    = len(closed)

            wr   = len(wins)/n*100
            wbr  = (len(wins)+len(bep))/n*100
            gw   = wins['exit_pct'].sum() + bep['exit_pct'].sum()
            gl   = loss['exit_pct'].abs().sum()
            pf   = gw/gl if gl > 0 else float('inf')
            avg_be = bep['exit_pct'].mean() if len(bep) else 0

            sl_label = str(SL_TP[vname]['sl'])
            tp_label = str(SL_TP[vname]['tp'])
            print(f"\n  [{vname}] SL -{sl_label}% / TP +{tp_label}%")
            print(f"  Trades: {n}  W:{len(wins)} BE+:{len(bep)} L:{len(loss)}  "
                  f"Win%:{wr:.0f}%  W+BE+%:{wbr:.0f}%")
            print(f"  PF: {pf:.2f}   AvgBE+: +{avg_be:.2f}%   Peak avg: +{closed['peak_pct'].mean():.2f}%")

            # SHORT vs LONG
            for d in ['SHORT', 'LONG']:
                ddf = closed[closed['direction'] == d]
                if ddf.empty:
                    continue
                dw = len(ddf[ddf['result']=='WIN'])
                db = len(ddf[ddf['result']=='BE+'])
                dl = len(ddf[ddf['result']=='LOSS'])
                dgw = ddf[ddf['result'].isin(['WIN','BE+'])]['exit_pct'].sum()
                dgl = ddf[ddf['result']=='LOSS']['exit_pct'].abs().sum()
                dpf = dgw/dgl if dgl > 0 else float('inf')
                print(f"    {d}: {len(ddf)} trades | W:{dw} BE+:{db} L:{dl} | "
                      f"W+BE+:{(dw+db)/len(ddf)*100:.0f}% | PF:{dpf:.2f}")

            # By coin
            print(f"  {'─'*61}")
            print(f"  {'Coin':<6} {'Trades':>6} {'W':>4} {'BE+':>4} {'L':>4}  {'W+BE+':>6}  {'Net%':>7}")
            for sym in SYMBOLS:
                sdf = closed[closed['symbol'] == sym]
                if sdf.empty:
                    continue
                sw  = len(sdf[sdf['result']=='WIN'])
                sb  = len(sdf[sdf['result']=='BE+'])
                sl_ = len(sdf[sdf['result']=='LOSS'])
                swr = (sw+sb)/len(sdf)*100
                net = sdf[sdf['result'].isin(['WIN','BE+'])]['exit_pct'].sum() - sdf[sdf['result']=='LOSS']['exit_pct'].abs().sum()
                print(f"  {SYM_LABELS[sym]:<6} {len(sdf):>6} {sw:>4} {sb:>4} {sl_:>4}  {swr:>5.0f}%  {net:>+7.1f}%")

    # ── Summary table: best PF per trail config ──
    print(f"\n{'═'*65}")
    print("  SUMMARY — Best PF across all 9 combinations")
    print(f"{'═'*65}")
    print(f"  {'Trail Config':<45} {'Variant':<6} {'PF':>5}  {'W+BE+':>6}")
    print(f"  {'─'*45} {'─'*6} {'─'*5}  {'─'*6}")
    for tname in TRAIL_CONFIGS:
        best_pf, best_v, best_wbr = 0, '', 0
        for vname in SL_TP:
            closed = df[(df['trail']==tname) & (df['variant']==vname) & (df['result']!='OPEN')]
            if closed.empty:
                continue
            gw = closed[closed['result'].isin(['WIN','BE+'])]['exit_pct'].sum()
            gl = closed[closed['result']=='LOSS']['exit_pct'].abs().sum()
            pf = gw/gl if gl > 0 else 0
            wbr = (len(closed[closed['result'].isin(['WIN','BE+'])])/len(closed)*100)
            if pf > best_pf:
                best_pf, best_v, best_wbr = pf, vname, wbr
        print(f"  {tname.strip():<45} {best_v:<6} {best_pf:>5.2f}  {best_wbr:>5.0f}%")

    print()


if __name__ == '__main__':
    df = run_backtest()
    report(df)

    out = "/Users/macbook/Downloads/Content Projects/02 - Trading Coin Hunter/Trade Logs/dual_mfi_backtest_v2.csv"
    df.to_csv(out, index=False)
    print(f"Raw data → {out}")
