# multibranch-builder

Compose a multi-branch source tree for an XRPLF repository, build it, and push the composed
tree to an integration branch as a GPG-signed commit. One package, one `kind` per repository
family (xrpld today), so the perf network (XRPLF/xrpld-perfnet) and the alphanet network
(XRPLF/xrplf-alphanet-network) depend on it instead of carrying their own copies.

```
multibranch-builder compose   clone the base, merge each branch in order, run the kind's prepare step, write manifest.json
multibranch-builder build     build the composed tree (or one branch) the kind's way, write build.json
multibranch-builder push      GPG-sign the composed tree and force-with-lease push it to the target branch
multibranch-builder manifest  print the manifest trailer (or a markdown table)
multibranch-builder kinds     list the kinds: tree_dir, default branch, options, settings
```

Each subcommand reads and writes one work directory:

```
<workdir>/
  <tree_dir>/      the composed git tree (rippled/ for the xrpld kind)
  manifest.json    what compose did: base, base_sha, kind, tree_dir, branches[{repo, branch, sha, outcome, rebase}],
                   composed_sha, target, options, prepared
  build.json       what build did: status, kind, options, tag, build_server, build_version, composed_sha,
                   plus the kind's own fields (xrpld: image, build_id, ci_image, ar, project, pool)
```

## Kinds

A kind describes one repository family: the directory its tree is cloned into, its default
branch, the merge drivers and attributes it registers, the guide appended to the resolver
prompt, the `--set` options it accepts, the conf settings it reads, a prepare step that runs
after the merges, and the build step. The kind comes from the conf's `kind` line, else from
the base repository (`rippled` or `xrpld*` means xrpld). Each kind is a package under
`multibranch_builder/targets/<kind>/`.

| kind | tree_dir | default branch | options | settings | prepare | build |
|---|---|---|---|---|---|---|
| `xrpld` | `rippled` | `develop` | `force_supported` ON\|OFF, `datagram` | none | none | Cloud Build with the dockerfiles under `targets/xrpld/cloudbuild/` |
| `xrpl_js` | `xrpl.js` | `main` | none | `definitions` (required) | writes the node's `server_definitions` over `packages/ripple-binary-codec/src/enums/definitions.json` and regenerates `package-lock.json` | checks the tree is clean and the definitions carry the five keys the codec loads plus `hash` |

The xrpl_js kind takes `definitions <json-rpc url>` from the conf and calls `server_definitions`
and `server_info` on that node, so it composes against a node that is already running the
branches the SDK tree merges, and the manifest's `prepared.build_version` names that node.
`definitions.json` and `package-lock.json` carry a `merge=ours` attribute: they keep the base
tree's content through every merge and get their composed content from the prepare step, which
is why neither reaches the conflict resolver.

## Conflict handling

Every merge runs `git merge --no-commit --no-ff <remote>/<branch>` and ends in one of four
outcomes recorded per branch in the manifest:

| outcome | meaning |
|---|---|
| `up-to-date` | the branch is already contained in the tree; nothing staged |
| `merged` | clean merge |
| `ai-resolved` | conflicts were finished by `git rerere`, by a kind's merge driver, or by `claude -p` |
| `conflict` | nothing could resolve it; the merge is aborted and the tree left clean |

Two mechanisms run before claude sees a conflict:

- The kind's merge drivers, registered repo-locally (`.git/info/attributes`, never committed).
  For xrpld, `multibranch_builder/targets/xrpld/registry_merge.py` handles `features.macro`,
  `ledger_entries.macro`, `transactions.macro`, `sfields.macro` and `jss.h`: it unions
  independent entries and renumbers an incoming entry whose numeric id collides. Anything it
  cannot prove safe stays a normal conflict.
- `git rerere` with `rerere.autoUpdate` replays resolutions recorded earlier in the same clone.

