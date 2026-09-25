"""Runtime envelope shape, assembly and trace privacy rules (no control-plane imports)."""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .cards import cards_from_text


log = logging.getLogger(__name__)

ENVELOPE_V = 1
TEXT_LIMIT = 32 * 1024
SMS_SEGMENT = 1000
FRAME = "message.v2"
KNOWN_BLOCKS = {"card", "cards", "location", "contact", "weather", "quote", "code", "a2ui"}   # brief 034
TIER3 = ("pay", "delete", "system", "bulk_new")
DEVICE_SURFACES = {"mac", "phone", "web", "pro"}
# Block ids name where a block came from. Clients treat them as opaque; the WhatsApp renderer uses them to
# send a list parsed from the reply text in its place. Every other block follows the reply (brief 034).
TEXT_CARDS_ID = "text-cards"
HELD_PREFIX = "held-"


class _Model(BaseModel):
    # Receivers ignore unknown fields within envelope version 1, so the models accept and keep them.
    model_config = ConfigDict(extra="allow")


class Block(_Model):
    type: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,39}$")
    id: str = Field(min_length=1, max_length=128)


class Attachment(_Model):
    id: str | None = Field(default=None, min_length=1, max_length=128)
    kind: Literal["image", "video", "audio", "document"]
    name: str = Field(min_length=1, max_length=255)
    mime: str = Field(min_length=1, max_length=255)
    url: str = Field(min_length=1, max_length=4096)
    caption: str | None = Field(default=None, max_length=1024)
    expires_at: AwareDatetime


class Option(_Model):
    id: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=200)
    style: str | None = Field(default=None, max_length=32)


class FormField(_Model):
    id: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=200)
    kind: Literal["text", "number", "email", "phone", "note", "date", "choice"]
    options: list[str] | None = Field(default=None, max_length=10)


class Prompt(_Model):
    prompt_id: str = Field(min_length=1, max_length=128)
    kind: Literal["approval", "choice", "form"]
    text: str = Field(min_length=1, max_length=4096)
    options: list[Option] = Field(max_length=10)
    fields: list[FormField] | None = Field(default=None, max_length=8)
    expires_at: AwareDatetime | None = None
    tier3: Literal["pay", "delete", "system", "bulk_new"] | None = None

    @model_validator(mode="after")
    def approval_is_tier3(self):
        # Only tier-3 actions ask for approval (brief 031); a question is a choice or a form.
        if self.kind == "approval" and self.tier3 is None:
            raise ValueError("an approval prompt needs tier3")
        return self


class Link(_Model):
    kind: Literal["live_view", "task", "calendar"]
    url: str = Field(min_length=1, max_length=4096)
    label: str = Field(min_length=1, max_length=200)
    expires_at: AwareDatetime | None = None


class Progress(_Model):
    id: str | None = Field(default=None, min_length=1, max_length=128)
    tool: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=200)
    state: Literal["started", "done", "failed"]


class _TraceDetail(BaseModel):
    # Trace is a privacy boundary: unlike the rest of v1, never forward unknown fields.
    model_config = ConfigDict(extra="ignore")


def redact_command(value: str) -> str:
    value = value.splitlines()[0] if value.splitlines() else ""
    value = re.sub(r"(?i)\bBearer\s+(?:\"[^\"]*\"|'[^']*'|[^\s\"']+)", "Bearer …", value)
    value = re.sub(r"(?i)([\w-]*(?:key|token|secret|password)[\w-]*[\"']?\s*[=:]\s*)"
                   r"(?:\"[^\"]*\"|'[^']*'|[^\s;&|]+)", r"\1…", value)
    value = re.sub(r"(?i)(--[\w-]*(?:key|token|secret|password)[\w-]*\s+)"
                   r"(?:\"[^\"]*\"|'[^']*'|[^\s;&|]+)", r"\1…", value)
    return re.sub(r"\b(?:sk-[\w-]+|gh[pousr]_[\w]+|xox[baprs]-[\w-]+|"
                  r"eyJ[\w-]+\.[\w-]+\.[\w-]+|[A-Za-z0-9_/-]{32,}={0,2})", "…", value)


def relative_trace_path(value: str) -> str:
    # Reject home expansion, drives, UNC paths and traversal on either host platform.
    if (value.startswith(("/", "~", "\\")) or re.match(r"^[A-Za-z]:", value)
            or ".." in value.replace("\\", "/").split("/") or any(ord(c) < 32 for c in value)):
        raise ValueError("trace path must be relative to the run working folder")
    return value


class ToolDetail(_TraceDetail):
    tool: str | None = Field(default=None, max_length=128)


