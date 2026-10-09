import asyncio
import json
import socket
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.gitlab_review.config import GitLabReviewConfig
from nanobot.channels.gitlab_review.events import ReviewCandidate
from nanobot.channels.gitlab_review.gitlab_api import GitLabApiError
from nanobot.channels.gitlab_review.proposals import ACTIONS_FENCE, ProposedAction
from nanobot.channels.gitlab_review.runtime import GitLabReviewChannel, _PendingRun
from nanobot.channels.gitlab_review.telegram_api import TelegramMessage

SECRET = "s3cret-token"
PROJECT = "astana-group/astana-motors"
CHAT = "49816954"
SHA = "a" * 40

Payload = dict[str, Any]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _config(port: int, **overrides: Any) -> GitLabReviewConfig:
    values: dict[str, Any] = {
        "enabled": True,
        "webhook_secret_token": SECRET,
        "host": "127.0.0.1",
        "port": port,
        "gitlab_url": "https://gitlab.example.com",
        "gitlab_token": "glpat",
        "reviewer_usernames": ["a.tyra"],
        "telegram_bot_token": "1:abc",
        "telegram_chat_id": CHAT,
        "debounce_seconds": 0.05,
        "review_timeout_s": 5,
    }
    values.update(overrides)
    return GitLabReviewConfig(**values)


class FakeGitLab:
    def __init__(self) -> None:
        self.mr: dict[str, Any] = {
            "iid": 42,
            "title": "feat: thing",
            "web_url": "https://gitlab.example.com/mr/42",
            "sha": SHA,
            "state": "opened",
            "draft": False,
            "author": {"username": "author"},
            "source_branch": "AMCRM-99999",
            "description": "Closes AMCRM-16127, AMCRM-16130",
            "diff_refs": {"base_sha": "b", "start_sha": "s", "head_sha": SHA},
        }
        self.discussions: dict[str, list[dict[str, Any]]] = {}
        self.writes: list[tuple[str, Any]] = []
        self.fail_writes = False
        self.reject_lines = False
        self.commits: list[str] = ["feat: thing"]
        self.open_mrs: list[dict[str, Any]] = []
        self.changes_by_iid: dict[int, list[dict[str, Any]]] = {}
        self.raw_by_iid: dict[int, str] = {}
        self.changes_by_sha: dict[str, list[dict[str, Any]]] = {}
        self.fail_changes = False
        self.changes: list[dict[str, Any]] = [
            {
                "new_path": "Pkg/AMLead/Schemas/LeadPage/LeadPage.js",
                "old_path": "Pkg/AMLead/Schemas/LeadPage/LeadPage.js",
                "diff": '@@ -1 +1 @@\n-old\n+this.set("X", 1, {silent: true});',
            },
        ]

    async def get_merge_request(self, iid: int) -> dict[str, Any]:
        return dict(self.mr, iid=iid)

    async def get_changes(self, iid: int) -> list[dict[str, Any]]:
        if self.fail_changes:
            raise GitLabApiError("diffs down", 502)
        return self.changes_by_iid.get(iid, self.changes)

    async def get_raw_diff(self, iid: int) -> str:
        return self.raw_by_iid.get(iid, "")

    async def get_commit(self, sha: str) -> dict[str, Any]:
        return {"id": sha, "parent_ids": ["p" * 40]}

    async def compare(self, base: str, head: str) -> list[dict[str, Any]]:
        return self.changes_by_sha.get(head, [])

    async def get_discussion(self, iid: int, discussion_id: str) -> dict[str, Any]:
        return {"id": discussion_id, "notes": self.discussions.get(discussion_id, [])}

    async def list_open_merge_requests(self, updated_after: str) -> list[dict[str, Any]]:
        return self.open_mrs

    async def get_commits(self, iid: int) -> list[dict[str, Any]]:
        return [{"message": message} for message in self.commits]

    async def list_discussions(self, iid: int) -> list[dict[str, Any]]:
        return [{"id": key, "notes": notes} for key, notes in self.discussions.items()]

    async def _write(self, kind: str, data: Any) -> dict[str, Any]:
        if self.fail_writes:
            raise GitLabApiError("boom", 500)
        self.writes.append((kind, data))
        return {}

    async def create_discussion(self, iid: int, body: str, position: dict[str, Any]) -> dict[str, Any]:
        if self.reject_lines:
            raise GitLabApiError(
                'GitLab 400: {"message":"400 Bad request - Note {:line_code=>[\\"can\'t be blank\\"]}"}', 400
            )
        return await self._write("discussion", (body, position))

    async def create_note(self, iid: int, body: str) -> dict[str, Any]:
        return await self._write("note", body)

    async def reply(self, iid: int, discussion_id: str, body: str) -> dict[str, Any]:
        return await self._write("reply", (discussion_id, body))

    async def approve(self, iid: int, sha: str) -> dict[str, Any]:
        return await self._write("approve", sha)

    async def aclose(self) -> None:
        return None


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send_message(self, chat_id: str, text: str) -> int:
        self.sent.append((chat_id, text))
        return 1000 + len(self.sent)

    async def send_typing(self, chat_id: str) -> None:
        return None

    async def get_updates(self, offset: int | None, timeout_s: int = 30) -> list[TelegramMessage]:
        await asyncio.sleep(3600)
        return []

    async def aclose(self) -> None:
        return None

    def text(self) -> str:
        return "\n".join(text for _, text in self.sent)


class FakeTasks:
    def __init__(self) -> None:
        self.asked: list[str] = []

    async def lookup(self, key: str) -> Any:
        from nanobot.channels.gitlab_review.tasks import TaskInfo

        self.asked.append(key)
        return TaskInfo(key, "Доработать интеграцию", f"https://app.clickup.com/t/{key}")

    async def aclose(self) -> None:
        return None