What remains goes to the local `claude` CLI: `claude -p <prompt> --permission-mode
bypassPermissions --allowedTools Read,Edit,Bash,Glob,Grep --add-dir <tree> --max-budget-usd 5`
under a one-hour subprocess timeout. The prompt lists the conflicted files and appends the
kind's guide (`multibranch_builder/targets/xrpld/merge.md` for xrpld: registry-number
collisions, namespace and file-move rules, the per-file strategy). After claude returns, files
that still contain conflict markers or stay unmerged make the outcome `conflict`.

`compose` attempts every branch even after a `conflict`, writes the manifest, then exits
non-zero. `build` and `push` refuse a manifest that records any `conflict` or no `composed_sha`.

## The conf format

```
# comments start with #
kind   xrpld                            # optional; default from the base repository
base   XRPLF/rippled develop            # required, exactly once
target XRPLF/rippled xrplf/alphanet     # optional; where `push` sends the tree

XRPLF/rippled xrplf/smart-contracts     # <owner/repo> <branch> [rebase], merged in file order
XRPLF/rippled dangell7/subscriptions rebase
XRPLF/rippled dangell7/datagram
```

Any other `<setting> <value>` header line is handed to the kind, which accepts or rejects it
(the xrpld kind accepts none; the xrpl_js kind requires `definitions <json-rpc url>`). `rebase` is parsed and recorded in the manifest
(`branches[].rebase`) for the network repos that keep a branch in step with develop;
`compose` itself does not act on it.

## CLI contract

```
multibranch-builder compose  (--conf FILE | --src URL [--features URL...]) --workdir DIR
                             [--set KEY=VALUE ...] [--dry-run]
```

- `--set` passes options to the kind, validated after the conf is parsed; `kinds` lists them.
  For xrpld: `force_supported=ON|OFF` (default OFF) is recorded in the manifest as the build
  default, and `datagram=<ref>` is merged before the conf entries.
- `--dry-run` prints the plan (kind, base, ordered branches, target, settings, options) and
  exits 0 without cloning.
- URLs are github `tree`, `commit` or repo URLs; `owner/repo@branch` is also accepted. A bare
  repo means the kind's default branch; a bare `datagram=owner/repo` means the `datagram` branch.

```
multibranch-builder build    --workdir DIR [--tree PATH | --src URL] [--set KEY=VALUE ...] [--tag TAG]
                             [--project P] [--ar AR] [--pool POOL] [--ci-image IMG]
```

- Without `--src`, builds the composed tree: `DIR/manifest.json` names the kind, the tree
  directory and the options; `--set` overrides the options. `--tree` defaults to
  `DIR/<tree_dir>`.
- `--src` builds one branch from scratch with the kind its repository implies; for xrpld the
  `datagram` option is fetched and merged inside the build and a real conflict fails the build
  (use `compose` for that case).
- Default tag: `<safe-branch>-<sha8>` plus the kind's suffix (`-dg` for an xrpld datagram
  build), where the branch is the target branch for a composed tree (else the base branch) and
  the sha is the composed sha; for `--src` it is the source branch and its resolved sha; a
  `commit` URL gives `<sha8>`.
- xrpld needs `--project` and `--ar`: `--ar` is passed through unchanged as `_AR` and the image
  is `<AR>/xrpld:<tag>`; `--pool` submits with `--worker-pool
  projects/P/locations/us-central1/workerPools/POOL --region us-central1`, without it
  `--machine-type E2_HIGHCPU_32` on the default pool. The tree is uploaded without `.git` and
  compiled with `targets/xrpld/cloudbuild/composed.dockerfile`; a branch with
  `xrpld.dockerfile`. The CI image comes from `.github/scripts/strategy-matrix/linux.json`
  (`image_tag` → `ghcr.io/xrplf/xrpld/nix-ubuntu:<image_tag>`): read from the tree, or via
  `gh api repos/<slug>/contents/...?ref=<sha>` for `--src`. When that lookup fails it prints a
  WARNING and uses the pinned default in the dockerfile, which may be stale. The submission is
  `gcloud builds submit --async`; the log is streamed with `gcloud builds log --stream` and the
  final status read with `gcloud builds describe`.
