#!/usr/bin/env python3
"""Run resource-classifier through the configured API; save offline drafts only.

The caller must remove private content and URL credentials from input resources.
This runner never mutates bookmarks, folders, proposals, profiles or approvals.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SKILL = Path.home() / ".codex" / "skills" / "resource-classifier"
AUTHORIZATION = {
    "mode": "draft_evaluation",
    "basis": "User requested API classification, quality inspection and iterative optimization.",
    "production_taxonomy_approved": False,
    "business_data_mutation_authorized_by_this_runner": False,
}
TRANSIENT_CODES = {"AI_TIMEOUT", "AI_RATE_LIMITED", "AI_TEMPORARY_FAILURE"}
LOG_LOCK = threading.Lock()
CATEGORY_COLUMNS = ("id", "parent_id", "name", "description", "include", "exclude", "aliases", "assignable")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def event(output, name, **details):
    data = {"at": utc_now(), "event": name, **details}
    with LOG_LOCK:
        with (output / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(canonical(data) + "\n")
        print(canonical(data), flush=True)


def load_validator(skill):
    spec = importlib.util.spec_from_file_location("resource_classifier_validator", skill / "scripts" / "validate_classification.py")
    if spec is None or spec.loader is None:
        raise ValueError("Skill validator could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def compact_taxonomy(taxonomy):
    """Lossless columnar transport; canonical approval/hash input stays unchanged."""
    return {
        **{key: value for key, value in taxonomy.items() if key != "categories"},
        "category_columns": list(CATEGORY_COLUMNS),
        "category_rows": [[category[column] for column in CATEGORY_COLUMNS] for category in taxonomy["categories"]],
    }


def request_character_count(model, prompt, payload):
    """Match ChatClient.complete's outer JSON serialization, including its spaces."""
    request = {"model": model, "messages": [{"role": "system", "content": prompt},
                                          {"role": "user", "content": canonical(payload)}],
               "stream": False, "response_format": {"type": "json_object"}}
    return len(json.dumps(request, ensure_ascii=False))


def bounded_classification_batches(rows, max_items, payload_base, prompt, model, limit):
    """Preserve original order and every field while varying only batch boundaries."""
    batches = []
    current = []
    for row in rows:
        candidate = [*current, row]
        if current and (len(candidate) > max_items or
                        request_character_count(model, prompt, {**payload_base, "resources": candidate}) > limit):
            batches.append((len(batches) + 1, {**payload_base, "resources": current}))
            current = []
        current.append(row)
        size = request_character_count(model, prompt, {**payload_base, "resources": current})
        if size > limit:
            raise ValueError(f"Resource {row['id']} cannot fit without truncation: {size} request characters > {limit}")
    if current:
        batches.append((len(batches) + 1, {**payload_base, "resources": current}))
    return batches


def system_prompt(skill, stage, *, compact=False):
    source = (skill / "prompts" / f"{stage}.md").read_text(encoding="utf-8-sig")
    fence = chr(96) * 3
    match = re.search(r"## System\s+" + fence + r"text\s*\n([\s\S]*?)\n" + fence, source)
    if not match:
        raise ValueError(f"No System text block in {stage} prompt")
    text = match[1].strip()
    if stage == "classify":
        text += (
            "\n宿主补充：本次是用户授权的目录草案离线评测。提供的目录版本已由宿主冻结供本次实验，"
            "不代表用户批准生产目录或条目移动。approved_taxonomy_hash 字段仅用于本次实验内容一致性校验；"
            "仍须原样返回 taxonomy_hash。不得输出批准字段或声称已经修改资源库。"
        )
        if compact:
            text += (
                "\n宿主传输格式说明：taxonomy.categories 使用无损列式表示：category_columns 是字段名称顺序，"
                "category_rows 中每行的值按此顺序逐一对应，解读为原 categories 类别对象数组。"
                "所有类别字段、边界与值均完整保留；只能选择已有 id，禁止发明类别。"
                "approved_taxonomy_hash 来自宿主持有的原始完整目录对象，不要对列式传输对象重新计算或改写。"
            )
    return text


