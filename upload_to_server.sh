#!/bin/bash
# ─────────────────────────────────────────────────────
# Upload trading files to your server
# Usage: bash upload_to_server.sh YOUR_SERVER_IP
# Example: bash upload_to_server.sh 157.245.80.123
# ─────────────────────────────────────────────────────

SERVER_IP=$1

if [ -z "$SERVER_IP" ]; then
    echo "Usage: bash upload_to_server.sh YOUR_SERVER_IP"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo ""
echo "Uploading to $SERVER_IP..."

# Create remote directory
ssh root@$SERVER_IP "mkdir -p /root/trader/logs"

# Upload files
scp "$SCRIPT_DIR/live_trader.py"   root@$SERVER_IP:/root/trader/
scp "$SCRIPT_DIR/config.py"        root@$SERVER_IP:/root/trader/
scp "$SCRIPT_DIR/requirements.txt" root@$SERVER_IP:/root/trader/
scp "$SCRIPT_DIR/deploy.sh"        root@$SERVER_IP:/root/trader/

echo ""
echo "✅ Files uploaded. Now SSH in and run:"
echo ""
echo "   ssh root@$SERVER_IP"
echo "   bash /root/trader/deploy.sh"
echo ""
