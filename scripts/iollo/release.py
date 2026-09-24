#!/usr/bin/env python3
"""Release metadata shared by local builds and GitHub Actions (stdlib only)."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

REPO = Path(__file__).resolve().parents[2]
TAG = re.compile(r"iollo-(v?\d{4}\.\d+\.\d+)(?:-[1-9]\d*)?(?:-rc[1-9]\d*)?")
COMMIT = re.compile(r"[0-9a-f]{40}")
BOX_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
IMAGE = "ghcr.io/webaround-ai/hermes-agent"
BOX_BASE = "registry.fly.io/instinct-sandboxes:hermes-base-"
DOWNLOAD = "https://github.com/Webaround-ai/hermes-agent/releases/download/"
SCHEMA = 1


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
    return {"tag": tag, "commit": git("rev-parse", "--verify", "HEAD^{commit}"),
            "upstream_version": upstream, "upstream_commit": commit,
            "image": f"{IMAGE}:{tag}-{git('rev-parse', '--short=12', 'HEAD')}"}


def box_digest(value):
    if value is not None and not BOX_DIGEST.fullmatch(value):
        raise ValueError(f"Invalid box digest {value!r}: expected sha256:<64 lowercase hex>")
    return value


def create(tag, directory, box_digest_value=None, require_both=False):
    source = provenance(tag)
    metadata = {"schema": SCHEMA, "tag": tag, "commit": source["commit"],
                "upstream_version": source["upstream_version"],
                "upstream_commit": source["upstream_commit"], "image": source["image"],
                "box_base_image": BOX_BASE + tag, "box_base_digest": box_digest(box_digest_value)}
    assets = []
    for arch in ("arm64", "x86_64"):
        path = directory / f"hermes-runtime-{tag}-macos-{arch}.tar.zst"
        if path.exists():
            assets.append({"name": path.name, "arch": arch, "sha256": digest(path),
                           "size": path.stat().st_size, "url": f"{DOWNLOAD}{tag}/{path.name}"})
    if not assets or (require_both and len(assets) != 2):
        raise ValueError("Missing runtime archive(s)")
    metadata["assets"] = assets
    (directory / "release.json").write_text(json.dumps(metadata, indent=2) + "\n")
    paths = [directory / asset["name"] for asset in assets] + [directory / "release.json"]
    (directory / "SHA256SUMS").write_text("".join(f"{digest(path)}  {path.name}\n" for path in paths))


def check_archive(archive):
    """Integrity of one archive against the adjacent release.json (rc4 shape or schema 1)."""
    metadata = json.loads((archive.parent / "release.json").read_text())
    if metadata.get("schema", SCHEMA) != SCHEMA:
        raise ValueError("Unsupported release.json schema")
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
    return metadata, asset


def check_train(metadata):
    """Problems with a published schema-1 release.json, one plain sentence each."""
    problems = []
    if metadata.get("schema") != SCHEMA:
        problems.append(f"schema is {metadata.get('schema')!r}, expected {SCHEMA}")
    tag = metadata.get("tag")
    if not isinstance(tag, str) or not TAG.fullmatch(tag):
        problems.append(f"tag {tag!r} is not an iollo-<YYYY.M.D>[-N][-rcN] tag")
        tag = None
    commit = metadata.get("commit")
    if not isinstance(commit, str) or not COMMIT.fullmatch(commit):
        problems.append(f"commit {commit!r} is not a 40-character lowercase hex SHA")
    if tag and metadata.get("box_base_image") != BOX_BASE + tag:
        problems.append(f"box_base_image is {metadata.get('box_base_image')!r}, expected {BOX_BASE + tag!r}")
    base_digest = metadata.get("box_base_digest")
    if base_digest is not None and (not isinstance(base_digest, str) or not BOX_DIGEST.fullmatch(base_digest)):
        problems.append(f"box_base_digest {base_digest!r} is not sha256:<64 lowercase hex> or null")
    if "signature_name" in json.dumps(metadata.get("assets")):
        problems.append("assets still carry signature_name; schema 1 has no signatures")
    assets = metadata.get("assets") if isinstance(metadata.get("assets"), list) else []
    arm64 = [asset for asset in assets if isinstance(asset, dict) and asset.get("arch") == "arm64"]
    if len(arm64) != 1:
        problems.append(f"expected exactly one arm64 asset, found {len(arm64)}")
    for asset in arm64:
        name = asset.get("name")
        if tag and name != f"hermes-runtime-{tag}-macos-arm64.tar.zst":
            problems.append(f"arm64 asset name {name!r} does not match tag {tag}")
        if tag and asset.get("url") != f"{DOWNLOAD}{tag}/{name}":
            problems.append(f"arm64 asset url {asset.get('url')!r} is not this tag's download URL")
        if not isinstance(asset.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", asset["sha256"]):
            problems.append(f"arm64 asset sha256 {asset.get('sha256')!r} is not 64 lowercase hex")
        if not isinstance(asset.get("size"), int) or asset["size"] <= 0:
            problems.append(f"arm64 asset size {asset.get('size')!r} is not a positive byte count")
    return problems


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check-tag")
    check.add_argument("tag")
    make = sub.add_parser("create")
    make.add_argument("tag")
    make.add_argument("directory", type=Path)
    make.add_argument("--box-digest", help="box image digest (sha256:<64 hex>); omitted locally")
    make.add_argument("--require-both", action="store_true")
    archive_check = sub.add_parser("check-archive")
    archive_check.add_argument("archive", type=Path)
    train_check = sub.add_parser("check-train")
    train_check.add_argument("release_json", type=Path)
    args = parser.parse_args()
    if args.command == "check-tag":
        print(json.dumps(provenance(args.tag), indent=2))
    elif args.command == "create":
        try:
            box_digest(args.box_digest)
        except ValueError as error:
            parser.error(str(error))
        create(args.tag, args.directory, args.box_digest, args.require_both)
    elif args.command == "check-archive":
        _, asset = check_archive(args.archive)
        print(f"{asset['name']}: size and SHA-256 match release.json")
    else:
        found = check_train(json.loads(args.release_json.read_text()))
        for problem in found:
            print(problem)
        if found:
            sys.exit(1)
        print(f"{args.release_json}: schema-1 release train manifest OK")
