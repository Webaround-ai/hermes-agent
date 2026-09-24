"""iollo_notes index (fork brief 003): FTS by alias, vector paraphrase, one hop, vec fallback,
incremental rebuild, and the real shipped model when present."""

import logging
import os

import pytest

from plugins.memory.iollo_notes import index as index_mod
from plugins.memory.iollo_notes.index import NoteIndex

from .iollo_notes_fakes import ConceptEmbedder, note_text, write_fixture


@pytest.fixture
def notes_dir(tmp_path):
    return write_fixture(tmp_path / "memory-notes")


def _open(tmp_path, notes_dir, embedder=None, use_vec=True):
    idx = NoteIndex(notes_dir, tmp_path / "memory-index.db", embedder, use_vec=use_vec)
    idx.refresh()
    return idx


def _ids(hits):
    return [h["id"] for h in hits]


def test_fts_finds_by_alias(tmp_path, notes_dir):
    idx = _open(tmp_path, notes_dir)
    hits = idx.search("what did I say about Sorriso?")
    assert _ids(hits)[0] == "organizations/clinica-sorriso"
    assert set(hits[0]) == {"id", "type", "title", "score", "bullets"}
    assert idx.count() == 4
    idx.close()


def test_hit_returns_three_best_bullets(tmp_path, notes_dir):
    (notes_dir / "people/ana-costa.md").write_text(note_text(
        "people/ana-costa", "person", "Ana Costa", ["Ana"],
        ["Likes jazz.", "Has a cat.", "Dentist in Lisbon.", "Lives near the river.", "Birthday in May."]),
        encoding="utf-8")
    idx = _open(tmp_path, notes_dir)
    bullets = idx.search("Ana dentist")[0]["bullets"]
    assert len(bullets) == 3 and bullets[0].startswith("2026-09-24 Dentist in Lisbon.")
    idx.close()


def test_vector_path_finds_a_paraphrase(tmp_path, notes_dir):
    fts_only = _open(tmp_path, notes_dir)
    assert "people/ana-costa" not in _ids(fts_only.search("who looks after my teeth"))
    fts_only.close()
    idx = NoteIndex(notes_dir, tmp_path / "vec.db", ConceptEmbedder())
    idx.refresh()
    assert _ids(idx.search("who looks after my teeth"))[0] == "people/ana-costa"
    idx.close()


def test_one_hop_adds_the_linked_note(tmp_path, notes_dir):
    idx = _open(tmp_path, notes_dir)
    ids = _ids(idx.search("Ana"))
    assert ids[0] == "people/ana-costa"
    assert "organizations/clinica-sorriso" in ids  # linked from the top hit, no word in common
    assert _ids(idx.search("Ana", hop=False)) == ["people/ana-costa"]
    idx.close()


def test_type_filter(tmp_path, notes_dir):
    idx = _open(tmp_path, notes_dir)
    assert _ids(idx.search("Ana", note_type="organization")) == ["organizations/clinica-sorriso"]
    idx.close()


def test_fallback_without_sqlite_vec(tmp_path, notes_dir, monkeypatch, caplog):
    monkeypatch.setattr(index_mod, "_vec_fallback_logged", False)
    import sqlite_vec

    def boom(conn):
        raise RuntimeError("no extension loading here")

    monkeypatch.setattr(sqlite_vec, "load", boom)
    with caplog.at_level(logging.WARNING, logger=index_mod.__name__):
        idx = NoteIndex(notes_dir, tmp_path / "fallback.db", ConceptEmbedder())
        NoteIndex(notes_dir, tmp_path / "fallback2.db", ConceptEmbedder()).close()
    assert idx._vec is False
    assert sum("vec_fallback" in r.getMessage() for r in caplog.records) == 1
    idx.refresh()
    assert _ids(idx.search("who looks after my teeth"))[0] == "people/ana-costa"
    idx.close()


def test_vec_table_is_used_when_the_extension_loads(tmp_path, notes_dir):
    pytest.importorskip("sqlite_vec")
    idx = _open(tmp_path, notes_dir, ConceptEmbedder())
    if not idx._vec:
        pytest.skip("this Python cannot load SQLite extensions")
    assert idx._conn.execute("SELECT COUNT(*) FROM notes_vec").fetchone()[0] == 4
    assert _ids(idx.search("who looks after my teeth"))[0] == "people/ana-costa"
    idx.close()


def test_incremental_rebuild(tmp_path, notes_dir):
    embedder = ConceptEmbedder()
    idx = _open(tmp_path, notes_dir, embedder)
    assert idx.refresh() == {"changed": 0, "removed": 0, "invalid": 0}
    calls = embedder.calls
    path = notes_dir / "preferences/coffee.md"
    path.write_text(note_text("preferences/coffee", "preference", "Coffee", [], ["Now prefers green tea."]),
                    encoding="utf-8")
    os.utime(path, ns=(1, 1))  # force a different mtime even on coarse clocks
    assert idx.refresh()["changed"] == 1
    assert embedder.calls == calls + 1
    assert _ids(idx.search("green tea"))[0] == "preferences/coffee"
    (notes_dir / "people/bruno-lima.md").unlink()
    (notes_dir / "people/broken.md").write_text("not a note", encoding="utf-8")
    assert idx.refresh() == {"changed": 0, "removed": 1, "invalid": 1}
    assert not idx.has("people/bruno-lima") and not idx.has("people/broken")
    idx.close()
    reopened = _open(tmp_path, notes_dir, embedder)  # persisted: nothing to redo
    assert reopened.refresh()["changed"] == 0 and reopened.count() == 3
    reopened.close()


def test_changing_the_model_re_embeds(tmp_path, notes_dir):
    _open(tmp_path, notes_dir).close()  # built without vectors
    embedder = ConceptEmbedder()
    idx = _open(tmp_path, notes_dir, embedder)
    assert embedder.calls >= 1
    assert _ids(idx.search("who looks after my teeth"))[0] == "people/ana-costa"
    idx.close()


def test_real_model_finds_the_paraphrase(tmp_path, notes_dir):
    pytest.importorskip("onnxruntime")
    pytest.importorskip("tokenizers")
    from plugins.memory.iollo_notes import embedder as embedder_mod

    if embedder_mod.verify_model_dir(embedder_mod.model_dir()):
        pytest.skip("shipped model not present (fetched by scripts/iollo/fetch-embedding-model.py at build time)")
    real = embedder_mod.load_embedder()
    assert real is not None and real.dim == 384
    idx = _open(tmp_path, notes_dir, real)
    assert _ids(idx.search("my dentist"))[0] == "people/ana-costa"
    assert _ids(idx.search("who takes care of my teeth"))[0] == "people/ana-costa"
    idx.close()
