#!/bin/bash
# ─────────────────────────────────────────────────────
# MFI Coin Hunter — Server Setup Script
# Run this ONCE on a fresh Ubuntu 22.04 server
# Usage: bash deploy.sh
# ─────────────────────────────────────────────────────

set -e
echo ""
echo "══════════════════════════════════════════════"
echo "  MFI Coin Hunter — Server Deploy"
echo "══════════════════════════════════════════════"

# 1. Update system
echo "[1/6] Updating system packages..."
apt-get update -qq && apt-get upgrade -y -qq

# 2. Install Python
echo "[2/6] Installing Python 3 + pip..."
apt-get install -y -qq python3 python3-pip python3-venv screen

# 3. Create trader directory and virtual environment
echo "[3/6] Setting up Python environment..."
mkdir -p /root/trader/logs
cd /root/trader
python3 -m venv venv
source venv/bin/activate
pip install -q --upgrade pip
pip install -q ccxt pandas requests flask numpy

# 4. Install systemd service
echo "[4/6] Installing systemd service..."
cat > /etc/systemd/system/live_trader.service << 'EOF'
[Unit]
Description=MFI Coin Hunter — Live Trader
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/trader
ExecStart=/root/trader/venv/bin/python3 /root/trader/live_trader.py
Restart=always
RestartSec=10
StandardOutput=append:/root/trader/logs/live_trader.log
StandardError=append:/root/trader/logs/live_trader.log
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable live_trader

# 5. Set up firewall (block dashboard from public — SSH tunnel only)
echo "[5/6] Configuring firewall..."
ufw allow ssh
ufw allow 22
ufw --force enable

# 6. Done
echo "[6/6] Done!"
echo ""
echo "══════════════════════════════════════════════"
echo "  Setup complete. Next steps:"
echo ""
echo "  Start trader:   systemctl start live_trader"
echo "  Check status:   systemctl status live_trader"
echo "  View logs:      tail -f /root/trader/logs/live_trader.log"
echo ""
echo "  View dashboard from your Mac:"
echo "  ssh -L 8083:localhost:8083 root@SERVER_IP -N"
echo "  Then open: http://localhost:8083"
echo "══════════════════════════════════════════════"
