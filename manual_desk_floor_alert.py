#!/usr/bin/env python3
"""
Manual Desk floor alert — READ-ONLY. Places no orders.

Watches Binance Account C (Manual Desk, Portfolio Margin) equity against a
trailing floor = FLOOR_PCT x highest equity seen, and messages Telegram so the
perps can be closed by hand. Also flags uniMMR danger and the $200k target.

Backtest basis (2026-09-19, agent_risk_memory.md): one account floor at 40% of
peak, no per-coin stops, gave the best 2y odds of $200k without liquidation risk.

Cron: */2 * * * *   Flags: --dry (print only)  --test (send a test message)
"""
import os, sys, json, time, urllib.parse, urllib.request
from datetime import datetime, timezone
import ccxt

FLOOR_PCT = 0.40        # close all perps at/below this share of peak equity
WATCH_PCT = 0.60        # early warning
DANGER_PCT = 0.48       # act-soon warning
UNIMMR_DANGER = 3.0     # Binance liquidates < 1.05; its own warnings come too late
TARGET_TOTAL = 200_000  # all Account C wallets + AlphaGrid (Account B)
STATE = "/opt/trader/state/manual_desk_floor.json"
REPEAT = {"WATCH": 12 * 3600, "DANGER": 3600, "FLOOR": 900, "MMR": 900}
DAILY_UTC_HOUR = 5      # 09:00 Dubai

def env(path):
    out = {}
    for line in open(path):
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out

