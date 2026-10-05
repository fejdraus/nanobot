"""GitLab review channel.

Flow:

1. A GitLab webhook arrives; :mod:`events` drops anything that can never
   concern the reviewer.
2. Activity is debounced per thread (or per MR) and queued. A single worker
   runs one review at a time, so two reviews never share a repository clone.
3. Before a run, live GitLab state is checked in code: the MR is open, not a
   draft, not authored by the reviewer unless ``reviewOwnMergeRequests`` is on;
   a note passes :mod:`triage`; the MR is within its hourly run budget. An
   approval of the reviewer's own MR is never drafted nor published.
4. The agent reviews in draft mode and answers with proposed actions.
5. The draft goes to one Telegram chat. Only «публикуй !N/V» from its approver
   makes the channel post to GitLab, and only if the branch has not moved since
   the review. A review that runs out of time is stopped, and an answer that
   arrives after that is not turned into a draft.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.channels.gitlab_review.config import GitLabReviewConfig
from nanobot.channels.gitlab_review.events import ReviewCandidate, is_draft, parse_event
from nanobot.channels.gitlab_review.gitlab_api import GitLabApi, GitLabApiError
from nanobot.channels.gitlab_review.prompts import reply_prompt, review_prompt
from nanobot.channels.gitlab_review.proposals import (
    ApprovalCommand,
    ProposedAction,
    ReviewDraft,
    parse_command,
    parse_draft,
    render_draft,
    render_notice,
)
from nanobot.channels.gitlab_review.state import GitLabReviewStateStore
from nanobot.channels.gitlab_review.telegram_api import TelegramApi, TelegramMessage
from nanobot.channels.gitlab_review.triage import triage_thread
from nanobot.config.paths import get_runtime_subdir
from nanobot.events import ResponseSourceEvent

MAX_WEBHOOK_BYTES = 1024 * 1024
TELEGRAM_OFFSET_KEY = "telegram_offset"
POLL_RETRY_S = 5.0
SOCKET_TIMEOUT_S = 30.0
STOP_COMMAND = "/stop"


class GitLabWebhookError(ValueError):
    """A webhook that must be rejected with a specific status code."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class _ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


@dataclass
class _PendingRun:
    future: asyncio.Future[None]
    head_sha: str
    title: str
    web_url: str
    own: bool = False


@dataclass
class _MergeRequestInfo:
    iid: int
    title: str
    web_url: str
    head_sha: str
    author: str | None
    open: bool
    draft: bool
    diff_refs: dict[str, Any] = field(default_factory=dict)


