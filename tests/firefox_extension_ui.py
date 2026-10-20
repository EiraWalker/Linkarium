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

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output" / "firefox-extension"
sys.path.insert(0, str(OUTPUT / "python-tools"))
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.support.ui import WebDriverWait

ORIGIN = "http://127.0.0.1:18831"
XPI = OUTPUT / "linkarium-firefox-test.xpi"
KEY = OUTPUT / "acceptance-data" / "runtime" / "fixture-key.pem"


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


def main():
    assert XPI.is_file(), "Build the loopback extension first"
    log = (OUTPUT / "acceptance-server.log").open("w", encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if key.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}}
    env.update(PYTHONUTF8="1", NO_PROXY="localhost,127.0.0.1")
    server = subprocess.Popen([str(ROOT / ".venv/Scripts/python.exe"), "-X", "utf8",
        str(ROOT / "tests/run_extension_acceptance_server.py")], cwd=ROOT, env=env, stdout=log, stderr=log)
    driver = None
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
        options.binary_location = os.environ.get("FIREFOX_EXTENSION_TEST_BINARY", r"C:\Program Files\Mozilla Firefox\firefox.exe")
        options.add_argument("-headless")
        options.set_preference("xpinstall.signatures.required", False)
        options.set_preference("extensions.autoDisableScopes", 0)
        service = Service(service_args=["--allow-system-access"], log_output=str(OUTPUT / "geckodriver.log"), env=env)
        driver = webdriver.Firefox(options=options, service=service)
        driver.set_window_size(1280, 900)
        driver.install_addon(str(XPI), temporary=True)
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
        if driver:
            driver.quit()
        server.terminate()
        server.wait(timeout=15)
        log.close()
        (OUTPUT / "acceptance-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
