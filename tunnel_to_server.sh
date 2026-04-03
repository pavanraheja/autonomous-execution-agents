#!/bin/bash
# Agent Trader — SSH Tunnel to Contabo Production Server
# Forwards all 8 trader dashboard ports to localhost
# Usage: bash tunnel_to_server.sh
# Then open: http://localhost:8081 through http://localhost:8088

SERVER="217.15.164.68"
PASS="ppC6Zyxmjqx0vWX971F"

echo "[$(date)] Opening SSH tunnel to $SERVER..."
echo "Dashboards will be at:"
echo "  8081 → Filtered MFI Paper   http://localhost:8081"
echo "  8082 → MFI Cascade SHORT    http://localhost:8082"
echo "  8083 → MFI Live Trader      http://localhost:8083"
echo "  8084 → Keltner Breakout     http://localhost:8084"
echo "  8085 → HTF MFI Scalper      http://localhost:8085"
echo "  8086 → Alligator Trader     http://localhost:8086"
echo "  8087 → MFI Monitor          http://localhost:8087"
echo "  8088 → Trading HQ           http://localhost:8088"
echo ""
echo "Press Ctrl+C to close tunnel."
echo ""

sshpass -p "$PASS" ssh -o StrictHostKeyChecking=no \
  -L 8081:localhost:8081 \
  -L 8082:localhost:8082 \
  -L 8083:localhost:8083 \
  -L 8084:localhost:8084 \
  -L 8085:localhost:8085 \
  -L 8086:localhost:8086 \
  -L 8087:localhost:8087 \
  -L 8088:localhost:8088 \
  -N root@$SERVER
