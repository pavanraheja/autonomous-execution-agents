#!/usr/bin/env python3
"""
Truth monitor — READ-ONLY. Places no orders.

Answers "are the live strategies actually trading, and does the exchange agree
with our ledgers?" — the question nobody could answer for 7+ weeks in 2026
(stop-verify race killed every AlphaGrid trade from 07-30; ledger froze 08-07;
kill switch flipped unnoticed). Uptime checks said all was fine the whole time.

Every run (cron */10):
  - controller log: any "SL UNVERIFIED" / "EMERGENCY CLOSE"  -> alert now
  - AlphaGrid kill switch changed                             -> alert now
  - a live bot's service down, or its log/state gone stale    -> alert now
  - strategies margin-starved (Bybit free < 0, Binance avail < $50) -> alert (24h)
Daily digest (05:15 UTC): exchange-truth P&L split AlphaGrid / bots / manual
(Binance clientOrderId: "AG-" = AlphaGrid, "x-" = ccxt bots, else manual),
Bybit closed P&L by symbol, controller executed/rejected counts, ledger freshness.

Flags: --dry (print, no Telegram, no state write)  --digest (force digest now)
"""
import os, sys, json, time, re, subprocess, collections, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta
import ccxt

STATE = "/opt/trader/state/truth_monitor.json"
CTRL_LOG = "/opt/trader/logs/live_controller.log"
LEDGER = "/opt/trader/state/live_portfolio.json"
SERVICES = ["trader-8095", "liq-zone-paxg", "liq-zone-xag", "dvol-live", "trader-8091"]
FRESH = {  # log file -> max age (minutes); these bots write every few minutes
    "/opt/trader/logs/live_controller.log": 30,
    "/opt/trader/logs/dvol_live.log": 15,
}
# Metal bots only write state on trade events, so file age is meaningless for them;
# ask their own /status endpoint for last_scan instead.
STATUS_PORTS = {"liq_zone_paxg": 8113, "liq_zone_xag": 8114}
SCAN_STALE_MIN = 180
DIGEST_UTC_HOUR = 5
LEDGER_STALE_H = 48

def env(path):
    out = {}
    for line in open(path):
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1); out[k.strip()] = v.strip().strip('"').strip("'")
    return out

def tg(msg, dry):
    if dry:
        print("[TG]", msg.replace("\n", " | ")); return True
    try:
        a = env("/opt/trader/.env.alerts")
        data = urllib.parse.urlencode({"chat_id": a["TG_CHAT_ID"], "text": msg[:4000], "parse_mode": "HTML"}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{a['TG_BOT_TOKEN']}/sendMessage", data=data, timeout=10).read()
        return True
    except Exception as e:
        print("TG send failed:", str(e)[:120]); return False

def scan_controller_log(st):
    """Incremental scan from the last byte offset; handles rotation/truncation."""
    size = os.path.getsize(CTRL_LOG)
    off = st.get("log_offset", size)          # first run: start at end, don't replay history
    if size < off: off = 0                    # rotated or truncated
    with open(CTRL_LOG, "rb") as f:
        f.seek(off); chunk = f.read().decode("utf-8", "replace")
    st["log_offset"] = size
    ev = {"unverified": [], "executed": [], "rejected": collections.Counter()}
    for line in chunk.splitlines():
        if "SL UNVERIFIED" in line or "EMERGENCY CLOSE" in line:
            ev["unverified"].append(line[:160])
        m = re.search(r"EXECUTING: (\w+) (\w+) (LONG|SHORT)", line)
        if m: ev["executed"].append(f"{m.group(1)} {m.group(2)} {m.group(3)}")
        m = re.search(r"REJECTED: (\w+) \w+ \w+ — ([^:(]+)", line)
        if m: ev["rejected"][f"{m.group(1)}: {m.group(2).strip()[:40]}"] += 1
    return ev

def kill_started_from_log():
    """First 'Kill switch active' rejection after the last EXECUTING line = when trading actually stopped."""
    try:
        out = subprocess.run(["grep", "-a", "-E", "EXECUTING:|Kill switch active", CTRL_LOG], capture_output=True, text=True).stdout.splitlines()
    except Exception:
        return None
    first = None
    for line in out:
        if "EXECUTING:" in line: first = None
        elif first is None: first = line[:16]
    return first + " (log time)" if first else None

def stale_bots():
    out = []
    for s in SERVICES:
        r = subprocess.run(["systemctl", "is-active", s], capture_output=True, text=True).stdout.strip()
        if r != "active": out.append(f"{s} is {r or 'unknown'}")
    now = time.time()
    for f, mx in FRESH.items():
        try:
            age = (now - os.path.getmtime(f)) / 60
            if age > mx: out.append(f"{os.path.basename(f)} not updated for {age:.0f} min (limit {mx})")
        except FileNotFoundError:
            out.append(f"{os.path.basename(f)} missing")
    for name, port in STATUS_PORTS.items():
        try:
            j = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=5).read())
            ls = datetime.fromisoformat(j["last_scan"])
            age = (datetime.now(timezone.utc) - ls).total_seconds() / 60
            if age > SCAN_STALE_MIN: out.append(f"{name} last scan {age:.0f} min ago (limit {SCAN_STALE_MIN})")
        except Exception as e:
            out.append(f"{name} /status not answering ({str(e)[:60]})")
    return out

