"""
MFI Cascade SHORT — Full Optimization Analysis
═══════════════════════════════════════════════
Tests every improvement lever to find path to 20%/month:

  Lever 1: Partial exit + runner (50% at TP, 50% trails)
  Lever 2: MFI-tiered sizing (1x / 1.5x / 2x based on MFI peak)
  Lever 3: Top 10 coins vs Top 8
  Lever 4: LONG signals added (MFI < 20 oversold)
  Lever 5: Tighter MFI threshold (85 vs 80)
  Lever 6: Combined best scenario

Run: python3 backtest_cascade_optimize.py
"""

import pickle, os
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone

CACHE = "/tmp/backtest_cascade_expanded.pkl"
with open(CACHE, 'rb') as f:
    cache = pickle.load(f)

TOP8  = ['XRP/USDT','TAO/USDT','BTC/USDT','ADA/USDT','DOT/USDT','ARB/USDT','LINK/USDT','LTC/USDT']
TOP10 = TOP8 + ['INJ/USDT','HYPE/USDT']

MFI_PERIOD=14; SL_PCT=3.0; TP_PCT=4.5; COOLDOWN_H=24; MONTHS=6.0
TRAIL_B = [(3.0,1.5),(4.0,2.5)]

def calc_mfi(df):
    tp=(df['high']+df['low']+df['close'])/3; rmf=tp*df['vol']
    ptp=tp.shift(1); pos=rmf.where(tp>ptp,0.0); neg=rmf.where(tp<ptp,0.0)
    ps=pos.rolling(MFI_PERIOD).sum(); ns=neg.rolling(MFI_PERIOD).sum().replace(0,np.nan)
    return (100-(100/(1+ps/ns))).fillna(50)

