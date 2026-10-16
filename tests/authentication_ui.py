"""Real browser acceptance with a virtual authenticator and an isolated library."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "output" / "passkey-auth" / time.strftime("ui-%Y%m%d-%H%M%S")
OUT.mkdir(parents=True)
DATA = OUT / "data"
ENV = dict(os.environ, BOOKMARK_DATA_ROOT=str(DATA), PLAYWRIGHT_BROWSERS_PATH=str(ROOT / "tools" / "browsers"),
    BOOKMARK_PASSKEY_ORIGIN="", BOOKMARK_PERF="0")
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = ENV["PLAYWRIGHT_BROWSERS_PATH"]
PYTHON = str(ROOT / ".venv" / "Scripts" / "python.exe") if os.name == "nt" else sys.executable
REPORT = {"checks": [], "state": "running", "isolated_data": str(DATA)}


def manage(*args, input=None):
    result = subprocess.run([PYTHON, "-X", "utf8", str(ROOT / "backend" / "manage.py"), *args], cwd=ROOT,
        env=ENV, input=input, capture_output=True, text=True, encoding="utf8", check=True)
    return result.stdout


def start(port, debug):
    output = (OUT / ("debug.log" if debug else "normal.log")).open("w", encoding="utf8")
    child = subprocess.Popen([PYTHON, "-X", "utf8", str(ROOT / "backend" / "manage.py"), "run_web", "--port", str(port),
        *(["--debug"] if debug else [])], cwd=ROOT, env=ENV, stdout=output, stderr=output)
    try:
        for _ in range(100):
            if child.poll() is not None:
                raise RuntimeError("Isolated server exited")
            try:
                with urllib.request.urlopen(f"http://localhost:{port}/health/ready", timeout=1) as response:
                    if response.status == 200:
                        return child, output
            except OSError:
                pass
            time.sleep(.1)
        raise TimeoutError("Isolated server not ready")
    except BaseException:
        stop(child, output)
        raise


def stop(child, output):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], capture_output=True, check=False)
    else:
        child.terminate()
    child.wait(timeout=10)
    output.close()


def logout(page):
    page.get_by_title("退出登录").click()
    page.wait_for_url("**/login")


def main():
    private_file = OUT / "test-key.pem"
    manage("file_key_setup", "--generate", str(private_file))
    manage("initialize", "--private-key", str(private_file))
    manage("shell", "--debug", "-c", "from django.contrib.auth import get_user_model;u=get_user_model().objects.get(username='owner');u.set_password('654321');u.save(update_fields=['password'])")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    origin = f"http://localhost:{port}"
    child, output = start(port, False)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1120, "height": 900})
            page = context.new_page()
            errors, posted = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: posted.append(request.post_data or "") if "/api/v1/session/" in request.url else None)
            cdp = context.new_cdp_session(page)
            cdp.send("WebAuthn.enable")
            cdp.send("WebAuthn.addVirtualAuthenticator", {"options": {"protocol": "ctap2", "transport": "internal",
                "hasResidentKey": True, "hasUserVerification": True, "isUserVerified": True,
                "automaticPresenceSimulation": True}})
            page.goto(origin + "/login")
            page.locator('input[type="file"]').set_input_files(str(private_file))
            page.wait_for_url("**/library", timeout=20000)
            page.goto(origin + "/settings")
            expect(page.get_by_role("heading", name="登录与 Passkey")).to_be_visible()
            page.get_by_label("Passkey 名称", exact=True).fill("浏览器验收设备")
            page.get_by_role("button", name="添加 Passkey", exact=True).click()
            expect(page.locator("strong").filter(has_text="浏览器验收设备")).to_be_visible(timeout=20000)
            page.screenshot(path=str(OUT / "passkey-settings.png"))
            REPORT["checks"].append("normal private-key initialization then file login and Settings Passkey enrollment")
            logout(page)
            assert page.get_by_label("调试密码", exact=True).count() == 0
            page.screenshot(path=str(OUT / "normal-login.png"))
            page.get_by_role("button", name="Passkey 登录", exact=True).click()
            page.wait_for_url("**/library", timeout=20000)
            REPORT["checks"].append("normal-mode Passkey login with actual browser-generated signature")
            logout(page)
            page.locator('input[type="file"]').set_input_files(str(private_file))
            page.wait_for_url("**/library", timeout=20000)
            REPORT["checks"].append("normal-mode PKCS8 file imported and signed locally using WebCrypto")
            assert all("PRIVATE KEY" not in body for body in posted)
            REPORT["checks"].append("private key absent from all auth requests")
            # An enrollment link must also work while already authenticated.
            another_setup = manage("passkey_setup", "--origin", origin, "--name", "Second device").strip().splitlines()[-1]
            page.goto(another_setup)
            expect(page.get_by_role("button", name="绑定 Passkey", exact=True)).to_be_visible()
            REPORT["checks"].append("enrollment link usable while already logged in")
            page.goto(origin + "/library")
            logout(page)
            stop(child, output)
            child, output = start(port, True)
            page.reload()
            expect(page.get_by_label("调试密码", exact=True)).to_be_visible()
            page.screenshot(path=str(OUT / "debug-login.png"))
            page.get_by_label("调试密码", exact=True).fill("654321")
            page.get_by_role("button", name="密码登录", exact=True).click()
            page.wait_for_url("**/library")
            REPORT["checks"].append("debug-mode six-digit password login")
            logout(page)
            page.get_by_role("button", name="Passkey 登录", exact=True).click()
            page.wait_for_url("**/library", timeout=20000)
            REPORT["checks"].append("debug-mode same registered Passkey login")
            logout(page)
            page.locator('input[type="file"]').set_input_files(str(private_file))
            page.wait_for_url("**/library", timeout=20000)
            REPORT["checks"].append("debug-mode private key login")
            logout(page)
            page.get_by_label("调试密码", exact=True).fill("654321")
            page.get_by_role("button", name="密码登录", exact=True).click()
            page.wait_for_url("**/library")
            stop(child, output)
            child, output = start(port, False)
            page.reload()
            page.wait_for_url("**/login")
            assert page.get_by_label("调试密码", exact=True).count() == 0
            REPORT["checks"].append("normal-mode restart rejects existing debug-password session")
            response = page.evaluate("""async () => {
                const session = await (await fetch('/api/v1/session')).json();
                const response = await fetch('/api/v1/session/login', {method:'POST',
                    headers:{'Content-Type':'application/json','X-CSRFToken':session.data.csrf_token},
                    body:JSON.stringify({password:'654321',debug:true})});
                return {status:response.status, body:await response.json()}; }""")
            assert response["status"] == 403 and response["body"]["error"]["code"] == "PASSWORD_LOGIN_DISABLED"
            REPORT["checks"].append("normal-mode HTTP password endpoint rejects debug=true bypass attempt")
            assert not errors, errors
            browser.close()
        REPORT["state"] = "passed"
    finally:
        stop(child, output)
        # Test-only private key should not remain as an unnecessary credential.
        private_file.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        main()
    except BaseException as error:
        REPORT.update(state="failed", error_type=type(error).__name__, error=str(error)[:2000])
        raise
    finally:
        (OUT / "report.json").write_text(json.dumps(REPORT, ensure_ascii=False, indent=2), encoding="utf8")
        print(json.dumps({"state": REPORT["state"], "report": str(OUT / "report.json")}), flush=True)
