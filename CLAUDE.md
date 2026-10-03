## Iollo product focus — effective 2026-10-02

Owner decision: focus development on **web** and the **Mac menu bar companion and its local tools**. **iOS and Desktop Pro (Mac Pro / Electron client) are parked indefinitely**, until Ricardo explicitly resumes the relevant surface.

- Cloud and Hermes work may continue to support the active product. This does not authorize unrelated backend work.
- Do not implement, maintain feature parity, refresh dependencies or vendored assets, build releases, publish, or deploy iOS or Desktop Pro as part of routine work. Old briefs, roadmaps, TODOs, and cross-client checklists do not authorize resuming them.
- Shared web widgets may evolve for web; do not propagate them into Pro. Do not change or release the shared Swift renderer solely for iOS, or update iOS pins. Mac menu bar work remains active.
- Preserve parked code, existing work, and history. Parking is not permission to delete clients, remove services, or deliberately break existing clients. Flag an unavoidable compatibility impact before proceeding with that impact; do not silently expand scope into parked clients.
- A generic request to update Iollo, all clients, or the Mac app does not lift this freeze. Resume only on an explicit owner instruction naming iOS or Desktop Pro; a one-off exception does not resume ongoing development.
- Carry this scope into task plans, delegated prompts, and new worktrees. Before acting on an old brief, check it against this decision.

## Mandatory Iollo architecture reading

For Iollo integration work read [docs/iollo/README.md](docs/iollo/README.md) and its cloud architecture,
model-policy, conversation and retention links before changing runtime behavior. Upstream Hermes features
are not automatically Iollo product features. Iollo has one personal box per owner; sessions share box
resources and are not private-resource isolation for multiple people. Cloud owns managed model/persona
policy and per-user gateway keys; preserve transport, cache-prefix and approval boundaries.
Inspect status/worktrees and the cloud runtime pin, not only this checkout's branch. Keep documentation
synchronized with behavior and distinguish merged code, runtime pins and verified deployment facts.
