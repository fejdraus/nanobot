from pathlib import Path

import pytest

from nanobot.channels.gitlab_review.lessons import (
    glob_to_regex,
    load_lessons,
    parse_lesson,
    render_lessons,
    select_lessons,
)


def _note(applies_to: str = "[]", keywords: str = "[]", body: str = "Урок.") -> str:
    return (
        "---\nname: x\ndescription: \"описание\"\n"
        f"applies_to: {applies_to}\nkeywords: {keywords}\nmetadata:\n  type: feedback\n---\n\n{body}\n"
    )


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("Pkg/AMLead/**", "Pkg/AMLead/Schemas/Page/Page.js", True),
        ("Pkg/AMLead/**", "Pkg/AMLeadExt/Schemas/x.js", False),
        ("**/*.cs", "Pkg/A/Files/cs/Svc.cs", True),
        ("**/*.cs", "Svc.cs", True),
        ("Pkg/*/Schemas/*Page*/**", "Pkg/A/Schemas/LeadPageV2/LeadPageV2.js", True),
        ("Pkg/*/Schemas/*Page*/**", "Pkg/A/B/Schemas/LeadPage/x.js", False),
        ("**/Files/src/js/**", "Pkg/A/Files/src/js/app.js", True),
    ],
)
def test_globs(pattern: str, path: str, expected: bool) -> None:
    assert bool(glob_to_regex(pattern).fullmatch(path)) is expected


def test_untagged_and_index_notes_are_skipped(tmp_path: Path) -> None:
    (tmp_path / "MEMORY.md").write_text("- [a](a.md) — x", encoding="utf-8")
    (tmp_path / "topic_freedom.md").write_text("- [a](a.md) — x", encoding="utf-8")
    (tmp_path / "plain.md").write_text("---\nname: p\n---\nbody", encoding="utf-8")
    (tmp_path / "tagged.md").write_text(_note('["**/*.cs"]'), encoding="utf-8")
    assert [lesson.name for lesson in load_lessons(tmp_path)] == ["tagged.md"]


def test_short_keywords_and_bad_lists_are_ignored() -> None:
    lesson = parse_lesson("n.md", _note("not json", '["ESQ", "EntitySchemaQuery"]'))
    assert lesson is not None
    assert lesson.applies_to == ()
    assert lesson.keywords == ("EntitySchemaQuery",)


def test_selection_by_path_and_keyword() -> None:
    by_path = parse_lesson("p.md", _note('["Pkg/AMLead/**"]'))
    by_word = parse_lesson("k.md", _note("[]", '["loadColumnsFromServer"]'))
    other = parse_lesson("o.md", _note('["Pkg/Other/**"]', '["SomethingElse"]'))
    lessons = [lesson for lesson in (by_path, by_word, other) if lesson is not None]
    matched = select_lessons(lessons, ["Pkg/AMLead/Schemas/A.js"], "this.LOADCOLUMNSFROMSERVER()")
    assert {m.lesson.name for m in matched} == {"p.md", "k.md"}


def test_render_respects_budget_and_points_to_the_rest() -> None:
    big = parse_lesson("big.md", _note('["**"]', body="x" * 500))
    small = parse_lesson("small.md", _note('["**"]', body="коротко"))
    assert big is not None and small is not None
    matched = select_lessons([big, small], ["a.cs"], "")
    text = render_lessons(matched, budget_chars=400)
    assert "коротко" in text
    assert "x" * 500 not in text
    assert "big.md — описание" in text
    assert render_lessons([], 1000) == ""


def test_tags_written_as_nested_yaml_lists_are_read() -> None:
    from nanobot.channels.gitlab_review.lessons import parse_lesson

    text = (
        "---\nname: feedback_x\ndescription: \"Пересчёт: сверить формулу\"\nmetadata:\n"
        "  node_type: memory\n  applies_to:\n    - Pkg/BanzaAMFreedom/Schemas/Contacts_FormPage_handlers/**\n"
        "  keywords:\n    - calculateDriverLicPeriod\n    - r.silent\n  type: feedback\n---\n\nТело."
    )
    lesson = parse_lesson("feedback_x.md", text)
    assert lesson is not None
    assert lesson.applies_to == ("Pkg/BanzaAMFreedom/Schemas/Contacts_FormPage_handlers/**",)
    assert lesson.keywords == ("calculateDriverLicPeriod", "r.silent")
    assert lesson.description == "Пересчёт: сверить формулу"


def test_tags_survive_a_front_matter_that_is_not_valid_yaml() -> None:
    from nanobot.channels.gitlab_review.lessons import parse_lesson

    text = '---\nname: x\ndescription: a: b: c\napplies_to: ["**/*.cs"]\nkeywords: []\n---\nТело.'
    lesson = parse_lesson("x.md", text)
    assert lesson is not None and lesson.applies_to == ("**/*.cs",)