class GitLabReviewChannel(BaseChannel):
    """Draft reviews of merge requests and publish them on approval."""

    name = "gitlab_review"
    display_name = "GitLab Review"

    def __init__(
        self,
        config: Any,
        bus: MessageBus,
        *,
        state_path: Path | None = None,
        gitlab_api: GitLabApi | None = None,
        telegram_api: TelegramApi | None = None,
    ) -> None:
        if isinstance(config, dict):
            config = GitLabReviewConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: GitLabReviewConfig = config
        self._state = GitLabReviewStateStore(
            state_path or get_runtime_subdir("gitlab_review") / "state.sqlite3"
        )
        self._gitlab = gitlab_api
        self._telegram = telegram_api
        self._server: _ReusableThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[ReviewCandidate] | None = None
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._pending: dict[int, _PendingRun] = {}

    async def start(self) -> None:
        self.config.validate_runtime()
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        if self._gitlab is None:
            self._gitlab = GitLabApi(
                self.config.gitlab_url, self.config.gitlab_token, self.config.project_path
            )
        if self._telegram is None:
            self._telegram = TelegramApi(self.config.telegram_bot_token)
        try:
            self._server = _ReusableThreadingHTTPServer(
                (self.config.host, self.config.port), self._make_handler()
            )
        except OSError:
            await self._close_clients()
            raise
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="nanobot-gitlab-review", daemon=True
        )
        self._thread.start()
        self._tasks = [
            asyncio.create_task(self._worker(), name="gitlab-review-worker"),
            asyncio.create_task(self._poll_telegram(), name="gitlab-review-telegram"),
        ]
        self._running = True
        self.logger.info(
            "GitLab review webhook listening on {}:{}{} for {}",
            self.config.host,
            self.config.port,
            self.config.webhook_path,
            self.config.project_path,
        )

    async def stop(self) -> None:
        self._running = False
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for pending in self._pending.values():
            if not pending.future.done():
                pending.future.cancel()
        self._pending.clear()
        server, self._server = self._server, None
        if server is not None:
            await asyncio.to_thread(server.shutdown)
            server.server_close()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            await asyncio.to_thread(thread.join, 2.0)
        await self._close_clients()
        self._loop = None

    async def _close_clients(self) -> None:
        for client in (self._gitlab, self._telegram):
            if client is not None:
                await client.aclose()

    async def send(self, msg: OutboundMessage) -> None:
        """Receive the agent's answer for one MR and forward it as a draft."""
        if msg.event is not None and not isinstance(msg.event, ResponseSourceEvent):
            return
        iid = self.config.iid_from_chat_id(msg.chat_id)
        if iid is None or not msg.content.strip():
            return
        pending = self._pending.get(iid)
        if pending is None:
            self.logger.warning("MR !{}: answer arrived with no review waiting, dropped", iid)
            await self._tell_safe(
                render_notice(
                    iid, "", "Ответ ревью пришёл после таймаута; черновик не сохранён."
                )
            )
            return
        try:
            await self._deliver_draft(iid, msg.content, pending)
        finally:
            if not pending.future.done():
                pending.future.set_result(None)

    async def _deliver_draft(self, iid: int, answer: str, pending: _PendingRun) -> None:
        draft = parse_draft(answer)
        if pending.own and any(action.type == "approve" for action in draft.actions):
            draft = ReviewDraft(
                summary=draft.summary,
                actions=tuple(action for action in draft.actions if action.type != "approve"),
                errors=(*draft.errors, "аппрув своего MR снят"),
            )
        version = self._state.next_draft_version(iid)
        if draft.actions:
            self._state.save_draft(iid, pending.head_sha, draft.actions, version)
        else:
            self._state.delete_draft(iid)
        await self._tell(
            render_draft(iid, pending.title, draft, web_url=pending.web_url, version=version)
        )

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        channel = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            timeout = SOCKET_TIMEOUT_S

            def do_POST(self) -> None:  # noqa: N802
                try:
                    status, body = channel.handle_webhook(self)
                except GitLabWebhookError as exc:
                    status, body = exc.status, str(exc).encode()
                except Exception:  # pragma: no cover - defensive
                    channel.logger.exception("GitLab webhook handling failed")
                    status, body = 500, b"internal error"
                self._send(status, body)

            def log_message(self, format: str, *args: Any) -> None:
                return

            def _send(self, status: int, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

        return Handler

    def handle_webhook(self, handler: BaseHTTPRequestHandler) -> tuple[int, bytes]:
        """Validate one delivery and schedule the work it asks for."""
        if handler.path.rstrip("/") != self.config.webhook_path.rstrip("/"):
            raise GitLabWebhookError("not found", status=404)

        raw = self._read_body(handler)
        self._verify_token(handler)
        payload = self._decode(raw)

        candidate = parse_event(
            handler.headers.get("X-Gitlab-Event"),
            payload,
            project_path=self.config.project_path,
            is_reviewer=self.config.is_reviewer,
        )
        if candidate is None:
            return 200, b'{"ok":true,"ignored":true}'

        delivery_key = self._delivery_key(handler, raw)
        if not self._state.claim(delivery_key, candidate.iid):
            return 200, b'{"ok":true,"duplicate":true}'

        loop = self._loop
        if loop is None or not self._running:
            self._state.release(delivery_key)
            raise GitLabWebhookError("channel is not running", status=503)

        loop.call_soon_threadsafe(self._schedule, candidate)
        return 200, b'{"ok":true}'

    def _schedule(self, candidate: ReviewCandidate) -> None:
        """Restart the quiet-period timer for this thread or MR."""
        key = candidate.debounce_key
        previous = self._timers.pop(key, None)
        if previous is not None:
            previous.cancel()
        if self._loop is None:
            return
        self._timers[key] = self._loop.call_later(
            self.config.debounce_seconds, self._enqueue, key, candidate
        )

    def _enqueue(self, key: str, candidate: ReviewCandidate) -> None:
        self._timers.pop(key, None)
        if self._queue is not None:
            self._queue.put_nowait(candidate)

    async def _worker(self) -> None:
        assert self._queue is not None
        while True:
            candidate = await self._queue.get()
            try:
                await self.process(candidate)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.exception("review of MR !{} failed", candidate.iid)
                await self._tell_safe(
                    render_notice(candidate.iid, "", f"Ревью не выполнено: {exc}")
                )
            finally:
                self._queue.task_done()

    async def process(self, candidate: ReviewCandidate) -> None:
        """Apply the code guards and, if they pass, run one review."""
        info = await self._merge_request(candidate.iid)
        if not info.open or info.draft:
            return

        own = self.config.is_reviewer(info.author)
        if candidate.kind == "merge_request":
            if own and not self.config.review_own_merge_requests:
                return
            prompt = review_prompt(info.iid, own=own)
        else:
            assert self._gitlab is not None and candidate.discussion_id
            discussion = await self._gitlab.get_discussion(info.iid, candidate.discussion_id)
            notes = discussion.get("notes")
            decision = triage_thread(
                cast("list[dict[str, Any]]", notes if isinstance(notes, list) else []),
                mr_author=info.author,
                is_reviewer=self.config.is_reviewer,
            )
            if decision.action == "skip":
                self.logger.info("MR !{}: {}", info.iid, decision.reason)
                return
            if decision.action in ("notify", "ask"):
                text = (
                    f"{decision.reason}.\nТред {candidate.discussion_id}, "
                    f"{decision.note_author}:\n{decision.note_body}"
                )
                await self._tell(render_notice(info.iid, info.title, text))
                return
            prompt = reply_prompt(
                info.iid, candidate.discussion_id, decision.note_author, decision.note_body
            )

        if not self._state.try_start_run(info.iid, self.config.max_runs_per_mr_per_hour):
            await self._tell(
                render_notice(
                    info.iid,
                    info.title,
                    "Лимит запусков ревью на этот MR за час исчерпан, событие пропущено.",
                )
            )
            return
        await self._run_agent(info, prompt, own=own)

    async def _run_agent(self, info: _MergeRequestInfo, prompt: str, *, own: bool = False) -> None:
        assert self._loop is not None
        future: asyncio.Future[None] = self._loop.create_future()
        self._pending[info.iid] = _PendingRun(
            future, info.head_sha, info.title, info.web_url, own=own
        )
        try:
            await self.bus.publish_inbound(
                InboundMessage(
                    channel=self.name,
                    sender_id=f"gitlab-mr-{info.iid}",
                    chat_id=self.config.review_chat_id(info.iid),
                    content=prompt,
                    timestamp=datetime.now(),
                    metadata={"gitlab": {"iid": info.iid, "project": self.config.project_path}},
                )
            )
            try:
                await asyncio.wait_for(asyncio.shield(future), self.config.review_timeout_s)
            except TimeoutError:
                await self._stop_turn(info.iid)
                await self._tell(
                    render_notice(
                        info.iid, info.title, "Ревью не завершилось за отведённое время и остановлено."
                    )
                )
        finally:
            self._pending.pop(info.iid, None)

    async def _stop_turn(self, iid: int) -> None:
        """Cancel the agent turn of this MR so it cannot run alongside the next one."""
        await self.bus.publish_inbound(
            InboundMessage(
                channel=self.name,
                sender_id=f"gitlab-mr-{iid}",
                chat_id=self.config.review_chat_id(iid),
                content=STOP_COMMAND,
                timestamp=datetime.now(),
            )
        )

    async def _poll_telegram(self) -> None:
        assert self._telegram is not None
        stored = self._state.get_value(TELEGRAM_OFFSET_KEY)
        offset = int(stored) if stored and stored.isdigit() else None
        while True:
            try:
                messages = await self._telegram.get_updates(offset)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("Telegram polling failed")
                await asyncio.sleep(POLL_RETRY_S)
                continue
            for message in messages:
                offset = message.update_id + 1
                self._state.set_value(TELEGRAM_OFFSET_KEY, str(offset))
                try:
                    await self.handle_telegram(message)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.logger.exception("Telegram command failed: {!r}", message.text[:80])
                    await self._tell_safe([f"Команда не выполнена: {exc}"])

    async def handle_telegram(self, message: TelegramMessage) -> None:
        """Act on a message from the approver; ignore every other chat and sender."""
        if not self.config.is_approver(message.chat_id, message.sender_id):
            return
        command = parse_command(message.text)
        if command is None:
            await self._tell(
                ["Команды: «публикуй !N/V», «публикуй !N/V 1,3», «отмена !N»."]
            )
            return
        if not command.publish:
            self._state.delete_draft(command.iid)
            await self._tell([f"!{command.iid}: черновик отменён, ничего не опубликовано."])
            return
        await self.publish(command)

    async def publish(self, command: ApprovalCommand) -> None:
        """Publish approved actions, refusing if the branch moved since the review."""
        stored = self._state.load_draft(command.iid)
        if stored is None:
            await self._tell([f"!{command.iid}: нет черновика для публикации."])
            return
        current = f"!{command.iid}/{stored.version}"
        if command.version is not None and command.version != stored.version:
            await self._tell([
                f"!{command.iid}: черновик {command.iid}/{command.version} уже заменён, "
                f"актуальный — {current}. Ничего не опубликовано."
            ])
            return
        if command.version is None and stored.replaced:
            await self._tell([
                f"!{command.iid}: черновик обновлялся. Укажите версию: «публикуй {current}»."
            ])
            return
        reviewed_sha, actions = stored.head_sha, stored.actions
        selected = self._select(actions, command.items)
        if isinstance(selected, str):
            await self._tell([f"!{command.iid}: {selected}"])
            return

        info = await self._merge_request(command.iid)
        if info.head_sha != reviewed_sha:
            self._state.delete_draft(command.iid)
            await self._tell([
                f"!{command.iid}: ветка изменилась после ревью "
                f"({reviewed_sha[:10]} → {info.head_sha[:10]}). "
                "Черновик снят, ничего не опубликовано."
            ])
            return

        report: list[str] = []
        for number, action in selected:
            if action.type == "approve" and self.config.is_reviewer(info.author):
                report.append(f"{number}. {action.label()}: пропущено — это ваш MR")
                continue
            try:
                await self._publish_action(info, action)
            except GitLabApiError as exc:
                report.append(f"{number}. {action.label()}: ошибка — {exc}")
            else:
                report.append(f"{number}. {action.label()}: опубликовано")
        self._state.delete_draft(command.iid)
        if len(selected) < len(actions):
            report.append("Остальные пункты черновика сняты.")
        await self._tell([f"!{command.iid}:\n" + "\n".join(report)])

    @staticmethod
    def _select(
        actions: tuple[ProposedAction, ...], items: tuple[int, ...]
    ) -> list[tuple[int, ProposedAction]] | str:
        numbered = list(enumerate(actions, start=1))
        if not items:
            return numbered
        wrong = [item for item in items if item < 1 or item > len(actions)]
        if wrong:
            return f"нет пунктов {', '.join(map(str, wrong))}; в черновике {len(actions)}."
        wanted = set(items)
        return [(number, action) for number, action in numbered if number in wanted]

    async def _publish_action(self, info: _MergeRequestInfo, action: ProposedAction) -> None:
        assert self._gitlab is not None
        if action.type == "discussion":
            refs = info.diff_refs
            position: dict[str, Any] = {
                "base_sha": refs.get("base_sha"),
                "start_sha": refs.get("start_sha"),
                "head_sha": refs.get("head_sha"),
                "new_path": action.path,
                "old_path": action.path,
            }
            if action.line is not None:
                position["new_line"] = action.line
            if action.old_line is not None:
                position["old_line"] = action.old_line
            await self._gitlab.create_discussion(info.iid, action.body, position)
        elif action.type == "reply":
            assert action.discussion_id
            await self._gitlab.reply(info.iid, action.discussion_id, action.body)
        elif action.type == "approve":
            await self._gitlab.approve(info.iid, info.head_sha)
        else:
            await self._gitlab.create_note(info.iid, action.body)

    async def _merge_request(self, iid: int) -> _MergeRequestInfo:
        assert self._gitlab is not None
        mr = await self._gitlab.get_merge_request(iid)
        author = mr.get("author")
        username: object = (
            cast("dict[str, Any]", author).get("username") if isinstance(author, dict) else None
        )
        refs = mr.get("diff_refs")
        return _MergeRequestInfo(
            iid=iid,
            title=str(mr.get("title") or ""),
            web_url=str(mr.get("web_url") or ""),
            head_sha=str(mr.get("sha") or ""),
            author=username if isinstance(username, str) else None,
            open=mr.get("state") == "opened",
            draft=is_draft(mr),
            diff_refs=cast("dict[str, Any]", refs) if isinstance(refs, dict) else {},
        )

    async def _tell(self, chunks: list[str]) -> None:
        assert self._telegram is not None
        for chunk in chunks:
            await self._telegram.send_message(str(self.config.telegram_chat_id), chunk)

    async def _tell_safe(self, chunks: list[str]) -> None:
        try:
            await self._tell(chunks)
        except Exception:
            self.logger.exception("Telegram notification failed")

    def _read_body(self, handler: BaseHTTPRequestHandler) -> bytes:
        raw_length = handler.headers.get("Content-Length")
        try:
            length = int(raw_length or "")
        except ValueError as exc:
            raise GitLabWebhookError("invalid content length") from exc
        if length <= 0:
            raise GitLabWebhookError("invalid content length")
        if length > MAX_WEBHOOK_BYTES:
            raise GitLabWebhookError("payload too large", status=413)
        raw = handler.rfile.read(length)
        if not raw:
            raise GitLabWebhookError("empty payload")
        return raw

    def _verify_token(self, handler: BaseHTTPRequestHandler) -> None:
        presented = handler.headers.get("X-Gitlab-Token", "")
        expected = self.config.webhook_secret_token
        if not presented or not hmac.compare_digest(presented, expected):
            raise GitLabWebhookError("invalid token", status=401)

    def _decode(self, raw: bytes) -> dict[str, Any]:
        try:
            value: object = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GitLabWebhookError("invalid JSON") from exc
        if not isinstance(value, dict):
            raise GitLabWebhookError("webhook body must be an object")
        return cast("dict[str, Any]", value)

    def _delivery_key(self, handler: BaseHTTPRequestHandler, raw: bytes) -> str:
        event_uuid = handler.headers.get("X-Gitlab-Event-UUID", "").strip()
        if event_uuid:
            return event_uuid
        return "sha256:" + hashlib.sha256(raw).hexdigest()


__all__ = ["GitLabReviewChannel", "GitLabWebhookError"]

