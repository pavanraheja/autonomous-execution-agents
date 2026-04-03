"""
Alligator LONG Backtest + Double-Down Analysis
═══════════════════════════════════════════════
1. LONG performance per coin (bull market simulation)
2. SHORT vs LONG head-to-head per coin
3. Ultra-tight spread double-down sizing test
4. Seasonal / half-year split (first 90d vs last 90d)
5. Best LONG configuration for Oct bull market
Period: 180 days
"""

import ccxt, pickle, os, time
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone

DAYS_BACK = 180
CACHE     = "/tmp/alligator_backtest.pkl"

CANDIDATES = [
    'BTC/USDT','ETH/USDT','SOL/USDT','XRP/USDT','BNB/USDT',
    'DOGE/USDT','AVAX/USDT','LINK/USDT','ADA/USDT','SUI/USDT',
]

JAW_P,JAW_S     = 13,8
TEETH_P,TEETH_S = 8,5
LIPS_P,LIPS_S   = 5,3

exchange = ccxt.binance({
    'enableRateLimit':True,'timeout':30000,
    'options':{'defaultType':'future'},
})

cache = {}
if os.path.exists(CACHE):
    with open(CACHE,'rb') as f: cache = pickle.load(f)

# Fetch missing
for sym in CANDIDATES:
    if f"{sym}_6h" not in cache:
        try:
            since = int((datetime.now(timezone.utc)-timedelta(days=DAYS_BACK)).timestamp()*1000)
            def fetch(s,tf):
                rows=[]
                si=since
                while True:
                    b=exchange.fetch_ohlcv(s,tf,since=si,limit=1000)
                    if not b: break
                    rows.extend(b); si=b[-1][0]+1
                    if len(b)<1000: break
                    time.sleep(0.05)
                df=pd.DataFrame(rows,columns=['ts','open','high','low','close','vol'])
                df['ts']=pd.to_datetime(df['ts'],unit='ms',utc=True)
                return df.drop_duplicates('ts').sort_values('ts').reset_index(drop=True)
            cache[f"{sym}_6h"]=fetch(sym,'6h')
            cache[f"{sym}_2h"]=fetch(sym,'2h')
            with open(CACHE,'wb') as f: pickle.dump(cache,f)
            time.sleep(0.1)
        except: pass

valid=[s for s in CANDIDATES if f"{s}_6h" in cache]

def smma(series,n,shift):
    return series.rolling(n).mean().shift(shift)

def calc_alligator(df):
    df=df.copy()
    mid=(df['high']+df['low'])/2
    df['jaw']  =smma(mid,JAW_P,  JAW_S)
    df['teeth']=smma(mid,TEETH_P,TEETH_S)
    df['lips'] =smma(mid,LIPS_P, LIPS_S)
    df['spread_pct']=(df['jaw']-df['lips']).abs()/df['close']*100
    return df

def simulate(sym, direction, variant, sl_pct, rr, spread_max, half=None):
    tp_pct  = sl_pct * rr
    df6     = calc_alligator(cache[f"{sym}_6h"].copy())
    df2     = cache[f"{sym}_2h"].copy()
    trades  = []

    # half=1 → first 90d, half=2 → last 90d
    if half == 1:
        cutoff = df6['ts'].max() - pd.Timedelta(days=90)
        df6 = df6[df6['ts'] <= cutoff]
    elif half == 2:
        cutoff = df6['ts'].max() - pd.Timedelta(days=90)
        df6 = df6[df6['ts'] > cutoff]

    for i in range(20, len(df6)-1):
        row   = df6.iloc[i-1]
        jaw,teeth,lips,spread = row['jaw'],row['teeth'],row['lips'],row['spread_pct']
        if pd.isna(jaw): continue
        if spread_max and spread > spread_max: continue

        is_bull = lips > teeth > jaw
        is_bear = lips < teeth < jaw

        dirs = []
        if direction in ('LONG','BOTH') and is_bull:  dirs.append('LONG')
        if direction in ('SHORT','BOTH') and is_bear: dirs.append('SHORT')
        if not dirs: continue

        c6_ts = df6.iloc[i]['ts']
        sub   = df2[df2['ts'] >= c6_ts].head(4)
        if len(sub) < 2: continue

        for d in dirs:
            # Variant A
            if variant in ('A','BOTH') and len(sub) >= 2:
                s0 = sub.iloc[0]
                ok = (d=='SHORT' and s0['close']<s0['open']) or (d=='LONG' and s0['close']>s0['open'])
                if ok: _trade(trades,sym,d,'A',s0,df2,sl_pct,tp_pct,spread,c6_ts)
            # Variant C
            if variant in ('C','BOTH') and len(sub) >= 3:
                s0,s1 = sub.iloc[0],sub.iloc[1]
                ok = (d=='SHORT' and s0['close']<s0['open'] and s1['close']<s1['open']) or \
                     (d=='LONG'  and s0['close']>s0['open'] and s1['close']>s1['open'])
                if ok: _trade(trades,sym,d,'C',s1,df2,sl_pct,tp_pct,spread,c6_ts)
    return trades

