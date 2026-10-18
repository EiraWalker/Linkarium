"""Single parent for the Web, worker and state-owned maintenance operations."""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from runtime.instance_lock import InstanceLock


class Supervisor:
    def __init__(self, host="127.0.0.1", port=8000, https=None):
        self.host, self.port = host, port
        self.https = https
        self.children = {}
        self.running = True

    def start_services(self):
        from runtime.network_access import web_addresses
        manage = str(Path(__file__).with_name("manage.py"))
        web = self.children.get("web")
        if web is None or web.poll() is not None:
            # Fail before spawning anything if this endpoint belongs to another app.
            for address in web_addresses(self.host):
                family = socket.AF_INET6 if ":" in address else socket.AF_INET
                with socket.socket(family, socket.SOCK_STREAM) as probe:
                    if os.name == "nt":
                        probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                    else:
                        # A stopped listener can leave accepted connections in TIME_WAIT.
                        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    try:
                        probe.bind((address, self.port))
                    except OSError:
                        raise RuntimeError("The configured Web address/port is already in use") from None
        commands = {"web": [sys.executable, manage, "run_web", "--host", self.host, "--port", str(self.port)],
                    "worker": [sys.executable, manage, "run_worker"]}
        if os.environ.get("BOOKMARK_AUTH_DEBUG") == "1":
            for command in commands.values():
                command.append("--debug")
        if self.https:
            commands["proxy"] = self.https.command()
        for name, command in commands.items():
            current = self.children.get(name)
            if current and current.poll() is None:
                continue
            options = {"start_new_session": True} if os.name != "nt" else {
                "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
            if name == "proxy":
                options["env"] = self.https.process_environment()
            self.children[name] = subprocess.Popen(command, stdin=subprocess.DEVNULL, **options)

    @staticmethod
    def _stop_child(child):
        if child.poll() is not None:
            return
        if os.name == "nt":
            # Only this parent's known child PID, never an arbitrary image name.
            subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW, check=False)
        else:
            os.killpg(child.pid, signal.SIGTERM)
        try:
            child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                child.kill()
            else:
                os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=10)

    def stop_services(self):
        from django.conf import settings
        from config.paths import DATA_ROOT, generation_root
        from runtime.models import RuntimeState, MaintenanceOperation
        from runtime.maintenance import MaintenanceError
        state = RuntimeState.objects.using("default").get(pk=1)
        generations = {state.active_generation}
        operation = MaintenanceOperation.objects.using("default").filter(pk=state.active_maintenance_id).first()
        if operation and operation.old_generation:
            generations.add(operation.old_generation)
        attempts = []
        for generation in generations:
            database = generation_root(generation) / "library.sqlite3"
            if not database.exists():
                continue
            connection = sqlite3.connect(database)
            try:
                attempts += list(connection.execute("""SELECT a.id,a.generation,a.library_epoch FROM library_jobattempt a
                    JOIN library_job j ON j.id=a.job_id WHERE a.state='running' AND j.kind IN ('capture','metadata')"""))
            finally:
                connection.close()
        for child in self.children.values():
            self._stop_child(child)
        self.children.clear()
        # Host-isolated browser processes are not descendants of the app container.
        # A request folder is created before a launcher start. Never mistake missing
        # socket/exit acknowledgement for successful cancellation of an issued start.
        if settings.BOOKMARK_CAPTURE_MODE == "isolated":
            from capture import LauncherClient
            spool = Path(os.environ.get("BOOKMARK_CAPTURE_SPOOL", "/capture-spool"))
            client = LauncherClient(settings.BOOKMARK_CAPTURE_SOCKET, spool)
            for attempt_id, generation, epoch in attempts:
                attempt_uuid = str(__import__("uuid").UUID(attempt_id))
                if not (spool / attempt_uuid / "request.json").exists():
                    continue
                try:
                    response = client.cancel(attempt_uuid, generation, epoch)
                    if not response.get("exited") and not response.get("state") in {"exited", "cancelled", "not_found"}:
                        status = client.status(attempt_uuid, generation, epoch)
                        if not status.get("exited") and status.get("state") not in {"exited", "cancelled", "not_found"}:
                            raise MaintenanceError("CAPTURE_STOP_UNCONFIRMED")
                except Exception as error:
                    raise MaintenanceError("CAPTURE_STOP_UNCONFIRMED") from error

    def ready_services(self, timeout=30):
        check_host = "127.0.0.1" if self.host in {"0.0.0.0", "::"} else self.host
        endpoint = f"http://{check_host}:{self.port}/health/ready"
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(child.poll() is not None for child in self.children.values()):
                return False
            try:
                # Do not inherit HTTP_PROXY and do not follow a proxy into the internet.
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(endpoint, timeout=2) as response:
                    body = json.loads(response.read(4096))
                    if response.status == 200 and isinstance(body, dict) and body.get("ready") is True and (
                            self.children and all(child.poll() is None for child in self.children.values())):
                        if self.https is None or self.https.ready():
                            return True
            except (OSError, ValueError):
                pass
            time.sleep(0.5)
        return False

    def recover_interrupted(self):
        from django.db import transaction
        from django.db.models import Q
        from runtime.models import RuntimeState, MaintenanceOperation
        from runtime.maintenance import _validate_database
        from config.paths import generation_root
        for operation in MaintenanceOperation.objects.using("default").filter(
                Q(state="running") | Q(state="queued", kind__in=["clear", "reset"])):
            if operation.kind in {"clear", "reset"}:
                from runtime.data_reset import process_data_operation
                if operation.state == "queued":
                    from runtime.maintenance import process_operation
                    process_operation(operation.id, stop_services=self.stop_services,
                        start_services=self.start_services, ready_services=self.ready_services)
                else:
                    process_data_operation(operation, stop_services=self.stop_services,
                        start_services=self.start_services, ready_services=self.ready_services)
                continue
            state = RuntimeState.objects.using("default").get(pk=1)
            if operation.kind == "restore" and state.active_generation == operation.target_generation:
                try:
                    _validate_database(generation_root(state.active_generation) / "library.sqlite3")
                    self.start_services()
                    if not self.ready_services():
                        raise RuntimeError("Candidate did not become ready")
                    operation.state, operation.stage = "succeeded", "completed_after_restart"
                    operation.result = {**operation.result, "requires_login": True}
                except Exception:
                    self.stop_services()
                    with transaction.atomic(using="default"):
                        state.active_generation, state.library_epoch = operation.old_generation, state.library_epoch + 1
                        state.save(using="default")
                    self.start_services()
                    if not self.ready_services():
                        operation.state, operation.stage, operation.error_code = "failed", "rollback_failed", "RESTORE_ROLLBACK_FAILED"
                        operation.active_key, operation.claim_token = None, None
                        operation.save(using="default")
                        continue
                    operation.state, operation.stage, operation.error_code = "failed", "failed_rolled_back", "INTERRUPTED"
            else:
                operation.state, operation.stage, operation.error_code = "failed", "interrupted_before_switch", "INTERRUPTED"
            operation.active_key, operation.claim_token = None, None
            operation.save(using="default")
            RuntimeState.objects.using("default").filter(pk=1).update(maintenance_mode=False, active_maintenance_id=None)

    def run(self):
        from django.conf import settings
        from django.contrib.auth import get_user_model
        from django.core.management import call_command
        from django.core.management.base import CommandError
        from runtime.models import RuntimeState, MaintenanceOperation
        from runtime.maintenance import process_operation
        from runtime.performance import span, trace
        from config.paths import DATA_ROOT
        from runtime.network_access import record_connection, clear_connection
        from runtime.auth_grants import invalidate_initialization_grants, passkey_exists
        try:
            # Recovery can also start children; every startup path belongs to the
            # same cleanup boundary as the steady-state loop.
            with trace("service.startup"):
                invalidate_initialization_grants()
                with span("service.recover"):
                    self.recover_interrupted()
                if RuntimeState.objects.using("default").get(pk=1).maintenance_mode:
                    raise RuntimeError("Maintenance recovery requires operator repair; preserved library generations")
                with span("service.spawn"):
                    self.start_services()
                with span("service.readiness_wait"):
                    if not self.ready_services():
                        if self.https:
                            raise RuntimeError(f"HTTPS 入口未就绪：{self.https.origin}。请检查域名 DNS／代理是否能到达 {self.https.adapter.ipv4}、所选端口及 CA 证书；未降级 HTTP。")
                        raise RuntimeError("Application did not become ready")
            record_connection(DATA_ROOT)
            origin = settings.BOOKMARK_PASSKEY_ORIGIN or f"http://localhost:{self.port}"
            print(f"网站收藏已启动：{origin}/", flush=True)
            from runtime.network_access import connection_status
            connection = connection_status()
            if connection:
                print(f"访问地址：{connection['access_url']}（{connection['adapter']}）", flush=True)
            if self.https:
                print(f"CA 证书：{self.https.ca_path.resolve()}", flush=True)
                print(f"证书安装页：{self.https.certificate_url}", flush=True)
            from runtime.authentication import normalize_origin
            if not passkey_exists(normalize_origin(origin)[1]):
                try:
                    call_command("passkey_initialize", origin=origin)
                    sys.stdout.flush()
                except CommandError:
                    # A valid initialization may finish between readiness and
                    # announcement. It must not stop the newly initialized app.
                    if not passkey_exists(normalize_origin(origin)[1]):
                        raise
            while self.running:
                operation = MaintenanceOperation.objects.using("default").filter(
                    state__in=["queued", "validating"]).order_by("created_at").first()
                if operation:
                    process_operation(operation.id, stop_services=self.stop_services,
                                      start_services=self.start_services, ready_services=self.ready_services)
                if RuntimeState.objects.using("default").get(pk=1).maintenance_mode:
                    raise RuntimeError("Maintenance rollback failed; manual repair is required")
                self.start_services()
                time.sleep(1)
        finally:
            with trace("service.shutdown"):
                try:
                    self.stop_services()
                finally:
                    # A broken runtime/library database can make stop_services fail
                    # while discovering launcher attempts. Still stop owned children.
                    for child in list(self.children.values()):
                        self._stop_child(child)
                    self.children.clear()
                    clear_connection(DATA_ROOT)


