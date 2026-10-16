#!/usr/bin/env python3
"""Nonactivation Linux capability checks; only owned ephemeral namespaces/services."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from uuid import uuid4

MAX_CAPTURE_BYTES = 96 * 1024 * 1024


def execute(*args, timeout=20, input=None):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, input=input)


def main():
    report = {'public_capture_verified': False, 'checks': {}, 'limitations': []}
    checks = report['checks']
    checks['root'] = os.geteuid() == 0
    checks['tools'] = {tool: bool(shutil.which(tool)) for tool in ('ip', 'nft', 'systemd-run', 'systemctl', 'python3')}
    checks['forwarding_enabled'] = Path('/proc/sys/net/ipv4/ip_forward').read_text().strip() == '1'
    report['kernel'] = execute('uname', '-a').stdout.strip()
    if not checks['root'] or not all(checks['tools'].values()):
        report['limitations'].append('Missing root privileges or mandatory Linux tools')
        print(json.dumps(report, indent=2))
        return 1
    identity = uuid4().hex[:12]
    namespace = 'bmc-test-' + identity
    try:
        made = execute('ip', 'netns', 'add', namespace)
        checks['network_namespace'] = made.returncode == 0
        if not checks['network_namespace']:
            report['limitations'].append(made.stderr.strip()[:400])
        else:
            execute('ip', '-n', namespace, 'link', 'set', 'lo', 'up')
            rules = '''table inet capture {
              set denied { type ipv4_addr; flags interval; auto-merge; elements = {
                0.0.0.0/8,10.0.0.0/8,100.64.0.0/10,127.0.0.0/8,169.254.0.0/16,
                172.16.0.0/12,192.0.0.0/24,192.0.2.0/24,192.168.0.0/16,
                198.18.0.0/15,198.51.100.0/24,203.0.113.0/24,224.0.0.0/4,240.0.0.0/4 } }
              chain input { type filter hook input priority 0; policy drop;
                quota over CAPTURE_BYTE_LIMIT bytes drop; ct state established,related accept; }
              chain output { type filter hook output priority 0; policy drop;
                quota over CAPTURE_BYTE_LIMIT bytes drop; meta nfproto ipv6 counter drop;
                ip daddr @denied counter drop; ip daddr {1.1.1.1,9.9.9.9} udp dport 53 accept;
                ip daddr {1.1.1.1,9.9.9.9} tcp dport 53 accept; tcp dport {80,443} counter accept; }
            }'''.replace('CAPTURE_BYTE_LIMIT', str(MAX_CAPTURE_BYTES))
            nft = execute('ip', 'netns', 'exec', namespace, 'nft', '-f', '-', input=rules)
            checks['nftables_filter_and_quota'] = nft.returncode == 0
            if nft.returncode:
                report['limitations'].append(nft.stderr.strip()[:1000])
                basic = execute('ip', 'netns', 'exec', namespace, 'nft', '-f', '-',
                    input=rules.replace(f'quota over {MAX_CAPTURE_BYTES} bytes drop;', ''))
                checks['nftables_filter_without_quota'] = basic.returncode == 0
                # This fallback is a capability probe only; the production launcher
                # never removes its quota rule or bypasses the acceptance gate.
            if nft.returncode == 0 or checks.get('nftables_filter_without_quota'):
                # An actual destination-IP denial. No existing host table is touched.
                blocked = execute('ip', 'netns', 'exec', namespace, 'python3', '-c',
                    "import socket;s=socket.socket();s.settimeout(1);"
                    "\ntry:s.connect(('127.0.0.1',80));print('UNEXPECTED_CONNECTION')"
                    "\nexcept OSError:print('blocked')")
                listing = execute('ip', 'netns', 'exec', namespace, 'nft', '-j', 'list', 'table', 'inet', 'capture')
                details = json.loads(listing.stdout)
                checks['loopback_denied_by_packet_filter'] = 'blocked' in blocked.stdout and any(
                    expression.get('counter', {}).get('packets', 0) > 0
                    for item in details['nftables'] for expression in item.get('rule', {}).get('expr', []))
    finally:
        execute('ip', 'netns', 'delete', namespace)
    unit = 'bmc-probe-' + identity
    sandbox = execute('systemd-run', '--quiet', '--wait', '--pipe', '--collect', '--unit=' + unit,
        '-p', 'PrivateUsers=yes', '-p', 'PrivateDevices=yes', '-p', 'NoNewPrivileges=yes',
        '-p', 'User=nobody', '--', '/usr/bin/python3', '-c',
        "import os;print(os.getuid());print(open('/proc/self/status').read())")
    checks['nonroot_user_namespace_no_privileges'] = sandbox.returncode == 0 and '65534' in sandbox.stdout and 'NoNewPrivs:\t1' in sandbox.stdout
    memory = execute('systemd-run', '--quiet', '--wait', '--pipe', '--collect', '--unit=' + unit + '-memory',
        '-p', 'MemoryMax=64M', '-p', 'RuntimeMaxSec=10', '--', '/usr/bin/python3', '-c',
        "allocation=bytearray(160*1024*1024);print('MEMORY_BUDGET_NOT_ENFORCED')", timeout=20)
    checks['memory_budget_enforced'] = memory.returncode != 0 and 'MEMORY_BUDGET_NOT_ENFORCED' not in memory.stdout
    if not checks['forwarding_enabled']:
        report['limitations'].append('Host IPv4 forwarding is disabled and was not changed')
    report['limitations'].append('Rootfs Chromium sandbox, DNS rebinding, post-DNAT filtering, disk quotas and full-cgroup cancellation still require end-to-end deployment acceptance')
    print(json.dumps(report, indent=2))
    return 0 if all(value is True for key, value in checks.items() if key != 'tools') else 1


if __name__ == '__main__':
    raise SystemExit(main())
