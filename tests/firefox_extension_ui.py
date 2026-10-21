"""Real Firefox toolbar -> file-key login -> real API/worker -> automatic filing.

Uses a new, disposable WebDriver profile. No user's browser profile or live library.
Install Selenium into a local tool environment and build the loopback XPI first.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import uuid
import io
import threading
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output" / "firefox-extension"
sys.path.insert(0, str(OUTPUT / "python-tools"))
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.support.ui import WebDriverWait

ORIGIN = "http://127.0.0.1:18831"
XPI = OUTPUT / "linkarium-firefox-test.xpi"
FIXTURE_DATA = OUTPUT / ("acceptance-data-" + uuid.uuid4().hex)
KEY = FIXTURE_DATA / "runtime" / "fixture-key.pem"


def toolbar_click(driver):
    driver.set_context("chrome")
    widget = "linkarium-test_eirawav_me-browser-action"
    driver.find_element(By.ID, "unified-extensions-button").click()
    selector = f'#{widget} .unified-extensions-item-action-button'
    WebDriverWait(driver, 10).until(lambda d: d.find_element(By.CSS_SELECTOR, selector).is_displayed())
    driver.find_element(By.CSS_SELECTOR, selector).click()
    driver.set_context("content")


def fetch(driver, path):
    return driver.execute_async_script("""
        fetch(arguments[0], {credentials:'same-origin',cache:'no-store'}).then(r=>r.json())
          .then(arguments[arguments.length-1]);
    """, path)


def bridge_post(driver, path, body):
    return driver.execute_async_script("""
        const [path, body, done] = arguments, id = crypto.randomUUID();
        const listener = event => {
          if(event.source === window && event.origin === location.origin && event.data?.channel === 'linkarium-reply-v1' && event.data.id === id) {
            window.removeEventListener('message', listener); done(event.data.result);
          }
        };
        window.addEventListener('message', listener);
        window.postMessage({channel:'linkarium-page-v1',id,kind:'linkarium-web',method:'POST',path,body,key:id}, location.origin);
    """, path, body)


def restart_background(driver):
    driver.set_context("chrome")
    driver.execute_async_script("""
        const done=arguments[arguments.length-1], extension=WebExtensionPolicy.getByID('linkarium-test@eirawav.me').extension;
        extension.terminateBackground({ignoreDevToolsAttached:true,disableResetIdleForTest:true})
          .then(()=>extension.wakeupBackground()).then(()=>done(true));
    """)
    driver.set_context("content")


def main():
    assert XPI.is_file(), "Build the loopback extension first"
    log = (OUTPUT / "acceptance-server.log").open("w", encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if key.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}}
    env.update(PYTHONUTF8="1", NO_PROXY="localhost,127.0.0.1", MOZ_DISABLE_LAUNCHER_PROCESS="1", LINKARIUM_EXTENSION_FIXTURE_DATA=str(FIXTURE_DATA))
    server = subprocess.Popen([str(ROOT / ".venv/Scripts/python.exe"), "-X", "utf8",
        str(ROOT / "tests/run_extension_acceptance_server.py")], cwd=ROOT, env=env, stdout=log, stderr=log)
    from tests.run_ai_acceptance_provider import Handler
    class DeviceProvider(Handler):
        calls = []
        unavailable = False

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.rfile = io.BytesIO(raw)
            request = json.loads(raw)
            user = request["messages"][-1]["content"]
            try:
                data = json.loads(user)
            except ValueError:
                data = {}
            self.calls.append({"model": request["model"], "stage": "taxonomy" if data.get("task") == "plan_taxonomy" else "classification" if "websites" in data else "validation"})
            if self.unavailable:
                self.send_error(503)
            else:
                super().do_POST()

    provider = ThreadingHTTPServer(("127.0.0.1", 18833), DeviceProvider)
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    driver = None
    second = None
    report = {"checks": [], "external_ai_and_capture": "controlled protocol fixtures", "production": "not accessed"}
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(150):
            if server.poll() is not None:
                raise RuntimeError("Fixture exited; see acceptance-server.log")
            try:
                if opener.open(ORIGIN + "/health/live", timeout=1).status == 200:
                    break
            except OSError:
                time.sleep(.2)
        else:
            raise TimeoutError("Fixture did not become ready")
        options = webdriver.FirefoxOptions()
        options.binary_location = os.environ.get("FIREFOX_EXTENSION_TEST_BINARY", r"C:\Program Files\Firefox Nightly\firefox.exe")
        options.add_argument("-headless")
        # An explicit isolated profile lets the final check really quit and reopen Firefox.
        # https://developer.mozilla.org/en-US/docs/Web/WebDriver/Reference/Capabilities/firefoxOptions
        profile_directory = OUTPUT / ("browser-profile-" + uuid.uuid4().hex)
        profile_directory.mkdir()
        options.add_argument("-profile")
        options.add_argument(str(profile_directory))
        options.set_preference("xpinstall.signatures.required", False)
        options.set_preference("extensions.autoDisableScopes", 0)
        service = Service(service_args=["--allow-system-access"], log_output=str(OUTPUT / "geckodriver.log"), env=env)
        driver = webdriver.Firefox(options=options, service=service)
        driver.set_window_size(1280, 900)
        assert Path(driver.capabilities["moz:profile"]).resolve() == profile_directory.resolve()
        driver.install_addon(str(XPI), temporary=False)
        source_url = "http://127.0.0.1:18832/article?case=" + uuid.uuid4().hex
        driver.get(source_url)
        source_handle = driver.current_window_handle
        toolbar_click(driver)

        def login_tab(d):
            for handle in d.window_handles:
                d.switch_to.window(handle)
                if d.current_url.startswith(ORIGIN):
                    return handle
            return False

        bridge = WebDriverWait(driver, 20).until(login_tab)
        WebDriverWait(driver, 15).until(lambda d: d.find_elements(By.CSS_SELECTOR, 'input[type="file"]') or
            d.find_elements(By.XPATH, '//button[normalize-space()="返回登录"]'))
        existing_login = driver.find_elements(By.XPATH, '//button[normalize-space()="返回登录"]')
        if existing_login:
            existing_login[0].click()
        WebDriverWait(driver, 15).until(lambda d: d.find_elements(By.CSS_SELECTOR, 'input[type="file"]'))
        driver.find_element(By.CSS_SELECTOR, 'input[type="file"]').send_keys(str(KEY))
        WebDriverWait(driver, 20).until(lambda d: fetch(d, "/api/v1/session")["data"]["authenticated"])
        report["checks"].append("real Firefox file-key login and cookie/CSRF bridge")

        def saved(d):
            result = fetch(d, "/api/v1/bookmarks?q=" + source_url.split("case=")[1])["data"]
            return next((row for row in result if row["url"] == source_url), False)

        # Configure the executor through its real options UI, before classification.
        driver.set_context("chrome")
        status_url = driver.execute_script("return WebExtensionPolicy.getByID('linkarium-test@eirawav.me').getURL('status.html')")
        driver.set_context("content")
        driver.switch_to.new_window("tab")
        options_handle = driver.current_window_handle
        driver.get(status_url)
        driver.find_element(By.ID, "endpoint").send_keys("http://127.0.0.1:18833/v1")
        driver.find_element(By.ID, "api-key").send_keys("ui-fixture-key")
        driver.find_element(By.ID, "model").send_keys("device-fixture")
        driver.find_element(By.ID, "validate").click()
        WebDriverWait(driver, 30).until(lambda d: "验证通过" in d.find_element(By.ID, "config-result").text)
        driver.switch_to.window(bridge)
        report["checks"].append("local options verify actual JSON inference; cloud socket cannot reach model port")
        bookmark = WebDriverWait(driver, 30).until(saved)
        WebDriverWait(driver, 30).until(lambda d: saved(d)["folder_path"] == "AI 协议验收 / 待校对")
        assert bookmark["title"] == "Controlled page"
        assert bookmark["is_starred"]
        assert fetch(driver, "/api/v1/classification/proposals?bookmark_id=" + bookmark["id"] + "&status=pending")["data"] == []
        report["checks"].append("toolbar click preserves original page across login and auto-files without review")
        driver.switch_to.window(source_handle)
        toolbar_click(driver)
        time.sleep(1)
        driver.switch_to.window(bridge)
        rows = fetch(driver, "/api/v1/bookmarks?q=" + source_url.split("case=")[1])["data"]
        assert len(rows) == 1 and rows[0]["id"] == bookmark["id"]
        report["checks"].append("repeat toolbar click reuses the existing bookmark")
        driver.set_context("chrome")
        status_url = driver.execute_script("return WebExtensionPolicy.getByID('linkarium-test@eirawav.me').getURL('status.html')")
        driver.set_context("content")
        for phase in ("before", "after"):
            marker = OUTPUT / ("delay-" + phase + "-entered")
            marker.unlink(missing_ok=True)
            source_url = "http://127.0.0.1:18832/article?case=" + uuid.uuid4().hex + "&delay-" + phase + "=1"
            driver.switch_to.window(source_handle)
            driver.get(source_url)
            toolbar_click(driver)
            WebDriverWait(driver, 15).until(lambda d: marker.exists())
            driver.set_context("chrome")
            driver.execute_async_script("""
                const done = arguments[arguments.length - 1];
                const extension = WebExtensionPolicy.getByID('linkarium-test@eirawav.me').extension;
                extension.terminateBackground({ignoreDevToolsAttached:true, disableResetIdleForTest:true})
                  .then(() => {
                    if (extension.backgroundState !== 'stopped') throw new Error('Background did not stop');
                    return extension.wakeupBackground();
                  }).then(() => done(true));
            """)
            driver.set_context("content")
            driver.switch_to.window(bridge)
            WebDriverWait(driver, 30).until(saved)
            WebDriverWait(driver, 30).until(lambda d: saved(d)["folder_path"] == "AI 协议验收 / 待校对")
            assert len(fetch(driver, "/api/v1/bookmarks?q=" + source_url.split("case=")[1].split("&")[0])["data"]) == 1
            report["checks"].append("background restart " + phase + " server acceptance reuses original submission")
        driver.switch_to.new_window("tab")
        driver.get(status_url)
        WebDriverWait(driver, 15).until(lambda d: "已自动归档" in d.find_element(By.ID, "state").text)
        driver.save_screenshot(str(OUTPUT / "status-ui.png"))
        report["checks"].append("extension status UI shows the persisted final folder")

        for phase in ("before", "after"):
            # The cached complete response must survive lost acknowledgement without another inference.
            marker = OUTPUT / ("result-" + phase + "-entered")
            marker.unlink(missing_ok=True)
            (OUTPUT / ("delay-result-" + phase)).write_text("delay once")
            before_calls = sum(call["stage"] == "classification" for call in DeviceProvider.calls)
            driver.switch_to.window(source_handle)
            source_url = "http://127.0.0.1:18832/article?case=" + uuid.uuid4().hex
            driver.get(source_url)
            toolbar_click(driver)
            WebDriverWait(driver, 20).until(lambda d: marker.exists())
            driver.switch_to.window(options_handle)
            assert driver.execute_async_script("browser.storage.local.get('localAIWork').then(s=>arguments[0](typeof s.localAIWork?.response === 'string'));"), "Complete response was not durable"
            restart_background(driver)
            driver.switch_to.window(bridge)
            WebDriverWait(driver, 30).until(lambda d: saved(d) and saved(d)["folder_path"] == "AI 协议验收 / 待校对")
            driver.switch_to.window(options_handle)
            WebDriverWait(driver, 20).until(lambda d: d.execute_async_script("browser.storage.local.get('localAIWork').then(s=>arguments[0](!s.localAIWork));"))
            assert sum(call["stage"] == "classification" for call in DeviceProvider.calls) == before_calls + 1
            report["checks"].append("durable response replay " + phase + " cloud acceptance performs no second inference")

        driver.switch_to.window(options_handle)
        driver.find_element(By.ID, "pause").click()
        device_a = driver.execute_async_script("browser.storage.local.get('localAIDevice').then(s=>arguments[0](s.localAIDevice.id));")
        driver.switch_to.window(bridge)
        manual_url = "https://example.org/manual-local-" + uuid.uuid4().hex
        created = bridge_post(driver, "/bookmarks", {"url": manual_url, "snapshot_enabled": False})
        assert created["ok"], created
        manual_id = created["data"]["id"]

        second_options = webdriver.FirefoxOptions()
        second_options.binary_location = options.binary_location
        second_options.add_argument("-headless")
        second_options.set_preference("xpinstall.signatures.required", False)
        second = webdriver.Firefox(options=second_options, service=Service(service_args=["--allow-system-access"], log_output=str(OUTPUT / "geckodriver-device-b.log"), env=env))
        second.install_addon(str(XPI), temporary=True)
        second.get(ORIGIN)
        WebDriverWait(second, 20).until(lambda d: d.find_elements(By.CSS_SELECTOR, 'input[type="file"]') or d.find_elements(By.XPATH, '//button[normalize-space()="返回登录"]'))
        back = second.find_elements(By.XPATH, '//button[normalize-space()="返回登录"]')
        if back:
            back[0].click()
        WebDriverWait(second, 15).until(lambda d: d.find_elements(By.CSS_SELECTOR, 'input[type="file"]'))
        second.find_element(By.CSS_SELECTOR, 'input[type="file"]').send_keys(str(KEY))
        WebDriverWait(second, 20).until(lambda d: fetch(d, "/api/v1/session")["data"]["authenticated"])
        second.set_context("chrome")
        second_status = second.execute_script("return WebExtensionPolicy.getByID('linkarium-test@eirawav.me').getURL('status.html')")
        second.set_context("content")
        second.get(second_status)
        for name, value in (("endpoint", "http://127.0.0.1:18833/v1"), ("api-key", "ui-fixture-key"), ("model", "device-b-fixture")):
            second.find_element(By.ID, name).send_keys(value)
        second.find_element(By.ID, "validate").click()
        WebDriverWait(second, 30).until(lambda d: "验证通过" in d.find_element(By.ID, "config-result").text)
        device_b = second.execute_async_script("browser.storage.local.get('localAIDevice').then(s=>arguments[0](s.localAIDevice.id));")
        assert device_b != device_a
        time.sleep(2)
        assert not any(call["stage"] == "classification" and call["model"] == "device-b-fixture" for call in DeviceProvider.calls)
        assert fetch(driver, "/api/v1/bookmarks/" + manual_id)["data"]["folder_id"] is None
        report["checks"].append("second isolated Firefox has independent configuration and cannot claim first device's waiting task")
        # Closing the business page is independent of the executor lifecycle.
        driver.switch_to.window(bridge)
        driver.close()
        driver.switch_to.window(options_handle)
        driver.find_element(By.ID, "resume").click()
        bridge = WebDriverWait(driver, 20).until(login_tab)
        driver.switch_to.window(bridge)
        def manual_proposal(d):
            rows = fetch(d, "/api/v1/classification/proposals?bookmark_id=" + manual_id + "&status=pending")["data"]
            return rows[0] if rows else False
        proposal = WebDriverWait(driver, 30).until(manual_proposal)
        assert fetch(driver, "/api/v1/bookmarks/" + manual_id)["data"]["folder_id"] is None
        report["checks"].append("web initiation binds to first device and produces pending human review")
        report["checks"].append("closing the business page preserves work and recreates its authenticated background bridge")
        accepted = driver.execute_async_script("""
          const proposal=arguments[0],done=arguments[arguments.length-1];
          fetch('/api/v1/session').then(r=>r.json()).then(s=>fetch('/api/v1/classification/accept',{
            method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-CSRFToken':s.data.csrf_token,'Idempotency-Key':crypto.randomUUID()},
            body:JSON.stringify({items:[{id:proposal.id,revision:proposal.revision}],tree_revision:proposal.expected_tree_revision})
          })).then(r=>r.json()).then(done);
        """, proposal)
        assert accepted.get("data", {}).get("accepted") == 1, accepted
        assert fetch(driver, "/api/v1/bookmarks/" + manual_id)["data"]["folder_id"]
        report["checks"].append("manual review acceptance applies the device-generated proposal")

        DeviceProvider.unavailable = True
        failed_url = "https://example.org/interrupted-local-" + uuid.uuid4().hex
        created = bridge_post(driver, "/bookmarks", {"url": failed_url, "snapshot_enabled": False})
        assert created["ok"]
        failed_id = created["data"]["id"]
        driver.switch_to.window(options_handle)
        WebDriverWait(driver, 20).until(lambda d: "HTTP 503" in d.find_element(By.ID, "executor-state").text)
        DeviceProvider.unavailable = False
        driver.find_element(By.ID, "resume").click()
        driver.switch_to.window(bridge)
        WebDriverWait(driver, 30).until(lambda d: bool(fetch(d, "/api/v1/classification/proposals?bookmark_id=" + failed_id + "&status=pending")["data"]))
        report["checks"].append("local API interruption pauses and original device resumes the unfinished batch")
        preview = driver.execute_async_script("""
          const done=arguments[arguments.length-1], suffix=crypto.randomUUID();
          const file=new Blob(['<DL><A HREF="https://example.org/import-local-'+suffix+'">Imported local fixture</A></DL>'],{type:'text/html'});
          const form=new FormData();form.append('file',file,'bookmarks.html');
          fetch('/api/v1/session').then(r=>r.json()).then(s=>fetch('/api/v1/imports',{method:'POST',credentials:'same-origin',
            headers:{'X-CSRFToken':s.data.csrf_token,'Idempotency-Key':suffix},body:form})).then(r=>r.json()).then(done);
        """)
        batch = preview["data"]
        executed = bridge_post(driver, "/imports/" + batch["id"] + "/execute", {"confirmation_token": batch["execution_plan"]["confirmation_token"]})
        assert executed["ok"], executed
        WebDriverWait(driver, 40).until(lambda d: fetch(d, "/api/v1/imports/" + batch["id"])["data"]["stage"] == "review")
        imported = fetch(driver, "/api/v1/classification/proposals?import_id=" + batch["id"])["data"]
        assert len(imported) == 1 and imported[0]["status"] == "pending" and not imported[0]["manual"]
        assert fetch(driver, "/api/v1/bookmarks/" + imported[0]["bookmark_id"])["data"]["folder_id"] is None
        report["checks"].append("HTML import is bound through the browser bridge, classified locally, and awaits human review")
        driver.switch_to.window(options_handle)
        driver.find_element(By.ID, "pause").click()
        driver.switch_to.window(bridge)
        restart_bookmark = bridge_post(driver, "/bookmarks", {"url": "https://example.org/browser-restart-" + uuid.uuid4().hex, "snapshot_enabled": False})
        assert restart_bookmark["ok"]
        restart_id = restart_bookmark["data"]["id"]
        driver.quit()
        driver = webdriver.Firefox(options=options, service=Service(service_args=["--allow-system-access"], log_output=str(OUTPUT / "geckodriver-restart.log"), env=env))
        driver.set_context("chrome")
        installed = driver.execute_async_script("""
          const done=arguments[arguments.length-1];
          const {AddonManager}=ChromeUtils.importESModule('resource://gre/modules/AddonManager.sys.mjs');
          AddonManager.getAddonByID('linkarium-test@eirawav.me').then(a=>done({version:a.version,active:a.isActive,temporary:!!a.temporarilyInstalled}));
        """)
        assert installed == {"version": "0.2.0", "active": True, "temporary": False}, installed
        driver.set_context("content")
        driver.get(status_url)
        restored = driver.execute_async_script("browser.storage.local.get(['localAIDevice','localAIConfig']).then(s=>arguments[0]({id:s.localAIDevice.id,verified:s.localAIConfig.verified}));")
        assert restored == {"id": device_a, "verified": True}, restored
        report["checks"].append("permanent 0.2.0 isolated addon, device identity and verified config survive a complete Firefox exit")
        driver.find_element(By.ID, "resume").click()
        bridge = WebDriverWait(driver, 20).until(login_tab)
        driver.switch_to.window(bridge)
        if not fetch(driver, "/api/v1/session")["data"]["authenticated"]:
            back = driver.find_elements(By.XPATH, '//button[normalize-space()="返回登录"]')
            if back:
                back[0].click()
            WebDriverWait(driver, 15).until(lambda d: d.find_elements(By.CSS_SELECTOR, 'input[type="file"]'))
            driver.find_element(By.CSS_SELECTOR, 'input[type="file"]').send_keys(str(KEY))
        WebDriverWait(driver, 40).until(lambda d: bool(fetch(d, "/api/v1/classification/proposals?bookmark_id=" + restart_id + "&status=pending")["data"]))
        report["checks"].append("original device resumes its unfinished classification after Firefox reopens")
        assert sum(call["stage"] == "taxonomy" for call in DeviceProvider.calls) == 1
        report["model_calls"] = {stage: sum(call["stage"] == stage for call in DeviceProvider.calls) for stage in ("validation", "taxonomy", "classification")}
        report["browser"] = driver.capabilities["browserVersion"]
        report["state"] = "passed"
        print(json.dumps(report, ensure_ascii=False))
    except Exception:
        report["state"] = "failed"
        if driver:
            try:
                driver.set_context("chrome")
                report["extension_errors"] = driver.execute_script("return Services.console.getMessageArray().map(m => m.message).filter(m => m.includes('moz-extension:')).slice(-10)")
                status_url = driver.execute_script("return WebExtensionPolicy.getByID('linkarium-test@eirawav.me').getURL('status.html')")
                driver.set_context("content")
                driver.switch_to.new_window("tab")
                driver.get(status_url)
                time.sleep(1)
                report["last_operation"] = driver.execute_async_script("const done=arguments[arguments.length-1]; browser.storage.local.get(null).then(s=>done(s['linkarium:'+s.lastOperation]));")
                report["permissions"] = driver.execute_async_script("browser.permissions.getAll().then(arguments[arguments.length-1]);")
                report["extension_status"] = driver.find_element(By.TAG_NAME, "body").text
            except Exception:
                driver.set_context("content")
            driver.save_screenshot(str(OUTPUT / "failed-ui.png"))
            report["visible_buttons"] = [element.text for element in driver.find_elements(By.TAG_NAME, "button")]
            report["page_title"] = driver.title
            print(json.dumps(report, ensure_ascii=False))
        raise
    finally:
        if second:
            second.quit()
        if driver:
            driver.quit()
        provider.shutdown()
        provider.server_close()
        server.terminate()
        server.wait(timeout=15)
        log.close()
        (OUTPUT / "acceptance-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