def _trade(trades,sym,d,var,ec,df2,sl_pct,tp_pct,spread,c6_ts):
    e=ec['close']; ets=ec['ts']
    sl = e*(1+sl_pct/100) if d=='SHORT' else e*(1-sl_pct/100)
    tp = e*(1-tp_pct/100) if d=='SHORT' else e*(1+tp_pct/100)
    future=df2[df2['ts']>ets].head(80)
    result=None; ep=None
    for _,r in future.iterrows():
        h,l=r['high'],r['low']
        if d=='SHORT':
            if h>=sl: result='LOSS';ep=sl;break
            if l<=tp: result='WIN'; ep=tp;break
        else:
            if l<=sl: result='LOSS';ep=sl;break
            if h>=tp: result='WIN'; ep=tp;break
    if result is None and len(future)>0:
        ep=future.iloc[-1]['close']
        pnl=((e-ep)/e if d=='SHORT' else (ep-e)/e)*100
        result='WIN' if pnl>0.05 else 'LOSS'
    if result is None: return
    pnl=tp_pct if result=='WIN' else -sl_pct
    trades.append({'sym':sym,'dir':d,'var':var,'result':result,'pnl':pnl,
                   'spread':spread,'entry_ts':ets,'c6_ts':c6_ts})

def S(trades):
    if not trades: return {'n':0,'wr':0,'pf':0,'total':0,'avg':0}
    w=[t for t in trades if t['result']=='WIN']
    l=[t for t in trades if t['result']=='LOSS']
    n=len(trades); wr=len(w)/n*100
    gw=sum(t['pnl'] for t in w); gl=abs(sum(t['pnl'] for t in l))
    pf=gw/gl if gl else (99 if gw>0 else 0)
    tot=sum(t['pnl'] for t in trades); avg=tot/n
    return {'n':n,'wr':round(wr,1),'pf':round(pf,2),'total':round(tot,1),'avg':round(avg,3)}

months=DAYS_BACK/30

# ══════════════════════════════════════════════════════════════════════════════
print()
print("═"*82)
print("  SECTION 1: LONG vs SHORT — ALL COINS  (spread<2%, variant Both, SL1.5% RR2.0)")
print("═"*82)
print(f"  {'Coin':<6} {'Dir':<6} {'Trades':>7} {'Per/Mo':>7} {'WR':>6} {'PF':>6} {'Avg/tr':>8} {'Total':>8} {'Mo EV':>8}")
print("  "+"-"*74)

coin_dir_results = {}
for sym in valid:
    coin = sym.replace('/USDT','')
    for d in ['LONG','SHORT']:
        t = simulate(sym,d,'BOTH',1.5,2.0,2.0)
        s = S(t)
        coin_dir_results[(coin,d)] = s
        mo = s['avg']*(s['n']/months) if s['n'] else 0
        if s['n'] >= 3:
            print(f"  {coin:<6} {d:<6} {s['n']:>7} {s['n']/months:>6.1f}/mo {s['wr']:>5.1f}% {s['pf']:>6.2f} {s['avg']:>+8.3f}% {s['total']:>+7.1f}% {mo:>+7.2f}%")
        else:
            print(f"  {coin:<6} {d:<6} {s['n']:>7}  (too few)")

print()
print("═"*82)
print("  SECTION 2: LONG — SPREAD FILTER IMPACT (all coins combined)")
print("═"*82)
print(f"  {'Spread':>12} {'Trades':>7} {'Per/Mo':>7} {'WR':>6} {'PF':>6} {'Avg/tr':>8} {'Total':>8} {'Mo EV':>8}")
print("  "+"-"*74)

for sp in [None,3.0,2.0,1.5,1.0,0.5]:
    t=[]
    for sym in valid:
        t+=simulate(sym,'LONG','BOTH',1.5,2.0,sp)
    s=S(t)
    if s['n']==0: continue
    mo=s['avg']*(s['n']/months)
    label=f"<{sp}%" if sp else "No filter"
    print(f"  {label:>12} {s['n']:>7} {s['n']/months:>6.1f}/mo {s['wr']:>5.1f}% {s['pf']:>6.2f} {s['avg']:>+8.3f}% {s['total']:>+7.1f}% {mo:>+7.2f}%")