def margin_status(k):
    # 2026-10-11: all Bybit strategies (mfi_long, XAG, DVOL) trade on the sub-account; main = manual book
    by = ccxt.bybit({"apiKey": k["BYBIT_SUB_API_KEY"], "secret": k["BYBIT_SUB_API_SECRET"]})
    bn = ccxt.binanceusdm({"apiKey": k["BINANCE_LIVE_API_KEY"], "secret": k["BINANCE_LIVE_SECRET"]})
    b_free = float(by.fetch_balance()["free"].get("USDT") or 0)
    n_avail = float(bn.fetch_balance()["info"]["availableBalance"])
    return b_free, n_avail, bn, by

def binance_truth(bn, days):
    """Realised P&L + fees per class over `days`, classified by clientOrderId."""
    now = int(time.time() * 1000); start = now - days * 86400000
    inc = bn.fapiPrivateGetIncome({"startTime": start, "endTime": now, "limit": 1000})
    syms = sorted({r["symbol"] for r in inc if r.get("symbol")})
    res = collections.defaultdict(lambda: {"pnl": 0.0, "fee": 0.0, "fills": 0})
    for s in syms:
        cid = {}
        t0 = start
        while t0 < now:                              # allOrders/userTrades windows are max 7 days
            t1 = min(t0 + 7 * 86400000 - 1, now)
            for o in bn.fapiPrivateGetAllOrders({"symbol": s, "startTime": t0, "endTime": t1, "limit": 1000}):
                cid[o["orderId"]] = o.get("clientOrderId", "")
            for f in bn.fapiPrivateGetUserTrades({"symbol": s, "startTime": t0, "endTime": t1, "limit": 1000}):
                c = cid.get(f["orderId"], "")
                klass = ("AlphaGrid " + {"LZ": "liq_zone", "HM": "htf_mfi"}.get(c.split("-")[1], c.split("-")[1])) if c.startswith("AG-") else ("bot " + s[:-4]) if c.startswith("x-") else "manual"
                r = res[klass]; r["pnl"] += float(f["realizedPnl"]); r["fills"] += 1
                if f["commissionAsset"] in ("USDT", "USDC", "BNFCR"): r["fee"] += float(f["commission"])
            t0 = t1 + 1
    return res

def bybit_truth(by, days):
    now = int(time.time() * 1000)
    r = by.privateGetV5PositionClosedPnl({"category": "linear", "startTime": now - min(days, 7) * 86400000, "endTime": now, "limit": 100})
    agg = collections.defaultdict(lambda: [0, 0.0])
    for x in r["result"]["list"]:
        agg[x["symbol"]][0] += 1; agg[x["symbol"]][1] += float(x["closedPnl"])
    return agg

