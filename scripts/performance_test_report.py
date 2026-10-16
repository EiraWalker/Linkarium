"""Render measured test results as a standalone, offline HTML report.

Usage: python scripts/performance_test_report.py RUN_DIR
The input is RUN_DIR/summary.json; this module never runs tests or changes results.
"""
from __future__ import annotations

import argparse
from collections import Counter
import html
import json
import math
import os
from pathlib import Path
import re
from urllib.parse import quote, urlsplit


LABELS = {
    "passed": "通过", "pass": "通过", "success": "通过", "complete": "完成",
    "failed": "失败", "failure": "失败", "error": "错误", "errors": "错误",
    "skipped": "跳过", "skip": "跳过", "incomplete": "未完成", "partial": "部分完成",
    "not_run": "未执行", "unknown": "未知", "running": "运行中",
}
GOOD = {"passed", "pass", "success", "complete"}
BAD = {"failed", "failure", "error", "errors"}
SCENARIOS = {
    "library": "网站库", "open_detail": "打开详情", "switch_device": "切换快照设备",
    "html_archive": "完整 HTML 抽样", "scroll": "滚动", "pagination": "下一页",
    "pagination_return": "返回上一页", "folder": "打开文件夹", "folder_return": "返回网站库",
    "settings": "打开设置", "settings_return": "从设置返回", "device_budget": "设备测试预算",
}


def clean(value):
    """Keep tracebacks useful while excluding common credentials and URL queries."""
    text = str(value)
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text)
    text = re.sub(r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|authorization|cookie)['\"]?\s*[=:]\s*['\"]?)[^\s,;]+",
                  r"\1[REDACTED]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[REDACTED]", text)
    text = re.sub(r"https?://[^\s<>\"']+", lambda m: _clean_url(m.group()), text)
    return text


def _clean_url(value):
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if ":" in host:
            host = "[" + host + "]"
        if parsed.port:
            host += ":" + str(parsed.port)
        return parsed.scheme + "://" + host + parsed.path + ("?[REDACTED]" if parsed.query else "")
    except ValueError:
        return "[REDACTED URL]"


def esc(value):
    return html.escape(clean(value), quote=True)


def ms(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return f"{value:,.1f}"
    return "—"


def status(value):
    value = str(value or "unknown").lower()
    tone = "good" if value in GOOD else "bad" if value in BAD else "warn"
    return '<span class="status ' + tone + '">' + esc(LABELS.get(value, value)) + "</span>"


def local_link(run_dir, value, label):
    if not value:
        return ""
    value = str(value)
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", value) and not re.match(r"^[a-zA-Z]:[\\/]", value):
        return ""
    if value.startswith(("//", "\\\\")):
        return ""
    target = Path(value)
    target = target if target.is_absolute() else run_dir / target
    href = quote(os.path.relpath(target, run_dir).replace("\\", "/"), safe="/.-_~")
    return '<a href="' + html.escape(href, quote=True) + '">' + esc(label) + "</a>"


def links(run_dir, row):
    return " · ".join(filter(None, [local_link(run_dir, row.get("result_file"), "原始结果"),
                                    local_link(run_dir, row.get("log_file"), "运行日志")]))


def details(value):
    if not value:
        return ""
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, indent=2)
    return '<details><summary>详情</summary><pre>' + esc(value) + "</pre></details>"


def test_row(name, result, duration, more="", group=""):
    category = "passed" if str(result).lower() in GOOD else "failed" if str(result).lower() in BAD else str(result or "unknown").lower()
    return '<tr data-case data-status="' + esc(category) + '" data-search="' + esc(str(group) + " " + str(name)) + '"><td>' + esc(name) + \
        ('<small>' + esc(group) + "</small>" if group else "") + "</td><td>" + status(result) + \
        '</td><td class="number">' + ms(duration) + "</td><td>" + more + "</td></tr>"


def load_browser(run_dir, source):
    if isinstance(source, dict) and any(key in source for key in ("runs", "scenarios", "html_samples")):
        return source
    reference = source.get("file") if isinstance(source, dict) else source
    if reference:
        path = Path(reference)
        path = path if path.is_absolute() else run_dir / path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}
    return {}


