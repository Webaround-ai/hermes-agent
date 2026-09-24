# Iollo Hermes distribution

The product consumes releases from **Webaround-ai/hermes-agent** and images from
**ghcr.io/webaround-ai/hermes-agent**. A single `iollo-*` tag produces the Linux box
image and both Mac Python runtimes. This machinery does not change Hermes behavior,
deploy boxes, update an installed Mac app, or change the upstream `hermes update`
command. The box deployer and Mac app must select these Iollo artifacts themselves;
they must not run upstream's installer/updater as their product update path.

## Branches and the verified starting point

- `main` tracks `NousResearch/hermes-agent:main`, without Iollo changes.
- `iollo` is an upstream release tag plus our patches: distribution machinery isolated in
  `scripts/iollo/`, this document, one workflow and its tests; and the bundled
  `plugins/iollo-permissions/` plugin (brief 002: tier-4 hard blocks, the approval model as
  tier judge, versions, `files_trash`, activity file), off unless enabled, plus one generic
  hook in `tools/approval_smart.py` (`register_rubric_provider`) that it uses.
- The initial base is upstream **v2026.9.14**, package version **0.21.3**, commit
  **345cd2b057a452236de401d3534b8502a7465e8d** (the peeled annotated tag).
  The previously recorded **e10934b03e115b036b3c38b9c6e6817039a5962d** is a later
  September 20 commit, not this tag. Confirm the running cloud boxes separately
  before assuming they have exactly this source.

```sh
git remote -v
git rev-parse 'v2026.9.14^{commit}'
git show v2026.9.14:pyproject.toml
```

## Taking an upstream release

Start from a clean checkout and record the old base before rebasing. Never move or
reuse a published Iollo tag. Branch protection should permit a coordinated rebase
of `iollo`, while protecting release tags from modification/deletion.

```sh
git fetch upstream main --tags
git switch main
git merge --ff-only upstream/main
git push origin main

git switch iollo
old_base=v2026.9.14
new_base=v2026.10.1                 # example; choose an actual reviewed upstream tag
git rebase --onto "$new_base" "$old_base" iollo
# Resolve conflicts in distribution files, preserving upstream runtime behavior.
uv sync --frozen --extra dev --extra messaging --extra mcp --extra web
scripts/run_tests.sh               # full upstream suite, never bare pytest
brew install zstd actionlint shellcheck
actionlint .github/workflows/iollo-release.yml
shellcheck scripts/iollo/*.sh
scripts/iollo/build-mac-bundle.sh "iollo-${new_base#v}-rc1" arm64 /tmp/iollo-candidate
git push --force-with-lease origin iollo
# Review the arm64 PR dry-run before tagging a candidate/release.
git tag -a "iollo-${new_base#v}" -m "Iollo runtime based on $new_base"
git push origin "iollo-${new_base#v}"
```

For a rebuild/packaging revision use `iollo-2026.9.14-1`, then `-2`, etc. Candidate
tags end in `-rcN`, for example `iollo-2026.9.14-1-rc1`, and become prereleases.
Tags without that suffix become normal releases. An upstream `v` prefix is also
accepted (`iollo-v2026.9.14`), but prefer the forms above consistently.
The workflow resolves the upstream tag to its full commit and verifies that it is
an ancestor of the tagged checkout; it does not fetch/build upstream HEAD.

Review upstream packaging, extras, Python support and launch paths on every rebase.
Update `python-standalone.json` deliberately when upgrading Python: use exact
python-build-standalone asset URLs, verify their SHA256s against the publisher's
release digests, and update both architectures together. Currently both are
CPython **3.12.14**, standalone release **20260901**, unstripped `install_only`
archives. The local and CI builder are the same script.

## Release layout

The workflow `.github/workflows/iollo-release.yml` runs on `iollo-*` tag pushes.
Publishing is restricted to this fork. It produces:

```text
registry.fly.io/instinct-sandboxes:hermes-base-<tag>   # the box base image
ghcr.io/webaround-ai/hermes-agent:<tag>
ghcr.io/webaround-ai/hermes-agent:<tag>-<12-character-checkout-sha>

GitHub release assets:
  hermes-runtime-<tag>-macos-arm64.tar.zst
  hermes-runtime-<tag>-macos-x86_64.tar.zst   # best effort
  release.json
  SHA256SUMS
```

