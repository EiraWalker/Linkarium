import uuid
from django.http import JsonResponse


def client_ip(request):
    # Waitress strips untrusted proxy headers. Only its resolved peer is used.
    return request.META.get("REMOTE_ADDR")


def error_response(code, message, status, request_id=None, **details):
    error = {"code": code, "message": message, "request_id": request_id or str(uuid.uuid4())}
    error.update(details)
    return JsonResponse({"error": error}, status=status)


def csrf_failure(request, reason=""):
    return error_response("CSRF_FAILED", "会话校验已失效，请重新登录或刷新页面", 403)


def axes_lockout(request, response=None, credentials=None, *args, **kwargs):
    result = error_response("LOGIN_LOCKED", "尝试过多，请在 15 分钟后重试", 429)
    result["Retry-After"] = "900"
    return result


class ResponsePolicyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.request_id = getattr(request, "request_id", None) or str(uuid.uuid4())
        response = self.get_response(request)
        response["X-Request-ID"] = request.request_id
        response["X-Content-Type-Options"] = "nosniff"
        response["Referrer-Policy"] = "no-referrer"
        if request.path.startswith("/api/") and "Cache-Control" not in response:
            response["Cache-Control"] = "private, no-store"
        if "Content-Security-Policy" not in response:
            response["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
        if "X-Frame-Options" not in response:
            response["X-Frame-Options"] = "DENY"
        return response


class PerformanceMiddleware:
    """Measure response construction and body iteration as separate boundaries."""
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from runtime import performance
        if not performance.enabled():
            return self.get_response(request)
        try:
            request.request_id = str(uuid.uuid4())
        except Exception:
            return self.get_response(request)
        totals = {"sql_count": 0, "sql_ms": 0.0}
        with performance.trace("http.request", request_id=request.request_id,
                method=request.method) as measurement:
            try:
                measurement.route = _performance_route(request)
            except Exception:
                pass
            with _performance_sql(totals):
                response = self.get_response(request)
            elapsed = measurement.elapsed_ms
            try:
                response["X-Request-ID"] = request.request_id
                existing = response.get("Server-Timing")
                timing = f"app;dur={elapsed:.3f}, sql;dur={totals['sql_ms']:.3f}"
                response["Server-Timing"] = f"{existing}, {timing}" if existing else timing
                size = int(response.get("Content-Length", 0)) if response.streaming else len(response.content)
                measurement.set_fields(status_code=response.status_code, response_bytes=size, **totals)
                if response.streaming:
                    _performance_stream(response, measurement)
            except Exception:
                pass
        return response


def _performance_route(request):
    import re
    path = request.path
    known_pages = {"/", "/login", "/library", "/classification", "/settings", "/transfers", "/health/live", "/health/ready"}
    if path in known_pages:
        return path
    if path.startswith("/assets/"):
        return "/assets/:asset"
    if not path.startswith("/api/v1/"):
        return ":unmatched"
    route = path[len("/api/v1/"):]
    fixed = {"session", "session/login", "session/logout", "data/clear", "data/reset",
        "runtime/status", "capture-network", "settings", "bookmarks", "bookmarks/delete-preview",
        "bookmarks/bulk", "folders", "tags", "job-groups", "jobs", "snapshots/delete-preview",
        "snapshots/bulk-delete", "ai-profiles", "classification/batches", "classification/proposals",
        "classification/accept", "classification/reject", "imports", "imports/preview", "backups",
        "restores/validate", "maintenance-operations/lookup"}
    if route in fixed:
        return f"/api/v1/{route}"
    identifier = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    patterns = {
        "bookmarks": ("", "/refresh", "/snapshots"),
        "folders": ("", "/refresh-preview", "/refresh", "/delete-preview"),
        "tags": ("",), "job-groups": ("",), "jobs": ("", "/cancel", "/retry"),
        "snapshots": ("/thumbnail", "/preview"), "ai-profiles": ("", "/test"),
        "imports": ("", "/execute", "/review-notified"), "classification/proposals": ("",),
        "backups": ("", "/download"), "restores": ("", "/execute", "/pre-backup/download"),
        "maintenance-operations": ("",),
    }
    for prefix, suffixes in patterns.items():
        for suffix in suffixes:
            allowed = f"(?:{identifier}|all)" if prefix == "folders" else identifier
            if re.fullmatch(re.escape(prefix) + "/" + allowed + re.escape(suffix), route):
                return f"/api/v1/{prefix}/:id{suffix}"
    return "/api/v1/:unmatched"


from contextlib import contextmanager


@contextmanager
def _performance_sql(totals):
    from contextlib import ExitStack
    import time
    from django.db import connections

    def measure(execute, sql, params, many, context):
        try:
            started = time.perf_counter()
        except Exception:
            return execute(sql, params, many, context)
        try:
            return execute(sql, params, many, context)
        finally:
            try:
                totals["sql_count"] += 1
                totals["sql_ms"] += (time.perf_counter() - started) * 1000
            except Exception:
                pass

    stack = ExitStack()
    try:
        for alias in connections:
            try:
                stack.enter_context(connections[alias].execute_wrapper(measure))
            except Exception:
                pass
        yield
    finally:
        try:
            stack.close()
        except Exception:
            pass


def _performance_stream(response, parent):
    from runtime import performance
    original = response.streaming_content

    if response.is_async:
        async def iterate():
            byte_count = 0
            with performance.linked_span("http.stream", parent) as measurement:
                try:
                    async for chunk in original:
                        byte_count += len(chunk)
                        yield chunk
                finally:
                    measurement.set_fields(response_bytes=byte_count)
        response.streaming_content = iterate()
    else:
        def iterate():
            byte_count = 0
            totals = {"sql_count": 0, "sql_ms": 0.0}
            with performance.linked_span("http.stream", parent) as measurement:
                try:
                    with _performance_sql(totals):
                        for chunk in original:
                            byte_count += len(chunk)
                            yield chunk
                finally:
                    measurement.set_fields(response_bytes=byte_count, **totals)
        response.streaming_content = iterate()
