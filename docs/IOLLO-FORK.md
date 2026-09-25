# Iollo Hermes distribution

The product consumes releases from **Webaround-ai/hermes-agent** and images from
**ghcr.io/webaround-ai/hermes-agent**. A single `iollo-*` tag produces the Linux box
image and both Mac Python runtimes. This machinery does not change Hermes behavior,
deploy boxes, update an installed Mac app, or change the upstream `hermes update`
command. The box deployer and Mac app must select these Iollo artifacts themselves;
they must not run upstream's installer/updater as their product update path.

## Branches and the verified starting point

- `main` tracks `NousResearch/hermes-agent:main`, without Iollo changes.
- `iollo` is an upstream release tag plus our small, isolated patches. Distribution
  machinery lives in `scripts/iollo/`, this document, one workflow and its tests.
  The additive runs-event trace patch is documented in [IOLLO-RUN-TRACE.md](IOLLO-RUN-TRACE.md).
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
brew install zstd minisign actionlint shellcheck
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
ghcr.io/webaround-ai/hermes-agent:<tag>
ghcr.io/webaround-ai/hermes-agent:<tag>-<12-character-checkout-sha>

GitHub release assets:
  hermes-runtime-<tag>-macos-arm64.tar.zst
  hermes-runtime-<tag>-macos-x86_64.tar.zst
  <each archive>.minisig                 # signed releases only
  release.json
  release.json.minisig                   # signed releases only
  SHA256SUMS
  SHA256SUMS.minisig                     # signed releases only
  iollo.minisign.pub                     # reference copy, NOT a trust anchor
```

The shared reply producer is vendored unchanged under `iollo_envelope/`, with its
source commit in `iollo_envelope/VENDORED`. Package discovery and catalog package
data include it in both the Mac wheel and box image. The bounded install hook at
the end of `gateway/platforms/api_server_runs.py` adds envelope SSE and inputs
routes and enriches run status with the same locally stored envelope. It retains
the existing runs authorization and callback seams. Mac callers may provide
`envelope_context` with `surface: mac` and a public integer `conversation_id`.
The producer uses `HERMES_HOME/state.db`; it does not import the control plane.
The build and relocated smoke verify imports, catalog data, revision identity,
and envelope route authentication. The scripted integration test is
`tests/gateway/test_iollo_envelope_runtime.py`.

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
runtime. The archive's outer hash and signature cover the manifest too.
`release.json` has `{tag, upstream_version, upstream_commit, image, assets}`; each
asset has `{name, arch, sha256, size, signature_name}` with a byte count and either
its `.minisig` name or `null`. `SHA256SUMS` covers both archives and `release.json`.

## Signing and trust

The **Iollo minisign Ed25519 key** signs each compressed archive, `release.json`
and `SHA256SUMS`. It does not sign Git tags, OCI images, Mach-O binaries or the Mac
app. Apple Developer ID signing/notarization of the app is separate.
If either signing secret is absent, signatures are omitted and the release title
and body explicitly say **UNSIGNED**. If both are present but invalid, signing
fails the release rather than falling back to unsigned publication.

Generate the encrypted key once on a trusted Mac, upload it directly to repository
secrets, and remove the temporary local secret file. Keep the public key in the Mac
app source; the app must embed and trust that key, never a key downloaded alongside
an update. Use a dedicated Iollo distribution key, not an upstream or Apple key.
The only persistent private-key copy should be GitHub Secrets; losing it requires
an app release that establishes trust in a replacement public key.

```sh
brew install minisign
umask 077
key_dir=$(mktemp -d)
minisign -G -s "$key_dir/iollo.key" -p "$key_dir/iollo.pub"+# Choose a strong nonempty single-line password at the minisign prompts.
gh secret set IOLLO_MINISIGN_SECRET_KEY --repo Webaround-ai/hermes-agent < "$key_dir/iollo.key"
gh secret set IOLLO_MINISIGN_PASSWORD --repo Webaround-ai/hermes-agent
# Enter the same password at gh's hidden prompt; do not put it in shell arguments.
cp "$key_dir/iollo.pub" /path/to/mac-app/resources/iollo.minisign.pub
rm -rf "$key_dir"
```

CI materializes the private key only in a mode-restricted temporary directory,
supplies its password on stdin, disables shell tracing and removes the directory
on exit. PR jobs receive no signing secrets and cannot publish.
See the [minisign usage and signature format](https://jedisct1.github.io/minisign/).

## Local and CI verification

Prerequisites: native macOS for the chosen architecture, git, curl, Python 3.9+
for the packaging helpers, zstd, and minisign for signed releases. No developer
Python environment is used to run Hermes. Keep the output directory outside the
checkout (or under the already-ignored `dist/`). A successful build automatically
relocates to a path containing spaces, runs imports and help checks, boots the
gateway with a temporary `HOME` and `HERMES_HOME`, uses a random fake API key to
GET `/v1/models`, waits for running state, and stops it with the upstream SIGINT
foreground shutdown contract. It then creates and verifies the archive.

```sh
brew install zstd minisign
scripts/iollo/build-mac-bundle.sh iollo-2026.9.14 arm64 /tmp/iollo-runtime
scripts/iollo/verify-bundle.sh /tmp/iollo-runtime/hermes-runtime-iollo-2026.9.14-macos-arm64.tar.zst

