from nanobot.agent import dream_filter

BEFORE = "# Memory\n\n## User\n- Lives in Lviv\n"
AFTER = (
    "# Memory\n\n## User\n- Lives in Lviv\n- Prefers short answers\n\n"
    "## Gaming\n- Battlefield 1 has 2-player co-op\n- Battlefield V has 4-player co-op\n"
)


def test_drops_whole_new_section_and_keeps_line(monkeypatch):
    def fake_scores(_key, lines, idx):
        return {j: (0.1 if lines[j].startswith("## Gaming") else 0.8) for j in idx}

    monkeypatch.setattr(dream_filter, "_scores", fake_scores)
    text, removed = dream_filter.filter_text(BEFORE, AFTER, "k")
    assert "Prefers short answers" in text
    assert "Battlefield" not in text and "## Gaming" not in text
    assert any("Battlefield V" in r for r in removed)


def test_never_touches_edited_lines(monkeypatch):
    monkeypatch.setattr(dream_filter, "_scores", lambda _k, _l, idx: {j: 0.0 for j in idx})
    edited = BEFORE.replace("Lives in Lviv", "Lives in Lviv, Ukraine")
    text, removed = dream_filter.filter_text(BEFORE, edited, "k")
    assert text == edited and removed == []


def test_snapshot_off_without_flag(monkeypatch):
    monkeypatch.delenv("DREAM_FILTER", raising=False)
    assert dream_filter.snapshot(object()) is None
