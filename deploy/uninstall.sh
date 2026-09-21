#!/usr/bin/env bash
#
# Tear the stack back down. Leaves /opt/stream/unicats.sdp in place unless you
# pass --purge, because that file is the one thing here that is not in the repo.
#
#   sudo ./deploy/uninstall.sh [--purge]
set -euo pipefail

PURGE=0
[[ "${1:-}" == "--purge" ]] && PURGE=1
[[ $EUID -eq 0 ]] || { echo "run as root (sudo)" >&2; exit 1; }

echo "==> Stopping services"
systemctl disable --now stream-admin 2>/dev/null || true
systemctl disable --now nic-rxring 2>/dev/null || true
[[ -d /opt/stream ]] && ( cd /opt/stream && docker compose down ) || true

echo "==> Removing units and tuning"
rm -f /etc/systemd/system/stream-admin.service
rm -rf /etc/systemd/system/stream-admin.service.d
rm -f /etc/systemd/system/nic-rxring.service
rm -f /etc/sysctl.d/99-stream.conf
systemctl daemon-reload
sysctl --system >/dev/null

echo "==> Removing the admin panel"
rm -rf /opt/stream-admin

if [[ $PURGE -eq 1 ]]; then
  echo "==> Purging /opt/stream (including the SDP)"
  rm -rf /opt/stream
else
  echo "==> Leaving /opt/stream in place (pass --purge to delete it)"
fi

echo
echo "Firewall rules were left alone. Review them with: ufw status numbered"
