#!/usr/bin/env bash
# Install Gravity Gate on Raspberry Pi OS (Bookworm or newer).
#   ./deploy/install.sh hub                          # top-of-track / single Pi
#   ./deploy/install.sh node ws://derby-top.local:8000 /dev/ttyUSB0 microwizard pi
set -euo pipefail
ROLE="${1:-hub}"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="$(id -un)"

sudo apt-get update
sudo apt-get install -y python3-venv python3-numpy python3-pil
if [ "$ROLE" = "node" ] && [ "${5:-none}" = "pi" ]; then
  sudo apt-get install -y python3-picamera2
fi
# Serial timers need the dialout group.
sudo usermod -aG dialout "$USER_NAME"

python3 -m venv --system-site-packages "$DIR/.venv"   # system site = apt's picamera2/numpy
"$DIR/.venv/bin/pip" install --upgrade pip
"$DIR/.venv/bin/pip" install -e "$DIR[camera]"

if [ "$ROLE" = "hub" ]; then
  EXEC="$DIR/.venv/bin/gravitygate serve --port 8000 --data $DIR/data"
else
  HUB="${2:?hub url, e.g. ws://derby-top.local:8000}"
  EXEC="$DIR/.venv/bin/gravitygate node --hub $HUB --serial ${3:-/dev/ttyUSB0} --profile ${4:-generic} --camera ${5:-none}"
fi

sudo tee /etc/systemd/system/gravitygate-$ROLE.service >/dev/null <<UNIT
[Unit]
Description=Gravity Gate ($ROLE)
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$DIR
ExecStart=$EXEC
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now gravitygate-$ROLE
echo "Gravity Gate $ROLE installed. Logs: journalctl -u gravitygate-$ROLE -f"
[ "$ROLE" = "hub" ] && echo "Open http://$(hostname).local:8000/admin"
