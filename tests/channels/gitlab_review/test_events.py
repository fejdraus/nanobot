from typing import Any

import pytest

from nanobot.channels.gitlab_review.events import (
    MERGE_REQUEST_EVENT,
    NOTE_EVENT,
    ReviewCandidate,
    parse_event,
)

PROJECT = "astana-group/astana-motors"


def _is_reviewer(name: str | None) -> bool:
    return (name or "").casefold() == "a.tyra"


def _parse(event: str, payload: dict[str, Any]) -> ReviewCandidate | None:
    return parse_event(event, payload, project_path=PROJECT, is_reviewer=_is_reviewer)


def _mr(action: str = "open", **attributes: Any) -> dict[str, Any]:
    return {
        "project": {"path_with_namespace": PROJECT},
        "object_attributes": {"iid": 42, "action": action, "author_id": 7, **attributes},
    }


def _note(username: str = "developer", **attributes: Any) -> dict[str, Any]:
    return {
        "project": {"path_with_namespace": PROJECT},
        "object_attributes": {
            "id": 900,
            "noteable_type": "MergeRequest",
            "discussion_id": "abc123",
            "system": False,
            **attributes,
        },
        "merge_request": {"iid": 42, "state": "opened", "author_id": 7},
        "user": {"username": username},
    }


@pytest.mark.parametrize("action", ["open", "reopen"])
def test_opened_merge_request_is_a_candidate(action: str) -> None:
    candidate = _parse(MERGE_REQUEST_EVENT, _mr(action))

    assert candidate == ReviewCandidate(kind="merge_request", iid=42, author_id=7)
    assert candidate.debounce_key == "42:mr"


@pytest.mark.parametrize("action", ["update", "close", "merge", "approved"])
def test_other_merge_request_actions_are_ignored(action: str) -> None:
    assert _parse(MERGE_REQUEST_EVENT, _mr(action)) is None


@pytest.mark.parametrize(
    "attributes",
    [{"draft": True}, {"work_in_progress": True}, {"title": "Draft: wip"}, {"title": "WIP: x"}],
)
def test_draft_merge_requests_are_ignored(attributes: dict[str, Any]) -> None:
    assert _parse(MERGE_REQUEST_EVENT, _mr(**attributes)) is None


def test_other_project_is_ignored() -> None:
    payload = _mr()
    payload["project"] = {"path_with_namespace": "other/repo"}

    assert _parse(MERGE_REQUEST_EVENT, payload) is None


def test_note_is_a_candidate_per_thread() -> None:
    candidate = _parse(NOTE_EVENT, _note())

    assert candidate is not None
    assert candidate.kind == "note"
    assert candidate.discussion_id == "abc123"
    assert candidate.note_id == 900
    assert candidate.debounce_key == "42:abc123"


def test_reviewer_own_note_is_ignored() -> None:
    assert _parse(NOTE_EVENT, _note(username="A.Tyra")) is None


def test_system_note_is_ignored() -> None:
    assert _parse(NOTE_EVENT, _note(system=True)) is None


def test_note_on_other_noteable_is_ignored() -> None:
    assert _parse(NOTE_EVENT, _note(noteable_type="Issue")) is None


def test_note_on_closed_merge_request_is_ignored() -> None:
    payload = _note()
    payload["merge_request"]["state"] = "merged"

    assert _parse(NOTE_EVENT, payload) is None


def test_unknown_event_is_ignored() -> None:
    assert _parse("Push Hook", _mr()) is None
