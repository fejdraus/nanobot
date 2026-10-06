from nanobot.channels.gitlab_review.reverts import (
    is_exact_revert,
    label,
    net_from_changes,
    net_from_raw,
    reverted_refs,
)

SHA = "0123456789abcdef0123456789abcdef01234567"


def _raw(path: str, hunks: str) -> str:
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n{hunks}\n"


def test_every_commit_must_name_what_it_reverts() -> None:
    assert reverted_refs(["Revert\n\nThis reverts merge request !5937", f"x\n\nThis reverts commit {SHA}."]) == [
        ("mr", "5937"), ("commit", SHA),
    ]
    assert reverted_refs(["Revert\n\nThis reverts merge request !1", "fix: revert logic in lead page"]) is None
    assert reverted_refs([]) is None
    assert [label(ref) for ref in [("mr", "5937"), ("commit", SHA)]] == ["!5937", "01234567"]


def test_exact_inverse_is_a_revert_and_anything_more_is_not() -> None:
    original = net_from_raw(_raw("a.cs", "@@ -1 +1 @@\n-old\n+new\n context"))
    assert is_exact_revert(net_from_raw(_raw("a.cs", "@@ -1 +1 @@\n-new\n+old\n context")), [original])
    assert not is_exact_revert(net_from_raw(_raw("a.cs", "@@ -1 +1,2 @@\n-new\n+old\n+extra")), [original])
    assert not is_exact_revert(
        net_from_raw(_raw("a.cs", "@@ -1 +1 @@\n-new\n+old") + _raw("b.cs", "@@ -0,0 +1 @@\n+x")), [original]
    )
    assert not is_exact_revert({}, [original])


def test_lines_that_look_like_headers_inside_a_hunk_are_content() -> None:
    original = net_from_raw(_raw("a.sql", "@@ -1 +1 @@\n--- old comment\n+++ new value"))
    assert original == {frozenset({"a.sql"}): {"-- old comment": -1, "++ new value": 1}}


def test_new_deleted_and_renamed_files_match_by_their_paths() -> None:
    original = net_from_raw(
        "diff --git a/N.cs b/N.cs\nnew file mode 100644\n--- /dev/null\n+++ b/N.cs\n@@ -0,0 +1 @@\n+n\n"
        "diff --git a/A.cs b/B.cs\nrename from A.cs\nrename to B.cs\n--- a/A.cs\n+++ b/B.cs\n@@ -1 +1 @@\n-a\n+b\n"
    )
    revert = net_from_raw(
        "diff --git a/N.cs b/N.cs\ndeleted file mode 100644\n--- a/N.cs\n+++ /dev/null\n@@ -1 +0,0 @@\n-n\n"
        "diff --git a/B.cs b/A.cs\nrename from B.cs\nrename to A.cs\n--- a/B.cs\n+++ b/A.cs\n@@ -1 +1 @@\n-b\n+a\n"
    )
    assert is_exact_revert(revert, [original])


def test_binary_or_collapsed_changes_are_never_trusted() -> None:
    assert net_from_raw("diff --git a/x.png b/x.png\nBinary files a/x.png and b/x.png differ\n") is None
    assert net_from_changes([{"old_path": "big.json", "new_path": "big.json", "diff": "", "collapsed": True}]) is None
    assert not is_exact_revert(None, [{}])
