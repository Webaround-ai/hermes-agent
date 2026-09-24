# Brief 002: the iollo-permissions plugin — hard blocks, the Jev tier judge, trash and versions

Written 2026-09-24. Work on the `iollo` branch; never touch `main` (docs/IOLLO-FORK.md).

Read first: `docs/IOLLO-FORK.md`, `tools/approval.py` (`request_tool_approval`, `_run_approval_gate`),
`tools/approval_floors.py`, `tools/approval_smart.py` (`call_llm(task="approval")`, `_VERDICTS`),
`tools/approval_context.py`, `tools/file_tools_write_guards.py`, `hermes_cli/plugins.py` (`pre_tool_call`,
`post_tool_call`, the `block`/`approve` directives), `agent/auxiliary_client.py`, and the cloud brief
`Webaround-ai/iollo` `briefs/031-permission-tiers.md`, which owns `permissions.yaml` and the tier table.

## Goal (decided by Ricardo, 2026-09-24)
Iollo is "pretty permissive, almost yolo, but without damage to the file system". The same Hermes code on the
box and the Mac enforces it: tier 1 runs and is logged, tier 3 raises exactly one owner approval, tier 4 is
refused even if asked. Jev (the existing `auxiliary.approval` slot, `jev/approval` through the gateway)
judges the tier of each acting tool call; keyword detectors are only the fallback when Jev is unavailable.
Hard blocks never depend on a model: they are code.

## Order and dependencies
1. Cloud brief 031 fixes the `permissions.yaml` shape (schema version 1) and the Jev shim's tier mode.
   Write against a fixture copy of that file in `tests/plugins/iollo_permissions/`.
2. **This brief.** Tag a new `iollo-*` release when merged; boxes get it through the release train (cloud
   030), the Mac by pinning the tag (Mac brief 029).

## What already exists (scope down)
- `approvals.deny` globs and the hardline floor run before yolo and `mode: off` (`approval_floors.py`).
- Plugin `pre_tool_call` may return `block` (veto with message) or `approve` (the human gate, as for
  dangerous commands); `request_tool_approval` fails closed where no one can answer.
- Smart approval already asks the `approval` auxiliary task for flagged terminal commands and maps the one
  word APPROVE / DENY / ESCALATE.

## Build: `plugins/iollo-permissions/` (bundled, off unless enabled)
Settings under `plugins.entries["iollo-permissions"]`: `path` (permissions.yaml), `workspace`, `write_roots`,
`versions`, `trash` (a directory) or `trash_command` (argv), `activity`, `judge_timeout_s` (default 3).
1. **Hard blocks (tier 4), first and unconditionally.** Match `tier4.commands` against the terminal command
   after the fork's own normalization (the variants `approval_detection` builds), and any destructive
   command whose target resolves (realpath) outside `workspace` and `write_roots`. Return `block` with
   "Stopped before <action>: Iollo never does this." No judge call, no approval, no yolo bypass.
2. **The judge.** For every tool call that is not a pure read (terminal, file writes, browser type/click,
   vault save, send tools, the Mac's `computer_*` input tools), call `call_llm(task="approval")` with the
   `<tier-rubric>` block rendered from `permissions.yaml` plus the tool name and redacted arguments.
   The shim answers APPROVE = tier 1 (proceed), ESCALATE = tier 3 (`approve` directive, reason
   `tier3:<detector>: <one plain sentence>`; the detector label comes from the keyword detectors, `other` if none match), DENY = hard block (`block` as in step 1).
   Cache verdicts per (tool, normalized arguments) for the session.
3. **Fallback.** On timeout, error or an unparseable answer, run the keyword detectors from
   `permissions.yaml` (`pay`, `delete`, `system`, `bulk_new`): a match is tier 3, otherwise tier 1. Log
   `judge_fallback` (no content). Never fail open on tier 4: step 1 already ran.
4. **Smart path.** When the plugin is enabled, `approval_smart` sends the same rubric for flagged terminal
   commands, so terminal and tool calls share one judge and one vocabulary.
5. **Versions and trash.** Before `write_file`/`patch` on an existing file in the workspace or a write root,
   copy it to `versions/<date>/<path>`, pruned after `versioning.keep_days`. Register `files_trash(paths)`:
   runs `trash_command` (the Mac's `instinct-tools trash`) or moves into `trash` (the box); refuses paths
   outside the workspace and roots. Terminal writes into a write root outside the workspace are blocked with
   a pointer to `write_file`/`patch`/`files_trash` (they cannot be versioned).
6. **Activity.** `post_tool_call`: a successful tool listed in `tier1_notify` appends one JSON line
   `{"did": "<plain sentence>", "tool", "at"}` to `activity`. No message bodies, no addresses in clear.

## Tests (`tests/plugins/iollo_permissions/`, the style of `tests/plugins/browser/`)
- Each tier-4 pattern blocks, including quoting tricks, symlinked targets and `sudo` wrappers; blocks happen
  with the judge stubbed to APPROVE and under yolo.
- Judge stub: APPROVE → no directive; ESCALATE → `approve` with a `tier3:` reason; DENY → `block`.
- Judge timeout → fallback: a fake card-number `browser_type` is tier 3, a normal `send_message` is tier 1.
- Versions are written before overwrite and pruned; `files_trash` uses the command or directory and refuses
  outside paths; the activity line is appended once per success and never on failure.
- The whole existing suite stays green with the plugin disabled (the default).

## Acceptance
With the fixture policy on a local profile: `rm -rf /etc` and `diskutil eraseDisk` are refused without a
model call; with a stub judge, a checkout click raises one approval and an email send raises none.

## Out of scope
The cloud side (policy file, shim tier mode, pipeline, persona: cloud 031); the Mac app (Mac 029); upstreaming.

## Finish with this summary
```
Files changed: (list)
Tests run: (commands and results)
Assumptions: (list, or "none")
Open questions: (list, or "none")
```
