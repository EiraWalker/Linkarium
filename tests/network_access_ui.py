"""Issue #5 acceptance through real startup, sockets and the built UI.

Owns only isolated data/processes below output; never changes host adapters.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shutil
import ssl
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

from playwright.sync_api import sync_playwright, expect
from authentication_ui import add_authenticator, api, decode_qr, enroll

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output" / "network-access-acceptance"
sys.path.insert(0, str(ROOT / "backend"))


def get(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=2) as response:
        return response.status, response.read()


def free_port():
    for _ in range(100):
        port = random.randint(49152, 64000)
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("No free browser-safe port")


def certificate_browser_argument(leaf):
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    certificate = x509.load_der_x509_certificate(leaf)
    spki = base64.b64encode(hashlib.sha256(certificate.public_key().public_bytes(
        Encoding.DER, PublicFormat.SubjectPublicKeyInfo)).digest()).decode()
    return f"--ignore-certificate-errors-spki-list={spki}"


def browser_api(page, path, method="GET", body=None):
    return page.evaluate("""async ({path, method, body}) => {
        const {data: session} = await (await fetch('/api/v1/session')).json();
        const response = await fetch('/api/v1' + path, {method,
            headers: {'Content-Type': 'application/json', 'X-CSRFToken': session.csrf_token},
            ...(body === null ? {} : {body: JSON.stringify(body)})});
        return {status: response.status, body: await response.json()};
    }""", {"path": path, "method": method, "body": body})


def preserved_library(site, data, *, seed=False):
    """Seed isolated business fixtures and compare stored content, not service liveness."""
    program = """import hashlib, json, os, django
