"""Integrity and archive-boundary contracts for Iollo release tooling."""
import importlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile

import pytest

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts/iollo"


def test_checksums_and_signatures_are_required_before_boot(tmp_path, monkeypatch):
    if not all(shutil.which(tool) for tool in ("zstd", "minisign", "bash")):
        pytest.skip("Install zstd and minisign to exercise release signature verification")
    monkeypatch.syspath_prepend(str(SCRIPTS))
    release = importlib.import_module("release")
    bundle = importlib.import_module("bundle")
    tag = "iollo-2026.9.14"
    name = f"hermes-runtime-{tag}-macos-arm64"
    root = tmp_path / name
    (root / "bin").mkdir(parents=True)
    marker = tmp_path / "booted"
    launcher = root / "bin/hermes"
    launcher.write_text(f'#!/bin/sh\ntouch "{marker}"\n')
    launcher.chmod(0o755)
    (root / "VERSION").write_text(tag + "\n")
    (root / "MANIFEST.json").write_text(json.dumps({"files": bundle.inventory(root)}))
    tar_path = tmp_path / "runtime.tar"
    with tarfile.open(tar_path, "w") as archive:
        archive.add(root, arcname=name)
    archive = tmp_path / (name + ".tar.zst")
    subprocess.run(["zstd", "-q", str(tar_path), "-o", str(archive)], check=True)
    metadata = {"tag": tag, "assets": [{"name": archive.name, "arch": "arm64",
                "sha256": release.digest(archive), "size": archive.stat().st_size, "signature_name": None}]}
    release_path = tmp_path / "release.json"
    release_path.write_text(json.dumps(metadata))

    def verify(*args, succeeds):
        marker.unlink(missing_ok=True)
        result = subprocess.run([str(SCRIPTS / "verify-bundle.sh"), str(archive), *args],
                                capture_output=True, text=True)
        assert (result.returncode == 0) is succeeds, result.stdout + result.stderr
        assert marker.exists() is succeeds

    verify(succeeds=True)
    original = archive.read_bytes()
    archive.write_bytes(original + b"tampered")
    verify(succeeds=False)
    archive.write_bytes(original)
    public = tmp_path / "test.pub"
    secret = tmp_path / "test.key"
    subprocess.run(["minisign", "-G", "-W", "-p", str(public), "-s", str(secret)], check=True, capture_output=True)
    verify(str(public), succeeds=False)  # A trusted key forbids unsigned downgrade.
    metadata["assets"][0]["signature_name"] = archive.name + ".minisig"
    release_path.write_text(json.dumps(metadata))
    for path in (archive, release_path):
        subprocess.run(["minisign", "-S", "-W", "-s", str(secret), "-m", str(path)], check=True, capture_output=True)
    verify(str(public), succeeds=True)
    verify(succeeds=False)  # Signed metadata cannot silently fall back to checksums.
    release_path.write_text(release_path.read_text() + " ")
    verify(str(public), succeeds=False)  # Metadata itself must authenticate too.
    secret.unlink()


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
