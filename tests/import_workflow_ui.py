"""Real browser/HTTP workflow acceptance against run_import_workflow_server.py."""
import json
import os
from pathlib import Path
import sqlite3
import time
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(ROOT / "tools" / "browsers"))
DATA_ROOT = Path(os.environ.get('BOOKMARK_ACCEPTANCE_DATA_ROOT', ROOT / 'output' / 'import-workflow-data')).resolve()
OUTPUT = Path(os.environ.get('BOOKMARK_ACCEPTANCE_OUTPUT', ROOT / 'output' / 'playwright')).resolve()
BASE_URL = f"http://127.0.0.1:{int(os.environ.get('BOOKMARK_ACCEPTANCE_PORT', '18767'))}"
if not DATA_ROOT.is_relative_to((ROOT / 'output').resolve()) or not OUTPUT.is_relative_to((ROOT / 'output').resolve()):
    raise SystemExit('Workflow acceptance requires isolated data and artifacts below this checkout/output')
OUTPUT.mkdir(parents=True, exist_ok=True)


def database_snapshot(batch_id=None):
    with sqlite3.connect(f'file:{(DATA_ROOT / "state.sqlite3").as_posix()}?mode=ro', uri=True) as state:
        generation = state.execute('SELECT active_generation FROM runtime_runtimestate WHERE id=1').fetchone()[0]
    path = DATA_ROOT / 'generations' / generation / 'library.sqlite3'
    with sqlite3.connect(f'file:{path.as_posix()}?mode=ro', uri=True) as connection:
        connection.row_factory = sqlite3.Row
        counts = {name: connection.execute(f'SELECT COUNT(*) FROM library_{name}').fetchone()[0]
                  for name in ('bookmark', 'job', 'snapshotversion', 'classificationproposal')}
        if batch_id:
            identity = batch_id.replace('-', '')
            counts['batch'] = dict(connection.execute('SELECT state,stage,capture_budget,ai_budget,ai_requests_used,length(execution_authorization) authorization_length FROM library_importbatch WHERE id=?', (identity,)).fetchone())
            counts['capture_jobs'] = [dict(row) for row in connection.execute('''SELECT j.variant,j.state,j.error_code
                FROM library_job j JOIN library_jobgroupmember m ON m.job_id=j.id
                JOIN library_importbatch b ON b.capture_group_id=m.group_id WHERE b.id=? ORDER BY j.created_at''', (identity,))]
        return counts


def api(page, route, method='GET', body=None):
    return page.evaluate('''async ({route,method,body}) => {
      const csrf=(await (await fetch('/api/v1/session')).json()).data.csrf_token;
      const response=await fetch('/api/v1/'+route,{method,headers:{'Content-Type':'application/json','X-CSRFToken':csrf},body:method==='GET'?undefined:JSON.stringify(body)});
      const result=await response.json(); if(!response.ok)throw new Error(JSON.stringify(result));return result.data;
    }''', {'route': route, 'method': method, 'body': body})


def preview_and_confirm(page, html, filename, entries, captures, ai_requests):
    before = database_snapshot()
    with page.expect_response(lambda response: response.url.endswith('/api/v1/imports/preview') and response.request.method == 'POST') as observed:
        page.get_by_label('选择浏览器 HTML 文件').set_input_files({'name': filename, 'mimeType': 'text/html', 'buffer': html.encode()})
    response = observed.value
    assert response.status == 201, response.text()
    batch = response.json()['data']
    plan = batch['execution_plan']
    assert (plan['entries'], plan['captures'], plan['ai_requests']) == (entries, captures, ai_requests), plan
    expect(page.get_by_text(f'确认后最多创建 {captures} 个双端抓取任务，最多进行 {ai_requests} 次 AI 请求（含目录规划）。', exact=True)).to_be_visible()
    expect(page.get_by_role('progressbar', name='导入工作流程进度')).not_to_be_visible()
    after = database_snapshot(batch['id'])
    assert after['bookmark'] == before['bookmark'] and after['job'] == before['job'], (before, after)
    assert after['batch']['state'] == 'previewed' and after['batch']['authorization_length'] == 0, after
    assert after['capture_jobs'] == []
    page.screenshot(path=str(OUTPUT / f'import-preview-{entries}.png'), full_page=True)
    with page.expect_response(lambda response: response.url.endswith(f"/api/v1/imports/{batch['id']}/execute") and response.request.method == 'POST') as execution:
        page.get_by_role('button', name=f'确认导入 {entries} 个网站', exact=True).click()
    assert execution.value.status == 202, execution.value.text()
    return batch['id'], {key: plan[key] for key in ('entries', 'captures', 'ai_requests')}


