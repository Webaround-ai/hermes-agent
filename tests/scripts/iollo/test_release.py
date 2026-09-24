"""Integrity and archive-boundary contracts for Iollo release tooling."""
import importlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

import pytest

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts/iollo"


TAG = "iollo-2026.9.14-rc5"
COMMIT = "a" * 40
DIGEST = "sha256:" + "b" * 64
ARM64 = f"hermes-runtime-{TAG}-macos-arm64.tar.zst"
DOWNLOAD = "https://github.com/Webaround-ai/hermes-agent/releases/download"
RC4 = {  # The published iollo-2026.9.14-rc4 release.json shape (signed, no schema field).
    "tag": "iollo-2026.9.14-rc4", "upstream_version": "v2026.9.14",
    "upstream_commit": "345cd2b057a452236de401d3534b8502a7465e8d",
    "image": "ghcr.io/webaround-ai/hermes-agent:iollo-2026.9.14-rc4-807f4c612f05",
}


def load_release(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    return importlib.import_module("release")


def fake_provenance(tag):
    return {"tag": tag, "commit": COMMIT, "upstream_version": "v2026.9.14",
            "upstream_commit": "c" * 40, "image": f"ghcr.io/webaround-ai/hermes-agent:{tag}-aaaaaaaaaaaa"}


def run_release(*args):
    return subprocess.run([sys.executable, str(SCRIPTS / "release.py"), *args], capture_output=True, text=True)


def good_train():
    return {"schema": 1, "tag": TAG, "commit": COMMIT, "upstream_version": "v2026.9.14",
            "upstream_commit": "c" * 40, "image": f"ghcr.io/webaround-ai/hermes-agent:{TAG}-aaaaaaaaaaaa",
            "box_base_image": f"registry.fly.io/instinct-sandboxes:hermes-base-{TAG}",
            "box_base_digest": DIGEST,
            "assets": [{"name": ARM64, "arch": "arm64", "sha256": "d" * 64, "size": 10,
                        "url": f"{DOWNLOAD}/{TAG}/{ARM64}"}]}


def test_create_writes_schema_1_train_manifest(tmp_path, monkeypatch):
    release = load_release(monkeypatch)
    monkeypatch.setattr(release, "provenance", fake_provenance)
    (tmp_path / ARM64).write_bytes(b"runtime")
    release.create(TAG, tmp_path, DIGEST)
    metadata = json.loads((tmp_path / "release.json").read_text())
    assert metadata["schema"] == 1
    assert metadata["commit"] == COMMIT
    assert metadata["box_base_image"] == f"registry.fly.io/instinct-sandboxes:hermes-base-{TAG}"
    assert metadata["box_base_digest"] == DIGEST
    [asset] = metadata["assets"]
    assert asset == {"name": ARM64, "arch": "arm64", "sha256": release.digest(tmp_path / ARM64),
                     "size": 7, "url": f"{DOWNLOAD}/{TAG}/{ARM64}"}
    assert "signature_name" not in (tmp_path / "release.json").read_text()
    assert release.check_train(metadata) == []
    sums = (tmp_path / "SHA256SUMS").read_text().splitlines()
    assert [line.split("  ")[1] for line in sums] == [ARM64, "release.json"]
    release.create(TAG, tmp_path)  # Local builds have no pushed image, so no digest.
    assert json.loads((tmp_path / "release.json").read_text())["box_base_digest"] is None


def test_invalid_box_digest_is_rejected(tmp_path, monkeypatch):
    release = load_release(monkeypatch)
    monkeypatch.setattr(release, "provenance", fake_provenance)
    (tmp_path / ARM64).write_bytes(b"runtime")
    for bad in ("", "sha256:" + "b" * 63, "sha256:" + "B" * 64, "b" * 64, "sha512:" + "b" * 64):
        with pytest.raises(ValueError):
            release.create(TAG, tmp_path, bad)
        assert not (tmp_path / "release.json").exists()
    result = run_release("create", TAG, str(tmp_path), "--box-digest", "sha256:nothex")
    assert result.returncode == 2 and "Invalid box digest" in result.stderr


def test_check_archive_accepts_rc4_and_schema_1(tmp_path, monkeypatch):
    release = load_release(monkeypatch)
    for tag, extra in ((RC4["tag"], RC4), (TAG, {k: v for k, v in good_train().items() if k != "assets"})):
        directory = tmp_path / tag
        directory.mkdir()
        archive = directory / f"hermes-runtime-{tag}-macos-arm64.tar.zst"
        archive.write_bytes(b"runtime")
        asset = {"name": archive.name, "arch": "arm64", "sha256": release.digest(archive), "size": 7}
        asset.update({"signature_name": archive.name + ".minisig"} if tag == RC4["tag"]
                     else {"url": f"{DOWNLOAD}/{tag}/{archive.name}"})
        (directory / "release.json").write_text(json.dumps({**extra, "assets": [asset]}))
        assert release.check_archive(archive)[1] == asset
        archive.write_bytes(b"runtimf")
        with pytest.raises(ValueError, match="SHA256 mismatch"):
            release.check_archive(archive)


def test_check_train_passes_good_and_names_each_problem(tmp_path, monkeypatch):
    release = load_release(monkeypatch)
    good = tmp_path / "good.json"
    good.write_text(json.dumps(good_train()))
    result = run_release("check-train", str(good))
    assert result.returncode == 0, result.stdout + result.stderr
    bad = good_train()
    bad["commit"] = "abc123"
    bad["box_base_image"] = "registry.fly.io/instinct-sandboxes:hermes-base-iollo-2026.9.14-rc4"
    bad["assets"][0]["url"] = f"{DOWNLOAD}/iollo-2026.9.14-rc4/{ARM64}"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(bad))
    result = run_release("check-train", str(path))
    assert result.returncode != 0
    lines = result.stdout.splitlines()
    assert len(lines) == 3, lines
    assert any(line.startswith("commit") for line in lines)
    assert any(line.startswith("box_base_image") for line in lines)
    assert any("url" in line and "this tag" in line for line in lines)
    no_arm64 = good_train()
    no_arm64["assets"][0]["arch"] = "x86_64"
    assert release.check_train(no_arm64) == ["expected exactly one arm64 asset, found 0"]
    bad_digest = {**good_train(), "box_base_digest": "sha256:short"}
    assert [p.split()[0] for p in release.check_train(bad_digest)] == ["box_base_digest"]
    assert release.check_train({**good_train(), "box_base_digest": None}) == []


