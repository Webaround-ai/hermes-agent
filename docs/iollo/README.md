# Iollo runtime integration

Source review 2026-10-03: Hermes `a49765487900` (`iollo-2026.9.24-rc10`); cloud
`af8ee4c`. This file is the fork entrypoint, not a parallel product architecture.

Read cloud [ARCHITECTURE](https://github.com/Webaround-ai/iollo/blob/main/docs/ARCHITECTURE.md),
[WEBAROUND-ROUTER](https://github.com/Webaround-ai/iollo/blob/main/docs/WEBAROUND-ROUTER.md),
[CONVERSATIONS](https://github.com/Webaround-ai/iollo/blob/main/docs/CONVERSATIONS.md) and
[DATA-RETENTION](https://github.com/Webaround-ai/iollo/blob/main/docs/DATA-RETENTION.md).
These are the canonical cloud guides; check their dated source and live evidence before operations.
For native TypeSafe transport and shim retirement, read cloud
[JEV](https://github.com/Webaround-ai/iollo/blob/main/docs/JEV.md).

Iollo runs the personal agent on one Linux machine/volume per owner. Web is a device client; Mac is
menu bar/local tools. iOS and Desktop Pro are parked. Upstream room, desktop, messaging or local-agent
features are not evidence that Iollo exposes them. Sessions/delegates share owner filesystem, memory,
browser and services; distinct session IDs do not isolate resources for multiple people.

Cloud `profile_policy.py` owns managed models/persona/permissions; `sandbox/boxsync.py` installs them
under root-owned `/etc/hermes`. Provider master keys stay at Bifrost; the managed runtime receives a
per-user virtual key. Do not tell operators to use `hermes model` as a persistent policy override.
The cloud repo's `sandbox/hermes-base.txt` pins rc10 at this review; `sandbox/Dockerfile`
has the same fallback and the sandbox workflow selects an explicit train override or that pin.
Record the final sandbox image/digest and rollout separately: this fork's branch HEAD or tag alone
does not identify a deployed image. See cloud
[DEPLOY](https://github.com/Webaround-ai/iollo/blob/main/docs/DEPLOY.md) and
[RELEASES](https://github.com/Webaround-ai/iollo/blob/main/docs/RELEASES.md).

| Runtime seam | Source | Integration rule |
|---|---|---|
| Durable runs | `gateway/platforms/api_server_runs.py` | Preserve declared session key, idempotency, provider/effort, envelope context and prompt version |
| Tool profile / turn-ending tools | `agent/tool_profile.py`, `agent/turn_tool_round.py`, `agent/turn_api_request.py` | Per-run `tool_profile` projects requests only (agent, stored prompt, pin stay full); `ends_turn` ends a succeeded text+tool round; rc12 branch: globs and `read_tools` (annotated read-only MCP tools, budgeted) and per-run `skip_background_review`; see [IOLLO-FORK](../IOLLO-FORK.md) |
| Prompt refresh | `agent/prompt_version.py`, `agent/conversation_loop.py` | Changed declared version rebuilds stored prompt and re-pins current tools once; transcript/session retained; compression inherits version |
| Sessions | `hermes_state.py`, API server session handlers | Full owner-box transcript, distinct from control-plane retained copies |
| Delegation | `tools/delegate_tool_config.py`, related delegate/escalate modules | Policy supplies tiers and required gates; no secret or budget bypass |
| Provider adapters | `agent/` provider transports | Preserve selected transport and cache prefix; OpenAI configuration updates are appended and validated |
| Native text stream | `gateway/platforms/api_server_runs.py` | `message.delta` events carry `stream_id` captured on the producing thread; consumers replace retried drafts; rc12 branch: Responses commentary beside turn-ending calls becomes the visible reply |
| Run progress events | `gateway/platforms/api_server_run_progress.py` | rc12 branch: `tool.generating {tool}` (name only, ≤ every 10 s) and `run.heartbeat` (≤ every 12 s while the agent is alive and silent); status `last_event` moves, never during an approval, never in transcripts |
| Recorded voice | `gateway/platforms/api_server_audio.py`, [voice API](voice-api.md) | Opt-in authenticated transcription/read-aloud; bounded temporary audio and busy accounting; no agent turn |

Read the relevant nested AGENTS.md before touching each runtime area. When changing an integration
seam, update this pointer and the cloud specialist guide/tests with a pinned source revision. Never
claim an upstream feature is deployed without image/config evidence. Preserve private history and
credentials; document only sanitized behavior and credential names.

Distribution and upstream-upgrade details live in [IOLLO-FORK](../IOLLO-FORK.md);
trace data handling lives in [IOLLO-RUN-TRACE](../IOLLO-RUN-TRACE.md). rc7–rc10 are
cloud runtime tags without a published GitHub runtime release as of this review.
A historical train manifest or native runtime bundle must not override the recorded cloud pin.
