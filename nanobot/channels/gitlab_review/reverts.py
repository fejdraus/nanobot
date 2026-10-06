"""Recognise a merge request that only reverts earlier work.

Git records no command, so «this is a revert» rests on two checks. Every
commit of the MR must carry the trailer git or GitLab writes on a revert
(«This reverts commit <sha>» or «This reverts merge request !N»), which names
what is reverted. Then the MR's changes must be exactly the inverse of those:
per file, every line the original added is removed and every line it removed
is added, nothing more. A trailer typed by hand, a conflict resolved by hand or
anything else added on top fails the second check, and the MR is reviewed.

Merge request diffs are read raw: the JSON diff API collapses large files and
would leave them uncomparable. A binary change cannot be compared line by line,
so it makes the check fail too.
"""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from typing import Any

_REVERTED_RE = re.compile(
    r"^This reverts (?:merge request !(?P<mr>[0-9]+)|commit (?P<sha>[0-9a-f]{40}))", re.MULTILINE
)

FileKey = frozenset[str]
Net = dict[FileKey, Counter[str]]


def reverted_refs(messages: Iterable[str]) -> list[tuple[str, str]] | None:
    """``("mr", "5937")`` / ``("commit", sha)`` per reverted item, or ``None`` if a commit is not a revert."""
    refs: list[tuple[str, str]] = []
    count = 0
    for message in messages:
        count += 1
        match = _REVERTED_RE.search(message or "")
        if match is None:
            return None
        ref = ("mr", match.group("mr")) if match.group("mr") else ("commit", match.group("sha"))
        if ref not in refs:
            refs.append(ref)
    return refs if count else None


def label(ref: tuple[str, str]) -> str:
    kind, value = ref
    return f"!{value}" if kind == "mr" else value[:8]


def net_from_raw(diff: str) -> Net | None:
    """Lines added (+1) and removed (−1) per file of a raw unified diff; ``None`` if binary."""
    result: Net = {}
    counter: Counter[str] | None = None
    paths: set[str] = set()
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            counter, paths, in_hunk = None, set[str](), False
            continue
        if line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            return None
        if line.startswith("@@"):
            if counter is None:
                counter = result.setdefault(frozenset(paths), Counter())
            in_hunk = True
            continue
        if not in_hunk:
            for prefix in ("--- a/", "+++ b/", "rename from ", "rename to "):
                if line.startswith(prefix):
                    paths.add(line[len(prefix):])
            continue
        assert counter is not None
        if line.startswith("+"):
            counter[line[1:]] += 1
        elif line.startswith("-"):
            counter[line[1:]] -= 1
    return _clean(result)


def net_from_changes(changes: Iterable[dict[str, Any]]) -> Net | None:
    """The same for GitLab's JSON diffs; ``None`` when a diff was collapsed or cut short."""
    result: Net = {}
    for change in changes:
        if change.get("too_large") or change.get("collapsed"):
            return None
        key: FileKey = frozenset(
            str(path) for path in (change.get("old_path"), change.get("new_path")) if path
        )
        counter = result.setdefault(key, Counter())
        for line in str(change.get("diff") or "").splitlines():
            if line.startswith(("+++", "---")):
                continue
            if line.startswith("+"):
                counter[line[1:]] += 1
            elif line.startswith("-"):
                counter[line[1:]] -= 1
    return _clean(result)


def is_exact_revert(current: Net | None, originals: list[Net | None]) -> bool:
    """Whether *current* undoes the sum of *originals* line for line and changes nothing else."""
    if current is None or any(original is None for original in originals):
        return False
    total: Net = {}
    for original in originals:
        assert original is not None
        for key, counter in original.items():
            total.setdefault(key, Counter()).update(counter)
    expected = _clean({key: Counter({line: -n for line, n in counter.items()}) for key, counter in total.items()})
    return bool(expected) and current == expected


def _clean(net: Net) -> Net:
    cleaned: Net = {}
    for key, counter in net.items():
        kept = Counter({line: n for line, n in counter.items() if n})
        if kept:
            cleaned[key] = kept
    return cleaned
