# Capture runner deployment boundary

Native Windows capture is explicitly development-only. Public capture uses the root-owned
Unix-socket launcher, not the Django process. The supplied Linux launcher implementation
has passed limited WSL2 component checks, but **full Linux deployment acceptance has not
passed**. It refuses start unless a root-owned
`/etc/bookmark-capture/VERIFIED.json` records its exact SHA-256 and
`"public_capture_verified": true`. Installation does not create this attestation.

`scripts/install-capture.ps1` / `scripts/install-capture.sh` install pinned tools in this
workspace. `build-rootfs.sh` builds a fresh private Ubuntu toolchain rootfs without copying
the application or keys. `install-launcher.sh` installs the fixed service and group mapping;
it does not activate the service, change forwarding or create an attestation.

The isolated rootfs must contain only Linux toolchain/runtime files, including
`/opt/capture/venv`, `backend/capture`, Chromium at `/opt/capture/browsers`, Monolith at
`/opt/capture/monolith`, fixed DNS resolv.conf, fonts, CA certificates, and writable mount
points `/request.json` and `/output`. It must contain no application data, keys or socket.
Use a minimal Debian Linux container rootfs exported during deployment image construction.
The namespace launcher uses systemd transient services for nonroot execution, mount/PID/user
isolation, cgroup memory/task/runtime budgets and cgroup-wide cancellation. Output writes
use a 384 MiB tmpfs and private `/tmp` is limited to 256 MiB; only verified HTML up to
96 MiB is copied to app-readable spool storage. The output budget covers simultaneous
Monolith temporary and final archives; the process-group memory limit stays 1536 MiB.
The launcher seals its own fixed 96 MiB budget; callers cannot select a larger one. Network namespaces
have nftables default-drop egress, IPv6 blocked, fixed DNS and public TCP 80/443. Host forward
filtering occurs after DNAT and denies private, own-application and reserved ranges.

The spool root is root-owned and group-readable, never application-writable. Only
`incoming/` accepts application-created request directories. The launcher atomically
moves an attempt into root-only `private/`, opens it with `O_DIRECTORY|O_NOFOLLOW`,
and removes application write permissions through that descriptor before reading its
bounded request. Mounts, sealed input and temporary outputs stay under this protected
parent. Verified final bytes move into root-owned, application-readable `results/`.
The worker calls the fixed `release` operation after publication and on errors; a failed
release never invalidates an already published archive. Cancellation and lease recovery
also release staging. An idle sweep runs every minute and removes inactive staging after
one hour. Admission is limited to 32 retained attempts and 1 GiB of retained output,
reserving each new attempt's full HTML and preview budget before execution. Active
attempts and persisted live cgroups are excluded from expiration cleanup.

Before activation on a Linux host, verify sandboxed Chromium launch without `--no-sandbox`,
dual-device fidelity, private IPv4/IPv6, mixed DNS, redirect/iframe/subresource/WebSocket and
DNS-rebinding denial, host/DNAT denial, no database/keys mounts, memory/disk/runtime budgets,
full-cgroup cancellation, permissions and successful normal public capture. Rootfs/cgroup
mount compatibility depends on the target's Linux/systemd configuration. No fallback to
unrestricted capture is allowed. A Linux host with forwarding disabled must configure its
network intentionally; the installer does not alter global firewall policy.

The live WSL2 probe is recorded in
[linux-preflight.json](../../tests/capture/results/linux-preflight.json): isolated network
namespaces, nonroot user namespaces and kernel loopback denial passed. The current kernel
cannot install nftables byte-quota expressions, systemd MemoryMax did not prevent a bounded
160 MiB allocation under a 64 MiB limit, and IPv4 forwarding is disabled. The probe created
and removed only its own temporary namespace/services. No acceptance attestation was issued.

Controlled dynamic fixture visual comparison passed below 0.2% changed pixels on both
desktop and mobile. [Fixture report](../../tests/capture/results/report.json) records the
actual outputs; diagnostic PNGs are not product archives. Default system-DNS capture refused
this workstation's Mihomo fake DNS reserved 198.18/15 addresses as designed.

## Optional development DNS compatibility

