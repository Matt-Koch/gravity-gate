#!/usr/bin/env bash
# Run on the master box right before shutting it down to clone its SD card / disk.
# Clears everything that must be unique per machine; each clone regenerates it on boot.
# (Machine IDs come from each box's own MAC address, so they are unique automatically.)
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
systemctl stop gravitygate-agent || true
rm -f /var/lib/gravitygate/agent.json            # roles: every clone starts on standby
rm -rf /var/lib/gravitygate/logs/* /var/lib/gravitygate/data/*   # no race data from the master
hostnamectl set-hostname gravitygate             # the agent renames each clone to gg-<id>
truncate -s 0 /etc/machine-id; rm -f /var/lib/dbus/machine-id
if systemctl list-unit-files regenerate_ssh_host_keys.service >/dev/null 2>&1; then
  rm -f /etc/ssh/ssh_host_*                      # Raspberry Pi OS regenerates them on first boot
  systemctl enable regenerate_ssh_host_keys
else
  echo "Note: SSH host keys kept (on Debian, run 'sudo dpkg-reconfigure openssh-server' on each clone)."
fi
echo "Ready to image. Power off now:  sudo poweroff"