os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings'
django.setup()
from django.core import serializers
from django.contrib.auth import get_user_model
from django.utils import timezone
from library.models import Folder, Tag, Bookmark, BookmarkTag, Artifact, SnapshotVersion, CaptureState, Job, JobAttempt
from library.artifacts import artifact_path
from runtime.models import PasskeyCredential, FileKeyCredential, AuthState, RuntimeState
if SEED:
    folder = Folder.objects.create(name='Preserved folder', name_key='preserved folder')
    tag = Tag.objects.create(name='Preserved tag', name_key='preserved tag')
    bookmark = Bookmark.objects.create(original_url='https://example.invalid/preserved',
        canonical_url='https://example.invalid/preserved', domain='example.invalid',
        source_title='Preserved bookmark', notes='Notes remain after switching modes',
        folder=folder, auto_refresh_paused=True)
    BookmarkTag.objects.create(bookmark=bookmark, tag=tag)
    now = timezone.now()
    state = RuntimeState.objects.get(pk=1)
    job = Job.objects.create(kind='capture', state='succeeded', available_at=now,
        generation=state.active_generation, library_epoch=state.library_epoch)
    attempt = JobAttempt.objects.create(job=job, attempt_no=1, state='succeeded',
        lease_expires_at=now, heartbeat_at=now, generation=state.active_generation, library_epoch=state.library_epoch)
    content = b'<!doctype html><title>Preserved snapshot</title><p>Saved page content</p>'
    digest = hashlib.sha256(content).hexdigest()
    path = artifact_path(digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    artifact = Artifact.objects.create(sha256=digest, relative_path=f'artifacts/{digest[:2]}/{digest}.html', byte_size=len(content))
    version = SnapshotVersion.objects.create(bookmark=bookmark, variant='desktop', artifact=artifact,
        capture_attempt=attempt, captured_at=now, source_url=bookmark.original_url,
        final_url=bookmark.original_url, viewport_width=1180, viewport_height=920, capture_epoch=1)
    CaptureState.objects.create(bookmark=bookmark, variant='desktop', current_version=version, last_success_at=now)
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    public = Ed25519PrivateKey.generate().public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    FileKeyCredential.objects.create(user=get_user_model().objects.get(username='owner'),
        public_key=public, fingerprint=hashlib.sha256(public).hexdigest(), name='Existing file key')
models = (Folder, Tag, Bookmark, BookmarkTag, Artifact, SnapshotVersion, CaptureState)
records = [json.loads(serializers.serialize('json', model.objects.order_by(model._meta.pk.name))) for model in models]
files = {item.sha256: hashlib.sha256(artifact_path(item.sha256, item.relative_path).read_bytes()).hexdigest() for item in Artifact.objects.all()}
credentials = list(PasskeyCredential.objects.order_by('id').values('id','user_id','credential_id','public_key','rp_id','name'))
file_keys = list(FileKeyCredential.objects.order_by('id').values('id','user_id','fingerprint','public_key','name'))
owners = list(get_user_model().objects.order_by('id').values('id','username','password'))
identity = list(AuthState.objects.values('credential_epoch','webauthn_user_handle'))
print(json.dumps(dict(records=records, files=files, credentials=credentials, file_keys=file_keys, owners=owners, identity=identity),
    default=lambda value: value.hex() if isinstance(value, (bytes, memoryview)) else str(value)))
""".replace("if SEED:", f"if {seed!r}:")
    env = {key: value for key, value in os.environ.items() if not key.startswith("BOOKMARK_")}
    env.update(BOOKMARK_DATA_ROOT=str(data), PYTHONPATH=str(site / "backend"), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", program], cwd=site,
        env=env, capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(result.stdout)


class RunningSite:
    def __init__(self, site, data, port, *arguments, extra_env=None):
        env = {key: value for key, value in os.environ.items() if not key.startswith("BOOKMARK_") and key.upper() not in {
            "DJANGO_SETTINGS_MODULE", "DJANGO_SECRET_KEY", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}}
        env["PYTHONPATH"] = str(site / "backend")
        env["PYTHONUNBUFFERED"] = "1"
        caddy = Path(os.environ.get("NETWORK_UI_CADDY", ROOT / "tools" / "caddy" / "caddy.exe"))
        if not caddy.exists():
            caddy = ROOT / "tools" / "wifi-https" / "bin" / "caddy.exe"
        env["BOOKMARK_CADDY"] = str(caddy)
        env.update(extra_env or {})
        # stdin substitutes only the console SIGINT boundary, not application code.
        program = """import runpy, signal, sys, threading
def stop():
    sys.stdin.readline()
    signal.raise_signal(signal.SIGINT)
threading.Thread(target=stop, daemon=True).start()
sys.argv = ['supervisor.py'] + sys.argv[1:]
runpy.run_path('backend/supervisor.py', run_name='__main__')
"""
        self.lines = []
        self.child = subprocess.Popen([sys.executable, "-X", "utf8", "-u", "-c", program,
            "--data-root", str(data), "--port", str(port), *arguments], cwd=site, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding="utf-8", text=True)
        self.reader = threading.Thread(target=lambda: self.lines.extend(iter(self.child.stdout.readline, "")), daemon=True)
        self.reader.start()

    def ready(self, url):
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if self.child.poll() is not None:
                raise AssertionError("Startup exited: " + "".join(self.lines))
            try:
                if get(url)[0] == 200:
                    return
            except OSError:
                pass
            time.sleep(.2)
        raise AssertionError("Startup did not become ready: " + "".join(self.lines))

    def close(self):
        forced = False
        if self.child.poll() is None:
            self.child.stdin.write("\n")
            self.child.stdin.flush()
            try:
                self.child.wait(timeout=25)
            except subprocess.TimeoutExpired:
                forced = True
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(self.child.pid), "/T", "/F"], capture_output=True)
                else:
                    self.child.kill()
                self.child.wait(timeout=10)
        self.reader.join(timeout=3)
        self.child.stdout.close()
        self.child.stdin.close()
        if forced:
            raise AssertionError("The supervisor did not stop its owned services after SIGINT")
        if any(line.startswith("网站收藏已启动：") for line in self.lines) and self.child.returncode != 0:
            raise AssertionError("A started supervisor exited with an error: " + "".join(self.lines))

    def output_containing(self, marker):
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            for line in list(self.lines):
                if line.startswith(marker):
                    return line[len(marker):].strip()
            if self.child.poll() is not None:
                break
            time.sleep(.1)
        raise AssertionError(f"Missing terminal output {marker}: " + "".join(self.lines))


@unittest.skipUnless(os.name == "nt", "Windows physical-adapter acceptance")
class NetworkAccessAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from runtime.network_access import select_adapter
        cls.adapter = select_adapter()
        OUTPUT.mkdir(parents=True, exist_ok=True)
        cls.work = Path(tempfile.mkdtemp(prefix="run-", dir=OUTPUT))
        cls.site = cls.work / "site"
        shutil.copytree(ROOT / "backend", cls.site / "backend", ignore=shutil.ignore_patterns("__pycache__"))
        build = Path(os.environ.get("NETWORK_UI_DIST", ROOT / "frontend" / "dist"))
        shutil.copytree(build, cls.site / "frontend" / "dist")

    def test_http_startup_listens_on_advertised_interface_and_localhost(self):
        port = free_port()
        server = RunningSite(self.site, self.work / "http-data", port, "--network-auto")
        try:
            server.ready(f"http://127.0.0.1:{port}/health/ready")
            self.assertEqual(get(f"http://{self.adapter.ipv4}:{port}/")[0], 200)
            session = json.loads(get(f"http://localhost:{port}/api/v1/session")[1])["data"]
            self.assertTrue(session["passkey_available"])
            time.sleep(.2)
            self.assertIn(f"http://{self.adapter.ipv4}:{port}/", "".join(server.lines))
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(viewport={"width": 1180, "height": 920})
                page = context.new_page()
                add_authenticator(context, page)
                origin = f"http://localhost:{port}"
                page.goto(server.output_containing("Open: "))
                enroll(page, server.output_containing("Authorization code: "), "HTTP local", bootstrap=True)
                page.goto(origin + "/settings")
                expect(page.get_by_label("访问链接", exact=True)).to_have_value(f"http://{self.adapter.ipv4}:{port}/")
                self.assertEqual(decode_qr(page.get_by_role("img", name="网站访问二维码")), f"http://{self.adapter.ipv4}:{port}/")
                expect(page.get_by_role("button", name="安装 CA 证书", exact=True)).to_have_count(0)
                expect(page.get_by_label("授权码", exact=True)).to_have_count(0)
                self.assertEqual(api(context, origin, "/session/passkey/grants", method="POST", body={}).status, 403)
                page.screenshot(path=str(self.work / "http-settings.png"), full_page=True)
                context.close()
                browser.close()
        finally:
            server.close()

    def test_auto_port_reports_the_actual_listening_address(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", free_port()))
            occupied.listen()
            requested = occupied.getsockname()[1]
            server = RunningSite(self.site, self.work / "auto-data", requested, "--network-auto", "--auto-port")
            try:
                address = server.output_containing("访问地址：").split("（", 1)[0]
                from urllib.parse import urlsplit
                actual = urlsplit(address)
                self.assertEqual(actual.hostname, self.adapter.ipv4)
                self.assertNotEqual(actual.port, requested)
                self.assertEqual(get(address)[0], 200)
                self.assertEqual(get(f"http://localhost:{actual.port}/health/ready")[0], 200)
            finally:
                server.close()

    def test_invalid_explicit_adapter_and_missing_https_dependency_fail_before_serving(self):
        cases = [
            (("--network-adapter", "Missing-issue5-adapter"), {}, "未找到网络适配器"),
            (("--lan-https",), {"BOOKMARK_CADDY": str(self.work / "missing-caddy.exe")}, "缺少 Caddy"),
            (("--lan-https", "--https-port", "65536"), {}, "有效端口"),
        ]
        for index, (arguments, environment, message) in enumerate(cases):
            with self.subTest(arguments=arguments):
                data = self.work / f"invalid-{index}"
                server = RunningSite(self.site, data, free_port(), *arguments, extra_env=environment)
                try:
                    self.assertNotEqual(server.child.wait(timeout=25), 0)
                    server.reader.join(timeout=2)
                    self.assertIn(message, "".join(server.lines))
                    self.assertNotIn("网站收藏已启动：", "".join(server.lines))
                    self.assertFalse(data.exists())
                finally:
                    server.close()

    def test_occupied_https_port_does_not_downgrade_to_http(self):
        with socket.socket() as occupied:
            occupied.bind((self.adapter.ipv4, free_port()))
            occupied.listen()
            server = RunningSite(self.site, self.work / "occupied-data", free_port(), "--lan-https",
                "--https-port", str(occupied.getsockname()[1]), "--certificate-port", str(free_port()))
            try:
                self.assertNotEqual(server.child.wait(timeout=30), 0)
                server.reader.join(timeout=2)
                self.assertIn("端口已被占用", "".join(server.lines))
                self.assertNotIn("网站收藏已启动：", "".join(server.lines))
            finally:
                server.close()

    def test_unreachable_canonical_domain_does_not_announce_success(self):
        server = RunningSite(self.site, self.work / "bad-domain", free_port(), "--lan-https",
            "--lan-domain", "localhost.localdomain", "--https-port", str(free_port()),
            "--certificate-port", str(free_port()))
        try:
            deadline = time.monotonic() + 50
            while server.child.poll() is None and time.monotonic() < deadline:
                self.assertNotIn("网站收藏已启动：", "".join(server.lines))
                time.sleep(.2)
            self.assertIsNotNone(server.child.poll(), "Invalid canonical domain did not fail startup")
            self.assertNotEqual(server.child.returncode, 0)
            self.assertIn("localhost.localdomain", "".join(server.lines))
            self.assertIn("DNS", "".join(server.lines))
        finally:
            server.close()

    def test_https_startup_publishes_ca_and_real_tls_service(self):
        import http.client
        from urllib.parse import urlsplit
        port, tls_port, cert_port = free_port(), free_port(), free_port()
        server = RunningSite(self.site, self.work / "https data", port, "--lan-https",
            "--https-port", str(tls_port), "--certificate-port", str(cert_port))
        try:
            server.ready(f"http://127.0.0.1:{port}/health/ready")
            ca_path = Path(server.output_containing("CA 证书："))
            origin = urlsplit(server.output_containing("网站收藏已启动："))
            self.assertTrue(ca_path.is_file())
            self.assertEqual(get(f"http://127.0.0.1:{cert_port}/linkarium-ca.crt")[1], ca_path.read_bytes())
            with self.assertRaises(OSError):
                socket.create_connection((self.adapter.ipv4, cert_port), timeout=1)
            for private in ("/root.key", "/state.sqlite3", "/api/v1/session"):
                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    get(f"http://127.0.0.1:{cert_port}{private}")
                self.assertEqual(rejected.exception.code, 404)
            with self.assertRaises(OSError):
                socket.create_connection((self.adapter.ipv4, port), timeout=1)
            context = ssl.create_default_context(cafile=ca_path)
            with socket.create_connection((self.adapter.ipv4, tls_port), timeout=5) as raw:
                with context.wrap_socket(raw, server_hostname=origin.hostname) as tls:
                    leaf = tls.getpeercert(binary_form=True)
                    tls.sendall(f"GET /api/v1/session HTTP/1.1\r\nHost: {origin.netloc}\r\nConnection: close\r\n\r\n".encode())
                    response = http.client.HTTPResponse(tls)
                    response.begin()
                    self.assertEqual(response.status, 200)
                    session = json.loads(response.read())["data"]
                    self.assertTrue(session["passkey_available"])
                    self.assertEqual(session["passkey_origin"], f"https://{origin.netloc}")
                    self.assertIn("Secure", response.getheader("Set-Cookie"))
            connection = http.client.HTTPConnection("127.0.0.1", port)
            connection.request("GET", "/settings")
            redirected = connection.getresponse()
            self.assertEqual(redirected.status, 302)
            self.assertEqual(redirected.getheader("Location"), f"https://{origin.netloc}/settings")
            connection.close()
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, args=["--no-proxy-server",
                    certificate_browser_argument(leaf)])
                context = browser.new_context(viewport={"width": 1180, "height": 920})
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                add_authenticator(context, page)
                site_origin = f"https://{origin.netloc}"
                previous_link = server.output_containing("Open: ")
                page.goto(previous_link)
                self.assertTrue(page.evaluate("window.isSecureContext"))
                previous_code = server.output_containing("Authorization code: ")
                retained_ca = ca_path.read_bytes()
                server.close()
                server = RunningSite(self.site, self.work / "https data", port, "--lan-https")
                current_code = server.output_containing("Authorization code: ")
                self.assertNotEqual(current_code, previous_code)
                self.assertEqual(ca_path.read_bytes(), retained_ca)
                page.goto(server.output_containing("Open: "))
                rejected = browser_api(page, "/session/passkey/register/options", method="POST",
                    body={"enrollment_token": previous_link.split("=", 1)[1], "authorization_code": previous_code, "name": "Expired startup code"})
                self.assertEqual(rejected["status"], 403)
                self.assertEqual(rejected["body"]["error"]["code"], "AUTH_REVOKED")
                enroll(page, current_code, "HTTPS owner", bootstrap=True)
                page.goto(site_origin + "/settings")
                expect(page.get_by_role("button", name="安装 CA 证书", exact=True)).to_have_count(0)
                expect(page.get_by_text("新设备的 CA 证书请从服务器本机通过可信渠道传输", exact=False)).to_be_visible()
                page.get_by_role("button", name="生成链接和授权码", exact=True).click()
                link = page.get_by_label("添加链接", exact=True)
                expect(link).to_have_value(re.compile("^" + re.escape(site_origin) + r"/login#enroll="))
                url = link.input_value()
                code = page.get_by_label("授权码", exact=True).input_value()
                self.assertEqual(decode_qr(page.get_by_role("img", name="新设备添加链接二维码")), url)
                self.assertNotIn(code, url)
                expect(page.get_by_role("button", name="安装 CA 证书", exact=True)).to_have_count(0)
                self.assertEqual(link.input_value(), url)
                second = browser.new_context(viewport={"width": 390, "height": 844})
                phone = second.new_page()
                add_authenticator(second, phone)
                phone.goto(url)
                enroll(phone, code, "New HTTPS device")
                self.assertEqual(browser_api(phone, "/bookmarks")["status"], 200)
                phone.goto(site_origin + "/settings")
                expect(phone.get_by_role("button", name="安装 CA 证书", exact=True)).to_have_count(0)
                phone.get_by_role("button", name="生成链接和授权码", exact=True).click()
                mobile_link = phone.get_by_label("添加链接", exact=True)
                expect(mobile_link).to_have_value(re.compile("^" + re.escape(site_origin) + r"/login#enroll="))
                self.assertEqual(decode_qr(phone.get_by_role("img", name="新设备添加链接二维码")), mobile_link.input_value())
                expect(phone.get_by_label("授权码", exact=True)).to_have_value(re.compile(r"^\d{8}$"))
                self.assertTrue(phone.evaluate("document.documentElement.scrollWidth <= innerWidth"))
                page.get_by_label("授权码", exact=True).evaluate("input => input.value = 'REDACTED'")
                phone.get_by_label("授权码", exact=True).evaluate("input => input.value = 'REDACTED'")
                page.screenshot(path=str(self.work / "https-settings.png"), full_page=True)
                phone.screenshot(path=str(self.work / "https-new-device.png"), full_page=True)
                original_ca = ca_path.read_bytes()
                server.close()
                restarted_port = free_port()
                server = RunningSite(self.site, self.work / "https data", restarted_port, "--lan-https")
                server.ready(f"http://127.0.0.1:{restarted_port}/health/ready")
                self.assertEqual(server.output_containing("网站收藏已启动："), site_origin + "/")
                self.assertEqual(Path(server.output_containing("CA 证书：")).read_bytes(), original_ca)
                self.assertEqual(browser_api(phone, "/session/logout", method="POST", body={})["status"], 200)
                phone.goto(site_origin + "/login")
                phone.get_by_role("button", name="Passkey 登录", exact=True).click()
                phone.wait_for_url("**/library", timeout=20000)
                self.assertEqual(browser_api(phone, "/bookmarks")["status"], 200)
                self.assertFalse(errors)
                second.close()
                context.close()
                browser.close()
        finally:
            server.close()

    def test_populated_library_and_existing_credentials_survive_mode_changes(self):
        data = self.work / "preserved-data"
        local_port, tls_port, cert_port = free_port(), free_port(), free_port()
        server = RunningSite(self.site, data, local_port, "--network-auto")
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, args=["--no-proxy-server"])
                local_context = browser.new_context()
                local = local_context.new_page()
                add_authenticator(local_context, local)
                local_origin = f"http://localhost:{local_port}"
                server.ready(local_origin + "/health/ready")
                local.goto(server.output_containing("Open: "))
                enroll(local, server.output_containing("Authorization code: "), "Original localhost", bootstrap=True)
                original = preserved_library(self.site, data, seed=True)
                self.assertTrue(original["records"][0])
                self.assertTrue(original["files"])
                self.assertEqual(len(original["credentials"]), 1)
                self.assertEqual(browser_api(local, "/session/logout", method="POST", body={})["status"], 200)
                server.close()
                server = RunningSite(self.site, data, free_port(), "--lan-https", "--https-port", str(tls_port),
                    "--certificate-port", str(cert_port))
                https_origin = server.output_containing("网站收藏已启动：").rstrip("/")
                self.assertEqual(preserved_library(self.site, data), original)
                from urllib.parse import urlsplit
                endpoint = urlsplit(https_origin)
                ca_path = Path(server.output_containing("CA 证书："))
                trusted = ssl.create_default_context(cafile=ca_path)
                with socket.create_connection((endpoint.hostname, endpoint.port), timeout=5) as raw:
                    with trusted.wrap_socket(raw, server_hostname=endpoint.hostname) as tls:
                        leaf = tls.getpeercert(binary_form=True)
                secure_browser = playwright.chromium.launch(headless=True, args=["--no-proxy-server",
                    certificate_browser_argument(leaf)])
                secure_context = secure_browser.new_context()
                secure = secure_context.new_page()
                add_authenticator(secure_context, secure)
                secure.goto(server.output_containing("Open: "))
                enroll(secure, server.output_containing("Authorization code: "), "Added HTTPS")
                after_registration = preserved_library(self.site, data)
                self.assertEqual(len(after_registration["credentials"]), 2)
                self.assertIn(original["credentials"][0], after_registration["credentials"])
                for key in original.keys() - {"credentials"}:
                    self.assertEqual(after_registration[key], original[key], key)
                self.assertEqual(browser_api(secure, "/bookmarks")["status"], 200)
                server.close()
                server = RunningSite(self.site, data, free_port(), "--lan-https")
                self.assertEqual(server.output_containing("网站收藏已启动："), https_origin + "/")
                self.assertEqual(preserved_library(self.site, data), after_registration)
                server.close()
                server = RunningSite(self.site, data, local_port, "--network-auto")
                server.output_containing("网站收藏已启动：")
                local.goto(local_origin + "/login")
                local.get_by_role("button", name="Passkey 登录", exact=True).click()
                local.wait_for_url("**/library", timeout=20000)
                self.assertEqual(browser_api(local, "/bookmarks")["status"], 200)
                self.assertEqual(preserved_library(self.site, data), after_registration)
                local.goto(local_origin + "/settings")
                expect(local.get_by_role("button", name="安装 CA 证书", exact=True)).to_have_count(0)
                for port in (tls_port, cert_port):
                    with self.assertRaises(OSError):
                        socket.create_connection((self.adapter.ipv4, port), timeout=1)
                secure_context.close()
                secure_browser.close()
                local_context.close()
                browser.close()
        finally:
            server.close()

    def test_legacy_ca_adoption_and_identity_failures_do_not_replace_the_ca(self):
        from urllib.parse import urlsplit
        tls_port, cert_port = free_port(), free_port()
        source = RunningSite(self.site, self.work / "import-source-data", free_port(), "--lan-https",
            "--https-port", str(tls_port), "--certificate-port", str(cert_port))
        try:
            source_ca = Path(source.output_containing("CA 证书："))
            original_ca = source_ca.read_bytes()
            domain = urlsplit(source.output_containing("网站收藏已启动：")).hostname
        finally:
            source.close()
        legacy = self.work / "legacy-https"
        authority = Path("pki/authorities/local")
        shutil.copytree(source_ca.parent.parent / "caddy" / authority, legacy / "state" / "caddy" / authority)
        (legacy / "config.json").write_text(json.dumps({"Address": self.adapter.ipv4, "Domain": domain,
            "HttpsPort": tls_port, "CertificatePort": cert_port}), encoding="utf-8")
        target_data = self.work / "import-target-data"
        target = RunningSite(self.site, target_data, free_port(), "--lan-https", "--lan-import", str(legacy))
        try:
            target_ca = Path(target.output_containing("CA 证书："))
            self.assertEqual(target_ca.read_bytes(), original_ca)
            self.assertEqual(source_ca.read_bytes(), original_ca)
            self.assertIn(domain, target.output_containing("网站收藏已启动："))
        finally:
            target.close()
        configuration = target_ca.parent.parent / "connection.json"
        original_config = configuration.read_bytes()
        changed = json.loads(original_config)
        changed["address"] = "192.0.2.99"
        configuration.write_text(json.dumps(changed), encoding="utf-8")
        rejected = RunningSite(self.site, target_data, free_port(), "--lan-https")
        try:
            self.assertNotEqual(rejected.child.wait(timeout=25), 0)
            rejected.reader.join(timeout=2)
            self.assertIn("不一致", "".join(rejected.lines))
            self.assertEqual(target_ca.read_bytes(), original_ca)
        finally:
            rejected.close()
            configuration.write_bytes(original_config)
        private_key = target_ca.parent.parent / "caddy" / authority / "root.key"
        preserved_key = private_key.with_suffix(".saved")
        private_key.rename(preserved_key)
        rejected = RunningSite(self.site, target_data, free_port(), "--lan-https")
        try:
            self.assertNotEqual(rejected.child.wait(timeout=25), 0)
            rejected.reader.join(timeout=2)
            self.assertIn("CA 资料缺失", "".join(rejected.lines))
            self.assertFalse(private_key.exists())
            self.assertEqual(target_ca.read_bytes(), original_ca)
        finally:
            rejected.close()
            preserved_key.rename(private_key)


if __name__ == "__main__":
    program = unittest.main(verbosity=2, exit=False)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "report.json").write_text(json.dumps({"tests": program.result.testsRun,
        "failures": len(program.result.failures), "errors": len(program.result.errors),
        "skipped": len(program.result.skipped), "passed": program.result.wasSuccessful(),
        "real_android": "not run", "system_ca_store_modified": False}, indent=2), encoding="utf-8")
    raise SystemExit(not program.result.wasSuccessful())
