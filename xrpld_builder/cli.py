"""`xrpld-builder` — compose, build, push, manifest."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import build as build_mod
from . import git_push
from .compose import MANIFEST_FILE, TREE_DIR, ComposeError, Manifest, compose, plan
from .conf import BranchEntry, ConfError, parse_config

SUCCESS = "SUCCESS"


def _sources(args: argparse.Namespace) -> tuple[BranchEntry, list[BranchEntry],
                                                BranchEntry | None, BranchEntry | None]:
    """(base, branches, datagram, target) from --conf or --src/--features."""
    datagram = BranchEntry.parse(args.datagram, "datagram") if args.datagram else None
    if args.conf:
        if args.src or args.features:
            raise ConfError("--conf excludes --src and --features")
        config = parse_config(args.conf)
        return config.base, config.branches, datagram, config.target
    if not args.src:
        raise ConfError("one of --conf or --src is required")
    base = BranchEntry.parse(args.src)
    return base, [BranchEntry.parse(f) for f in args.features], datagram, None


def cmd_compose(args: argparse.Namespace) -> int:
    base, branches, datagram, target = _sources(args)
    ordered = plan(branches, datagram)
    if args.dry_run:
        print(f"base    {base.label}")
        for i, entry in enumerate(ordered, 1):
            suffix = "  (datagram)" if entry is datagram else ("  rebase" if entry.rebase else "")
            print(f"merge {i:>2} {entry.label}{suffix}")
        print(f"target  {target.label if target else '(none)'}")
        print(f"force_supported {args.force_supported}")
        return 0
    try:
        manifest = compose(base, branches, args.workdir, datagram=datagram, target=target,
                           force_supported=args.force_supported)
    except ComposeError as e:
        sys.exit(f"[xrpld-builder] {e}")
    print(f"composed {manifest.composed_sha} at {Path(args.workdir) / TREE_DIR}")
    return 0


def _tag(args: argparse.Namespace, ref: str, sha: str, datagram: bool) -> str:
    return args.tag or build_mod.image_tag(ref, sha, datagram)


def cmd_build(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    pool = args.pool or None
    try:
        if args.tree:
            tree = Path(args.tree)
            manifest_path = tree.parent / MANIFEST_FILE
            if not manifest_path.is_file():
                sys.exit(f"[xrpld-builder] {manifest_path} not found — run `xrpld-builder compose` first")
            manifest = Manifest.load(manifest_path)
            if manifest.failed:
                sys.exit(f"[xrpld-builder] manifest records unmerged branches: {manifest.failed}")
            base = BranchEntry.from_slug(manifest.base)
            name = BranchEntry.from_slug(manifest.target).branch if manifest.target else base.branch
            force_supported = args.force_supported or manifest.force_supported
            tag = _tag(args, name, manifest.composed_sha, bool(manifest.datagram))
            record = build_mod.submit_tree(tree, args.project, args.ar, tag, pool=pool,
                                           force_supported=force_supported, ci_image=args.ci_image)
            record.update(build_server=f"https://github.com/{base.slug}/tree/{base.branch}",
                          build_version=manifest.base_sha, composed_sha=manifest.composed_sha)
        else:
            if not args.src:
                sys.exit("[xrpld-builder] one of --tree or --src is required")
            source = BranchEntry.parse(args.src)
            datagram = BranchEntry.parse(args.datagram, "datagram") if args.datagram else None
            sha = build_mod.resolve_sha(source.url, source.branch)
            force_supported = args.force_supported or "OFF"
            tag = _tag(args, source.branch, sha, datagram is not None)
            record = build_mod.submit_branch(source, sha, args.project, args.ar, tag, datagram=datagram,
                                             pool=pool, force_supported=force_supported,
                                             ci_image=args.ci_image)
            record.update(build_server=f"https://github.com/{source.slug}/tree/{source.branch}",
                          build_version=sha)
    except build_mod.BuildError as e:
        sys.exit(f"[xrpld-builder] {e}")
    record.update(tag=tag, ar=args.ar, project=args.project, pool=pool, force_supported=force_supported)
    build_mod.write_build(workdir, record)
    if record["status"] != SUCCESS:
        sys.exit(f"[xrpld-builder] build {record['build_id']} finished {record['status']}")
    print(record["image"])
    return 0


def cmd_push(args: argparse.Namespace) -> int:
    build_path = Path(args.build)
    if not build_path.is_file():
        sys.exit(f"[xrpld-builder] {build_path} not found — build first")
    record = build_mod.load_build(build_path)
    if record.get("status") != SUCCESS:
        sys.exit(f"[xrpld-builder] build status is {record.get('status')!r}, not SUCCESS — refusing to push")
    manifest = Manifest.load(args.manifest)
    if manifest.failed:
        sys.exit(f"[xrpld-builder] manifest records unmerged branches: {manifest.failed}")
    target_text = args.target or manifest.target
    if not target_text:
        sys.exit("[xrpld-builder] no --target and the manifest names none")
    target = BranchEntry.from_slug(target_text)
    tree = str(args.tree)
    head = git_push._run(["git", "rev-parse", "HEAD"], cwd=tree).stdout.strip()
    if head != manifest.composed_sha:
        sys.exit(f"[xrpld-builder] {tree} HEAD {head[:12]} is not the manifest's composed_sha "
                 f"{manifest.composed_sha[:12]}")
    message = (f"compose: {target.branch} from {manifest.base} @ {manifest.base_sha[:8]} "
               f"({len(manifest.branches)} branches)\n\n{manifest.trailer()}")
    try:
        git_push.setup_signing(tree)
        sha = git_push.commit_all(tree, message, include_untracked=False, allow_empty=True)
        git_push.push(tree, target.owner, target.repo, target.branch, force=True)
    except (git_push.SigningNotConfigured, RuntimeError) as e:
        sys.exit(f"[xrpld-builder] {e}")
    print(f"pushed {sha} -> {target.label}")
    return 0


def cmd_manifest(args: argparse.Namespace) -> int:
    manifest = Manifest.load(Path(args.dir) / MANIFEST_FILE)
    print(manifest.markdown() if args.markdown else manifest.trailer(), end="")
    if not args.markdown:
        print()
    return 0


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="xrpld-builder",
                                 description="Compose a multi-branch xrpld tree, build it on Cloud Build, push it signed.")
    sub = ap.add_subparsers(dest="command", required=True)

    c = sub.add_parser("compose", help="clone the base and merge the branches in order")
    c.add_argument("--conf", help="conf file: base/target header, then owner/repo branch [rebase] lines")
    c.add_argument("--src", help="base github URL (tree/commit/repo) when no --conf")
    c.add_argument("--features", nargs="*", default=[], help="ordered github URLs merged after the datagram")
    c.add_argument("--datagram", default="", help="datagram github URL or owner/repo@branch, merged first")
    c.add_argument("--workdir", required=True, help="directory receiving rippled/ and manifest.json")
    c.add_argument("--dry-run", action="store_true", help="print the plan and exit without cloning")
    c.add_argument("--force-supported", default="OFF", choices=["ON", "OFF"],
                   help="recorded in the manifest as the build default")
    c.set_defaults(func=cmd_compose)

    b = sub.add_parser("build", help="compile a composed tree or a single branch on Cloud Build")
    b.add_argument("--tree", help="composed tree (DIR/rippled); manifest.json is read from DIR")
    b.add_argument("--src", help="branch github URL to build from scratch")
    b.add_argument("--datagram", default="", help="datagram ref merged inside the build (--src only)")
    b.add_argument("--project", required=True, help="GCP project")
    b.add_argument("--ar", required=True, help="Artifact Registry repo, e.g. us-central1-docker.pkg.dev/<project>/xrpld")
    b.add_argument("--tag", help="image tag (default <safe-branch>-<sha8>[-dg])")
    b.add_argument("--pool", default="", help="private worker pool name ('' = default pool)")
    b.add_argument("--force-supported", choices=["ON", "OFF"],
                   help="compile every amendment as Supported::Yes (default: manifest value, else OFF)")
    b.add_argument("--ci-image", help="override the CI image resolved from the strategy-matrix")
    b.add_argument("--workdir", required=True, help="directory receiving build.json")
    b.set_defaults(func=cmd_build)

    p = sub.add_parser("push", help="GPG-sign the composed tree and force-with-lease push it")
    p.add_argument("--tree", required=True, help="composed tree (DIR/rippled)")
    p.add_argument("--target", help="owner/repo@branch (default: the manifest's target)")
    p.add_argument("--manifest", required=True, help="manifest.json written by compose")
    p.add_argument("--build", required=True, help="build.json written by build; status must be SUCCESS")
    p.set_defaults(func=cmd_push)

    m = sub.add_parser("manifest", help="print the manifest trailer")
    m.add_argument("dir", help="workdir holding manifest.json")
    m.add_argument("--markdown", action="store_true", help="print a per-branch table instead")
    m.set_defaults(func=cmd_manifest)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfError as e:
        sys.exit(f"[xrpld-builder] {e}")


if __name__ == "__main__":
    sys.exit(main())