class BrowserDetail(_TraceDetail):
    url_host: str | None = Field(default=None, max_length=128)
    url_path: str | None = Field(default=None, max_length=160)
    title: str | None = Field(default=None, max_length=120)

    @field_validator("url_path", mode="before")
    @classmethod
    def strip_query(cls, value):
        return value.split("?", 1)[0].split("#", 1)[0] if isinstance(value, str) else value


class FileDetail(_TraceDetail):
    path: str | None = Field(default=None, max_length=200)
    op: Literal["read", "edit", "create", "delete"] | None = None
    lines_changed: int | None = Field(default=None, strict=True)

    @field_validator("path")
    @classmethod
    def relative_path(cls, value):
        return relative_trace_path(value) if value is not None else value


class CommandDetail(_TraceDetail):
    command: str | None = Field(default=None, max_length=120)
    exit_code: int | None = Field(default=None, strict=True)

    @field_validator("command", mode="before")
    @classmethod
    def redact(cls, value):
        return redact_command(value) if isinstance(value, str) else value


class ApprovalDetail(_TraceDetail):
    prompt_id: str | None = Field(default=None, max_length=128)
    tier: Literal["pay", "delete", "system", "bulk_new"] | None = None


TRACE_DETAILS = {"tool": ToolDetail, "browser": BrowserDetail, "file": FileDetail,
                 "command": CommandDetail, "approval": ApprovalDetail}


class Trace(_TraceDetail):
    id: str = Field(min_length=1, max_length=128)
    kind: Literal["tool", "browser", "file", "command", "approval"]
    label: str = Field(min_length=1, max_length=200)
    state: Literal["started", "done", "failed"]
    at: AwareDatetime
    detail: dict | None = None
    attachment_ids: list[str] | None = Field(default=None, max_length=50)

    @model_validator(mode="after")
    def validate_detail(self):
        if self.detail is not None:
            self.detail = TRACE_DETAILS[self.kind].model_validate(self.detail).model_dump(exclude_none=True)
        if self.attachment_ids is not None and any(not x or len(x) > 128 for x in self.attachment_ids):
            raise ValueError("invalid trace attachment id")
        return self


def trace_for_tool(tool: str, event: dict, *, id: str, label: str, state: str) -> dict:
    """Map the runs API's available previews only; never treat arbitrary preview text as detail."""
    kind, detail = "tool", {"tool": tool[:128]}
    preview = event.get("preview")
    preview = preview if isinstance(preview, str) else ""
    if tool.startswith("browser_"):
        kind, detail = "browser", {}
        if tool == "browser_navigate":
            try:
                url = urlsplit(preview)
                if url.scheme in {"http", "https"} and url.hostname:
                    detail = {"url_host": url.hostname[:128], "url_path": url.path[:160]}
            except ValueError:
                pass
    elif tool in {"read_file", "write_file", "patch", "edit_file", "create_file", "delete_file", "search_files"}:
        kind, detail = "file", {}
        op = {"read_file": "read", "patch": "edit", "edit_file": "edit",
              "create_file": "create", "delete_file": "delete", "search_files": "read"}.get(tool)
        if op:
            detail["op"] = op
        # read_file's preview is a basename plus line range, not a relative path.
        # write_file can create OR overwrite; its preview alone cannot tell us which.
        if tool in {"write_file", "patch"} and preview:
            try:
                detail["path"] = relative_trace_path(preview)[:200]
            except ValueError:
                pass
    elif tool in {"terminal", "shell", "bash", "run_command"}:
        kind, detail = "command", {}
        if tool == "terminal" and preview:
            detail["command"] = redact_command(preview)[:120]
    at = stamp()
    timestamp = event.get("timestamp")
    if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
        try:
            at = stamp(datetime.fromtimestamp(timestamp, timezone.utc))
        except (ValueError, OverflowError, OSError):
            pass
    return {"id": id, "kind": kind, "label": label, "state": state, "at": at, "detail": detail}


def without_trace_detail(value: dict) -> dict:
    """Copy for control-plane persistence; live frames and box storage retain detail."""
    if "trace" not in value:
        return value
    return {**value, "trace": [{k: v for k, v in entry.items() if k != "detail"} for entry in value["trace"]]}


class Meta(_Model):
    surface_origin: Literal["whatsapp", "sms", "mac", "phone", "web", "pro"]
    locale: str | None = Field(default=None, max_length=35)
    truncated: bool
    legacy: bool | None = None


