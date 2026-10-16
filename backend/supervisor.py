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


class InstanceLock:
    def __init__(self, path):
        self.path, self.file = Path(path), None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        self.file.seek(0)
        self.file.write(b"0")
        self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError("A supervisor already owns this data directory") from None
        return self

    def __exit__(self, *args):
        if os.name == "nt":
            import msvcrt
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        self.file.close()


class Supervisor:
    def __init__(self, host="127.0.0.1", port=8000):
        self.host, self.port = host, port
        self.children = {}
        self.running = True

    def start_services(self):
        manage = str(Path(__file__).with_name("manage.py"))
        web = self.children.get("web")
        if web is None or web.poll() is not None:
            # Fail before spawning anything if this endpoint belongs to another app.
            family = socket.AF_INET6 if ":" in self.host else socket.AF_INET
            with socket.socket(family, socket.SOCK_STREAM) as probe:
                if os.name == "nt":
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                try:
                    probe.bind((self.host, self.port))
                except OSError:
                    raise RuntimeError("The configured Web port is already in use") from None
        commands = {"web": [sys.executable, manage, "run_web", "--host", self.host, "--port", str(self.port)],
                    "worker": [sys.executable, manage, "run_worker"]}
        if os.environ.get("BOOKMARK_AUTH_DEBUG") == "1":
            for command in commands.values():
                command.append("--debug")
        for name, command in commands.items():
            current = self.children.get(name)
            if current and current.poll() is None:
                continue
            options = {"start_new_session": True} if os.name != "nt" else {
                "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
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
        from runtime.models import RuntimeState, MaintenanceOperation
        from runtime.maintenance import process_operation
        from runtime.performance import span, trace
        try:
            # Recovery can also start children; every startup path belongs to the
            # same cleanup boundary as the steady-state loop.
            with trace("service.startup"):
                with span("service.recover"):
                    self.recover_interrupted()
                if RuntimeState.objects.using("default").get(pk=1).maintenance_mode:
                    raise RuntimeError("Maintenance recovery requires operator repair; preserved library generations")
                with span("service.spawn"):
                    self.start_services()
                with span("service.readiness_wait"):
                    if not self.ready_services():
                        raise RuntimeError("Application did not become ready")
            display_host = "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host
            if ":" in display_host:
                display_host = f"[{display_host}]"
            print(f"网站收藏已启动：http://{display_host}:{self.port}/", flush=True)
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


def available_port(host, starting_port, attempts=20):
    """Choose a free default port without connecting to or stopping another app."""
    if not 1 <= starting_port <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    for port in range(starting_port, min(65536, starting_port + attempts)):
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            if os.name == "nt":
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                probe.bind((host, port))
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
    parser.add_argument("--debug", action="store_true", help="Allow debug password login alongside public keys")
    options = parser.parse_args()
    os.environ["BOOKMARK_AUTH_DEBUG"] = "1" if options.debug else "0"
    if options.data_root:
        os.environ["BOOKMARK_DATA_ROOT"] = options.data_root
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django
    django.setup()
    from config.paths import DATA_ROOT
    selected_port = available_port(options.host, options.port) if options.auto_port else options.port
    if selected_port != options.port:
        print(f"端口 {options.port} 已被其他服务占用，改用 {selected_port}。", flush=True)
    owner = Supervisor(options.host, selected_port)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: setattr(owner, "running", False))
    with InstanceLock(DATA_ROOT / "runtime" / "supervisor.lock"):
        owner.run()


if __name__ == "__main__":
    main()
