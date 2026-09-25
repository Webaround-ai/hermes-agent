# Runs trace inputs (fork brief 036)

This isolated behavior patch adds fields to the existing authenticated
`GET /v1/runs/{run_id}/events` stream. Existing fields, event timestamps,
status persistence and authentication are unchanged. The mapping lives in
`gateway/platforms/api_server_run_trace.py`; executor callbacks supply invocation
identity and task context. Desktop JSON-RPC is unchanged.

| Event | Added fields |
| --- | --- |
| `tool.started` | `tool_call_id`, supported tools' effective absolute `working_dir`, allowlisted `args` |
| `tool.completed` | `tool_call_id`, allowlisted `result_summary` when available |

IDs come from executor invocation IDs (SHA-256 only if longer than 128 characters),
never tool names or completion order. Older callback producers without an ID
remain accepted without fabricating an identity. Unsupported tools get no detail.
Browser tools expose sanitized URL and result title; file tools expose relative
path and known operation; terminal exposes command and integer exit code.
Booleans are not integers. Unknown/unavailable result fields are omitted.
`write_file` reports create/edit from the backend existence probe under its existing
path lock. Patch operation uses its actual file outcome lists; mixed outcomes are
omitted. `lines_changed` is forwarded only when supplied as an integer by the tool.

## Producer handoff for brief 037

These are transient producer inputs, not an envelope and not durable run status.
Never persist/log cwd, args or result summaries in the control plane. The existing
owner-box agent conversation persistence is unchanged.

- Preserve the start timestamp when updating a trace entry on completion.
- URL credentials, queries and fragments are removed. Host/path/title limits are
  128/160/120 characters. Split URL into envelope `url_host` and `url_path`.
- Paths are resolved using the file tool's live task cwd; only paths inside it are
  forwarded, relative and at most 200 characters. Home expansion, traversal and
  outside symlinks are omitted. Never relay the absolute `working_dir` to devices.
- Commands retain newlines, with secrets redacted and a transient 16,384-character
  bound. The producer must take the true first line, reapply envelope credential
  redaction, and cap it at 120 characters before envelope delivery.
- No attachment IDs are currently emitted. Only add them after existing file-channel
  metadata has reached the producer, which must validate IDs against attachments in
  the same envelope revision (at most 50 IDs, each 1–128 characters). Never derive
  IDs from arbitrary tool output, paths or screenshot URLs.
- Tool output never goes on the run stream. Upstream v2026.9.24 added a redacted
  `preview` of the tool result to `tool.completed`; it stays in the code but is off
  (`_EMIT_TOOL_COMPLETED_PREVIEW = False` in `api_server_runs.py`), because the
  envelope producer maps event previews into trace detail.
- Approval trace continues to come from the envelope prompt. No new approval event,
  transport frame or deployment is introduced here.
