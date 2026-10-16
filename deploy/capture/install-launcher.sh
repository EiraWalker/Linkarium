#!/usr/bin/env bash
set -euo pipefail
if [ "$(id -u)" != 0 ]; then echo 'Run as root for the dedicated launcher only.' >&2; exit 1; fi
capture_source="$(cd "$(dirname "$0")" && pwd)"
for capture_command in python3 ip nft systemd-run systemctl; do
  command -v "$capture_command" >/dev/null || { echo "Missing $capture_command" >&2; exit 1; }
done
capture_uid="${BOOKMARK_CAPTURE_UID:-10001}"
capture_gid="${BOOKMARK_CAPTURE_GID:-10001}"
if ! getent group "$capture_gid" >/dev/null; then groupadd -g "$capture_gid" bookmark-capture-app; fi
install -d -m 0750 /opt/bookmark-capture /etc/bookmark-capture /run/bookmark-capture
chgrp "$capture_gid" /run/bookmark-capture
install -d -m 0770 -g "$capture_gid" /var/lib/bookmark-capture/spool
install -m 0755 "$capture_source/launcher.py" /opt/bookmark-capture/launcher.py
if [ ! -f /etc/bookmark-capture/launcher.json ]; then
  python3 - "$capture_source/launcher.example.json" "$capture_uid" "$capture_gid" <<'PY'
import json,sys
with open(sys.argv[1]) as source: configuration=json.load(source)
configuration.update(app_uid=int(sys.argv[2]),app_gid=int(sys.argv[3]))
with open('/etc/bookmark-capture/launcher.json','x') as output: json.dump(configuration,output,indent=2)
PY
  chmod 0640 /etc/bookmark-capture/launcher.json
fi
install -m 0644 "$capture_source/bookmark-capture.service" /etc/systemd/system/bookmark-capture.service
systemctl daemon-reload
python3 "$capture_source/preflight.py" || true
printf '%s\n' 'Launcher installed, remains unverified and is not enabled or started.' \
  'Provision isolated rootfs, configure deployment deny addresses, and run full acceptance before activation.'
