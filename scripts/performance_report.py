"""Aggregate measured intervals without adding overlapping spans together."""
from collections import defaultdict
import csv
from datetime import datetime, timezone
import html
import json
import math
from pathlib import Path


def statistics(values):
    values = sorted(float(v) for v in values if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0)
    def percentile(fraction):
        if not values:
            return None
        index = (len(values) - 1) * fraction
        lower, upper = math.floor(index), math.ceil(index)
        return round(values[lower] + (values[upper] - values[lower]) * (index - lower), 3)
    return {"count": len(values), "p50_ms": percentile(.5), "p95_ms": percentile(.95),
        "max_ms": round(values[-1], 3) if values else None, "mean_ms": round(sum(values) / len(values), 3) if values else None}


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def summarize_spans(events):
    grouped = defaultdict(list)
    for row in events:
        timing_field = "duration_ms"
        if row.get("record_type") == "event":
            if row.get("operation") == "worker.queue_age":
                timing_field = "queue_ms"
            elif not isinstance(row.get("duration_ms"), (int, float)):
                continue
        elif row.get("record_type") != "span":
            continue
        key = (row.get("operation", "unknown"), row.get("route", ""), row.get("kind", ""), row.get("variant", ""), row.get("stage", ""), timing_field)
        grouped[key].append(row)
    result = []
    for (operation, route, kind, variant, stage, timing_field), rows in grouped.items():
        value = {"operation": operation, "route": route, "kind": kind, "variant": variant, "stage": stage, "timing_field": timing_field,
            **statistics(row.get(timing_field) for row in rows),
            "error_count": sum(row.get("outcome") == "error" or row.get("result_state") in ("handled_error", "failed") for row in rows),
            "sql_count": sum(row.get("sql_count", 0) for row in rows) if any("sql_count" in row for row in rows) else None,
            "sql_time": statistics(row.get("sql_ms") for row in rows),
            "queue_age": statistics(row.get("queue_ms") for row in rows),
            "ready_wait": statistics(row.get("wait_ms") for row in rows)}
        value["p95_sample_note"] = "low_sample_count" if value["count"] < 20 else "observed_distribution"
        result.append(value)
    return sorted(result, key=lambda row: row["p95_ms"] if row["p95_ms"] is not None else -1, reverse=True)


def _metric_rows(value, prefix=""):
    """Inventory keeps the metric owner/schema; report discovers numeric summaries."""
    if isinstance(value, dict):
        if "p50_ms" in value or "p95_ms" in value:
            yield {"metric": prefix, **value, "p50_ms": value.get("p50_ms", value.get("median_ms"))}
        else:
            for key, child in value.items():
                yield from _metric_rows(child, f"{prefix}.{key}" if prefix else key)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            label = child.get("stage", child.get("kind", str(index))) if isinstance(child, dict) else str(index)
            yield from _metric_rows(child, f"{prefix}.{label}")


