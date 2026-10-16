#!/usr/bin/env bash
# Turn the hub Pi into its own Wi-Fi access point so the event needs no building Wi-Fi.
# Phones join this network, then scan the QR code on the projector.
#   sudo ./deploy/setup-hotspot.sh "Pack63-Derby" "gofast63"
set -euo pipefail
SSID="${1:-GravityGate}"
PASS="${2:?password (8+ characters)}"
nmcli connection delete gravitygate-ap >/dev/null 2>&1 || true
nmcli connection add type wifi ifname wlan0 con-name gravitygate-ap autoconnect yes ssid "$SSID" \
  802-11-wireless.mode ap 802-11-wireless.band bg ipv4.method shared ipv4.addresses 10.42.0.1/24 \
  wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PASS"
nmcli connection up gravitygate-ap
echo "Hotspot '$SSID' is up. Phones will reach the hub at http://10.42.0.1:8000/s"
echo "Tip: in Admin -> Settings set Wi-Fi name/password (for the join QR) and public URL http://10.42.0.1:8000"
