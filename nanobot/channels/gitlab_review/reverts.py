"""Recognise a merge request that only reverts earlier work.

Git records no command, so «this is a revert» rests on two checks. Every
commit of the MR must carry the trailer git or GitLab writes on a revert
(«This reverts commit <sha>» or «This reverts merge request !N»), which names
what is reverted. Then the MR's changes must be exactly the inverse of those:
per file, every line the original added is removed and every line it removed
is added, nothing more. A trailer typed by hand, a conflict resolved by hand or
anything else added on top fails the second check, and the MR is reviewed.
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


def net_lines(changes: Iterable[dict[str, Any]]) -> dict[FileKey, Counter[str]] | None:
    """Lines added (+1) and removed (−1) per file, or ``None`` when a diff was cut short."""
    result: dict[FileKey, Counter[str]] = {}
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
    return {key: Counter({line: n for line, n in counter.items() if n}) for key, counter in result.items()}


def is_exact_revert(current: list[dict[str, Any]], originals: list[list[dict[str, Any]]]) -> bool:
    """Whether *current* undoes the sum of *originals* line for line and changes nothing else."""
    mine = net_lines(current)
    if mine is None:
        return False
    total: dict[FileKey, Counter[str]] = {}
    for original in originals:
        lines = net_lines(original)
        if lines is None:
            return False
        for key, counter in lines.items():
            total.setdefault(key, Counter()).update(counter)
    expected = {
        key: Counter({line: -n for line, n in counter.items() if n})
        for key, counter in total.items()
    }
    mine = {key: counter for key, counter in mine.items() if counter}
    expected = {key: counter for key, counter in expected.items() if counter}
    return bool(expected) and mine == expected