class Harness:
    def __init__(self, tmp_path: Path, **overrides: Any) -> None:
        self.bus = MessageBus()
        self.inbound: list[InboundMessage] = []
        self.gitlab = FakeGitLab()
        self.telegram = FakeTelegram()
        self.tasks = FakeTasks()
        self.port = _free_port()

        async def capture(message: InboundMessage) -> None:
            self.inbound.append(message)

        self.bus.publish_inbound = capture  # type: ignore[method-assign]
        self.channel = GitLabReviewChannel(
            _config(self.port, **overrides),
            self.bus,
            state_path=tmp_path / "state.sqlite3",
            gitlab_api=self.gitlab,  # type: ignore[arg-type]
            telegram_api=self.telegram,  # type: ignore[arg-type]
            task_lookup=self.tasks,  # type: ignore[arg-type]
        )

    async def __aenter__(self) -> "Harness":
        await self.channel.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.channel.stop()

    def expect(self, iid: int) -> None:
        chat_id = f"gitlab-review:{iid}"
        if chat_id not in self.channel._pending:
            future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            self.channel._pending[chat_id] = _PendingRun(
                future, str(self.gitlab.mr["sha"]), str(self.gitlab.mr["title"]), "", iid=iid
            )

    async def answer(
        self, iid: int, actions: list[dict[str, Any]], summary: str = "Сводка", *, waiting: bool = True
    ) -> None:
        if waiting:
            self.expect(iid)
        content = f"{summary}\n\n```{ACTIONS_FENCE}\n{json.dumps({'actions': actions})}\n```"
        await self.channel.send(
            OutboundMessage(channel="gitlab_review", chat_id=f"gitlab-review:{iid}", content=content)
        )
        self.channel._pending.pop(f"gitlab-review:{iid}", None)

    async def review(self, iid: int, actions: list[dict[str, Any]], summary: str = "Сводка") -> InboundMessage:
        """Run a real review of MR *iid* and answer it with *actions*."""
        before = len(self.inbound)
        task = asyncio.create_task(self.channel.process(ReviewCandidate(kind="merge_request", iid=iid)))
        await _until(lambda: len(self.inbound) > before)
        await self.reply_as_agent(f"gitlab-review:{iid}", _answer(summary, actions))
        await task
        return self.inbound[before]

    async def say(self, update_id: int, text: str, *, reply_to: int | None = None) -> InboundMessage:
        """Send the approver's message and wait for the agent turn it starts."""
        before = len(self.inbound)
        await self.channel.handle_telegram(_tg(update_id, text, reply_to=reply_to))
        await _until(lambda: len(self.inbound) > before)
        return self.inbound[-1]

    async def reply_as_agent(self, chat_id: str, content: str) -> None:
        await _until(lambda: chat_id in self.channel._pending)
        await self.channel.send(OutboundMessage(channel="gitlab_review", chat_id=chat_id, content=content))
        await _until(lambda: chat_id not in self.channel._pending)


def _answer(summary: str, actions: list[dict[str, Any]] | None = None, decision: dict[str, Any] | None = None) -> str:
    content = summary
    if actions is not None:
        content += f"\n\n```{ACTIONS_FENCE}\n{json.dumps({'actions': actions})}\n```"
    if decision is not None:
        content += f"\n\n```gitlab-review-decision\n{json.dumps(decision)}\n```"
    return content


def _tg(
    update_id: int, text: str, *, chat: str = CHAT, sender: str = CHAT, reply_to: int | None = None
) -> TelegramMessage:
    return TelegramMessage(update_id, chat, text, sender, reply_to)


def _post(
    port: int,
    payload: Payload,
    *,
    token: str | None = SECRET,
    event: str = "Merge Request Hook",
    event_uuid: str = "uuid-1",
    path: str = "/gitlab/webhook",
) -> int:
    headers = {"Content-Type": "application/json", "X-Gitlab-Event": event, "X-Gitlab-Event-UUID": event_uuid}
    if token is not None:
        headers["X-Gitlab-Token"] = token
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=json.dumps(payload).encode(), method="POST", headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def _mr_payload(iid: int = 42) -> Payload:
    return {
        "project": {"path_with_namespace": PROJECT},
        "object_attributes": {"iid": iid, "action": "open", "author_id": 7},
    }


def _note_payload(discussion_id: str = "d1", username: str = "author") -> Payload:
    return {
        "project": {"path_with_namespace": PROJECT},
        "object_attributes": {
            "id": 5,
            "noteable_type": "MergeRequest",
            "discussion_id": discussion_id,
            "system": False,
        },
        "merge_request": {"iid": 42, "state": "opened", "author_id": 7},
        "user": {"username": username},
    }


def _note(username: str, body: str = "text") -> dict[str, Any]:
    return {"id": 1, "author": {"username": username}, "body": body, "system": False}