print()
print("═"*82)
print("  SECTION 3: LONG — BEST COINS (spread<2%)")
print("═"*82)
print(f"  {'Coin':<7} {'Trades':>7} {'Per/Mo':>7} {'WR':>6} {'PF':>6} {'Avg/tr':>8} {'Total':>8} {'Mo EV':>8}  Verdict")
print("  "+"-"*80)

long_qualified = []
for sym in valid:
    t=simulate(sym,'LONG','BOTH',1.5,2.0,2.0)
    s=S(t)
    if s['n']<5:
        print(f"  {sym.replace('/USDT',''):<7} {s['n']:>7}  (too few trades)")
        continue
    mo=s['avg']*(s['n']/months)
    ok = s['wr']>=45 and s['pf']>=1.2 and s['n']>=5
    verdict = "✓ PASS" if ok else "✗"
    if ok: long_qualified.append(sym)
    print(f"  {sym.replace('/USDT',''):<7} {s['n']:>7} {s['n']/months:>6.1f}/mo {s['wr']:>5.1f}% {s['pf']:>6.2f} {s['avg']:>+8.3f}% {s['total']:>+7.1f}% {mo:>+7.2f}%  {verdict}")

print()
print("═"*82)
print("  SECTION 4: LONG — SL/RR MATRIX  (spread<2%, best coins)")
print("═"*82)
print(f"  {'SL':>5} {'RR':>5} {'TP':>6} {'Trades':>7} {'Per/Mo':>7} {'WR':>6} {'PF':>6} {'Avg/tr':>8} {'Total':>8} {'Mo EV':>8}")
print("  "+"-"*80)

best_long_rr = []
coins_for_rr = long_qualified if long_qualified else valid[:3]
for sl in [0.75,1.0,1.5,2.0]:
    for rr in [1.5,2.0,2.5,3.0]:
        t=[]
        for sym in coins_for_rr:
            t+=simulate(sym,'LONG','BOTH',sl,rr,2.0)
        s=S(t)
        if s['n']<5: continue
        mo=s['avg']*(s['n']/months)
        best_long_rr.append((sl,rr,s,mo))
        print(f"  {sl:>4.2f}% {rr:>4.1f}× {sl*rr:>5.2f}% {s['n']:>7} {s['n']/months:>6.1f}/mo {s['wr']:>5.1f}% {s['pf']:>6.2f} {s['avg']:>+8.3f}% {s['total']:>+7.1f}% {mo:>+7.2f}%")

print()
print("═"*82)
print("  SECTION 5: ULTRA-TIGHT SPREAD DOUBLE-DOWN  (spread<0.5% vs <2% sizing)")
print("  Live trades with spread<0.5%: BTC +3%, ETH +3%, ETH +3% → 3/3 TP hits")
print("═"*82)
print(f"  {'Spread':>10} {'Size':>8} {'Trades':>7} {'Per/Mo':>7} {'WR':>6} {'PF':>6} {'Avg/tr':>8} {'Mo EV (1x)':>12} {'Mo EV (2x)':>12}")
print("  "+"-"*84)

for sp,lbl in [(2.0,'<2% 1×'),(1.0,'<1% 1×'),(0.5,'<0.5% 1×')]:
    t=[]
    for sym in valid[:4]:  # BTC ETH SOL XRP
        t+=simulate(sym,'SHORT','BOTH',1.5,2.0,sp)
    s=S(t)
    if s['n']==0: continue
    mo=s['avg']*(s['n']/months)
    mo2=mo*2
    print(f"  {lbl:>10}   {'1×':>8} {s['n']:>7} {s['n']/months:>6.1f}/mo {s['wr']:>5.1f}% {s['pf']:>6.2f} {s['avg']:>+8.3f}% {mo:>+11.2f}% {mo2:>+11.2f}% (if 2×)")

