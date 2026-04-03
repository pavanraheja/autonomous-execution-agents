"""
Trading HQ — Goal Tracker
──────────────────────────
Aggregates all 7 trading ports into one dashboard.
Tracks progress toward the $30,000 / 4-week profit goal.

Dashboard: http://localhost:8088
"""

import json, os, time, threading
from datetime import datetime, date
from flask import Flask, render_template_string

app = Flask(__name__)

GOAL_USD       = 30_000
GOAL_START     = date(2026, 3, 27)   # today
GOAL_END       = date(2026, 4, 24)   # 4 weeks
TOTAL_DAYS     = (GOAL_END - GOAL_START).days   # 28

BASE = "/opt/trader/Trade Logs"

PORTS = [
    {
        "port":     8081,
        "name":     "Filtered MFI Hunter",
        "script":   "filtered_mfi_paper.py",
        "mode":     "Paper",
        "log":      "filtered_paper_trades.json",
        "size":     50,
        "notes":    "Whitelist 7 coins, 67% WR backtest. Fresh start.",
        "optimize": "Accumulate 15 trades, validate WR holds live",
    },
    {
        "port":     8082,
        "name":     "MFI Cascade SHORT",
        "script":   "cascade_short.py",
        "mode":     "Paper",
        "log":      "cascade_trades.json",
        "size":     50,
        "notes":    "No trades yet — strict multi-level filter.",
        "optimize": "Relax signal filters, check scan logic",
    },
    {
        "port":     8083,
        "name":     "MFI Monitor LIVE",
        "script":   "mfi_live_trader_8083.py",
        "mode":     "LIVE",
        "log":      "live_monitor_trades.json",
        "size":     50,
        "notes":    "4 closed trades. WR 25%. $155 wallet.",
        "optimize": "Monitor entries, check signal vs 18 OB coins",
    },
    {
        "port":     8085,
        "name":     "HTF MFI Scalper",
        "script":   "htf_mfi_paper_trader.py",
        "mode":     "Paper",
        "log":      "htf_mfi_paper_trades.json",
        "size":     50,
        "notes":    "SL-1.5% wins. Direction now LONG (BTC+ETH).",
        "optimize": "Accumulate 20 trades, then standardise SL-1.5%",
    },
    {
        "port":     8086,
        "name":     "Williams Alligator",
        "script":   "alligator_paper_trader.py",
        "mode":     "Paper",
        "log":      "alligator_trades.json",
        "size":     2000,
        "notes":    "17-trade history restored. PF 1.82. Variant C wins.",
        "optimize": "Drop PAXG, go Variant C only, let it build",
    },
    {
        "port":     8087,
        "name":     "MFI Monitor Paper",
        "script":   "mfi_monitor.py",
        "mode":     "Paper",
        "log":      "paper_monitor_trades.json",
        "size":     1000,
        "notes":    "2 open paper trades in profit.",
        "optimize": "Let open trades close, evaluate partial close system",
    },
]