def build_report(run_dir, *, baseline=None):
    run_dir = Path(run_dir)
    manifest = read_json(run_dir / "run.json", {})
    inventory = read_json(run_dir / "inventory-after.json", read_json(run_dir / "inventory.json", {}))
    resources = read_json(run_dir / "resources.json", {})
    browser = read_json(run_dir / "browser.json", {})
    events = []
    log_file = run_dir / "events.jsonl"
    if log_file.is_file():
        with log_file.open(encoding="utf-8") as incoming:
            for line in incoming:
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        events.append(value)
                except ValueError:
                    pass
    timings = summarize_spans(events)
    samples = resources.get("samples", [])
    resource_summary = {
        "sample_count": len(samples),
        "peak_application_rss_sum_bytes": max((s["application"]["rss_sum_bytes"] for s in samples), default=None),
        "peak_application_private_commit_bytes": max((s["application"]["private_commit_sum_bytes"] for s in samples if s["application"].get("private_commit_sum_bytes") is not None), default=None),
        "minimum_system_available_physical_bytes": min((s["system"]["physical_available_bytes"] for s in samples if s["system"].get("physical_available_bytes") is not None), default=None),
        "minimum_system_available_commit_bytes": min((s["system"]["commit_available_bytes"] for s in samples if s["system"].get("commit_available_bytes") is not None), default=None),
        "max_system_cpu_percent": max((s["system"]["cpu_percent"] for s in samples if s["system"].get("cpu_percent") is not None), default=None),
        "max_application_cpu_percent_of_machine": max((s["application"]["cpu_percent_of_machine"] for s in samples if s["application"].get("cpu_percent_of_machine") is not None), default=None),
        "sampler_cost": statistics(s.get("sampling_ms") for s in samples),
        "observed_service_processes": max((s["application"]["process_count"] for s in samples), default=0),
    }
    coverage = {
        "runtime_inventory": bool(inventory) and not inventory.get("error"),
        "request_and_sql_instrumentation": any(r.get("operation") == "http.request" for r in events),
        "background_instrumentation": any(r.get("operation", "").startswith(("job.", "worker.", "preview.", "ai.", "maintenance.")) for r in events),
        "capture_stages": bool(inventory.get("capture_diagnostics", {}).get("usable")),
        "resources": bool(samples),
        "browser": browser.get("status", browser.get("state", "not_requested")),
        "notes": ["No observation is not zero duration.", "Spans are inclusive and overlap; do not sum nested timings.",
            "p95 from fewer than 20 observations is labelled low_sample_count; this is not a performance SLO.",
            "SQL timings measure driver execution, excluding later Python row processing; stream iteration is not exact network delivery.",
            "Browser display timings are load/decode plus outer rendering opportunities, not physical display presentation.",
            "Default collection never runs a new capture, AI call, import, backup, restore, retry or classification approval."],
    }
    report = {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(), "run": manifest,
        "coverage": coverage, "timings": timings, "inventory_metrics": list(_metric_rows(inventory)),
        "inventory": inventory, "resource_summary": resource_summary, "browser": browser,
        "comparison": []}
    if baseline:
        previous = read_json(baseline, {})
        old = {(row["operation"], row.get("route", ""), row.get("kind", ""), row.get("variant", ""), row.get("stage", ""), row.get("timing_field", "duration_ms")): row for row in previous.get("timings", [])}
        for row in timings:
            prior = old.get((row["operation"], row["route"], row["kind"], row["variant"], row["stage"], row["timing_field"]))
            if prior and prior.get("p95_ms") is not None and row["p95_ms"] is not None:
                report["comparison"].append({"operation": row["operation"], "route": row["route"], "kind": row["kind"], "variant": row["variant"],
                    "previous_count": prior["count"], "current_count": row["count"],
                    "p95_delta_ms": round(row["p95_ms"] - prior["p95_ms"], 3),
                    "p95_change_percent": round(100 * (row["p95_ms"] / prior["p95_ms"] - 1), 2) if prior["p95_ms"] else None})
    return report


def write_csv(path, rows, columns):
    with Path(path).open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: ("'" + str(value) if isinstance(value, str) and value.startswith(("=", "+", "-", "@")) else value)
                for key, value in row.items()})


def _number(value):
    return "未采集" if value is None else f"{value:,.2f}" if isinstance(value, (int, float)) else str(value)


