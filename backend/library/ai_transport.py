"""A small, bounded Chat Completions adapter. Never execute model output."""
from __future__ import annotations

import hashlib
import json
import re
import time
from urllib.parse import urlsplit, urlunsplit

import httpx

from runtime import performance

PROTOCOL = "chat-completions"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_EXCERPT_CHARS = 6000
MAX_BATCH_ITEMS = 10
MAX_INPUT_CHARS = 48000


class AIError(ValueError):
    def __init__(self, code: str, message: str = "AI request could not be completed"):
        self.code = code
        super().__init__(message)


def normalize_endpoint(base_url: str) -> str:
    if not isinstance(base_url, str) or len(base_url) > 2000:
        raise AIError("AI_ENDPOINT_INVALID")
    try:
        parsed = urlsplit(base_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError
        if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            raise ValueError
        host = parsed.hostname.encode("idna").decode("ascii").lower()
        if ":" in host:
            host = f"[{host}]"
        port = parsed.port
        if port and port != {"http": 80, "https": 443}[parsed.scheme]:
            host += f":{port}"
        path = parsed.path.rstrip("/")
        if any(segment in {".", ".."} for segment in path.split("/")):
            raise ValueError
        return urlunsplit((parsed.scheme.lower(), host, path, "", ""))
    except (ValueError, UnicodeError):
        raise AIError("AI_ENDPOINT_INVALID", "API address must be an HTTP(S) base URL without credentials") from None


def endpoint_fingerprint(base_url: str, protocol: str = PROTOCOL) -> str:
    if protocol != PROTOCOL:
        raise AIError("AI_PROTOCOL_UNSUPPORTED")
    value = normalize_endpoint(base_url) + "\n" + protocol
    return hashlib.sha256(value.encode()).hexdigest()


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AIError("AI_INVALID_RESULT", "Duplicate JSON keys are not accepted")
        result[key] = value
    return result


def strict_json(text: str, *, allow_fence=False):
    try:
        if allow_fence:
            fenced = re.fullmatch(r"```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n```", text.strip(), re.IGNORECASE)
            if fenced:
                text = fenced[1]
        return json.loads(text, object_pairs_hook=_no_duplicate_keys,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (ValueError, TypeError, RecursionError):
        raise AIError("AI_INVALID_RESULT", "Expected a valid JSON object") from None


class ChatClient:
    def __init__(self, base_url: str, api_key: str, model: str,
                 output_mode: str = "json_object", *, transport=None, total_timeout: float = 120):
        self.base_url = normalize_endpoint(base_url)
        if not api_key or len(api_key) > 8192 or "\n" in api_key or "\r" in api_key:
            raise AIError("AI_UNCONFIGURED")
        if not isinstance(model, str) or not model.strip() or len(model) > 200:
            raise AIError("AI_MODEL_INVALID")
        if output_mode not in {"json_object", "json_schema", "text"}:
            raise AIError("AI_OUTPUT_MODE_INVALID")
        self.api_key, self.model, self.output_mode = api_key, model, output_mode
        self.transport, self.total_timeout = transport, total_timeout

    def complete(self, messages: list[dict], *, schema: dict | None = None) -> dict:
        with performance.span("ai.complete", category="ai"):
            payload = {"model": self.model, "messages": messages, "stream": False}
            if self.output_mode == "json_object":
                payload["response_format"] = {"type": "json_object"}
            elif self.output_mode == "json_schema":
                if not schema:
                    raise AIError("AI_SCHEMA_REQUIRED")
                payload["response_format"] = {"type": "json_schema", "json_schema": {
                    "name": "bookmark_classification", "strict": True, "schema": schema}}
            if len(json.dumps(payload, ensure_ascii=False)) > MAX_INPUT_CHARS + 12000:
                raise AIError("AI_INPUT_TOO_LARGE")
            # Non-streaming models may spend most of the response budget generating
            # before sending headers. Do not impose a separate 30s read ceiling.
            timeout = httpx.Timeout(self.total_timeout, connect=min(10, self.total_timeout),
                                    write=min(10, self.total_timeout), pool=min(5, self.total_timeout))
            started = time.monotonic()
            try:
                with performance.span("ai.request", category="ai"):
                    with httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False,
                                      transport=self.transport) as client:
                        with client.stream("POST", self.base_url + "/chat/completions", json=payload,
                                           headers={"Authorization": "Bearer " + self.api_key,
                                                    "Accept": "application/json"}) as response:
                            performance.emit("ai.response_headers", category="ai", http_status=response.status_code)
                            if response.status_code == 429:
                                raise AIError("AI_RATE_LIMITED")
                            if response.status_code >= 500:
                                raise AIError("AI_TEMPORARY_FAILURE")
                            if response.status_code != 200:
                                raise AIError("AI_HTTP_REJECTED")
                            body = bytearray()
                            for chunk in response.iter_bytes():
                                body.extend(chunk)
                                if len(body) > MAX_RESPONSE_BYTES:
                                    raise AIError("AI_RESPONSE_TOO_LARGE")
                                if time.monotonic() - started > self.total_timeout:
                                    raise AIError("AI_TIMEOUT")
                with performance.span("ai.parse", category="ai", response_bytes=len(body)):
                    result = strict_json(body.decode("utf-8"))
                    choices = result.get("choices") if isinstance(result, dict) else None
                    if not isinstance(choices, list) or len(choices) != 1:
                        raise AIError("AI_INVALID_RESULT")
                    message = choices[0].get("message", {})
                    if message.get("tool_calls") or message.get("function_call") or message.get("refusal"):
                        raise AIError("AI_INVALID_RESULT")
                    content = message.get("content")
                    if not isinstance(content, str):
                        raise AIError("AI_INVALID_RESULT")
                    parsed = strict_json(content, allow_fence=True)
                    if not isinstance(parsed, dict):
                        raise AIError("AI_INVALID_RESULT")
                    return parsed
            except httpx.TimeoutException:
                raise AIError("AI_TIMEOUT") from None
            except httpx.HTTPError:
                raise AIError("AI_TEMPORARY_FAILURE") from None
            except (UnicodeError, AttributeError, IndexError, KeyError, TypeError):
                raise AIError("AI_INVALID_RESULT") from None

    def test(self) -> dict:
        value = self.complete([
            {"role": "system", "content": "Return only a JSON object: {\"ok\":true}. No tools."},
            {"role": "user", "content": "Verify this API supports a non-streaming JSON response."},
        ], schema={"type": "object", "properties": {"ok": {"type": "boolean"}},
                   "required": ["ok"], "additionalProperties": False})
        if value != {"ok": True}:
            raise AIError("AI_INVALID_RESULT")
        return {"protocol": PROTOCOL, "output_mode": self.output_mode, "model": self.model}
