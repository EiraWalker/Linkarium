"""Only content-addressed objects may be read or removed from a generation."""
import re
from config.paths import generation_root
from .errors import ApiError


def artifact_path(sha256, relative_path=None, generation=None):
    if not isinstance(sha256, str) or re.fullmatch(r"[0-9a-f]{64}", sha256) is None:
        raise ApiError("INVALID_ARTIFACT", "归档文件标识无效")
    canonical = f"artifacts/{sha256[:2]}/{sha256}.html"
    if relative_path is not None and relative_path != canonical:
        raise ApiError("INVALID_ARTIFACT", "归档文件路径无效")
    root = generation_root(generation)
    candidate = root / canonical
    if any(path.is_symlink() for path in (root / "artifacts", candidate.parent, candidate)) or not candidate.resolve().is_relative_to(root.resolve()):
        raise ApiError("INVALID_ARTIFACT", "归档文件路径超出收藏库")
    return candidate