# For a downloaded signed release: put release.json, release.json.minisig,
# the archive and its .minisig together. Supply the app's trusted public key.
scripts/iollo/verify-bundle.sh /path/to/archive.tar.zst /path/to/iollo.minisign.pub

# To repeat the full smoke against an already extracted runtime:
/path/to/runtime/python/bin/python3 scripts/iollo/smoke-bundle.py /path/to/runtime
scripts/run_tests.sh tests/scripts/iollo/test_release.py
```

The verifier first checks size/SHA256 against adjacent `release.json`. Signed
releases require a trusted public key (a file or base64 minisign key); supplying
one also forbids an unsigned downgrade. It verifies the archive and metadata
signatures before extraction, rejects path escapes/special files, checks the
complete manifest and embedded tag, then boots `bin/hermes --version` with empty
environment, temporary user state and a different working directory. Unsigned
verification emits a warning and checks integrity only. A production app should
require signatures. Select an expected tag and prevent rollback in the app's
update policy; minisign alone does not implement version ordering.

PRs targeting `iollo` get an arm64 dry-run with the same builder and smoke checks;
the unsigned archive, checksum list and metadata are workflow artifacts for seven
days. Tag jobs verify each architecture natively, publish the box image, and only
then stage a GitHub draft containing all assets before publishing the release.
If final publication fails, inspect/delete the incomplete draft before rerunning
that job; never overwrite a release consumers have already downloaded.

## Owner setup and product integration

Enable Actions and select the Intel runner as above, allow the jobs' scoped token permissions,
and ensure this repository can write its GHCR package. If the package already exists,
grant it Actions access to this repository. Choose public package visibility if Fly
pulls anonymously; otherwise provision Fly's registry pull credentials. GitHub's
repository/package visibility settings are separate. Configure both minisign secrets
and embed the matching public key in the app before relying on signed updates.

Open integration decisions: confirm whether cloud boxes really use the release tag
or the later `e10934b0` commit; enable an Intel runner; choose GHCR visibility; wire
the Mac updater and box deployer to this fork's releases, choose stable versus RC
channels, and define staged rollout/rollback policy. These consumer changes are
outside this build/release-only patch.

## Box image on the Fly registry (added 2026-09-24)

The `box` job also pushes the same image to `registry.fly.io/instinct-sandboxes:hermes-base-<tag>`,
using the repository secret `FLY_API_TOKEN` (a Fly deploy token scoped to the `instinct-sandboxes`
app). The iollo cloud repository's `sandbox/Dockerfile` starts `FROM` that tag, so a fork release is the
box base image with no manual mirroring step. GHCR keeps a copy for reference; it is private.
