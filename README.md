# xrpld-compose

Compose a multi-branch xrpld source tree, build it on Google Cloud Build, and push the
composed tree to a target branch as a GPG-signed commit. One package, three verbs, so the
perf network (XRPLF/xrpld-perfnet) and the alphanet network (XRPLF/xrplf-alphanet-network)
depend on it instead of carrying their own copies.

```
xrpld-compose compose   clone the base, merge each branch in order, write manifest.json
xrpld-compose build     compile the composed tree (or one branch) on Cloud Build, write build.json
xrpld-compose push      GPG-sign the composed tree and force-with-lease push it to the target branch
xrpld-compose manifest  print the manifest trailer (or a markdown table)
```

Each subcommand reads and writes one work directory:

```
<workdir>/
  rippled/         the composed git tree
  manifest.json    what compose did: base, base_sha, branches[{repo, branch, sha, outcome}], composed_sha
  build.json       what build did: image, build_id, status, tag, ar, project, build_server, build_version
```

## Conflict handling

Every merge runs `git merge --no-commit --no-ff <remote>/<branch>` and ends in one of four
outcomes recorded per branch in the manifest:

| outcome | meaning |
|---|---|
| `up-to-date` | the branch is already contained in the tree; nothing staged |
| `merged` | clean merge |
| `ai-resolved` | conflicts were finished by `git rerere`, by the registry merge driver, or by `claude -p` |
| `conflict` | nothing could resolve it; the merge is aborted and the tree left clean |

Two mechanisms run before claude sees a conflict:

- `xrpld_compose/registry_merge.py` is registered as the repo-local merge driver
  (`.git/info/attributes`, never committed) for `features.macro`, `ledger_entries.macro`,
  `transactions.macro`, `sfields.macro` and `jss.h`. It unions independent entries and
  renumbers an incoming entry whose numeric id collides. Anything it cannot prove safe stays
  a normal conflict.
- `git rerere` with `rerere.autoUpdate` replays resolutions recorded earlier in the same clone.

What remains goes to the local `claude` CLI: `claude -p <prompt> --permission-mode
bypassPermissions --allowedTools Read,Edit,Bash,Glob,Grep --add-dir <tree> --max-budget-usd 5`
under a one-hour subprocess timeout. The prompt lists the conflicted files and appends
`xrpld_compose/merge.md`, the resolution guide (registry-number collisions, namespace and
file-move rules, the per-file strategy). After claude returns, files that still contain
conflict markers or stay unmerged make the outcome `conflict`.

`compose` attempts every branch even after a `conflict`, writes the manifest, then exits
non-zero. `build --tree` and `push` refuse a manifest that records any `conflict`.

## The conf format

```
# comments start with #
base   XRPLF/rippled develop            # required, exactly once
target Transia-RnD/rippled alphanet     # optional; where `push` sends the tree

XRPLF/rippled xrplf/smart-contracts     # <owner/repo> <branch> [rebase], merged in file order
XRPLF/rippled dangell7/subscriptions rebase
XRPLF/rippled dangell7/datagram
```

`rebase` is parsed and recorded in the manifest (`branches[].rebase`) for the network repos
that keep a branch in step with develop; `compose` itself does not act on it.

## CLI contract

```
xrpld-compose compose  --conf FILE | (--src URL [--features URL...]) [--datagram URL]
                       --workdir DIR [--dry-run] [--force-supported ON|OFF]
```

- Merge order: the `--datagram` ref first when given, then the conf entries (or
  `--features`) in order. `--dry-run` prints the plan (base, ordered branches, target) and
  exits 0 without cloning.
- URLs are github `tree`, `commit` or repo URLs; `owner/repo@branch` is also accepted. A bare
  repo URL means `develop`; a bare `--datagram owner/repo` means the `datagram` branch.
- `--force-supported` is recorded in the manifest as the build default.

```
xrpld-compose build    (--tree DIR/rippled | --src URL [--datagram URL])
                       --project P --ar AR [--tag TAG] [--pool POOL] [--force-supported ON|OFF]
                       [--ci-image IMG] --workdir DIR
```

- `--tree` uploads the composed tree without `.git` and compiles it with
  `cloudbuild/composed.dockerfile` via `cloudbuild/cloudbuild.composed.yaml`. It reads
  `DIR/manifest.json` for the shas and the force-supported default.
- `--src` compiles one branch from scratch with `cloudbuild/xrpld.dockerfile` via
  `cloudbuild/cloudbuild.yaml`; the datagram ref is fetched and merged inside the build and a
  real conflict fails the build (use `compose` for that case).
- Default tag: `<safe-branch>-<sha8>[-dg]`, where the branch is the target branch for a
  composed tree (else the base branch) and the sha is the composed sha; for `--src` it is the
  source branch and its resolved sha; a `commit` URL gives `<sha8>[-dg]`.
- `--ar` is required and is passed through unchanged as `_AR`; the image is `<AR>/xrpld:<tag>`.
- `--pool` submits with `--worker-pool projects/P/locations/us-central1/workerPools/POOL
  --region us-central1`; without it, `--machine-type E2_HIGHCPU_32` on the default pool.
- The CI image comes from `.github/scripts/strategy-matrix/linux.json` (`image_tag` →
  `ghcr.io/xrplf/xrpld/nix-ubuntu:<image_tag>`): read from the tree for `--tree`, via
  `gh api repos/<slug>/contents/...?ref=<sha>` for `--src`. When that lookup fails it prints a
  WARNING and uses the pinned default in the dockerfile, which may be stale.