def simulate(sym, direction='SHORT', mfi_ob=80, mfi_lookback=3,
             partial_exit=1.0, trail_gap=0.5, size_tiers=None):
    """
    size_tiers: list of (mfi_threshold, multiplier) — e.g. [(90,2.0),(85,1.5),(80,1.0)]
    partial_exit: fraction closed at TP (1.0 = no runner)
    """
    df6h = cache[f"{sym}_6h"].copy()
    df2h = cache[f"{sym}_2h"].copy()
    df6h['mfi'] = calc_mfi(df6h)
    trades=[]; cooldown_until=None

    for i in range(mfi_lookback+2, len(df6h)-1):
        now_ts = df6h.iloc[i]['ts']
        if cooldown_until and now_ts < cooldown_until: continue

        mfi_w = df6h['mfi'].iloc[i-mfi_lookback-1:i-1]
        mfi_peak = float(mfi_w.max())

        if direction == 'SHORT':
            if not (mfi_w >= mfi_ob).any(): continue
            c1 = df6h.iloc[i-1]
            if c1['close'] >= c1['open']: continue   # must be red
        else:  # LONG
            if not (mfi_w <= (100-mfi_ob)).any(): continue
            c1 = df6h.iloc[i-1]
            if c1['close'] <= c1['open']: continue   # must be green

        c2_start = df6h.iloc[i]['ts']
        sub = df2h[df2h['ts'] >= c2_start].head(3)
        if len(sub) < 2: continue
        first_sub = sub.iloc[0]

        if direction == 'SHORT':
            if first_sub['close'] >= first_sub['open']: continue
        else:
            if first_sub['close'] <= first_sub['open']: continue

        # Size multiplier from MFI tier
        size_mult = 1.0
        if size_tiers:
            for thresh, mult in sorted(size_tiers, reverse=True):
                if mfi_peak >= thresh:
                    size_mult = mult; break

        entry=first_sub['close']; entry_ts=first_sub['ts']
        if direction == 'SHORT':
            sl_price=entry*(1+SL_PCT/100); tp_price=entry*(1-TP_PCT/100)
        else:
            sl_price=entry*(1-SL_PCT/100); tp_price=entry*(1+TP_PCT/100)

        future=df2h[df2h['ts']>entry_ts].head(120)
        result=None; exit_price=None; trail_sl=sl_price; trail_stage=0
        peak_pnl=0.0; tp_hit=False; tp_exit_price=None; runner_trail=None

        for _,row in future.iterrows():
            h,l=row['high'],row['low']; mid=(h+l)/2
            pnl=(entry-mid)/entry*100 if direction=='SHORT' else (mid-entry)/entry*100

            # Trail-B stages (pre-TP only)
            if not tp_hit:
                for si,(trig,lock) in enumerate(TRAIL_B):
                    if trail_stage<=si and pnl>=trig:
                        new_sl=(entry*(1-lock/100) if direction=='SHORT' else entry*(1+lock/100))
                        if (direction=='SHORT' and new_sl<trail_sl) or (direction=='LONG' and new_sl>trail_sl):
                            trail_sl=new_sl; trail_stage=si+1
                peak_pnl=max(peak_pnl,pnl)

                # SL hit before TP
                if (direction=='SHORT' and h>=trail_sl) or (direction=='LONG' and l<=trail_sl):
                    exit_price=trail_sl
                    ep=(entry-exit_price)/entry*100 if direction=='SHORT' else (exit_price-entry)/entry*100
                    result='BE+' if (trail_stage>=1 and ep>0.05) else 'LOSS'; break

                # TP hit
                if (direction=='SHORT' and l<=tp_price) or (direction=='LONG' and h>=tp_price):
                    if partial_exit >= 1.0:
                        exit_price=tp_price; result='WIN'; break
                    else:
                        tp_hit=True; tp_exit_price=tp_price
                        runner_trail=(tp_price*(1+trail_gap/100) if direction=='SHORT'
                                      else tp_price*(1-trail_gap/100))
                        continue

            else:  # Runner active
                if direction=='SHORT':
                    new_t=l*(1+trail_gap/100)
                    if new_t<runner_trail: runner_trail=new_t
                    if h>=runner_trail: exit_price=runner_trail; break
                else:
                    new_t=h*(1-trail_gap/100)
                    if new_t>runner_trail: runner_trail=new_t
                    if l<=runner_trail: exit_price=runner_trail; break

        if exit_price is None and len(future)>0:
            exit_price=future.iloc[-1]['close']

        if exit_price is None: continue

        if result=='LOSS':
            ep_final=-SL_PCT
            cooldown_until=entry_ts+pd.Timedelta(hours=COOLDOWN_H)
        elif result=='BE+':
            ep_final=(entry-exit_price)/entry*100 if direction=='SHORT' else (exit_price-entry)/entry*100
        elif result=='WIN':
            ep_final=TP_PCT
        elif tp_hit:
            # Blended runner PnL
            tp_pnl_pct = TP_PCT
            runner_pnl_pct = ((tp_exit_price-exit_price)/tp_exit_price*100 if direction=='SHORT'
                               else (exit_price-tp_exit_price)/tp_exit_price*100)
            ep_final = partial_exit*tp_pnl_pct + (1-partial_exit)*runner_pnl_pct
            result   = 'WIN' if ep_final>0.05 else ('BE+' if ep_final>-0.1 else 'LOSS')
        else:
            ep_final=(entry-exit_price)/entry*100 if direction=='SHORT' else (exit_price-entry)/entry*100
            result='WIN' if ep_final>0.05 else ('BE+' if ep_final>-0.1 else 'LOSS')

        trades.append({
            'symbol':sym,'result':result,'pnl':round(ep_final,3),
            'size_mult':size_mult,'weighted_pnl':round(ep_final*size_mult,3),
            'mfi_peak':round(mfi_peak,1),'trail_stage':trail_stage,
            'direction':direction,'entry_ts':entry_ts,
        })

    return trades

