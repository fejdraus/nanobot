"""What the reviewer learns about the developers it talks to.

It works like nanobot's Dream, in two steps:

1. Evidence. After a review, a thread reply or a conversation the agent may end
   its answer with a block of observations about the people involved. They are
   kept as dated evidence tied to the MR, never shown as a profile.
2. Consolidation. Once a day the agent reads, per developer, the current
   profile, the new evidence and the developer's own recent messages in review
   threads, and rewrites the profile: how they communicate, habits seen in more
   than one MR, what they know well. Ordinary work and one-off episodes stay out.

The profile of the MR author (and of whoever answered in a thread) goes into
the next prompt that concerns them. The guards live here, not in the prompt:
only the people of the work at hand can be written about, developers who did
not agree are never profiled, judgements of character are refused, and a
consolidated profile must keep its sections and its size.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from loguru import logger

PEOPLE_FENCE = "gitlab-review-people"
PROFILE_FENCE = "gitlab-review-profile"
PROFILE_SECTIONS = ("## Communication", "## Code habits", "## Strengths and areas")
MAX_NOTES_PER_PERSON = 5
MAX_NOTE_CHARS = 300

_FENCE_RE = re.compile(
    r"```" + re.escape(PEOPLE_FENCE) + r"[ \t]*\r?\n(?P<body>.*?)\r?\n```",
    re.DOTALL,
)
_PROFILE_RE = re.compile(
    r"```" + re.escape(PROFILE_FENCE) + r"[ \t]*\r?\n(?P<body>.*?)\r?\n```",
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
            refused.append(f"{note.username}: not a participant of this work")
            continue
        if _LABEL_RE.search(note.text):
            refused.append(f"{note.username}: a judgement of character, not an observation")
            continue
        if per_person.get(key, 0) >= MAX_NOTES_PER_PERSON:
            refused.append(f"{note.username}: more than {MAX_NOTES_PER_PERSON} notes at once")
            continue
        per_person[key] = per_person.get(key, 0) + 1
        kept.append(note)
    return kept, refused


def parse_profile(answer: str, max_chars: int) -> tuple[str | None, str]:
    """The consolidated profile in *answer*, or ``None`` with the reason it was refused."""
    matches = list(_PROFILE_RE.finditer(answer or ""))
    if not matches:
        return None, "no profile block"
    body = matches[-1].group("body").strip()
    lines = [line.rstrip() for line in body.splitlines() if line.strip() and line.strip() != "- ..."]
    headings = [line for line in lines if line.startswith("## ")]
    if tuple(headings) != PROFILE_SECTIONS:
        return None, f"sections must be exactly {', '.join(PROFILE_SECTIONS)}"
    stray = [line for line in lines if not line.startswith(("## ", "- "))]
    if stray:
        return None, f"not a heading or a bullet: {stray[0][:80]}"
    if _LABEL_RE.search(body):
        return None, "a judgement of character"
    text = "\n".join(lines)
    if len(text) > max_chars:
        return None, f"{len(text)} characters, more than {max_chars}"
    return text, ""


class PeopleStore:
    """One Markdown profile per GitLab username in *directory*."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def path_for(self, username: str) -> Path | None:
        if not _USERNAME_RE.match(username or ""):
            return None
        return self.directory / f"{username.casefold()}.md"

    def read(self, username: str) -> str:
        """The profile body without its front matter, or ``""``."""
        path = self.path_for(username)
        if path is None or not path.exists():
            return ""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("people: cannot read {}: {}", path.name, exc)
            return ""
        return _body(text)

    def write(self, username: str, profile: str) -> bool:
        """Replace the profile of *username*; ``False`` for a name that cannot be a file."""
        path = self.path_for(username)
        if path is None:
            return False
        self.directory.mkdir(parents=True, exist_ok=True)
        path.write_text(_front_matter(username) + profile.strip() + "\n", encoding="utf-8", newline="\n")
        return True

    def render(self, usernames: Iterable[str], budget: int, stats: dict[str, str] | None = None) -> str:
        """The profiles of *usernames* for a prompt, each within its share of *budget*."""
        sections: list[str] = []
        names = list(dict.fromkeys(name for name in usernames if name))
        if not names or budget <= 0:
            return ""
        share = budget // len(names)
        for username in names:
            body = self.read(username)
            line = (stats or {}).get(username, "")
            if not body and not line:
                continue
            text = f"### {username}\n"
            if line:
                text += f"{line}\n"
            if body:
                text += _head(body, max(share - len(text), 0))
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


def _front_matter(username: str) -> str:
    return (
        f"---\nname: dev_{username.casefold()}\n"
        f'description: "Developer {username}: how to discuss reviews with them, what is typical of their code"\n'
        f"---\n\n# {username}\n\n"
    )


def _body(text: str) -> str:
    """Drop the front matter and the title line of a profile file."""
    lines = text.splitlines()
    if lines and lines[0] == "---":
        end = next((index for index, line in enumerate(lines[1:], 1) if line == "---"), 0)
        lines = lines[end + 1 :]
    return "\n".join(line for line in lines if not line.startswith("# ")).strip()


def _head(text: str, budget: int) -> str:
    """The first lines of *text* that fit *budget*: sections come in a fixed order."""
    kept: list[str] = []
    used = 0
    for line in text.splitlines():
        if used + len(line) + 1 > budget:
            break
        kept.append(line)
        used += len(line) + 1
    return "\n".join(kept)
