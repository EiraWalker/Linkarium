#!/usr/bin/env python3
"""Prepare a bounded, read-only semantic audit without exposing held-out answers.

Creates audit-input.json and audit-selection.json only. Does not call an API.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import resource_classification_run as runner

ANCHORS = (
    ("game_community_operations", r"ゲームコミュニティ運営|游戏社区运营"),
    ("steam_market_data", r"Video Game Insights|Steam.*(?:market|sales|revenue)|SteamDB"),
    ("guardian", r"\bGuardian\b"),
    ("lawfare", r"\bLawfare\b"),
    ("ahrefs", r"\bAhrefs\b"),
    ("probability_time_series", r"Probability|Statistical|Statistics|Markov|Random Processes|Time.?Series"),
)
BOUNDARIES = (
    ("development_or_player_content", r"Modding|mod.?development|game.?wiki"),
    ("development_or_consumer_ai", r"\bMCP\b|Webhook|API|PaddleOCR"),
    ("academic_typesetting", r"LaTeX|Latex|排版|引文"),
    ("maps_and_geospatial", r"地图|BIGEMAP|\bGIS\b"),
    ("financial_application_or_foundation", r"Probability|Markov|Time.?Series|Pricing|金融工程"),
    ("art_tools_or_implementation", r"Blender|Shader|HLSL|声音克隆|3D.*AI|AI.*3D"),
    ("banking_or_payment_service", r"银行|开户|信用卡|代充|Banking"),
)


def rank(seed, resource_id):
    return hashlib.sha256(f"{seed}:{resource_id}".encode("utf-8")).hexdigest()


def resource_search_text(resource):
    return "\n".join([resource["title"], *resource.get("source_titles", []), resource["url"]])


def read_exclusions(holdout_file, stability_file):
    """Read ID-only selection metadata, never the holdout expectations file."""
    holdout = json.loads(holdout_file.read_text(encoding="utf-8-sig"))
    stability = json.loads(stability_file.read_text(encoding="utf-8-sig"))
    holdout_ids = {item["id"] for item in holdout["selected"]}
    stability_ids = set(stability["ids"])
    if not holdout_ids or not stability_ids:
        raise ValueError("Independent holdout and stability ID lists must both be nonempty")
    return holdout_ids, stability_ids, set(holdout.get("excluded_fixture_resource_ids", []))


def read_version_exclusions(directory, taxonomy_version):
    """Read only ID columns of the additional independent v4 evaluation sets."""
    if taxonomy_version < 4:
        return set(), set(), []
    holdout_file = directory / "holdout-v4-expectations.json"
    stability_file = directory / "stability-v4-resources.json"
    # Never access expected labels, rationales, or scoring guidance. The audit
    # receives none of this file's contents; only its IDs are used for exclusion.
    holdout = json.loads(holdout_file.read_text(encoding="utf-8-sig"))
    holdout_ids = {case["resource_id"] for case in holdout["cases"]}
    del holdout
    stability = json.loads(stability_file.read_text(encoding="utf-8-sig"))
    stability_ids = {resource["id"] for resource in stability["resources"]}
    del stability
    if not holdout_ids or not stability_ids:
        raise ValueError("V4 holdout and stability exclusion ID lists must both be nonempty")
    records = [{"file": str(path.resolve()), "kind": kind, "count": len(ids),
                "ids_sha256": runner.digest(sorted(ids))}
               for path, kind, ids in ((holdout_file, "v4_holdout", holdout_ids),
                                       (stability_file, "v4_stability", stability_ids))]
    return holdout_ids, stability_ids, records


def regression_ids(resources, fixture_file, known_fixture_ids):
    # Only title fields are used to match the established regression fixtures.
    fixture = json.loads(fixture_file.read_text(encoding="utf-8-sig"))
    names = set()
    for case in fixture["cases"]:
        for field in ("title", "current_display_title"):
            value = case.get(field)
            if isinstance(value, str) and value.strip():
                names.add(" ".join(value.split()).casefold())
    matched = set(known_fixture_ids)
    for resource in resources.values():
        titles = [resource["title"], *resource.get("source_titles", [])]
        if any(" ".join(title.split()).casefold() in names for title in titles):
            matched.add(resource["id"])
    return matched & resources.keys(), len(fixture["cases"])


def build_candidates(resources, assignments, excluded, regressions, seed):
    eligible = set(resources) - excluded
    regression_pool = eligible & regressions
    queues = {"regression": [], "needs_review": [], "boundary_sample": []}
    used_regression = set()
    for anchor, pattern in ANCHORS:
        matches = [rid for rid in regression_pool - used_regression
                   if re.search(pattern, resource_search_text(resources[rid]), re.I)]
        if matches:
            rid = min(matches, key=lambda value: rank(seed + ":anchor:" + anchor, value))
            queues["regression"].append({"resource_id": rid, "reason": "regression",
                                         "detail": "Known regression resource matched by " + anchor})
            used_regression.add(rid)
    for rid in sorted(regression_pool - used_regression, key=lambda value: rank(seed + ":regression", value)):
        queues["regression"].append({"resource_id": rid, "reason": "regression",
                                     "detail": "Known regression fixture title or mapped resource ID"})
    review_order = {"taxonomy_gap": 0, "conflicting_evidence": 1, "insufficient_evidence": 2}
    review_ids = [rid for rid in eligible - regression_pool if assignments[rid]["status"] == "needs_review"]
    review_ids.sort(key=lambda rid: (review_order.get(assignments[rid]["review_reason"], 3),
                                     rank(seed + ":review", rid)))
    for rid in review_ids:
        queues["needs_review"].append({"resource_id": rid, "reason": "needs_review",
                                      "detail": "Model left unresolved: " + assignments[rid]["review_reason"]})
    used_boundaries = set()
    for boundary, pattern in BOUNDARIES:
        matches = [rid for rid in eligible - regression_pool - set(review_ids) - used_boundaries
                   if re.search(pattern, resource_search_text(resources[rid]), re.I)]
        if matches:
            rid = min(matches, key=lambda value: rank(seed + ":boundary:" + boundary, value))
            queues["boundary_sample"].append({"resource_id": rid, "reason": "boundary_sample",
                                              "detail": "Evidence/title boundary trigger: " + boundary})
            used_boundaries.add(rid)
    # These slots reserve uncertain and boundary records near the beginning so a
    # size reduction does not turn the audit into only already-known regressions.
    schedule = ["regression"] * 3 + ["needs_review", "boundary_sample",
                "regression", "regression", "regression", "needs_review",
                "boundary_sample", "needs_review", "regression"]
    selected = []
    for group in schedule:
        if queues[group]:
            selected.append(queues[group].pop(0))
    for group in ("regression", "needs_review", "boundary_sample"):
        selected.extend(queues[group])
    seen = set()
    return [item for item in selected if not (item["resource_id"] in seen or seen.add(item["resource_id"]))]


def audit_payload(reference_profile, taxonomy, resources, assignment_header, assignments, selected, total):
    ids = [item["resource_id"] for item in selected]
    return {
        "reference_profile": reference_profile,
        "taxonomy": taxonomy,
        "resources": [resources[rid] for rid in ids],
        "assignments": {**assignment_header, "items": [assignments[rid] for rid in ids]},
        "audit_scope": {
            "purpose": "Bounded semantic spot audit of real classification suggestions",
            "selected_resource_count": len(ids),
            "source_resource_count": total,
            "sampling": "Known regressions, unresolved suggestions and adjacent-purpose boundary records",
            "independent_holdout_and_stability_samples_excluded": True,
            "expected_answers_provided": False,
            "limitation": "This selected sample does not establish full-library semantic accuracy or stability.",
        },
    }


def resolve_model_source(assignment_file, directory, assignment_ids, explicit_model=None, provenance_file=None,
                         *, taxonomy_version=3, taxonomy_hash=None):
    """Resolve accounting model from actual run records or an explicit override."""
    manifest_file = assignment_file.parent / "manifest.json"
    provenance_name = "classification-provenance.json" if taxonomy_version == 3 else f"classification-provenance-v{taxonomy_version}.json"
    provenance_file = provenance_file or directory / provenance_name
    source_info = {"model_override": explicit_model, "manifest_file": None, "provenance_file": None}
    source_by_id = {}
    models = set()
    if manifest_file.is_file():
        manifest = json.loads(manifest_file.read_text(encoding="utf-8-sig"))
        if taxonomy_hash is not None and manifest.get("taxonomy_hash") != taxonomy_hash:
            raise ValueError("Original run manifest taxonomy hash differs from supplied assignments")
        models.add(manifest["model"])
        source_info.update(manifest_file=str(manifest_file.resolve()), manifest_sha256=runner.digest(manifest))
    elif provenance_file.is_file():
        provenance = json.loads(provenance_file.read_text(encoding="utf-8-sig"))
        provenance_summary = provenance.get("summary", {})
        if taxonomy_version >= 4 and (provenance_summary.get("taxonomy_version") != taxonomy_version
                                     or provenance_summary.get("taxonomy_hash") != taxonomy_hash):
            raise ValueError("Merge provenance taxonomy version/hash differs from supplied assignments")
        if provenance_summary.get("taxonomy_version", taxonomy_version) != taxonomy_version:
            raise ValueError("Merge provenance taxonomy version differs from supplied assignments")
        if taxonomy_hash is not None and provenance_summary.get("taxonomy_hash", taxonomy_hash) != taxonomy_hash:
            raise ValueError("Merge provenance taxonomy hash differs from supplied assignments")
        source_by_id = provenance.get("items", {})
        if set(source_by_id) != set(assignment_ids):
            raise ValueError("Merge provenance resource IDs do not match the supplied assignments")
        for item in source_by_id.values():
            model = item.get("model")
            if not isinstance(model, str) or not model:
                raise ValueError("Merge provenance must record the source model for every assignment")
            models.add(model)
        source_info.update(provenance_file=str(provenance_file.resolve()),
                           provenance_sha256=runner.digest(provenance),
                           provenance_resource_count=len(source_by_id),
                           source_note=provenance.get("note"))
        requests = {}
        for rid, item in source_by_id.items():
            folder = (directory / item["batch_directory"]).resolve()
            if not folder.is_relative_to(directory.resolve()):
                raise ValueError("Merge provenance source must remain inside the run directory")
            if folder not in requests:
                request = json.loads((folder / "request.json").read_text(encoding="utf-8-sig"))
                request_hash = request.pop("request_sha256")
                if runner.digest(request) != request_hash:
                    raise ValueError("Merge source request hash mismatch")
                success = json.loads((folder / "success.json").read_text(encoding="utf-8-sig"))
                if success.get("valid") is not True or success.get("request_sha256") != request_hash:
                    raise ValueError("Merge source success record does not match its request")
                payload = json.loads(request["messages"][1]["content"])
                if payload["taxonomy"]["version"] != taxonomy_version:
                    raise ValueError("Merge source request taxonomy version mismatch")
                if taxonomy_hash is not None and (request.get("taxonomy_hash") != taxonomy_hash
                                                 or payload.get("approved_taxonomy_hash") != taxonomy_hash):
                    raise ValueError("Merge source request taxonomy hash mismatch")
                requests[folder] = (request_hash, request["model"], {row["id"] for row in payload["resources"]})
            request_hash, request_model, resource_ids = requests[folder]
            if item["request_sha256"] != request_hash or item["model"] != request_model or rid not in resource_ids:
                raise ValueError("Per-resource merge provenance does not match its source request")
        source_info["verified_source_request_count"] = len(requests)
        source_info["verified_taxonomy_version"] = taxonomy_version
        source_info["verified_taxonomy_hash"] = taxonomy_hash
        # Original manifests are optional cross-checks, never synthesized copies.
        manifests = []
        for source in sorted({batch["source"] for batch in provenance.get("summary", {}).get("batches", [])}):
            path = directory / source / "manifest.json"
            if path.is_file():
                manifest = json.loads(path.read_text(encoding="utf-8-sig"))
                if taxonomy_hash is not None and manifest.get("taxonomy_hash") != taxonomy_hash:
                    raise ValueError("Original run manifest taxonomy hash disagrees with merged assignments")
                if manifest.get("model") not in models:
                    raise ValueError("Original run manifest model disagrees with merge provenance")
                manifests.append({"file": str(path.resolve()), "model": manifest["model"],
                                  "sha256": runner.digest(manifest)})
        source_info["original_manifests"] = manifests
    if explicit_model:
        model = explicit_model
        source_info["resolution"] = "explicit_model_for_audit_request_budget"
    elif len(models) == 1:
        model = next(iter(models))
        source_info["resolution"] = "run_manifest" if source_info["manifest_file"] else "merge_provenance"
    else:
        raise ValueError("Cannot infer one audit model from the available records; provide --model explicitly")
    source_info["classification_source_models"] = sorted(models)
    return model, source_info, source_by_id


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--taxonomy", type=Path)
    parser.add_argument("--assignments", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--skill", type=Path, default=runner.DEFAULT_SKILL)
    parser.add_argument("--model", help="Defaults to the original run manifest or merged classification provenance's model")
    parser.add_argument("--provenance", type=Path, help="Merge provenance; inferred from the supplied assignments' taxonomy version")
    parser.add_argument("--seed", default="resource-classifier-semantic-audit-2026-10-10")
    parser.add_argument("--max-items", type=int, default=12)
    parser.add_argument("--max-payload-chars", type=int, default=58000)
    args = parser.parse_args(argv)
    if not 1 <= args.max_items <= 12:
        parser.error("--max-items must be 1..12")
    if not 1000 <= args.max_payload_chars <= 58000:
        parser.error("--max-payload-chars must be 1000..58000")
    directory = args.run_dir.resolve()
    taxonomy_file = args.taxonomy or directory / "taxonomy-v3.json"
    assignment_file = args.assignments or directory / "classify-v3" / "combined.json"
    output = (args.output or directory).resolve()
    validator = runner.load_validator(args.skill)
    taxonomy = validator.read_json(taxonomy_file)
    source = validator.read_json(directory / "resources.json")
    resources = validator.resources(source)
    assignment_data = validator.read_json(assignment_file)
    taxonomy_hash = validator.taxonomy(taxonomy)["taxonomy_hash"]
    # Require a complete valid run before preparing a partial audit. Unknown IDs
    # and failed/missing classification batches cannot disappear silently.
    validator.assignments(taxonomy, source, assignment_data, taxonomy_hash)
    assignments = {item["resource_id"]: item for item in assignment_data["items"]}
    header = {key: value for key, value in assignment_data.items() if key != "items"}
    holdout_file = directory / "holdout-selection-inputs.json"
    stability_file = directory / "stability-selection.json"
    holdout_ids, stability_ids, fixture_ids = read_exclusions(holdout_file, stability_file)
    v4_holdout_ids, v4_stability_ids, additional_exclusion_sources = read_version_exclusions(directory, taxonomy["version"])
    initial_holdout_ids, initial_stability_ids = set(holdout_ids), set(stability_ids)
    holdout_ids |= v4_holdout_ids
    stability_ids |= v4_stability_ids
    excluded = holdout_ids | stability_ids
    fixture_file = runner.ROOT / "tests" / "fixtures" / "classification-quality-cases.json"
    if not fixture_file.is_file():
        fixture_file = fixture_file.with_name("classification-quality-examples.json")
    regressions, fixture_count = regression_ids(resources, fixture_file, fixture_ids)
    candidates = build_candidates(resources, assignments, excluded, regressions, args.seed)
    reference_profile = validator.read_json(directory / "plan-input.json")["reference_profile"]
    model, model_source, assignment_sources = resolve_model_source(
        assignment_file, directory, assignments, args.model, args.provenance,
        taxonomy_version=assignment_data["taxonomy_version"], taxonomy_hash=taxonomy_hash)
    prompt = runner.system_prompt(args.skill, "audit")
    selected = candidates[:args.max_items]
    reduced = []
    while selected:
        payload = audit_payload(reference_profile, taxonomy, resources, header, assignments, selected, len(resources))
        size = runner.request_character_count(model, prompt, payload)
        if size <= args.max_payload_chars:
            break
        reduced.insert(0, selected.pop())
    if not selected:
        raise ValueError("Full taxonomy and one audit resource do not fit; no evidence was truncated")
    selected_ids = {item["resource_id"] for item in selected}
    if selected_ids & excluded:
        raise ValueError("Independent evaluation resource leaked into audit selection")
    audit_info = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Bounded semantic audit input; no expected labels and no full-library accuracy claim",
        "seed": args.seed,
        "taxonomy_file": str(taxonomy_file.resolve()),
        "taxonomy_sha256": taxonomy_hash,
        "assignments_file": str(assignment_file.resolve()),
        "assignments_sha256": runner.digest(assignment_data),
        "audit_input_sha256": runner.digest(payload),
        "source_resource_count": len(resources),
        "selected_count": len(selected),
        "maximum_items": args.max_items,
        "model": model,
        "model_source": model_source,
        "selected_assignment_sources": {rid: assignment_sources[rid] for rid in sorted(selected_ids)
                                         if rid in assignment_sources},
        "request_characters": size,
        "maximum_request_characters": args.max_payload_chars,
        "request_character_method": "Exact ChatClient json.dumps(payload, ensure_ascii=False), including system prompt and outer request",
        "audit_system_prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "taxonomy_transport": "Full original categories objects; no field or evidence truncation",
        "selected": selected,
        "selected_reason_counts": dict(Counter(item["reason"] for item in selected)),
        "regression_fixture_count": fixture_count,
        "matched_regression_resource_count": len(regressions),
        "excluded_holdout_ids": sorted(holdout_ids),
        "excluded_stability_ids": sorted(stability_ids),
        "exclusion_sets": {"initial_holdout": sorted(initial_holdout_ids),
                           "initial_stability": sorted(initial_stability_ids),
                           "v4_holdout": sorted(v4_holdout_ids), "v4_stability": sorted(v4_stability_ids)},
        "additional_exclusion_sources": additional_exclusion_sources,
        "removed_to_fit_request": reduced,
        "uncovered_resource_ids": sorted(resources.keys() - selected_ids),
        "unverified": [
            "Semantic correctness of all unselected resources",
            "Independent holdout answers and outcomes are neither read nor sent to the audit",
            "Cross-run or input-order stability is not measured by this audit",
            "Semantic accuracy cannot be inferred from this targeted selection",
        ],
    }
    runner.write_json(output / "audit-input.json", payload)
    runner.write_json(output / "audit-selection.json", audit_info)
    print(json.dumps({"audit_input": str(output / "audit-input.json"),
                      "selection": str(output / "audit-selection.json"),
                      "selected": len(selected), "request_characters": size,
                      "reasons": audit_info["selected_reason_counts"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
