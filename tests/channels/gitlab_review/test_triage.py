from typing import Any

from nanobot.channels.gitlab_review.triage import triage_thread

REVIEWER = "a.tyra"
AUTHOR = "author"


def _is_reviewer(name: str | None) -> bool:
    return (name or "").casefold() == REVIEWER


def _n(username: str, body: str = "text", *, system: bool = False, note_id: int = 1) -> dict[str, Any]:
    return {"id": note_id, "author": {"username": username}, "body": body, "system": system}


def _triage(*notes: dict[str, Any]) -> str:
    return triage_thread(list(notes), mr_author=AUTHOR, is_reviewer=_is_reviewer).action


def test_author_reply_to_reviewer_is_reviewed() -> None:
    assert _triage(_n(REVIEWER, "issue"), _n(AUTHOR, "fixed")) == "review"


def test_series_of_author_notes_still_answers_reviewer() -> None:
    assert _triage(_n(REVIEWER), _n(AUTHOR, "![image]"), _n(AUTHOR, "see screenshot")) == "review"


def test_third_party_answering_reviewer_is_reviewed() -> None:
    assert _triage(_n(REVIEWER), _n("lead", "this is by design")) == "review"


def test_third_party_replying_to_author_is_skipped() -> None:
    assert _triage(_n(REVIEWER), _n(AUTHOR, "fixed"), _n("lead", "nice")) == "skip"


def test_question_to_someone_else_is_skipped() -> None:
    assert _triage(_n(REVIEWER), _n(AUTHOR, "@lead what do you think?")) == "skip"


def test_reviewer_mention_in_own_thread_is_reviewed() -> None:
    assert _triage(_n(REVIEWER), _n(AUTHOR), _n("lead", "@a.tyra agree?")) == "review"


def test_foreign_thread_is_skipped() -> None:
    assert _triage(_n("lead", "nit"), _n(AUTHOR, "done")) == "skip"


def test_reviewer_mentioned_in_foreign_thread_only_notifies() -> None:
    assert _triage(_n("lead", "nit"), _n(AUTHOR, "@A.Tyra please check")) == "notify"


def test_author_after_third_party_is_ambiguous() -> None:
    assert _triage(_n(REVIEWER), _n("lead", "hm"), _n(AUTHOR, "ok, changed")) == "ask"


def test_system_notes_are_ignored() -> None:
    assert _triage(_n(REVIEWER), _n("bot", "changed the description", system=True), _n(AUTHOR)) == "review"


def test_reviewer_newest_note_is_skipped() -> None:
    assert _triage(_n(REVIEWER), _n(AUTHOR), _n(REVIEWER, "thanks")) == "skip"


def test_empty_thread_is_skipped() -> None:
    assert _triage() == "skip"


def test_email_like_text_is_not_a_mention() -> None:
    assert _triage(_n(REVIEWER), _n(AUTHOR, "write to dev@example.com")) == "review"