def load_port_stats(p):
    path = os.path.join(BASE, p["log"])
    if not os.path.exists(path):
        return {**p, "open": 0, "closed": 0, "wins": 0, "losses": 0,
                "wr": 0, "closed_pnl": 0.0, "open_pnl": 0.0, "total_pnl": 0.0,
                "open_trades": []}

    with open(path) as f:
        trades = json.load(f)

    open_t   = [t for t in trades if t.get("status") == "OPEN"]
    closed_t = [t for t in trades if t.get("status") != "OPEN"]
    wins     = [t for t in closed_t if t.get("result") in ("WIN",) or t.get("status") == "WIN"]
    losses   = [t for t in closed_t if t.get("result") in ("LOSS",) or t.get("status") == "LOSS"]
    wr       = round(len(wins) / len(closed_t) * 100, 1) if closed_t else 0

    # Closed PnL
    closed_pnl = 0.0
    for t in closed_t:
        if t.get("pnl_usd") is not None:
            closed_pnl += float(t["pnl_usd"])
        elif t.get("pnl_pct") is not None:
            sz = float(t.get("position_usd") or t.get("notional") or p["size"])
            closed_pnl += float(t["pnl_pct"]) / 100 * sz

    # Open unrealised (+ locked partials for 8087)
    open_pnl = 0.0
    for t in open_t:
        open_pnl += float(t.get("unrealised_usd") or 0)
        open_pnl += float(t.get("partial_close_usd") or 0)

    # Open trade summaries
    open_details = []
    for t in open_t:
        pnl_pct = t.get("unrealised_pnl") or 0
        pnl_usd = (t.get("unrealised_usd") or 0) + (t.get("partial_close_usd") or 0)
        open_details.append({
            "symbol":  t.get("symbol", "?"),
            "pnl_pct": round(float(pnl_pct), 2),
            "pnl_usd": round(float(pnl_usd), 2),
            "partial": t.get("partial_closed", False),
        })

    total_pnl = round(closed_pnl + open_pnl, 2)

    return {
        **p,
        "open":        len(open_t),
        "closed":      len(closed_t),
        "wins":        len(wins),
        "losses":      len(losses),
        "wr":          wr,
        "closed_pnl":  round(closed_pnl, 2),
        "open_pnl":    round(open_pnl, 2),
        "total_pnl":   total_pnl,
        "open_trades": open_details,
    }