def validate_evidence(v, citations, resource, where, minimum=0):
    v.require(isinstance(citations, list) and minimum <= len(citations) <= 2, f"{where}: expected {minimum}..2 citations")
    for index, citation in enumerate(citations):
        label = f"{where}[{index}]"
        v.object_fields(citation, {"field", "quote"}, label)
        v.string(citation["field"], label + ".field")
        v.require(citation["field"] in v.EVIDENCE_FIELDS, label + ".field: unsupported field")
        v.string(citation["quote"], label + ".quote", nonempty=True)
        quote = v.normalize_whitespace(citation["quote"])
        v.require(3 <= len(quote) <= 240, label + ".quote: expected 3..240 normalized characters")
        value = resource.get(citation["field"], "")
        values = value if isinstance(value, list) else [value]
        v.require(any(quote in v.normalize_whitespace(value) for value in values),
                  label + ".quote: must be literal evidence from one supplied field value")


def validate_distillation(v, payload, result):
    v.object_fields(result, {"batch_id", "items", "candidate_topics"}, "distillation")
    v.require(result["batch_id"] == payload["batch_id"], "distillation.batch_id: mismatch")
    resources = v.resources({"resources": payload["resources"]})
    v.require(isinstance(result["items"], list), "distillation.items: expected array")
    seen, sufficient = set(), set()
    for index, item in enumerate(result["items"]):
        where = f"distillation.items[{index}]"
        v.object_fields(item, {"resource_id", "summary", "primary_use", "topic_terms", "resource_form", "evidence_state", "evidence"}, where)
        v.identity(item["resource_id"], where + ".resource_id")
        resource_id = item["resource_id"]
        v.require(resource_id in resources and resource_id not in seen, where + ".resource_id: unknown or duplicate")
        seen.add(resource_id)
        for field in ("summary", "primary_use", "resource_form"):
            v.string(item[field], where + "." + field, nonempty=field == "summary", limit=1000)
        v.strings(item["topic_terms"], where + ".topic_terms")
        v.require(len(item["topic_terms"]) <= 4, where + ".topic_terms: maximum 4")
        v.require(item["evidence_state"] in ("sufficient", "insufficient", "conflicting"), where + ".evidence_state: invalid")
        minimum = int(item["evidence_state"] == "sufficient")
        validate_evidence(v, item["evidence"], resources[resource_id], where + ".evidence", minimum)
        if minimum:
            sufficient.add(resource_id)
    v.require(seen == resources.keys(), "distillation.items: missing resource IDs " + ",".join(sorted(resources.keys() - seen)))
    v.require(isinstance(result["candidate_topics"], list), "distillation.candidate_topics: expected array")
    supported = set()
    for index, topic in enumerate(result["candidate_topics"]):
        where = f"distillation.candidate_topics[{index}]"
        v.object_fields(topic, {"label", "description", "support_ids"}, where)
        v.string(topic["label"], where + ".label", nonempty=True, limit=200)
        v.string(topic["description"], where + ".description", nonempty=True, limit=1000)
        v.strings(topic["support_ids"], where + ".support_ids")
        ids = topic["support_ids"]
        v.require(bool(ids) and len(set(ids)) == len(ids), where + ".support_ids: expected nonempty distinct IDs")
        v.require(set(ids) <= seen, where + ".support_ids: unknown resource")
        supported.update(ids)
    v.require(sufficient <= supported, "distillation.candidate_topics: sufficient resources missing topic coverage " + ",".join(sorted(sufficient - supported)))
    return {"resources": len(seen), "sufficient": len(sufficient), "candidate_topics": len(result["candidate_topics"])}


def validate_audit(v, payload, result):
    v.object_fields(result, {"verdict", "scope", "findings", "metrics", "taxonomy_changes", "unverified"}, "audit")
    v.require(result["verdict"] in ("pass", "revise", "insufficient_evidence"), "audit.verdict: invalid")
    v.string(result["scope"], "audit.scope", nonempty=True)
    for field in ("findings", "metrics"):
        v.require(isinstance(result[field], list), f"audit.{field}: expected array")
    for field in ("taxonomy_changes", "unverified"):
        v.strings(result[field], f"audit.{field}")
    resources = payload.get("resources", [])
    if isinstance(resources, dict):
        resources = resources.get("resources", [])
    resource_ids = {resource["id"] for resource in resources}
    category_ids = {category["id"] for category in payload.get("taxonomy", {}).get("categories", [])}
    for index, finding in enumerate(result["findings"]):
        where = f"audit.findings[{index}]"
        v.object_fields(finding, {"resource_ids", "category_ids", "kind", "evidence", "recommendation"}, where)
        for field, known in (("resource_ids", resource_ids), ("category_ids", category_ids)):
            v.strings(finding[field], where + "." + field)
            v.require(set(finding[field]) <= known, where + "." + field + ": unknown IDs")
        v.require(finding["kind"] in ("boundary", "coverage", "synonym", "granularity", "inconsistency", "evidence", "uncertainty"), where + ".kind: invalid")
        for field in ("evidence", "recommendation"):
            v.string(finding[field], where + "." + field, nonempty=True)
    for index, metric in enumerate(result["metrics"]):
        where = f"audit.metrics[{index}]"
        v.object_fields(metric, {"name", "numerator", "denominator", "basis"}, where)
        for field in ("name", "basis"):
            v.string(metric[field], where + "." + field, nonempty=True)
        for field in ("numerator", "denominator"):
            value = metric[field]
            v.require(value is None or (type(value) is int and value >= 0), where + "." + field + ": expected nonnegative integer or null")
    return {"verdict": result["verdict"], "findings": len(result["findings"])}


