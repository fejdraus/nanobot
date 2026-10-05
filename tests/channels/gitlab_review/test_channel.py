import asyncio
import json
import socket
import urllib.error
import urllib.request
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
            "source_branch": "AMCRM-16127",
            "description": "Closes AMCRM-16127",
            "diff_refs": {"base_sha": "b", "start_sha": "s", "head_sha": SHA},
        }
        self.discussions: dict[str, list[dict[str, Any]]] = {}
        self.writes: list[tuple[str, Any]] = []
        self.fail_writes = False
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
        return self.changes

    async def get_discussion(self, iid: int, discussion_id: str) -> dict[str, Any]:
        return {"id": discussion_id, "notes": self.discussions.get(discussion_id, [])}

    async def _write(self, kind: str, data: Any) -> dict[str, Any]:
        if self.fail_writes:
            raise GitLabApiError("boom", 500)
        self.writes.append((kind, data))
        return {}

    async def create_discussion(self, iid: int, body: str, position: dict[str, Any]) -> dict[str, Any]:
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
        if iid not in self.channel._pending:
            future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            self.channel._pending[iid] = _PendingRun(
                future, str(self.gitlab.mr["sha"]), str(self.gitlab.mr["title"]), ""
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
        self.channel._pending.pop(iid, None)


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
        assert "Режим черновика" in h.inbound[0].content

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
    assert "самого ревьюера" in prompt


@pytest.mark.asyncio
async def test_approval_of_own_mr_is_never_drafted_or_published(tmp_path: Path) -> None:
    async with Harness(tmp_path, review_own_merge_requests=True) as h:
        h.gitlab.mr["author"] = {"username": "a.tyra"}
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        h.channel._pending[42] = _PendingRun(future, SHA, "feat: thing", "", own=True)
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
    assert "Уроки из памяти" not in h.inbound[0].content


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
async def test_short_command_without_reply_gets_a_hint(tmp_path: Path) -> None:
    async with Harness(tmp_path) as h:
        await h.answer(42, [{"type": "note", "body": "general"}])
        await h.channel.handle_telegram(_tg(1, "публикуй"))
    assert h.gitlab.writes == []
    assert "Ответьте на сообщение черновика" in h.telegram.text()


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
    assert h.tasks.asked == ["AMCRM-16127"]

