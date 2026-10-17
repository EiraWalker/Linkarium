"""Server-owned access addresses, separate from the authentication origin."""
import json
import ipaddress
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class NetworkAdapter:
    name: str
    index: int
    ipv4: str


def discover_adapters():
    """Read the operating system's physical/virtual adapter inventory."""
    if os.name != "nt":
        raise RuntimeError("Automatic network adapter selection currently requires Windows; use the existing explicit-host deployment on this platform.")
    script = r"""$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new()
$items = @(Get-NetAdapter | ForEach-Object {
    $a = $_
    $ips = @(Get-NetIPAddress -InterfaceIndex $a.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.AddressState -eq 'Preferred' } | ForEach-Object { $_.IPAddress })
    $kind = if ($a.NdisPhysicalMedium -in 1,9) { 'wifi' } elseif ($a.NdisPhysicalMedium -eq 14 -or [string]$a.MediaType -eq '802.3') { 'ethernet' } else { 'other' }
    @{ name=$a.Name; index=$a.ifIndex; kind=$kind; hardware=[bool]$a.HardwareInterface; up=($a.Status -eq 'Up'); ipv4=$ips }
})
ConvertTo-Json -InputObject $items -Depth 3 -Compress
"""
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, encoding="utf-8-sig", check=True, timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW)
    return json.loads(result.stdout)


def select_adapter(name=None):
    inventory = discover_adapters()
    if name is not None:
        inventory = [item for item in inventory if name in (item["name"], str(item["index"]))]
        if not inventory:
            raise RuntimeError(f"未找到网络适配器：{name}")
    candidates = []
    for item in inventory:
        if not item["up"]:
            continue
        addresses = []
        for value in item["ipv4"]:
            address = ipaddress.IPv4Address(value)
            if not (address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast):
                addresses.append(str(address))
        if addresses and (name is not None or (item["hardware"] and item["kind"] in {"wifi", "ethernet"})):
            candidates.append((item, sorted(set(addresses))))
    if name is None:
        wireless = [candidate for candidate in candidates if candidate[0]["kind"] == "wifi"]
        candidates = wireless or candidates
    if not candidates:
        raise RuntimeError(f"网络适配器不可用或没有有效 IPv4：{name or '物理 Wi-Fi / 以太网'}")
    if len(candidates) != 1 or len(candidates[0][1]) != 1:
        names = ", ".join(f"{item['name']} ({', '.join(addresses)})" for item, addresses in candidates)
        raise RuntimeError(f"存在多个可用网络地址，请明确指定网络适配器并为其配置唯一 IPv4：{names}")
    item, addresses = candidates[0]
    return NetworkAdapter(item["name"], int(item["index"]), addresses[0])


def connection_status():
    """Expose only the public connection fields supplied by the startup owner."""
    encoded = os.environ.get("BOOKMARK_CONNECTION")
    if not encoded:
        return None  # Existing direct/proxy deployments retain their configuration.
    value = json.loads(encoded)
    return {key: value.get(key) for key in ("mode", "adapter", "ipv4", "access_url", "certificate_url")} | {
        "device_enrollment": value["mode"] in {"lan_https", "https"}}


def web_addresses(default_host):
    return json.loads(os.environ.get("BOOKMARK_WEB_ADDRESSES", "null")) or [default_host]


def configure_http(adapter, port):
    connection = {"mode": "http", "adapter": adapter.name, "ipv4": adapter.ipv4,
        "access_url": f"http://{adapter.ipv4}:{port}/", "certificate_url": None}
    os.environ["BOOKMARK_CONNECTION"] = json.dumps(connection)
    os.environ["BOOKMARK_WEB_ADDRESSES"] = json.dumps(["127.0.0.1", adapter.ipv4])
    hosts = os.environ.get("BOOKMARK_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]").split(",")
    os.environ["BOOKMARK_ALLOWED_HOSTS"] = ",".join(dict.fromkeys([*hosts, adapter.ipv4]))
    return connection


def configure_external_https(adapter, origin, host):
    """Keep existing externally managed HTTPS proxies on their loopback upstream."""
    from urllib.parse import urlsplit
    os.environ["BOOKMARK_WEB_ADDRESSES"] = json.dumps([host])
    os.environ["BOOKMARK_CONNECTION"] = json.dumps({"mode": "https", "adapter": adapter.name,
        "ipv4": adapter.ipv4, "access_url": origin.rstrip("/") + "/", "certificate_url": None})
    hosts = os.environ.get("BOOKMARK_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]").split(",")
    os.environ["BOOKMARK_ALLOWED_HOSTS"] = ",".join(dict.fromkeys([*hosts, urlsplit(origin).hostname]))


def record_connection(data_root):
    """Publish the active process's public ports for the administrator helper."""
    connection = connection_status()
    if connection is None:
        return
    path = Path(data_root) / "runtime" / "network-access.json"
    pending = path.with_suffix(".json.tmp")
    pending.write_text(json.dumps({**connection, "process_id": os.getpid(), "recorded_at": time.time()}, ensure_ascii=False, indent=2), encoding="utf-8")
    pending.replace(path)


def clear_connection(data_root):
    path = Path(data_root) / "runtime" / "network-access.json"
    try:
        if json.loads(path.read_text(encoding="utf-8"))["process_id"] == os.getpid():
            path.unlink()
    except FileNotFoundError:
        pass
