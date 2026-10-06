"""Translate GitLab webhook payloads into review candidates.

Kept free of I/O so the routing rules can be tested against recorded payloads.
A candidate is only "maybe wake the reviewer": deciding whether a note is
addressed to the reviewer needs the thread history, which :mod:`triage` judges
after the debounce window, against live GitLab state.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

MERGE_REQUEST_EVENT = "Merge Request Hook"
NOTE_EVENT = "Note Hook"

_REVIEW_ACTIONS = frozenset({"open", "reopen"})
_DRAFT_TITLE_PREFIXES = ("draft:", "[draft]", "(draft)", "wip:")

CandidateKind = Literal["merge_request", "note"]


@dataclass(frozen=True)
class ReviewCandidate:
    """Webhook activity that may need the reviewer."""

    kind: CandidateKind
    iid: int
    author_id: int | None = None
    discussion_id: str | None = None
    note_id: int | None = None
    requested: bool = False

    @property
    def debounce_key(self) -> str:
        """Group activity that should end in a single review run.

        A reply often arrives as several notes in a row (a screenshot, then the
        text), so notes are grouped per thread and the run starts once the
        thread goes quiet.
        """
        if self.kind == "note" and self.discussion_id:
            return f"{self.iid}:{self.discussion_id}"
        return f"{self.iid}:mr"


def parse_event(
    event_type: str | None,
    payload: dict[str, Any],
    *,
    project_path: str,
    is_reviewer: Callable[[str | None], bool],
) -> ReviewCandidate | None:
    """Return a candidate, or ``None`` when the event can never concern the reviewer."""
    normalized = event_type.strip() if isinstance(event_type, str) else ""
    if _project_path(payload) != project_path:
        return None
    if normalized == MERGE_REQUEST_EVENT:
        return _parse_merge_request(payload)
    if normalized == NOTE_EVENT:
        return _parse_note(payload, is_reviewer=is_reviewer)
    return None


def is_draft(attributes: Mapping[str, Any]) -> bool:
    if attributes.get("draft") or attributes.get("work_in_progress"):
        return True
    title = attributes.get("title")
    return isinstance(title, str) and title.strip().casefold().startswith(_DRAFT_TITLE_PREFIXES)


def _parse_merge_request(payload: dict[str, Any]) -> ReviewCandidate | None:
    attributes = _mapping(payload.get("object_attributes"))
    if attributes is None:
        return None
    if attributes.get("action") not in _REVIEW_ACTIONS:
        return None
    if is_draft(attributes):
        return None
    iid = _positive_int(attributes.get("iid"))
    if iid is None:
        return None
    return ReviewCandidate(
        kind="merge_request",
        iid=iid,
        author_id=_positive_int(attributes.get("author_id")),
    )


def _parse_note(
    payload: dict[str, Any], *, is_reviewer: Callable[[str | None], bool]
) -> ReviewCandidate | None:
    attributes = _mapping(payload.get("object_attributes"))
    merge_request = _mapping(payload.get("merge_request"))
    if attributes is None or merge_request is None:
        return None
    if attributes.get("noteable_type") != "MergeRequest":
        return None
    if merge_request.get("state") != "opened":
        return None
    if attributes.get("system"):
        return None
    author = _mapping(payload.get("user"))
    username = author.get("username") if author else None
    if is_reviewer(username if isinstance(username, str) else None):
        return None
    iid = _positive_int(merge_request.get("iid"))
    discussion_id = attributes.get("discussion_id")
    if iid is None or not isinstance(discussion_id, str) or not discussion_id:
        return None
    return ReviewCandidate(
        kind="note",
        iid=iid,
        author_id=_positive_int(merge_request.get("author_id")),
        discussion_id=discussion_id,
        note_id=_positive_int(attributes.get("id")),
    )


def _project_path(payload: dict[str, Any]) -> str | None:
    project = _mapping(payload.get("project"))
    if project is None:
        return None
    path = project.get("path_with_namespace")
    return path if isinstance(path, str) and path else None


def _mapping(value: object) -> dict[str, Any] | None:
    """Narrow decoded JSON to a string-keyed mapping."""
    if not isinstance(value, dict):
        return None
    mapping = cast("Mapping[Any, Any]", value)
    return {str(key): item for key, item in mapping.items()}


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        return parsed if parsed > 0 else None
    return None