def render_browser(run_dir, source):
    browser = load_browser(run_dir, source)
    rows = []
    measurements = []
    for key in ("runs", "scenarios", "html_samples"):
        values = browser.get(key, [])
        if isinstance(values, list):
            measurements.extend(value for value in values if isinstance(value, dict))
    counts = Counter(str(row.get("status", "unknown")) for row in measurements)
    for row in measurements:
        preview = row.get("preview", {})
        preview = preview if isinstance(preview, dict) else {}
        scenario = SCENARIOS.get(row.get("scenario"), row.get("scenario", "浏览器场景"))
        device = {"desktop": "电脑", "mobile": "手机"}.get(row.get("device"), row.get("device", "—"))
        cache = {"cold": "冷缓存", "warm": "暖缓存", "fixture": "内存夹具"}.get(row.get("cache"), row.get("cache", "—"))
        target = row.get("target_variant", row.get("snapshot_variant"))
        if target:
            scenario += " / " + {"desktop": "桌面快照", "mobile": "手机快照"}.get(target, str(target))
        error = {key: row[key] for key in ("error", "error_code", "reason", "stage", "http_status", "display_error") if row.get(key) is not None}
        count = " / ".join(str(preview.get(key, "—")) for key in ("success", "available", "errors"))
        more = details(error)
        if row.get("scenario") == "html_archive":
            more += details({key: row[key] for key in ("artifact_bytes", "head_status", "head_request_elapsed_ms", "iframe_load_ms", "outer_paint_opportunity_ms", "timing_definition") if key in row})
        if row.get("network"):
            more += details({"network": row["network"]})
        category = "passed" if row.get("status") in GOOD else "failed" if row.get("status") in BAD else row.get("status", "unknown")
        duration = row.get("duration_ms", row.get("elapsed_ms"))
        rows.append('<tr data-case data-status="' + esc(category) + '" data-search="' + esc(device + " " + scenario) + '"><td>' + esc(scenario) + '<small>' + esc(device + " · " + cache) + '</small></td><td>' + status(row.get("status")) + '</td>' +
                    ''.join('<td class="number">' + ms(value) + '</td>' for value in
                            (preview.get("first_ready_ms"), preview.get("first_paint_opportunity_ms"),
                             preview.get("all_ready_ms"), preview.get("all_paint_opportunity_ms"), duration)) +
                    '<td>' + esc(count) + '</td><td>' + more + '</td></tr>')
    if not rows:
        return '<p class="empty">没有可读取的浏览器场景结果，未将其记为通过。</p>'
    count_text = " · ".join(LABELS.get(key, key) + " " + str(count) for key, count in sorted(counts.items()))
    return '<p class="subtle">' + esc(count_text) + '</p><p class="subtle">时间单位均为 ms。显示时点为图片就绪及浏览器两次 requestAnimationFrame 绘制机会；不代表硬件屏幕呈现。完整 HTML 抽样的 iframe 加载时间见详情。缺失值显示 —。</p>' + \
        '<div class="table-wrap"><table><thead><tr><th>场景 / 设备 / 缓存</th><th>状态</th><th>首图就绪</th><th>首图绘制机会</th><th>全部就绪</th><th>全部绘制机会</th><th>场景总耗时</th><th>就绪 / 可用 / 错误</th><th>详情</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>'


