"""3-way git merge driver for xrpld's append-only registry files.

features.macro, ledger_entries.macro, transactions.macro, sfields.macro and
jss.h are registries of independent `MACRO(...)` entries. Textual merges
conflict whenever two branches append in the same region, but the domain
resolution is mechanical: union the entries, keep ours where both sides agree
with base, and renumber an incoming entry whose numeric id collides.

Registered as a repo-local merge driver (see `configure_repo`) so it fires
inside `git merge` for exactly these files, in the integration workspace only —
PR branches are never rewritten. Anything the structured merge cannot prove
safe (both sides modified one entry, parse failure, leftover duplicate) exits
non-zero, which leaves the normal conflict for the doctor agent.

Stdlib-only: git invokes this file directly as a script with
    python3 registry_merge.py %O %A %B %P
(the merged result must be written to %A; exit 0 means resolved).
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import Optional

# Filename → (numeric arg index, scope arg index or None). Files absent from
# this map get a pure union with no renumbering (features.macro, jss.h).
_NUMBERED: dict[str, tuple[int, Optional[int]]] = {
    "ledger_entries.macro": (1, None),
    "transactions.macro": (1, None),
    "sfields.macro": (2, 1),
}

_HANDLED = (
    "features.macro",
    "ledger_entries.macro",
    "transactions.macro",
    "sfields.macro",
    "jss.h",
)

_ENTRY_START = re.compile(r"^\s*([A-Z][A-Za-z0-9_]*)\s*\(")


@dataclass
class Entry:
    macro: str
    name: str          # first argument — the registry key
    text: str          # full entry text, including trailing newline(s)


def _strip_comments(line: str) -> str:
    return line.split("//", 1)[0]


def parse(text: str) -> Optional[list]:
    """Split file into a sequence of str (opaque text) and Entry chunks.

    Returns None when the file cannot be parsed confidently.
    """
    chunks: list = []
    lines = text.splitlines(keepends=True)
    i = 0
    opaque: list[str] = []
    while i < len(lines):
        m = _ENTRY_START.match(lines[i])
        if not m:
            opaque.append(lines[i])
            i += 1
            continue
        # Balance parens (comment-stripped) to find the end of the entry.
        depth = 0
        entry_lines = []
        start = i
        while i < len(lines):
            code = _strip_comments(lines[i])
            depth += code.count("(") - code.count(")")
            entry_lines.append(lines[i])
            i += 1
            if depth <= 0:
                break
        if depth > 0:
            return None  # unbalanced — refuse to guess
        entry_text = "".join(entry_lines)
        args = _first_args(entry_text, m.group(1))
        if args is None:
            return None
        if opaque:
            chunks.append("".join(opaque))
            opaque = []
        chunks.append(Entry(macro=m.group(1), name=args[0], text=entry_text))
    if opaque:
        chunks.append("".join(opaque))
    return chunks


def _first_args(entry_text: str, macro: str) -> Optional[list[str]]:
    """Top-level comma-split arguments of MACRO(...); nested parens/braces kept."""
    body_start = entry_text.index("(")
    depth = 0
    args: list[str] = []
    cur: list[str] = []
    for ch in _strip_multiline(entry_text[body_start:]):
        if ch in "({[":
            depth += 1
            if depth == 1:
                continue
        elif ch in ")}]":
            depth -= 1
            if depth == 0:
                break
        if ch == "," and depth == 1:
            args.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if cur:
        args.append("".join(cur).strip())
    if not args or not args[0]:
        return None
    return args


def _strip_multiline(text: str) -> str:
    out = []
    for line in text.splitlines(keepends=True):
        out.append(_strip_comments(line))
    return "".join(out)


def _entries(chunks: list) -> dict[tuple, Entry]:
    return {(c.macro, c.name): c for c in chunks if isinstance(c, Entry)}


def _number_of(entry: Entry, num_idx: int, scope_idx: Optional[int]):
    args = _first_args(entry.text, entry.macro)
    if args is None or len(args) <= num_idx:
        return None
    raw = args[num_idx].strip()
    try:
        value = int(raw, 0)
    except ValueError:
        return None
    scope = args[scope_idx].strip() if scope_idx is not None and len(args) > scope_idx else ""
    return scope, value, raw


def _renumber(entry: Entry, num_idx: int, old_raw: str, new_value: int) -> Entry:
    new_raw = f"0x{new_value:04X}" if old_raw.lower().startswith("0x") else str(new_value)
    # Skip exactly the arguments before the numeric one (all simple tokens in
    # these registries), then replace the number itself.
    pattern = re.compile(
        r"(\(\s*" + re.escape(entry.name) + r"\s*,"
        + r"(?:[^,()]*,){" + str(num_idx - 1) + r"}\s*)"
        + re.escape(old_raw)
    )
    new_text, n = pattern.subn(lambda m: m.group(1) + new_raw, entry.text, count=1)
    if n != 1:
        return entry  # could not substitute safely — caller re-validates
    return Entry(macro=entry.macro, name=entry.name, text=new_text)


def merge3(base_text: str, ours_text: str, theirs_text: str, filename: str) -> Optional[str]:
    """Structured 3-way merge. Returns merged text, or None if not provably safe."""
    base_c, ours_c, theirs_c = parse(base_text), parse(ours_text), parse(theirs_text)
    if base_c is None or ours_c is None or theirs_c is None:
        return None
    base, ours, theirs = _entries(base_c), _entries(ours_c), _entries(theirs_c)

    out_chunks: list = []
    # Walk OURS preserving its layout; apply theirs' modifications/deletions.
    for chunk in ours_c:
        if not isinstance(chunk, Entry):
            out_chunks.append(chunk)
            continue
        key = (chunk.macro, chunk.name)
        in_theirs = theirs.get(key)
        in_base = base.get(key)
        if in_theirs is not None and in_theirs.text != chunk.text:
            if in_base is not None and chunk.text == in_base.text:
                out_chunks.append(in_theirs)      # only theirs changed it
                continue
            if in_base is not None and in_theirs.text == in_base.text:
                out_chunks.append(chunk)          # only ours changed it
                continue
            return None                            # both changed — real conflict
        if in_theirs is None and in_base is not None:
            if in_base.text == chunk.text:
                continue                           # theirs deleted, ours untouched
            return None                            # theirs deleted what ours changed
        out_chunks.append(chunk)

    # Theirs' new entries (not in base, not in ours) — append after last entry.
    new_entries = [
        c for c in theirs_c
        if isinstance(c, Entry)
        and (c.macro, c.name) not in base
        and (c.macro, c.name) not in ours
    ]

    numbering = _NUMBERED.get(filename)
    if numbering and new_entries:
        num_idx, scope_idx = numbering
        used: dict[str, set[int]] = {}
        maxima: dict[str, int] = {}
        for c in out_chunks:
            if isinstance(c, Entry):
                info = _number_of(c, num_idx, scope_idx)
                if info:
                    scope, value, _ = info
                    used.setdefault(scope, set()).add(value)
                    maxima[scope] = max(maxima.get(scope, value), value)
        renumbered = []
        for entry in new_entries:
            info = _number_of(entry, num_idx, scope_idx)
            if info is None:
                return None
            scope, value, raw = info
            if value in used.get(scope, set()):
                new_value = maxima.get(scope, value) + 1
                entry = _renumber(entry, num_idx, raw, new_value)
                shown = f"0x{new_value:04X}" if raw.lower().startswith("0x") else str(new_value)
                print(
                    f"registry-merge: renumbered {entry.macro}({entry.name}) {raw} -> {shown}",
                    file=sys.stderr,
                )
                value = new_value
            used.setdefault(scope, set()).add(value)
            maxima[scope] = max(maxima.get(scope, value), value)
            renumbered.append(entry)
        new_entries = renumbered

    if new_entries:
        last_entry_idx = max(
            (i for i, c in enumerate(out_chunks) if isinstance(c, Entry)),
            default=None,
        )
        if last_entry_idx is None:
            return None
        insert_at = last_entry_idx + 1
        for offset, entry in enumerate(new_entries):
            out_chunks.insert(insert_at + offset, entry)

    merged = "".join(c.text if isinstance(c, Entry) else c for c in out_chunks)
    return merged if _validate(merged, filename) else None


def _validate(text: str, filename: str) -> bool:
    chunks = parse(text)
    if chunks is None:
        return False
    keys = [(c.macro, c.name) for c in chunks if isinstance(c, Entry)]
    if len(keys) != len(set(keys)):
        return False
    numbering = _NUMBERED.get(filename)
    if numbering:
        num_idx, scope_idx = numbering
        seen: set = set()
        for c in chunks:
            if isinstance(c, Entry):
                info = _number_of(c, num_idx, scope_idx)
                if info is None:
                    continue
                scope, value, _ = info
                if (c.macro, scope, value) in seen:
                    return False
                seen.add((c.macro, scope, value))
    return True


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        print("usage: registry_merge.py <base> <ours> <theirs> <pathname>", file=sys.stderr)
        return 1
    base_path, ours_path, theirs_path, pathname = argv[1:5]
    filename = pathname.rsplit("/", 1)[-1]
    if filename not in _HANDLED:
        return 1
    with open(base_path) as f:
        base_text = f.read()
    with open(ours_path) as f:
        ours_text = f.read()
    with open(theirs_path) as f:
        theirs_text = f.read()
    merged = merge3(base_text, ours_text, theirs_text, filename)
    if merged is None:
        print(f"registry-merge: {pathname}: structured merge not provably safe — "
              f"leaving conflict for manual/doctor resolution", file=sys.stderr)
        return 1
    with open(ours_path, "w") as f:
        f.write(merged)
    print(f"registry-merge: {pathname}: resolved structurally", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
