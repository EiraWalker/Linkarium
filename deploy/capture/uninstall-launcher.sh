#!/usr/bin/env bash
set -euo pipefail
if [ "$(id -u)" != 0 ]; then echo 'Run as root.' >&2; exit 1; fi
systemctl disable --now bookmark-capture.service || true
# Remove only fixed files created by install-launcher; preserve configuration and all data.
rm -f -- /etc/systemd/system/bookmark-capture.service /opt/bookmark-capture/launcher.py
systemctl daemon-reload
printf '%s\n' 'Launcher service removed; configuration, rootfs and capture data preserved.'
