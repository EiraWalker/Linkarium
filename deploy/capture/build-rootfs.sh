#!/usr/bin/env bash
set -euo pipefail
# Builds a NEW isolated Ubuntu rootfs; never copies the application's database or keys.
if [ "$(id -u)" != 0 ]; then echo 'Root is required for debootstrap and the private rootfs.' >&2; exit 1; fi
command -v debootstrap >/dev/null || { echo 'Install debootstrap first.' >&2; exit 1; }
capture_source="$(cd "$(dirname "$0")/../.." && pwd)"
capture_rootfs="${CAPTURE_ROOTFS:-/opt/bookmark-capture/rootfs}"
capture_playwright_version="${CAPTURE_PLAYWRIGHT_VERSION:-1.63.0}"
if [ -e "$capture_rootfs" ]; then
  echo 'Rootfs target already exists. Choose a fresh explicit path; existing state is preserved.' >&2; exit 1
fi
case "$(uname -m)" in
 x86_64) capture_arch=amd64; capture_mirror=https://archive.ubuntu.com/ubuntu; capture_asset=monolith-gnu-linux-x86_64; capture_sha=663ca914b078e91d5a854b4a07e913c613bbbcfe8fb11a24da1a6ab23c9205df ;;
 aarch64) capture_arch=arm64; capture_mirror=https://ports.ubuntu.com/ubuntu-ports; capture_asset=monolith-gnu-linux-aarch64; capture_sha=7f8cac6291b4015b24b964fcdf115a196abecc59e9149f7aa485175943f99997 ;;
 *) echo 'Unsupported capture rootfs architecture' >&2; exit 1 ;;
esac
mkdir -p "$(dirname "$capture_rootfs")"
debootstrap --arch="$capture_arch" --variant=minbase \
 --include=python3,python3-venv,ca-certificates,curl,fonts-liberation \
 noble "$capture_rootfs" "$capture_mirror"
mkdir -p "$capture_rootfs/opt/capture/backend" "$capture_rootfs/output"
cp -a "$capture_source/backend/capture" "$capture_rootfs/opt/capture/backend/capture"
find "$capture_rootfs/opt/capture/backend/capture" -type d -name __pycache__ -prune -exec rm -r -- {} +
chroot "$capture_rootfs" python3 -m venv /opt/capture/venv
chroot "$capture_rootfs" /opt/capture/venv/bin/python -m pip install \
 "playwright==$capture_playwright_version" 'html5lib==1.1' 'tinycss2==1.4.0'
chroot "$capture_rootfs" /usr/bin/env PLAYWRIGHT_BROWSERS_PATH=/opt/capture/browsers \
 /opt/capture/venv/bin/python -m playwright install --with-deps chromium
curl --fail --location --proto '=https' --tlsv1.2 \
 "https://github.com/Y2Z/monolith/releases/download/v2.10.1/$capture_asset" \
 --output "$capture_rootfs/opt/capture/monolith"
printf '%s  %s\n' "$capture_sha" "$capture_rootfs/opt/capture/monolith" | sha256sum --check -
chmod 0755 "$capture_rootfs/opt/capture/monolith"
printf 'nameserver 1.1.1.1\nnameserver 9.9.9.9\n' > "$capture_rootfs/etc/resolv.conf"
touch "$capture_rootfs/request.json"
chmod -R a+rX "$capture_rootfs/opt/capture"
printf '%s\n' 'Fresh isolated toolchain rootfs built. No launcher verification/activation was performed.' \
 'The isolated capture-only Python uses Ubuntu runtime; application Django Python remains separate.'
