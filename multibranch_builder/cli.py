"""`multibranch-builder` — compose, build, push, manifest, kinds."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import git_push
from .build import BUILD_FILE, BuildError, image_tag, load_build, resolve_sha, write_build
from .compose import MANIFEST_FILE, ComposeError, Manifest, compose
from .conf import BranchEntry, Config, ConfError, parse_config
from .targets import KINDS, for_base, for_config, for_name
from .targets.base import BuildRequest, Kind

SUCCESS = "SUCCESS"


def parse_set(values: list[str]) -> dict[str, str]:
    """`--set KEY=VALUE` pairs as a dict."""
    out: dict[str, str] = {}
    for item in values:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise ConfError(f"--set expects KEY=VALUE, got {item!r}")
        out[key] = value
    return out


def _source(text: str) -> tuple[BranchEntry, Kind]:
    """A `--src` URL or slug and the kind its repository implies; a bare repo means the kind's default branch."""
    kind = for_base(BranchEntry.parse(text, "probe").slug)
    return BranchEntry.parse(text, kind.default_branch), kind


def _sources(args: argparse.Namespace) -> tuple[Config, Kind]:
    """The conf and its kind from --conf, or from --src/--features."""
    if args.conf:
        if args.src or args.features:
            raise ConfError("--conf excludes --src and --features")
        config = parse_config(args.conf)
        return config, for_config(config)
    if not args.src:
        raise ConfError("one of --conf or --src is required")
    base, kind = _source(args.src)
    features = [BranchEntry.parse(f, kind.default_branch) for f in args.features]
    return Config(base=base, branches=features), kind


def cmd_compose(args: argparse.Namespace) -> int:
    config, kind = _sources(args)
    options = kind.validate_options(parse_set(args.set))
    kind.validate_settings(config.settings)
    ordered = kind.plan(config.branches, options)
    if args.dry_run:
        print(f"kind    {kind.name}")
        print(f"base    {config.base.label}")
        for i, entry in enumerate(ordered, 1):
            suffix = "  (option)" if entry not in config.branches else ("  rebase" if entry.rebase else "")
            print(f"merge {i:>2} {entry.label}{suffix}")
        print(f"target  {config.target.label if config.target else '(none)'}")
        for key, value in sorted(config.settings.items()):
            print(f"setting {key}={value}")
        print("options " + (" ".join(f"{k}={v}" for k, v in sorted(options.items())) or "(none)"))
        return 0
    try:
        manifest = compose(config, args.workdir, kind=kind, options=options)
    except ComposeError as e:
        sys.exit(f"[multibranch-builder] {e}")
    print(f"composed {manifest.composed_sha} at {Path(args.workdir) / kind.tree_dir}")
    return 0


def _load_manifest(path: Path) -> Manifest:
    if not path.is_file():
        sys.exit(f"[multibranch-builder] {path} not found — run `multibranch-builder compose` first")
    manifest = Manifest.load(path)
    if manifest.failed:
        sys.exit(f"[multibranch-builder] manifest records unmerged branches: {manifest.failed}")
    if not manifest.composed_sha:
        sys.exit("[multibranch-builder] manifest records no composed tree (prepare failed) — compose again")
    return manifest


def cmd_build(args: argparse.Namespace) -> int:
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    pool = args.pool or None
    overrides = parse_set(args.set)
    try:
        if args.src:
            source, kind = _source(args.src)
            options = kind.validate_options(overrides)
            sha = resolve_sha(source.url, source.branch)
            tag = args.tag or image_tag(source.branch, sha, kind.image_suffix(options))
            record = kind.build(BuildRequest(options=options, tag=tag, source=source, sha=sha,
                                             project=args.project, ar=args.ar, pool=pool,
                                             ci_image=args.ci_image))
            record.update(build_server=f"https://github.com/{source.slug}/tree/{source.branch}",
                          build_version=sha)
        else:
            manifest = _load_manifest(workdir / MANIFEST_FILE)
            kind = for_name(manifest.kind)
            options = kind.validate_options({**manifest.options, **overrides})
            tree = Path(args.tree) if args.tree else workdir / manifest.tree_dir
            if not tree.is_dir():
                sys.exit(f"[multibranch-builder] no composed tree at {tree}")
            base = BranchEntry.from_slug(manifest.base)
            name = BranchEntry.from_slug(manifest.target).branch if manifest.target else base.branch
            tag = args.tag or image_tag(name, manifest.composed_sha, kind.image_suffix(options))
            record = kind.build(BuildRequest(options=options, tag=tag, tree=tree, manifest=manifest,
                                             sha=manifest.composed_sha, project=args.project, ar=args.ar,
                                             pool=pool, ci_image=args.ci_image))
            record.update(build_server=f"https://github.com/{base.slug}/tree/{base.branch}",
                          build_version=manifest.base_sha, composed_sha=manifest.composed_sha)
    except BuildError as e:
        sys.exit(f"[multibranch-builder] {e}")
    record.update(kind=kind.name, tag=tag, ar=args.ar, project=args.project, pool=pool, options=options)
    write_build(workdir, record)
    if record.get("status") != SUCCESS:
        sys.exit(f"[multibranch-builder] build {record.get('build_id', '')} finished {record.get('status')}")
    print(record.get("image") or record.get("summary", ""))
    return 0


