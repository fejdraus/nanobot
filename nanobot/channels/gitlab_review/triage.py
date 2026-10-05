"""Decide whether a new note in a merge-request thread is the reviewer's business.

The rules are enforced here, in code, because the comments are published under
a real person's account: a prompt that says "do not answer other people's
threads" is a request the model may ignore, while a filter that never starts
the run cannot be ignored.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

Action = Literal["review", "skip", "notify", "ask"]

_MENTION_RE = re.compile(r"(?<![\w@])@([A-Za-z0-9_][A-Za-z0-9_.\-]*)")


@dataclass(frozen=True)
class TriageDecision:
    """What to do with a thread after its newest activity.

    ``review``: the note answers the reviewer, run the review for this thread.
    ``notify``: the reviewer is mentioned in someone else's thread; tell the
    human, propose nothing.
    ``ask``: the note might be for the reviewer or for someone else; tell the
    human and let them decide.
    ``skip``: not addressed to the reviewer; stay silent.
    """

    action: Action
    reason: str
    note_id: int | None = None
    note_author: str | None = None
    note_body: str = ""


def mentions(body: str) -> set[str]:
    return {match.casefold() for match in _MENTION_RE.findall(body or "")}


def triage_thread(
    notes: Sequence[Mapping[str, Any]],
    *,
    mr_author: str | None,
    is_reviewer: Callable[[str | None], bool],
) -> TriageDecision:
    """Judge the newest note of one discussion thread."""
    human = [note for note in notes if not note.get("system")]
    if not human:
        return TriageDecision("skip", "thread has no human notes")

    newest = human[-1]
    author = _author(newest)
    body = str(newest.get("body") or "")
    note_id = _int(newest.get("id"))

    def decide(action: Action, reason: str) -> TriageDecision:
        return TriageDecision(action, reason, note_id=note_id, note_author=author, note_body=body)

    if is_reviewer(author):
        return decide("skip", "newest note is the reviewer's own")

    mentioned = mentions(body)
    reviewer_mentioned = any(is_reviewer(name) for name in mentioned)
    starter = _author(human[0])

    if not is_reviewer(starter):
        if reviewer_mentioned:
            return decide("notify", "reviewer mentioned in someone else's thread")
        return decide("skip", "thread was not started by the reviewer")

    if reviewer_mentioned:
        return decide("review", "reviewer mentioned in own thread")
    if mentioned:
        return decide("skip", "question addressed to someone else")

    previous = _previous_speaker(human, author)
    if previous is None or is_reviewer(previous):
        return decide("review", "reply to the reviewer's note")

    same = (author or "").casefold()
    if mr_author and same == mr_author.casefold() and previous.casefold() != same:
        return decide(
            "ask", f"merge request author replied after {previous}; unclear whom it addresses"
        )
    return decide("skip", f"reply to {previous}, not to the reviewer")


def _previous_speaker(notes: Sequence[Mapping[str, Any]], author: str | None) -> str | None:
    """Return who spoke before the newest note's author started their run of notes."""
    same = (author or "").casefold()
    for note in reversed(notes[:-1]):
        speaker = _author(note)
        if (speaker or "").casefold() != same:
            return speaker
    return None


def _author(note: Mapping[str, Any]) -> str | None:
    author = note.get("author")
    if isinstance(author, Mapping):
        username = cast("Mapping[str, Any]", author).get("username")
        return username if isinstance(username, str) else None
    return None


def _int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None