- `build.json` is written either way; the exit code is non-zero unless the status is
  `SUCCESS`. The last line on stdout is the image ref (or the kind's summary).
- `build.json` carries `build_server` (the base or source tree URL) and `build_version` (the
  base or source branch sha, never the composed sha): xrpld-lab resolves the amendments to
  enable from the branch commit, so `--build_version` must name that commit.

```
multibranch-builder push     --workdir DIR [--tree PATH] [--manifest PATH] [--build PATH]
                             [--target owner/repo@branch]
```

- Paths default from `--workdir`; without it all three must be given.
- Refuses unless `build.json` exists with `status == SUCCESS`, the manifest records no
  `conflict` and a `composed_sha`, and the tree's HEAD is that `composed_sha`.
- `--target` defaults to the manifest's `target` (the conf's `target` line).
- Configures signing from the environment, creates one signed commit on top of the composed
  tree (an empty commit when nothing else changed) whose message is
  `compose: <branch> from <base> @ <sha8> (<n> branches)` with the trailer
  `Multibranch-Builder-Manifest: <compact manifest json>`, verifies the signature with
  `git verify-commit HEAD`, and pushes with `--force-with-lease=refs/heads/<branch>:<sha the
  branch has right now>` over PAT-authenticated HTTPS. Unsigned, or without a PAT, it refuses.

```
multibranch-builder manifest DIR [--markdown]
multibranch-builder kinds [--kind NAME]
```

`manifest` prints the `Multibranch-Builder-Manifest:` trailer, or a per-branch outcome table
for a job summary. `kinds` prints each kind's tree_dir, default branch, options and settings.

## Environment variables

Read at call time, never at import, never printed.

| variable | used by | meaning |
|---|---|---|
| `GITHUB_BOT_PAT` | push | PAT of the service account; the only credential that can push |
| `GIT_BOT_NAME` | compose, push | git `user.name`; compose falls back to `multibranch-builder` |
| `GIT_BOT_EMAIL` | compose, push | git `user.email`; must match a uid on the signing key |
| `GIT_SIGNING_KEY` | push | the armored GPG private key, or its base64 |
| `ANTHROPIC_API_KEY` | compose | consumed by the `claude` CLI when it resolves conflicts (unset locally to use your own login) |

## Runtime prerequisites

- `git`, `gpg`
- `gcloud` (Cloud SDK) authenticated against the build project — xrpld `build`
- `gh` authenticated — xrpld `build --src` CI image lookup (falls back loudly without it)
- `claude` (`npm install -g @anthropic-ai/claude-code`) — `compose` when a merge conflicts
- `npm` — xrpl_js `compose`, to regenerate `package-lock.json` (recorded as not refreshed without it)
- Python 3.11+

## Reusable workflow

`.github/workflows/compose.yml` is a `workflow_call` workflow. Inputs: `conf-path`,
`project`, `ar`, `pool`, `options` (space-separated `KEY=VALUE` pairs passed as `--set`),
`dry-run`, `workload-identity-provider`, `service-account`, `bot-name`, `bot-email`,
`multibranch-builder-ref`. Secrets: `GITHUB_BOT_PAT`, `GPG_PRIVATE_KEY`, `ANTHROPIC_API_KEY`.
It checks out the caller, installs this package and the claude CLI, authenticates to GCP with
`google-github-actions/auth`, then runs compose, build and push on green unless `dry-run`, and
writes the per-branch outcome table to the job summary.

```yaml
jobs:
  alphanet:
    uses: XRPLF/multibranch-builder/.github/workflows/compose.yml@main
    with:
      conf-path: alphanet.conf
      project: xrplf-alphanet
      ar: us-central1-docker.pkg.dev/xrplf-alphanet/xrpld
      pool: xrpld-pool
      options: "force_supported=ON"
      workload-identity-provider: projects/…/locations/global/workloadIdentityPools/…/providers/…
      service-account: cloud-build@xrplf-alphanet.iam.gserviceaccount.com
    secrets: inherit
```

## Development

```
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
```
