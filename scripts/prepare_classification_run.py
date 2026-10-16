"""Read an HTML export and cached public metadata; write isolated evaluation inputs."""
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import sys
from collections import Counter
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
import django
django.setup()
from library.importing import parse_bookmark_html, canonicalize_import_url
from library.models import Bookmark, PageMetadata
from library.classification import verified_client


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def safe_url(url):
    parsed = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
             if not any(word in k.casefold() for word in ("token", "password", "secret", "signature", "auth", "session", "key", "code"))]
    # Fragments can contain authentication state; not needed for topic induction.
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def private_url(url):
    host = (urlsplit(url).hostname or "").casefold()
    if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        return True
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        return False


def main():
    source = Path(sys.argv[1])
    output = Path(sys.argv[2])
    output.mkdir(parents=True, exist_ok=True)
    if (output / "resources.json").exists():
        raise SystemExit("Input snapshot exists; use a new output directory")
    evidence_path = ROOT / "output/classification-v2-draft/backend/library/classification_evidence.py"
    spec = importlib.util.spec_from_file_location("library.evidence_for_evaluation", evidence_path)
    evidence = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(evidence)
    sites = {b.canonical_url: b for b in Bookmark.objects.all()}
    metadata = {str(m.bookmark_id): m for m in PageMetadata.objects.all()}
    parsed = parse_bookmark_html(source.read_bytes())
    seen, resources, originals, skipped, excluded = set(), [], {}, [], []
    top_counts = Counter()
    matched, cached, polluted = 0, 0, 0
    for entry in parsed:
        if entry.error_code:
            skipped.append({"title": entry.title, "reason": entry.error_code})
            continue
        canonical = canonicalize_import_url(entry.url)
        if canonical in seen:
            skipped.append({"title": entry.title, "reason": "duplicate_url"})
            continue
        seen.add(canonical)
        identity = hashlib.sha256(canonical.encode()).hexdigest()[:16]
        originals[identity] = {"url": entry.url, "title": entry.title, "source_path": entry.original_folder_path}
        if private_url(canonical):
            excluded.append({"resource_id": identity, "reason": "private_network_url"})
            continue
        site = sites.get(canonical)
        meta = metadata.get(str(site.id)) if site else None
        matched += bool(site)
        is_polluted = bool(meta and evidence.metadata_is_interstitial(meta.title, meta.description, meta.text_excerpt))
        polluted += is_polluted
        usable = meta is not None and not is_polluted
        cached += usable
        warnings = ["cached_public_metadata"] if usable else ["insufficient_metadata"]
        if is_polluted:
            warnings.append("interstitial")
        row = {"id": identity, "title": entry.title[:500], "url": safe_url(canonical),
               "source_titles": [meta.title[:500]] if usable and meta.title and meta.title != entry.title else [],
               "description": meta.description[:500] if usable else "",
               "text_excerpt": meta.text_excerpt[:1600] if usable else "",
               "notes": "", "tags": [],
               "source_folder_paths": [entry.original_folder_path] if entry.original_folder_path else [],
               "evidence_warnings": warnings}
        resources.append(row)
        key = entry.original_folder_path[1] if len(entry.original_folder_path) > 1 else "未分组"
        top_counts[key] += 1
    profile, _ = verified_client()
    save(output / "resources.json", {"resources": resources})
    save(output / "originals.local.json", originals)
    save(output / "excluded.local.json", excluded)
    save(output / "skipped.local.json", skipped)
    summary = {"source": str(source), "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
               "parsed": len(parsed), "unique_public": len(resources), "skipped": len(skipped),
               "excluded_private": len(excluded), "matched_library": matched,
               "usable_cached_metadata": cached, "interstitial_metadata_excluded": polluted,
               "source_folder_counts": dict(top_counts), "configured_model": profile.model,
               "api_host": urlsplit(profile.base_url).netloc,
               "authorization": "User requested API classification and iterative evaluation of this HTML export. Output files only; no database moves or approval of proposals."}
    save(output / "input-summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