def validate_result(v, stage, payload, result, taxonomy, taxonomy_hash):
    if stage == "distill":
        return validate_distillation(v, payload, result)
    if stage == "classify":
        return v.assignments(taxonomy, {"resources": payload["resources"]}, result, taxonomy_hash)
    if stage == "plan":
        validation = v.taxonomy(result)
        for input_field, result_field in (("taxonomy_id", "taxonomy_id"), ("next_version", "version")):
            if input_field in payload:
                v.require(result[result_field] == payload[input_field], "taxonomy." + result_field + ": differs from host request")
        return validation
    return validate_audit(v, payload, result)


def response_metadata(body, status_code):
    """Keep provider accounting intact; it may include non-additive token fields."""
    metadata = {"http_status": status_code, "response_bytes": len(body),
                "usage": None, "content_empty": None}
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError):
        return metadata
    if not isinstance(value, dict):
        return metadata
    metadata["usage"] = value.get("usage")
    choices = value.get("choices")
    if isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict):
            content = message.get("content")
            metadata["content_empty"] = content is None or content == ""
            metadata["content_chars"] = len(content) if isinstance(content, str) else None
        metadata["finish_reason"] = choices[0].get("finish_reason")
    return metadata


def recording_transport(path, secret):
    """Preserve bounded response bytes even when ChatClient rejects their JSON."""
    import httpx

    class RecordingStream(httpx.SyncByteStream):
        def __init__(self, stream, status_code):
            self.stream = stream
            self.status_code = status_code
            self.body = bytearray()

        def __iter__(self):
            for chunk in self.stream:
                remaining = 2 * 1024 * 1024 - len(self.body)
                if remaining > 0:
                    self.body.extend(chunk[:remaining])
                yield chunk

        def close(self):
            try:
                self.stream.close()
            finally:
                body = bytes(self.body).replace(secret.encode("utf-8"), b"[REDACTED_API_KEY]")
                path.write_bytes(body)
                write_json(path.with_suffix(".metadata.json"), response_metadata(body, self.status_code))

    class RecordingTransport(httpx.BaseTransport):
        def __init__(self):
            self.inner = httpx.HTTPTransport()

        def handle_request(self, request):
            response = self.inner.handle_request(request)
            if response.status_code != 200:
                # ChatClient rejects HTTP errors before consuming their bodies.
                # Capture a bounded body here so their diagnostics are not lost.
                body = bytearray()
                try:
                    for chunk in response.iter_raw():
                        remaining = 2 * 1024 * 1024 - len(body)
                        body.extend(chunk[:remaining])
                        if len(body) >= 2 * 1024 * 1024:
                            break
                finally:
                    response.close()
                    safe_body = bytes(body).replace(secret.encode("utf-8"), b"[REDACTED_API_KEY]")
                    path.write_bytes(safe_body)
                    write_json(path.with_suffix(".metadata.json"), response_metadata(safe_body, response.status_code))
                return httpx.Response(response.status_code, headers=response.headers,
                                      stream=httpx.ByteStream(bytes(body)), extensions=response.extensions)
            return httpx.Response(response.status_code, headers=response.headers,
                                  stream=RecordingStream(response.stream, response.status_code), extensions=response.extensions)

        def close(self):
            self.inner.close()

    return RecordingTransport()


