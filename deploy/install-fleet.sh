#!/usr/bin/env bash
# Make this machine a Gravity Gate fleet box: the same install on every Pi / Protectli.
# After boot it shows its ID + IP + a role picker on the HDMI console (tty1).
#
#   sudo ./deploy/install-fleet.sh [fleet-key]
#
# Optional fleet-key: any passphrase; use the SAME one on every box. Machines with a
# different key ignore each other (keeps a stray laptop from posing as the brain).
# Then either repeat on each box, or run deploy/prepare-image.sh and clone the disk.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
KEY="${1:-}"
DIR="$(cd "$(dirname "$0")/.." && pwd)"

apt-get update
apt-get install -y python3-venv python3-numpy python3-pil cage seatd iproute2
apt-get install -y chromium-browser 2>/dev/null || apt-get install -y chromium
if grep -qs "Raspberry Pi" /proc/device-tree/model; then
  apt-get install -y python3-picamera2
fi

# Unprivileged user for the race server, timer node and kiosk browser.
id gravitygate >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin gravitygate
usermod -aG video,render,input,dialout gravitygate
systemctl enable --now seatd

python3 -m venv --system-site-packages "$DIR/.venv"   # system site = apt's picamera2/numpy
"$DIR/.venv/bin/pip" install --upgrade pip
"$DIR/.venv/bin/pip" install -e "$DIR[camera]"
ln -sf "$DIR/.venv/bin/gravitygate" /usr/local/bin/gravitygate
install -m 755 "$DIR/deploy/gravitygate-kiosk" /usr/local/bin/gravitygate-kiosk

install -d -o gravitygate /var/lib/gravitygate /var/lib/gravitygate/data /var/lib/gravitygate/logs
install -d /etc/gravitygate
KEY="$KEY" python3 -c 'import json, os; print(json.dumps({"state_dir": "/var/lib/gravitygate", "fleet_key": os.environ["KEY"], "run_as": "gravitygate", "set_hostname": True}))' > /etc/gravitygate/agent.json
chmod 600 /etc/gravitygate/agent.json

# The agent owns tty1 (the HDMI console) instead of a login prompt.
systemctl disable getty@tty1.service 2>/dev/null || true
cat > /etc/systemd/system/gravitygate-agent.service <<UNIT
[Unit]
Description=Gravity Gate fleet agent (role picker on HDMI + role supervisor)
After=network-online.target seatd.service
Wants=network-online.target
Conflicts=getty@tty1.service

[Service]
ExecStart=/usr/local/bin/gravitygate agent
StandardInput=tty
StandardOutput=tty
TTYPath=/dev/tty1
TTYReset=yes
TTYVHangup=yes
Environment=TERM=linux
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now gravitygate-agent
echo "Done. The HDMI screen now shows this box's ID and the role picker."
echo "Logs: journalctl -u gravitygate-agent -f   and   /var/lib/gravitygate/logs/"
