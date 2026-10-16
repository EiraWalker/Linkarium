"""Real browser/HTTP workflow acceptance against run_import_workflow_server.py."""
import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(ROOT / "tools" / "browsers")
OUTPUT = ROOT / "output" / "playwright"
OUTPUT.mkdir(parents=True, exist_ok=True)


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("http://127.0.0.1:18767")
        page.locator('input[type="password"]').fill((ROOT / "output/import-workflow-data/runtime/acceptance-password").read_text())
        page.get_by_role("button", name="进入收藏库").click()
        expect(page.get_by_role("heading", name="全部网站", exact=True)).to_be_visible()
        page.evaluate("""async () => {
          const token=(await (await fetch('/api/v1/session')).json()).data.csrf_token;
          const settings=(await (await fetch('/api/v1/settings')).json()).data;
          const r=await fetch('/api/v1/settings',{method:'PATCH',headers:{'Content-Type':'application/json','X-CSRFToken':token},body:JSON.stringify({revision:settings.revision,active_ai_profile_id:null})});
          if (!r.ok) throw new Error('Could not reset isolated test AI settings');
        }""")
        assert page.get_by_role("link", name="分类校对", exact=True).count() == 0
        page.get_by_role("link", name="导入与备份", exact=True).first.click()
        stamp = str(__import__('time').time_ns())
        html = f'<DL><H3>Source Folder</H3><DL><A HREF="https://example.org/{stamp}">Success Site</A><A HREF="https://example.org/failed-{stamp}">Failed Site</A><A HREF="https://example.org/{stamp}">Duplicate</A><A HREF="file:///private">Invalid</A></DL></DL>'
        page.get_by_label("选择浏览器 HTML 文件").set_input_files({"name": "firefox.html", "mimeType": "text/html", "buffer": html.encode()})
        progress = page.get_by_role("progressbar", name="导入工作流程进度")
        expect(progress).to_be_visible(timeout=10000)
        page.screenshot(path=str(OUTPUT / "import-workflow-progress.png"), full_page=True)
        page.get_by_role("link", name="设置", exact=True).first.click()
        page.wait_for_function("async () => (await (await fetch('/api/v1/imports')).json()).data.some(b => b.review_ready)", timeout=20000)
        assert page.get_by_role("dialog", name="分类校对", exact=True).count() == 0
        page.get_by_role("link", name="导入与备份", exact=True).first.click()
        review = page.get_by_role("dialog", name="分类校对", exact=True)
        expect(review).to_be_visible(timeout=10000)
        expect(progress).not_to_be_visible()
        expect(review.get_by_text("抓取失败", exact=True)).to_be_visible()
        assert review.get_by_text("未进行 AI 分类", exact=True).count() == 2
        page.screenshot(path=str(OUTPUT / "import-workflow-review-desktop.png"), full_page=True)
        review.get_by_role("button", name="修改目标").first.click()
        edit = page.get_by_role("dialog", name="修改分类目标", exact=True)
        edit.get_by_label("新建路径", exact=True).check()
        edit.get_by_label("新建文件夹路径").fill(f"Manual {stamp} / Review")
        edit.get_by_role("button", name="保存建议", exact=True).click()
        expect(edit).not_to_be_visible()
        review.get_by_label("选择建议 Success Site", exact=True).check()
        review.get_by_role("button", name="确认选中", exact=True).click()
        expect(review.get_by_label("选择建议 Success Site", exact=True)).not_to_be_visible()
        review.get_by_role("button", name="关闭", exact=True).click()
        page.get_by_role("link", name="设置", exact=True).first.click()
        page.get_by_role("link", name="导入与备份", exact=True).first.click()
        page.wait_for_timeout(2500)
        assert page.get_by_role("dialog", name="分类校对", exact=True).count() == 0
        page.get_by_role("button", name="查看建议", exact=True).click()
        expect(review).to_be_visible()
        page.set_viewport_size({"width": 390, "height": 844})
        page.screenshot(path=str(OUTPUT / "import-workflow-review-mobile.png"), full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        review.get_by_role("button", name="关闭", exact=True).click()
        page.set_viewport_size({"width": 1440, "height": 960})
        # AI uses a real HTTP protocol fixture; never a paid or personal endpoint.
        profile = page.evaluate("""async () => {
          const token=(await (await fetch('/api/v1/session')).json()).data.csrf_token;
          const r=await fetch('/api/v1/ai-profiles',{method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':token},body:JSON.stringify({name:'Workflow protocol fixture',base_url:'http://127.0.0.1:18808/v1',model:'fixture',api_key:'ui-fixture-key'})});
          const p=(await r.json()).data;
          const t=await fetch(`/api/v1/ai-profiles/${p.id}/test`,{method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':token},body:'{}'});
          return (await t.json()).data;
        }""")
        page.wait_for_function("async id => (await (await fetch('/api/v1/jobs/'+id)).json()).data.state === 'succeeded'", arg=profile["job_id"], timeout=10000)
        html = f'<DL><A HREF="https://example.org/ai-{stamp}">AI Success Site</A></DL>'
        page.get_by_label("选择浏览器 HTML 文件").set_input_files({"name": "chrome.html", "mimeType": "text/html", "buffer": html.encode()})
        expect(review).to_be_visible(timeout=20000)
        expect(review.get_by_text("AI Success Site", exact=True)).to_be_visible()
        assert review.get_by_text("未进行 AI 分类", exact=True).count() == 0
        review.get_by_role("button", name="关闭", exact=True).click()
        # The same modal is reachable from a site's detail.
        page.get_by_role("link", name="网站库", exact=True).first.click()
        page.get_by_role("button", name="查看 AI Success Site", exact=True).first.click()
        page.get_by_role("button", name="查看建议", exact=True).click()
        expect(review.get_by_text("AI Success Site", exact=True)).to_be_visible()
        assert not errors, errors
        result={"immediate_import":True,"progress":True,"deferred_review":True,"no_repeat_on_close":True,"manual_accept":True,"failure_label":True,"ai_http_flow":True,"shared_detail_modal":True,"mobile_overflow":False,"page_errors":errors}
        (OUTPUT / "import-workflow-report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result))
        browser.close()


if __name__ == "__main__":
    main()
