# Iollo runtime integration

Source audit 2026-10-02: Hermes `430513efacf1` (`origin/iollo`, local rc9 tag); cloud
`4f8777d94dbe`. This file is the fork entrypoint, not a parallel product architecture.

Read cloud [ARCHITECTURE](https://github.com/Webaround-ai/iollo/blob/main/docs/ARCHITECTURE.md),
[WEBAROUND-ROUTER](https://github.com/Webaround-ai/iollo/blob/main/docs/WEBAROUND-ROUTER.md),
[CONVERSATIONS](https://github.com/Webaround-ai/iollo/blob/main/docs/CONVERSATIONS.md) and
[DATA-RETENTION](https://github.com/Webaround-ai/iollo/blob/main/docs/DATA-RETENTION.md).
The umbrella Iollo README identifies the local audited docs checkout while these edits are unmerged.

Iollo runs the personal agent on one Linux machine/volume per owner. Web is a device client; Mac is
menu bar/local tools. iOS and Desktop Pro are parked. Upstream room, desktop, messaging or local-agent
features are not evidence that Iollo exposes them. Sessions/delegates share owner filesystem, memory,
browser and services; distinct session IDs do not isolate resources for multiple people.

Cloud `profile_policy.py` owns managed models/persona/permissions; `sandbox/boxsync.py` installs them
under root-owned `/etc/hermes`. Provider master keys stay at Bifrost; the managed runtime receives a
per-user virtual key. Do not tell operators to use `hermes model` as a persistent policy override.
The cloud repo's `sandbox/hermes-base.txt` and build override select the runtime; this fork's branch
HEAD alone does not identify a deployed image.

| Runtime seam | Source | Integration rule |
|---|---|---|
| Durable runs | `gateway/platforms/api_server_runs.py` | Preserve declared session key, idempotency, provider/effort, envelope context and prompt version |
| Prompt refresh | `agent/prompt_version.py` | Changed declared version rebuilds stored prompt once; transcript/session retained; compression inherits version |
| Sessions | `hermes_state.py`, API server session handlers | Full owner-box transcript, distinct from control-plane retained copies |
| Delegation | `tools/delegate_tool_config.py`, related delegate/escalate modules | Policy supplies tiers and required gates; no secret or budget bypass |
| Provider adapters | `agent/` provider transports | Preserve selected transport and cache prefix; OpenAI configuration updates are appended and validated |

Read the relevant nested AGENTS.md before touching each runtime area. When changing an integration
seam, update this pointer and the cloud specialist guide/tests with a pinned source revision. Never
claim an upstream feature is deployed without image/config evidence. Preserve private history and
credentials; document only sanitized behavior and credential names.