def main():
    server = json.loads((DATA_ROOT / 'runtime' / 'acceptance-server.json').read_text(encoding='utf-8'))
    assert Path(server['data_root']).resolve() == DATA_ROOT and server['fixture_capture'] is True
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(BASE_URL)
        page.get_by_role('button', name='返回登录', exact=True).click()
        page.get_by_label('调试密码', exact=True).fill((DATA_ROOT / 'runtime' / 'acceptance-password').read_text())
        page.get_by_role('button', name='密码登录', exact=True).click()
        expect(page.get_by_role("heading", name="全部网站", exact=True)).to_be_visible()
        page.evaluate("""async () => {
          const token=(await (await fetch('/api/v1/session')).json()).data.csrf_token;
          const settings=(await (await fetch('/api/v1/settings')).json()).data;
          const r=await fetch('/api/v1/settings',{method:'PATCH',headers:{'Content-Type':'application/json','X-CSRFToken':token},body:JSON.stringify({revision:settings.revision,active_ai_profile_id:null})});
          if (!r.ok) throw new Error('Could not reset isolated test AI settings');
        }""")
        assert page.get_by_role("link", name="分类校对", exact=True).count() == 0
        page.get_by_role("link", name="导入与备份", exact=True).first.click()
        stamp = str(time.time_ns())
        html = f'<DL><H3>Source Folder</H3><DL><A HREF="https://example.org/{stamp}">Success Site</A><A HREF="https://example.org/failed-{stamp}">Failed Site</A><A HREF="https://example.org/{stamp}">Duplicate</A><A HREF="file:///private">Invalid</A></DL></DL>'
        manual_batch, manual_plan = preview_and_confirm(page, html, 'firefox.html', 2, 4, 2)
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
        manual_evidence = database_snapshot(manual_batch)
        assert manual_evidence['batch']['capture_budget'] == 4 and manual_evidence['batch']['ai_budget'] == 2
        assert manual_evidence['batch']['ai_requests_used'] == 0
        assert manual_evidence['batch']['authorization_length'] > 0
        assert len(manual_evidence['capture_jobs']) == 4, manual_evidence
        assert sum(job['state'] == 'succeeded' for job in manual_evidence['capture_jobs']) == 2, manual_evidence
        assert sum(job['state'] == 'failed' for job in manual_evidence['capture_jobs']) == 2, manual_evidence
        page.screenshot(path=str(OUTPUT / "import-workflow-review-desktop.png"), full_page=True)
        review.locator('article').filter(has=page.get_by_role('heading', name='Success Site', exact=True)).get_by_role('button', name='修改目标', exact=True).click()
        edit = page.get_by_role("dialog", name="修改分类目标", exact=True)
        edit.get_by_label("新建路径", exact=True).check()
        edit.get_by_label("新建文件夹路径").fill(f"Manual {stamp} / Review")
        edit.get_by_role("button", name="保存建议", exact=True).click()
        expect(edit).not_to_be_visible()
        review.get_by_label("选择建议 Success Site", exact=True).check()
        with page.expect_response(lambda response: response.url.endswith('/api/v1/classification/accept') and response.request.method == 'POST') as accepted_response:
            review.get_by_role("button", name="确认选中", exact=True).click()
        assert accepted_response.value.ok, accepted_response.value.text()
        expect(review.get_by_label("选择建议 Success Site", exact=True)).not_to_be_visible()
        bookmarks = api(page, 'bookmarks')
        accepted = next(row for row in bookmarks if row['url'] == f'https://example.org/{stamp}')
        assert accepted['folder_path'] == f'Manual {stamp} / Review', accepted
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
        ai_batch, ai_plan = preview_and_confirm(page, html, 'chrome.html', 1, 2, 2)
        expect(review).to_be_visible(timeout=20000)
        expect(review.get_by_text("AI Success Site", exact=True)).to_be_visible()
        assert review.get_by_text("未进行 AI 分类", exact=True).count() == 0
        ai_evidence = database_snapshot(ai_batch)
        assert ai_evidence['batch']['ai_requests_used'] == 2 and ai_evidence['batch']['ai_budget'] == 2, ai_evidence
        assert len(ai_evidence['capture_jobs']) == 2 and all(job['state'] == 'succeeded' for job in ai_evidence['capture_jobs']), ai_evidence
        assert api(page, f'imports/{ai_batch}')['ai_status'] == 'succeeded'
        ai_bookmark = next(row for row in api(page, 'bookmarks') if row['url'] == f'https://example.org/ai-{stamp}')
        assert not ai_bookmark['folder_id'], ai_bookmark
        review.get_by_role("button", name="关闭", exact=True).click()
        # The same modal is reachable from a site's detail.
        page.get_by_role("link", name="网站库", exact=True).first.click()
        page.get_by_role("button", name="查看 AI Success Site", exact=True).first.click()
        page.get_by_role("button", name="查看建议", exact=True).click()
        expect(review.get_by_text("AI Success Site", exact=True)).to_be_visible()
        page.reload()
        expect(page.get_by_role('heading', name='全部网站', exact=True)).to_be_visible()
        restored = next(row for row in api(page, 'bookmarks') if row['id'] == accepted['id'])
        assert restored['folder_path'] == accepted['folder_path'], restored
        assert not errors, errors
        result={"preview_requires_confirmation":True,"exact_workload_display":True,"preview_creates_no_jobs_or_bookmarks":True,"progress":True,"deferred_review":True,"no_repeat_on_close":True,"manual_accept_persisted_after_reload":True,"failure_label":True,"ai_http_flow":True,"ai_outgoing_requests_persisted":True,"ai_does_not_move_before_review":True,"shared_detail_modal":True,"mobile_overflow":False,"page_errors":errors,"isolated_server":server,"manual_plan":manual_plan,"ai_plan":ai_plan,"manual_database":manual_evidence,"ai_database":ai_evidence}
        (OUTPUT / "import-workflow-report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result))
        browser.close()


if __name__ == "__main__":
    main()
