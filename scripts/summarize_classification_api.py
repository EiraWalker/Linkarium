#!/usr/bin/env python3
"""Summarize recorded API attempts and deduplicated draft results without API calls.

An attempt request file proves local preparation, not provider acceptance or billing.
Use --stopped-run NAME only for runs the operator actually stopped.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ATTEMPT_SUFFIX = "-request.json"
TRANSIENT = {"AI_TIMEOUT", "AI_RATE_LIMITED", "AI_TEMPORARY_FAILURE"}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def read_json(path, warnings, optional=True):
    if optional and not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as error:
        warnings.append({"file": str(path), "code": type(error).__name__})
        return None


def numeric_leaves(value, prefix=""):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from numeric_leaves(child, f"{prefix}.{key}" if prefix else key)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        yield prefix, value


def usage_summary(attempts):
    sums = Counter()
    counts = Counter()
    with_usage = 0
    for attempt in attempts:
        usage = attempt.get("usage")
        if isinstance(usage, dict):
            with_usage += 1
            for field, value in numeric_leaves(usage):
                sums[field] += value
                counts[field] += 1
    return {
        "attempts_with_reported_usage": with_usage,
        "attempts_without_reported_usage": len(attempts) - with_usage,
        "reported_field_sums": dict(sorted(sums.items())),
        "reporting_attempt_count_per_field": dict(sorted(counts.items())),
        "accounting_rule": "Sum each provider-reported numeric field independently. Preserve reported total_tokens; never reconstruct it or add nested reasoning tokens to it.",
        "billing_limit": "Missing responses or usage do not establish zero usage or zero charges. These records are not an invoice.",
    }


def attempt_summary(attempts):
    return {
        "prepared_attempt_records": len(attempts),
        "response_recorded": sum(a["response_recorded"] for a in attempts),
        "nonempty_response_body_recorded": sum(a["raw_response_bytes"] > 0 for a in attempts),
        "no_response_recorded": sum(not a["response_recorded"] for a in attempts),
        "outcomes": dict(sorted(Counter(a["outcome"] for a in attempts).items())),
        "error_codes": dict(sorted(Counter(a["error_code"] for a in attempts if a["error_code"]).items())),
        "http_statuses": dict(sorted(Counter(str(a["http_status"]) for a in attempts if a["http_status"] is not None).items())),
        "empty_content_responses": sum(a["content_empty"] is True for a in attempts),
        "usage": usage_summary(attempts),
    }


def response_observation(raw_path, metadata_path, warnings):
    metadata = read_json(metadata_path, warnings) or {}
    raw_size = raw_path.stat().st_size if raw_path.exists() else 0
    # Older runner versions did not save metadata; only read the response
    # envelope for accounting, never include message content in this report.
    envelope = read_json(raw_path, warnings) if raw_size else None
    envelope = envelope if isinstance(envelope, dict) else {}
    raw_usage = envelope.get("usage")
    recorded_usage = metadata.get("usage")
    usage = raw_usage if raw_usage is not None else recorded_usage
    choices = envelope.get("choices")
    content_empty = metadata.get("content_empty")
    reasoning_chars = None
    if isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict):
            content = message.get("content")
            content_empty = content is None or content == ""
            reasoning = message.get("reasoning_content")
            if isinstance(reasoning, str):
                reasoning_chars = len(reasoning)
    return {
        "response_recorded": raw_path.exists() or metadata_path.exists(),
        "raw_response_bytes": raw_size,
        "http_status": metadata.get("http_status"),
        "content_empty": content_empty,
        "reasoning_content_characters": reasoning_chars,
        "usage": usage,
        "usage_source": "response_envelope" if raw_usage is not None else "response_metadata" if recorded_usage is not None else None,
        "usage_metadata_matches_envelope": raw_usage == recorded_usage if raw_usage is not None and recorded_usage is not None else None,
    }


def scan_request(folder, run_name, stage, manifest, stopped, root, warnings):
    request = read_json(folder / "request.json", warnings) or {}
    payload = {}
    for message in request.get("messages", []):
        if message.get("role") == "user":
            try:
                payload = json.loads(message["content"])
            except (KeyError, TypeError, ValueError):
                pass
            break
    payload = payload if isinstance(payload, dict) else {}
    payload_taxonomy = payload.get("taxonomy") or {}
    resource_rows = payload.get("resources", [])
    resource_ids = [r["id"] for r in resource_rows if isinstance(r, dict) and isinstance(r.get("id"), str)]
    batch_id = request.get("batch_id", folder.parent.name)
    request_hash = request.get("request_sha256")
    success = read_json(folder / "success.json", warnings)
    result = read_json(folder / "result.json", warnings) if success else None
    accepted = bool(success and success.get("valid") is True and request_hash and success.get("request_sha256") == request_hash and isinstance(result, dict))
    if success and not accepted:
        warnings.append({"file": str(folder / "success.json"), "code": "success_record_not_verified_against_request"})
    attempts = []
    for path in sorted(folder.glob("*-request.json")):
        stem = path.name[:-len(ATTEMPT_SUFFIX)]
        if not re.search(r"-attempt-\d+$", stem):
            continue
        validation = read_json(folder / f"{stem}-validation.json", warnings)
        observation = response_observation(folder / f"{stem}-response.raw", folder / f"{stem}-response.metadata.json", warnings)
        code = validation.get("code") if isinstance(validation, dict) else None
        if validation and validation.get("valid") is True:
            outcome = "valid_result"
        elif code == "AI_TIMEOUT":
            outcome = "timeout"
        elif code in TRANSIENT or (observation["http_status"] is not None and observation["http_status"] != 200):
            outcome = "transport_failure"
        elif validation and validation.get("valid") is False:
            outcome = "invalid_result"
        elif stopped:
            outcome = "operator_stopped_unfinished"
        else:
            outcome = "unfinished_or_interrupted_unknown"
        attempts.append({
            "attempt_id": str(path.relative_to(root)).replace("\\", "/"),
            "run": run_name, "stage": stage, "batch_id": batch_id,
            "request_sha256": request_hash,
            "attempt_number": int(stem.rsplit("-", 1)[-1]),
            "attempt_series": stem.rsplit("-attempt-", 1)[0],
            "model": request.get("model", manifest.get("model")),
            "timeout_seconds": request.get("timeout_seconds"),
            "resource_count": len(resource_ids),
            "outcome": outcome, "error_code": code,
            "elapsed_seconds": validation.get("elapsed_seconds") if validation else None,
            **observation,
        })
    record = {
        "run": run_name, "stage": stage, "batch_id": batch_id,
        "request_sha256": request_hash,
        "request_directory": str(folder.relative_to(root)).replace("\\", "/"),
        "taxonomy_id": payload_taxonomy.get("taxonomy_id"),
        "taxonomy_version": payload_taxonomy.get("version"),
        "taxonomy_hash": request.get("taxonomy_hash"),
        "source_input_sha256": manifest.get("input_sha256"),
        "resource_ids": resource_ids,
        "prepared_attempt_count": len(attempts),
        "accepted_result": accepted,
        "completed_at": success.get("completed_at") if accepted else None,
    }
    if accepted and stage == "classify":
        result_ids = [item.get("resource_id") for item in result.get("items", []) if isinstance(item, dict)]
        if (set(result_ids) != set(resource_ids) or len(result_ids) != len(resource_ids)
                or result.get("taxonomy_hash") != request.get("taxonomy_hash")
                or result.get("taxonomy_version") != record["taxonomy_version"]
                or result.get("taxonomy_id") != record["taxonomy_id"]):
            record["accepted_result"] = False
            warnings.append({"file": str(folder / "result.json"), "code": "classification_coverage_or_taxonomy_mismatch"})
        else:
            record["classification_items"] = [{key: item.get(key) for key in ("resource_id", "category_id", "status", "review_reason")} for item in result["items"]]
    return record, attempts


def classification_coverage(requests, source_ids, source_hash):
    by_taxonomy = defaultdict(list)
    for record in requests:
        if record["stage"] == "classify" and record["accepted_result"] and record["source_input_sha256"] == source_hash:
            key = (record.get("taxonomy_id"), record.get("taxonomy_version"), record["taxonomy_hash"])
            by_taxonomy[key].append(record)
    reports = []
    for (taxonomy_id, taxonomy_version, taxonomy_hash), records in sorted(by_taxonomy.items(), key=lambda item: repr(item[0])):
        by_resource = defaultdict(list)
        for record in records:
            for item in record.get("classification_items", []):
                by_resource[item["resource_id"]].append({
                    **item, "run": record["run"], "batch_id": record["batch_id"],
                    "request_directory": record["request_directory"], "completed_at": record["completed_at"],
                })
        chosen = []
        duplicates = []
        for rid, candidates in sorted(by_resource.items()):
            candidates.sort(key=lambda candidate: (candidate["completed_at"] or "", candidate["request_directory"]))
            chosen.append(candidates[-1])
            if len(candidates) > 1:
                labels = {(c["status"], c["category_id"], c["review_reason"]) for c in candidates}
                duplicates.append({"resource_id": rid, "result_count": len(candidates), "classification_disagrees": len(labels) > 1,
                                   "selected_source": candidates[-1]["request_directory"], "sources": candidates})
        covered = set(by_resource)
        reports.append({
            "taxonomy_id": taxonomy_id,
            "taxonomy_version": taxonomy_version,
            "taxonomy_hash": taxonomy_hash,
            "source_resource_count": len(source_ids),
            "accepted_result_request_count": len(records),
            "sum_of_result_items_before_deduplication": sum(len(r["resource_ids"]) for r in records),
            "unique_resource_count": len(covered & source_ids),
            "missing_resource_ids": sorted(source_ids - covered),
            "unknown_resource_ids": sorted(covered - source_ids),
            "complete_coverage": covered == source_ids,
            "duplicate_resource_count": len(duplicates),
            "duplicate_extra_items": sum(d["result_count"] - 1 for d in duplicates),
            "duplicate_classification_disagreement_count": sum(d["classification_disagrees"] for d in duplicates),
            "deduplicated_status_counts": dict(Counter(c["status"] for c in chosen)),
            "selection_policy": "For accounting coverage only, choose the most recently completed locally valid result per resource within one taxonomy ID, version and hash. Different taxonomy versions remain separate full-library result sets. This does not overwrite or approve any classification artifact.",
            "source_filter": "Only runs whose input hash exactly equals run-root resources.json are full-library runs. Audit/stability/subset experiments are excluded.",
            "duplicates": duplicates,
            "chosen_result_sources": chosen,
        })
    return reports


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--stopped-run", action="append", default=[], metavar="NAME",
                        help="Annotate unfinished attempts in an operator-confirmed stopped run; repeat for multiple runs")
    args = parser.parse_args(argv)
    root = args.run_dir.resolve()
    output = (args.output or root / "api-run-summary.json").resolve()
    warnings = []
    source = read_json(root / "resources.json", warnings, optional=False)
    if not isinstance(source, dict) or not isinstance(source.get("resources"), list):
        raise ValueError("Run-root resources.json is required to identify full-library coverage")
    source_ids = {item["id"] for item in source["resources"]}
    source_hash = digest(source)
    stopped_runs = set(args.stopped_run)
    runs = []
    requests = []
    attempts = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or not (directory / "batches").is_dir():
            continue
        manifest = read_json(directory / "manifest.json", warnings) or {}
        summary = read_json(directory / "summary.json", warnings) or {}
        stage = manifest.get("stage", directory.name.split("-")[0])
        run_requests = []
        run_attempts = []
        for request_path in sorted((directory / "batches").glob("*/*/request.json")):
            record, observations = scan_request(request_path.parent, directory.name, stage, manifest, directory.name in stopped_runs, root, warnings)
            run_requests.append(record)
            run_attempts.extend(observations)
        prepared_batch_numbers = {int(r["batch_id"].rsplit("-", 1)[-1]) for r in run_requests if re.search(r"-\d+$", r["batch_id"])}
        selected_indices = manifest.get("selected_batch_indices") or list(range(1, manifest.get("total_batches", 0) + 1))
        resumed_events = 0
        events_path = directory / "events.jsonl"
        if events_path.exists():
            for line in events_path.read_text(encoding="utf-8-sig").splitlines():
                try:
                    resumed_events += json.loads(line).get("event") == "resumed"
                except ValueError:
                    warnings.append({"file": str(events_path), "code": "incomplete_event_line"})
        run_report = {
            "run": directory.name, "stage": stage, "model": manifest.get("model"),
            "started_at": manifest.get("started_at"), "finished_at": summary.get("finished_at"),
            "runner_summary_complete": summary.get("complete"),
            "operator_confirmed_stopped": directory.name in stopped_runs,
            "selected_batch_indices": selected_indices,
            "no_prepared_request_batch_indices": sorted(set(selected_indices) - prepared_batch_numbers),
            "request_variants": len(run_requests),
            "accepted_result_requests": sum(r["accepted_result"] for r in run_requests),
            "resumed_events_not_new_calls": resumed_events,
            **attempt_summary(run_attempts),
        }
        runs.append(run_report)
        requests.extend(run_requests)
        attempts.extend(run_attempts)
    stage_attempts = defaultdict(list)
    for attempt in attempts:
        stage_attempts[attempt["stage"]].append(attempt)
    by_stage = {stage: {**attempt_summary(items),
                        "accepted_result_requests": sum(r["accepted_result"] for r in requests if r["stage"] == stage)}
                for stage, items in sorted(stage_attempts.items())}
    coverage = classification_coverage(requests, source_ids, source_hash)
    all_counts = attempt_summary(attempts)
    draft = [
        f"截至快照时，记录到 {len(attempts)} 个本地请求尝试，{all_counts['outcomes'].get('valid_result', 0)} 次返回通过当时宿主校验。校验通过不等于分类语义正确。",
        f"记录到超时 {all_counts['outcomes'].get('timeout', 0)} 次、传输失败 {all_counts['outcomes'].get('transport_failure', 0)} 次、返回或结构校验失败 {all_counts['outcomes'].get('invalid_result', 0)} 次。",
        f"明确停止后未完成的尝试 {all_counts['outcomes'].get('operator_stopped_unfinished', 0)} 次；另有 {all_counts['outcomes'].get('unfinished_or_interrupted_unknown', 0)} 次尚无终态记录，可能仍在执行。",
        "token 用量仅汇总响应中实际提供的字段，保留 reasoning 子项与上游 total_tokens 原值；无返回的调用不能据此认定未消耗或未计费。",
    ]
    for group in coverage:
        draft.append(f"目录 v{group['taxonomy_version']}（{group['taxonomy_hash'][:12]}）的全库覆盖按资源 ID 去重后为 {group['unique_resource_count']} / {group['source_resource_count']}，重复结果 {group['duplicate_extra_items']} 条不重复计入覆盖；不同目录版本独立统计，重复调用仍保留在 API 尝试与用量统计中。")
    report = {
        "schema_version": 1, "snapshot_at": datetime.now(timezone.utc).isoformat(), "run_dir": str(root),
        "scope": "Recorded runner attempts under this run directory; may be changing while the report is read. Provider proxy diagnostics and probes stored elsewhere are not included.",
        "counting_notes": [
            "Attempt request files count local preparation; a missing response does not prove upstream acceptance, rejection, or zero billing.",
            "Each attempt path is counted once. Resume events, summary.json copies, combined results and success markers are not additional calls.",
            "Accepted results are supported by matching request/success hashes and result coverage checks, not a new semantic audit.",
            "Only explicit --stopped-run annotations distinguish operator cancellation from unresolved/in-flight attempts.",
            "Stopped local execution does not prove an upstream request was cancelled.",
            "Old classify-v3 and replacement long runs remain separate API calls; full-library coverage is deduplicated by resource ID within each taxonomy ID, version and hash. V3 and V4 are separate coverage sets.",
        ],
        "operator_stopped_run_annotations": sorted(stopped_runs),
        "source_resource_count": len(source_ids), "source_input_sha256": source_hash,
        "totals": all_counts, "stages": by_stage, "runs": runs,
        "full_library_classification_coverage": coverage,
        "attempts": attempts,
        "requests": [{key: value for key, value in r.items() if key != "classification_items"} for r in requests],
        "read_warnings": warnings, "report_draft_zh": draft,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)
    print(json.dumps({"output": str(output), "prepared_attempts": len(attempts), "outcomes": all_counts["outcomes"],
                      "full_library_coverage": [{key: value for key, value in c.items() if key in ("taxonomy_version", "unique_resource_count", "source_resource_count", "duplicate_extra_items", "complete_coverage")} for c in coverage],
                      "warnings": len(warnings)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
