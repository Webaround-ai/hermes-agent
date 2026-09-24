# Brief 001: one tag publishes the box base image and the Mac runtime tarball, nothing signed by us

Written 2026-09-24 for the iollo release train. This fork had no product briefs before this one.
Work on the `iollo` branch. Never touch `main`, which tracks upstream untouched (see docs/IOLLO-FORK.md).

Read first: `docs/IOLLO-FORK.md`, `.github/workflows/iollo-release.yml`, `scripts/iollo/release.py`,
`scripts/iollo/sign-release.sh`, `scripts/iollo/verify-bundle.sh`, `tests/scripts/iollo/test_release.py`,
and the published `release.json` of `iollo-2026.9.14-rc4`.

## Goal (decided by Ricardo, 2026-09-24)
The release train has one mechanism. The Mac app embeds the Hermes runtime and the persona, ships as
one notarized DMG, and updates itself. Boxes take the same fork tag as their base image. This fork's
job: on an `iollo-*` tag, publish the box base image and an arm64 Mac runtime tarball, with a SHA-256,
from the same commit. The Mac app build embeds that tarball. **Nothing is signed by us**: Apple's
signature and notarization of the app cover what it embeds. No minisign, no signing key.

## Order and dependencies
- This brief goes **first**. Mac brief `Webaround-ai/iollo-mac` `briefs/028-app-self-update.md` pins a
  tag of this fork and embeds its tarball. Cloud brief `Webaround-ai/iollo` `briefs/030-release-train-promote.md`
  rolls boxes onto `hermes-base-<tag>` and comes last.
- The rc4 tarball already exists, so the Mac brief can start against rc4 while this one lands.

## What already exists (scope down)
Shipped 2026-09-23/24 (commits d275abf22b to 807f4c612f):
- `iollo-release.yml` on `iollo-*` tags does the following:
  - builds the root `Dockerfile` and pushes `registry.fly.io/instinct-sandboxes:hermes-base-<tag>` (and private GHCR copies);
  - builds and smoke-boots `hermes-runtime-<tag>-macos-arm64.tar.zst` (x86_64 best effort);
  - publishes a GitHub release with `release.json` and `SHA256SUMS`.
- The tarball layout (`bin/hermes`, `python/`, `hermes/`, `VERSION`, `MANIFEST.json`, ...) is what the
  Mac's `RuntimeInstaller`/`RuntimeLayout` accept. **Do not change the layout.**
- Minisign signing exists (`sign-release.sh`, secrets `IOLLO_MINISIGN_SECRET_KEY`/`_PASSWORD`,
  `docs/iollo/iollo.minisign.pub`). The new design drops it.

## Build
1. **Remove our signing.** In `iollo-release.yml` drop every minisign install/sign/verify-with-key step,
   the `IOLLO_MINISIGN_*` secret references, the `iollo.minisign.pub` asset and the SIGNED/UNSIGNED title
   logic. Replace `scripts/iollo/sign-release.sh` with `scripts/iollo/make-release.sh <tag> <dir>`,
   which only runs `release.py create` (release.json + SHA256SUMS). Delete `docs/iollo/iollo.minisign.pub`.
   `verify-bundle.sh` keeps its integrity checks (size, SHA-256, manifest, path safety, boot) and loses
   the public-key argument and the unsigned warning.
2. **Train manifest.** `release.py create` writes:
   ```json
   {"schema": 1, "tag": "...", "commit": "<40-hex tagged checkout>", "upstream_version": "...",
    "upstream_commit": "...", "image": "<unchanged GHCR sha tag>",
    "box_base_image": "registry.fly.io/instinct-sandboxes:hermes-base-<tag>",
    "box_base_digest": "sha256:<64 hex> or null",
    "assets": [{"name": "...", "arch": "arm64", "sha256": "...", "size": 0,
                "url": "https://github.com/Webaround-ai/hermes-agent/releases/download/<tag>/<name>"}]}
   ```
   `signature_name` goes. `box_base_digest` comes from a new `--box-digest` option. CI passes the `box`
   job's `docker/build-push-action` digest through a job output; locally it is null. `check_archive()`
   accepts the rc4 shape and schema 1.
3. **Self-check.** `release.py check-train <release.json>` validates schema 1: tag regex, 40-hex commit,
   `box_base_image` equal to the expected tag, digest format, an arm64 asset whose `url` is this tag's
   download URL. It prints one plain line per problem and exits non-zero. After publishing, the
   `release` job downloads its own `release.json` and runs it.
4. **Docs.** Update `docs/IOLLO-FORK.md`:
   - "Release layout" gets the schema-1 fields.
   - "Signing and trust" becomes: we do not sign; the Mac app embeds the tarball at build time,
     checks it against the SHA-256 pinned in its own repo, and Apple notarization covers the result.
   - "Open integration decisions" gets the answers: the Mac pins a tag and embeds its tarball; the cloud
     promote command rolls boxes onto the tag.
   - After merge, Ricardo may delete the two minisign secrets (optional, not required by any step).

Do not change models or persona text. There is no persona here; do not add one. Do not touch Hermes
behaviour, the root `Dockerfile`, `main` or any existing tag. Never move or reuse a published tag.

## Tests
In `tests/scripts/iollo/test_release.py`, existing style:
- `create` writes schema 1 with `commit`, `box_base_image`, `box_base_digest` and per-asset `url`, and no `signature_name`.
- An invalid `--box-digest` is rejected.
- `check_archive` still accepts an rc4-shaped manifest.
- `check-train` passes a good manifest and names each problem in a bad one (wrong base tag, no arm64,
  URL for another tag, bad commit).
- Replace the signature-required test with an integrity-only one; keep the extraction-safety test.

Run `scripts/run_tests.sh tests/scripts/iollo/test_release.py` (never bare pytest), then
`actionlint .github/workflows/iollo-release.yml` and `shellcheck scripts/iollo/*.sh`.

## Acceptance
Ricardo (not the agent) tags `iollo-2026.9.14-rc5` on `iollo` and pushes it. The run publishes
`hermes-base-iollo-2026.9.14-rc5` on the Fly registry, the arm64 tarball, `SHA256SUMS` and a schema-1
`release.json` that passes `check-train`, with no `.minisig` files. Mac brief 028's pin script accepts the
tag and URL/SHA-256 as published. Cloud brief 030 rolls boxes onto the same tag.

## Out of scope
The Mac app build and the persona (Mac brief 028), promotion and box rollout (cloud brief 030), an Intel
runner, rebasing on a new upstream.

## Finish with this summary
```
Files changed: (list)
Tests run: (commands and results)
Assumptions: (list, or "none")
Open questions: (list, or "none")
```