def available_port(host, starting_port, attempts=20, addresses=None):
    """Choose a free default port without connecting to or stopping another app."""
    if not 1 <= starting_port <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    for port in range(starting_port, min(65536, starting_port + attempts)):
        try:
            for address in addresses or [host]:
                family = socket.AF_INET6 if ":" in address else socket.AF_INET
                with socket.socket(family, socket.SOCK_STREAM) as probe:
                    if os.name == "nt":
                        probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                    probe.bind((address, port))
        except OSError:
            continue
        return port
    raise RuntimeError(f"No available Web port starting at {starting_port}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--auto-port", action="store_true", help="Choose an available default port; explicit deployments keep a fixed port")
    parser.add_argument("--network-auto", action="store_true", help="Publish access on physical Wi-Fi, falling back to Ethernet (Windows)")
    parser.add_argument("--network-adapter", help="Network adapter name or index; overrides automatic selection")
    parser.add_argument("--lan-https", action="store_true", help="Serve a persistent local HTTPS identity and public CA installation page")
    parser.add_argument("--https-port", type=int, help="LAN HTTPS port; defaults to the saved port or 8443")
    parser.add_argument("--certificate-port", type=int, help="HTTP CA installation port; defaults to the saved port or 8081")
    parser.add_argument("--lan-domain", help="HTTPS domain; defaults to the saved domain or an sslip.io hostname")
    parser.add_argument("--lan-import", help="Adopt an existing local HTTPS configuration directory without replacing its CA")
    parser.add_argument("--debug", action="store_true", help="Allow debug password login alongside public keys")
    options = parser.parse_args()
    os.environ["BOOKMARK_AUTH_DEBUG"] = "1" if options.debug else "0"
    if options.data_root:
        os.environ["BOOKMARK_DATA_ROOT"] = options.data_root
    # A configured canonical origin may depend on a fixed proxy upstream. Never
    # silently move that deployment when its requested port is occupied.
    from runtime.network_access import select_adapter, configure_http, configure_external_https
    adapter = select_adapter(options.network_adapter) if options.network_auto or options.network_adapter or options.lan_https else None
    https = None
    canonical = os.environ.get("BOOKMARK_PASSKEY_ORIGIN", "")
    if options.lan_https:
        from config.paths import DATA_ROOT
        from runtime.lan_https import LanHttps
        from urllib.parse import urlsplit
        if canonical and not canonical.startswith("https://"):
            raise RuntimeError("局域网 HTTPS 与非 HTTPS 的 BOOKMARK_PASSKEY_ORIGIN 不兼容。")
        parsed = urlsplit(canonical)
        https = LanHttps(DATA_ROOT, adapter, domain=options.lan_domain or parsed.hostname,
            https_port=options.https_port if options.https_port is not None else (parsed.port or 443 if canonical else None),
            certificate_port=options.certificate_port, import_from=options.lan_import)
        if canonical and canonical.rstrip("/") != https.origin:
            raise RuntimeError("局域网 HTTPS 来源与 BOOKMARK_PASSKEY_ORIGIN 不一致。")
        options.host = "127.0.0.1"
    elif any(value is not None for value in (options.https_port, options.certificate_port, options.lan_domain, options.lan_import)):
        parser.error("HTTPS configuration options require --lan-https")
    addresses = ["127.0.0.1", adapter.ipv4] if adapter and not (https or canonical.startswith("https://")) else [options.host]
    choose_port = options.auto_port and not os.environ.get("BOOKMARK_PASSKEY_ORIGIN")
    selected_port = available_port(options.host, options.port, addresses=addresses) if choose_port else options.port
    if https:
        if selected_port in (https.https_port, https.certificate_port):
            raise RuntimeError("Web 上游、HTTPS 和证书安装页必须使用不同端口。")
        https.configure(selected_port)
    elif adapter and canonical.startswith("https://"):
        configure_external_https(adapter, canonical, options.host)
    elif adapter:
        configure_http(adapter, selected_port)
    if not os.environ.get("BOOKMARK_PASSKEY_ORIGIN"):
        if os.environ.get("BOOKMARK_HTTPS") == "1":
            raise RuntimeError("Set BOOKMARK_PASSKEY_ORIGIN to the canonical HTTPS address for an HTTPS deployment.")
        if options.host not in {"localhost", "127.0.0.1", "::1", "0.0.0.0", "::"}:
            raise RuntimeError("Set BOOKMARK_PASSKEY_ORIGIN to the canonical HTTPS address before binding a network address.")
        os.environ["BOOKMARK_PASSKEY_ORIGIN"] = f"http://localhost:{selected_port}"
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django
    django.setup()
    from runtime.authentication import normalize_origin
    try:
        normalize_origin(os.environ["BOOKMARK_PASSKEY_ORIGIN"])
    except (ValueError, TypeError):
        raise RuntimeError("BOOKMARK_PASSKEY_ORIGIN must be an HTTPS domain origin, or http://localhost:<port> for local development.") from None
    from config.paths import DATA_ROOT
    if selected_port != options.port:
        print(f"端口 {options.port} 已被其他服务占用，改用 {selected_port}。", flush=True)
    owner = Supervisor(options.host, selected_port, https=https)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: setattr(owner, "running", False))
    with InstanceLock(DATA_ROOT / "runtime" / "supervisor.lock"):
        from runtime.bootstrap import prepare_databases
        prepare_databases(automatic=True)
        if https:
            https.prepare()
        owner.run()


if __name__ == "__main__":
    main()
