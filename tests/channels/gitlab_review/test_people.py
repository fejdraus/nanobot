from datetime import date
from pathlib import Path

from nanobot.channels.gitlab_review.people import (
    PEOPLE_FENCE,
    PeopleStore,
    PersonNote,
    accept_notes,
    extract_people,
    mentioned_usernames,
)


def _block(body: str) -> str:
    return f"```{PEOPLE_FENCE}\n{body}\n```"


def test_people_block_is_taken_out_of_the_answer() -> None:
    answer = 'Сводка\n\n' + _block('{"people": [{"username": "@ivan", "notes": ["  забывает   descriptor.json "]}]}')
    notes, rest = extract_people(answer)
    assert notes == [PersonNote("ivan", "забывает descriptor.json")]
    assert rest == "Сводка"


def test_broken_block_yields_no_notes_and_is_still_removed() -> None:
    notes, rest = extract_people("Текст\n" + _block("not json"))
    assert notes == [] and rest == "Текст"


def test_only_participants_and_no_character_labels_are_accepted() -> None:
    notes = [
        PersonNote("Ivan", "оспаривает замечания ссылкой на документацию"),
        PersonNote("petr", "пишет короткие ответы"),
        PersonNote("ivan", "ленивый и небрежный"),
        PersonNote("ivan", "грубая ошибка в EntitySchemaQuery повторяется"),
    ]
    kept, refused = accept_notes(notes, ["ivan"])
    assert [note.text for note in kept] == [
        "оспаривает замечания ссылкой на документацию",
        "грубая ошибка в EntitySchemaQuery повторяется",
    ]
    assert any("не участник" in reason for reason in refused)
    assert any("оценка характера" in reason for reason in refused)


def test_notes_per_person_are_capped() -> None:
    kept, refused = accept_notes([PersonNote("ivan", f"факт {n}") for n in range(7)], ["ivan"])
    assert len(kept) == 5 and len(refused) == 2


def test_profile_is_dated_tied_to_the_mr_and_rendered_newest_first_within_budget(tmp_path: Path) -> None:
    store = PeopleStore(tmp_path / "people")
    store.append([PersonNote("Ivan", "старое наблюдение")], iid=1, today=date(2026, 1, 1))
    store.append([PersonNote("ivan", "новое наблюдение")], iid=6318, today=date(2026, 10, 6))
    text = (tmp_path / "people" / "ivan.md").read_text(encoding="utf-8")
    assert text.startswith("---\nname: dev_ivan\n")
    assert "- 2026-10-06 !6318: новое наблюдение" in text

    rendered = store.render(["ivan"], 10_000, {"ivan": "MRs of theirs reviewed: 2"})
    assert "### ivan\nMRs of theirs reviewed: 2" in rendered
    assert "старое наблюдение" in rendered and "новое наблюдение" in rendered
    tight = store.render(["ivan"], 80)
    assert "новое наблюдение" in tight and "старое наблюдение" not in tight


def test_unsafe_username_is_never_a_file(tmp_path: Path) -> None:
    store = PeopleStore(tmp_path)
    assert store.path_for("../etc/passwd") is None
    assert store.append([PersonNote("../x", "y")], iid=1) == 0


def test_unknown_people_render_nothing(tmp_path: Path) -> None:
    assert PeopleStore(tmp_path).render(["nobody"], 1000) == ""


def test_mentions_are_usernames() -> None:
    assert mentioned_usernames("запомни про @i.petrov и @i.petrov, а также @anna_k") == ["i.petrov", "anna_k"]