HTML = r"""
<!DOCTYPE html>
<html>
<head>
    <title>Trading HQ — $30k Goal</title>
    <meta http-equiv="refresh" content="120">
    <style>
        * { margin:0; padding:0; box-sizing:border-box; }
        body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
               background:#0d0d0d; color:#e0e0e0; padding:30px; }
        h1   { font-size:26px; color:#fff; margin-bottom:4px; }
        .sub { color:#555; font-size:13px; margin-bottom:28px; }

        /* ── GOAL CARD ── */
        .goal-card {
            background: linear-gradient(135deg, #0f0f1a 0%, #1a0a2e 100%);
            border: 1px solid #7c3aed;
            border-radius: 16px;
            padding: 28px 32px;
            margin-bottom: 32px;
        }
        .goal-title { font-size:13px; color:#7c3aed; text-transform:uppercase;
                      letter-spacing:2px; margin-bottom:8px; }
        .goal-amount { font-size:48px; font-weight:800; color:#fff; line-height:1; }
        .goal-amount span { color:#7c3aed; }
        .goal-meta { display:flex; gap:32px; margin-top:16px; flex-wrap:wrap; }
        .goal-meta .item { }
        .goal-meta .label { font-size:11px; color:#555; text-transform:uppercase;
                            letter-spacing:1px; margin-bottom:2px; }
        .goal-meta .val { font-size:18px; font-weight:700; color:#fff; }
        .goal-meta .val.green { color:#00c853; }
        .goal-meta .val.amber { color:#ffd600; }
        .goal-meta .val.red   { color:#ff1744; }

        /* ── PROGRESS BAR ── */
        .progress-wrap { margin-top:22px; }
        .progress-label { display:flex; justify-content:space-between;
                          font-size:12px; color:#555; margin-bottom:6px; }
        .progress-track { background:#1a1a1a; border-radius:8px; height:20px;
                          overflow:hidden; border:1px solid #2a2a2a; }
        .progress-fill  { height:100%; border-radius:8px;
                          background: linear-gradient(90deg, #7c3aed, #00c853);
                          transition: width 0.5s ease;
                          display:flex; align-items:center; justify-content:flex-end;
                          padding-right:8px; font-size:11px; font-weight:700; color:#fff; }
        .milestones { display:flex; gap:8px; margin-top:8px; flex-wrap:wrap; }
        .ms { font-size:11px; color:#333; }
        .ms.done { color:#00c853; }

        /* ── DAILY TARGET ── */
        .daily-bar { background:#111; border:1px solid #222; border-radius:12px;
                     padding:16px 24px; margin-bottom:28px;
                     display:flex; gap:32px; flex-wrap:wrap; align-items:center; }
        .daily-bar .item .label { font-size:11px; color:#555; text-transform:uppercase;
                                   letter-spacing:1px; margin-bottom:2px; }
        .daily-bar .item .val { font-size:20px; font-weight:700; }

        h2 { font-size:13px; color:#888; margin:28px 0 12px;
             text-transform:uppercase; letter-spacing:1.5px; }

        /* ── PORT CARDS ── */
        .port-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(340px,1fr));
                     gap:16px; margin-bottom:32px; }
        .port-card { background:#111; border:1px solid #1e1e1e; border-radius:12px;
                     padding:18px 20px; }
        .port-card.live-card { border-color:#ff1744; background:#110005; }
        .port-card.best-card { border-color:#00c853; background:#001105; }
        .port-header { display:flex; justify-content:space-between; align-items:flex-start;
                       margin-bottom:12px; }
        .port-name { font-size:15px; font-weight:700; color:#fff; }
        .port-port { font-size:11px; color:#555; margin-top:2px; }
        .mode-badge { font-size:10px; font-weight:700; padding:3px 8px; border-radius:4px; }
        .mode-live  { background:#1a0000; color:#ff1744; border:1px solid #ff1744; }
        .mode-paper { background:#1a1a2e; color:#7c3aed; border:1px solid #7c3aed; }

        .port-stats { display:flex; gap:14px; margin-bottom:12px; }
        .stat { text-align:center; }
        .stat .lbl { font-size:10px; color:#444; text-transform:uppercase; letter-spacing:1px; }
        .stat .val { font-size:16px; font-weight:700; color:#fff; margin-top:1px; }
        .stat .val.green { color:#00c853; }
        .stat .val.red   { color:#ff1744; }
        .stat .val.amber { color:#ffd600; }

        .pnl-row { display:flex; gap:10px; margin-bottom:10px; }
        .pnl-box { flex:1; background:#0d0d0d; border-radius:8px; padding:8px 12px;
                   border:1px solid #1a1a1a; }
        .pnl-box .lbl { font-size:10px; color:#444; text-transform:uppercase; letter-spacing:1px; }
        .pnl-box .val { font-size:15px; font-weight:700; margin-top:2px; }
        .green { color:#00c853; }
        .red   { color:#ff1744; }
        .amber { color:#ffd600; }
        .grey  { color:#555; }

        .optimize-tag { background:#1a1a00; border:1px solid #333; border-radius:6px;
                        padding:6px 10px; font-size:11px; color:#888; margin-bottom:8px; }
        .optimize-tag strong { color:#ffd600; font-size:10px; text-transform:uppercase;
                               letter-spacing:1px; display:block; margin-bottom:2px; }

        .open-trades { margin-top:10px; border-top:1px solid #1a1a1a; padding-top:8px; }
        .open-trades .lbl { font-size:10px; color:#555; text-transform:uppercase;
                            letter-spacing:1px; margin-bottom:4px; }
        .trade-pill { display:inline-block; background:#0d0d0d; border:1px solid #222;
                      border-radius:4px; padding:2px 8px; font-size:11px; margin:2px; }

        .empty-tag { font-size:11px; color:#333; font-style:italic; margin-top:4px; }

        .updated { font-size:11px; color:#333; text-align:right; margin-top:20px; }
    </style>
</head>
<body>

<h1>🎯 Trading HQ</h1>
<p class="sub">All ports · Real-time aggregation · Auto-refresh 2 min · Port 8088</p>

<!-- ── BIG GOAL CARD ─────────────────────────────── -->
<div class="goal-card">
    <div class="goal-title">🚀 Mission Target</div>
    <div class="goal-amount">
        ${{ '%.0f'|format(total_pnl) }}
        <span style="font-size:22px;font-weight:400;color:#555;"> / $30,000</span>
    </div>

    <div class="progress-wrap">
        <div class="progress-label">
            <span>$0</span>
            <span style="color:#7c3aed;font-weight:700;">{{ '%.1f'|format(pct_done) }}% of goal</span>
            <span>$30,000</span>
        </div>
        <div class="progress-track">
            <div class="progress-fill" style="width:{{ progress_width }}%;">
                {% if pct_done >= 5 %}{{ '%.1f'|format(pct_done) }}%{% endif %}
            </div>
        </div>
        <div class="milestones">
            <span class="ms {{ 'done' if total_pnl >= 5000 else '' }}">▸ $5k</span>
            <span class="ms {{ 'done' if total_pnl >= 10000 else '' }}">▸ $10k</span>
            <span class="ms {{ 'done' if total_pnl >= 15000 else '' }}">▸ $15k</span>
            <span class="ms {{ 'done' if total_pnl >= 20000 else '' }}">▸ $20k</span>
            <span class="ms {{ 'done' if total_pnl >= 25000 else '' }}">▸ $25k</span>
            <span class="ms {{ 'done' if total_pnl >= 30000 else '' }}">✓ $30k 🏆</span>
        </div>
    </div>

    <div class="goal-meta">
        <div class="item">
            <div class="label">Deadline</div>
            <div class="val">Apr 24, 2026</div>
        </div>
        <div class="item">
            <div class="label">Days Elapsed</div>
            <div class="val amber">{{ days_elapsed }}</div>
        </div>
        <div class="item">
            <div class="label">Days Left</div>
            <div class="val {{ days_cls }}">{{ days_left }}</div>
        </div>
        <div class="item">
            <div class="label">Remaining</div>
            <div class="val red">${{ '%.0f'|format(remaining) }}</div>
        </div>
        <div class="item">
            <div class="label">Run Rate (actual)</div>
            <div class="val {{ daily_cls }}">
                ${{ '%.0f'|format(daily_actual) }}/day
            </div>
        </div>
        <div class="item">
            <div class="label">Needed Now</div>
            <div class="val amber">${{ '%.0f'|format(daily_needed) }}/day</div>
        </div>
    </div>
</div>

<!-- ── DAILY BREAKDOWN ───────────────────────────── -->
<div class="daily-bar">
    <div class="item">
        <div class="label">Closed PnL (all ports)</div>
        <div class="val {{ 'green' if closed_pnl >= 0 else 'red' }}">${{ '%.2f'|format(closed_pnl) }}</div>
    </div>
    <div class="item">
        <div class="label">Open Unrealised + Locked</div>
        <div class="val {{ 'green' if open_pnl >= 0 else 'red' }}">${{ '%.2f'|format(open_pnl) }}</div>
    </div>
    <div class="item">
        <div class="label">Combined Total</div>
        <div class="val {{ 'green' if total_pnl >= 0 else 'red' }}" style="font-size:24px;">${{ '%.2f'|format(total_pnl) }}</div>
    </div>
    <div class="item">
        <div class="label">Active Ports</div>
        <div class="val">{{ active_ports }}/7</div>
    </div>
    <div class="item">
        <div class="label">Open Trades</div>
        <div class="val amber">{{ total_open }}</div>
    </div>
    <div class="item">
        <div class="label">Total Closed</div>
        <div class="val">{{ total_closed }}</div>
    </div>
</div>

<!-- ── PORT CARDS ────────────────────────────────── -->
<h2>📊 Port Breakdown</h2>
<div class="port-grid">
{% for p in ports %}
{% if p.mode == 'LIVE' %}<div class="port-card live-card">{% elif p.total_pnl > 0 and p.closed > 5 %}<div class="port-card best-card">{% else %}<div class="port-card">{% endif %}

    <div class="port-header">
        <div>
            <div class="port-name">{{ p.name }}</div>
            <div class="port-port">:{{ p.port }} · {{ p.script }}</div>
        </div>
        <span class="mode-badge {{ 'mode-live' if p.mode == 'LIVE' else 'mode-paper' }}">
            {{ p.mode }}
        </span>
    </div>

    <div class="port-stats">
        <div class="stat">
            <div class="lbl">Closed</div>
            <div class="val">{{ p.closed }}</div>
        </div>
        <div class="stat">
            <div class="lbl">W / L</div>
            <div class="val">{{ p.wins }}/{{ p.losses }}</div>
        </div>
        <div class="stat">
            <div class="lbl">WR</div>
            <div class="val {{ p.wr_cls }}">{{ p.wr }}%</div>
        </div>
        <div class="stat">
            <div class="lbl">Open</div>
            <div class="val amber">{{ p.open }}</div>
        </div>
    </div>

    <div class="pnl-row">
        <div class="pnl-box">
            <div class="lbl">Closed PnL</div>
            <div class="val {{ p.cl_cls }}">
                {{ '+' if p.closed_pnl >= 0 else '' }}${{ '%.2f'|format(p.closed_pnl) }}
            </div>
        </div>
        <div class="pnl-box">
            <div class="lbl">Open + Locked</div>
            <div class="val {{ p.op_cls }}">
                {{ '+' if p.open_pnl >= 0 else '' }}${{ '%.2f'|format(p.open_pnl) }}
            </div>
        </div>
        <div class="pnl-box">
            <div class="lbl">Total</div>
            <div class="val {{ p.tot_cls }}" style="font-size:17px;">
                {{ '+' if p.total_pnl >= 0 else '' }}${{ '%.2f'|format(p.total_pnl) }}
            </div>
        </div>
    </div>

    {% if p.open_trades %}
    <div class="open-trades">
        <div class="lbl">Open positions</div>
        {% for t in p.open_trades %}
        <span class="trade-pill {{ 'green' if t.pnl_pct >= 0 else 'red' }}">
            {{ t.symbol }} {{ '+' if t.pnl_pct >= 0 else '' }}{{ t.pnl_pct }}%
            {% if t.partial %}🔒{% endif %}
        </span>
        {% endfor %}
    </div>
    {% endif %}

    <div class="optimize-tag" style="margin-top:10px;">
        <strong>🔧 Tomorrow's Focus</strong>
        {{ p.optimize }}
    </div>

</div>
{% endfor %}
</div>

<div class="updated">Last updated: {{ now }}</div>

</body>
</html>
"""