def execute_batch(args, validator, prompt, batch_number, payload, taxonomy, taxonomy_hash):
    from django.db import connections
    from library.classification import verified_client
    from library.ai_transport import AIError, endpoint_fingerprint

    started = time.monotonic()
    batch_id = f"{args.stage}-{batch_number:04d}"
    folder = None
    try:
        profile, client = verified_client()
        client.output_mode = "json_object"
        client.total_timeout = args.timeout
        messages = [{"role": "system", "content": prompt}, {"role": "user", "content": canonical(payload)}]
        request = {"stage": args.stage, "batch_id": batch_id, "model": client.model,
                   "endpoint_fingerprint": endpoint_fingerprint(client.base_url), "output_mode": "json_object",
                   "timeout_seconds": args.timeout, "messages": messages, "authorization_context": AUTHORIZATION,
                   "taxonomy_hash": taxonomy_hash,
                   "validator_sha256": hashlib.sha256((args.skill / "scripts" / "validate_classification.py").read_bytes()).hexdigest(),
                   "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        if args.stage == "classify":
            request["taxonomy_transport_mode"] = "columnar_lossless" if getattr(args, "compact_taxonomy", False) else "object"
            request["taxonomy_projection_sha256"] = digest(payload["taxonomy"])
        request_hash = digest(request)
        folder = args.output / "batches" / batch_id / request_hash[:16]
        folder.mkdir(parents=True, exist_ok=True)
        success = folder / "success.json"
        if success.exists():
            previous = validator.read_json(success)
            if previous.get("request_sha256") == request_hash:
                result = validator.read_json(folder / "result.json")
                info = validate_result(validator, args.stage, payload, result, taxonomy, taxonomy_hash)
                event(args.output, "resumed", batch_id=batch_id, request_sha256=request_hash)
                return {"batch_id": batch_id, "valid": True, "resumed": True, "result": result,
                        "validation": info, "directory": str(folder), "request_sha256": request_hash}
        write_json(folder / "request.json", {**request, "request_sha256": request_hash})
        event(args.output, "started", batch_id=batch_id, model=client.model, request_sha256=request_hash)
        repairs = retries = 0
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        for attempt in range(1, 4):
            attempt_stem = f"{run_id}-attempt-{attempt}"
            write_json(folder / f"{attempt_stem}-request.json", {"model": client.model, "messages": messages})
            client.transport = recording_transport(folder / f"{attempt_stem}-response.raw", client.api_key)
            attempt_started = time.monotonic()
            try:
                parsed = client.complete(messages)
                write_json(folder / f"{attempt_stem}-parsed.json", parsed)
                info = validate_result(validator, args.stage, payload, parsed, taxonomy, taxonomy_hash)
            except (AIError, validator.Invalid) as error:
                code = getattr(error, "code", "STRUCTURAL_VALIDATION")
                detail = str(error).replace(client.api_key, "[REDACTED_API_KEY]")
                write_json(folder / f"{attempt_stem}-validation.json", {"valid": False, "code": code, "error": detail,
                           "elapsed_seconds": round(time.monotonic() - attempt_started, 3)})
                event(args.output, "attempt_failed", batch_id=batch_id, attempt=attempt, code=code, error=detail)
                if code in TRANSIENT_CODES and retries < 1:
                    retries += 1
                    time.sleep(2)
                    continue
                if code in ("AI_INVALID_RESULT", "STRUCTURAL_VALIDATION") and repairs < 1:
                    repairs += 1
                    messages = request["messages"] + [{"role": "user", "content": (
                        "前次返回未通过宿主校验，请重新返回这个批次的完整 JSON，不要遗漏任何输入 ID。"
                        "具体校验错误：" + detail + "。特别检查每个引用字段和原文：quote 必须在对应字段单个值中逐字存在，"
                        "规范化空白后长度必须为 3–240 字符；两字题名请引用完整 URL 或实际存在的较长字段。"
                        "没有证据时诚实使用不确定状态，不编造文本。修复结构时不要改变既定目录版本。"
                    )}]
                    continue
                return {"batch_id": batch_id, "valid": False, "error": detail, "code": code,
                        "directory": str(folder), "request_sha256": request_hash}
            write_json(folder / f"{attempt_stem}-validation.json", {"valid": True, "details": info,
                       "elapsed_seconds": round(time.monotonic() - attempt_started, 3)})
            write_json(folder / "result.json", parsed)
            write_json(success, {"valid": True, "request_sha256": request_hash, "completed_at": utc_now(),
                       "validation": info, "authorization_context": AUTHORIZATION})
            event(args.output, "completed", batch_id=batch_id, elapsed_seconds=round(time.monotonic() - started, 3))
            return {"batch_id": batch_id, "valid": True, "resumed": False, "result": parsed,
                    "validation": info, "directory": str(folder), "request_sha256": request_hash}
        return {"batch_id": batch_id, "valid": False, "code": "RETRY_BUDGET_EXHAUSTED", "directory": str(folder)}
    except Exception as error:
        code = getattr(error, "code", type(error).__name__)
        event(args.output, "batch_failed", batch_id=batch_id, code=code)
        return {"batch_id": batch_id, "valid": False, "code": code, "directory": str(folder) if folder else None}
    finally:
        connections.close_all()


def run_batches(batches, workers, execute, on_result=None):
    """Bound in-flight work and stop scheduling after two final transient failures."""
    results = []
    pending = iter(batches)
    scheduled = set()
    transient_streak = []
    circuit_break = None
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="classification-draft") as pool:
        futures = {}

        def schedule_one():
            try:
                number, payload = next(pending)
            except StopIteration:
                return False
            scheduled.add(number)
            futures[pool.submit(execute, number, payload)] = number
            return True

        for _ in range(workers):
            if not schedule_one():
                break
        while futures:
            done, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in sorted(done, key=lambda item: futures[item]):
                futures.pop(future)
                result = future.result()
                results.append(result)
                if on_result:
                    on_result(results)
                if not result["valid"] and result.get("code") in TRANSIENT_CODES:
                    transient_streak.append({"batch_id": result["batch_id"], "code": result["code"]})
                else:
                    transient_streak.clear()
                if len(transient_streak) >= 2 and circuit_break is None:
                    circuit_break = {"reason": "two_consecutive_final_transient_failures",
                                     "trigger_batches": list(transient_streak)}
            if circuit_break is None:
                while len(futures) < workers and schedule_one():
                    pass
    pending_indices = [number for number, _ in batches if number not in scheduled]
    return results, pending_indices, circuit_break


