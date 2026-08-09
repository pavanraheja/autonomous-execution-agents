# PHIL-B live-micro — deployment runbook (Contabo 217.15.164.68)

Host choice: Contabo holds the Binance live keys (`/opt/trader/.env.live`, Account B) and the
Telegram alert config (`/opt/trader/.env.alerts`). Keys are NOT copied to Bangalore.

## 1. Deploy (DRY — places zero orders)
```
S="/Users/macbook/Downloads/Content Projects/02 - Trading Coin Hunter/Scripts/Python/.claude/worktrees/phil-b-live-micro"
sshpass -p "$PASS" ssh root@217.15.164.68 "mkdir -p /opt/trader/logs"
sshpass -p "$PASS" scp "$S/phil_b_trader.py"            root@217.15.164.68:/opt/trader/scripts/
sshpass -p "$PASS" scp "$S/deploy/phil-b-trader.service" root@217.15.164.68:/etc/systemd/system/
sshpass -p "$PASS" scp "$S/deploy/phil-b-trader.timer"   root@217.15.164.68:/etc/systemd/system/
sshpass -p "$PASS" ssh root@217.15.164.68 "systemctl daemon-reload && systemctl enable --now phil-b-trader.timer"
```

## 2. Verify
```
ssh root@217.15.164.68 "python3 /opt/trader/scripts/phil_b_trader.py --test"   # Telegram + balance
ssh root@217.15.164.68 "systemctl start phil-b-trader.service; sleep 60; tail -20 /opt/trader/logs/phil_b.log"
ssh root@217.15.164.68 "cat /opt/trader/state/phil_b.json | python3 -m json.tool | head -40"
ssh root@217.15.164.68 "systemctl list-timers phil-b-trader.timer --no-pager"
```

## 3. Go live (ONLY after 3-5 clean DRY days + Pavan sign-off)
Add `--live` to ExecStart, then reload:
```
ssh root@217.15.164.68 "cp /etc/systemd/system/phil-b-trader.service{,.bak_pre_live_$(date +%Y%m%d)} && \
  sed -i 's|phil_b_trader.py$|phil_b_trader.py --live|' /etc/systemd/system/phil-b-trader.service && \
  systemctl daemon-reload && systemctl cat phil-b-trader.service | grep ExecStart"
```

## 4. Kill switches
- `touch /opt/trader/state/phil_b.HALT` — blocks all new entries (open positions keep exchange SL/TP).
- `systemctl stop phil-b-trader.timer` — stops the loop entirely.
- Automatic: cum <= -10R halt, 4 stop-outs in 24h -> 24h pause.
- Positions carry exchange-native SL/TP, so they are protected even if the service is down.

## 5. Watch items in first week
- `slip_pct` per entry in state/ledger (backtest assumed 20bp; kill if consistently > 0.15R).
- Shadow ledger: signals blocked by the 2-position cap — measures the cost of the cap.
- Confirm SL/TP orders exist on exchange: `fetch_open_orders(sym, params={'stop': True})`
  (Binance CONDITIONAL orders are invisible to the default call — 2026-06-01 lesson).
- `phil-b-trader` is NOT covered by the existing `trader-watchdog` (it matches `trader-*`);
  add it there or rely on the daily ops-loop check.

## 6. Review gates (frozen)
- n=20: kill if PF < 1.0 or cumulative < -10R.
- n=50: promote size only if PF >= 2.0, plus Sharpe-additive pass vs TG-1 + TG-2 book.