def stats(trades, label):
    if not trades: return {'label':label,'total':0,'wr':0,'pf':0,'ev_mo':0,'total_pnl':0,'avg':0}
    w=[t for t in trades if t['result']=='WIN']
    b=[t for t in trades if t['result']=='BE+']
    l=[t for t in trades if t['result']=='LOSS']
    total=len(trades); wr=len(w)/(len(w)+len(l))*100 if (w or l) else 0
    # Use weighted PnL (accounts for sizing)
    total_wpnl = sum(t['weighted_pnl'] for t in trades)
    avg_wpnl   = total_wpnl / total
    gw=sum(t['weighted_pnl'] for t in w)+sum(t['weighted_pnl'] for t in b)
    gl=abs(sum(t['weighted_pnl'] for t in l))
    pf=gw/gl if gl else 99
    trades_mo=total/MONTHS; ev_mo=avg_wpnl*trades_mo
    return {'label':label,'total':total,'wins':len(w),'beps':len(b),'losses':len(l),
            'wr':round(wr,1),'pf':round(pf,2),'avg':round(avg_wpnl,3),
            'total_pnl':round(total_wpnl,1),'trades_mo':round(trades_mo,1),
            'ev_mo':round(ev_mo,1)}

def run(coins, direction='SHORT', mfi_ob=80, partial_exit=1.0,
        trail_gap=0.5, size_tiers=None):
    all_t=[]
    for sym in coins:
        if f"{sym}_6h" not in cache: continue
        all_t.extend(simulate(sym, direction, mfi_ob, 3, partial_exit, trail_gap, size_tiers))
    return all_t

# ══════════════════════════════════════════════════════════════════
print("\n" + "═"*70)
print("  MFI CASCADE SHORT — OPTIMIZATION ANALYSIS")
print("  Base: Top 8 coins, SL 3%, TP 4.5%, Trail-B, SHORT only")
print("═"*70)

scenarios = []

# ── BASELINE ──────────────────────────────────────────────────────
t = run(TOP8)
scenarios.append(stats(t, "Baseline (Top 8, current)"))

# ── LEVER 1: Top 10 coins ─────────────────────────────────────────
t = run(TOP10)
scenarios.append(stats(t, "L1: Top 10 coins (+INJ, HYPE)"))

# ── LEVER 2: Partial exit + runner ────────────────────────────────
t = run(TOP8, partial_exit=0.5, trail_gap=0.5)
scenarios.append(stats(t, "L2: 50% runner @ 0.5% trail"))

t = run(TOP8, partial_exit=0.5, trail_gap=1.0)
scenarios.append(stats(t, "L2b: 50% runner @ 1.0% trail"))

# ── LEVER 3: MFI-tiered sizing ────────────────────────────────────
tiers_1 = [(90,2.0),(85,1.5),(80,1.0)]
t = run(TOP8, size_tiers=tiers_1)
scenarios.append(stats(t, "L3: Tiered size 1x/1.5x/2x (80/85/90)"))

tiers_2 = [(85,1.5),(80,1.0)]
t = run(TOP8, size_tiers=tiers_2)
scenarios.append(stats(t, "L3b: Tiered size 1x/1.5x (80/85)"))

# ── LEVER 4: Tighter MFI threshold ───────────────────────────────
t = run(TOP8, mfi_ob=85)
scenarios.append(stats(t, "L4: MFI ≥ 85 (tighter signal)"))

# ── LEVER 5: Add LONG signals ─────────────────────────────────────
t_short = run(TOP8)
t_long  = run(TOP8, direction='LONG')
t_both  = t_short + t_long
scenarios.append(stats(t_long,  "L5: LONG only (MFI < 20)"))
scenarios.append(stats(t_both,  "L5b: SHORT + LONG combined"))

# ── LEVER 6: Combined best ────────────────────────────────────────
# Top 10 + tiered sizing + LONG
t_s = run(TOP10, size_tiers=tiers_1)
t_l = run(TOP10, direction='LONG', size_tiers=tiers_1)
t_combo = t_s + t_l
scenarios.append(stats(t_combo, "L6: Top10 + tiered + LONG (combined)"))

