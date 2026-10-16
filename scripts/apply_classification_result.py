"""Apply an explicitly accepted offline classification, without importing bookmarks.

Dry-run is the default. Exact URL coverage is required; --apply replaces only
folders/assignments and the shared classification vocabulary, without a backup.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

from export_classification_review import load_and_validate


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = args.run_dir.resolve()
    summary = json.loads((root / "final/export-summary.json").read_text(encoding="utf-8"))
    version = summary["taxonomy_version"]
    source = load_and_validate(root, root / f"taxonomy-v{version}.json", root / f"assignments-v{version}.json")
    assert source["taxonomy_hash"] == summary["taxonomy_hash"], "Export taxonomy hash changed"
    import django
    django.setup()
    from django.db import transaction
    from django.db.models import F
    from django.utils import timezone
    from config.paths import DATA_ROOT, current_generation
    from runtime.models import RuntimeState
    from library.catalog import clean_name, normalize_url
    from library.classification import name_key
    from library.models import (Artifact, Bookmark, BookmarkTag, ClassificationProposal,
        ClassificationTaxonomy, Folder, Job, LibrarySettings, SnapshotVersion, Tag)

    for category in source["categories"].values():
        clean_name(category["name"])
    review_name = "待人工判断"
    expected_urls = {rid: normalize_url(item["url"])[0] for rid, item in source["originals"].items()}
    assert len(set(expected_urls.values())) == len(expected_urls), "Canonical source URL collision"

    def match():
        sites = list(Bookmark.objects.all())
        by_url = {site.canonical_url: site for site in sites}
        missing = set(expected_urls.values()) - set(by_url)
        extra = set(by_url) - set(expected_urls.values())
        if missing or extra:
            raise ValueError(f"Exact coverage required: source={len(expected_urls)}, library={len(sites)}, missing={len(missing)}, extra={len(extra)}")
        return {rid: by_url[url] for rid, url in expected_urls.items()}

    sites = match()
    counts = Counter(item["status"] for item in source["items"].values())
    report = {"applied": False, "backup_created": False, "data_root": str(DATA_ROOT),
        "generation": current_generation(), "taxonomy_hash": source["taxonomy_hash"],
        "taxonomy_version": version, "matched_sites": len(sites), "classified": counts["classified"],
        "needs_review": counts["needs_review"], "old_folder_count": Folder.objects.count(),
        "expected_folder_count": len(source["categories"]) + int(counts["needs_review"] > 0)}
    if not args.apply:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    def protected_digest():
        ignored = {"folder", "revision", "classification_revision", "updated_at"}
        fields = [f.attname for f in Bookmark._meta.concrete_fields if f.name not in ignored]
        return digest({"bookmarks": list(Bookmark.objects.order_by("id").values(*fields)),
            "other": {model.__name__: list(model.objects.order_by("pk").values())
                for model in (Tag, BookmarkTag, SnapshotVersion, Artifact)}})

    with transaction.atomic(using="library"):
        # Obtain SQLite's write lock before checking coverage or changing the tree.
        LibrarySettings.objects.filter(pk=1).update(tree_revision=F("tree_revision") + 1)
        state = RuntimeState.objects.get(pk=1)
        assert not state.maintenance_mode and state.active_generation == report["generation"], "Library generation changed or under maintenance"
        sites = match()
        preserved = protected_digest()
        old_ids = set(Folder.objects.values_list("id", flat=True))
        now = timezone.now()
        report["cancelled_old_classification_jobs"] = Job.objects.filter(kind="classification",
            state__in=["queued", "running", "retry_wait"]).update(state="cancelled", active_key=None,
            active_attempt_id=None, error_code="TASK_SUPERSEDED", updated_at=now)
        report["invalidated_old_proposals"] = ClassificationProposal.objects.filter(
            status__in=["pending", "stale"]).update(status="rejected", revision=F("revision") + 1, updated_at=now)
        folders = {}
        destination_ids = set()
        positions = Counter()
        def ensure_folder(category_id):
            if category_id in folders:
                return folders[category_id]
            category = source["categories"][category_id]
            parent = ensure_folder(category["parent_id"]) if category["parent_id"] else None
            position = positions[category["parent_id"]]
            positions[category["parent_id"]] += 1
            node, _ = Folder.objects.get_or_create(parent=parent, name_key=name_key(category["name"]),
                defaults={"name": category["name"], "position": position, "created_by_ai": True})
            Folder.objects.filter(pk=node.id).update(name=category["name"], position=position, updated_at=now)
            folders[category_id] = node
            destination_ids.add(node.id)
            return node
        for category_id in source["categories"]:
            ensure_folder(category_id)
        review = None
        if counts["needs_review"]:
            review, _ = Folder.objects.get_or_create(parent=None, name_key=name_key(review_name),
                defaults={"name": review_name, "position": positions[None], "created_by_ai": False})
            destination_ids.add(review.id)
        for rid, item in source["items"].items():
            target = folders[item["category_id"]] if item["status"] == "classified" else review
            updated = Bookmark.objects.filter(pk=sites[rid].id,
                classification_revision=sites[rid].classification_revision).update(folder=target,
                classification_revision=F("classification_revision") + 1, revision=F("revision") + 1, updated_at=now)
            assert updated == 1, "Concurrent bookmark change"
        # Empty leaves only: recursive deletion would cascade to bookmarks.
        obsolete = old_ids - destination_ids
        removed = 0
        while obsolete:
            leaves = list(Folder.objects.filter(pk__in=obsolete, bookmarks__isnull=True,
                children__isnull=True).values_list("id", flat=True))
            assert leaves, "Obsolete folders still contain data; refusing cascading deletion"
            Folder.objects.filter(pk__in=leaves).delete()
            obsolete.difference_update(leaves)
            removed += len(leaves)
        vocabulary = [{"path": [source["categories"][ancestor]["name"] for ancestor in source["paths"][cid]],
            "description": (category["description"] + " 纳入：" + "；".join(category["include"])
                + " 排除：" + "；".join(category["exclude"]))[:900]}
            for cid, category in source["categories"].items() if category["assignable"]]
        ClassificationTaxonomy.objects.update_or_create(pk=1, defaults={"categories": vocabulary})
        assert protected_digest() == preserved, "Non-classification data changed"
        assert Bookmark.objects.count() == len(sites)
        assert Folder.objects.count() == report["expected_folder_count"]
        after = {site.id: site.folder_id for site in Bookmark.objects.all()}
        assert all(after[sites[rid].id] == (folders[item["category_id"]].id if item["status"] == "classified" else review.id)
            for rid, item in source["items"].items()), "Assignment verification failed"
        report.update(applied=True, removed_old_folders=removed, folder_count=Folder.objects.count(),
            root_folder_count=Folder.objects.filter(parent=None).count(), protected_data_unchanged=True,
            updated_sites=len(sites), shared_destinations=len(vocabulary), all_assignments_verified=True,
            root_counts={node.name: sum(len(source["grouped"][cid]) for cid, path in source["paths"].items()
                if path[0] == root_id) for root_id, node in folders.items()
                if source["categories"][root_id]["parent_id"] is None})
        report["root_counts"][review_name] = counts["needs_review"]
    (root / "production-apply-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
