"""iollo_notes recall (iollo docs/MAC-RECALL-AND-MEMORY-DESIGN.md sections 6 and 7): the two new note types, the
people relation bullet, catalogue provenance, and the ``recall`` tool against stub box and control-plane servers.
Every name, ref and token here is fake."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from plugins.memory.iollo_notes import IolloNotesProvider, notes, where_it_is

from .iollo_notes_fakes import write_fixture


# -- note types, relation, provenance -------------------------------------------------------------

def test_projects_and_documents_are_note_types():
    assert notes.TYPE_FOLDERS["project"] == "projects" and notes.TYPE_FOLDERS["document"] == "documents"
    note = notes.new_note("documents/porto-lease", "document", "Porto lease", [], "2026-10-14")
    notes.append_fact(note, "Lives in Documents/Housing; renews every March.", "2026-10-14",
                      notes.recall_provenance("dev_fake", "Documents/Housing/porto-lease.pdf"))
    text = notes.render(note)
    assert notes.validate("documents/porto-lease.md", text) == []
    assert "(from: recall:dev_fake:Documents/Housing/porto-lease.pdf)" in text
    assert notes.validate("projects/porto-lease.md", text.replace("documents/", "projects/")) == [
        "type does not match folder"]


def test_recall_provenance_is_one_short_line_that_round_trips():
    assert notes.recall_provenance("dev_fake", "a (b).pdf") == "recall:dev_fake:a [b].pdf"
    long_ref = "Documents/" + "deep/" * 60 + "file.pdf"
    short = notes.recall_provenance("dev_fake", long_ref)
    assert short.startswith("recall:dev_fake:sha256:") and len(short) < 60
    assert short == notes.recall_provenance("dev_fake", long_ref)
    note = notes.new_note("documents/x", "document", "X", [], "2026-10-14")
    notes.append_fact(note, "A fact.", "2026-10-14", notes.recall_provenance("dev_fake", "a (b).pdf"))
    assert notes.parse(notes.render(note)).facts[0].provenance == "recall:dev_fake:a [b].pdf"


def test_a_person_has_at_most_one_current_relation():
    note = notes.new_note("people/fake-landlord", "person", "Fake Landlord", [], "2026-10-01")
    assert notes.relation_of(note) is None
    notes.append_fact(note, notes.relation_text("Colleague"), "2026-10-01", "box, consolidation")
    notes.append_fact(note, "~~relation: colleague~~ (until 2026-10-14)", "2026-10-14", "box, consolidation")
    notes.append_fact(note, notes.relation_text("landlord"), "2026-10-14", "recall:dev_fake:mail:1")
    assert notes.relation_of(note) == "landlord"
    assert notes.validate("people/fake-landlord.md", notes.render(note)) == []
    with pytest.raises(notes.NoteError):
        notes.relation_text("the person who rents me the flat in Porto, since 2019!")
    org = notes.new_note("organizations/acme", "organization", "Acme", [], "2026-10-14")
    notes.append_fact(org, "relation: landlord", "2026-10-14", "box")
    assert notes.validate("organizations/acme.md", notes.render(org)) == [
        "a relation bullet belongs only in a people note"]


# -- where a hit is ------------------------------------------------------------------------------

def test_where_it_is_reads_like_a_place():
    assert where_it_is({"source": "files", "kind": "pdf", "ref": "Documents/Taxes/2025/irs.pdf", "loc": 3}) == \
        "Documents/Taxes/2025, irs.pdf, page 3"
    assert where_it_is({"source": "files", "kind": "audio", "ref": "Desktop/call.m4a", "loc": 83}) == \
        "Desktop, call.m4a, at 1:23"
    assert where_it_is({"source": "messages", "people": ["Fake Ana"], "when": "2026-03-03T10:00:00Z"}) == \
        "message from Fake Ana on 2026-03-03"
    assert where_it_is({"source": "mail", "people": ["Fake Ana"], "when": "2026-03-03", "title": "Lease"}) == \
        'email from Fake Ana on 2026-03-03 ("Lease")'
    assert where_it_is({"source": "notes", "title": "Porto"}) == 'note "Porto" in Notes'
    assert where_it_is({"source": "history", "ref": "https://www.example.com/a", "when": "2026-10-01"}) == \
        "visited www.example.com on 2026-10-01"


# -- the recall tool -----------------------------------------------------------------------------

class Stub:
    """One HTTP server playing the box sync service (/recall/search) and the control plane (/relay/device-tool)."""

    def __init__(self):
        self.requests, self.listing, self.mac_answer = [], {"connected": False, "linked": True}, None
        self.catalogue = {"hits": [], "devices": []}
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self, payload, status=200):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                stub.requests.append(("GET", self.path, None, dict(self.headers)))
                self._answer(stub.listing)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                stub.requests.append(("POST", self.path, body, dict(self.headers)))
                if self.path == "/recall/search":
                    self._answer(stub.catalogue)
                else:
                    self._answer(stub.mac_answer)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stub(monkeypatch):
    s = Stub()
    monkeypatch.setenv("IOLLO_TEST_SYNC_KEY", "fake-sync-key")
    monkeypatch.setenv("RELAY_URL", s.base)
    monkeypatch.setenv("RELAY_TOKEN", "fake-relay-token")
    yield s
    s.close()


@pytest.fixture
def provider(tmp_path, stub):
    write_fixture(tmp_path / "memory-notes")
    p = IolloNotesProvider({"decide_url": stub.base + "/memory/decide", "decide_token_env": "IOLLO_TEST_SYNC_KEY",
                            "device": "box"}, embedder=None)
    p.initialize("sess-fake", hermes_home=str(tmp_path), platform="cli")
    yield p
    p.shutdown()


CATALOGUE_HIT = {"device_id": "dev_fake_mac", "ref": "Documents/Housing/porto-lease.pdf", "source": "files",
                 "kind": "pdf", "title": "Porto lease", "when": "2026-09-01T00:00:00Z", "people": ["Fake Landlord"],
                 "excerpt": "Rent due on the first.", "score": 1.0}


def test_recall_is_a_tool_of_the_provider(provider):
    names = [schema["name"] for schema in provider.get_tool_schemas()]
    assert "recall" in names and "memory_search" in names


def test_recall_searches_the_catalogue_when_the_mac_is_away(provider, stub):
    stub.catalogue = {"hits": [CATALOGUE_HIT], "devices": [{"device_id": "dev_fake_mac", "items": 9,
                                                            "still_reading": False, "last_pass": None}]}
    out = json.loads(provider.handle_tool_call("recall", {"query": "porto lease", "sources": ["files"],
                                                          "since": "2026-01-01", "limit": 50}))
    assert out["mac"] == "not_connected" and out["count"] == 1
    hit = out["hits"][0]
    assert hit["from"] == "catalogue" and hit["where"] == "Documents/Housing, porto-lease.pdf"
    search = next(r for r in stub.requests if r[1] == "/recall/search")
    assert search[2] == {"query": "porto lease", "sources": ["files"], "since": "2026-01-01", "limit": 20}
    assert search[3]["Authorization"] == "Bearer fake-sync-key"
    assert not [r for r in stub.requests if r[0] == "POST" and r[1] == "/relay/device-tool"]


def test_recall_asks_the_connected_mac_and_puts_its_hits_first(provider, stub):
    stub.catalogue = {"hits": [CATALOGUE_HIT, {**CATALOGUE_HIT, "ref": "Documents/old.pdf", "title": "Old"}],
                      "devices": []}
    stub.listing = {"connected": True, "device": {"id": "dev_fake_mac", "name": "Fake Mac"},
                    "tools": [{"name": "mac_recall_local"}], "recall": {"enabled": True, "items": 10, "indexed": 4,
                                                                         "last_pass_at": None}}
    stub.mac_answer = {"state": "done", "output": "2 hits", "error": None, "data": {
        "hits": [{**CATALOGUE_HIT, "loc": 3, "item": 7}, {"source": "messages", "kind": "message", "ref": "msg:1",
                                                          "people": ["Fake Landlord"], "when": "2026-10-10T09:00:00Z",
                                                          "excerpt": "The keys are with me."}],
        "indexed": 4, "total": 10}}
    out = json.loads(provider.handle_tool_call("recall", {"query": "porto lease"}))
    assert out["mac"] == "searched" and [h["from"] for h in out["hits"]] == ["mac", "mac", "catalogue"]
    assert out["hits"][0]["where"] == "Documents/Housing, porto-lease.pdf, page 3"
    assert out["hits"][1]["where"] == "message from Fake Landlord on 2026-10-10"
    assert out["hits"][2]["ref"] == "Documents/old.pdf"                  # the duplicate of a Mac hit is dropped
    assert out["still_reading"] is True and "still reading your Mac" in out["note"]
    call = next(r for r in stub.requests if r[0] == "POST" and r[1] == "/relay/device-tool")
    assert call[2] == {"tool": "mac_recall_local", "args": {"query": "porto lease", "limit": 10},
                       "deadline_s": 20, "device_id": "dev_fake_mac"}
    assert call[3]["X-Relay-Token"] == "fake-relay-token"


def test_recall_skips_a_mac_without_recall_and_says_when_nothing_is_found(provider, stub):
    stub.listing = {"connected": True, "device": {"id": "dev_fake_mac"}, "tools": [{"name": "mac_recall_local"}],
                    "recall": {"enabled": False, "items": 0, "indexed": 0}}
    out = json.loads(provider.handle_tool_call("recall", {"query": "porto lease"}))
    assert out["mac"] == "recall_off" and out["hits"] == [] and out["note"].startswith("Nothing found")
    stub.listing = {**stub.listing, "recall": {"enabled": True, "items": 5, "indexed": 0}}
    stub.mac_answer = {"state": "failed", "output": "", "data": {"hits": [CATALOGUE_HIT], "indexed": 0, "total": 5},
                       "error": {"code": "indexing", "message": "first pass", "retryable": True}}
    out = json.loads(provider.handle_tool_call("recall", {"query": "porto lease"}))
    assert out["mac"] == "indexing" and out["hits"][0]["from"] == "mac" and out["still_reading"] is True
    stub.mac_answer = {"state": "failed", "output": "", "data": {},
                       "error": {"code": "mac_unreachable", "message": "asleep", "retryable": True}}
    assert json.loads(provider.handle_tool_call("recall", {"query": "porto lease"}))["mac"] == "unreachable"
    assert "error" in json.loads(provider.handle_tool_call("recall", {"query": "  "}))


def test_recall_without_a_box_service_still_answers(tmp_path, monkeypatch):
    monkeypatch.delenv("RELAY_TOKEN", raising=False)
    write_fixture(tmp_path / "memory-notes")
    p = IolloNotesProvider({"decide_url": "", "device": "box"}, embedder=None)
    p.initialize("sess-fake", hermes_home=str(tmp_path), platform="cli")
    out = json.loads(p.handle_tool_call("recall", {"query": "porto lease"}))
    assert out["hits"] == [] and out["catalogue"] == "no catalogue on this device" and out["mac"] == "not_connected"
    p.shutdown()
