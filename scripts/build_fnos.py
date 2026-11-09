"""Build an fnOS Native FPK with the official fnpack tool (no pip dependencies)."""
from __future__ import annotations

import argparse
import copy
import gzip
import hashlib
import io
import json
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APPNAME = "electronics-inventory"


def normalize_archive(data, replacements=None):
    """Windows fnpack emits 0666 files; restore Linux permissions in the FPK."""
    output = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as source:
        with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode="w") as target:
                for original in source.getmembers():
                    member = copy.copy(original)
                    if not (member.isfile() or member.isdir()):
                        raise RuntimeError(f"Unexpected archive entry: {member.name}")
                    member.mode = 0o755 if member.isdir() or member.name.startswith("cmd/") else 0o644
                    content = None
                    if member.isfile():
                        content = (replacements or {}).get(member.name)
                        if content is None:
                            content = source.extractfile(original).read()
                        member.size = len(content)
                    target.addfile(member, io.BytesIO(content) if content is not None else None)
    return output.getvalue()


def finalize_fpk(path):
    original = path.read_bytes()
    with tarfile.open(fileobj=io.BytesIO(original), mode="r:gz") as archive:
        app = normalize_archive(archive.extractfile("app.tgz").read())
        manifest = archive.extractfile("manifest").read().decode("utf-8")
    checksum = hashlib.md5(app).hexdigest()  # fnOS FPK payload checksum format.
    manifest, count = re.subn(r"(?m)^(checksum\s*=\s*)[a-fA-F0-9]{32}", lambda m: m[1] + checksum, manifest)
    if count != 1:
        raise RuntimeError("Missing or ambiguous FPK payload checksum")
    final = normalize_archive(original, {"app.tgz": app, "manifest": manifest.encode("utf-8")})
    with tarfile.open(fileobj=io.BytesIO(final), mode="r:gz") as archive:
        assert archive.getmember("cmd/main").mode == 0o755
        assert hashlib.md5(archive.extractfile("app.tgz").read()).hexdigest() == checksum
        with tarfile.open(fileobj=archive.extractfile("app.tgz"), mode="r:gz") as payload:
            for member in payload.getmembers():
                if member.name.endswith((".db", ".db-wal", ".db-shm", ".pyc")):
                    raise RuntimeError(f"Private/generated data in package: {member.name}")
    path.write_bytes(final)


def prepare():
    build_root = (ROOT / ".build" / "fnos").resolve()
    if not build_root.is_relative_to(ROOT.resolve()):
        raise RuntimeError(f"Build directory escapes the workspace: {build_root}")
    stage = build_root / APPNAME
    # Only remove this script's own build output, never source or inventory data.
    if stage.is_symlink() or stage.resolve() != build_root / APPNAME:
        raise RuntimeError(f"Unsafe staging path: {stage}")
    if stage.exists():
        shutil.rmtree(stage)
    shutil.copytree(ROOT / "fnos", stage, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "healthcheck.py"))
    server = stage / "app" / "server"
    server.mkdir(parents=True)
    shutil.copyfile(ROOT / "app.py", server / "app.py")
    shutil.copyfile(ROOT / "bom.py", server / "bom.py")
    shutil.copyfile(ROOT / "fnos" / "healthcheck.py", server / "healthcheck.py")
    static = server / "static"
    static.mkdir()
    for name in ("index.html", "app.js", "projects.js", "style.css", "favicon.svg"):
        shutil.copyfile(ROOT / "static" / name, static / name)
    # fnOS executes lifecycle scripts on Linux: always use LF and executable mode.
    for script in (stage / "cmd").iterdir():
        if script.is_file():
            script.write_text(script.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
            script.chmod(0o755)
    for name in ("privilege", "resource"):
        json.loads((stage / "config" / name).read_text(encoding="utf-8"))
    json.loads((stage / "app" / "ui" / "config").read_text(encoding="utf-8"))
    return stage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fnpack", type=Path, help="Path to the official fnpack executable")
    parser.add_argument("--prepare-only", action="store_true", help="Prepare .build/fnos without running fnpack")
    args = parser.parse_args()
    stage = prepare()
    print(f"Prepared: {stage}", flush=True)
    if args.prepare_only:
        return
    local_tool = ROOT / ".tools" / "fnpack.exe"
    tool = str(args.fnpack.resolve()) if args.fnpack else shutil.which("fnpack")
    if not tool and local_tool.is_file():
        tool = str(local_tool)
    if not tool:
        parser.exit(1, "fnpack not found. Download it from https://developer.fnnas.com/docs/cli/fnpack/ and pass --fnpack.\n")
    output = ROOT / "dist"
    output.mkdir(exist_ok=True)
    subprocess.run([tool, "build", "--directory", str(stage)], cwd=output, check=True)
    fpk = output / f"{APPNAME}.fpk"
    if not fpk.is_file():
        raise RuntimeError(f"fnpack did not produce {fpk}")
    finalize_fpk(fpk)
    print(f"FPK: {fpk} ({fpk.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
