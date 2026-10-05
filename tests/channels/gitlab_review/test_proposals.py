import json

import pytest

from nanobot.channels.gitlab_review.proposals import (
    ACTIONS_FENCE,
    TELEGRAM_LIMIT,
    ApprovalCommand,
    ProposedAction,
    ReviewDraft,
    parse_command,
    parse_draft,
    render_draft,
)


def _answer(actions: object, summary: str = "Сводка") -> str:
    return f"{summary}\n\n```{ACTIONS_FENCE}\n{json.dumps(actions, ensure_ascii=False)}\n```\n"


def test_parse_draft_reads_all_action_types() -> None:
    draft = parse_draft(
        _answer({
            "actions": [
                {"type": "discussion", "path": "a.cs", "line": 10, "body": "fix"},
                {"type": "reply", "discussion_id": "d1", "body": "ok"},
                {"type": "note", "body": "general"},
                {"type": "approve"},
            ]
        })
    )

    assert draft.summary == "Сводка"
    assert draft.errors == ()
    assert [action.type for action in draft.actions] == ["discussion", "reply", "note", "approve"]
    assert draft.actions[0] == ProposedAction("discussion", "fix", path="a.cs", line=10)


def test_invalid_actions_are_reported_not_published() -> None:
    draft = parse_draft(
        _answer({
            "actions": [
                {"type": "discussion", "body": "no path"},
                {"type": "merge"},
                {"type": "reply", "body": "no thread"},
                {"type": "note", "body": "  "},
            ]
        })
    )

    assert draft.actions == ()
    assert len(draft.errors) == 4


def test_answer_without_block_has_no_actions() -> None:
    draft = parse_draft("Просто текст")

    assert draft == ReviewDraft(summary="Просто текст", actions=())


def test_broken_json_is_reported() -> None:
    draft = parse_draft(f"S\n```{ACTIONS_FENCE}\n{{not json\n```")

    assert draft.actions == ()
    assert draft.errors


def test_last_block_wins() -> None:
    answer = _answer({"actions": [{"type": "note", "body": "old"}]}) + _answer(
        {"actions": [{"type": "note", "body": "new"}]}, summary=""
    )

    assert parse_draft(answer).actions == (ProposedAction("note", "new"),)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("публикуй !6260", ApprovalCommand(True, 6260, ())),
        ("публикуй !6260/2 1,3", ApprovalCommand(True, 6260, (1, 3), version=2)),
        ("публикуй !6260/3", ApprovalCommand(True, 6260, (), version=3)),
        ("Публикуй !6260 1,3", ApprovalCommand(True, 6260, (1, 3))),
        ("опубликуй 6260 2 4", ApprovalCommand(True, 6260, (2, 4))),
        ("/publish 6260", ApprovalCommand(True, 6260, ())),
        ("отмена !6260", ApprovalCommand(False, 6260, ())),
        ("/cancel 6260", ApprovalCommand(False, 6260, ())),
    ],
)
def test_parse_command(text: str, expected: ApprovalCommand) -> None:
    assert parse_command(text) == expected


@pytest.mark.parametrize("text", ["да", "публикуй", "публикуй все", "publish !abc", ""])
def test_unrecognised_text_is_not_a_command(text: str) -> None:
    assert parse_command(text) is None


def test_render_draft_numbers_actions_and_explains_commands() -> None:
    draft = ReviewDraft(summary="S", actions=(ProposedAction("note", "n"), ProposedAction("approve")))

    text = "\n".join(render_draft(6260, "Title", draft, version=2))

    assert "1. [общий комментарий]" in text
    assert "2. [аппрув MR]" in text
    assert "Черновик 6260/2" in text
    assert "публикуй !6260/2" in text
    assert "«публикуй 1,3»" in text


def test_render_draft_splits_long_messages() -> None:
    draft = ReviewDraft(summary="x" * (TELEGRAM_LIMIT * 2), actions=())

    chunks = render_draft(1, "T", draft)

    assert len(chunks) >= 2
    assert all(len(chunk) <= TELEGRAM_LIMIT for chunk in chunks)
