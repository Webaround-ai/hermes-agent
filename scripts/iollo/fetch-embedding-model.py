#!/usr/bin/env python3
"""Place the iollo_notes embedding model into a release, pinned by SHA-256 (fork brief 003).

Build time only: the release workflow runs this before building the box image and inside the Mac
runtime build, so the model ships in both artifacts and the runtime never downloads it. Every
file is checked against plugins/memory/iollo_notes/model.json (size and SHA-256); a mismatch
fails the build. Standard library only; works on Apple's Python 3.9.

Usage: fetch-embedding-model.py <destination-directory> [--verify-only]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import urllib.request

REPO = Path(__file__).resolve().parents[2]
SPEC = REPO / "plugins/memory/iollo_notes/model.json"


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def problems(directory, spec):
    out = []
    for name, meta in spec["files"].items():
        path = directory / name
        if not path.is_file():
            out.append(f"{name}: missing")
        elif path.stat().st_size != meta["size"] or digest(path) != meta["sha256"]:
            out.append(f"{name}: does not match the pinned size/sha256")
    return out


def fetch(directory, spec, opener=urllib.request.urlopen):
    directory.mkdir(parents=True, exist_ok=True)
    for name, meta in spec["files"].items():
        target = directory / name
        if target.is_file() and target.stat().st_size == meta["size"] and digest(target) == meta["sha256"]:
            continue
        url = spec["url"].format(name=spec["name"], revision=spec["revision"], source=meta["source"])
        if not url.startswith("https://"):
            raise SystemExit(f"refusing non-https model URL for {name}")
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".fetch-", suffix=".part")
        try:
            h, size = hashlib.sha256(), 0
            with os.fdopen(fd, "wb") as out, opener(url, timeout=120) as response:
                for chunk in iter(lambda: response.read(1 << 20), b""):
                    size += len(chunk)
                    if size > meta["size"]:
                        raise SystemExit(f"{name}: larger than the pinned {meta['size']} bytes")
                    h.update(chunk)
                    out.write(chunk)
            if size != meta["size"] or h.hexdigest() != meta["sha256"]:
                raise SystemExit(f"{name}: downloaded file does not match the pinned sha256 {meta['sha256']}")
            os.chmod(tmp, 0o644)
            os.replace(tmp, target)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("destination", type=Path)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(argv)
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    if not args.verify_only:
        fetch(args.destination, spec)
    found = problems(args.destination, spec)
    if found:
        print("Embedding model check failed: " + "; ".join(found), file=sys.stderr)
        return 1
    print(f"Embedding model {spec['name']}@{spec['revision'][:12]} verified in {args.destination}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