# Top 10 + tiered + runner + LONG
t_s2 = run(TOP10, partial_exit=0.5, trail_gap=0.5, size_tiers=tiers_1)
t_l2 = run(TOP10, direction='LONG', partial_exit=0.5, trail_gap=0.5, size_tiers=tiers_1)
t_all = t_s2 + t_l2
scenarios.append(stats(t_all, "L7: ALL levers combined"))

# ── Print results ─────────────────────────────────────────────────
print(f"\n  {'Scenario':<40} {'Trades':>6} {'/Mo':>5}  {'WR':>6}  {'PF':>5}  {'Avg/tr':>7}  {'EV/mo':>7}")
print("  " + "-"*82)

base_ev = scenarios[0]['ev_mo']
for s in scenarios:
    delta = f"(+{s['ev_mo']-base_ev:.1f}%)" if s['ev_mo'] != base_ev else "(base)"
    flag  = " ◄ 20%!" if s['ev_mo'] >= 20 else (" ◄ close" if s['ev_mo'] >= 16 else "")
    print(f"  {s['label']:<40} {s['total']:>6} {s['trades_mo']:>4.1f}/mo  {s['wr']:>5.1f}%  {s['pf']:>5.2f}  {s['avg']:>+6.2f}%  {s['ev_mo']:>+6.1f}% {delta}{flag}")

# ── Detailed breakdown of best scenario ──────────────────────────
print()
print("═"*70)
print("  BEST SCENARIO DETAIL — L6: Top10 + Tiered Sizing + LONG")
print("═"*70)

for sym in TOP10:
    if f"{sym}_6h" not in cache: continue
    coin = sym.replace('/USDT','')
    ts = run([sym], size_tiers=tiers_1) + run([sym], direction='LONG', size_tiers=tiers_1)
    if not ts: continue
    w=sum(1 for t in ts if t['result']=='WIN'); l=sum(1 for t in ts if t['result']=='LOSS')
    wr=w/(w+l)*100 if (w+l) else 0
    tp=round(sum(t['weighted_pnl'] for t in ts),1); avg=tp/len(ts)
    longs=sum(1 for t in ts if t['direction']=='LONG'); shorts=sum(1 for t in ts if t['direction']=='SHORT')
    print(f"  {coin:<6} {len(ts):>3} trades ({len(ts)/MONTHS:.1f}/mo)  WR {wr:.0f}%  PnL {tp:+.1f}%  "
          f"avg {avg:+.2f}%  [{shorts}S / {longs}L]")

# ── MFI tier distribution ─────────────────────────────────────────
print()
print("  MFI PEAK DISTRIBUTION (sizing lever impact):")
all_bt = run(TOP8, size_tiers=tiers_1)
for tier_label, (lo, hi) in [("MFI 90+ (2x)", (90,100)), ("MFI 85-89 (1.5x)", (85,89.99)), ("MFI 80-84 (1x)", (80,84.99))]:
    bucket = [t for t in all_bt if lo <= t['mfi_peak'] <= hi]
    if not bucket: continue
    bw=sum(1 for t in bucket if t['result']=='WIN'); bl=sum(1 for t in bucket if t['result']=='LOSS')
    bwr=bw/(bw+bl)*100 if (bw+bl) else 0
    print(f"  {tier_label:<25} {len(bucket):>3} trades  WR {bwr:.0f}%  avg {sum(t['pnl'] for t in bucket)/len(bucket):+.2f}%")

# ── Final verdict ─────────────────────────────────────────────────
best = max(scenarios, key=lambda s: s['ev_mo'])
print()
print("═"*70)
print("  VERDICT — PATH TO 20%/MONTH")
print("═"*70)
print(f"  Baseline (current v1.1):   {base_ev:+.1f}%/month")
print(f"  Max achievable (backtest): {best['ev_mo']:+.1f}%/month  ({best['label']})")
print()
for s in scenarios:
    if s['ev_mo'] >= 20:
        print(f"  ✓ {s['label']}: {s['ev_mo']:+.1f}%/month  REACHES TARGET")
    elif s['ev_mo'] >= 16:
        print(f"  ~ {s['label']}: {s['ev_mo']:+.1f}%/month  within range")
print("═"*70)
