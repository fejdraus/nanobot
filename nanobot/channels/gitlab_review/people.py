"""What the reviewer learns about the developers it talks to.

After a review, a thread reply or a conversation the agent may end its answer
with a block of observations about the people involved: recurring mistakes and
habits in their code, the modules they know, how they take a remark and what
helps them understand one. The channel files them, one Markdown profile per
GitLab username, and puts the profile of the MR author (and of whoever answered
in a thread) into the next prompt that concerns them.

The guards live here, not in the prompt: only the people of the work at hand
can be written about, every observation is dated and tied to its MR, and
judgements of character are refused outright. A profile adapts how the
reviewer talks, never how strictly it reviews.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import cast

from loguru import logger

PEOPLE_FENCE = "gitlab-review-people"
MAX_NOTES_PER_PERSON = 5
MAX_NOTE_CHARS = 300

_FENCE_RE = re.compile(
    r"```" + re.escape(PEOPLE_FENCE) + r"[ \t]*\r?\n(?P<body>.*?)\r?\n```",
    re.DOTALL,
)
_USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MENTION_RE = re.compile(r"@([A-Za-z0-9][A-Za-z0-9._-]{0,63})")
_LABEL_RE = re.compile(
    r"ленив|небрежн|упрям|тупой|тупица|некомпетент|халтур|безответствен|неадекват|"
    r"токсичн|хамит|хамств|высокомер|бездар|криворук|"
    r"\blazy\b|sloppy|stubborn|stupid|incompetent|careless|toxic|arrogant|\brude\b|dumb",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PersonNote:
    username: str
    text: str


def extract_people(answer: str) -> tuple[list[PersonNote], str]:
    """Take the people block out of *answer*; return its notes and the rest of the text."""
    notes: list[PersonNote] = []
    rest = answer or ""
    matches = list(_FENCE_RE.finditer(rest))
    for match in reversed(matches):
        notes[:0] = _parse_block(match.group("body"))
        rest = rest[: match.start()] + rest[match.end():]
    return notes, rest.strip() if matches else rest


def mentioned_usernames(text: str) -> list[str]:
    return list(dict.fromkeys(_MENTION_RE.findall(text or "")))


def accept_notes(
    notes: Iterable[PersonNote], allowed: Iterable[str]
) -> tuple[list[PersonNote], list[str]]:
    """Keep the notes that may be filed; return them with the reasons others were refused."""
    permitted = {name.casefold() for name in allowed if name}
    kept: list[PersonNote] = []
    refused: list[str] = []
    per_person: dict[str, int] = {}
    for note in notes:
        key = note.username.casefold()
        if key not in permitted:
            refused.append(f"{note.username}: не участник этой работы")
            continue
        if _LABEL_RE.search(note.text):
            refused.append(f"{note.username}: оценка характера, а не наблюдение")
            continue
        if per_person.get(key, 0) >= MAX_NOTES_PER_PERSON:
            refused.append(f"{note.username}: больше {MAX_NOTES_PER_PERSON} наблюдений за раз")
            continue
        per_person[key] = per_person.get(key, 0) + 1
        kept.append(note)
    return kept, refused


class PeopleStore:
    """One Markdown profile per GitLab username in *directory*."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def path_for(self, username: str) -> Path | None:
        if not _USERNAME_RE.match(username or ""):
            return None
        return self.directory / f"{username.casefold()}.md"

    def append(self, notes: Iterable[PersonNote], *, iid: int | None, today: date | None = None) -> int:
        """File *notes*, each dated and tied to the MR; return how many were written."""
        day = (today or date.today()).isoformat()
        source = f" !{iid}" if iid is not None else ""
        grouped: dict[str, list[str]] = {}
        for note in notes:
            grouped.setdefault(note.username, []).append(note.text)
        written = 0
        for username, texts in grouped.items():
            path = self.path_for(username)
            if path is None:
                continue
            self.directory.mkdir(parents=True, exist_ok=True)
            if path.exists():
                current = path.read_text(encoding="utf-8").rstrip("\n")
            else:
                current = _new_profile(username)
            lines = [f"- {day}{source}: {text}" for text in texts]
            path.write_text(current + "\n" + "\n".join(lines) + "\n", encoding="utf-8", newline="\n")
            written += len(lines)
        return written

    def render(self, usernames: Iterable[str], budget: int, stats: dict[str, str] | None = None) -> str:
        """The profiles of *usernames* for a prompt, newest observations kept within *budget*."""
        sections: list[str] = []
        names = list(dict.fromkeys(name for name in usernames if name))
        if not names or budget <= 0:
            return ""
        share = budget // len(names)
        for username in names:
            path = self.path_for(username)
            body = ""
            if path is not None and path.exists():
                try:
                    body = _observations(path.read_text(encoding="utf-8"))
                except OSError as exc:
                    logger.warning("people: cannot read {}: {}", path.name, exc)
            line = (stats or {}).get(username, "")
            if not body and not line:
                continue
            text = f"### {username}\n"
            if line:
                text += f"{line}\n"
            if body:
                text += _tail(body, max(share - len(text), 0))
            sections.append(text.rstrip())
        if not sections:
            return ""
        return (
            "Profiles of the people involved — how best to discuss with them. Adapt the delivery, "
            "but review everyone equally strictly.\n\n" + "\n\n".join(sections)
        )


def _parse_block(body: str) -> list[PersonNote]:
    try:
        data: object = json.loads(body)
    except json.JSONDecodeError:
        return []
    people = cast("dict[str, object]", data).get("people") if isinstance(data, dict) else None
    if not isinstance(people, list):
        return []
    notes: list[PersonNote] = []
    for raw in cast("list[object]", people):
        if not isinstance(raw, dict):
            continue
        entry = cast("dict[str, object]", raw)
        username = entry.get("username")
        texts = entry.get("notes")
        if not isinstance(username, str) or not isinstance(texts, list):
            continue
        for text in cast("list[object]", texts):
            if isinstance(text, str) and text.strip():
                clean = " ".join(text.split())[:MAX_NOTE_CHARS]
                notes.append(PersonNote(username.strip().lstrip("@"), clean))
    return notes


def _new_profile(username: str) -> str:
    return (
        f"---\nname: dev_{username.casefold()}\n"
        f'description: "Разработчик {username}: как с ним обсуждать ревью, что типично в его коде"\n'
        f"---\n\n# {username}\n"
    )


def _observations(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if line.startswith("- "))


def _tail(lines: str, budget: int) -> str:
    """The newest lines of *lines* that fit *budget*: observations are appended in order."""
    kept: list[str] = []
    used = 0
    for line in reversed(lines.splitlines()):
        if used + len(line) + 1 > budget:
            break
        kept.append(line)
        used += len(line) + 1
    return "\n".join(reversed(kept))