STYLE = """
:root{color-scheme:light;--ink:#182436;--muted:#64748b;--line:#dce3ed;--blue:#1765e8;--bg:#f3f5f8}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.65 system-ui,-apple-system,'Segoe UI','Microsoft YaHei',sans-serif}main{max-width:1480px;margin:auto;padding:32px 24px 60px}h1{font-size:28px;line-height:1.3;margin:0 0 12px}h2{font-size:19px;margin:0 0 12px}a{color:var(--blue);text-decoration:none}a:hover{text-decoration:underline}header{margin-bottom:24px}.meta,.subtle,small{color:var(--muted)}.meta{margin-top:10px}small{display:block;font-size:12px}.cards{display:grid;grid-template-columns:repeat(5,minmax(120px,1fr));gap:12px;margin:24px 0}.card,section{background:white;border:1px solid var(--line);border-radius:7px;box-shadow:0 1px 3px #172c4b05}.card{padding:16px}.card strong{font-size:26px;display:block}.card span{color:var(--muted)}section{padding:20px;margin-top:18px}.status{display:inline-block;border-radius:4px;padding:2px 8px;white-space:nowrap;font-size:12px}.good{color:#176340;background:#e9f6ed}.bad{color:#b42324;background:#fff0f0}.warn{color:#9a6209;background:#fff5df}.toolbar{position:sticky;top:0;z-index:1;background:#f3f5f8f5;backdrop-filter:blur(8px);border:1px solid var(--line);border-radius:6px;display:flex;gap:12px;align-items:center;padding:12px;flex-wrap:wrap}.toolbar input{flex:1;min-width:180px}.toolbar input,.toolbar select{font:inherit;color:inherit;background:white;border:1px solid var(--line);padding:7px 10px;border-radius:4px}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse}th{text-align:left;font-weight:600;background:#f7f9fc;color:#45546d;white-space:nowrap}th,td{padding:10px 12px;border-bottom:1px solid var(--line);vertical-align:top}td.number{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}details{max-width:800px}summary{color:var(--blue);cursor:pointer}pre{font:12px/1.65 ui-monospace,Consolas,monospace;white-space:pre-wrap;word-break:break-word;background:#f7f9fc;padding:12px;border-radius:4px;max-height:420px;overflow:auto}.suite{padding-top:20px}.suite:first-child{padding-top:0}.suite-head{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.suite-head h3{margin:0;font-size:16px}.counts{margin:8px 0;color:var(--muted)}.notes{border-left:3px solid #e6ae41;padding-left:16px}li{margin:6px 0}.empty{color:#9a6209}footer{color:var(--muted);margin-top:24px}.result-line{display:flex;align-items:center;gap:12px;flex-wrap:wrap}[hidden]{display:none!important}@media(max-width:760px){main{padding:20px 12px}.cards{grid-template-columns:repeat(2,minmax(100px,1fr))}section{padding:14px}h1{font-size:24px}}
"""

SCRIPT = """
const input=document.querySelector('#search'),filter=document.querySelector('#state'),count=document.querySelector('#visible-count');
function update(){let visible=0,total=0;for(const row of document.querySelectorAll('[data-case]')){total++;const show=(!filter.value||row.dataset.status===filter.value)&&row.dataset.search.toLocaleLowerCase().includes(input.value.toLocaleLowerCase());row.hidden=!show;if(show)visible++}count.textContent=`显示 ${visible} / ${total} 项`}
input.addEventListener('input',update);filter.addEventListener('change',update);update();
"""


