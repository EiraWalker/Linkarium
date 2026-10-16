from dataclasses import asdict, dataclass, field
from pathlib import Path
from uuid import UUID
import re


DEFAULT_CAPTURE_BYTES = 96 * 1024 * 1024
MAX_CAPTURE_BYTES = DEFAULT_CAPTURE_BYTES


class CaptureError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class CaptureRequest:
    attempt_uuid: str
    generation: str
    library_epoch: int
    url: str
    output_dir: str
    variant: str = "desktop"
    mode: str = "capture"
    development: bool = False
    allow_test_hosts: tuple[str, ...] = ()
    deny_hosts: tuple[str, ...] = ()
    timeout_seconds: int = 120
    max_bytes: int = DEFAULT_CAPTURE_BYTES
    monolith_path: str | None = None
    dns_mode: str = "system"
    proxy_url: str = ""

    def validate(self):
        try:
            self.attempt_uuid = str(UUID(self.attempt_uuid))
        except (ValueError, TypeError, AttributeError) as exc:
            raise CaptureError("invalid_request", "Invalid attempt UUID") from exc
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", self.generation):
            raise CaptureError("invalid_request", "Invalid generation identifier")
        if type(self.library_epoch) is not int or self.library_epoch < 0:
            raise CaptureError("invalid_request", "Invalid library epoch")
        if self.variant not in ("desktop", "mobile") or self.mode not in ("capture", "metadata"):
            raise CaptureError("invalid_request", "Invalid capture variant or mode")
        if not 1 <= self.timeout_seconds <= 120 or not 1024 <= self.max_bytes <= MAX_CAPTURE_BYTES:
            raise CaptureError("invalid_request", "Capture budget out of bounds")
        if self.allow_test_hosts and not self.development:
            raise CaptureError("invalid_request", "Fixture hosts are only permitted in explicit development")
        if self.dns_mode not in ('system', 'public-doh'):
            raise CaptureError('invalid_request', 'Unsupported DNS mode')
        if self.dns_mode == 'public-doh' and (not self.development or self.allow_test_hosts):
            raise CaptureError('invalid_request', 'Public DoH is an explicit development-only public-target mode')
        if self.proxy_url:
            if not self.development or self.allow_test_hosts:
                raise CaptureError('invalid_request', 'Local proxy requires public-target development capture')
            from .proxy_config import validate_proxy
            self.proxy_url = validate_proxy(self.proxy_url)
        self.output_dir = str(Path(self.output_dir).resolve())


@dataclass
class CaptureResult:
    artifact_path: str | None
    title: str
    description: str
    text_excerpt: str
    final_url: str
    viewport_width: int
    viewport_height: int
    warnings: list[str] = field(default_factory=list)
    sha256: str | None = None
    byte_size: int = 0
    recipe_version: str = "dynamic-html-v2"
    monolith_version: str | None = None
    preview_path: str | None = None
    preview_sha256: str | None = None
    preview_byte_size: int = 0

    def to_dict(self):
        return asdict(self)
