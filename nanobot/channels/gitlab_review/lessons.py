"""Pick the reviewer's lessons that concern the files a merge request changes.

A lesson is a memory note whose front matter may carry two tags, as YAML lists
(inline ``["…"]`` or one ``- item`` per line, at the top level or nested under
``metadata:``, as Claude Code writes them):

- ``applies_to``: path globs relative to the repository root (``**`` crosses
  directories, ``*`` does not), matched against the changed file paths;
- ``keywords``: terms matched case-insensitively against the changed lines.

The selection is made here, in code, rather than left to the model: a lesson
about the code under review must reach the prompt even when the model would not
think of opening it.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import yaml
from loguru import logger

INDEX_FILES = frozenset({"MEMORY.md"})
INDEX_PREFIX = "topic_"
_FIELD_RE = re.compile(r"^(applies_to|keywords|description):\s*(\S.*)$", re.MULTILINE)
_TAG_KEYS = frozenset({"applies_to", "keywords", "description"})


@dataclass(frozen=True)
class Lesson:
    name: str
    description: str
    applies_to: tuple[str, ...]
    keywords: tuple[str, ...]
    body: str


@dataclass(frozen=True)
class MatchedLesson:
    lesson: Lesson
    reasons: tuple[str, ...]


def load_lessons(directory: Path, *, tagged_only: bool = True) -> list[Lesson]:
    """Read the notes in *directory*: by default only tagged ones, else every note with a description."""
    lessons: list[Lesson] = []
    for path in sorted(directory.glob("*.md")):
        if path.name in INDEX_FILES or path.name.startswith(INDEX_PREFIX):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("lessons: cannot read {}: {}", path.name, exc)
            continue
        lesson = parse_lesson(path.name, text)
        if lesson is None:
            continue
        if lesson.applies_to or lesson.keywords or (not tagged_only and lesson.description):
            lessons.append(lesson)
    return lessons


def parse_lesson(name: str, text: str) -> Lesson | None:
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end < 0:
        return None
    head, body = text[3:end], text[end + 4:].strip()
    fields = _front_matter(head)
    description = fields.get("description")
    return Lesson(
        name=name,
        description=description.strip() if isinstance(description, str) else "",
        applies_to=_string_list(fields.get("applies_to")),
        keywords=tuple(k for k in _string_list(fields.get("keywords")) if len(k) >= 4),
        body=body,
    )


def _front_matter(head: str) -> dict[str, object]:
    """The tag fields of a front matter, wherever in it they are nested.

    A front matter that is not valid YAML still yields the fields written on
    one line each, so a stray colon in a description does not lose the tags.
    """
    try:
        data: object = yaml.safe_load(head)
    except yaml.YAMLError:
        data = None
    found: dict[str, object] = {}
    stack: list[object] = [data]
    while stack:
        current = stack.pop()
        if not isinstance(current, dict):
            continue
        for key, value in cast("dict[object, object]", current).items():
            if key in _TAG_KEYS and key not in found:
                found[str(key)] = value
            elif isinstance(value, dict):
                stack.append(cast("object", value))
    for match in _FIELD_RE.finditer(head):
        found.setdefault(match.group(1), match.group(2).strip())
    return found


def select_lessons(
    lessons: Iterable[Lesson], changed_paths: Iterable[str], diff_text: str
) -> list[MatchedLesson]:
    """Lessons whose globs match a changed path or whose keywords occur in the diff."""
    paths = [p.lstrip("/") for p in changed_paths if p]
    haystack = diff_text.casefold()
    matched: list[MatchedLesson] = []
    for lesson in lessons:
        reasons: list[str] = []
        for pattern in lesson.applies_to:
            regex = glob_to_regex(pattern)
            hit = next((p for p in paths if regex.fullmatch(p)), None)
            if hit is not None:
                reasons.append(f"{pattern} ← {hit}")
        reasons.extend(f"«{k}» в диффе" for k in lesson.keywords if k.casefold() in haystack)
        if reasons:
            matched.append(MatchedLesson(lesson, tuple(reasons)))
    matched.sort(key=lambda m: -len(m.reasons))
    return matched


def render_lessons(matched: list[MatchedLesson], budget_chars: int) -> str:
    """Prompt block: full text of the best matches within the budget, pointers to the rest."""
    if not matched:
        return ""
    parts = [
        "Lessons from the review memory that concern the changed code (picked by the MR's files "
        "and diff). Apply them in the review; the full notes are in memory under the same names."
    ]
    used = len(parts[0])
    pointers: list[str] = []
    for item in matched:
        block = (
            f"\n### {item.lesson.name}\nWhy: {'; '.join(item.reasons[:3])}\n"
            f"{item.lesson.body}"
        )
        if used + len(block) <= budget_chars:
            parts.append(block)
            used += len(block)
        else:
            pointers.append(f"- {item.lesson.name} — {item.lesson.description}")
    if pointers:
        parts.append("\nAlso relevant (read the ones you need):\n" + "\n".join(pointers))
    return "\n".join(parts)


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """``**`` spans directories, ``*`` and ``?`` stay within one path segment."""
    out: list[str] = []
    i = 0
    text = pattern.lstrip("/")
    while i < len(text):
        if text.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif text.startswith("**", i):
            out.append(".*")
            i += 2
        elif text[i] == "*":
            out.append("[^/]*")
            i += 1
        elif text[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(text[i]))
            i += 1
    return re.compile("".join(out), re.IGNORECASE)


def _string_list(raw: object) -> tuple[str, ...]:
    value: object = raw
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return ()
    if not isinstance(value, list):
        return ()
    items = cast("list[object]", value)
    return tuple(str(item).strip() for item in items if isinstance(item, str) and item.strip())
