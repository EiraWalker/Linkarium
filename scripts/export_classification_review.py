#!/usr/bin/env python3
"""Export a local classification run to an offline review and importable bookmarks.

Uses only the Python standard library. Reads JSON files; never accesses a database,
fetches URLs, changes source bookmarks, or approves a classification proposal.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
from html import escape
from html.parser import HTMLParser
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit


REVIEW_NAME = "待人工判断"
EVIDENCE_FIELDS = {
    "title", "url", "source_titles", "description", "text_excerpt", "notes", "tags"
}
REVIEW_REASONS = {
    "insufficient_evidence": "证据不足",
    "conflicting_evidence": "证据冲突",
    "taxonomy_gap": "目录缺项",
}


class ExportError(ValueError):
    """An input or export does not meet the preservation contract."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ExportError(message)


def unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        require(key not in result, f"Duplicate JSON key: {key!r}")
        result[key] = value
    return result


def read_json(path: Path) -> dict:
    with path.open(encoding="utf-8-sig") as handle:
        result = json.load(handle, object_pairs_hook=unique_object)
    require(isinstance(result, dict), f"Expected a JSON object: {path}")
    return result


def string(value: object, label: str, *, nonempty: bool = False) -> str:
    require(isinstance(value, str), f"{label} must be a string")
    require(not nonempty or bool(value.strip()), f"{label} must not be empty")
    return value


def strings(value: object, label: str) -> list[str]:
    require(isinstance(value, list), f"{label} must be an array")
    for index, item in enumerate(value):
        string(item, f"{label}[{index}]")
    return value


def indexed(items: object, id_field: str, label: str) -> dict[str, dict]:
    require(isinstance(items, list), f"{label} must be an array")
    result = {}
    for item in items:
        require(isinstance(item, dict), f"Each {label} item must be an object")
        key = string(item.get(id_field), f"{label}.{id_field}", nonempty=True)
        require(key not in result, f"Duplicate {label} ID: {key}")
        result[key] = item
    return result


def matching_ids(actual: dict, expected: dict, label: str) -> None:
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    require(not missing and not extra,
            f"{label} coverage mismatch: missing={missing[:8]}, unknown={extra[:8]}")


def normalized(value: str) -> str:
    return " ".join(value.split())


def load_review_flags(path: Path | None, resources: dict) -> dict[str, list[str]]:
    """Read independent review notes without changing model assignments."""
    if path is None:
        return {}
    flags = read_json(path)
    unknown = sorted(set(flags) - set(resources))
    require(not unknown, f"Review flags contain unknown resource IDs: {unknown[:8]}")
    result = {}
    for resource_id, notes in flags.items():
        strings(notes, f"Review flags for {resource_id}")
        for note in notes:
            string(note, f"Review flag for {resource_id}", nonempty=True)
        if notes:
            result[resource_id] = notes
    return result


