"""Bounded capture evidence: never persist URLs, page text, headers or exception text."""
from contextvars import ContextVar
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time
import traceback
from urllib.parse import urlsplit

_active = ContextVar('capture_diagnostics', default=None)
REASONS = {
    'Target page requires access or rate-limit recovery': 'http_access_denied',
    'Target page does not exist': 'http_not_found',
    'Target page requires human verification': 'human_verification_required',
    'Target page resources require human verification': 'resource_human_verification_required',
    'Local proxy connection failed': 'local_proxy_connection_failed',
    'Page DOM exceeds capture node budget': 'dom_node_budget',
    'Archive contains broken images': 'archive_broken_images',
    'Archive introduced broken first-screen images': 'archive_new_first_screen_broken_images',
    'Archive lost its readable page content': 'archive_text_missing',
    'Archive attempted external network access': 'archive_external_requests',
    'Target page did not return a successful document': 'http_document_rejected',
    'Page did not become ready within its capture budget': 'browser_stage_timeout',
    'Capture process exceeded its total time budget': 'process_deadline',
    'Capture exceeded its time budget': 'capture_deadline',
    'Visible media could not be frozen safely': 'visible_media_not_frozen',
    'Frozen HTML exceeds capture byte budget': 'frozen_html_byte_budget',
    'Monolith output exceeds capture byte budget': 'archive_byte_budget',
    'Final HTML exceeds capture byte budget': 'final_html_byte_budget',
    'Resource exceeds per-resource byte budget': 'single_resource_byte_budget',
    'Capture resource byte budget exceeded': 'resources_byte_budget',
    'Capture network byte budget exceeded': 'network_byte_budget',
    'Capture response exceeds byte budget': 'proxy_response_byte_budget',
    'Capture proxy concurrent connection limit exceeded': 'proxy_concurrent_connections',
    'Capture proxy connection budget exceeded': 'proxy_connection_budget',
    'Capture proxy request headers exceed budget': 'proxy_request_headers_budget',
    'Capture HTTP headers exceed byte budget': 'proxy_headers_byte_budget',
    'Capture request body exceeds budget': 'request_body_budget',
    'Capture request limit exceeded': 'request_count_budget',
}
SIGNATURES = {
    'Execution context was destroyed': 'execution_context_destroyed',
    'Cannot find context with specified id': 'execution_context_missing',
    'Target page, context or browser has been closed': 'browser_target_closed',
    'DOM exceeds capture node budget': 'dom_node_budget',
    'Timeout ': 'timeout',
    'ReferenceError:': 'javascript_reference_error',
    'TypeError:': 'javascript_type_error',
}


def host_only(url):
    try:
        return (urlsplit(url).hostname or '')[:253]
    except ValueError:
        return ''


def mime_only(value):
    """Keep only a conventional media type, never parameters or arbitrary headers."""
    mime = value.split(';', 1)[0].strip().lower()
    return mime if re.fullmatch(r'[a-z0-9!#$&^_.+-]{1,48}/[a-z0-9!#$&^_.+-]{1,48}', mime) else ''


def error_evidence(error):
    chain, seen = [], set()
    while error is not None and id(error) not in seen and len(chain) < 5:
        seen.add(id(error))
        chain.append({
            'type': type(error).__name__,
            'code': re.sub(r'[^a-zA-Z0-9_]', '', str(getattr(error, 'code', '')))[:64],
            'reason': REASONS.get(str(error), ''),
            'signatures': [value for key, value in SIGNATURES.items() if key in str(error)],
            'network_codes': sorted(set(re.findall(r'net::ERR_[A-Z_]+', str(error))))[:10],
            'frames': [{'file': Path(f.filename).name, 'function': f.name, 'line': f.lineno}
                       for f in traceback.extract_tb(error.__traceback__)[-8:]],
        })
        error = error.__cause__ or error.__context__
    return chain


def event(stage, **facts):
    trace = _active.get()
    if trace is not None:
        trace.event(stage, **facts)


def metrics(**facts):
    """Record bounded aggregate counters without replacing the failure stage."""
    trace = _active.get()
    if trace is not None:
        trace.body['metrics'] = facts
        trace.save()


def bind_event():
    """Carry this attempt's recorder into callbacks running in another greenlet."""
    trace = _active.get()

    def record(stage, **facts):
        if trace is not None:
            trace.event(stage, **facts)

    return record


def bind_proxy_http_responses():
    """Bind a bounded proxy summary, flushed after its handler threads stop."""
    trace = _active.get()

    def record(summary):
        if trace is not None and summary['total']:
            # Keep the capture's stage, counters and original error chain intact.
            trace.body['proxy_http_responses'] = summary
            trace.save()

    return record


class CaptureTrace:
    def __init__(self, path, request):
        self.path = Path(path)
        self.started = time.monotonic()
        self.body = {'schema_version': 1, 'attempt_id': request.attempt_uuid,
                     'generation': request.generation, 'host': host_only(request.url),
                     'variant': request.variant, 'dns_mode': request.dns_mode,
                     'timeout_seconds': request.timeout_seconds, 'max_bytes': request.max_bytes,
                     'started_at': datetime.now(timezone.utc).isoformat(),
                     'state': 'running', 'stage': 'starting', 'events': []}

    def __enter__(self):
        self.token = _active.set(self)
        self.event('starting')
        return self

    def save(self):
        # Diagnostics must not make the capture fail, including on a full disk.
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix('.tmp')
            temporary.write_text(json.dumps(self.body, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(self.path)
        except OSError:
            pass

    def event(self, stage, **facts):
        self.body['stage'] = stage
        self.body['events'].append({'stage': stage, 'elapsed_seconds': round(time.monotonic()-self.started, 3), **facts})
        self.body['events'] = self.body['events'][-80:]
        self.save()

    def __exit__(self, kind, error, tb):
        self.body['state'] = 'failed' if error else 'succeeded'
        self.body['elapsed_seconds'] = round(time.monotonic()-self.started, 3)
        if error:
            self.body['errors'] = error_evidence(error)
        self.save()
        _active.reset(self.token)


def development_trace_path(request):
    """Only the conventional development spool gets automatic persistent diagnostics."""
    directory = Path(request.output_dir).resolve()
    if (not request.development or directory.name != request.attempt_uuid
            or directory.parent.name != 'capture' or directory.parent.parent.name != 'work'):
        return None
    root = directory.parent.parent.parent / 'diagnostics' / 'capture'
    # Bound disk use; never remove capture output, only our UUID-named trace files.
    try:
        if root.exists():
            files = sorted(root.glob('????????-????-????-????-????????????.json'), key=lambda p: p.stat().st_mtime, reverse=True)
            for index, path in enumerate(files):
                if index >= 999 or time.time()-path.stat().st_mtime > 14*86400:
                    path.unlink()
    except OSError:
        pass
    return root / (request.attempt_uuid + '.json')
