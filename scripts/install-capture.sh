#!/usr/bin/env bash
set -euo pipefail
# Install only this project's toolchain. The privileged launcher is installed separately
# after preflight; this script never enables forwarding or issues an acceptance attestation.
capture_workspace="$(cd "$(dirname "$0")/.." && pwd)"
capture_python="${CAPTURE_PYTHON:-$capture_workspace/.venv/bin/python}"
capture_tools="$capture_workspace/tools"
mkdir -p "$capture_tools/monolith"
case "$(uname -m)" in
  x86_64) capture_asset=monolith-gnu-linux-x86_64; capture_expected=663ca914b078e91d5a854b4a07e913c613bbbcfe8fb11a24da1a6ab23c9205df ;;
  aarch64) capture_asset=monolith-gnu-linux-aarch64; capture_expected=7f8cac6291b4015b24b964fcdf115a196abecc59e9149f7aa485175943f99997 ;;
  *) echo 'Unsupported CPU for pinned Monolith release' >&2; exit 1 ;;
esac
capture_binary="$capture_tools/monolith/monolith"
if [ ! -f "$capture_binary" ]; then
  curl --fail --location --proto '=https' --tlsv1.2 \
    "https://github.com/Y2Z/monolith/releases/download/v2.10.1/$capture_asset" \
    --output "$capture_binary.pending"
  chmod 0755 "$capture_binary.pending"
  mv "$capture_binary.pending" "$capture_binary"
fi
printf '%s  %s\n' "$capture_expected" "$capture_binary" | sha256sum --check -
"$capture_binary" -V
sha256sum "$capture_binary" > "$capture_tools/monolith/download.sha256"
export PLAYWRIGHT_BROWSERS_PATH="$capture_tools/browsers"
"$capture_python" -m playwright install chromium
printf '%s\n' 'Tools installed. Production capture requires isolated rootfs and successful Linux acceptance.'
