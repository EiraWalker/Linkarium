"""Validate and combine explicitly assigned, disjoint evaluation shards."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import resource_classification_run as runner


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def version_spec(version):
    if version == 3:
        return {
            "shards": [("classify-v3", [1]), ("classify-v3-long-a", range(2, 14)),
                       ("classify-v3-long-b", range(14, 25))],
            "progress": "classification-progress.json",
            "assignments": "assignments-v3.json",
            "provenance": "classification-provenance.json",
            "note": "Old batch 1 retained; long-a 2–13 and long-b 14–24 selected explicitly. Old batch 3 is a duplicate response retained only as supplementary repeat evidence.",
        }
    if version == 4:
        return {
            "shards": [("classify-v4-a", range(1, 13)), ("classify-v4-b", range(13, 25))],
            "progress": "classification-progress-v4.json",
            "assignments": "assignments-v4.json",
            "provenance": "classification-provenance-v4.json",
            "note": "Only classify-v4-a batches 1–12 and classify-v4-b batches 13–24 are accepted. V3 and stability results are excluded.",
        }
    raise ValueError("Only taxonomy versions 3 and 4 are supported")


def collect(root, version=3, require_complete=False):
    root = root.resolve()
    spec = version_spec(version)
    tax = read(root / f"taxonomy-v{version}.json")
    if tax["version"] != version:
        raise ValueError("Taxonomy file version does not match --version")
    source = read(root / "resources.json")
    validator = runner.load_validator(runner.DEFAULT_SKILL)
    resources = validator.resources(source)
    source_hash = runner.digest(source)
    tax_hash = validator.taxonomy(tax)["taxonomy_hash"]
    items, provenance, batches = {}, {}, []
    expected_indices = {number for _, indices in spec["shards"] for number in indices}
    for directory, indices in spec["shards"]:
        manifest_path = root / directory / "manifest.json"
        manifest = read(manifest_path) if manifest_path.is_file() else None
        if manifest:
            if manifest.get("stage") != "classify" or manifest.get("input_sha256") != source_hash:
                raise ValueError(f"Source manifest drift: {directory}")
            if manifest.get("taxonomy_hash") != tax_hash:
                raise ValueError(f"Taxonomy manifest drift: {directory}")
        for number in indices:
            candidates = list((root / directory / "batches" / f"classify-{number:04d}").glob("*/success.json"))
            if len(candidates) > 1:
                raise ValueError(f"Multiple successful request versions for batch {number}; require explicit selection")
            if not candidates:
                continue
            if manifest is None or number not in manifest.get("selected_batch_indices", []):
                raise ValueError(f"Successful batch is absent from its source run manifest: {directory}/{number}")
            folder = candidates[0].parent
            request = read(folder / "request.json")
            success = read(folder / "success.json")
            result = read(folder / "result.json")
            request_hash = request.pop("request_sha256")
            if runner.digest(request) != request_hash or success.get("request_sha256") != request_hash or success.get("valid") is not True:
                raise ValueError("Request provenance hash or success validity mismatch")
            if request.get("stage") != "classify" or request.get("batch_id") != f"classify-{number:04d}":
                raise ValueError("Request batch identity mismatch")
            payload = json.loads(request["messages"][1]["content"])
            batch_resources = validator.resources({"resources": payload["resources"]})
            if request.get("taxonomy_hash") != tax_hash or payload.get("approved_taxonomy_hash") != tax_hash:
                raise ValueError("Request taxonomy hash drift")
            if any(resource != resources.get(rid) for rid, resource in batch_resources.items()):
                raise ValueError("Original resource content drift")
            expected_projection = runner.compact_taxonomy(tax) if request.get("taxonomy_transport_mode") == "columnar_lossless" else tax
            if payload.get("taxonomy") != expected_projection:
                raise ValueError("Transmitted taxonomy does not match the frozen taxonomy")
            if request.get("taxonomy_projection_sha256") != runner.digest(expected_projection):
                raise ValueError("Transmitted taxonomy projection hash mismatch")
            if request.get("model") != manifest.get("model"):
                raise ValueError("Request model does not match its source manifest")
            validator.assignments(tax, {"resources": payload["resources"]}, result, tax_hash)
            for item in result["items"]:
                key = item["resource_id"]
                if key in items:
                    raise ValueError(f"Duplicate final resource {key}")
                items[key] = item
                provenance[key] = {"request_sha256": request_hash, "batch_directory": str(folder.relative_to(root)), "model": request["model"]}
            batches.append({"number": number, "source": directory, "resources": len(result["items"]), "model": request["model"]})
    combined = {"taxonomy_id": tax["taxonomy_id"], "taxonomy_version": tax["version"], "taxonomy_hash": tax_hash,
                "items": [items[resource["id"]] for resource in source["resources"] if resource["id"] in items]}
    complete = set(items) == set(resources) and {batch["number"] for batch in batches} == expected_indices
    summary = {"complete": complete, "processed": len(items), "total": len(resources), "valid_batches": len(batches),
               "batches": sorted(batches, key=lambda batch: batch["number"]),
               "classified": sum(item["status"] == "classified" for item in items.values()),
               "needs_review": sum(item["status"] == "needs_review" for item in items.values()),
               "taxonomy_id": tax["taxonomy_id"], "taxonomy_version": version, "taxonomy_hash": tax_hash,
               "source_input_sha256": source_hash,
               "pending_batch_indices": sorted(expected_indices - {batch["number"] for batch in batches})}
    runner.write_json(root / spec["progress"], summary)
    if complete:
        validator.assignments(tax, source, combined, tax_hash)
        runner.write_json(root / spec["assignments"], combined)
        runner.write_json(root / spec["provenance"], {"summary": summary, "items": provenance, "note": spec["note"]})
    elif require_complete:
        raise ValueError(f"Classification incomplete: {len(items)}/{len(resources)}")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--version", type=int, choices=(3, 4), default=3)
    parser.add_argument("--require-complete", action="store_true")
    args = parser.parse_args(argv)
    summary = collect(args.run_dir, args.version, args.require_complete)
    print(json.dumps({key: value for key, value in summary.items() if key != "batches"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
