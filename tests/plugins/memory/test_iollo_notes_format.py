"""iollo_notes note format (fork brief 003): parse/render round-trip, validation, conflict siblings."""

from pathlib import Path

import pytest

from plugins.memory.iollo_notes import notes

ANA = """---
id: people/ana-costa
type: person            # preference | person | organization | conversation
aliases: [Ana, Ana Costa]
updated: 2026-09-24
---
# Ana Costa
- 2026-09-24 Ricardo's dentist in Lisbon, see [[organizations/clinica-sorriso]]. (from: box, whatsapp)
- 2026-09-24 Away until mid-October. {valid_until: 2026-10-15} (from: mac:3f2a, conversation 8c1d)
"""


def test_parse_reads_header_bullets_and_provenance():
    note = notes.parse(ANA)
    assert (note.id, note.type, note.aliases, note.updated, note.title) == (
        "people/ana-costa", "person", ["Ana", "Ana Costa"], "2026-09-24", "Ana Costa")
    first, second = note.facts
    assert first.date == "2026-09-24" and first.provenance == "box, whatsapp" and first.valid_until is None
    assert first.text == "Ricardo's dentist in Lisbon, see [[organizations/clinica-sorriso]]."
    assert second.valid_until == "2026-10-15" and second.provenance == "mac:3f2a, conversation 8c1d"


def test_render_round_trips():
    note = notes.parse(ANA)
    again = notes.parse(notes.render(note))
    assert again == note
    assert notes.render(again) == notes.render(note)


def test_round_trip_keeps_unusual_aliases_and_free_lines():
    note = notes.new_note("organizations/acme", "organization", "Acme, Inc.",
                          ['Acme, Inc.', 'The "Big" One', "Zé Café"], "2026-09-01")
    note.body.append("Some free text the consolidator wrote.")
    notes.append_fact(note, "Supplies  the\noffice", "2026-09-02", "box (whatsapp)")
    parsed = notes.parse(notes.render(note))
    assert parsed.aliases == note.aliases
    assert parsed.body[0] == "Some free text the consolidator wrote."
    assert parsed.facts[0].text == "Supplies the office"  # one line only
    assert parsed.facts[0].provenance == "box [whatsapp]"
    assert parsed.updated == "2026-09-02"


def test_links_and_slugify():
    assert notes.links(ANA) == ["organizations/clinica-sorriso"]
    assert notes.links("[[people/a|Ana]] and [[people/a]] and [[people/b]]") == ["people/a", "people/b"]
    assert notes.slugify("Clínica Sorriso, Lda.") == "clinica-sorriso-lda"
    assert notes.slugify("日本") == "note"


def test_validate_accepts_a_good_note():
    assert notes.validate("people/ana-costa.md", ANA) == []


@pytest.mark.parametrize("path,content,expected", [
    ("organizations/ana-costa.md", ANA.replace("people/ana-costa", "organizations/ana-costa"), "type does not match folder"),
    ("people/ana-costa.md", ANA.replace("id: people/ana-costa\n", ""), "missing header field: id"),
    ("people/other.md", ANA, "id does not match path"),
    (".hidden/ana-costa.md", ANA, "dot-prefixed path component"),
    ("people/.ana-costa.md", ANA, "dot-prefixed path component"),
    ("people/ana-costa.txt", ANA, "not a .md file"),
    ("people/Ana Costa.md", ANA, "file name is not a slug"),
    ("people/ana-costa.md", ANA + "- 2026-09-24 " + "x" * 9 * 1024 + "\n", "larger than 8192 bytes"),
    ("people/ana-costa.md", "no header here", "missing header"),
])
def test_validate_rejects(path, content, expected):
    assert any(expected in err for err in notes.validate(path, content))


def test_validate_rejects_non_utf8():
    assert "not valid UTF-8" in notes.validate("people/ana-costa.md", ANA.encode() + b"\xff\xfe")


def test_append_fact_rejects_bad_dates_and_empty_text():
    note = notes.parse(ANA)
    with pytest.raises(notes.NoteError):
        notes.append_fact(note, "x", "24-09-2026", "box")
    with pytest.raises(notes.NoteError):
        notes.append_fact(note, "x", "2026-09-24", "box", valid_until="soon")
    with pytest.raises(notes.NoteError):
        notes.append_fact(note, "   ", "2026-09-24", "box")


def test_iter_note_files_skips_conflicts_profile_and_dot_paths(tmp_path: Path):
    for rel in ("people/ana-costa.md", "people/ana-costa.conflict-3f2a9c.md", "USER.md",
                "people/.draft.md", ".git/people.md", "preferences/coffee.md", "people/readme.txt"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x", encoding="utf-8")
    assert list(notes.iter_note_files(tmp_path)) == ["people/ana-costa.md", "preferences/coffee.md"]
    assert notes.is_conflict_sibling("people/ana-costa.conflict-3f2a9c.md")