def render_markdown(report):
    coverage = report["coverage"]
    lines = ["# 软件耗时监测报告", "", f"采集时间：{report['run'].get('started_at_local', report['generated_at'])}",
        "", "仅观测与汇总，不执行修复或性能优化。", "", "## 数据覆盖", "",
        "| 范围 | 本次记录 |", "| --- | --- |"]
    names = {"runtime_inventory": "库与任务清单", "request_and_sql_instrumentation": "请求和SQL", "background_instrumentation": "后台执行", "capture_stages": "抓取阶段", "resources": "CPU与内存", "browser": "浏览器显示"}
    lines.extend(f"| {name} | {'有' if coverage[key] is True else '未采集' if coverage[key] is False else coverage[key]} |" for key, name in names.items())
    lines += ["", "## 请求和后台计时", "", "各行是独立区间的统计，父子区间可能重叠。少于20个样本的P95仅用于观察，不作为稳定结论。", "",
        "| 操作 / 路由 | 类型 / 设备 | 样本 | P50 ms | P95 ms | 最大 ms | 异常 |", "| --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    for row in report["timings"][:80]:
            lines.append(f"| {row['operation']} {row['route']} {row['stage']} | {row['kind']} {row['variant']} | {row['count']} | {_number(row['p50_ms'])} | {_number(row['p95_ms'])} | {_number(row['max_ms'])} | {row['error_count']} |")
    if not report["timings"]:
        lines.append("| 尚无启用监测后的操作记录 | | 0 | | | | |")
    requests = [row for row in report["timings"] if row["operation"] == "http.request"]
    if requests:
        lines += ["", "## API 与数据库", "", "SQL 时间是驱动调用区间，不含随后逐行转换；请求时间已包含SQL，不能相加。", "",
            "| 路由 | 请求数 | SQL调用总数 | 每请求SQL P50 ms | 每请求SQL P95 ms | 请求P95 ms |", "| --- | ---: | ---: | ---: | ---: | ---: |"]
        for row in requests:
            lines.append(f"| {row['route']} | {row['count']} | {_number(row['sql_count'])} | {_number(row['sql_time']['p50_ms'])} | {_number(row['sql_time']['p95_ms'])} | {_number(row['p95_ms'])} |")
    queues = [row for row in report["timings"] if row["operation"] == "worker.queue_age"]
    if queues:
        lines += ["", "## 排队与可执行等待", "", "queue_age 是任务创建至本次认领的总历时，重试时可能含上次执行；ready_wait 从 available_at 至认领。首次排队和重试间隔另见历史attempt指标。", "",
            "| 类型 / 设备 | 样本 | 创建至认领P50 ms | 创建至认领P95 ms | 可执行后等待P50 ms | 可执行后等待P95 ms |", "| --- | ---: | ---: | ---: | ---: | ---: |"]
        for row in queues:
            lines.append(f"| {row['kind']} {row['variant']} | {row['count']} | {_number(row['p50_ms'])} | {_number(row['p95_ms'])} | {_number(row['ready_wait']['p50_ms'])} | {_number(row['ready_wait']['p95_ms'])} |")
    if report["inventory_metrics"]:
        lines += ["", "## 已有任务与抓取阶段", "", "| 指标 | 样本 | P50 ms | P95 ms | 最大 ms |", "| --- | ---: | ---: | ---: | ---: |"]
        for row in report["inventory_metrics"][:100]:
            lines.append(f"| {row['metric']} | {row.get('count', row.get('sample_count', ''))} | {_number(row.get('p50_ms'))} | {_number(row.get('p95_ms'))} | {_number(row.get('max_ms'))} |")
    summary = report["resource_summary"]
    lines += ["", "## 资源占用", "", f"资源样本 {summary['sample_count']} 个；已识别的软件进程最多 {summary['observed_service_processes']} 个。",
        f"应用RSS合计峰值：{_number(summary['peak_application_rss_sum_bytes'])} B（共享页可能重复计算）。",
        f"系统可用物理内存最低：{_number(summary['minimum_system_available_physical_bytes'])} B。",
        f"系统可用提交额度最低：{_number(summary['minimum_system_available_commit_bytes'])} B。",
        "", "## 浏览器快照显示", "", "含列表首张与整页就绪、图片下载/解码与外层绘制机会、详情和完整HTML抽样；冷暖缓存分别记录。完整数据见 browser.json。", ""]
    browser = report["browser"]
    if browser:
        lines += ["| 设备 / 场景 | 首图就绪 ms | 首图绘制机会 ms | 全部就绪 ms | 就绪/可用 | 待生成 | 状态 |", "| --- | ---: | ---: | ---: | ---: | ---: | --- |"]
        for row in browser.get("runs", []) + browser.get("scenarios", []):
            preview = row.get("preview", {})
            label = " / ".join(str(row.get(key, "")) for key in ("device", "cache", "scenario") if row.get(key))
            lines.append(f"| {label} | {_number(preview.get('first_ready_ms'))} | {_number(preview.get('first_paint_opportunity_ms'))} | {_number(preview.get('all_ready_ms'))} | {preview.get('success', 0)}/{preview.get('available', 0)} | {preview.get('pending', 0)} | {row.get('status', '')} |")
        if browser.get("html_samples"):
            lines += ["", "| 完整HTML抽样 | 文件字节 | iframe加载 ms | 外层绘制机会 ms | 状态 |", "| --- | ---: | ---: | ---: | --- |"]
            for row in browser["html_samples"]:
                lines.append(f"| {row.get('device', '')} | {_number(row.get('artifact_bytes'))} | {_number(row.get('iframe_load_ms'))} | {_number(row.get('outer_paint_opportunity_ms'))} | {row.get('status', '')} |")
        lines += ["", "## 浏览器资源与主线程", "", "网络阶段、脚本/样式等资源的细分记录在 browser.json；资源耗时重叠，不是可相加的整页耗时。", "",
            "| 设备 / 缓存 | 导航TTFB ms | DOM就绪 ms | 传输字节 | 长任务数 | 长任务总ms |", "| --- | ---: | ---: | ---: | ---: | ---: |"]
        for row in browser.get("runs", []):
            tasks = row.get("longtasks", [])
            transfer = sum(value.get("transfer_bytes", 0) for value in row.get("resources", []))
            lines.append(f"| {row.get('device', '')} {row.get('cache', '')} | {_number(row.get('navigation', {}).get('ttfb_ms'))} | {_number(row.get('navigation', {}).get('dom_content_loaded_ms'))} | {transfer} | {len(tasks)} | {_number(sum(value.get('duration_ms', 0) for value in tasks))} |")
    else:
        lines.append("本次未启用浏览器探测；使用 collect --browser 采集。")
    lines += ["", "## 口径与边界", "", "- 未观测到的环节保留缺口，不填零值。", "- 排队等待与执行耗时分别统计；重试、失败、取消分别记录。", "- 抓取阶段由已有诊断相邻事件差分，表示两事件之间的区间，不声称每个函数的独占耗时。", "- SQL与应用流迭代不是端到端网络传输；浏览器load/decode/绘制机会不是硬件呈现时间。", "- 探测使用独立浏览器，不代表日常Firefox、实体手机或公网部署的实测结果。", "- 未主动运行AI、导入、备份和恢复时，这些环节可能仅有历史或无记录；实际操作后重新收集。", ""]
    return "\n".join(lines)


def write_report(run_dir, *, baseline=None):
    run_dir = Path(run_dir)
    report = build_report(run_dir, baseline=baseline)
    (run_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown = render_markdown(report)
    (run_dir / "report.md").write_text(markdown, encoding="utf-8")
    columns = ["operation", "route", "kind", "variant", "stage", "timing_field", "count", "p50_ms", "p95_ms", "max_ms", "mean_ms", "error_count", "sql_count", "p95_sample_note"]
    write_csv(run_dir / "timings.csv", report["timings"], columns)
    write_csv(run_dir / "inventory-timings.csv", report["inventory_metrics"], ["metric", "count", "sample_count", "p50_ms", "p95_ms", "max_ms", "mean_ms"])
    from performance_tasks_report import write_task_report
    write_task_report(run_dir)
    return report


def _markdown_html(markdown):
    """Render only the small, controlled report dialect; all text is escaped."""
    rendered, table, in_list = [], [], False
    def finish_table():
        if not table:
            return
        header, *rows = table
        rendered.append('<div class="table"><table><thead><tr>' + ''.join('<th>' + html.escape(cell.strip()) + '</th>' for cell in header) + '</tr></thead><tbody>')
        for row in rows:
            if all(set(cell.strip()) <= set('-: ') for cell in row):
                continue
            rendered.append('<tr>' + ''.join('<td>' + html.escape(cell.strip()) + '</td>' for cell in row) + '</tr>')
        rendered.append('</tbody></table></div>')
        table.clear()
    for line in markdown.splitlines():
        if line.startswith('|'):
            table.append(line.strip('|').split('|'))
            continue
        finish_table()
        if line.startswith('- '):
            if not in_list:
                rendered.append('<ul>')
                in_list = True
            rendered.append('<li>' + html.escape(line[2:]) + '</li>')
            continue
        if in_list:
            rendered.append('</ul>')
            in_list = False
        if line.startswith('# '):
            continue
        if line.startswith('## '):
            rendered.append('<h2>' + html.escape(line[3:]) + '</h2>')
        elif line:
            rendered.append('<p>' + html.escape(line) + '</p>')
    finish_table()
    if in_list:
        rendered.append('</ul>')
    return '\n'.join(rendered)
