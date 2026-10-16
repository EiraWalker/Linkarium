"""Classification progress rendering against controlled HTTP group responses."""
import json
import os
from pathlib import Path
import uuid
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(ROOT / "tools/browsers")


def main():
    identity = str(uuid.uuid4())
    group = {"id": identity, "group_id": identity, "kind": "classification", "state": "queued",
             "requested_count": 482, "website_total": 482, "website_completed": 0,
             "total": 49, "completed": 0, "queued_sites": 482, "running_sites": 0,
             "retrying_sites": 0, "succeeded_sites": 0, "failed_sites": 0,
             "counts": {"queued": 49, "running": 0, "retry_wait": 0, "failed": 0},
             "jobs": [{"id": str(uuid.uuid4()), "website_count": 10 if n < 48 else 2,
                       "state": "queued", "error_code": ""} for n in range(49)]}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 933, "height": 828})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.route("**/api/v1/classification/batches", lambda route: route.fulfill(status=202, json={"data": group}))
        page.route(f"**/api/v1/job-groups/{identity}*", lambda route: route.fulfill(json={"data": group}))
        page.goto("http://127.0.0.1:18767/library")
        fixture_root = Path(os.environ.get("BOOKMARK_ACCEPTANCE_DATA_ROOT", ROOT / "output/import-workflow-data"))
        page.locator('input[type="password"]').fill((fixture_root / "runtime/acceptance-password").read_text())
        page.get_by_role("button", name="进入收藏库").click()
        page.get_by_role("button", name="AI分类", exact=True).click()
        dialog = page.get_by_role("dialog", name="后台任务", exact=True)
        expect(dialog.get_by_text("等待执行", exact=True).first).to_be_visible()
        expect(dialog.get_by_text("网站已处理 0 / 482", exact=False)).to_be_visible()
        expect(dialog.get_by_text("已完成 0 / 49 批次", exact=False)).to_be_visible()
        expect(dialog.get_by_text("正在排队，等待后台工作进程处理；较早提交的任务会先执行。", exact=True)).to_be_visible()
        expect(dialog.get_by_text("AI 分类（10 个网站）", exact=True)).to_have_count(48)
        expect(dialog.get_by_text("AI 分类（2 个网站）", exact=True)).to_have_count(1)
        group.update(state="running", completed=1, website_completed=10, succeeded_sites=10,
                     queued_sites=442, running_sites=10, retrying_sites=20)
        group["counts"].update(queued=45, running=1, retry_wait=2, succeeded=1)
        group["jobs"][0].update(state="succeeded")
        group["jobs"][1].update(state="running")
        for row in group["jobs"][2:4]:
            row.update(state="retry_wait", error_code="AI_TIMEOUT")
        group["jobs"][3]["error_code"] = "LEASE_EXPIRED"
        group["jobs"][4].update(state="failed", error_code="AI_INVALID_RESULT")
        expect(dialog.get_by_text("网站已处理 10 / 482", exact=False)).to_be_visible(timeout=10000)
        expect(dialog.get_by_text("部分 AI 请求失败，正在等待自动重试。具体原因见下方任务状态。", exact=True)).to_be_visible()
        rows = dialog.locator("tbody tr")
        for index, error in [(2, "AI 服务响应超时"), (3, "任务执行中断"), (4, "AI 服务返回的分类结果无效")]:
            expect(rows.nth(index).locator("td").nth(2)).to_have_text(error)
            expect(rows.nth(index).locator(".status-bad")).to_be_visible()
            expect(rows.nth(index).get_by_role("button", name="重试", exact=True)).to_be_enabled()
            expect(rows.nth(index).get_by_role("button", name="取消", exact=True)).to_have_count(0)
        expect(rows.nth(1).get_by_role("button", name="取消", exact=True)).to_be_enabled()
        retry_posts = []
        def retry(route):
            assert route.request.method == "POST"
            identity = route.request.url.split("/")[-2]
            retry_posts.append(identity)
            job = next(row for row in group["jobs"] if row["id"] == identity)
            job.update(state="queued", error_code="")
            route.fulfill(status=202, json={"data": {"job_id": identity, "state": "queued"}})
        page.route("**/api/v1/jobs/*/retry", retry)
        for index in [2, 4]:
            identity = group["jobs"][index]["id"]
            rows.nth(index).get_by_role("button", name="重试", exact=True).click()
            expect(rows.nth(index).locator("td").nth(2)).to_have_text("等待执行")
            expect(rows.nth(index).get_by_role("button", name="取消", exact=True)).to_be_enabled()
            assert retry_posts[-1] == identity
        output = ROOT / "output/playwright"
        page.screenshot(path=str(output / "classification-progress.png"), full_page=True)
        assert not errors, errors
        (output / "classification-progress-report.json").write_text(json.dumps({
            "website_and_batch_counts": True, "queue_explanation": True,
            "per_batch_website_count": True, "retry_feedback": True, "page_errors": errors,
            "error_text_only": True, "retry_wait_and_failed_use_retry": True,
            "retry_posts_correct_job": True, "normal_running_keeps_cancel": True
        }, indent=2), encoding="utf-8")
        browser.close()
        print("Classification progress browser acceptance passed.")


if __name__ == "__main__":
    main()