@app.route("/")
def index():
    today      = date.today()
    days_elapsed = max((today - GOAL_START).days, 0)
    days_left    = max((GOAL_END - today).days, 0)

    ports = [load_port_stats(p) for p in PORTS]

    closed_pnl = sum(p["closed_pnl"] for p in ports)
    open_pnl   = sum(p["open_pnl"]   for p in ports)
    total_pnl  = round(closed_pnl + open_pnl, 2)
    pct_done   = round(total_pnl / GOAL_USD * 100, 2) if total_pnl > 0 else 0

    remaining      = max(GOAL_USD - total_pnl, 0)
    daily_needed   = round(remaining / days_left, 2) if days_left > 0 else 0
    daily_actual   = round(total_pnl / days_elapsed, 2) if days_elapsed > 0 else 0
    progress_width = min(round(pct_done, 1), 100)

    active_ports = 7
    total_open   = sum(p["open"]   for p in ports)
    total_closed = sum(p["closed"] for p in ports)

    # Pre-compute per-port wr colour to avoid nested ternaries in Jinja2
    for p in ports:
        p["wr_cls"]  = "green" if p["wr"] >= 50 else ("amber" if p["wr"] >= 40 else "red")
        p["tot_cls"] = "green" if p["total_pnl"] >= 0 else "red"
        p["cl_cls"]  = "green" if p["closed_pnl"] >= 0 else "red"
        p["op_cls"]  = "green" if p["open_pnl"] >= 0 else "red"

    return render_template_string(HTML,
        ports          = ports,
        total_pnl      = total_pnl,
        closed_pnl     = closed_pnl,
        open_pnl       = open_pnl,
        pct_done       = pct_done,
        progress_width = progress_width,
        remaining      = remaining,
        days_elapsed   = days_elapsed,
        days_left      = days_left,
        daily_needed   = daily_needed,
        daily_actual   = daily_actual,
        daily_cls      = "green" if daily_actual >= daily_needed else "red",
        days_cls       = "red" if days_left <= 7 else ("amber" if days_left <= 14 else ""),
        active_ports   = active_ports,
        total_open     = total_open,
        total_closed   = total_closed,
        now            = datetime.now().strftime("%Y-%m-%d %H:%M"),
    )

if __name__ == "__main__":
    print("═" * 50)
    print("  Trading HQ — Goal Tracker")
    print("  Dashboard: http://localhost:8088")
    print(f"  Goal: $30,000 by {GOAL_END}")
    print("═" * 50)
    app.run(host="0.0.0.0", port=8088, debug=False)
