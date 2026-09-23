#!/usr/bin/env python3
"""Release metadata shared by local builds and GitHub Actions (stdlib only)."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

REPO = Path(__file__).resolve().parents[2]
TAG = re.compile(r"iollo-(v?\d{4}\.\d+\.\d+)(?:-[1-9]\d*)?(?:-rc[1-9]\d*)?")
IMAGE = "ghcr.io/webaround-ai/hermes-agent"


def digest(path):
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def git(*args):
    return subprocess.check_output(["git", "-C", str(REPO), *args], text=True).strip()


def provenance(tag):
    match = TAG.fullmatch(tag)
    if not match:
        raise ValueError("Expected iollo-<YYYY.M.D>[-N][-rcN] (optional upstream v prefix)")
    upstream = "v" + match[1].removeprefix("v")
    commit = git("rev-parse", "--verify", f"refs/tags/{upstream}^{{commit}}")
    subprocess.run(["git", "-C", str(REPO), "merge-base", "--is-ancestor", commit, "HEAD"], check=True)
    return {"tag": tag, "upstream_version": upstream, "upstream_commit": commit,
            "image": f"{IMAGE}:{tag}-{git('rev-parse', '--short=12', 'HEAD')}"}


def create(tag, directory, signed=False, require_both=False):
    metadata = provenance(tag)
    assets = []
    for arch in ("arm64", "x86_64"):
        path = directory / f"hermes-runtime-{tag}-macos-{arch}.tar.zst"
        if path.exists():
            assets.append({"name": path.name, "arch": arch, "sha256": digest(path),
                           "size": path.stat().st_size,
                           "signature_name": path.name + ".minisig" if signed else None})
    if not assets or (require_both and len(assets) != 2):
        raise ValueError("Missing runtime archive(s)")
    metadata["assets"] = assets
    (directory / "release.json").write_text(json.dumps(metadata, indent=2) + "\n")
    paths = [directory / asset["name"] for asset in assets] + [directory / "release.json"]
    (directory / "SHA256SUMS").write_text("".join(f"{digest(path)}  {path.name}\n" for path in paths))


def check_archive(archive):
    metadata = json.loads((archive.parent / "release.json").read_text())
    if not TAG.fullmatch(metadata["tag"]):
        raise ValueError("Invalid release tag")
    matches = [asset for asset in metadata["assets"] if asset["name"] == archive.name]
    if len(matches) != 1:
        raise ValueError("Archive must occur exactly once in release.json")
    asset = matches[0]
    if asset["arch"] not in ("arm64", "x86_64"):
        raise ValueError("Unsupported architecture")
    if archive.name != f'hermes-runtime-{metadata["tag"]}-macos-{asset["arch"]}.tar.zst':
        raise ValueError("Archive name does not match release tag/architecture")
    if asset["size"] != archive.stat().st_size or asset["sha256"] != digest(archive):
        raise ValueError("Archive size/SHA256 mismatch")
    signature = asset["signature_name"]
    if signature not in (None, archive.name + ".minisig"):
        raise ValueError("Unexpected signature filename")
    return metadata, asset


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check-tag")
    check.add_argument("tag")
    make = sub.add_parser("create")
    make.add_argument("tag")
    make.add_argument("directory", type=Path)
    make.add_argument("--signed", action="store_true")
    make.add_argument("--require-both", action="store_true")
    archive_check = sub.add_parser("check-archive")
    archive_check.add_argument("archive", type=Path)
    args = parser.parse_args()
    if args.command == "check-tag":
        print(json.dumps(provenance(args.tag), indent=2))
    elif args.command == "create":
        create(args.tag, args.directory, args.signed, args.require_both)
    else:
        _, asset = check_archive(args.archive)
        print(asset["signature_name"] or "UNSIGNED")
