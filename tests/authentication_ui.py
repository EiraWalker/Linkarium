"""Built-UI acceptance with real WebAuthn signatures and isolated code/data.

Run after building the frontend. AUTH_UI_DIST selects an alternate build directory.
Virtual authenticators and a phone viewport are automation, not physical-device proof.
Grant secrets, credential material and session cookies stay in process memory.
"""
import json
import os
import re
import random
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import traceback
import urllib.request
import uuid

from playwright.sync_api import sync_playwright, expect


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "playwright" / ("passkey-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
SITE = OUT / "site"
DATA = OUT / "data"
BUILD = Path(os.environ.get("AUTH_UI_DIST", ROOT / "frontend" / "dist")).resolve()
PYTHON = str(ROOT / ".venv" / "Scripts" / "python.exe") if os.name == "nt" else sys.executable
ENV = {key: value for key, value in os.environ.items() if not key.startswith("BOOKMARK_") and key.upper() not in {
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "DJANGO_SETTINGS_MODULE", "DJANGO_SECRET_KEY",
}}
ENV.update(BOOKMARK_DATA_ROOT=str(DATA), BOOKMARK_PERF="0", BOOKMARK_DEBUG="0", BOOKMARK_AUTH_DEBUG="0",
           BOOKMARK_HTTPS="0", BOOKMARK_TRUST_PROXY="0", BOOKMARK_ALLOWED_HOSTS="localhost,127.0.0.1",
           PLAYWRIGHT_BROWSERS_PATH=os.environ.get("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / "tools" / "browsers")), PYTHONDONTWRITEBYTECODE="1")
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = ENV["PLAYWRIGHT_BROWSERS_PATH"]
REPORT = {"state": "running", "checks": [], "step": "prepare", "isolated_data": str(DATA),
          "isolated_code": str(SITE), "frontend_build": str(BUILD), "real_device_acceptance": "not run"}


def step(label):
    REPORT["step"] = label


def passed(label):
    REPORT["checks"].append(label)


def manage(*args):
    result = subprocess.run([PYTHON, "-X", "utf8", str(SITE / "backend" / "manage.py"), *args],
                            cwd=SITE, env=ENV, capture_output=True, text=True, encoding="utf8")
    if result.returncode:
        raise RuntimeError("Isolated management command failed")
    return result.stdout


def console_grant(command):
    lines = manage(command).splitlines()
    return (next(line.removeprefix("Open: ") for line in lines if line.startswith("Open: ")),
            next(line.removeprefix("Authorization code: ") for line in lines if line.startswith("Authorization code: ")))


def start(port):
    log = (OUT / "server.log").open("w", encoding="utf8")
    child = subprocess.Popen([PYTHON, "-X", "utf8", str(SITE / "backend" / "manage.py"),
                              "run_web", "--port", str(port)], cwd=SITE, env=ENV, stdout=log, stderr=log)
    try:
        for _ in range(100):
            if child.poll() is not None:
                raise RuntimeError("Isolated server exited")
            try:
                with urllib.request.urlopen(f"http://localhost:{port}/health/ready", timeout=1) as response:
                    if response.status == 200:
                        return child, log
            except OSError:
                pass
            time.sleep(.1)
        raise TimeoutError("Isolated server not ready")
    except BaseException:
        stop(child, log)
        raise


def stop(child, log):
    if child.poll() is None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], capture_output=True, check=False)
        else:
            child.terminate()
        child.wait(timeout=10)
    log.close()


def add_authenticator(context, page, credential=None):
    cdp = context.new_cdp_session(page)
    cdp.send("WebAuthn.enable")
    identity = cdp.send("WebAuthn.addVirtualAuthenticator", {"options": {
        "protocol": "ctap2", "transport": "internal", "hasResidentKey": True,
        "hasUserVerification": True, "isUserVerified": True, "automaticPresenceSimulation": True,
    }})["authenticatorId"]
    if credential:
        cdp.send("WebAuthn.addCredential", {"authenticatorId": identity, "credential": credential})
    return cdp, identity


def session(context, origin):
    return context.request.get(origin + "/api/v1/session").json()["data"]


def api(context, origin, path, method="GET", body=None):
    token = session(context, origin)["csrf_token"]
    return context.request.fetch(origin + "/api/v1" + path, method=method, data=body,
                                 headers={"X-CSRFToken": token, "Origin": origin,
                                          "Idempotency-Key": str(uuid.uuid4())})


def enroll(page, code, name, bootstrap=False):
    page.get_by_label("初始化码" if bootstrap else "授权码", exact=True).fill(code)
    page.get_by_label("Passkey 名称", exact=True).fill(name)
    page.get_by_role("button", name="创建 Passkey", exact=True).click()
    page.wait_for_url("**/library", timeout=20000)


def settings(page, origin):
    page.goto(origin + "/settings")
    heading = page.get_by_role("heading", name="登录与 Passkey", exact=True)
    expect(heading).to_be_visible()
    return heading.locator("..")


def item(panel, name):
    return panel.locator("li").filter(has=panel.page.get_by_role("heading", name=name, exact=False))


def remove_passkey(page, panel, name):
    item(panel, name).get_by_role("button", name="删除", exact=True).click()
    page.get_by_role("dialog", name="删除 Passkey", exact=True).get_by_role("button", name="确认删除", exact=True).click()


def decode_qr(canvas):
    expect(canvas).to_have_attribute("width", "220")
    pixels = canvas.evaluate("canvas => ({ width: canvas.width, height: canvas.height, data: Array.from(canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data) })")
    result = subprocess.run(["node", "-e",
        "let s='';process.stdin.on('data',c=>s+=c);process.stdin.on('end',()=>{const p=JSON.parse(s);const r=require('./node_modules/jsqr/dist/jsQR.js')(Uint8ClampedArray.from(p.data),p.width,p.height);if(!r)process.exit(2);process.stdout.write(r.data);});"],
        cwd=ROOT / "frontend", input=json.dumps(pixels), capture_output=True, text=True, encoding="utf8")
    if result.returncode:
        raise RuntimeError("QR decoding failed")
    return result.stdout


def main():
    OUT.mkdir(parents=True)
    if not (BUILD / "index.html").is_file():
        raise RuntimeError("Build the frontend or set AUTH_UI_DIST before running")
    shutil.copytree(ROOT / "backend", SITE / "backend", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(BUILD, SITE / "frontend" / "dist")
    # Some hosts assign low ephemeral ports (including Chromium-blocked SIP 5060).
    # Reserve a free high port explicitly so urllib readiness and browser reachability agree.
    for _ in range(50):
        with socket.socket() as probe:
            port = random.randint(49152, 65535)
            try:
                probe.bind(("127.0.0.1", port))
                break
            except OSError:
                continue
    else:
        raise RuntimeError("No free browser-safe test port")
    origin = f"http://localhost:{port}"
    ENV["BOOKMARK_PASSKEY_ORIGIN"] = origin
    manage("initialize")
    first_url, first_code = console_grant("passkey_initialize")
    private_file = OUT / "test-key.pem"
    legacy_private = OUT / "legacy-key.pem"
    child, log = start(port)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            errors = []
            private_key_leaked = []

            def new_context(**kwargs):
                context = browser.new_context(viewport={"width": 1180, "height": 920}, **kwargs)
                context.on("page", lambda page: page.on("pageerror", lambda error: errors.append(type(error).__name__)))
                context.on("request", lambda request: private_key_leaked.append(True)
                           if "/api/v1/session/" in request.url and "PRIVATE KEY" in (request.post_data or "") else None)
                return context

            step("anonymous guesses do not allocate sessions or lock first registration")
            for _ in range(6):
                anonymous = new_context()
                rejected = api(anonymous, origin, "/session/passkey/register/options", "POST", {
                    "enrollment_token": uuid.uuid4().hex + uuid.uuid4().hex, "authorization_code": "00000000",
                })
                assert rejected.status == 403
                assert not any(cookie["name"] in {"sessionid", "linkarium_auth_nonce"} for cookie in anonymous.cookies())
                anonymous.close()
            assert manage("shell", "-c", "from django.contrib.sessions.models import Session; print(Session.objects.count())").strip().endswith("0")
            assert manage("shell", "-c", "from runtime.models import EnrollmentGrant; print(EnrollmentGrant.objects.get(active_slot='bootstrap').failed_attempts)").strip().endswith("0")
            passed("invalid anonymous links allocate no login sessions or nonce cookies and cannot consume the first grant's attempts")

            step("first initialization through built UI")
            original = new_context()
            page = original.new_page()
            cdp, authenticator = add_authenticator(original, page)
            page.goto(origin + "/login")
            expect(page.get_by_text("请打开服务器终端提供的完整注册链接，再输入授权码。", exact=True)).to_be_visible()
            assert not page.get_by_label("初始化码", exact=True).count()
            page.goto(first_url)
            expect(page.get_by_role("heading", name="首次配置", exact=True)).to_be_visible()
            assert not session(original, origin)["authenticated"]
            assert not page.get_by_label("调试密码", exact=True).count()
            page.screenshot(path=str(OUT / "01-first-configuration.png"))
            enroll(page, first_code, "Initial passkey", bootstrap=True)
            original_session = session(original, origin)
            assert original_session["authenticated"] and not original_session["initialization_required"]
            original_credential = cdp.send("WebAuthn.getCredentials", {"authenticatorId": authenticator})["credentials"][0]
            passed("first initialization creates the owner and signs in with a real browser attestation")

            step("local enrollment keeps the original credential and expiry")
            cdp.send("WebAuthn.removeVirtualAuthenticator", {"authenticatorId": authenticator})
            cdp, local_authenticator = add_authenticator(original, page)
            panel = settings(page, origin)
            verification_requests = []

            def lose_first_registration_reply(route):
                verification_requests.append(route.request.post_data)
                if len(verification_requests) == 1:
                    response = route.fetch()
                    assert response.status == 200
                    route.abort("connectionreset")
                else:
                    route.continue_()

            page.route("**/api/v1/session/passkey/register/verify", lose_first_registration_reply)
            panel.get_by_label("Passkey 名称", exact=True).fill("Local second passkey")
            panel.get_by_role("button", name="在本机添加 Passkey", exact=True).click()
            expect(item(panel, "Local second passkey")).to_be_visible(timeout=20000)
            page.unroute("**/api/v1/session/passkey/register/verify", lose_first_registration_reply)
            assert len(verification_requests) == 2 and verification_requests[0] == verification_requests[1]
            assert len(api(original, origin, "/session/passkeys").json()["data"]["items"]) == 2
            after_local = session(original, origin)
            assert after_local["credential_id"] == original_session["credential_id"]
            assert after_local["auth_expires_at"] == original_session["auth_expires_at"]
            assert not panel.get_by_label("授权码", exact=True).count()
            passed("local enrollment recovers a lost server reply by replaying the same signed result exactly once; no code or expiry extension")

            step("passkey login and persistent fourteen-day browser session")
            clone = new_context()
            clone_page = clone.new_page()
            add_authenticator(clone, clone_page, original_credential)
            clone_page.goto(origin + "/login")
            clone_page.get_by_role("button", name="Passkey 登录", exact=True).click()
            clone_page.wait_for_url("**/library", timeout=20000)
            signed_in = session(clone, origin)
            cookie = next(cookie for cookie in clone.cookies() if cookie["name"] == "sessionid")
            assert 14 * 86400 - 120 < cookie["expires"] - time.time() <= 14 * 86400 + 5
            saved = clone.storage_state()
            clone.close()
            restored = new_context(storage_state=saved)
            restored_page = restored.new_page()
            restored_page.goto(origin + "/library")
            assert session(restored, origin)["credential_id"] == signed_in["credential_id"]
            passed("passkey login creates a persistent fourteen-day cookie retained after browser context reopen")

            step("new-device QR and independent authorization code")
            panel.get_by_role("button", name="生成链接和授权码", exact=True).click()
            expect(panel.get_by_label("添加链接", exact=True)).to_be_visible()
            link = panel.get_by_label("添加链接", exact=True).input_value()
            code = panel.get_by_label("授权码", exact=True).input_value()
            decoded = decode_qr(panel.locator('canvas[aria-label="新设备添加链接二维码"]'))
            assert decoded == link and code not in decoded
            mobile = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
            mobile_page = mobile.new_page()
            mobile_page.on("pageerror", lambda error: errors.append(type(error).__name__))
            add_authenticator(mobile, mobile_page)
            mobile_page.goto(link)
            expect(mobile_page.get_by_role("heading", name="添加此设备的 Passkey", exact=True)).to_be_visible()
            assert "#" not in mobile_page.url
            saved_intent = mobile_page.evaluate("sessionStorage.getItem('linkarium.passkey-enrollment')")
            mobile_page.reload()
            expect(mobile_page.get_by_role("heading", name="添加此设备的 Passkey", exact=True)).to_be_visible()
            assert mobile_page.evaluate("sessionStorage.getItem('linkarium.passkey-enrollment')") == saved_intent
            mobile_page.screenshot(path=str(OUT / "02-phone-enrollment.png"))
            enroll(mobile_page, code, "Phone passkey")
            assert mobile_page.evaluate("sessionStorage.getItem('linkarium.passkey-enrollment')") is None
            assert session(mobile, origin)["authenticated"]
            passed("locally rendered QR decodes to the link only; phone viewport joins using a separate code")

            step("rename and revoke every session authenticated by one passkey")
            phone_panel = settings(mobile_page, origin)
            item(phone_panel, "Initial passkey").get_by_role("button", name="重命名", exact=True).click()
            phone_panel.get_by_label("新名称", exact=True).fill("Retired original passkey")
            phone_panel.get_by_role("button", name="保存名称", exact=True).click()
            expect(item(phone_panel, "Retired original passkey")).to_be_visible()
            remove_passkey(mobile_page, phone_panel, "Retired original passkey")
            expect(item(phone_panel, "Retired original passkey")).to_have_count(0)
            assert api(original, origin, "/bookmarks").status == 401
            assert api(restored, origin, "/bookmarks").status == 401
            assert api(mobile, origin, "/bookmarks").status == 200
            restored.close()
            passed("rename persists; deleting a passkey revokes both associated sessions and preserves unrelated sessions")

            step("delete the current passkey and guard the last credential")
            remove_passkey(mobile_page, phone_panel, "Phone passkey")
            mobile_page.wait_for_url("**/login", timeout=20000)
            assert not session(mobile, origin)["authenticated"]
            mobile.close()
            page.goto(origin + "/login")
            page.get_by_role("button", name="Passkey 登录", exact=True).click()
            page.wait_for_url("**/library", timeout=20000)
            panel = settings(page, origin)
            expect(item(panel, "Local second passkey").get_by_role("button", name="最后一种登录凭据不能删除", exact=True)).to_be_disabled()
            last_id = session(original, origin)["credential_id"]
            denied = api(original, origin, "/session/passkeys/" + last_id, "DELETE")
            assert denied.status == 409 and denied.json()["error"]["code"] == "AUTH_LAST_CREDENTIAL"
            passed("deleting the current passkey exits the UI; the last credential is guarded in UI and API")

            step("optional file key signs in without sending private material")
            manage("file_key_setup", "--generate", str(private_file))
            file_context = new_context()
            file_page = file_context.new_page()
            file_page.goto(origin + "/login")
            file_page.locator('input[type="file"]').set_input_files(str(private_file))
            file_page.wait_for_url("**/library", timeout=20000)
            assert session(file_context, origin)["authenticated"] and not private_key_leaked
            passed("optional Ed25519 PKCS8 file signs in through WebCrypto without transmitting PRIVATE KEY material")

            step("prepare retained bookmark and an unused enrollment grant")
            bookmark_response = api(original, origin, "/bookmarks", "POST", {
                "url": "https://example.com/passkey-acceptance", "title": "Retained bookmark", "notes": "Must survive credential reset", "snapshot_enabled": False,
            })
            assert bookmark_response.status == 201
            bookmark_id = bookmark_response.json()["data"]["id"]
            outstanding = api(original, origin, "/session/passkey/grants", "POST", {}).json()["data"]
            reset_url, reset_code = console_grant("passkey_reset")
            assert api(original, origin, "/bookmarks").status == 200
            assert api(file_context, origin, "/bookmarks").status == 200

            step("explicit reset link overrides an existing login and survives refresh within the tab")
            page.goto(reset_url)
            expect(page.get_by_role("heading", name="重置登录凭据", exact=True)).to_be_visible()
            assert "#" not in page.url
            page.reload()
            expect(page.get_by_role("heading", name="重置登录凭据", exact=True)).to_be_visible()
            cdp.send("WebAuthn.removeVirtualAuthenticator", {"authenticatorId": local_authenticator})
            reset_cdp, reset_authenticator = add_authenticator(original, page)
            enroll(page, reset_code, "Replacement passkey")
            expect(page.get_by_role("status").filter(has_text="旧登录凭据已撤销")).to_be_visible()
            assert api(file_context, origin, "/bookmarks").status == 401
            current = api(original, origin, "/session/passkeys").json()["data"]["items"]
            assert len(current) == 1 and current[0]["name"] == "Replacement passkey"
            retained = api(original, origin, "/bookmarks/" + bookmark_id).json()["data"]
            assert retained["title"] == "Retained bookmark" and retained["notes"] == "Must survive credential reset"
            assert not session(original, origin)["file_key_configured"]
            stale_token = outstanding["enrollment_url"].split("#enroll=", 1)[1]
            rejected = api(file_context, origin, "/session/passkey/register/options", "POST", {
                "name": "Stale grant", "enrollment_token": stale_token, "authorization_code": outstanding["authorization_code"],
            })
            assert rejected.status >= 400
            passed("terminal reset issues no early revocation; completion replaces all credentials and retains bookmark notes")
            passed("enrollment and reset forms survive refresh in the same tab without extending the stored deadline; completion clears tab state")
            passed("reset invalidates prior file-key sessions and outstanding grants")

            step("old file key fails and the replacement passkey logs in")
            file_page.goto(origin + "/login")
            file_page.locator('input[type="file"]').set_input_files(str(private_file))
            expect(file_page.get_by_role("alert")).to_be_visible(timeout=20000)
            assert not session(file_context, origin)["authenticated"]
            page.get_by_title("退出登录", exact=True).click()
            page.wait_for_url("**/login")
            page.get_by_role("button", name="Passkey 登录", exact=True).click()
            page.wait_for_url("**/library", timeout=20000)
            final_panel = settings(page, origin)
            expect(item(final_panel, "Replacement passkey")).to_be_visible()
            page.screenshot(path=str(OUT / "03-final-settings.png"), full_page=True)
            denied_password = api(original, origin, "/session/login", "POST", {"password": "654321", "debug": True})
            assert denied_password.status == 403 and denied_password.json()["error"]["code"] == "PASSWORD_LOGIN_DISABLED"
            assert not private_key_leaked and not errors
            file_context.close()
            passed("old file key cannot log in; replacement passkey logs in; normal password endpoint rejects debug bypass")

            step("existing owner without any Passkey needs the terminal's complete first setup link")
            manage("file_key_setup", "--generate", str(legacy_private))
            current_id = session(original, origin)["credential_id"]
            assert api(original, origin, "/session/passkeys/" + current_id, "DELETE").status == 200
            legacy = new_context()
            legacy_page = legacy.new_page()
            add_authenticator(legacy, legacy_page)
            legacy_page.goto(origin + "/login")
            expect(legacy_page.get_by_role("heading", name="创建第一个 Passkey", exact=True)).to_be_visible()
            legacy_page.reload()
            expect(legacy_page.get_by_role("heading", name="创建第一个 Passkey", exact=True)).to_be_visible()
            legacy_state = session(legacy, origin)
            assert not legacy_state["initialization_required"] and not legacy_state["passkey_configured"] and legacy_state["file_key_configured"]
            legacy_page.screenshot(path=str(OUT / "04-existing-owner-first-passkey.png"))
            code_url, code_only = console_grant("passkey_initialize")
            assert code_url.startswith(origin + "/login#initialize=")
            expect(legacy_page.get_by_text("请打开服务器终端提供的完整注册链接，再输入授权码。", exact=True)).to_be_visible()
            assert not legacy_page.get_by_label("授权码", exact=True).count()
            legacy_page.goto(code_url)
            enroll(legacy_page, code_only, "Existing owner first passkey")
            assert session(legacy, origin)["file_key_configured"]
            assert api(legacy, origin, "/bookmarks/" + bookmark_id).json()["data"]["notes"] == "Must survive credential reset"
            assert len(api(legacy, origin, "/session/passkeys").json()["data"]["items"]) == 1
            passed("existing owner with zero Passkeys configures the first one using a secret terminal link and independent code; owner, optional file key and bookmarks survive")

            step("expired enrollment state is discarded and a configured site shows normal login")
            expired = new_context()
            expired_page = expired.new_page()
            expired_page.goto(origin + "/login")
            expired_page.evaluate("sessionStorage.setItem('linkarium.passkey-enrollment', JSON.stringify({intent:{kind:'enroll',token:'expired-test'},expiresAt:Date.now()-1}))")
            expired_page.reload()
            expect(expired_page.get_by_role("button", name="Passkey 登录", exact=True)).to_be_visible()
            assert expired_page.evaluate("sessionStorage.getItem('linkarium.passkey-enrollment')") is None
            expired_page.evaluate("sessionStorage.setItem('linkarium.passkey-enrollment', JSON.stringify({intent:{kind:'enroll',token:'expiry-timer-test'},expiresAt:Date.now()+3000}))")
            expired_page.reload()
            expect(expired_page.get_by_role("heading", name="添加此设备的 Passkey", exact=True)).to_be_visible()
            expired_page.wait_for_function("() => sessionStorage.getItem('linkarium.passkey-enrollment') === null", timeout=5000)
            expired_page.reload()
            expect(expired_page.get_by_role("button", name="Passkey 登录", exact=True)).to_be_visible()
            passed("expired tab state is removed; after the first Passkey is configured the login page offers normal authentication")
            assert not private_key_leaked and not errors
            browser.close()
        REPORT["state"] = "passed"
    finally:
        stop(child, log)
        private_file.unlink(missing_ok=True)
        legacy_private.unlink(missing_ok=True)


if __name__ == "__main__":
    status = 0
    try:
        main()
    except BaseException as error:
        # Playwright messages can contain filled values or grant URLs. Keep only the step and type.
        REPORT.update(state="failed", error_type=type(error).__name__)
        browser_error = re.search(r"net::[A-Z_]+", str(error))
        if browser_error:
            REPORT["browser_error_type"] = browser_error.group(0)
        REPORT["error_location"] = [{"function": frame.name, "line": frame.lineno} for frame in traceback.extract_tb(error.__traceback__)
                                    if Path(frame.filename).resolve() == Path(__file__).resolve()]
        status = 1
    finally:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "report.json").write_text(json.dumps(REPORT, ensure_ascii=False, indent=2), encoding="utf8")
        print(json.dumps({"state": REPORT["state"], "step": REPORT["step"], "report": str(OUT / "report.json")}), flush=True)
    sys.exit(status)