def main():
    dry, force_digest = "--dry" in sys.argv, "--digest" in sys.argv
    try: st = json.load(open(STATE))
    except Exception: st = {}
    ts = datetime.now(timezone.utc); now = time.time()
    k = env("/opt/trader/.env.live")
    alerts = []

    ev = scan_controller_log(st)
    if ev["unverified"]:
        alerts.append("🔴 <b>AlphaGrid trade closed instantly (stop not confirmed)</b>\n" + "\n".join(ev["unverified"][:6]))
    d = st.setdefault("day", {"executed": 0, "rejected": {}, "unverified": 0})
    d["executed"] += len(ev["executed"]); d["unverified"] += sum("SL UNVERIFIED" in l for l in ev["unverified"])
    for r, n in ev["rejected"].items(): d["rejected"][r] = d["rejected"].get(r, 0) + n

    led = json.load(open(LEDGER)); ks = bool(led.get("kill_switch"))
    if "kill_switch" in st and ks != st["kill_switch"]:
        alerts.append(f"⚠️ <b>AlphaGrid kill switch turned {'ON — all strategy trading halted' if ks else 'OFF — strategies can trade'}</b>")
        st["kill_since"] = ts.isoformat() if ks else None
    st["kill_switch"] = ks
    if ks and not st.get("kill_since"): st["kill_since"] = kill_started_from_log() or ts.isoformat()

    for s in stale_bots():
        key = "stale:" + s.split()[0]
        if now - st.get(key, 0) > 6 * 3600:
            alerts.append("🟠 <b>Strategy bot stalled</b>\n" + s); st[key] = now

    try:
        b_free, n_avail, bn, by = margin_status(k)
        if (b_free < 0 or n_avail < 50) and now - st.get("margin_alert", 0) > 24 * 3600:
            alerts.append(f"🟠 <b>Strategies margin-starved</b>\nBybit strategy sub-account free ${b_free:,.0f} · Binance B available ${n_avail:,.0f}\n"
                          "New strategy entries will fail until margin is freed.")
            st["margin_alert"] = now
        st["fails"] = 0
    except Exception as e:
        st["fails"] = st.get("fails", 0) + 1; bn = by = None; b_free = n_avail = None
        if st["fails"] == 3: alerts.append(f"⚠️ <b>Truth monitor is BLIND</b> — exchange reads failing: {str(e)[:120]}")

    for a in alerts:
        tg(a, dry)

    day = ts.strftime("%Y-%m-%d")
    if (force_digest or (ts.hour == DIGEST_UTC_HOUR and st.get("digest") != day)) and bn is not None:
        lines = [f"🧾 <b>Truth digest</b> {day}"]
        try:
            for days in (1, 7):
                t = binance_truth(bn, days)
                part = " · ".join(f"{c} ${v['pnl'] + v['fee']:+.2f} ({v['fills']}f)" for c, v in sorted(t.items())) or "no fills"
                lines.append(f"Binance {days}d: {part}")
            bt = bybit_truth(by, 7)
            lines.append("Bybit 7d closed: " + (" · ".join(f"{s} {n}× ${p:+.2f}" for s, (n, p) in sorted(bt.items())) or "none"))
        except Exception as e:
            lines.append(f"exchange truth ERR {str(e)[:100]}")
        rej = ", ".join(f"{r} ×{n}" for r, n in sorted(d["rejected"].items(), key=lambda x: -x[1])[:5]) or "none"
        lines.append(f"AlphaGrid 24h: executed {d['executed']} · instant-closed {d['unverified']} · rejected: {rej}")
        lines.append(f"Kill switch: {'ON since ' + str(st.get('kill_since'))[:16] if ks else 'off'}")
        last_closed = led["closed"][-1].get("closed_at") if led.get("closed") else None
        if last_closed:
            age_h = (ts - datetime.fromisoformat(last_closed)).total_seconds() / 3600
            flag = " ⚠️ STALE (fills since then are missing from the ledger)" if age_h > LEDGER_STALE_H else ""
            lines.append(f"AlphaGrid ledger last close {last_closed[:10]} ({age_h/24:.0f}d ago){flag}")
        lines.append(f"Margin: Bybit sub free ${b_free:,.0f} · Binance B avail ${n_avail:,.0f}")
        if tg("\n".join(lines), dry):
            st["digest"] = day; st["day"] = {"executed": 0, "rejected": {}, "unverified": 0}
    print(ts.isoformat(), f"alerts={len(alerts)} kill={ks} exec+={len(ev['executed'])} unverified+={len(ev['unverified'])} "
          f"bybit_free={b_free} bn_avail={n_avail}")
    if not dry:
        json.dump(st, open(STATE, "w"))

if __name__ == "__main__":
    main()