def usage_observations(results):
    observations = []
    for result in results:
        if not result.get("directory"):
            continue
        for path in sorted(Path(result["directory"]).glob("*-response.metadata.json")):
            try:
                metadata = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeError):
                continue
            observations.append({"batch_id": result["batch_id"], "artifact": str(path), **metadata})
    return observations


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("distill", "plan", "classify", "audit"), required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--taxonomy", type=Path)
    parser.add_argument("--compact-taxonomy", action="store_true",
                        help="Classify only: transmit all taxonomy fields using a lossless category column/row representation")
    parser.add_argument("--max-payload-chars", type=int,
                        help="Classify only: split batches before this ChatClient request character limit without truncation")
    parser.add_argument("--skill", type=Path, default=DEFAULT_SKILL)
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--workers", type=int, choices=(1, 2), default=2)
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--indices", help="Optional comma-separated 1-based batch indices for a bounded subset")
    args = parser.parse_args(argv)
    if args.compact_taxonomy and args.stage != "classify":
        parser.error("--compact-taxonomy is only supported for --stage classify")
    if args.max_payload_chars is not None:
        if args.stage != "classify":
            parser.error("--max-payload-chars is only supported for --stage classify")
        if not 1000 <= args.max_payload_chars <= 60000:
            parser.error("--max-payload-chars must be 1000..60000")
    if args.timeout <= 0 or args.timeout > 600:
        parser.error("--timeout must be between 0 and 600 seconds")
    args.batch_size = args.batch_size or (20 if args.stage == "distill" else 10)
    if args.batch_size < 1 or args.batch_size > 100:
        parser.error("--batch-size must be 1..100")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    validator = load_validator(args.skill)
    source = validator.read_json(args.input)
    prompt = system_prompt(args.skill, args.stage, compact=args.compact_taxonomy)
    taxonomy = validator.read_json(args.taxonomy) if args.taxonomy else None
    taxonomy_hash = validator.taxonomy(taxonomy)["taxonomy_hash"] if taxonomy else None
    if args.stage == "classify" and taxonomy is None:
        parser.error("--taxonomy is required for classification")
    taxonomy_projection = compact_taxonomy(taxonomy) if args.compact_taxonomy else taxonomy
    if args.stage in ("distill", "classify"):
        validator.resources(source)
        rows = source["resources"]
        if not rows:
            parser.error("Resource list must not be empty")
        batches = []
        for offset in range(0, len(rows), args.batch_size):
            number = offset // args.batch_size + 1
            payload = {"resources": rows[offset:offset + args.batch_size]}
            if args.stage == "distill":
                payload["batch_id"] = f"distill-{number:04d}"
            else:
                payload.update(taxonomy=taxonomy_projection, approved_taxonomy_hash=taxonomy_hash,
                               existing_tags=[], human_corrections=[])
            batches.append((number, payload))
    else:
        if not isinstance(source, dict):
            parser.error("Plan/audit input must be an object")
        batches = [(1, source)]
    sys.path.insert(0, str(ROOT / "backend"))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django
    django.setup()
    from django.db import connections
    from library.classification import verified_client
    profile, client = verified_client()
    model = client.model
    del client, profile
    connections.close_all()
    if args.max_payload_chars is not None:
        payload_base = {"taxonomy": taxonomy_projection, "approved_taxonomy_hash": taxonomy_hash,
                        "existing_tags": [], "human_corrections": []}
        batches = bounded_classification_batches(rows, args.batch_size, payload_base, prompt, model, args.max_payload_chars)
    total_batches = len(batches)
    batch_sizes = [{"index": number, "resources": len(payload.get("resources", [])),
                    "request_characters": request_character_count(model, prompt, payload)}
                   for number, payload in batches] if args.stage == "classify" else None
    if args.indices:
        try:
            indices = {int(index) for index in args.indices.split(",")}
        except ValueError:
            parser.error("--indices must contain comma-separated positive integers")
        if not indices or not indices <= set(range(1, total_batches + 1)):
            parser.error("--indices contains an unknown batch number")
        batches = [(number, payload) for number, payload in batches if number in indices]
    manifest = {"started_at": utc_now(), "stage": args.stage, "input": str(args.input.resolve()),
                "input_sha256": digest(source), "taxonomy_hash": taxonomy_hash, "model": model,
                "batch_size": args.batch_size, "workers": args.workers, "total_batches": total_batches,
                "selected_batch_indices": [number for number, _ in batches], "authorization_context": AUTHORIZATION}
    if args.stage == "classify":
        manifest["taxonomy_transport_mode"] = "columnar_lossless" if args.compact_taxonomy else "object"
        manifest["taxonomy_projection_sha256"] = digest(taxonomy_projection)
        manifest["max_payload_characters"] = args.max_payload_chars
        manifest["batch_sizes"] = batch_sizes
    write_json(args.output / "manifest.json", manifest)
    def update_progress(results):
        write_json(args.output / "progress.json", {"stage": args.stage, "finished": len(results), "total": len(batches),
                   "valid": sum(row["valid"] for row in results), "updated_at": utc_now()})

    results, pending_indices, circuit_break = run_batches(
        batches, args.workers,
        lambda number, payload: execute_batch(args, validator, prompt, number, payload, taxonomy, taxonomy_hash),
        update_progress,
    )
    if circuit_break:
        event(args.output, "scheduling_stopped", pending_batches=len(pending_indices), **circuit_break)
    results.sort(key=lambda row: row["batch_id"])
    valid = [row for row in results if row["valid"]]
    failed = [{key: value for key, value in row.items() if key != "result"} for row in results if not row["valid"]]
    if args.stage == "distill":
        combined = {"items": [item for row in valid for item in row["result"]["items"]],
                    "candidate_topics": [topic for row in valid for topic in row["result"]["candidate_topics"]]}
    elif args.stage == "classify":
        combined = {"taxonomy_id": taxonomy["taxonomy_id"], "taxonomy_version": taxonomy["version"],
                    "taxonomy_hash": taxonomy_hash, "items": [item for row in valid for item in row["result"]["items"]]}
    else:
        combined = valid[0]["result"] if valid else None
    write_json(args.output / "combined.json", combined)
    summary = {**manifest, "finished_at": utc_now(), "selected_batches": len(batches), "valid_batches": len(valid),
               "resumed_batches": sum(row.get("resumed", False) for row in valid), "failed_batches": failed,
               "pending_batch_indices": pending_indices, "circuit_break": circuit_break,
               "complete": not failed and not pending_indices and len(batches) == total_batches,
               "response_observations": usage_observations(results),
               "batch_results": [{key: value for key, value in row.items() if key != "result"} for row in results]}
    if args.stage == "classify":
        summary["status_counts"] = dict(Counter(item["status"] for item in combined["items"]))
        summary["category_counts"] = dict(Counter(item["category_id"] for item in combined["items"] if item["category_id"]))
    write_json(args.output / "summary.json", summary)
    event(args.output, "stage_finished", stage=args.stage, valid_batches=len(valid), failed_batches=len(failed),
          complete=summary["complete"], summary=str(args.output / "summary.json"))
    return int(bool(failed or pending_indices))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