- The submission is `gcloud builds submit --async`; the log is streamed with `gcloud builds
  log --stream` and the final status read with `gcloud builds describe`. `build.json` is
  written either way; the exit code is non-zero unless the status is `SUCCESS`. The image ref
  is the last line on stdout.
- `build.json` carries `build_server` (the base or source tree URL) and `build_version` (the
  base or source branch sha, never the composed sha): xrpld-lab resolves the amendments to
  enable from the branch commit, so `--build_version` must name that commit.

```
xrpld-compose push     --tree DIR/rippled [--target owner/repo@branch]
                       --manifest DIR/manifest.json --build DIR/build.json
```

- Refuses unless `build.json` exists with `status == SUCCESS`, the manifest records no
  `conflict`, and the tree's HEAD is the manifest's `composed_sha`.
- `--target` defaults to the manifest's `target` (the conf's `target` line).
- Configures signing from the environment, creates one signed commit on top of the composed
  tree (an empty commit when nothing else changed) whose message is
  `compose: <branch> from <base> @ <sha8> (<n> branches)` with the trailer
  `Xrpld-Compose-Manifest: <compact manifest json>`, verifies the signature with
  `git verify-commit HEAD`, and pushes with `--force-with-lease=refs/heads/<branch>:<sha the
  branch has right now>` over PAT-authenticated HTTPS. Unsigned, or without a PAT, it refuses.

```
xrpld-compose manifest DIR [--markdown]
```

Prints the `Xrpld-Compose-Manifest:` trailer, or a per-branch outcome table for a job summary.

## Environment variables

Read at call time, never at import, never printed.

| variable | used by | meaning |
|---|---|---|
| `GITHUB_BOT_PAT` | push | PAT of the service account; the only credential that can push |
| `GIT_BOT_NAME` | compose, push | git `user.name`; compose falls back to `xrpld-compose` |
| `GIT_BOT_EMAIL` | compose, push | git `user.email`; must match a uid on the signing key |
| `GIT_SIGNING_KEY` | push | the armored GPG private key, or its base64 |
| `ANTHROPIC_API_KEY` | compose | consumed by the `claude` CLI when it resolves conflicts (unset locally to use your own login) |

## Runtime prerequisites

- `git`, `gpg`
- `gcloud` (Cloud SDK) authenticated against the build project — `build`
- `gh` authenticated — `build --src` CI image lookup (falls back loudly without it)
- `claude` (`npm install -g @anthropic-ai/claude-code`) — `compose` when a merge conflicts
- Python 3.11+

## Reusable workflow

`.github/workflows/compose.yml` is a `workflow_call` workflow. Inputs: `conf-path`,
`project`, `ar`, `pool`, `force-supported`, `dry-run`, `workload-identity-provider`,
`service-account`, `bot-name`, `bot-email`, `xrpld-compose-ref`. Secrets: `GITHUB_BOT_PAT`,
`GPG_PRIVATE_KEY`, `ANTHROPIC_API_KEY`. It checks out the caller, installs this package and the
claude CLI, authenticates to GCP with `google-github-actions/auth`, then runs compose, build and
push on green unless `dry-run`, and writes the per-branch outcome table to the job summary.

```yaml
jobs:
  alphanet:
    uses: XRPLF/xrpld-compose/.github/workflows/compose.yml@main
    with:
      conf-path: alphanet.conf
      project: xrplf-alphanet
      ar: us-central1-docker.pkg.dev/xrplf-alphanet/xrpld
      pool: xrpld-pool
      force-supported: "ON"
      workload-identity-provider: projects/…/locations/global/workloadIdentityPools/…/providers/…
      service-account: cloud-build@xrplf-alphanet.iam.gserviceaccount.com
    secrets: inherit
```

## Development

```
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
```

## Lifted from

- **XRPLF/xrpld-perfnet @ 01ddd24** — `cloudbuild/compose.py` (clone, ordered merges, the
  registry driver registration, the `claude -p` resolver, the staged upload to
  `cloudbuild.composed.yaml`), `cloudbuild/compose_lib/registry_merge.py` and its tests
  (verbatim), the two Cloud Build configs and two dockerfiles, and the `build` Makefile
  target's sha resolution, tag derivation and strategy-matrix CI image lookup. Changes:
  `composed.dockerfile` now builds `b2/*` and runs `patchelf --set-interpreter` like
  `xrpld.dockerfile`; `cloudbuild.composed.yaml` takes `_CI_IMAGE`; the `xrpld-lab create:gcp`
  handoff print is gone (that subcommand no longer exists); `--ar` is passed through to every
  submission instead of being recomputed from the project.
- **Transia-RnD/sentinel-ai** — `libs/github/git_push.py` and its tests (identity and PAT
  from `GITHUB_BOT_PAT`/`GIT_BOT_NAME`/`GIT_BOT_EMAIL`, key from `GIT_SIGNING_KEY`, ref update
  reimplemented on the GitHub REST API with `requests`, plus the signed-HEAD and lease checks);
  from `services/xrpld.py` on branch `docs/merge-skill-registry-collisions`:
  `_files_with_conflict_markers`, `_merge_source_into_current` and its four outcomes,
  `_plan_integration` and its tests, the `owner/repo branch [rebase]` conf grammar, and
  `skills/rippled/commands/merge.md` (275 lines) as the resolver prompt. Not lifted: the
  per-branch build gate, the on-box AI build fixer, the fingerprint cache, the GitHub App
  token flow, the fork force-sync and the conf-drift guard.
