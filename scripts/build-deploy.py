"""Build a cloud deployment bundle from a committed Git revision."""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def run(arguments, cwd):
    subprocess.run(arguments, cwd=cwd, check=True)


def output(arguments, cwd):
    return subprocess.check_output(arguments, cwd=cwd, text=True).strip()


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def build(ref, version, destination):
    project = Path(__file__).resolve().parents[1]
    commit = output(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"], project)
    tree = output(["git", "rev-parse", f"{commit}^{{tree}}"], project)
    npm = shutil.which("npm.cmd") or shutil.which("npm")
    if not npm:
        raise RuntimeError("npm is required to build the frontend")
    destination.mkdir(parents=True, exist_ok=True)
    name = f"Linkarium-{version}-deploy"
    archive_paths = [destination / f"{name}.tar.gz", destination / f"{name}.zip"]
    if any(path.exists() for path in [*archive_paths, destination / "SHA256SUMS.txt"]):
        raise FileExistsError("Release output already exists; choose an empty output directory")
    with tempfile.TemporaryDirectory(prefix=".linkarium-build-", dir=destination) as temporary:
        staging = Path(temporary).resolve()
        source = staging / "source.tar"
        with source.open("wb") as stream:
            subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.eol=lf",
                            "archive", "--format=tar", f"--prefix={name}/", commit],
                           cwd=project, stdout=stream, check=True)
        with tarfile.open(source) as archive:
            archive.extractall(staging, filter="data")
        package = staging / name
        # Git is the source boundary: ignored runtime data and unfinished edits
        # never enter the build, including when this runs in a dirty checkout.
        for path in package.rglob("*"):
            relative = path.relative_to(package)
            if (set(relative.parts) & {"data", ".venv", "node_modules", ".git", "tools", "output"}
                    or (path.name.startswith(".env") and path.name != ".env.example")
                    or path.suffix.lower() in {".pem", ".key", ".p12", ".pfx", ".sqlite3", ".sqlite", ".db"}):
                raise RuntimeError(f"Runtime or credential file in Git export: {relative}")
        run([npm, "ci", "--no-audit", "--no-fund", "--cache", str(destination / "npm-cache")], package / "frontend")
        run([npm, "run", "build"], package / "frontend")
        modules = (package / "frontend" / "node_modules").resolve()
        if not modules.is_relative_to(staging):
            raise RuntimeError("Dependency cleanup escaped the build staging directory")
        shutil.rmtree(modules)
        cloud = package / "deploy" / "cloud"
        shutil.copyfile(cloud / "dockerignore", package / ".dockerignore")
        # Copying the guide keeps the first file users see in sync with the repo.
        guide = (cloud / "README.md").read_text(encoding="utf-8")
        guide = guide.replace("Linkarium-v0.1.0-deploy", name).replace("--version v0.1.0", f"--version {version}")
        guide = guide.replace("output/releases/v0.1.0/", f"output/releases/{version}/")
        for old, new in [("(../nginx.conf.example)", "(deploy/nginx.conf.example)"),
                         ("(../../docs/authentication.md)", "(docs/authentication.md)"),
                         ("(../capture/README.md)", "(deploy/capture/README.md)"),
                         ("(../README.md)", "(deploy/README.md)")]:
            guide = guide.replace(old, new)
        (package / "README-DEPLOY.md").write_text(guide, encoding="utf-8", newline="\n")
        environment = (cloud / ".env.example").read_text(encoding="utf-8")
        environment = environment.replace("LINKARIUM_VERSION=local", f"LINKARIUM_VERSION={version}")
        environment = environment.replace("LINKARIUM_REVISION=unknown", f"LINKARIUM_REVISION={commit}")
        (cloud / ".env.example").write_text(environment, encoding="utf-8", newline="\n")
        manifest = {
            "application": "Linkarium", "version": version, "commit": commit, "git_tree": tree,
            "built_at": datetime.now(timezone.utc).isoformat(),
            "node_version": output(["node", "--version"], project),
            "prebuilt_frontend": True, "includes_personal_data": False,
            "capture_mode": "isolated; host launcher configured separately",
            "source_url": f"https://github.com/EiraWalker/Linkarium/tree/{commit}",
        }
        (package / "release.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        files = sorted(path for path in package.rglob("*") if path.is_file())
        checksums = "".join(f"{sha256(path)}  {path.relative_to(package).as_posix()}\n" for path in files)
        (package / "SHA256SUMS").write_text(checksums, encoding="utf-8", newline="\n")
        with tarfile.open(archive_paths[0], "w:gz", format=tarfile.PAX_FORMAT) as archive:
            archive.add(package, arcname=name)
        shutil.make_archive(str(destination / name), "zip", staging, name)
    (destination / "SHA256SUMS.txt").write_text(
        "".join(f"{sha256(path)}  {path.name}\n" for path in archive_paths), encoding="utf-8", newline="\n")
    for path in archive_paths:
        print(f"{path} ({path.stat().st_size:,} bytes)")
    print(f"Source commit: {commit}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="HEAD", help="Committed Git revision; working tree edits are excluded")
    parser.add_argument("--version", required=True, help="Release tag, for example v0.1.0")
    parser.add_argument("--output", type=Path, help="Empty output directory; defaults to output/releases/<version>")
    args = parser.parse_args()
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:[-.][A-Za-z0-9]+)*", args.version):
        parser.error("version must be a safe release tag such as v0.1.0 or v0.1.0-rc.1")
    project = Path(__file__).resolve().parents[1]
    build(args.ref, args.version, (args.output or project / "output" / "releases" / args.version).resolve())


if __name__ == "__main__":
    main()
