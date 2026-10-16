"""Apply the approved short names in place, preserving folder IDs and bookmark data."""
import argparse
import copy
import importlib.util
import json
import os
from pathlib import Path
import re
import sys

from apply_classification_result import digest

ROOT = Path(__file__).resolve().parents[1]
SKILL = Path.home() / ".codex/skills/resource-classifier"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_new(path, value):
    if path.exists():
        assert read(path) == value, f"Refusing to overwrite a different version: {path}"
    else:
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    run = args.run_dir.resolve()
    old = read(run / "taxonomy-v4.json")
    assignments = read(run / "assignments-v4.json")
    mapping = read(ROOT / "docs/classification-short-names.json")
    module_spec = importlib.util.spec_from_file_location("classification_validator", SKILL / "scripts/validate_classification.py")
    validator = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(validator)
    old_info = validator.taxonomy(old)
    assert assignments["taxonomy_hash"] == old_info["taxonomy_hash"]
    assert set(mapping) == {c["id"] for c in old["categories"]}
    new = copy.deepcopy(old)
    new["version"] = old["version"] + 1
    new["reference_summary"].append("2026-10-10 用户批准 short-cn 命名；v5 仅缩短名称并保留旧名别名，沿用 v4 的定义、层级与全部资源归属。")
    new["rules"].append("显示名称使用 short-cn：一级中文两字词或一个英文单词；二级及更深推荐中文2–4字或1–2个英文单词。中文优先，专有领域名词优先英文，允许简短中英组合与有理由的标准专名例外。完整分类范围由说明与纳入/排除规则表达。")
    for category in new["categories"]:
        before = category["name"]
        category["name"] = mapping[category["id"]]
        if before != category["name"] and before not in category["aliases"]:
            category["aliases"].append(before)
        name = category["name"]
        if category["parent_id"] is None:
            assert re.fullmatch(r"[\u4e00-\u9fff]{2}|[A-Za-z]+", name), name
        else:
            assert re.fullmatch(r"[\u4e00-\u9fff]{2,4}|[A-Za-z][A-Za-z0-9]*(?: [A-Za-z][A-Za-z0-9]*)?|[A-Za-z][A-Za-z0-9]*[\u4e00-\u9fff]{2,4}", name), name
    info = validator.taxonomy(new)
    migrated = copy.deepcopy(assignments)
    migrated.update(taxonomy_version=new["version"], taxonomy_hash=info["taxonomy_hash"])
    # Only the envelope changes: no model call or resource reassignment.
    assert migrated["items"] == assignments["items"]
    for before, after in zip(old["categories"], new["categories"]):
        assert {k:v for k,v in before.items() if k not in {"name", "aliases"}} == {k:v for k,v in after.items() if k not in {"name", "aliases"}}
    for filename, value in [("taxonomy-v5.json", new), ("assignments-v5.json", migrated)]:
        write_new(run / filename, value)

    import django
    django.setup()
    from django.db import transaction
    from django.db.models import F
    from django.utils import timezone
    from config.paths import DATA_ROOT, current_generation
    from runtime.models import RuntimeState
    from library.catalog import clean_name
    from library.models import (Folder, Bookmark, BookmarkTag, Tag, Artifact, SnapshotVersion,
        ClassificationTaxonomy, ClassificationProposal, LibrarySettings, Job)

    path_changes = {tuple(old_info["paths"][cid]): info["paths"][cid] for cid in mapping}
    path_changes[("待人工判断",)] = ["待审"]
    def inspect():
        nodes = {str(f.id): f for f in Folder.objects.all()}
        paths = {}
        for fid, node in nodes.items():
            path, seen, cursor = [], set(), node
            while cursor:
                assert str(cursor.id) not in seen
                seen.add(str(cursor.id))
                path.insert(0, cursor.name)
                cursor = nodes[str(cursor.parent_id)] if cursor.parent_id else None
            paths[tuple(path)] = node
        assert set(paths) == set(path_changes), "Live folders differ from approved v4; refusing partial rename"
        return paths
    paths = inspect()
    report = {"applied": False, "backup_created": False, "data_root": str(DATA_ROOT),
        "generation": current_generation(), "old_taxonomy_hash": old_info["taxonomy_hash"],
        "taxonomy_version": 5, "taxonomy_hash": info["taxonomy_hash"],
        "folder_count": len(paths), "rename_count": sum(p[-1] != path_changes[p][-1] for p in paths),
        "website_count": Bookmark.objects.count(), "new_names_pass_length_check": True,
        "active_classification_jobs": Job.objects.filter(kind="classification", state__in=["queued", "running", "retry_wait"]).count()}
    if not args.apply:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    def protected_digest():
        return digest({model.__name__: list(model.objects.order_by("pk").values())
            for model in (Bookmark, BookmarkTag, Tag, Artifact, SnapshotVersion)})
    with transaction.atomic(using="library"):
        LibrarySettings.objects.filter(pk=1).update(tree_revision=F("tree_revision") + 1)
        state = RuntimeState.objects.get(pk=1)
        assert not state.maintenance_mode and state.active_generation == report["generation"]
        paths = inspect()
        before_data = protected_digest()
        before_structure = set(Folder.objects.values_list("id", "parent_id", "position", "created_by_ai"))
        vocabulary = copy.deepcopy(ClassificationTaxonomy.objects.get(pk=1).categories)
        for category in vocabulary:
            category["path"] = path_changes[tuple(category["path"])]
        now = timezone.now()
        report["cancelled_old_jobs"] = Job.objects.filter(kind="classification", state__in=["queued", "running", "retry_wait"]).update(
            state="cancelled", active_key=None, active_attempt_id=None, error_code="TASK_SUPERSEDED", updated_at=now)
        report["invalidated_pending_proposals"] = ClassificationProposal.objects.filter(status="pending").update(
            status="stale", revision=F("revision") + 1, updated_at=now)
        # Temporary unique names also make sibling name swaps safe.
        changes = [(node, path_changes[path][-1]) for path, node in paths.items() if path[-1] != path_changes[path][-1]]
        for node, name in changes:
            Folder.objects.filter(pk=node.id).update(name_key="rename-" + str(node.id))
        for node, name in changes:
            display, key = clean_name(name)
            Folder.objects.filter(pk=node.id).update(name=display, name_key=key, revision=F("revision") + 1, updated_at=now)
        ClassificationTaxonomy.objects.filter(pk=1).update(categories=vocabulary)
        assert protected_digest() == before_data, "Bookmark or other protected data changed"
        assert set(Folder.objects.values_list("id", "parent_id", "position", "created_by_ai")) == before_structure
        for path, node in paths.items():
            assert Folder.objects.get(pk=node.id).name == path_changes[path][-1]
        report.update(applied=True, folder_ids_and_hierarchy_preserved=True,
            all_bookmark_fields_and_assignments_preserved=True, snapshots_and_tags_preserved=True,
            shared_destinations=len(vocabulary), roots=list(Folder.objects.filter(parent=None).order_by("position").values_list("name", flat=True)))
    (run / "short-name-apply-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 当前短名称目录", "", "v5：仅改名；沿用 v4 的全部分类归属。", ""]
    for category in new["categories"]:
        lines.append("- " + " → ".join(info["paths"][category["id"]]))
    lines.extend(["- 待审", "", "本目录为已应用版本；原 v4 预览作为历史评测记录保留。", ""])
    (run / "taxonomy-v5.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
