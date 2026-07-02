#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════
#  Physician Assistant Bot — One-time remote HTTPS setup (macOS)
#  Run:  bash setup-remote.sh
# ══════════════════════════════════════════════════════════════════
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

TAILSCALE_BIN="/Applications/Tailscale.app/Contents/MacOS/Tailscale"

if [ ! -x "$TAILSCALE_BIN" ]; then
  echo "[ERROR] Tailscale app binary not found at: $TAILSCALE_BIN"
  exit 1
fi

if ! "$TAILSCALE_BIN" status >/dev/null 2>&1; then
  echo "[ERROR] Tailscale is not connected. Open the Tailscale app and connect first."
  exit 1
fi

PUBLIC_HOSTNAME="$($TAILSCALE_BIN status --json | python3 -c 'import json,sys; data=json.load(sys.stdin); dns_name=(data.get("Self", {}) or {}).get("DNSName", ""); print(dns_name.rstrip(".") if dns_name else "")')"

if [ -z "$PUBLIC_HOSTNAME" ]; then
  echo "[ERROR] Could not determine the Tailscale hostname."
  exit 1
fi

TLS_DIR=".tls"
mkdir -p "$TLS_DIR"

CERT_FILE="$TLS_DIR/${PUBLIC_HOSTNAME}.crt"
KEY_FILE="$TLS_DIR/${PUBLIC_HOSTNAME}.key"

echo "[1/2] Creating Tailscale certificate for $PUBLIC_HOSTNAME"
"$TAILSCALE_BIN" cert --cert-file "$CERT_FILE" --key-file "$KEY_FILE" "$PUBLIC_HOSTNAME"

cat > .env.remote <<EOF
export PAB_PUBLIC_HOSTNAME=$PUBLIC_HOSTNAME
export PAB_SSL_CERT_FILE=$SCRIPT_DIR/$CERT_FILE
export PAB_SSL_KEY_FILE=$SCRIPT_DIR/$KEY_FILE
EOF

chmod 600 .env.remote

echo "[2/2] Remote HTTPS setup complete"
echo "Start the app with: bash start.sh"
echo "Open: https://$PUBLIC_HOSTNAME:5001"