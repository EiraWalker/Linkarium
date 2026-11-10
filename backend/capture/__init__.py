"""Bounded dynamic HTML capture; has no dependency on Django."""
from .models import CaptureError, CaptureRequest, CaptureResult
from .client import LauncherClient

__all__ = ["CaptureError", "CaptureRequest", "CaptureResult", "capture", "LauncherClient"]


def __getattr__(name):
    # Reading shared models/limits must work in stdlib-only offline tools.
    # Keep the public capture function while loading Playwright only on demand.
    if name == "capture":
        from .runner import capture
        globals()[name] = capture
        return capture
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