def test_checksums_are_required_before_boot(tmp_path, monkeypatch):
    if not all(shutil.which(tool) for tool in ("zstd", "bash")):
        pytest.skip("Install zstd to exercise release integrity verification")
    release = load_release(monkeypatch)
    bundle = importlib.import_module("bundle")
    name = ARM64.removesuffix(".tar.zst")
    root = tmp_path / name
    (root / "bin").mkdir(parents=True)
    marker = tmp_path / "booted"
    launcher = root / "bin/hermes"
    launcher.write_text(f'#!/bin/sh\ntouch "{marker}"\n')
    launcher.chmod(0o755)
    (root / "VERSION").write_text(TAG + "\n")
    (root / "MANIFEST.json").write_text(json.dumps({"files": bundle.inventory(root)}))
    tar_path = tmp_path / "runtime.tar"
    with tarfile.open(tar_path, "w") as archive:
        archive.add(root, arcname=name)
    archive = tmp_path / ARM64
    subprocess.run(["zstd", "-q", str(tar_path), "-o", str(archive)], check=True)
    monkeypatch.setattr(release, "provenance", fake_provenance)
    release.create(TAG, tmp_path)

    def verify(*args, succeeds):
        marker.unlink(missing_ok=True)
        result = subprocess.run([str(SCRIPTS / "verify-bundle.sh"), str(archive), *args],
                                capture_output=True, text=True)
        assert (result.returncode == 0) is succeeds, result.stdout + result.stderr
        assert marker.exists() is succeeds
        return result

    assert "UNSIGNED" not in verify(succeeds=True).stderr
    original = archive.read_bytes()
    archive.write_bytes(original + b"tampered")
    verify(succeeds=False)  # Size/SHA-256 mismatch stops before extraction or boot.
    archive.write_bytes(original)
    verify("some.pub", succeeds=False)  # No key argument any more.
    (root / "VERSION").write_text("iollo-2026.9.14-rc4\n")
    (root / "MANIFEST.json").write_text(json.dumps({"files": bundle.inventory(root)}))
    with tarfile.open(tar_path, "w") as rebuilt:
        rebuilt.add(root, arcname=name)
    subprocess.run(["zstd", "-qf", str(tar_path), "-o", str(archive)], check=True)
    release.create(TAG, tmp_path)
    verify(succeeds=False)  # The embedded VERSION must match release.json.


def test_extraction_refuses_paths_and_links_outside_the_runtime(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    bundle = importlib.import_module("bundle")
    for index, (name, link) in enumerate([
        ("../escape", None), ("/escape", None), ("runtime/link", "../../escape"),
        ("runtime/link/child", None),
    ]):
        tar_path = tmp_path / f"unsafe-{index}.tar"
        with tarfile.open(tar_path, "w") as archive:
            if name == "runtime/link/child":
                parent = tarfile.TarInfo("runtime/link")
                parent.type = tarfile.SYMTYPE
                parent.linkname = "inside"
                archive.addfile(parent)
            member = tarfile.TarInfo(name)
            if link:
                member.type = tarfile.SYMTYPE
                member.linkname = link
            else:
                member.size = 1
            archive.addfile(member, None if link else io.BytesIO(b"x"))
        destination = tmp_path / f"extracted-{index}"
        destination.mkdir()
        with pytest.raises(ValueError):
            bundle.extract(tar_path, destination, "runtime")
        assert not list(destination.iterdir())