def cmd_push(args: argparse.Namespace) -> int:
    if not args.workdir and not (args.tree and args.manifest and args.build):
        raise ConfError("push needs --workdir, or --tree, --manifest and --build")
    workdir = Path(args.workdir) if args.workdir else None
    build_path = Path(args.build) if args.build else workdir / BUILD_FILE
    if not build_path.is_file():
        sys.exit(f"[multibranch-builder] {build_path} not found — build first")
    record = load_build(build_path)
    if record.get("status") != SUCCESS:
        sys.exit(f"[multibranch-builder] build status is {record.get('status')!r}, not SUCCESS — refusing to push")
    manifest = _load_manifest(Path(args.manifest) if args.manifest else workdir / MANIFEST_FILE)
    target_text = args.target or manifest.target
    if not target_text:
        sys.exit("[multibranch-builder] no --target and the manifest names none")
    target = BranchEntry.from_slug(target_text)
    tree = str(Path(args.tree) if args.tree else workdir / manifest.tree_dir)
    head = git_push._run(["git", "rev-parse", "HEAD"], cwd=tree).stdout.strip()
    if head != manifest.composed_sha:
        sys.exit(f"[multibranch-builder] {tree} HEAD {head[:12]} is not the manifest's composed_sha "
                 f"{manifest.composed_sha[:12]}")
    message = (f"compose: {target.branch} from {manifest.base} @ {manifest.base_sha[:8]} "
               f"({len(manifest.branches)} branches)\n\n{manifest.trailer()}")
    try:
        git_push.setup_signing(tree)
        sha = git_push.commit_all(tree, message, include_untracked=False, allow_empty=True)
        git_push.push(tree, target.owner, target.repo, target.branch, force=True)
    except (git_push.SigningNotConfigured, RuntimeError) as e:
        sys.exit(f"[multibranch-builder] {e}")
    print(f"pushed {sha} -> {target.label}")
    return 0


def cmd_manifest(args: argparse.Namespace) -> int:
    manifest = Manifest.load(Path(args.dir) / MANIFEST_FILE)
    print(manifest.markdown() if args.markdown else manifest.trailer(), end="")
    if not args.markdown:
        print()
    return 0


def cmd_kinds(args: argparse.Namespace) -> int:
    for kind in [for_name(args.kind)] if args.kind else KINDS.values():
        print(kind.name)
        print(f"  tree_dir        {kind.tree_dir}")
        print(f"  default_branch  {kind.default_branch}")
        for key, text in sorted(kind.options.items()):
            print(f"  option   {key}: {text}")
        for key, text in sorted(kind.settings.items()):
            print(f"  setting  {key}: {text}")
    return 0


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="multibranch-builder",
                                 description="Compose a multi-branch tree, build it, push it signed.")
    sub = ap.add_subparsers(dest="command", required=True)
    set_help = "KEY=VALUE option for the conf's kind (see `kinds`); repeatable"

    c = sub.add_parser("compose", help="clone the base and merge the branches in order")
    c.add_argument("--conf", help="conf file: base/target/kind/setting headers, then owner/repo branch [rebase] lines")
    c.add_argument("--src", help="base github URL (tree/commit/repo) or owner/repo[@branch] when no --conf")
    c.add_argument("--features", nargs="*", default=[], help="ordered github URLs merged after the base")
    c.add_argument("--workdir", required=True, help="directory receiving <tree_dir>/ and manifest.json")
    c.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help=set_help)
    c.add_argument("--dry-run", action="store_true", help="print the plan and exit without cloning")
    c.set_defaults(func=cmd_compose)

    b = sub.add_parser("build", help="build the composed tree in --workdir, or a single branch")
    b.add_argument("--workdir", required=True, help="directory holding manifest.json; receives build.json")
    b.add_argument("--tree", help="composed tree (default: <workdir>/<tree_dir> from the manifest)")
    b.add_argument("--src", help="branch github URL or owner/repo[@branch] to build from scratch")
    b.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                   help=set_help + "; overrides the manifest's options")
    b.add_argument("--tag", help="image tag (default <safe-branch>-<sha8> plus the kind's suffix)")
    b.add_argument("--project", help="GCP project (xrpld)")
    b.add_argument("--ar", help="Artifact Registry repo, e.g. us-central1-docker.pkg.dev/<project>/xrpld (xrpld)")
    b.add_argument("--pool", default="", help="private worker pool name ('' = default pool)")
    b.add_argument("--ci-image", help="override the CI image resolved from the strategy-matrix (xrpld)")
    b.set_defaults(func=cmd_build)

    p = sub.add_parser("push", help="GPG-sign the composed tree and force-with-lease push it")
    p.add_argument("--workdir", help="directory holding manifest.json, build.json and the tree")
    p.add_argument("--tree", help="composed tree (default: <workdir>/<tree_dir>)")
    p.add_argument("--target", help="owner/repo@branch (default: the manifest's target)")
    p.add_argument("--manifest", help="manifest.json (default: <workdir>/manifest.json)")
    p.add_argument("--build", help="build.json; status must be SUCCESS (default: <workdir>/build.json)")
    p.set_defaults(func=cmd_push)

    m = sub.add_parser("manifest", help="print the manifest trailer")
    m.add_argument("dir", help="workdir holding manifest.json")
    m.add_argument("--markdown", action="store_true", help="print a per-branch table instead")
    m.set_defaults(func=cmd_manifest)

    k = sub.add_parser("kinds", help="list the kinds: tree_dir, default branch, options, settings")
    k.add_argument("--kind", help="one kind only")
    k.set_defaults(func=cmd_kinds)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfError as e:
        sys.exit(f"[multibranch-builder] {e}")


if __name__ == "__main__":
    sys.exit(main())