def render(run_dir, summary):
    run_dir = Path(run_dir)
    suites = [row for row in summary.get("suites", []) if isinstance(row, dict)]
    totals = Counter()
    suite_parts = []
    for suite in suites:
        counts = suite.get("counts", {})
        if isinstance(counts, dict):
            for key in ("tests", "passed", "failures", "errors", "skipped"):
                value = counts.get(key)
                if isinstance(value, int) and value >= 0:
                    totals[key] += value
        rows = [test_row(case.get("id", case.get("name", "未命名测试")), case.get("status"), case.get("duration_ms"),
                         details(case.get("details")), suite.get("name", ""))
                for case in suite.get("cases", []) if isinstance(case, dict)]
        counts_text = " · ".join(label + " " + str(counts[key]) for key, label in
                                 (("tests", "测试数"), ("passed", "通过"), ("failures", "失败"), ("errors", "错误"), ("skipped", "跳过")) if key in counts)
        suite_parts.append('<div class="suite"><div class="suite-head"><h3>' + esc(suite.get("name", "未命名测试组")) + '</h3>' + status(suite.get("status")) + '</div><p class="subtle">' + esc(suite.get("scope", "")) + '</p><p class="counts">' + esc(counts_text or "未提供可统计的测试计数") + ' · 耗时 ' + ms(suite.get("duration_ms")) + ' ms · 退出码 ' + esc(suite.get("exit_code", "—")) + '</p><p>' + links(run_dir, suite) + '</p>' +
                           ('<div class="table-wrap"><table><thead><tr><th>测试名称</th><th>状态</th><th>耗时 ms</th><th>详情</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>' if rows else '<p class="empty">此测试组没有逐项结果，不能据此将未记录的用例记为通过。</p>') + '</div>')
    checks = [test_row(row.get("name", "未命名检查"), row.get("status"), row.get("duration_ms"), details(row.get("details")) + links(run_dir, row))
              for row in summary.get("checks", []) if isinstance(row, dict)]
    browser_source = summary.get("browser", {})
    browser_file = browser_source.get("file") if isinstance(browser_source, dict) else browser_source
    browser_report = browser_source.get("report_file") if isinstance(browser_source, dict) else None
    fixture_source = summary.get("fixture_browser")
    fixture_section = ('<section><h2>隔离夹具：快照显示监测验证</h2><p class="subtle">全部 API 使用内存响应和人工 JPEG，仅验证图片就绪、解码与绘制机会的监测链路；这些数值不代表生产性能，也不属于生产冷暖缓存结果。</p><p>' +
        local_link(run_dir, fixture_source.get("file"), "夹具原始数据") + '</p>' + render_browser(run_dir, fixture_source) + '</section>') if isinstance(fixture_source, dict) else ''
    references = " · ".join(filter(None, [local_link(run_dir, "summary.json", "本轮汇总 JSON"),
        local_link(run_dir, browser_file, "浏览器原始 JSON"), local_link(run_dir, browser_report, "浏览器详细报告"),
        local_link(run_dir, summary.get("monitoring_report"), "耗时监测 HTML 报告")]))
    notes = ''.join('<li>' + esc(note) + '</li>' for note in summary.get("notes", []))
    cards = ''.join('<div class="card"><strong>' + str(totals.get(key, 0)) + '</strong><span>' + label + '</span></div>' for key, label in
                    (("tests", "自动化用例总数"), ("passed", "自动化用例通过"), ("failures", "自动化用例失败"), ("errors", "自动化用例错误"), ("skipped", "自动化用例跳过")))
    return '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>' + esc(summary.get("title", "性能监测工作流测试报告")) + '</title><style>' + STYLE + '</style></head><body><main><header><h1>' + esc(summary.get("title", "性能监测工作流测试报告")) + '</h1><div class="result-line">' + status(summary.get("overall_status")) + '<span>本轮实际执行结果</span></div><div class="meta">开始：' + esc(summary.get("started_at", "—")) + '<br>结束：' + esc(summary.get("finished_at", "—")) + '</div><p>' + references + '</p></header><div class="cards">' + cards + '</div>' + \
        '<div class="toolbar"><label for="search">搜索测试 / 场景</label><input id="search" type="search" placeholder="输入名称"><label for="state">状态</label><select id="state"><option value="">全部</option><option value="passed">通过 / 完成</option><option value="failed">失败 / 错误</option><option value="incomplete">未完成</option><option value="skipped">跳过</option><option value="not_run">未执行</option><option value="unknown">未知</option></select><span id="visible-count" class="subtle"></span></div>' + \
        ('<section class="notes"><h2>测试范围与限制</h2><ul>' + notes + '</ul></section>' if notes else '') + \
        '<section><h2>自动化测试</h2><p class="subtle">上方计数只来自各测试组的 counts，覆盖单元及集成用例；不包含下方命令检查和浏览器观测场景。未执行、跳过及未完成项分别保留。</p>' + (''.join(suite_parts) or '<p class="empty">未提供自动化测试结果。</p>') + '</section>' + \
        '<section><h2>命令与工作流检查</h2><div class="table-wrap"><table><thead><tr><th>检查名称</th><th>状态</th><th>耗时 ms</th><th>详情 / 原始日志</th></tr></thead><tbody>' + ''.join(checks) + '</tbody></table></div>' + ('<p class="empty">未提供命令检查结果。</p>' if not checks else '') + '</section>' + \
        '<section><h2>真实服务：浏览器与快照显示实测</h2>' + render_browser(run_dir, browser_source) + '</section>' + fixture_section + '<footer>此报告由本轮 summary.json 和浏览器原始数据生成；无外部脚本、字体或 CDN。所有耗时保留原始测量口径。</footer></main><script>' + SCRIPT + '</script></body></html>'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args(argv)
    summary_path = args.run_dir / "summary.json"
    if not summary_path.is_file():
        parser.error("summary.json does not exist: " + str(summary_path))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    if not isinstance(summary, dict):
        parser.error("summary.json must contain an object")
    output = args.run_dir / "report.html"
    output.write_text(render(args.run_dir, summary), encoding="utf-8")
    print(output.resolve())


if __name__ == "__main__":
    main()
