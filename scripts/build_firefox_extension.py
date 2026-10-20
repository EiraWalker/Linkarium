"""Package compiled Firefox sources; test origins are limited to loopback builds."""
import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit
import zipfile

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "extensions" / "firefox"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-origin")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    origin = "https://link.eirawav.me"
    manifest = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))
    if args.test_origin:
        parsed = urlsplit(args.test_origin)
        if (parsed.scheme not in ("http", "https") or parsed.hostname not in ("localhost", "127.0.0.1")
                or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
            parser.error("test origin must be a loopback HTTP(S) origin without path or credentials")
        origin = args.test_origin
        manifest["name"] += "（隔离测试）"
        manifest["browser_specific_settings"]["gecko"]["id"] = "linkarium-test@eirawav.me"
        pattern = f"{parsed.scheme}://{parsed.hostname}/*"
        manifest["host_permissions"] = [pattern]
        manifest["content_scripts"][0]["matches"] = [pattern]
    destination = (args.output or EXTENSION / "artifacts" / ("linkarium-firefox-test.xpi" if args.test_origin else "linkarium-firefox-unsigned.xpi")).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    compiled = [EXTENSION / "dist" / (name + ".js") for name in ("common", "background", "bridge", "status")]
    if not all(path.is_file() for path in compiled):
        parser.error("compiled sources missing; run npm --prefix extensions/firefox run build")
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for path in [*compiled, *(EXTENSION / name for name in ("icon.svg", "status.html", "status.css"))]:
            archive.writestr(path.name, path.read_text(encoding="utf-8").replace("https://link.eirawav.me", origin))
    print(json.dumps({"path": str(destination), "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
        "test_build": bool(args.test_origin), "signed": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
