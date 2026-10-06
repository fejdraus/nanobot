from nanobot.channels.gitlab_review.reverts import is_exact_revert, label, reverted_refs

SHA = "0123456789abcdef0123456789abcdef01234567"


def _change(path: str, diff: str, **extra: object) -> dict[str, object]:
    return {"old_path": path, "new_path": path, "diff": diff, **extra}


def test_every_commit_must_name_what_it_reverts() -> None:
    assert reverted_refs(["Revert\n\nThis reverts merge request !5937", f"x\n\nThis reverts commit {SHA}."]) == [
        ("mr", "5937"), ("commit", SHA),
    ]
    assert reverted_refs(["Revert\n\nThis reverts merge request !1", "fix: revert logic in lead page"]) is None
    assert reverted_refs([]) is None
    assert [label(ref) for ref in [("mr", "5937"), ("commit", SHA)]] == ["!5937", "01234567"]


def test_exact_inverse_is_a_revert_and_anything_more_is_not() -> None:
    original = [_change("a.cs", "@@\n-old\n+new\n context")]
    assert is_exact_revert([_change("a.cs", "@@\n-new\n+old\n context")], [original])
    assert not is_exact_revert([_change("a.cs", "@@\n-new\n+old\n+extra")], [original])
    assert not is_exact_revert([_change("a.cs", "@@\n-new\n+old"), _change("b.cs", "+x")], [original])
    assert not is_exact_revert([], [original])


def test_renamed_and_created_files_are_matched_by_both_paths() -> None:
    original = [{"old_path": "A.cs", "new_path": "B.cs", "diff": "-a\n+b"}, _change("N.cs", "+n", new_file=True)]
    revert = [{"old_path": "B.cs", "new_path": "A.cs", "diff": "-b\n+a"}, _change("N.cs", "-n", deleted_file=True)]
    assert is_exact_revert(revert, [original])


def test_a_cut_short_diff_is_never_trusted() -> None:
    original = [_change("big.json", "", too_large=True)]
    assert not is_exact_revert([_change("big.json", "", too_large=True)], [original])