The box uses the **unchanged root Dockerfile**, built from the tagged checkout,
and pushes with `GITHUB_TOKEN` and job-scoped `packages: write`. It is **linux/amd64
only**: Fly boxes do not need a multi-architecture image. `release.json.image` is
the SHA-suffixed image tag; deployments can additionally record the GHCR digest.
Neither an upstream `latest` image nor upstream release assets are used.

Mac builds are native: `macos-15` for arm64 and **`macos-15-large` for x86_64**.
The Intel larger runner requires owner access/billing configuration. Set repository
variable `IOLLO_MACOS_X86_RUNNER=macos-15-intel` to use GitHub's standard native Intel
runner when the larger runner is unavailable. There is no
claimed cross-build fallback: python-build-standalone publishes separate Darwin
architecture archives, and a universal interpreter alone would not make all
third-party native extension wheels universal. Native builds let CI actually boot
both artifacts. Select/enable the Intel runner before tagging; do not silently
ship an untested cross-build.
See GitHub's [standard runner labels](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)
and [larger runner labels](https://docs.github.com/en/actions/reference/runners/larger-runners).

Each archive extracts into one directory named like the archive without `.tar.zst`:

```text
bin/hermes                # relative launcher; preserves the caller's HERMES_HOME
python/                   # complete standalone Python + installed packages
hermes/                   # complete tracked checkout, no .git or untracked secrets
VERSION                   # exact Iollo release tag
MANIFEST.json             # regular-file hashes/sizes/modes and symlink hashes/targets
PYTHON-STANDALONE.json     # reviewed interpreter URLs and SHA256s
requirements.lock.txt     # selected upstream uv.lock closure, with hashes
DEPENDENCIES-REPORT.json   # pip's dependency installation receipt
INSTALL-REPORT.json       # pip's installation receipt for this checkout
```

The builder exports `uv.lock` using a temporary **uv 0.11.6** environment, installs
the dependency closure with pip's hash checking, then runs `pip install --no-deps`
on this checkout with **messaging, mcp, web** extras. `aiohttp` in messaging powers
the `api_server` gateway adapter; MCP and the web/serve stack are also included.
There is no standalone `api_server --help`: it is an adapter enabled through gateway
configuration. CI checks the gateway CLI help, the `gateway.run` module help and
the actual HTTP server in a foreground gateway.

Upstream's `setup.py` blocks normal wheel builds because wheels omit source-relative
assets. The builder uses the existing **HERMES_NIX_BUILD=1** switch only for the pip
build and retains the tracked source tree beside the installed wheel. A relative
`.pth` entry makes that complete tree importable from the bundled interpreter,
including child Python processes. No flag is set at runtime to pretend this is Nix.
No package contents, lazy modules, skills, plugins, locales or stdlib modules are
stripped. Python console-script shebangs are made relative as part of packaging.
The only source-tree cleanup removes pip-generated build/egg-info directories.

`bin/hermes` uses only `python/bin/python3`, prevents host Python path/user-site
injection, and exports the supplied `HERMES_HOME`; if absent, it asks Hermes's own
`get_hermes_home()` for the default. It preserves the working directory and arguments.
Hermes's own `--version` remains 0.21.3; `VERSION` is the Iollo product release tag.
The distribution is called `hermes-agent`; this checkout has **no import package
named `hermes_agent`**. Smoke tests import its real top-level modules instead.

This is the Python gateway/API runtime, not a Mac app/DMG, bundled JS dashboard,
Node runtime, browser download, or every optional provider/tool's external binary.
Those optional integrations retain upstream's setup/lazy-install behavior. The app
and box orchestrator own the product update policy and any additional tool provisioning.

`MANIFEST.json` lists every regular file and symlink except itself (a self-hash is
impossible). Symlinks hash the literal link-target bytes and must stay inside the
runtime. The archive's outer SHA-256 covers the manifest too.

`release.json` is the release train manifest, schema 1:

```json
{"schema": 1, "tag": "<tag>", "commit": "<40-hex tagged checkout>",
 "upstream_version": "v2026.9.14", "upstream_commit": "<40-hex>",
 "image": "ghcr.io/webaround-ai/hermes-agent:<tag>-<12-hex>",
 "box_base_image": "registry.fly.io/instinct-sandboxes:hermes-base-<tag>",
 "box_base_digest": "sha256:<64 hex> or null",
 "assets": [{"name": "hermes-runtime-<tag>-macos-arm64.tar.zst", "arch": "arm64",
             "sha256": "<64 hex>", "size": 0,
             "url": "https://github.com/Webaround-ai/hermes-agent/releases/download/<tag>/<name>"}]}
```

`box_base_digest` is the `box` job's pushed image digest; local builds write `null`.
Releases up to `iollo-2026.9.14-rc4` have the older shape (no `schema`, `commit`,
`box_base_*` or `url`; a `signature_name` per asset). `check-archive` accepts both.
`SHA256SUMS` covers the archives and `release.json`. After publishing, the `release`
job downloads its own `release.json` and runs `release.py check-train` on it, which
prints one line per problem and fails the job.

## Signing and trust

We do not sign anything. There is no minisign key, signature or public key. The Mac
app embeds the arm64 tarball at build time, checks it against the SHA-256 pinned in
its own repository, and Apple's signature and notarization of the app cover the
result. Boxes take `box_base_image` for the same tag. `SHA256SUMS` and the per-asset
SHA-256 are integrity checks only. After this change the old repository secrets
`IOLLO_MINISIGN_SECRET_KEY` and `IOLLO_MINISIGN_PASSWORD` are unused; the owner may
delete them.

## Local and CI verification

Prerequisites: native macOS for the chosen architecture, git, curl, Python 3.9+
for the packaging helpers, and zstd. No developer
Python environment is used to run Hermes. Keep the output directory outside the
checkout (or under the already-ignored `dist/`). A successful build automatically
relocates to a path containing spaces, runs imports and help checks, boots the
gateway with a temporary `HOME` and `HERMES_HOME`, uses a random fake API key to
GET `/v1/models`, waits for running state, and stops it with the upstream SIGINT
foreground shutdown contract. It then creates and verifies the archive.

```sh
brew install zstd
scripts/iollo/build-mac-bundle.sh iollo-2026.9.14 arm64 /tmp/iollo-runtime
scripts/iollo/verify-bundle.sh /tmp/iollo-runtime/hermes-runtime-iollo-2026.9.14-macos-arm64.tar.zst

# For a downloaded release: put release.json and the archive together.
scripts/iollo/verify-bundle.sh /path/to/archive.tar.zst
python3 scripts/iollo/release.py check-train /path/to/release.json

# To repeat the full smoke against an already extracted runtime:
/path/to/runtime/python/bin/python3 scripts/iollo/smoke-bundle.py /path/to/runtime
scripts/run_tests.sh tests/scripts/iollo/test_release.py
```

The verifier first checks size/SHA256 against adjacent `release.json`, then
rejects path escapes/special files, checks the complete manifest and embedded tag,
and boots `bin/hermes --version` with empty environment, temporary user state and a
different working directory. Version ordering and rollback belong to the Mac app,
which pins one tag.

PRs targeting `iollo` get an arm64 dry-run with the same builder and smoke checks;
the archive, checksum list and metadata are workflow artifacts for seven
days. Tag jobs verify each architecture natively, publish the box image, and only
then stage a GitHub draft containing all assets before publishing the release.
If final publication fails, inspect/delete the incomplete draft before rerunning
that job; never overwrite a release consumers have already downloaded.

## Owner setup and product integration

Enable Actions and select the Intel runner as above, allow the jobs' scoped token permissions,
and ensure this repository can write its GHCR package. If the package already exists,
grant it Actions access to this repository. Choose public package visibility if Fly
pulls anonymously; otherwise provision Fly's registry pull credentials. GitHub's
repository/package visibility settings are separate.

Open integration decisions, answered 2026-09-24: the Mac app pins one fork tag and
embeds that tag's arm64 tarball, checked against the SHA-256 in its own repository
(iollo-mac brief 028); the cloud promote command rolls boxes onto
`hermes-base-<tag>` for the same tag (iollo brief 030). Still open: an Intel runner
and GHCR visibility. These consumer changes live in those repositories.

## Box image on the Fly registry (added 2026-09-24)

The `box` job also pushes the same image to `registry.fly.io/instinct-sandboxes:hermes-base-<tag>`,
using the repository secret `FLY_API_TOKEN` (a Fly deploy token scoped to the `instinct-sandboxes`
app). The iollo cloud repository's `sandbox/Dockerfile` starts `FROM` that tag, so a fork release is the
box base image with no manual mirroring step. GHCR keeps a copy for reference; it is private.