def load_and_validate(run_dir: Path, taxonomy_path: Path, assignments_path: Path) -> dict:
    taxonomy = read_json(taxonomy_path)
    assignments = read_json(assignments_path)
    resources = indexed(read_json(run_dir / "resources.json").get("resources"), "id", "resources")
    originals = read_json(run_dir / "originals.local.json")
    input_summary = read_json(run_dir / "input-summary.json")
    require(bool(resources), "The resource list is empty")
    matching_ids(originals, resources, "Original resources")
    categories = indexed(taxonomy.get("categories"), "id", "categories")
    require(0 < len(categories) <= 256, "Expected 1 to 256 taxonomy categories")
    require(taxonomy.get("schema_version") == 1, "Unsupported taxonomy schema_version")
    string(taxonomy.get("taxonomy_id"), "taxonomy_id", nonempty=True)
    require(type(taxonomy.get("version")) is int and taxonomy["version"] > 0,
            "Taxonomy version must be a positive integer")
    taxonomy_hash = hashlib.sha256(json.dumps(
        taxonomy, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    require(assignments.get("taxonomy_id") == taxonomy["taxonomy_id"], "Taxonomy ID mismatch")
    require(assignments.get("taxonomy_version") == taxonomy["version"], "Taxonomy version mismatch")
    require(assignments.get("taxonomy_hash") == taxonomy_hash, "Taxonomy content hash mismatch")

    children = defaultdict(list)
    sibling_names = set()
    for category_id, category in categories.items():
        name = string(category.get("name"), f"Category {category_id} name", nonempty=True)
        parent = category.get("parent_id")
        require(parent is None or isinstance(parent, str), f"Invalid parent: {category_id}")
        require(parent is None or parent in categories, f"Unknown parent for {category_id}: {parent}")
        require(isinstance(category.get("assignable"), bool), f"Missing assignable: {category_id}")
        require((parent, name) not in sibling_names, f"Duplicate sibling name: {name}")
        sibling_names.add((parent, name))
        children[parent].append(category_id)

    paths = {}
    for category_id in categories:
        ancestors = []
        current = category_id
        while current is not None:
            require(current not in ancestors, f"Taxonomy cycle at {category_id}")
            ancestors.append(current)
            require(len(ancestors) <= 8, f"Taxonomy depth exceeds 8 at {category_id}")
            current = categories[current]["parent_id"]
        paths[category_id] = list(reversed(ancestors))

    for resource_id, resource in resources.items():
        for field in ("title", "url", "description", "text_excerpt", "notes"):
            string(resource.get(field, ""), f"{resource_id}.{field}")
        for field in ("source_titles", "tags", "evidence_warnings"):
            strings(resource.get(field, []), f"{resource_id}.{field}")
        original = originals[resource_id]
        require(isinstance(original, dict), f"Original {resource_id} must be an object")
        string(original.get("title"), f"Original {resource_id} title")
        string(original.get("url"), f"Original {resource_id} URL", nonempty=True)
        strings(original.get("source_path"), f"Original {resource_id} source_path")
    urls = [item["url"] for item in originals.values()]
    require(len(set(urls)) == len(urls), "Original URL list contains duplicates")
    if "unique_public" in input_summary:
        require(input_summary["unique_public"] == len(resources), "Input summary unique_public mismatch")

    items = indexed(assignments.get("items"), "resource_id", "assignments")
    matching_ids(items, resources, "Assignments")
    grouped = defaultdict(list)
    total_counts = Counter()
    for resource_id, item in items.items():
        status = item.get("status")
        category_id = item.get("category_id")
        string(item.get("reason"), f"{resource_id} reason", nonempty=True)
        tags = strings(item.get("tags"), f"{resource_id} tags")
        require(len(tags) <= 5 and all(1 <= len(tag) <= 40 for tag in tags),
                f"Invalid tags: {resource_id}")
        evidence = item.get("evidence")
        require(isinstance(evidence, list), f"Evidence must be an array: {resource_id}")
        if status == "classified":
            require(isinstance(category_id, str) and category_id in categories,
                    f"Unknown classification category: {resource_id}")
            require(categories[category_id]["assignable"], f"Non-assignable category: {resource_id}")
            require(item.get("review_reason") == "", f"Classified item has review_reason: {resource_id}")
            require(1 <= len(evidence) <= 2, f"Expected 1 to 2 evidence entries: {resource_id}")
            for ancestor in paths[category_id]:
                total_counts[ancestor] += 1
        elif status == "needs_review":
            require(category_id is None, f"needs_review must have a null category: {resource_id}")
            require(item.get("review_reason") in REVIEW_REASONS, f"Invalid review_reason: {resource_id}")
            require(len(evidence) <= 2, f"Expected 0 to 2 evidence entries: {resource_id}")
        else:
            raise ExportError(f"Unknown assignment status: {resource_id}: {status}")
        for entry in evidence:
            require(isinstance(entry, dict), f"Evidence entry must be an object: {resource_id}")
            field = entry.get("field")
            require(isinstance(field, str) and field in EVIDENCE_FIELDS, f"Invalid evidence field: {resource_id}")
            quote = normalized(string(entry.get("quote"), f"{resource_id} evidence quote"))
            require(3 <= len(quote) <= 240, f"Evidence quote length is invalid: {resource_id}")
            source = resources[resource_id].get(field, "")
            sources = source if isinstance(source, list) else [source]
            require(any(quote in normalized(value) for value in sources),
                    f"Evidence quote not found in {field}: {resource_id}")
        grouped[category_id].append(resource_id)

    # Keep the original input ordering inside each directory for stable exports.
    rank = {resource_id: index for index, resource_id in enumerate(resources)}
    for group in grouped.values():
        group.sort(key=rank.__getitem__)
    require(not grouped[None] or not any(
        categories[key]["name"] == REVIEW_NAME for key in children[None]
    ), f"A taxonomy root already uses the reserved review folder name: {REVIEW_NAME}")
    return {
        "taxonomy": taxonomy, "taxonomy_hash": taxonomy_hash, "categories": categories,
        "resources": resources, "originals": originals, "items": items,
        "input_summary": input_summary, "children": children, "paths": paths,
        "grouped": grouped, "total_counts": total_counts,
    }


def h(value: object) -> str:
    return escape(str(value), quote=True)


def path_label(data: dict, category_id: str) -> str:
    return " › ".join(data["categories"][key]["name"] for key in data["paths"][category_id])


def link_or_text(url: str, title: str, class_name: str = "") -> str:
    try:
        safe = urlsplit(url).scheme.lower() in {"http", "https"}
    except ValueError:
        safe = False
    if safe:
        return (f'<a class="{h(class_name)}" href="{h(url)}" target="_blank" '
                f'rel="noopener noreferrer">{h(title)}</a>')
    return f'<span class="{h(class_name)}">{h(title)}</span>'


STYLE = """
:root{color-scheme:light;--ink:#20252d;--muted:#697281;--line:#dfe3e8;--blue:#3266a8;--pale:#edf3fa}
*{box-sizing:border-box}body{margin:0;font:14px/1.65 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;color:var(--ink);background:#f5f6f8}
button,input{font:inherit}button{cursor:pointer}a{color:var(--blue);text-decoration:none}a:hover{text-decoration:underline}
.layout{display:grid;grid-template-columns:292px minmax(0,1fr);min-height:100vh}aside{background:#fff;border-right:1px solid var(--line);padding:25px 18px;position:sticky;top:0;height:100vh;overflow:auto}
.brand{font-size:18px;font-weight:650;margin-bottom:4px}.muted,.version{color:var(--muted)}.version{font-size:12px;overflow-wrap:anywhere}.tree{padding:0;margin:18px 0 0;list-style:none}.tree ul{list-style:none;padding:0 0 0 12px;border-left:1px solid var(--line);margin-left:8px}
.tree button{display:flex;gap:10px;align-items:baseline;justify-content:space-between;width:100%;border:0;background:transparent;border-radius:5px;padding:7px 9px;text-align:left;color:var(--ink);line-height:1.4}.tree button:hover,.tree button.active{background:var(--pale);color:var(--blue)}.count{color:var(--muted);font-variant-numeric:tabular-nums;font-size:12px;white-space:nowrap}
main{max-width:1230px;width:100%;padding:30px 38px 60px}h1{margin:0;font-size:27px;letter-spacing:-.4px}h2{font-size:18px;font-weight:650;margin:0}.intro{margin:7px 0 19px;color:var(--muted)}.metrics{display:flex;flex-wrap:wrap;gap:10px;margin:18px 0}.metric{background:#fff;border:1px solid var(--line);border-radius:7px;padding:11px 17px;min-width:130px}.metric strong{font-size:23px;font-weight:600;display:block;line-height:1.3}.metric span{color:var(--muted);font-size:12px}
.toolbar{display:flex;gap:8px;margin:24px 0 12px;position:sticky;top:0;padding:12px 0;background:#f5f6f8;z-index:2}.toolbar input{width:100%;min-width:0;border:1px solid #cbd2dc;border-radius:6px;padding:11px 13px;background:#fff;color:var(--ink)}.toolbar button{border:1px solid #cbd2dc;border-radius:6px;padding:8px 15px;background:#fff;color:var(--ink);white-space:nowrap}input:focus-visible,button:focus-visible,a:focus-visible,summary:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
.result-meta{display:flex;justify-content:space-between;gap:14px;color:var(--muted);margin:2px 0 20px}.group{margin:0 0 27px}.group-head{display:flex;gap:12px;align-items:baseline;margin-bottom:10px}.group-head span{color:var(--muted);font-size:12px}.card{border:1px solid var(--line);border-radius:7px;background:white;padding:15px 18px;margin:8px 0}.title-row{display:flex;gap:12px;align-items:baseline;justify-content:space-between}.title{font-size:16px;font-weight:600;overflow-wrap:anywhere}.status{flex:none;font-size:11px;border:1px solid var(--line);border-radius:4px;padding:1px 6px;color:var(--muted)}.status.review{background:var(--pale);color:var(--blue);border-color:#cfdbeb}.url{display:block;margin-top:3px;color:var(--muted);font-size:12px;overflow-wrap:anywhere}.reason{margin:10px 0 6px}.tags{display:flex;gap:6px;flex-wrap:wrap}.tag{font-size:11px;border-radius:4px;background:#f0f2f5;color:#5d6877;padding:1px 7px}details{margin-top:10px;border-top:1px solid #eef0f3;padding-top:8px}summary{cursor:pointer;color:var(--blue);font-size:12px}.details-body{padding-top:9px}dl{display:grid;grid-template-columns:110px minmax(0,1fr);margin:8px 0;gap:7px 12px;font-size:13px}dt{color:var(--muted)}dd{margin:0;overflow-wrap:anywhere}pre{white-space:pre-wrap;overflow-wrap:anywhere;margin:0;font:inherit}blockquote{margin:5px 0 9px;padding-left:12px;border-left:2px solid #ccd8e8;color:#505c6d}.evidence-label{font-size:11px;color:var(--muted)}.empty{padding:40px;background:#fff;border:1px dashed #cbd2dc;text-align:center;color:var(--muted)}footer{font-size:12px;color:var(--muted);margin-top:30px}.empty[hidden],[hidden]{display:none!important}
.card-statuses{display:flex;flex:none;flex-wrap:wrap;gap:6px;align-items:baseline}.status.flagged{color:var(--blue);border-color:#a9c1df;font-weight:600}.review-flags{background:#f5f8fc;border:1px solid #dce5f1;border-radius:5px;padding:8px 11px}.review-flags summary{font-weight:600}.review-flags ul{margin:8px 0 3px;padding-left:21px}.review-flags li{margin:5px 0;white-space:pre-wrap;overflow-wrap:anywhere}.review-note{font-size:12px;color:var(--muted);margin:8px 0 0}
@media(max-width:800px){.layout{display:block}aside{height:auto;position:static;border-right:0;border-bottom:1px solid var(--line);padding:18px}.tree{max-height:250px;overflow:auto;margin-top:12px}main{padding:22px 18px}.title-row{display:block}.status{display:inline-block;margin-top:5px}dl{grid-template-columns:1fr;gap:2px}dd{margin-bottom:9px}.result-meta{font-size:12px}}
@media print{aside,.toolbar,.result-meta{display:none}.layout{display:block}main{max-width:none;padding:0}.card{break-inside:avoid}.group[hidden],.card[hidden]{display:none!important}body{background:white}.status{border:0}}
"""


SCRIPT = """
(() => {
  const query = document.getElementById('search');
  const cards = [...document.querySelectorAll('.card')].map(el => ({
    el, text: el.textContent.toLocaleLowerCase(), keys: JSON.parse(el.dataset.ancestors)
  }));
  const groups = [...document.querySelectorAll('.group')];
  const buttons = [...document.querySelectorAll('.tree button')];
  let selected = 'all';
  function update() {
    const words = query.value.toLocaleLowerCase().trim().split(/\\s+/).filter(Boolean);
    const matched = new Map();
    let visible = 0;
    for (const card of cards) {
      const matches = words.every(word => card.text.includes(word));
      if (matches) {
        for (const key of ['all', ...card.keys]) matched.set(key, (matched.get(key) || 0) + 1);
      }
      card.el.hidden = !(matches && (selected === 'all' || card.keys.includes(selected)));
      if (!card.el.hidden) visible++;
    }
    for (const group of groups) {
      const count = [...group.querySelectorAll('.card')].filter(el => !el.hidden).length;
      group.hidden = count === 0;
      group.querySelector('.group-count').textContent = count + ' 项';
    }
    for (const button of buttons) {
      button.classList.toggle('active', button.dataset.key === selected);
      button.setAttribute('aria-pressed', String(button.dataset.key === selected));
      const count = button.querySelector('.count');
      count.textContent = words.length ? `${matched.get(button.dataset.key) || 0} / ${count.dataset.total}` : count.dataset.total;
    }
    document.getElementById('visible-count').textContent = '显示 ' + visible + ' / ' + cards.length + ' 项';
    document.getElementById('empty').hidden = visible !== 0;
  }
  query.addEventListener('input', update);
  for (const button of buttons) button.addEventListener('click', () => {
    selected = button.dataset.key;
    document.getElementById('selection').textContent = button.dataset.label;
    update();
  });
  document.getElementById('reset').addEventListener('click', () => {
    query.value = ''; selected = 'all';
    document.getElementById('selection').textContent = '全部资源';
    update(); query.focus();
  });
  update();
})();
"""


def review_html(data: dict, generated_at: str, review_flags: dict[str, list[str]] | None = None) -> str:
    review_flags = review_flags or {}
    taxonomy, categories = data["taxonomy"], data["categories"]
    grouped, total_counts = data["grouped"], data["total_counts"]
    keys = {category_id: f"node-{index}" for index, category_id in enumerate(categories)}
    total = len(data["resources"])
    pending = len(grouped[None])

    def tree_button(key: str, name: str, count: int, label: str) -> str:
        return (f'<button type="button" data-key="{h(key)}" data-label="{h(label)}" aria-pressed="false">'
                f'<span>{h(name)}</span><span class="count" data-total="{count}">{count}</span></button>')

    def tree_node(category_id: str) -> str:
        child_markup = "".join(tree_node(child) for child in data["children"][category_id])
        result = "<li>" + tree_button(keys[category_id], categories[category_id]["name"],
                                     total_counts[category_id], path_label(data, category_id))
        return result + (f"<ul>{child_markup}</ul>" if child_markup else "") + "</li>"

    def field(label: str, value: str) -> str:
        return f"<dt>{h(label)}</dt><dd>{h(value) if value else '—'}</dd>"

    def card(resource_id: str) -> str:
        resource, original, item = (data[key][resource_id] for key in ("resources", "originals", "items"))
        category_id = item["category_id"]
        ancestors = [keys[key] for key in data["paths"][category_id]] if category_id else ["review"]
        is_review = item["status"] == "needs_review"
        status = REVIEW_REASONS[item["review_reason"]] if is_review else "已建议归类"
        flags = review_flags.get(resource_id, [])
        flag_badge = '<span class="status flagged">需复核</span>' if flags else ""
        flag_details = (
            f'<details class="review-flags"><summary>需复核 · {len(flags)} 条评审提示</summary>'
            '<ul>' + "".join(f'<li>{h(note)}</li>' for note in flags) + '</ul>'
            '<p class="review-note">以下归属与理由仍为模型原始候选，评审提示尚未改动分类。</p></details>'
        ) if flags else ""
        metadata = [field("资源 ID", resource_id), field("原始标题", original["title"]),
                    field("原始来源目录", " › ".join(original["source_path"])),
                    field("分类依据标题", resource["title"]), field("分类依据 URL", resource["url"]),
                    field("其他来源标题", "\n".join(resource.get("source_titles", []))),
                    field("已有标签", "、".join(resource.get("tags", []))),
                    field("证据提示", "、".join(resource.get("evidence_warnings", [])))]
        for source, label in (("description", "页面描述"), ("notes", "备注"), ("text_excerpt", "页面摘录")):
            if resource.get(source):
                metadata.append(f"<dt>{h(label)}</dt><dd><pre>{h(resource[source])}</pre></dd>")
        if is_review:
            metadata.append(field("待判断原因", f'{status} ({item["review_reason"]})'))
        evidence = "".join(
            f'<div class="evidence-label">{h(entry["field"])}</div><blockquote>{h(entry["quote"])}</blockquote>'
            for entry in item["evidence"]
        ) or '<p class="muted">未提供可引用证据。</p>'
        tags = "".join(f'<span class="tag">{h(tag)}</span>' for tag in item["tags"])
        return (
            f'<article class="card" data-resource-id="{h(resource_id)}" data-ancestors="{h(json.dumps(ancestors))}">'
            f'<div class="title-row">{link_or_text(original["url"], resource["title"] or original["title"] or "（空标题）", "title")}'
            f'<div class="card-statuses">{flag_badge}<span class="status{" review" if is_review else ""}">{h(status)}</span></div></div>'
            f'<span class="url">{h(original["url"])}</span>{flag_details}<p class="reason">{h(item["reason"])}</p>'
            f'<div class="tags">{tags}</div><details><summary>查看证据、原标题与来源目录</summary>'
            f'<div class="details-body">{evidence}<dl>{"".join(metadata)}</dl></div></details></article>'
        )

    sections = []

    def add_section(category_id: str | None) -> None:
        ids = grouped[category_id]
        if ids:
            label = path_label(data, category_id) if category_id is not None else REVIEW_NAME
            sections.append(f'<section class="group"><div class="group-head"><h2>{h(label)}</h2>'
                            f'<span class="group-count">{len(ids)} 项</span></div>'
                            + "".join(card(resource_id) for resource_id in ids) + "</section>")
        for child in data["children"][category_id] if category_id is not None else []:
            add_section(child)

    for category_id in data["children"][None]:
        add_section(category_id)
    add_section(None)
    tree = ("<li>" + tree_button("all", "全部资源", total, "全部资源") + "</li>"
            + "".join(tree_node(category_id) for category_id in data["children"][None])
            + "<li>" + tree_button("review", REVIEW_NAME, pending, REVIEW_NAME) + "</li>")
    metrics = "".join(f'<div class="metric"><strong>{count}</strong><span>{label}</span></div>'
                      for count, label in ((total, "完整资源"), (total - pending, "归类建议"),
                                           (pending, "模型待人工判断"), (len(review_flags), "评审需复核"),
                                           (len(categories), "目录节点")))
    return (
        '<!doctype html>\n<html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
        "style-src 'unsafe-inline'; script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">"
        '<title>资源分类审阅</title><style>' + STYLE + '</style></head><body><div class="layout">'
        '<aside><div class="brand">资源分类审阅</div>'
        f'<div class="version">{h(taxonomy["taxonomy_id"])} · v{taxonomy["version"]}</div>'
        f'<nav aria-label="目录筛选"><ul class="tree">{tree}</ul></nav></aside><main>'
        '<h1>书签分类候选</h1><p class="intro">按目录检查模型候选，展开查看理由与来源证据。搜索可匹配标题、链接、标签、理由、原始目录和复核提示。</p>'
        '<p class="review-note">“需复核”来自评审或重复运行中的分歧；可导入书签保留模型原始分类候选。模型待判断与评审需复核可能重叠，条数不能直接相加。</p>'
        f'<div class="metrics">{metrics}</div><div class="toolbar">'
        '<input id="search" type="search" aria-label="搜索资源" placeholder="搜索标题、链接、主题或原始目录…">'
        '<button id="reset" type="button">重置筛选</button></div>'
        f'<div class="result-meta"><span id="selection">全部资源</span><span id="visible-count" role="status">显示 {total} / {total} 项</span></div>'
        + "".join(sections)
        + '<div id="empty" class="empty" hidden>没有匹配的资源。可更换关键词或重置筛选。</div>'
        + f'<footer>本页在本地运行。目录数和完整覆盖只证明结构有效，不代表语义分类正确；这些结果仍是建议。<br>'
        f'生成时间：{h(generated_at)} · 目录 SHA256：{h(data["taxonomy_hash"])}</footer>'
        '</main></div><script>' + SCRIPT + '</script></body></html>\n'
    )


class BookmarkAudit(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links = []
        self.current = None

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == "a":
            require(self.current is None, "Nested links in generated bookmarks")
            self.current = (dict(attrs), [])

    def handle_data(self, value: str) -> None:
        if self.current is not None:
            self.current[1].append(value)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.current is not None:
            attrs, title_parts = self.current
            self.links.append((attrs.get("data-resource-id"), attrs.get("href"), "".join(title_parts)))
            self.current = None


def bookmarks_html(data: dict) -> str:
    lines = ['<!DOCTYPE NETSCAPE-Bookmark-file-1>',
             '<!-- Generated classification suggestions; original bookmark URLs and titles preserved. -->',
             '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">',
             '<TITLE>Classified Bookmarks</TITLE>', '<H1>Classified Bookmarks</H1>', '<DL><p>']

    def bookmarks(category_id: str | None, depth: int) -> None:
        indent = "    " * depth
        for resource_id in data["grouped"][category_id]:
            original = data["originals"][resource_id]
            lines.append(f'{indent}<DT><A HREF="{h(original["url"])}" DATA-RESOURCE-ID="{h(resource_id)}">{h(original["title"])}</A>')

    def category(category_id: str, depth: int) -> None:
        indent = "    " * depth
        lines.append(f'{indent}<DT><H3>{h(data["categories"][category_id]["name"])}</H3>')
        lines.append(f"{indent}<DL><p>")
        bookmarks(category_id, depth + 1)
        for child in data["children"][category_id]:
            category(child, depth + 1)
        lines.append(f"{indent}</DL><p>")

    for category_id in data["children"][None]:
        category(category_id, 1)
    if data["grouped"][None]:
        lines += [f"    <DT><H3>{REVIEW_NAME}</H3>", "    <DL><p>"]
        bookmarks(None, 2)
        lines.append("    </DL><p>")
    lines.append("</DL><p>")
    output = "\n".join(lines) + "\n"
    audit = BookmarkAudit()
    audit.feed(output)
    audit.close()
    expected = Counter((resource_id, item["url"], item["title"])
                       for resource_id, item in data["originals"].items())
    require(Counter(audit.links) == expected,
            "Generated bookmark round-trip failed: IDs, original URLs or titles changed")
    return output


def taxonomy_markdown(data: dict) -> str:
    taxonomy = data["taxonomy"]
    pending = len(data["grouped"][None])
    lines = ["# 资源分类目录", "", f"资源总数：{len(data['resources'])}；归类建议：{len(data['resources']) - pending}；待人工判断：{pending}。", "",
             "以下数量为该目录及其子目录的合计；“本级”表示直接放在当前目录中的数量。名称中的斜杠是名称文字，层级仅由 parent_id 决定。", ""]

    def one_line(value: object) -> str:
        return " ".join(str(value).splitlines())

    def branch(category_id: str, prefix: str, last: bool) -> None:
        category = data["categories"][category_id]
        own = len(data["grouped"][category_id])
        total = data["total_counts"][category_id]
        suffix = f"合计 {total} · 本级 {own}" + (" · 容器" if not category["assignable"] else "")
        lines.append(f"    {prefix}{'└── ' if last else '├── '}{one_line(category['name'])} [{suffix}]")
        children = data["children"][category_id]
        for index, child in enumerate(children):
            branch(child, prefix + ("    " if last else "│   "), index == len(children) - 1)

    for category_id in data["children"][None]:
        branch(category_id, "", False)
    lines.append(f"    └── {REVIEW_NAME} [{pending}]")
    lines += ["", "目录版本：", "", f"    {one_line(taxonomy['taxonomy_id'])} / v{taxonomy['version']}",
              f"    SHA256: {data['taxonomy_hash']}", "", "这些文件是分类建议导出，未改变原始书签或数据库。完整覆盖不等于语义质量已经确认。", ""]
    return "\n".join(lines)


def export(run_dir: Path, taxonomy_path: Path, assignments_path: Path, output_dir: Path,
           review_flags_path: Path | None = None) -> dict:
    data = load_and_validate(run_dir, taxonomy_path, assignments_path)
    review_flags = load_review_flags(review_flags_path, data["resources"])
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    content = {
        "review.html": review_html(data, generated_at, review_flags),
        "classified-bookmarks.html": bookmarks_html(data),
        "taxonomy.md": taxonomy_markdown(data),
    }
    pending = len(data["grouped"][None])
    summary = {
        "generated_at": generated_at,
        "taxonomy_id": data["taxonomy"]["taxonomy_id"],
        "taxonomy_version": data["taxonomy"]["version"],
        "taxonomy_hash": data["taxonomy_hash"],
        "resource_count": len(data["resources"]),
        "original_count": len(data["originals"]),
        "assignment_count": len(data["items"]),
        "classified_count": len(data["resources"]) - pending,
        "needs_review_count": pending,
        "model_needs_review_count": pending,
        "flagged_count": len(review_flags),
        "flagged_classified_count": sum(data["items"][key]["status"] == "classified" for key in review_flags),
        "flagged_needs_review_count": sum(data["items"][key]["status"] == "needs_review" for key in review_flags),
        "category_count": len(data["categories"]),
        "bookmark_count": len(data["originals"]),
        "unique_original_url_count": len({item["url"] for item in data["originals"].values()}),
        "coverage_validated": True,
        "bookmark_urls_and_titles_roundtrip_validated": True,
        "semantic_quality_validated": False,
        "outputs": [str((output_dir / name).resolve()) for name in (*content, "export-summary.json")],
    }
    content["export-summary.json"] = json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    protected = {path.resolve() for path in (
        taxonomy_path, assignments_path, run_dir / "resources.json",
        run_dir / "originals.local.json", run_dir / "input-summary.json",
    )}
    if isinstance(data["input_summary"].get("source"), str):
        protected.add(Path(data["input_summary"]["source"]).resolve())
    if review_flags_path is not None:
        protected.add(review_flags_path.resolve())
    for name in content:
        require((output_dir / name).resolve() not in protected, f"Output would overwrite an input: {name}")
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, text in content.items():
        (output_dir / name).write_text(text, encoding="utf-8", newline="\n")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True,
                        help="Directory containing resources.json, originals.local.json and input-summary.json")
    parser.add_argument("--taxonomy", type=Path, required=True)
    parser.add_argument("--assignments", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--review-flags", type=Path,
                        help="Optional JSON object mapping resource IDs to arrays of independent review notes")
    args = parser.parse_args()
    try:
        summary = export(args.run_dir, args.taxonomy, args.assignments, args.output, args.review_flags)
    except (ExportError, OSError, json.JSONDecodeError, UnicodeError) as exc:
        print(f"Export failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
