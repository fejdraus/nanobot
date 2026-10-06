from pathlib import Path

from nanobot.channels.gitlab_review.people import (
    PEOPLE_FENCE,
    PeopleStore,
    PersonNote,
    accept_notes,
    extract_people,
    mentioned_usernames,
    parse_profile,
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
    assert any("not a participant" in reason for reason in refused)
    assert any("judgement of character" in reason for reason in refused)


def test_notes_per_person_are_capped() -> None:
    kept, refused = accept_notes([PersonNote("ivan", f"факт {n}") for n in range(7)], ["ivan"])
    assert len(kept) == 5 and len(refused) == 2


def _profile_block(*lines: str) -> str:
    return "```gitlab-review-profile\n" + "\n".join(lines) + "\n```"


def test_profile_must_keep_its_sections_bullets_and_size() -> None:
    good = _profile_block("## Communication", "- Пишет по-украински (!1)", "- ...", "## Code habits", "## Strengths and areas")
    profile, reason = parse_profile("Вот:\n" + good, 3000)
    assert profile == "## Communication\n- Пишет по-украински (!1)\n## Code habits\n## Strengths and areas"
    assert reason == ""
    assert parse_profile("нет блока", 3000)[0] is None
    assert parse_profile(_profile_block("## Communication", "- x"), 3000)[0] is None
    assert parse_profile(_profile_block("## Communication", "просто текст", "## Code habits", "## Strengths and areas"), 3000)[0] is None
    assert parse_profile(_profile_block("## Communication", "- небрежный (!1)", "## Code habits", "## Strengths and areas"), 3000)[0] is None
    assert parse_profile(good, 60)[1].endswith("more than 60")
    wordy = _profile_block("## Communication", "- " + "очень " * 30 + "(!1)", "## Code habits", "## Strengths and areas")
    assert "longer than 150" in parse_profile(wordy, 3000)[1]


def test_profile_is_stored_without_its_front_matter_in_prompts(tmp_path: Path) -> None:
    store = PeopleStore(tmp_path / "people")
    store.write("Ivan", "## Communication\n- коротко (!1)\n## Code habits\n- забывает descriptor.json (!1, !2)\n## Strengths and areas")
    text = (tmp_path / "people" / "ivan.md").read_text(encoding="utf-8")
    assert text.startswith("---\nname: dev_ivan\n")
    assert store.read("ivan").startswith("## Communication\n- коротко (!1)")
    rendered = store.render(["ivan"], 10_000, {"ivan": "MRs of theirs reviewed: 2"})
    assert "### ivan\nMRs of theirs reviewed: 2\n## Communication" in rendered
    tight = store.render(["ivan"], 60)
    assert "коротко" in tight and "descriptor" not in tight


def test_unsafe_username_is_never_a_file(tmp_path: Path) -> None:
    store = PeopleStore(tmp_path)
    assert store.path_for("../etc/passwd") is None
    assert store.write("../x", "y") is False


def test_unknown_people_render_nothing(tmp_path: Path) -> None:
    assert PeopleStore(tmp_path).render(["nobody"], 1000) == ""


def test_mentions_are_usernames() -> None:
    assert mentioned_usernames("запомни про @i.petrov и @i.petrov, а также @anna_k") == ["i.petrov", "anna_k"]
