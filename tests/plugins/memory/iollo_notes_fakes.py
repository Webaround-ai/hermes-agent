"""Shared fixtures for the iollo_notes tests: a tiny concept embedder and a notes tree."""

import math
import re
from pathlib import Path

# Words that mean the same thing share an axis, so a paraphrase with no word in common still lands
# on the right note. Unknown words hash onto the remaining axes.
CONCEPTS = {
    "dentist": 0, "teeth": 0, "tooth": 0, "dental": 0,
    "coffee": 1, "espresso": 1,
    "travel": 2, "trip": 2, "flight": 2,
    "clinic": 3, "clinica": 3,
}
DIM = 16


class ConceptEmbedder:
    name = "fake-concepts"
    dim = DIM

    def __init__(self):
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        out = []
        for text in texts:
            vec = [0.0] * DIM
            for word in re.findall(r"\w+", text.lower()):
                axis = CONCEPTS.get(word, 4 + sum(map(ord, word)) % (DIM - 4))
                vec[axis] += 3.0 if word in CONCEPTS else 0.2
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out


def note_text(note_id, note_type, title, aliases, bullets):
    lines = ["---", f"id: {note_id}", f"type: {note_type}", "aliases: [" + ", ".join(aliases) + "]",
             "updated: 2026-09-24", "---", f"# {title}"]
    lines += [f"- 2026-09-24 {b} (from: box, whatsapp)" for b in bullets]
    return "\n".join(lines) + "\n"


FIXTURE = {
    "people/ana-costa": ("person", "Ana Costa", ["Ana", "Ana Costa"],
                         ["Ricardo's dentist in Lisbon, see [[organizations/clinica-sorriso]].",
                          "Away until mid-October."]),
    "organizations/clinica-sorriso": ("organization", "Clinica Sorriso", ["Sorriso"],
                                      ["Opens at 9 on weekdays.", "Parking behind the building."]),
    "preferences/coffee": ("preference", "Coffee", [], ["Drinks espresso without sugar."]),
    "people/bruno-lima": ("person", "Bruno Lima", ["Bruno"], ["Plays football on Thursdays."]),
}


def write_fixture(root: Path, fixture=FIXTURE) -> Path:
    for note_id, (kind, title, aliases, bullets) in fixture.items():
        path = root / f"{note_id}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(note_text(note_id, kind, title, aliases, bullets), encoding="utf-8")
    (root / "USER.md").write_text("# Profile\nRicardo lives in Lisbon.\n", encoding="utf-8")
    return root