def tg(msg, dry):
    """Returns True only if delivered, so unsent alerts are retried next run."""
    if dry:
        print("[TG]", msg.replace("\n", " | ")); return True
    try:
        a = env("/opt/trader/.env.alerts")
        data = urllib.parse.urlencode({"chat_id": a["TG_CHAT_ID"], "text": msg, "parse_mode": "HTML"}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{a['TG_BOT_TOKEN']}/sendMessage", data=data, timeout=10).read()
        return True
    except Exception as e:
        print("TG send failed:", str(e)[:120]); return False

def load_state():
    try: return json.load(open(STATE))
    except Exception: return {}

def snapshot():
    k = env("/opt/trader/.env.live")
    C = ccxt.binance({"apiKey": k["BINANCE_MANUAL_API_KEY"], "secret": k["BINANCE_MANUAL_SECRET"]})
    acct = C.papi_get_account()
    btc = float(ccxt.binance().fetch_ticker("BTC/USDT")["last"])
    c_total = sum(float(w["balance"]) for w in C.sapi_get_asset_wallet_balance()) * btc
    perps = [(p["symbol"], float(p["positionAmt"]), float(p["markPrice"]))
             for p in C.papi_get_um_positionrisk() if float(p["positionAmt"]) != 0]
    B = ccxt.binanceusdm({"apiKey": k["BINANCE_LIVE_API_KEY"], "secret": k["BINANCE_LIVE_SECRET"]})
    b_total = float(B.fetch_balance()["info"]["totalMarginBalance"])
    return {"equity": float(acct["actualEquity"]), "unimmr": float(acct["uniMMR"]),
            "c_total": c_total, "b_total": b_total, "perps": perps}

def main():
    dry, test = "--dry" in sys.argv, "--test" in sys.argv
    st = load_state(); now = time.time(); ts = datetime.now(timezone.utc)
    try:
        s = snapshot()
        if st.get("fails", 0) >= 3:   # tell him the blind spot is over, and how long it lasted
            tg(f"✅ <b>Manual Desk floor alert recovered</b> after {st['fails']} failed checks "
               f"(~{st['fails'] * 2 / 60:.0f}h blind).", dry)
        st["fails"] = 0; st.pop("blind_ts", None)
    except Exception as e:
        st["fails"] = st.get("fails", 0) + 1
        print(ts.isoformat(), "snapshot failed:", str(e)[:160])
        # Silent failure is the real danger for an alert. 2026-10-10: a single warning at the
        # 3rd failure left it blind for 34h unnoticed — now repeat every 6h while blind.
        if st["fails"] >= 3 and now - st.get("blind_ts", 0) >= 6 * 3600:
            if tg("⚠️ <b>Manual Desk floor alert is BLIND</b>\n"
                  f"{st['fails']} checks in a row failed to read Binance (~{st['fails'] * 2 / 60:.1f}h).\n"
                  f"Last error: {str(e)[:120]}", dry):
                st["blind_ts"] = now
        if not dry: json.dump(st, open(STATE, "w"))
        return
    eq = s["equity"]
    peak = max(st.get("peak", eq), eq)
    if eq >= st.get("peak", 0): st["peak_ts"] = ts.isoformat()
    st["peak"] = peak
    floor = FLOOR_PCT * peak
    total = s["c_total"] + s["b_total"]
    notional = sum(abs(q) * m for _, q, m in s["perps"])
    pct = eq / peak
    tier = ("FLOOR" if pct <= FLOOR_PCT else "DANGER" if pct <= DANGER_PCT else
            "WATCH" if pct <= WATCH_PCT else None)
    if s["unimmr"] < UNIMMR_DANGER: tier = "MMR"
    if notional < 50: tier = None   # no perps left → spot only, nothing to close, nothing to liquidate
    head = (f"Equity <b>${eq:,.0f}</b> ({(pct-1)*100:+.0f}% from peak ${peak:,.0f})\n"
            f"Floor ${floor:,.0f} · uniMMR {s['unimmr']:.1f} · perps ${notional:,.0f}\n"
            f"Total (C+B) ${total:,.0f} / $200k")
    perps_txt = "\n".join(f"  {sym} {q:g}" for sym, q, _ in s["perps"])
    print(ts.isoformat(), f"eq={eq:.0f} peak={peak:.0f} floor={floor:.0f} pct={pct:.3f} "
          f"unimmr={s['unimmr']:.1f} total={total:.0f} tier={tier}")
    if test:
        tg("🧪 <b>TEST — Manual Desk floor alert online</b> (read-only, no orders)\n" + head, dry)
    last, last_ts = st.get("tier"), st.get("tier_ts", 0)
    rank = {None: 0, "WATCH": 1, "DANGER": 2, "FLOOR": 3, "MMR": 4}
    if tier and (rank[tier] > rank.get(last, 0) or now - last_ts >= REPEAT[tier]):
        msg = {"WATCH": "🟡 <b>Manual Desk — WATCH</b> (equity ≤60% of peak)",
               "DANGER": "🟠 <b>Manual Desk — DANGER</b> (equity ≤48% of peak) — floor is close",
               "FLOOR": "🔴 <b>Manual Desk — FLOOR HIT</b>\n<b>Close ALL perps now, keep spot.</b>",
               "MMR": "🔴 <b>Manual Desk — uniMMR below 3</b>\nLiquidation zone is near. <b>Close perps now.</b>"}[tier]
        if tg(msg + "\n" + head + ("\nPerps to close:\n" + perps_txt if tier in ("FLOOR", "MMR") else ""), dry):
            st["tier"], st["tier_ts"] = tier, now
    elif tier is None and last and notional >= 50 and pct > WATCH_PCT + 0.03:
        if tg("🟢 <b>Manual Desk — back above warning levels</b>\n" + head, dry):
            st["tier"], st["tier_ts"] = None, now
    elif tier is None and last and notional < 50:
        if tg("✅ <b>Manual Desk — no perps open</b>, spot only. Floor watch paused until perps reopen.\n" + head, dry):
            st["tier"], st["tier_ts"] = None, now
    if total >= TARGET_TOTAL and not st.get("target_sent"):
        if tg("🎯 <b>$200k TARGET HIT</b> (Account C + AlphaGrid)\nPlan: move to 1x spot / stables now.\n" + head, dry):
            st["target_sent"] = True
    day = ts.strftime("%Y-%m-%d")
    if ts.hour == DAILY_UTC_HOUR and st.get("daily") != day:
        if tg("📊 <b>Manual Desk daily</b>\n" + head + f"\nDistance to floor: {(1-floor/eq)*100:.0f}%", dry):
            st["daily"] = day
    if not dry: json.dump(st, open(STATE, "w"))

if __name__ == "__main__":
    main()