# Now test mixed sizing: 2× when spread<0.5%, 1× otherwise
print()
print("  --- Mixed sizing: 2× for spread<0.5%, 1× for spread 0.5-2% ---")
for sym_set,label in [(['BTC/USDT','ETH/USDT'],'BTC+ETH'),
                       (['XRP/USDT','LINK/USDT'],'XRP+LINK')]:
    all_t=[]
    for sym in sym_set:
        t=simulate(sym,'SHORT','BOTH',1.5,2.0,2.0)
        all_t+=t
    if not all_t: continue
    # Apply mixed sizing: weight pnl by 2 if spread<0.5
    weighted_pnl=0; total_units=0
    wins=0; losses=0
    for t in all_t:
        mult=2.0 if t['spread']<0.5 else 1.0
        weighted_pnl+=t['pnl']*mult
        total_units+=mult
        if t['result']=='WIN': wins+=1
        else: losses+=1
    n=len(all_t)
    wr=wins/n*100
    avg_w=weighted_pnl/total_units if total_units else 0
    mo_ev_w=avg_w*(total_units/months)
    print(f"  {label} mixed 2×/1×: {n} trades | WR {wr:.0f}% | Weighted avg {avg_w:+.3f}% | Mo EV {mo_ev_w:+.2f}%")

print()
print("═"*82)
print("  SECTION 6: TEMPORAL — FIRST 90d vs LAST 90d (market condition split)")
print("  First 90d ≈ Oct–Dec (earlier data) | Last 90d ≈ Jan–Mar (recent)")
print("═"*82)
print(f"  {'Period':<15} {'Dir':<7} {'Trades':>7} {'WR':>6} {'PF':>6} {'Avg/tr':>8} {'Total':>8}")
print("  "+"-"*65)

for half,label in [(1,'First 90d'),(2,'Last 90d (now)')]:
    for d in ['LONG','SHORT']:
        t=[]
        for sym in valid[:4]:
            t+=simulate(sym,d,'BOTH',1.5,2.0,2.0,half=half)
        s=S(t)
        if s['n']<3: continue
        print(f"  {label:<15} {d:<7} {s['n']:>7} {s['wr']:>5.1f}% {s['pf']:>6.2f} {s['avg']:>+8.3f}% {s['total']:>+7.1f}%")

print()
print("═"*82)
print("  SECTION 7: BEST BULL MARKET CONFIG — LONG qualified coins, best RR")
print("═"*82)
if long_qualified:
    print(f"  Long-qualified coins: {', '.join(s.replace('/USDT','') for s in long_qualified)}")
    print()
    best = sorted(best_long_rr, key=lambda x: x[3], reverse=True)[:5]
    for sl,rr,s,mo in best:
        print(f"  SL {sl}% / TP {sl*rr:.2f}% (RR {rr}×)  →  {s['n']} trades | WR {s['wr']}% | PF {s['pf']} | Avg {s['avg']:+.3f}% | Mo EV {mo:+.2f}%")
else:
    print("  No coins passed all thresholds — showing top 3 by monthly EV")
    best=sorted([(sym,S(simulate(sym,'LONG','BOTH',1.5,2.0,2.0))) for sym in valid],
                key=lambda x:-x[1]['avg']*x[1]['n'])[:3]
    for sym,s in best:
        mo=s['avg']*(s['n']/months)
        print(f"  {sym.replace('/USDT','')}: {s['n']} trades | WR {s['wr']}% | PF {s['pf']} | Mo EV {mo:+.2f}%")

print()
print("═"*82)
print("  SECTION 8: COMBINED PORTFOLIO — BEAR (SHORT) + BULL (LONG) SCENARIO")
print("  Bear coins: XRP+LINK SHORT | Bull coins: LONG qualified | Spread<2%")
print("═"*82)
short_coins=['XRP/USDT','LINK/USDT']
long_coins = long_qualified if long_qualified else ['BTC/USDT','ETH/USDT']
for sl,rr in [(1.5,2.0),(1.5,2.5),(2.0,2.5)]:
    bear_t=[]; bull_t=[]
    for s in short_coins: bear_t+=simulate(s,'SHORT','BOTH',sl,rr,2.0)
    for s in long_coins:  bull_t+=simulate(s,'LONG', 'BOTH',sl,rr,2.0)
    bs=S(bear_t); ls=S(bull_t)
    all_t=bear_t+bull_t; cs=S(all_t)
    bmo=bs['avg']*(bs['n']/months); lmo=ls['avg']*(ls['n']/months); cmo=cs['avg']*(cs['n']/months)
    print(f"  SL {sl}% RR {rr}×:")
    print(f"    SHORT (XRP+LINK): {bs['n']}tr | WR {bs['wr']}% | PF {bs['pf']} | Mo {bmo:+.2f}%")
    print(f"    LONG  ({'+'.join(s.replace('/USDT','') for s in long_coins)}): {ls['n']}tr | WR {ls['wr']}% | PF {ls['pf']} | Mo {lmo:+.2f}%")
    print(f"    COMBINED: {cs['n']}tr/180d | WR {cs['wr']}% | PF {cs['pf']} | Mo EV {cmo:+.2f}%")
    print()
