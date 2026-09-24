"""The iollo_notes embedding model ships in the release, pinned by SHA-256 (fork brief 003):
the fetch step verifies every byte, and both release paths (Mac runtime, box image) run it."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "scripts/iollo"
SPEC = json.loads((REPO / "plugins/memory/iollo_notes/model.json").read_text(encoding="utf-8"))


def load_fetch():
    spec = importlib.util.spec_from_file_location("fetch_embedding_model", SCRIPTS / "fetch-embedding-model.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_spec(payloads):
    files = {name: {"source": f"src/{name}", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
             for name, data in payloads.items()}
    return {"name": "org/model", "revision": "r" * 40, "url": "https://example.invalid/{name}/{revision}/{source}",
            "files": files}


def opener_for(served, seen):
    def opener(url, timeout):
        seen.append(url)
        return io.BytesIO(served[url.rsplit("/", 1)[-1]])
    return opener


def test_pinned_spec_is_complete():
    assert SPEC["name"] == "sentence-transformers/all-MiniLM-L6-v2" and SPEC["dim"] == 384
    assert len(SPEC["revision"]) == 40 and SPEC["url"].startswith("https://huggingface.co/")
    assert set(SPEC["files"]) == {"model.onnx", "tokenizer.json"}
    for meta in SPEC["files"].values():
        assert len(meta["sha256"]) == 64 and int(meta["sha256"], 16) >= 0 and meta["size"] > 0
    assert 20_000_000 < SPEC["files"]["model.onnx"]["size"] < 30_000_000  # the int8 model


def test_fetch_verifies_and_skips_when_present(tmp_path):
    fetch = load_fetch()
    payloads = {"model.onnx": b"onnx-bytes", "tokenizer.json": b"{}"}
    spec, seen = fake_spec(payloads), []
    fetch.fetch(tmp_path, spec, opener=opener_for(payloads, seen))
    assert fetch.problems(tmp_path, spec) == []
    assert (tmp_path / "model.onnx").stat().st_mode & 0o777 == 0o644
    assert seen == ["https://example.invalid/org/model/" + "r" * 40 + "/src/model.onnx",
                    "https://example.invalid/org/model/" + "r" * 40 + "/src/tokenizer.json"]
    fetch.fetch(tmp_path, spec, opener=opener_for(payloads, seen))
    assert len(seen) == 2  # already verified: nothing downloaded again


def test_fetch_refuses_a_wrong_file(tmp_path):
    tmp_path = tmp_path / "model"
    fetch = load_fetch()
    spec = fake_spec({"model.onnx": b"good", "tokenizer.json": b"{}"})
    with pytest.raises(SystemExit):
        fetch.fetch(tmp_path, spec, opener=opener_for({"model.onnx": b"evil", "tokenizer.json": b"{}"}, []))
    with pytest.raises(SystemExit):
        fetch.fetch(tmp_path, spec, opener=opener_for({"model.onnx": b"good-and-longer", "tokenizer.json": b"{}"}, []))
    assert not (tmp_path / "model.onnx").exists()
    assert list(tmp_path.iterdir()) == []  # no partial files left behind


def test_verify_only_reports_missing(tmp_path, capsys):
    assert load_fetch().main([str(tmp_path), "--verify-only"]) == 1
    assert "model.onnx: missing" in capsys.readouterr().err


def test_both_release_paths_ship_the_model():
    mac = (SCRIPTS / "build-mac-bundle.sh").read_text(encoding="utf-8")
    assert 'fetch-embedding-model.py" "$root/hermes/plugins/memory/iollo_notes/model"' in mac
    assert "--extra iollo-memory" in mac and "web,iollo-memory]" in mac
    workflow = (REPO / ".github/workflows/iollo-release.yml").read_text(encoding="utf-8")
    box = workflow.split("\n  mac:")[0]
    assert box.index("fetch-embedding-model.py plugins/memory/iollo_notes/model") < box.index("docker/build-push-action")
    from plugins.memory.iollo_notes.embedder import DEFAULT_MODEL_DIR

    assert DEFAULT_MODEL_DIR == REPO / "plugins/memory/iollo_notes/model"