async def _until(predicate: Any, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_webhook_rejects_wrong_token_and_path(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        assert await asyncio.to_thread(_post, h.port, _mr_payload(), token="nope") == 401
        assert await asyncio.to_thread(_post, h.port, _mr_payload(), token=None) == 401
        assert await asyncio.to_thread(_post, h.port, _mr_payload(), path="/other") == 404
    assert h.inbound == []


@pytest.mark.asyncio
async def test_opened_mr_runs_one_review_and_drafts_go_to_telegram(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        assert await asyncio.to_thread(_post, h.port, _mr_payload()) == 200
        assert await asyncio.to_thread(_post, h.port, _mr_payload()) == 200
        await _until(lambda: len(h.inbound) == 1)
        assert h.inbound[0].chat_id == "gitlab-review:42"
        assert not h.inbound[0].content.startswith("/")
        assert "review-gitlab-mrs" in h.inbound[0].content
        assert "Draft mode" in h.inbound[0].content

        await h.answer(42, [{"type": "note", "body": "замечание"}])

        assert "замечание" in h.telegram.text()
        assert "Ответьте на это сообщение" in h.telegram.text()
        assert "публикуй !42/1" in h.telegram.text()
        assert h.gitlab.writes == []


@pytest.mark.asyncio
async def test_mr_by_reviewer_is_not_reviewed(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        h.gitlab.mr["author"] = {"username": "A.Tyra"}
        await h.channel.process(ReviewCandidate(kind="merge_request", iid=42))
    assert h.inbound == []


@pytest.mark.asyncio
async def test_draft_or_closed_mr_is_not_reviewed(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        h.gitlab.mr["draft"] = True
        await h.channel.process(ReviewCandidate(kind="merge_request", iid=42))
        h.gitlab.mr["draft"] = False
        h.gitlab.mr["state"] = "merged"
        await h.channel.process(ReviewCandidate(kind="merge_request", iid=42))
    assert h.inbound == []


@pytest.mark.asyncio
async def test_reply_in_reviewer_thread_runs_scoped_review(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        h.gitlab.discussions["d1"] = [_note("a.tyra", "issue"), _note("author", "fixed it")]
        assert await asyncio.to_thread(_post, h.port, _note_payload(), event="Note Hook") == 200
        await _until(lambda: len(h.inbound) == 1)
        assert "d1" in h.inbound[0].content
        assert "fixed it" in h.inbound[0].content
        await h.answer(42, [])


@pytest.mark.asyncio
async def test_notes_in_one_thread_are_debounced_into_one_run(tmp_path: Path) -> None:
    async with Harness(tmp_path, debounce_seconds=0.3) as h:
        h.gitlab.discussions["d1"] = [_note("a.tyra"), _note("author", "img"), _note("author", "text")]
        for uuid in ("u1", "u2", "u3"):
            await asyncio.to_thread(_post, h.port, _note_payload(), event="Note Hook", event_uuid=uuid)
        await _until(lambda: len(h.inbound) == 1)
        await asyncio.sleep(0.5)
        assert len(h.inbound) == 1
        await h.answer(42, [])


@pytest.mark.asyncio
async def test_foreign_thread_does_not_start_a_run(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        h.gitlab.discussions["d1"] = [_note("lead", "nit"), _note("author", "done")]
        await h.channel.process(ReviewCandidate(kind="note", iid=42, discussion_id="d1"))
    assert h.inbound == []
    assert h.telegram.sent == []


@pytest.mark.asyncio
async def test_mention_in_foreign_thread_only_notifies(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        h.gitlab.discussions["d1"] = [_note("lead", "nit"), _note("author", "@a.tyra look")]
        await h.channel.process(ReviewCandidate(kind="note", iid=42, discussion_id="d1"))
    assert h.inbound == []
    assert "@a.tyra look" in h.telegram.text()


@pytest.mark.asyncio
async def test_hourly_limit_stops_runs(tmp_path: Path) -> None:
    async with Harness(tmp_path, max_runs_per_mr_per_hour=1) as h:
        await h.channel.process(ReviewCandidate(kind="merge_request", iid=42))
        await h.answer(42, [])
    async with Harness(tmp_path, max_runs_per_mr_per_hour=1) as h2:
        h2.gitlab.mr["sha"] = "f" * 40
        await h2.channel.process(ReviewCandidate(kind="merge_request", iid=42))
        assert h2.inbound == []
        assert "Лимит" in h2.telegram.text()


@pytest.mark.asyncio
async def test_publish_posts_only_approved_items(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        await h.answer(
            42,
            [
                {"type": "discussion", "path": "a.cs", "line": 10, "body": "inline"},
                {"type": "note", "body": "general"},
                {"type": "reply", "discussion_id": "d1", "body": "answer"},
            ],
        )
        await h.channel.handle_telegram(_tg(1, "публикуй !42 1,3"))

    kinds = [kind for kind, _ in h.gitlab.writes]
    assert kinds == ["discussion", "reply"]
    body, position = h.gitlab.writes[0][1]
    assert body == "inline"
    assert position["new_line"] == 10 and position["head_sha"] == SHA


@pytest.mark.asyncio
async def test_publish_refused_when_branch_moved(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        await h.answer(42, [{"type": "approve"}])
        h.gitlab.mr["sha"] = "b" * 40
        await h.channel.handle_telegram(_tg(1, "публикуй !42"))
    assert h.gitlab.writes == []
    assert "ветка изменилась" in h.telegram.text()


@pytest.mark.asyncio
async def test_other_chats_cannot_approve(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        await h.answer(42, [{"type": "note", "body": "general"}])
        sent_before = len(h.telegram.sent)
        await h.channel.handle_telegram(_tg(1, "публикуй !42", chat="999", sender="999"))
    assert h.gitlab.writes == []
    assert len(h.telegram.sent) == sent_before


@pytest.mark.asyncio
async def test_cancel_drops_the_draft(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        await h.answer(42, [{"type": "note", "body": "general"}])
        await h.channel.handle_telegram(_tg(1, "отмена !42"))
        await h.channel.handle_telegram(_tg(2, "публикуй !42"))
    assert h.gitlab.writes == []
    assert "нет черновика" in h.telegram.text()


@pytest.mark.asyncio
async def test_publish_reports_errors(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        await h.answer(42, [{"type": "note", "body": "general"}])
        h.gitlab.fail_writes = True
        await h.channel.handle_telegram(_tg(1, "публикуй !42"))
    assert "ошибка" in h.telegram.text()


@pytest.mark.asyncio
async def test_progress_messages_are_not_drafts(tmp_path: Path) -> None:
    from nanobot.events import RetryWaitEvent

    async with Harness(tmp_path) as h:
        await h.channel.send(
            OutboundMessage(
                channel="gitlab_review",
                chat_id="gitlab-review:42",
                content="retrying",
                event=RetryWaitEvent.__new__(RetryWaitEvent),
            )
        )
    assert h.telegram.sent == []


@pytest.mark.asyncio
async def test_answer_after_timeout_is_not_turned_into_a_draft(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        await h.answer(42, [{"type": "approve"}], waiting=False)
        await h.channel.handle_telegram(_tg(1, "публикуй !42"))
    assert h.gitlab.writes == []
    assert "после таймаута" in h.telegram.text()


@pytest.mark.asyncio
async def test_timeout_stops_the_agent_turn(tmp_path: Path) -> None:
    async with Harness(tmp_path, review_timeout_s=0.05) as h:
        await h.channel.process(ReviewCandidate(kind="merge_request", iid=42))
    assert [message.content for message in h.inbound][-1] == "/stop"
    assert h.inbound[-1].chat_id == "gitlab-review:42"
    assert "остановлено" in h.telegram.text()


@pytest.mark.asyncio
async def test_replaced_draft_needs_its_version(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        await h.answer(42, [{"type": "note", "body": "first"}])
        await h.answer(42, [{"type": "note", "body": "second"}])
        await h.channel.handle_telegram(_tg(1, "публикуй !42"))
        await h.channel.handle_telegram(_tg(2, "публикуй !42/1"))
        assert h.gitlab.writes == []
        await h.channel.handle_telegram(_tg(3, "публикуй !42/2"))
    assert h.gitlab.writes == [("note", "second")]
    assert "Укажите версию" in h.telegram.text()
    assert "уже заменён" in h.telegram.text()


@pytest.mark.asyncio
async def test_group_chat_accepts_only_listed_senders(tmp_path: Path) -> None:
    group = "-100123"
    async with Harness(tmp_path, telegram_chat_id=group, telegram_user_ids=["49816954"]) as h:
        await h.answer(42, [{"type": "note", "body": "general"}])
        await h.channel.handle_telegram(_tg(1, "публикуй !42", chat=group, sender="777"))
        assert h.gitlab.writes == []
        await h.channel.handle_telegram(_tg(2, "публикуй !42", chat=group, sender="49816954"))
    assert h.gitlab.writes == [("note", "general")]


def test_group_chat_without_listed_senders_is_rejected() -> None:
    with pytest.raises(ValueError, match="telegramUserIds"):
        _config(3980, telegram_chat_id="-100123")


@pytest.mark.asyncio
async def test_failed_command_does_not_stop_polling(tmp_path: Path) -> None:
    class OneShotTelegram(FakeTelegram):
        def __init__(self) -> None:
            super().__init__()
            self.batches = [[_tg(1, "публикуй !42")], [_tg(2, "отмена !42")]]

        async def get_updates(self, offset: int | None, timeout_s: int = 30) -> list[TelegramMessage]:
            if self.batches:
                return self.batches.pop(0)
            await asyncio.sleep(3600)
            return []

    h = Harness(tmp_path)
    h.telegram = OneShotTelegram()
    h.channel._telegram = h.telegram  # type: ignore[assignment]
    h.expect(42)
    await h.answer(42, [{"type": "note", "body": "general"}])

    async def broken(iid: int) -> Any:
        raise GitLabApiError("gitlab down", 502)

    h.channel._merge_request = broken  # type: ignore[method-assign]
    async with h:
        await _until(lambda: "черновик отменён" in h.telegram.text())
    assert "Команда не выполнена" in h.telegram.text()


@pytest.mark.asyncio
async def test_own_mr_is_reviewed_when_enabled(tmp_path: Path) -> None:
    async with Harness(tmp_path, review_own_merge_requests=True, review_timeout_s=0.05) as h:
        h.gitlab.mr["author"] = {"username": "a.tyra"}
        await h.channel.process(ReviewCandidate(kind="merge_request", iid=42))
    prompt = h.inbound[0].content
    assert "review-gitlab-mrs" in prompt
    assert "reviewer's own merge request" in prompt


@pytest.mark.asyncio
async def test_approval_of_own_mr_is_never_drafted_or_published(tmp_path: Path) -> None:
    async with Harness(tmp_path, review_own_merge_requests=True) as h:
        h.gitlab.mr["author"] = {"username": "a.tyra"}
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        h.channel._pending["gitlab-review:42"] = _PendingRun(future, SHA, "feat: thing", "", own=True, iid=42)
        await h.answer(42, [{"type": "note", "body": "general"}, {"type": "approve"}], waiting=False)
        assert "аппрув своего MR снят" in h.telegram.text()
        h.channel._state.save_draft(42, SHA, (ProposedAction("approve"),), 9)
        await h.channel.handle_telegram(_tg(1, "публикуй !42/9"))
    assert ("approve", SHA) not in h.gitlab.writes
    assert "это ваш MR" in h.telegram.text()


def _write_lessons(directory: Path) -> None:
    directory.mkdir()
    for name, glob, body in (
        ("lead.md", "Pkg/AMLead/**", "Урок про лиды."),
        ("cs.md", "**/*.cs", "Урок про C#."),
    ):
        (directory / name).write_text(
            f'---\nname: {name}\ndescription: "x"\napplies_to: ["{glob}"]\nkeywords: []\n---\n\n{body}\n',
            encoding="utf-8",
        )


@pytest.mark.asyncio
async def test_matching_lessons_go_into_the_review_prompt(tmp_path: Path) -> None:
    _write_lessons(tmp_path / "memory")
    async with Harness(tmp_path, lessons_dir=str(tmp_path / "memory"), review_timeout_s=0.05) as h:
        await h.channel.process(ReviewCandidate(kind="merge_request", iid=42))
    prompt = h.inbound[0].content
    assert "Урок про лиды." in prompt
    assert "Урок про C#." not in prompt


@pytest.mark.asyncio
async def test_lesson_failure_does_not_block_the_review(tmp_path: Path) -> None:
    _write_lessons(tmp_path / "memory")
    async with Harness(tmp_path, lessons_dir=str(tmp_path / "memory"), review_timeout_s=0.05) as h:
        h.gitlab.fail_changes = True
        await h.channel.process(ReviewCandidate(kind="merge_request", iid=42))
    assert "review-gitlab-mrs" in h.inbound[0].content
    assert "Lessons from the review memory" not in h.inbound[0].content


@pytest.mark.asyncio
async def test_reply_to_draft_publishes_that_draft(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        await h.answer(42, [{"type": "note", "body": "first"}, {"type": "note", "body": "second"}])
        draft_message = 1000 + len(h.telegram.sent)
        await h.answer(43, [{"type": "note", "body": "other mr"}])
        await h.channel.handle_telegram(_tg(1, "публикуй 2", reply_to=draft_message))
        await h.channel.handle_telegram(_tg(2, "публикуй 1,3", reply_to=draft_message))
    assert h.gitlab.writes == [("note", "second")]
    assert "нет черновика для публикации" in h.telegram.text()


@pytest.mark.asyncio
async def test_reply_to_replaced_draft_is_refused(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        await h.answer(42, [{"type": "note", "body": "old"}])
        old_message = 1000 + len(h.telegram.sent)
        await h.answer(42, [{"type": "note", "body": "new"}])
        await h.channel.handle_telegram(_tg(1, "публикуй", reply_to=old_message))
        await h.channel.handle_telegram(_tg(2, "отмена", reply_to=old_message))
    assert h.gitlab.writes == []
    assert "уже заменён" in h.telegram.text()
    assert "Ничего не отменено" in h.telegram.text()


@pytest.mark.asyncio
async def test_reply_cancel_drops_the_draft(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        await h.answer(42, [{"type": "note", "body": "general"}])
        message = 1000 + len(h.telegram.sent)
        await h.channel.handle_telegram(_tg(1, "отмена", reply_to=message))
        await h.channel.handle_telegram(_tg(2, "публикуй !42"))
    assert h.gitlab.writes == []
    assert "черновик отменён" in h.telegram.text()


@pytest.mark.asyncio
async def test_bare_publish_applies_to_the_only_waiting_draft(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        await h.answer(42, [{"type": "note", "body": "general"}])
        await h.channel.handle_telegram(_tg(1, "Публикуй"))
    assert h.gitlab.writes == [("note", "general")]


@pytest.mark.asyncio
async def test_bare_publish_with_several_drafts_asks_which(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        await h.answer(42, [{"type": "note", "body": "first"}])
        await h.answer(43, [{"type": "note", "body": "second"}])
        await h.channel.handle_telegram(_tg(1, "публикуй"))
        await h.channel.handle_telegram(_tg(2, "отмена"))
    assert h.gitlab.writes == []
    assert "Черновиков несколько" in h.telegram.text()
    assert "!42/1" in h.telegram.text() and "!43/1" in h.telegram.text()


@pytest.mark.asyncio
async def test_draft_names_the_task(tmp_path: Path) -> None:
    async with Harness(tmp_path, review_timeout_s=0.5) as h:
        task = asyncio.create_task(h.channel.process(ReviewCandidate(kind="merge_request", iid=42)))
        await _until(lambda: len(h.inbound) == 1)
        await h.channel.send(
            OutboundMessage(channel="gitlab_review", chat_id="gitlab-review:42",
                            content=f"S\n\n```{ACTIONS_FENCE}\n{{\"actions\": []}}\n```")
        )
        await task
    text = h.telegram.text()
    assert "Задача: AMCRM-16127 — Доработать интеграцию" in text
    assert "https://app.clickup.com/t/AMCRM-16127" in text
    assert h.tasks.asked == ["AMCRM-16127", "AMCRM-16130"]
    assert "AMCRM-99999" not in text


@pytest.mark.asyncio
async def test_each_review_starts_its_own_claude_session(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        first = await h.review(42, [{"type": "note", "body": "a"}])
        h.gitlab.mr["sha"] = "f" * 40
        second = await h.review(42, [{"type": "note", "body": "b"}])
    sessions = [message.metadata["claude_cli"] for message in (first, second)]
    assert sessions[0]["session_id"] != sessions[1]["session_id"]
    assert not sessions[0]["resume"] and not sessions[1]["resume"]


@pytest.mark.asyncio
async def test_question_about_a_draft_continues_its_review_session(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        review = await h.review(42, [{"type": "note", "body": "флаг не сбрасывается"}])
        draft_message = 1000 + len(h.telegram.sent)
        turn = await h.say(1, "почему флаг не сбрасывается?", reply_to=draft_message)
        assert turn.metadata["claude_cli"] == {
            "session_id": review.metadata["claude_cli"]["session_id"], "resume": True,
        }
        assert "почему флаг не сбрасывается?" in turn.content
        assert "1. [общий комментарий] флаг не сбрасывается" in turn.content
        await h.reply_as_agent("gitlab-review:42", "Потому что присваивается только true.")
        answer_message = 1000 + len(h.telegram.sent)
        assert h.telegram.sent[-1][1] == "!42 · Потому что присваивается только true."

        follow_up = await h.say(2, "а в C#?", reply_to=answer_message)
        assert follow_up.metadata["claude_cli"]["session_id"] == review.metadata["claude_cli"]["session_id"]
        await h.reply_as_agent("gitlab-review:42", "Тоже нет.")
    assert h.gitlab.writes == []


@pytest.mark.asyncio
async def test_question_without_reply_goes_to_the_only_waiting_draft(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        review = await h.review(42, [{"type": "note", "body": "a"}])
        turn = await h.say(1, "уточни второе замечание")
        await h.reply_as_agent("gitlab-review:42", "Уточняю.")
    assert turn.metadata["claude_cli"]["session_id"] == review.metadata["claude_cli"]["session_id"]


@pytest.mark.asyncio
async def test_revised_draft_becomes_a_new_version(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        await h.review(42, [{"type": "note", "body": "old"}, {"type": "note", "body": "keep"}])
        await h.say(1, "убери первое замечание")
        await h.reply_as_agent("gitlab-review:42", _answer("Убрал.", [{"type": "note", "body": "keep"}]))
        assert "Черновик 42/2" in h.telegram.text()
        await h.channel.handle_telegram(_tg(2, "публикуй !42/1"))
        assert h.gitlab.writes == []
        await h.channel.handle_telegram(_tg(3, "публикуй !42/2"))
    assert h.gitlab.writes == [("note", "keep")]


@pytest.mark.asyncio
async def test_agent_decision_publishes_on_the_humans_own_words(tmp_path: Path) -> None:
    decision = {"decision": "publish", "iid": 42, "items": [2]}
    async with Harness(tmp_path) as h:
        await h.review(42, [{"type": "note", "body": "one"}, {"type": "note", "body": "two"}])
        await h.say(1, "выкатывай второе")
        await h.reply_as_agent("gitlab-review:42", _answer("Понял.", decision=decision))
    assert h.gitlab.writes == [("note", "two")]
    assert "!42: публикую по вашему «выкатывай второе»." in h.telegram.text()


@pytest.mark.asyncio
async def test_decision_next_to_a_revised_draft_is_not_executed(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        await h.review(42, [{"type": "note", "body": "one"}])
        await h.say(1, "перепиши и публикуй")
        await h.reply_as_agent(
            "gitlab-review:42",
            _answer("Готово.", [{"type": "note", "body": "new"}], {"decision": "publish", "iid": 42}),
        )
    assert h.gitlab.writes == []
    assert "сначала посмотрите новую версию" in h.telegram.text()


@pytest.mark.asyncio
async def test_question_about_an_earlier_review_brings_its_archive_entry(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        await h.review(42, [{"type": "note", "body": "старое замечание про флаг"}])
        await h.channel.handle_telegram(_tg(1, "публикуй !42"))
        h.gitlab.mr["description"] = "Closes AMCRM-20000"
        await h.review(43, [{"type": "note", "body": "новое"}])
        turn = await h.say(2, "что было в ревью по AMCRM-16127?", reply_to=1000 + len(h.telegram.sent))
        await h.reply_as_agent("gitlab-review:43", "Там был флаг.")
    assert "From the archive" in turn.content
    assert "старое замечание про флаг" in turn.content
    assert "опубликовано" in turn.content
    entries = sorted((tmp_path / "archive").glob("*.md"))
    assert [entry.name for entry in entries] == ["00001-mr42.md", "00002-mr43.md"]
    assert "AMCRM-16127" in entries[0].read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_talk_without_any_review_gets_its_own_session(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        turn = await h.say(1, "что ты умеешь?")
        chat_id = turn.chat_id
        assert chat_id.startswith("gitlab-review:chat-")
        assert turn.metadata["claude_cli"]["resume"] is False
        await h.reply_as_agent(chat_id, "Ревьюить MR.")
        follow_up = await h.say(2, "а ещё?", reply_to=1000 + len(h.telegram.sent))
        await h.reply_as_agent(chat_id, "Всё.")
    assert follow_up.metadata["claude_cli"] == {
        "session_id": turn.metadata["claude_cli"]["session_id"], "resume": True,
    }


@pytest.mark.asyncio
async def test_cancel_without_a_draft_says_so(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        await h.channel.handle_telegram(_tg(1, "отмена !42"))
    assert "нет черновика для отмены" in h.telegram.text()


@pytest.mark.asyncio
async def test_question_about_a_draft_without_archive_entry_starts_one(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        await h.answer(42, [{"type": "note", "body": "old draft"}])
        turn = await h.say(1, "почему так?", reply_to=1000 + len(h.telegram.sent))
        await h.reply_as_agent("gitlab-review:42", "Потому что.")
    assert turn.metadata["claude_cli"]["resume"] is False
    assert "old draft" in turn.content
    assert "MR !42" in turn.content
    assert h.channel._state.latest_review(42) is not None


@pytest.mark.asyncio
async def test_slash_message_is_talked_about_like_any_other(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        turn = await h.say(1, "/start")
        await h.reply_as_agent(turn.chat_id, "Я ревьюер.")
    assert not turn.content.startswith("/")
    assert "> /start" in turn.content
    assert h.telegram.sent[-1][1] == "Я ревьюер."


def _people(body: str) -> str:
    return f"\n\n```gitlab-review-people\n{body}\n```"


def _profile(*lines: str) -> str:
    body = "\n".join(lines)
    return f"Готово.\n\n```gitlab-review-profile\n{body}\n```"


@pytest.mark.asyncio
async def test_notes_after_a_review_are_evidence_not_a_profile(tmp_path: Path) -> None:
    people = tmp_path / "people"
    async with Harness(tmp_path, people_dir=str(people)) as h:
        first = await h.review(42, [{"type": "note", "body": "a"}])
        assert "You may write only about: author" in first.content
        before = len(h.inbound)
        h.gitlab.mr["sha"] = "f" * 40
        task = asyncio.create_task(h.channel.process(ReviewCandidate(kind="merge_request", iid=42)))
        await _until(lambda: len(h.inbound) > before)
        await h.reply_as_agent(
            "gitlab-review:42",
            _answer("S", [])
            + _people('{"people": [{"username": "author", "notes": ["отвечает скрином"]},'
                      ' {"username": "stranger", "notes": ["что-то"]}]}'),
        )
        await task
        evidence = h.channel._state.evidence_for("author", datetime.now() - timedelta(days=1))
        stranger = h.channel._state.evidence_users(datetime.now() - timedelta(days=1))
    assert [(iid, text) for _, iid, text in evidence] == [(42, "отвечает скрином")]
    assert stranger == ["author"]
    assert not (people / "author.md").exists()
    assert "отвечает скрином" not in h.telegram.text()
    assert "```gitlab-review-people" not in h.telegram.text()


@pytest.mark.asyncio
async def test_daily_consolidation_builds_the_profile_from_evidence_and_own_words(tmp_path: Path) -> None:
    people = tmp_path / "people"
    async with Harness(tmp_path, people_dir=str(people)) as h:
        await h.review(42, [{"type": "note", "body": "a"}])
        h.channel._state.add_evidence("author", 42, "отвечает скрином переписки")
        h.gitlab.discussions["d1"] = [
            _note("a.tyra", "вопрос"),
            dict(_note("author", "Відповідальний аналітик дав обгрунтовну відповідь"), created_at="2026-10-02T07:30:29Z"),
        ]
        h.gitlab.discussions["d2"] = [_note("author", "чужой тред"), _note("other", "не наш")]
        task = asyncio.create_task(h.channel.dream())
        await _until(lambda: "gitlab-review:dream-author" in h.channel._pending)
        prompt = h.inbound[-1].content
        assert "отвечает скрином переписки" in prompt
        assert "!42: Відповідальний аналітик дав обгрунтовну відповідь" in prompt
        assert "не наш" not in prompt
        await h.reply_as_agent(
            "gitlab-review:dream-author",
            _profile(
                "## Communication",
                "- Пишет по-украински, коротко; на вопрос отвечает скрином переписки (!42)",
                "## Code habits",
                "## Strengths and areas",
            ),
        )
        await task
    profile = (people / "author.md").read_text(encoding="utf-8")
    assert "## Communication\n- Пишет по-украински" in profile
    assert "author: +1 −0\n+ Пишет по-украински" in h.telegram.text()
    assert "ночная сводка" in h.telegram.text()


@pytest.mark.asyncio
async def test_profile_that_breaks_the_rules_is_not_written(tmp_path: Path) -> None:
    people = tmp_path / "people"
    async with Harness(tmp_path, people_dir=str(people)) as h:
        await h.review(42, [])
        h.channel._state.add_evidence("author", 42, "x")
        task = asyncio.create_task(h.channel.dream())
        await h.reply_as_agent("gitlab-review:dream-author", _profile("## Communication", "- ленивый (!42)"))
        await task
    assert not (people / "author.md").exists()
    assert "профиль не обновлён" in h.telegram.text()


@pytest.mark.asyncio
async def test_profile_goes_into_the_next_review(tmp_path: Path) -> None:
    people = tmp_path / "people"
    people.mkdir()
    (people / "author.md").write_text(
        "---\nname: dev_author\n---\n\n# author\n\n## Communication\n- Пишет по-украински (!1)\n"
        "## Code habits\n## Strengths and areas\n",
        encoding="utf-8",
    )
    async with Harness(tmp_path, people_dir=str(people)) as h:
        review = await h.review(42, [])
    assert "### author" in review.content
    assert "## Communication\n- Пишет по-украински (!1)" in review.content
    assert "name: dev_author" not in review.content


@pytest.mark.asyncio
async def test_human_can_ask_to_remember_a_mentioned_developer(tmp_path: Path) -> None:
    people = tmp_path / "people"
    async with Harness(tmp_path, people_dir=str(people)) as h:
        await h.review(42, [{"type": "note", "body": "a"}])
        turn = await h.say(1, "запомни: @i.petrov просит пример кода к замечанию")
        assert "i.petrov" in turn.content and "author" in turn.content
        await h.reply_as_agent(
            "gitlab-review:42",
            "Запомнил." + _people('{"people": [{"username": "i.petrov", "notes": ["просит пример кода"]}]}'),
        )
        evidence = h.channel._state.evidence_for("i.petrov", datetime.now() - timedelta(days=1))
    assert [text for _, _, text in evidence] == ["просит пример кода"]


@pytest.mark.asyncio
async def test_without_a_people_directory_nothing_is_asked_or_filed(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        review = await h.review(42, [])
    assert "gitlab-review-people" not in review.content


@pytest.mark.asyncio
async def test_excluded_developer_is_never_profiled(tmp_path: Path) -> None:
    people = tmp_path / "people"
    people.mkdir()
    (people / "author.md").write_text("# author\n## Communication\n- старое\n", encoding="utf-8")
    async with Harness(tmp_path, people_dir=str(people), people_excluded=["Author"]) as h:
        review = await h.review(42, [])
        assert "gitlab-review-people" not in review.content
        assert "старое" not in review.content
        before = len(h.inbound)
        h.gitlab.mr["sha"] = "f" * 40
        task = asyncio.create_task(h.channel.process(ReviewCandidate(kind="merge_request", iid=42)))
        await _until(lambda: len(h.inbound) > before)
        await h.reply_as_agent(
            "gitlab-review:42",
            _answer("S", []) + _people('{"people": [{"username": "author", "notes": ["новое"]}]}'),
        )
        await task
        h.channel._state.add_evidence("author", 42, "подложено")
        inbound = len(h.inbound)
        await h.channel.dream()
        evidence = h.channel._state.evidence_for("author", datetime.now() - timedelta(days=1))
    assert [text for _, _, text in evidence] == ["подложено"]
    assert len(h.inbound) == inbound


@pytest.mark.asyncio
async def test_plain_message_continues_the_latest_conversation(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        first = await h.say(1, "есть ещё MR без ревью?")
        await h.reply_as_agent(first.chat_id, "Разобрать ответ в !42?")
        second = await h.say(2, "Да")
        await h.reply_as_agent(second.chat_id, "Разобрал.")
    assert second.chat_id == first.chat_id
    assert second.metadata["claude_cli"] == {
        "session_id": first.metadata["claude_cli"]["session_id"], "resume": True,
    }


@pytest.mark.asyncio
async def test_draft_from_a_conversation_names_its_mr_and_can_be_published(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        turn = await h.say(1, "разбери ответ в треде !42")
        await h.reply_as_agent(
            turn.chat_id,
            "Предлагаю ответить так:\n\n```gitlab-review-actions\n"
            '{"iid": 42, "actions": [{"type": "reply", "discussion_id": "d1", "body": "Принято"}]}\n```',
        )
        assert "Черновик 42/1" in h.telegram.text()
        assert "Черновик из разговора." in h.telegram.text()
        assert "!42 · Предлагаю ответить так:\n\n(черновик — следующим сообщением)" in h.telegram.text()
        assert "Принято" in h.telegram.text()
        await h.channel.handle_telegram(_tg(2, "публикуй", reply_to=1000 + len(h.telegram.sent)))
        review = h.channel._state.latest_review(42)
    assert h.gitlab.writes == [("reply", ("d1", "Принято"))]
    assert review is not None and review.kind == "conversation"


@pytest.mark.asyncio
async def test_draft_without_an_mr_is_not_dropped_silently(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        turn = await h.say(1, "что ответить?")
        await h.reply_as_agent(
            turn.chat_id,
            'Так:\n\n```gitlab-review-actions\n{"actions": [{"type": "note", "body": "x"}]}\n```',
        )
    assert "не указан MR" in h.telegram.text()
    assert h.channel._state.pending_drafts() == []


ENGLISH = (
    "The revert is exact. Excluding the CI scripts, the branch matches the commit before the merge "
    "with no differences, and nothing references the removed code anymore."
)


@pytest.mark.asyncio
async def test_english_summary_is_asked_again_in_russian_in_the_same_session(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        task = asyncio.create_task(h.channel.process(ReviewCandidate(kind="merge_request", iid=42)))
        await _until(lambda: len(h.inbound) == 1)
        session = h.inbound[0].metadata["claude_cli"]["session_id"]
        await h.channel.send(OutboundMessage(channel="gitlab_review", chat_id="gitlab-review:42",
                                             content=_answer(ENGLISH, [{"type": "note", "body": "Ревёрт точный"}])))
        await _until(lambda: len(h.inbound) == 2)
        assert h.inbound[1].metadata["claude_cli"] == {"session_id": session, "resume": True}
        assert "in Russian" in h.inbound[1].content
        assert h.telegram.sent == []
        await h.channel.send(OutboundMessage(channel="gitlab_review", chat_id="gitlab-review:42",
                                             content=_answer("Ревёрт точный, замечаний нет.", [{"type": "note", "body": "Ревёрт точный"}])))
        await task
    assert "Ревёрт точный, замечаний нет." in h.telegram.text()
    assert "The revert is exact" not in h.telegram.text()


@pytest.mark.asyncio
async def test_english_twice_is_delivered_rather_than_looping(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        task = asyncio.create_task(h.channel.process(ReviewCandidate(kind="merge_request", iid=42)))
        await _until(lambda: len(h.inbound) == 1)
        for expected in (2, 2):
            await h.channel.send(OutboundMessage(channel="gitlab_review", chat_id="gitlab-review:42",
                                                 content=_answer(ENGLISH, [])))
            await _until(lambda: len(h.inbound) == expected)
        await task
    assert "The revert is exact" in h.telegram.text()


def test_russian_prose_with_code_is_not_english() -> None:
    from nanobot.channels.gitlab_review.proposals import written_in_english

    text = "Флаг `BnzIsHideSendForImplementationButton` нигде не сбрасывается в `false`, см. https://gitlab.example.com/a/b"
    assert not written_in_english(text)
    assert written_in_english(ENGLISH)


def _change(path: str, diff: str) -> dict[str, Any]:
    return {"old_path": path, "new_path": path, "diff": diff}


SHA_B = "ab" * 20


def _revert_setup(h: Harness, revert_diff: str) -> None:
    h.gitlab.commits = [
        'Revert "Merge branch \'AMDEV-310\'"\n\nThis reverts merge request !5937',
        f'Revert "fix"\n\nThis reverts commit {SHA_B}.',
    ]
    h.gitlab.raw_by_iid[5937] = _raw("a.cs", "@@ -1 +1,2 @@\n-old\n+new\n+added")
    h.gitlab.changes_by_sha[SHA_B] = [_change("b.js", "@@ -1 +1 @@\n-x\n+y")]
    h.gitlab.raw_by_iid[42] = _raw("a.cs", revert_diff) + _raw("b.js", "@@ -1 +1 @@\n-y\n+x")


def _raw(path: str, hunks: str) -> str:
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n{hunks}\n"


@pytest.mark.asyncio
async def test_exact_revert_is_skipped_with_a_notice(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        _revert_setup(h, "@@ -1,2 +1 @@\n-new\n-added\n+old")
        await h.channel.process(ReviewCandidate(kind="merge_request", iid=42))
    assert h.inbound == []
    assert "!42 — ревёрт !5937, abababab, ревью пропущено. Проверить всё же: «проверь !42»." in h.telegram.text()


@pytest.mark.asyncio
async def test_revert_that_changes_anything_else_is_reviewed(tmp_path: Path) -> None:
    async with Harness(tmp_path, review_timeout_s=0.05) as h:
        _revert_setup(h, "@@ -1,2 +1 @@\n-new\n-added\n+old\n+sneaked in")
        await h.channel.process(ReviewCandidate(kind="merge_request", iid=42))
    assert "review-gitlab-mrs" in h.inbound[0].content


@pytest.mark.asyncio
async def test_revert_mixed_with_new_commits_is_reviewed(tmp_path: Path) -> None:
    async with Harness(tmp_path, review_timeout_s=0.05) as h:
        _revert_setup(h, "@@ -1,2 +1 @@\n-new\n-added\n+old")
        h.gitlab.commits.append("fix: new code")
        await h.channel.process(ReviewCandidate(kind="merge_request", iid=42))
    assert "review-gitlab-mrs" in h.inbound[0].content


@pytest.mark.asyncio
async def test_requested_review_runs_even_for_a_revert(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        h.gitlab.commits = ["Revert \"x\"\n\nThis reverts merge request !1"]
        await h.channel.handle_telegram(_tg(1, "Проверь !42"))
        await _until(lambda: len(h.inbound) == 1)
        assert "review-gitlab-mrs" in h.inbound[0].content
        assert "!42: запускаю ревью." in h.telegram.text()
        await h.reply_as_agent("gitlab-review:42", _answer("Ревёрт точный.", []))


async def _vpn_control(answers: dict[str, str]) -> tuple[asyncio.AbstractServer, str, list[str]]:
    seen: list[str] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = (await reader.readline()).decode().strip()
        seen.append(line)
        writer.write((answers.get(line.split()[0], "error: ?") + "\n").encode())
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, f"127.0.0.1:{server.sockets[0].getsockname()[1]}", seen


@pytest.mark.asyncio
async def test_vpn_waiting_for_a_code_asks_once_and_takes_the_reply(tmp_path: Path) -> None:
    server, endpoint, seen = await _vpn_control({"status": "needs_code", "code": "ok"})
    async with server:
        async with Harness(tmp_path, vpn_control=endpoint, vpn_check_interval_s=0.02) as h:
            await _until(lambda: "ждёт код подтверждения" in h.telegram.text())
            await asyncio.sleep(0.1)
            assert h.telegram.text().count("ждёт код подтверждения") == 1
            await h.channel.handle_telegram(_tg(1, "123456"))
    assert "code 123456" in seen
    assert "код принят, Jira доступна" in h.telegram.text()
    assert h.inbound == []


@pytest.mark.asyncio
async def test_rejected_vpn_code_asks_for_another(tmp_path: Path) -> None:
    server, endpoint, _ = await _vpn_control({"status": "ok", "code": "error: Kerio did not accept the code"})
    async with server:
        async with Harness(tmp_path, vpn_control=endpoint, vpn_check_interval_s=10) as h:
            await h.channel.handle_telegram(_tg(1, "654321"))
    assert "код не принят (Kerio did not accept the code). Пришлите новый." in h.telegram.text()


@pytest.mark.asyncio
async def test_six_digits_are_a_question_without_a_vpn(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        turn = await h.say(1, "123456")
        await h.reply_as_agent(turn.chat_id, "Это не MR.")
    assert "> 123456" in turn.content


@pytest.mark.asyncio
async def test_vpn_down_for_a_while_is_reconnected_on_its_own(tmp_path: Path) -> None:
    server, endpoint, seen = await _vpn_control({"status": "down", "reconnect": "ok"})
    async with server:
        async with Harness(tmp_path, vpn_control=endpoint, vpn_check_interval_s=0.02, vpn_reconnect_after_s=0.05) as h:
            await _until(lambda: "reconnect" in seen)
    assert "Kerio VPN: переподключаю (сам: VPN не работает уже несколько минут)." in h.telegram.text()


@pytest.mark.asyncio
async def test_human_can_ask_to_reconnect_the_vpn(tmp_path: Path) -> None:
    server, endpoint, seen = await _vpn_control({"status": "ok", "reconnect": "ok"})
    async with server:
        async with Harness(tmp_path, vpn_control=endpoint, vpn_check_interval_s=10) as h:
            await h.channel.handle_telegram(_tg(1, "Переподключи VPN"))
    assert "reconnect" in seen
    assert "Kerio VPN: переподключаю (по вашей просьбе)." in h.telegram.text()
    assert h.inbound == []


@pytest.mark.asyncio
async def test_approval_of_a_merged_mr_is_skipped_but_comments_go_out(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        await h.answer(42, [{"type": "note", "body": "general"}, {"type": "approve"}])
        h.gitlab.mr["state"] = "merged"
        await h.channel.handle_telegram(_tg(1, "публикуй !42"))
    assert h.gitlab.writes == [("note", "general")]
    assert "2. аппрув MR: пропущено — MR уже смёржен" in h.telegram.text()




@pytest.mark.asyncio
async def test_comment_on_a_line_gitlab_cannot_show_goes_out_as_a_general_note(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        await h.answer(42, [{"type": "discussion", "path": "bundle.js", "line": 275478, "body": "замечание"}])
        h.gitlab.reject_lines = True
        await h.channel.handle_telegram(_tg(1, "публикуй !42"))
    assert h.gitlab.writes == [("note", "`bundle.js:275478`\n\nзамечание")]
    assert "опубликовано общим комментарием" in h.telegram.text()


@pytest.mark.asyncio
async def test_failed_items_stay_in_a_new_draft_to_retry(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        await h.answer(42, [{"type": "note", "body": "one"}])
        h.gitlab.fail_writes = True
        await h.channel.handle_telegram(_tg(1, "публикуй !42"))
        assert "повторить: «публикуй !42/2»" in h.telegram.text()
        h.gitlab.fail_writes = False
        await h.channel.handle_telegram(_tg(2, "публикуй !42/2"))
    assert h.gitlab.writes == [("note", "one")]


@pytest.mark.asyncio
async def test_new_commits_do_not_hold_back_a_thread_reply(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        await h.answer(
            42,
            [
                {"type": "reply", "discussion_id": "d1", "body": "Потому что так короче"},
                {"type": "discussion", "path": "a.cs", "line": 3, "body": "inline"},
                {"type": "approve"},
            ],
        )
        h.gitlab.mr["sha"] = "b" * 40
        await h.channel.handle_telegram(_tg(1, "публикуй !42"))
    assert h.gitlab.writes == [("reply", ("d1", "Потому что так короче"))]
    assert "2. инлайн a.cs:3: пропущено — ветка изменилась после ревью" in h.telegram.text()
    assert "3. аппрув MR: пропущено — ветка изменилась после ревью" in h.telegram.text()


def _open_mr(iid: int, sha: str, **extra: Any) -> dict[str, Any]:
    return {"iid": iid, "sha": sha, "title": f"feat {iid}", "author": {"username": "author"}, "draft": False, **extra}


@pytest.mark.asyncio
async def test_catch_up_queues_only_mrs_without_a_finished_review(tmp_path: Path) -> None:
    async with Harness(tmp_path, catch_up_days=0, review_timeout_s=0.05) as h:
        await h.review(42, [])
        h.gitlab.open_mrs = [
            _open_mr(42, SHA),
            _open_mr(43, "c" * 40),
            _open_mr(44, "d" * 40, draft=True),
            _open_mr(45, "e" * 40, author={"username": "a.tyra"}),
        ]
        h.gitlab.mr["sha"] = "c" * 40
        before = len(h.inbound)
        await h.channel.handle_telegram(_tg(1, "проверь новые"))
        await _until(lambda: len(h.inbound) > before)
    assert "Нашёл MR без ревью текущей версии (за 3 дн.), ставлю в очередь:\n!43 feat 43" in h.telegram.text()
    assert "!42 feat 42" not in h.telegram.text() and "!44" not in h.telegram.text() and "!45" not in h.telegram.text()
    assert "Run the review-gitlab-mrs skill for merge request !43" in h.inbound[before].content


@pytest.mark.asyncio
async def test_interrupted_review_is_caught_up(tmp_path: Path) -> None:
    async with Harness(tmp_path, catch_up_days=0) as h:
        h.channel._state.start_review(
            iid=42, title="t", web_url="", task_keys=[], kind="review", session_id="s", head_sha=SHA,
        )
        assert not h.channel._state.reviewed_head(42, SHA)
        h.gitlab.open_mrs = [_open_mr(42, SHA)]
        await h.channel.catch_up(asked=True)
    assert "!42 feat 42" in h.telegram.text()


@pytest.mark.asyncio
async def test_nothing_new_is_said_only_when_asked(tmp_path: Path) -> None:
    async with Harness(tmp_path, catch_up_days=0) as h:
        await h.channel.catch_up()
        assert h.telegram.sent == []
        await h.channel.catch_up(asked=True)
    assert "Открытых MR без ревью за последние 3 дн. нет." in h.telegram.text()


@pytest.mark.asyncio
async def test_the_same_commit_is_not_reviewed_twice_from_webhooks(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        await h.review(42, [])
        before = len(h.inbound)
        await h.channel.process(ReviewCandidate(kind="merge_request", iid=42))
    assert len(h.inbound) == before


@pytest.mark.asyncio
async def test_attachments_go_to_a_readable_folder_per_mr(tmp_path: Path) -> None:
    async with Harness(tmp_path, attachments_dir="/home/u/review-attachments/") as h:
        review = await h.review(42, [])
        turn = await h.say(1, "посмотри скриншот в задаче")
        await h.reply_as_agent(turn.chat_id, "Посмотрел.")
    for prompt in (review.content, turn.content):
        assert "download them into /home/u/review-attachments/42" in prompt
        assert "Never open any other file in its place" in prompt


@pytest.mark.asyncio
async def test_without_an_attachments_folder_nothing_is_said(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        review = await h.review(42, [])
    assert "Attachments (screenshots" not in review.content