class Envelope(_Model):
    envelope_v: Literal[1]
    message_id: str = Field(min_length=1, max_length=128)
    conversation_id: int | None = Field(default=None, ge=1)
    run_id: str | None = Field(default=None, min_length=1, max_length=128)
    rev: int = Field(ge=1)
    created_at: AwareDatetime
    status: Literal["final", "partial", "error"]
    text_md: str = Field(max_length=TEXT_LIMIT)
    blocks: list[Block]
    attachments: list[Attachment]
    prompt: Prompt | None = None
    links: list[Link]
    progress: list[Progress] | None = None
    meta: Meta
    trace: list[Trace] | None = Field(default=None, max_length=50)

    @model_validator(mode="after")
    def trace_attachments_exist(self):
        ids = {a.id for a in self.attachments if a.id is not None}
        if any(ref not in ids for t in self.trace or [] for ref in t.attachment_ids or []):
            raise ValueError("trace attachment is absent from envelope")
        return self


def validate_envelope(value: Any) -> dict:
    """The envelope as plain JSON data, or ValueError. Absent optional keys stay absent."""
    try:
        validated = Envelope.model_validate(value)
    except ValidationError as e:
        raise ValueError(f"invalid envelope: {e.errors()[0]['loc']}") from None
    if validated.trace is not None:
        value = {**value, "trace": [t.model_dump(mode="json", exclude_none=True) for t in validated.trace]}
    return value


def stamp(value: datetime | None = None) -> str:
    value = value or datetime.now(timezone.utc)
    value = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def new_message_id() -> str:
    return str(uuid.uuid4())


def cards_block(block_id: str, items: list[dict], intro: str = "") -> dict:
    """Today's card dicts as one ``cards`` block, items in the catalog's card shape (brief 034)."""
    from .cards import cards_block as catalog_cards_block
    return catalog_cards_block(block_id, items, intro)


def held_blocks(held: list | tuple) -> list[dict]:
    """What the assistant made for this run: ``(intro, cards)`` from the cards code, or catalog blocks from
    ``show_widget``, in the order they were made. Ids stay unique within the envelope."""
    blocks: list[dict] = []
    seen = {TEXT_CARDS_ID}
    for i, entry in enumerate(held, 1):
        if isinstance(entry, dict):
            block = dict(entry)
            if block.get("id") in seen:
                block["id"] = f"{HELD_PREFIX}{i}"
        else:
            intro, items = entry
            if not items:
                continue
            block = cards_block(f"{HELD_PREFIX}{i}", items, intro)
        seen.add(block["id"])
        blocks.append(block)
    return blocks


def build(run: Any = None, text: str = "", held_cards: list[tuple[str, list[dict]]] | tuple = (),
          attachments: list[dict] | tuple = (), prompt: dict | None = None, links: list[dict] | tuple = (), *,
          surface: str, message_id: str | None = None, rev: int = 1, status: str = "final",
          conversation_id: int | None = None, progress: list[dict] | None = None, parse_cards: bool = True,
          created_at: datetime | None = None, trace: list[dict] | None = None, locale: str | None = None) -> dict:
    """One envelope for a reply. A reply that is a list of linked items becomes ``text_md`` (its intro and
    outro) plus a ``cards`` block; cards the assistant held for this run become ``cards`` blocks after it."""
    text = (text or "").strip()
    blocks: list[dict] = []
    parsed = cards_from_text(text) if parse_cards and text else None
    if parsed is not None:
        intro, items, outro = parsed
        text = "\n\n".join(x for x in (intro, outro) if x)
        blocks.append(cards_block(TEXT_CARDS_ID, items))
    held = held_blocks(held_cards)
    blocks += held[-(50 - len(blocks)):]
    truncated = len(text) > TEXT_LIMIT
    envelope: dict[str, Any] = {
        "envelope_v": ENVELOPE_V, "message_id": message_id or new_message_id(), "rev": rev,
        "created_at": stamp(created_at), "status": status, "text_md": text[:TEXT_LIMIT], "blocks": blocks,
        "attachments": [dict(a) for a in attachments][-50:], "links": [dict(link) for link in links][-50:],
        "meta": {"surface_origin": surface, "truncated": truncated}}
    if locale is not None:
        envelope["meta"]["locale"] = locale
    cid = conversation_id if conversation_id is not None else getattr(run, "conversation_id", None)
    if cid is not None:
        envelope["conversation_id"] = cid
    if getattr(run, "run_id", None):
        envelope["run_id"] = run.run_id
    if prompt is not None:
        envelope["prompt"] = prompt
    if progress:
        envelope["progress"] = [dict(p) for p in progress][-50:]
    if trace:
        envelope["trace"] = trace[-50:]
    return validate_envelope(envelope)