`CaptureRequest(development=True, dns_mode="public-doh", ...)` enables a per-capture
loopback-only authenticated HTTP/CONNECT proxy. The default remains `dns_mode="system"`.
Default development capture uses the same proxy with the system resolver: each host/port
is resolved once within a capture and every connection uses the validated literal IP.
Chromium never performs a second target-host resolution. Mixed public/private answers
are refused; public IPv4 and IPv6 system answers remain supported. Test-only allow-host
exceptions are kept inside the explicit fixture request contract.
The compatibility mode queries the fixed `https://1.1.1.1/dns-query` JSON API through HTTPX
with environment proxies disabled and certificate verification enabled. It accepts only
validated public IPv4 answers and connects to those literal addresses; it never falls
back to system DNS or DIRECT connections. DNS queries disclose the requested domains to
the fixed Cloudflare resolver. No system DNS, proxy, firewall or Mihomo configuration is
changed. An unavailable resolver produces a reported failure.

Chromium retains the original website URL, Host, cookies, redirect handling, TLS SNI and
certificate verification. HTTPS passes as an opaque CONNECT tunnel. The explicit proxy
has no DIRECT fallback, and `--proxy-bypass-list=<-loopback>` disables Chromium's implicit
loopback bypass. Private/reserved/own-application addresses, credentials and ports outside
80/443 are refused on actual proxy connections as well as browser request events. The
deployment deny-host list accepts exact names and a leading-dot suffix for the base domain
and all subdomains; IDNA and bracketed IPv6 entries are normalized.

Each proxy is restricted to 32 active handlers, 2,048 TCP attempts, a shared bidirectional
96 MiB default maximum, request headers up to 16 KiB and HTTP response headers up to 64 KiB. The
fixed resolver allows 128 queries, responses up to 64 KiB and at most 32 IPv4 answers per
query. TCP attempts, idle connections, and resolver calls have bounded timeouts. Cancellation
closes tracked sockets and requires server/handler thread exit within a bounded shutdown
period. These controls are additional development checks, **not kernel egress isolation**.
The low-level `capture()` time budget is cooperative across navigation, network, archive
subprocess and stage checks; hostile renderer JavaScript can still block an evaluation
before cleanup. The application's development worker runs the capture CLI in an owned
subprocess and supplies a 120-second parent timeout plus cancellation checks. This protects
its job lifetime without providing kernel network/filesystem/cgroup containment. The
production launcher supplies the process/cgroup runtime cutoff; that deployment boundary
remains unverified on this host.

This mode is rejected for production requests, public deployments and fixture allow-host
exceptions. It cannot be selected through the production launcher request protocol: the
launcher continues to use its fixed, operator-owned resolver and containment configuration.

The [public DoH report](../../tests/capture/results/public-doh/example-report.json) records
real anonymous Example Domain desktop/mobile Monolith HTML outputs. Desktop live/replay
changed pixels were approximately 0.213%, with no broken images or external replay requests.
The mobile archive passed capture's offline replay, but its additional live comparison
failed when a fresh DoH TLS connection timed out; that error is retained in the report.
One simple public page and a controlled dynamic fixture do not verify arbitrary websites,
anti-bot pages, changing content, cross-origin media or every responsive layout. No further
MDN live capture was performed during this bounded validation.

Run the opt-in controlled suite with `RUN_CAPTURE_INTEGRATION=1` and
`python -m unittest discover -s tests/capture -v` (set `PYTHONPATH=backend`).
`tests/capture/verify_public_doh.py` is an explicit live-network evidence script, not a
startup task. Its HTML files are product artifacts; PNGs are comparison diagnostics only.

Sources: [Playwright Chromium sandbox](https://playwright.dev/python/docs/api/class-browsertype),
[Monolith official 2.10.1 binaries](https://github.com/Y2Z/monolith/releases/tag/v2.10.1),
[nftables chains](https://wiki.nftables.org/wiki-nftables/index.php/Configuring_chains),
[Chromium proxy behavior and loopback bypass](https://chromium.googlesource.com/chromium/src/+/HEAD/net/docs/proxy.md),
[Cloudflare DNS JSON API](https://developers.cloudflare.com/1.1.1.1/encryption/dns-over-https/make-api-requests/dns-json/).
