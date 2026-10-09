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
5. The draft goes to one Telegram chat. Only an explicit «публикуй» from its
   approver makes the channel post to GitLab — as a reply to the draft, with
   its number, or bare when exactly one draft is waiting — and only if the
   branch has not moved since the review. A review that runs out of time is
   stopped, and an answer that arrives after that is not turned into a draft.
6. Any other message from the approver is a conversation. It continues the
   Claude session of the review it is about (the one replied to, or the only
   waiting draft), so the agent remembers what it read; every new review
   starts a new session. The agent may answer, revise the draft, or ask for a
   publication or a cancellation, which the channel then performs on the draft
   the human was shown, if the branch has not moved. Every review, draft, decision and message is kept in an archive the
   agent can consult when asked about an earlier review.
9. After a restart, and on «проверь новые», open MRs updated within
   ``catchUpDays`` that have no finished review of their current commit are
   queued, so webhooks lost while the reviewer was down, or reviews cut short
   by a restart, are not lost for good.
8. With ``vpnControl`` set, the channel watches the VPN container that gives
   access to Jira (:mod:`vpn`): it asks in Telegram for the authenticator code
   when the VPN waits for one and passes on the six digits sent back.
7. The agent may also note what it learned about the developers involved. The
   notes are kept as evidence; once a day each developer's profile is
   consolidated from it and from their own messages in the reviewer's threads
   (:mod:`people`), and the profile comes back in the next prompt about them.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.channels.gitlab_review.config import GitLabReviewConfig
from nanobot.channels.gitlab_review.events import ReviewCandidate, is_draft, parse_event
from nanobot.channels.gitlab_review.gitlab_api import GitLabApi, GitLabApiError
from nanobot.channels.gitlab_review.lessons import load_lessons, render_lessons, select_lessons
from nanobot.channels.gitlab_review.people import (
    PeopleStore,
    PersonNote,
    accept_notes,
    extract_people,
    mentioned_usernames,
    parse_profile,
)
from nanobot.channels.gitlab_review.prompts import (
    RUSSIAN_RETRY,
    chat_prompt,
    dream_prompt,
    reply_prompt,
    review_prompt,
)
from nanobot.channels.gitlab_review.proposals import (
    ApprovalCommand,
    ChatAnswer,
    ProposedAction,
    ReviewDraft,
    parse_bare_command,
    parse_chat_answer,
    parse_command,
    parse_draft,
    parse_reply_command,
    render_chat,
    render_draft,
    render_notice,
    written_in_english,
)
from nanobot.channels.gitlab_review.reverts import (
    Net,
    is_exact_revert,
    label,
    net_from_changes,
    net_from_raw,
    reverted_refs,
)
from nanobot.channels.gitlab_review.state import GitLabReviewStateStore, ReviewRecord
from nanobot.channels.gitlab_review.tasks import TaskLookup, find_task_keys
from nanobot.channels.gitlab_review.telegram_api import TelegramApi, TelegramMessage
from nanobot.channels.gitlab_review.triage import triage_thread
from nanobot.channels.gitlab_review.vpn import asks_reconnect, verification_code, vpn_request
from nanobot.config.paths import get_runtime_subdir
from nanobot.events import ResponseSourceEvent
from nanobot.providers.claude_cli_provider import SESSION_METADATA_KEY

MAX_WEBHOOK_BYTES = 1024 * 1024
TELEGRAM_OFFSET_KEY = "telegram_offset"
POLL_RETRY_S = 5.0
SOCKET_TIMEOUT_S = 30.0
STOP_COMMAND = "/stop"
MAX_TASKS = 5
ARCHIVE_LIMIT = 5
ARCHIVE_ENTRY_CHARS = 6000
TYPING_INTERVAL_S = 4.0
_MR_REF_RE = re.compile(r"!(\d+)")
_CATCH_UP_RE = re.compile(r"^\s*(?:проверь|проверить|check)\s+(?:новые|все|new)(?:\s+(?:mr|мр|ревью))?\s*$", re.IGNORECASE)
_REVIEW_REQUEST_RE = re.compile(r"^\s*(?:проверь|отревьюй|ревью|review)\s+!?(\d+)\s*$", re.IGNORECASE)
CONTINUE_WINDOW = timedelta(hours=12)
_TIED_TO_CODE = frozenset({"discussion", "approve"})
DREAM_CURSOR_KEY = "people_dream_cursor"
DREAM_MR_LIMIT = 30
DREAM_MESSAGES = 15
DREAM_MESSAGE_CHARS = 500


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
    task: str = ""
    kind: str = "review"
    review_id: int | None = None
    iid: int | None = None
    user_text: str = ""
    authors: tuple[str, ...] = ()
    session: str = ""
    retried: bool = False


@dataclass(frozen=True)
class _CatchUpRequest:
    """Look for open MRs whose current commit has not been reviewed."""

    asked: bool = False


@dataclass(frozen=True)
class _DreamRequest:
    """Time to consolidate the developer profiles."""


@dataclass(frozen=True)
class _ChatRequest:
    """A message from the approver that is not a command."""

    text: str
    reply_to: int | None = None


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
    description: str = ""
    state: str = "opened"

    @property
    def state_label(self) -> str:
        return {"merged": "смёржен", "closed": "закрыт", "locked": "заблокирован"}.get(self.state, self.state)


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
        task_lookup: TaskLookup | None = None,
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
        self._task_lookup = task_lookup
        people_path = self.config.people_path()
        self._people = PeopleStore(Path(people_path).expanduser()) if people_path else None
        self._server: _ReusableThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[ReviewCandidate | _ChatRequest | _DreamRequest | _CatchUpRequest] | None = None
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._pending: dict[str, _PendingRun] = {}
        self._dream_report: list[str] = []
        self._vpn_reminded: float | None = None
        self._vpn_down_since: float | None = None

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
        if self._task_lookup is None:
            self._task_lookup = TaskLookup(
                clickup_token=self.config.clickup_token,
                clickup_team_id=self.config.clickup_team_id,
                jira_url=self.config.jira_url,
            )
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
        if self._people is not None:
            self._tasks.append(asyncio.create_task(self._dream_clock(), name="gitlab-review-dream"))
        if self.config.vpn_control:
            self._tasks.append(asyncio.create_task(self._watch_vpn(), name="gitlab-review-vpn"))
        if self.config.catch_up_days > 0:
            self._queue.put_nowait(_CatchUpRequest())
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
        for client in (self._gitlab, self._telegram, self._task_lookup):
            if client is not None:
                await client.aclose()

    async def send(self, msg: OutboundMessage) -> None:
        """Receive the agent's answer and forward it as a draft or a conversation reply."""
        if msg.event is not None and not isinstance(msg.event, ResponseSourceEvent):
            return
        if not msg.content.strip():
            return
        pending = self._pending.get(msg.chat_id)
        notes, content = extract_people(msg.content)
        if pending is None:
            iid = self.config.iid_from_chat_id(msg.chat_id)
            if iid is None:
                return
            self.logger.warning("MR !{}: answer arrived with no review waiting, dropped", iid)
            await self._tell_safe(
                render_notice(
                    iid, "", "Ответ ревью пришёл после таймаута; черновик не сохранён."
                )
            )
            return
        if pending.kind != "dream" and not pending.retried and written_in_english(
            parse_draft(content).summary
        ):
            pending.retried = True
            self.logger.info("{}: answer came in English, asking for it in Russian", msg.chat_id)
            await self.bus.publish_inbound(
                InboundMessage(
                    channel=self.name,
                    sender_id="gitlab-review",
                    chat_id=msg.chat_id,
                    content=RUSSIAN_RETRY,
                    timestamp=datetime.now(),
                    metadata={SESSION_METADATA_KEY: {"session_id": pending.session, "resume": True}},
                )
            )
            return
        try:
            if pending.kind == "dream":
                await self._deliver_profile(msg.content, pending)
            elif pending.kind == "chat":
                await self._deliver_chat(content, pending)
            else:
                iid = pending.iid if pending.iid is not None else self.config.iid_from_chat_id(msg.chat_id)
                assert iid is not None
                await self._deliver_draft(iid, content, pending)
            await self._file_people(notes, pending)
        finally:
            if not pending.future.done():
                pending.future.set_result(None)

    async def _file_people(self, notes: list[PersonNote], pending: _PendingRun) -> None:
        """File what the agent noted about the developers of this work, after the guards."""
        if self._people is None or not notes:
            return
        kept, refused = accept_notes(notes, pending.authors)
        if refused:
            self.logger.warning("people notes refused: {}", "; ".join(refused))
        if not kept:
            return
        for note in kept:
            self._state.add_evidence(note.username, pending.iid, note.text)
        self.logger.info("people: {} notes kept as evidence", len(kept))
        if pending.review_id is not None:
            self._state.add_event(
                pending.review_id,
                "system",
                "Evidence for profiles:\n" + "\n".join(f"- {note.username}: {note.text}" for note in kept),
            )

    async def catch_up(self, *, asked: bool = False) -> None:
        """Queue reviews for open MRs whose current commit has no finished review."""
        assert self._gitlab is not None and self._queue is not None
        days = self.config.catch_up_days or 3
        since = (datetime.now().astimezone() - timedelta(days=days)).isoformat(timespec="seconds")
        found: list[str] = []
        for mr in await self._gitlab.list_open_merge_requests(since):
            iid, sha = mr.get("iid"), str(mr.get("sha") or "")
            if not isinstance(iid, int) or not sha or is_draft(mr):
                continue
            author = mr.get("author")
            username = cast("dict[str, Any]", author).get("username") if isinstance(author, dict) else None
            own = self.config.is_reviewer(username if isinstance(username, str) else None)
            if own and not self.config.review_own_merge_requests:
                continue
            if self._state.reviewed_head(iid, sha):
                continue
            self._queue.put_nowait(ReviewCandidate(kind="merge_request", iid=iid))
            found.append(f"!{iid} {str(mr.get('title') or '')[:70]}".rstrip())
        if found:
            lead = "Нашёл MR без ревью текущей версии" if asked else "После перезапуска нашёл MR без ревью"
            await self._tell_safe([f"{lead} (за {days} дн.), ставлю в очередь:\n" + "\n".join(found)])
        elif asked:
            await self._tell_safe([f"Открытых MR без ревью за последние {days} дн. нет."])

    async def _watch_vpn(self) -> None:
        """Ask for the authenticator code whenever the Jira VPN waits for one."""
        assert self._loop is not None
        while True:
            status = await vpn_request(self.config.vpn_control, "status")
            now = self._loop.time()
            if status == "down":
                self._vpn_down_since = self._vpn_down_since or now
                if now - self._vpn_down_since >= self.config.vpn_reconnect_after_s:
                    self._vpn_down_since = now
                    await self._reconnect_vpn("сам: VPN не работает уже несколько минут")
            else:
                self._vpn_down_since = None
            due = self._vpn_reminded is None or now - self._vpn_reminded >= self.config.vpn_reminder_interval_s
            if status == "ok":
                self._vpn_reminded = None
            elif due:
                self._vpn_reminded = now
                if status == "needs_code":
                    await self._tell_safe([
                        "Kerio VPN ждёт код подтверждения — без него Jira недоступна. "
                        "Пришлите 6 цифр из приложения-аутентификатора."
                    ])
                else:
                    await self._tell_safe(["Kerio VPN не подключён — Jira недоступна."])
            await asyncio.sleep(self.config.vpn_check_interval_s)

    async def _reconnect_vpn(self, why: str) -> None:
        answer = await vpn_request(self.config.vpn_control, "reconnect")
        if answer == "ok":
            await self._tell_safe([f"Kerio VPN: переподключаю ({why})."])
        else:
            await self._tell_safe([f"Kerio VPN не отвечает, переподключить не удалось ({why})."])

    async def _submit_vpn_code(self, code: str) -> None:
        answer = await vpn_request(self.config.vpn_control, f"code {code}")
        if answer == "ok":
            self._vpn_reminded = None
            await self._tell(["Kerio VPN: код принят, Jira доступна."])
        elif answer == "down":
            await self._tell(["Kerio VPN не отвечает — код не передан."])
        else:
            await self._tell([f"Kerio VPN: код не принят ({answer.removeprefix('error: ')}). Пришлите новый."])

    async def _dream_clock(self) -> None:
        """Queue the profile consolidation daily at ``peopleDreamHour``, and once at first start."""
        assert self._queue is not None
        if self._state.get_value(DREAM_CURSOR_KEY) is None:
            self._queue.put_nowait(_DreamRequest())
        while True:
            now = datetime.now()
            due = now.replace(hour=self.config.people_dream_hour, minute=0, second=0, microsecond=0)
            if due <= now:
                due += timedelta(days=1)
            await asyncio.sleep((due - now).total_seconds())
            self._queue.put_nowait(_DreamRequest())

    async def dream(self) -> None:
        """Consolidate the profile of every developer with something new since the last time."""
        if self._people is None:
            return
        assert self._loop is not None
        now = datetime.now()
        stored = self._state.get_value(DREAM_CURSOR_KEY)
        history = now - timedelta(days=self.config.people_history_days)
        since = datetime.fromisoformat(stored) if stored else history
        messages = await self._thread_messages(history)
        fresh = {
            name
            for name, items in messages.items()
            if any(item[0] >= since.isoformat(timespec="seconds") for item in items)
        }
        candidates = sorted(
            {*self._state.evidence_users(since), *fresh},
            key=str.casefold,
        )
        self._dream_report = []
        for username in candidates:
            if not self.config.may_profile(username):
                continue
            evidence = [
                f"- {day} !{iid}: {text}" if iid is not None else f"- {day}: {text}"
                for day, iid, text in self._state.evidence_for(username, history)
            ]
            own = [
                f"- {at[:10]} !{iid}: {body}"
                for at, iid, body in messages.get(username, [])[-DREAM_MESSAGES:]
            ]
            if not evidence and not own:
                continue
            prompt = dream_prompt(
                username,
                profile=self._people.read(username),
                evidence=evidence,
                messages=own,
                max_chars=self.config.people_profile_chars,
            )
            pending = _PendingRun(
                self._loop.create_future(), "", "", "", kind="dream", user_text=username
            )
            finished = await self._ask_agent(
                f"{self.config.chat_id}:dream-{username.casefold()}",
                prompt,
                pending,
                session=str(uuid.uuid4()),
            )
            if not finished:
                self._dream_report.append(f"{username}: не уложился во время, профиль прежний")
        self._state.set_value(DREAM_CURSOR_KEY, now.isoformat(timespec="seconds"))
        if self._dream_report:
            await self._tell_safe(["Профили разработчиков, ночная сводка:\n\n" + "\n\n".join(self._dream_report)])

    async def _thread_messages(self, since: datetime) -> dict[str, list[tuple[str, int, str]]]:
        """What each developer wrote in the reviewer's threads of recently reviewed MRs."""
        assert self._gitlab is not None
        found: dict[str, list[tuple[str, int, str]]] = {}
        for iid in self._state.reviewed_since(since, DREAM_MR_LIMIT):
            try:
                discussions = await self._gitlab.list_discussions(iid)
            except GitLabApiError as exc:
                self.logger.warning("MR !{}: threads unavailable for profiles: {}", iid, exc)
                continue
            for discussion in discussions:
                raw_notes = discussion.get("notes")
                notes = cast("list[dict[str, Any]]", raw_notes if isinstance(raw_notes, list) else [])
                if not notes or not self.config.is_reviewer(_note_author(notes[0])):
                    continue
                for note in notes[1:]:
                    author = _note_author(note)
                    if not author or note.get("system") or self.config.is_reviewer(author):
                        continue
                    body = " ".join(str(note.get("body") or "").split())[:DREAM_MESSAGE_CHARS]
                    found.setdefault(author, []).append((_local_time(note.get("created_at")), iid, body))
        for items in found.values():
            items.sort()
        return found

    async def _deliver_profile(self, answer: str, pending: _PendingRun) -> None:
        """Write a consolidated profile once it passes the guards; note what changed."""
        assert self._people is not None
        username = pending.user_text
        profile, refusal = parse_profile(answer, self.config.people_profile_chars)
        if profile is None:
            self.logger.warning("profile of {} refused: {}", username, refusal)
            self._dream_report.append(f"{username}: профиль не обновлён — {refusal}")
            return
        before = self._people.read(username)
        if before.strip() == profile.strip():
            return
        self._people.write(username, profile)
        old = {line for line in before.splitlines() if line.startswith("- ")}
        new = {line for line in profile.splitlines() if line.startswith("- ")}
        added = [f"+ {line[2:]}" for line in profile.splitlines() if line in new - old]
        removed = [f"− {line[2:]}" for line in before.splitlines() if line in old - new]
        if not added and not removed:
            return
        self._dream_report.append(
            f"{username}: +{len(added)} −{len(removed)}\n" + "\n".join([*added, *removed])
        )

    def _profiled(self, usernames: list[str]) -> list[str]:
        """The people of this work who may have a profile, without repeats."""
        if self._people is None:
            return []
        return list(dict.fromkeys(name for name in usernames if self.config.may_profile(name)))

    def _people_for(self, usernames: list[str]) -> str:
        """Profiles of *usernames* for a prompt; never blocks a run on failure."""
        if self._people is None or not usernames:
            return ""
        try:
            stats = {name: self._state.author_stats(name) for name in usernames}
            return self._people.render(usernames, self.config.people_budget_chars, stats)
        except Exception:
            self.logger.exception("people profiles unavailable, continuing without them")
            return ""

    async def _deliver_draft(self, iid: int, answer: str, pending: _PendingRun) -> None:
        draft = parse_draft(answer)
        if pending.own and any(action.type == "approve" for action in draft.actions):
            draft = ReviewDraft(
                summary=draft.summary,
                actions=tuple(action for action in draft.actions if action.type != "approve"),
                errors=(*draft.errors, "аппрув своего MR снят"),
            )
        await self._send_draft(iid, draft, pending)

    async def _send_draft(self, iid: int, draft: ReviewDraft, pending: _PendingRun) -> None:
        """Store a new draft version of this MR, show it, and file it in the archive."""
        version = self._state.next_draft_version(iid)
        if draft.actions:
            self._state.save_draft(iid, pending.head_sha, draft.actions, version)
        else:
            self._state.delete_draft(iid)
        chunks = render_draft(
            iid,
            pending.title,
            draft,
            web_url=pending.web_url,
            version=version,
            task=pending.task,
        )
        message_ids = await self._tell(chunks)
        if draft.actions and message_ids:
            self._state.record_draft_messages(iid, version, message_ids)
        if pending.review_id is not None:
            self._state.record_review_messages(pending.review_id, message_ids)
            self._state.add_event(pending.review_id, "reviewer", "\n\n".join(chunks))
            self._state.set_review_status(
                pending.review_id,
                f"draft {iid}/{version} awaiting a decision" if draft.actions else "nothing to publish",
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
            item = await self._queue.get()
            try:
                if isinstance(item, _CatchUpRequest):
                    await self.catch_up(asked=item.asked)
                elif isinstance(item, _DreamRequest):
                    await self.dream()
                elif isinstance(item, _ChatRequest):
                    await self.converse(item)
                else:
                    await self.process(item)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if isinstance(item, _CatchUpRequest):
                    self.logger.exception("catch-up failed")
                    if item.asked:
                        await self._tell_safe([f"Проверка новых MR не удалась: {exc}"])
                elif isinstance(item, _DreamRequest):
                    self.logger.exception("profile consolidation failed")
                elif isinstance(item, _ChatRequest):
                    self.logger.exception("conversation turn failed")
                    await self._tell_safe([f"Ответ не получен: {exc}"])
                else:
                    self.logger.exception("review of MR !{} failed", item.iid)
                    await self._tell_safe(render_notice(item.iid, "", f"Ревью не выполнено: {exc}"))
            finally:
                self._queue.task_done()

    async def process(self, candidate: ReviewCandidate) -> None:
        """Apply the code guards and, if they pass, run one review."""
        info = await self._merge_request(candidate.iid)
        if not info.open or info.draft:
            return

        if candidate.kind == "merge_request" and not candidate.requested:
            if self._state.reviewed_head(info.iid, info.head_sha):
                self.logger.info("MR !{}: commit {} already reviewed", info.iid, info.head_sha[:10])
                return
            reverted = await self._reverted_by(info.iid)
            if reverted:
                await self._tell([
                    f"!{info.iid} — ревёрт {', '.join(reverted)}, ревью пропущено. "
                    f"Проверить всё же: «проверь !{info.iid}»."
                ])
                return

        own = self.config.is_reviewer(info.author)
        authors = self._profiled([info.author] if info.author else [])
        if candidate.kind == "merge_request":
            if own and not self.config.review_own_merge_requests:
                return
            prompt = review_prompt(
                info.iid,
                own=own,
                lessons=await self._lessons_for(info.iid),
                people=self._people_for(authors),
                authors=authors,
                attachments_dir=self.config.attachments_dir,
            )
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
                await self._tell(render_notice(info.iid, info.title, text, task=await self._task_line(info)))
                return
            authors = self._profiled([decision.note_author or "", info.author or ""])
            prompt = reply_prompt(
                info.iid,
                candidate.discussion_id,
                decision.note_author,
                decision.note_body,
                lessons=await self._lessons_for(info.iid),
                people=self._people_for(authors),
                authors=authors,
                attachments_dir=self.config.attachments_dir,
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
        await self._run_agent(
            info,
            prompt,
            own=own,
            kind="review" if candidate.kind == "merge_request" else "thread",
            authors=authors,
        )

    async def _reverted_by(self, iid: int) -> list[str]:
        """What the MR reverts, when it provably does nothing else; otherwise nothing.

        See :mod:`reverts`: every commit must name what it reverts, and the
        MR's changes must be the exact inverse of it. Anything unverifiable is
        reviewed as usual.
        """
        assert self._gitlab is not None
        try:
            commits = await self._gitlab.get_commits(iid)
            refs = reverted_refs(str(commit.get("message") or "") for commit in commits)
            if not refs:
                return []
            originals: list[Net | None] = []
            for kind, value in refs:
                if kind == "mr":
                    originals.append(net_from_raw(await self._gitlab.get_raw_diff(int(value))))
                    continue
                parents = (await self._gitlab.get_commit(value)).get("parent_ids")
                if not isinstance(parents, list) or not parents:
                    return []
                first = str(cast("list[object]", parents)[0])
                originals.append(net_from_changes(await self._gitlab.compare(first, value)))
            current = net_from_raw(await self._gitlab.get_raw_diff(iid))
        except GitLabApiError as exc:
            self.logger.warning("MR !{}: revert not verifiable, reviewing as usual: {}", iid, exc)
            return []
        if not is_exact_revert(current, originals):
            self.logger.info("MR !{}: names a revert but changes more than that, reviewing", iid)
            return []
        return [label(ref) for ref in refs]

    async def _task_line(self, info: _MergeRequestInfo) -> str:
        """«Задача: KEY — name» with a link per task the MR names, or nothing."""
        keys = find_task_keys(self.config.task_key_pattern, info.title, info.description)
        if not keys or self._task_lookup is None:
            return ""
        lines: list[str] = []
        for key in keys[:MAX_TASKS]:
            try:
                lines.append((await self._task_lookup.lookup(key)).line())
            except Exception:
                self.logger.exception("MR !{}: lookup of {} failed", info.iid, key)
                lines.append(f"Задача: {key}")
        return "\n".join(lines)

    async def _lessons_for(self, iid: int) -> str:
        """Lessons matching this MR's changes; never blocks a review on failure."""
        directory = self.config.lessons_dir.strip()
        if not directory or self.config.lessons_budget_chars <= 0:
            return ""
        assert self._gitlab is not None
        try:
            lessons = await asyncio.to_thread(load_lessons, Path(directory).expanduser())
            if not lessons:
                return ""
            changes = await self._gitlab.get_changes(iid)
            paths = {
                str(change.get(key) or "")
                for change in changes
                for key in ("new_path", "old_path")
            }
            diff_text = "\n".join(
                line[1:]
                for change in changes
                for line in str(change.get("diff") or "").splitlines()
                if line[:1] in "+-" and not line.startswith(("+++", "---"))
            )
            matched = select_lessons(lessons, paths, diff_text)
        except Exception:
            self.logger.exception("MR !{}: lesson selection failed, reviewing without it", iid)
            return ""
        self.logger.info(
            "MR !{}: {} of {} tagged lessons match", iid, len(matched), len(lessons)
        )
        return render_lessons(matched, self.config.lessons_budget_chars)

    async def _run_agent(
        self,
        info: _MergeRequestInfo,
        prompt: str,
        *,
        own: bool = False,
        kind: str = "review",
        authors: list[str] | None = None,
    ) -> None:
        """Run one review in a Claude session of its own."""
        assert self._loop is not None
        record = self._state.start_review(
            iid=info.iid,
            title=info.title,
            web_url=info.web_url,
            task_keys=find_task_keys(self.config.task_key_pattern, info.title, info.description),
            kind=kind,
            session_id=str(uuid.uuid4()),
            head_sha=info.head_sha,
            author=info.author or "",
        )
        pending = _PendingRun(
            self._loop.create_future(),
            info.head_sha,
            info.title,
            info.web_url,
            own=own,
            task=await self._task_line(info),
            review_id=record.id,
            iid=info.iid,
            authors=tuple(authors or ()),
        )
        finished = await self._ask_agent(
            self.config.review_chat_id(info.iid), prompt, pending, session=record.session_id
        )
        if not finished:
            self._state.set_review_status(record.id, "stopped on timeout")
            await self._tell(
                render_notice(
                    info.iid, info.title, "Ревью не завершилось за отведённое время и остановлено."
                )
            )

    async def _ask_agent(
        self, chat_id: str, prompt: str, pending: _PendingRun, *, session: str, resume: bool = False
    ) -> bool:
        """Hand *prompt* to the agent and wait for its answer; ``False`` on timeout."""
        self._pending[chat_id] = pending
        pending.session = session
        metadata: dict[str, Any] = {SESSION_METADATA_KEY: {"session_id": session, "resume": resume}}
        if pending.iid is not None:
            metadata["gitlab"] = {"iid": pending.iid, "project": self.config.project_path}
        typing = asyncio.create_task(self._keep_typing()) if pending.kind == "chat" else None
        try:
            await self.bus.publish_inbound(
                InboundMessage(
                    channel=self.name,
                    sender_id=f"gitlab-mr-{pending.iid}" if pending.iid is not None else "gitlab-chat",
                    chat_id=chat_id,
                    content=prompt,
                    timestamp=datetime.now(),
                    metadata=metadata,
                )
            )
            try:
                await asyncio.wait_for(asyncio.shield(pending.future), self.config.review_timeout_s)
            except TimeoutError:
                await self._stop_turn(chat_id)
                return False
            return True
        finally:
            if typing is not None:
                typing.cancel()
            self._pending.pop(chat_id, None)

    async def _keep_typing(self) -> None:
        assert self._telegram is not None
        while True:
            try:
                await self._telegram.send_typing(str(self.config.telegram_chat_id))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.debug("typing indicator failed: {}", exc)
            await asyncio.sleep(TYPING_INTERVAL_S)

    async def _stop_turn(self, chat_id: str) -> None:
        """Cancel the agent turn so it cannot run alongside the next one."""
        await self.bus.publish_inbound(
            InboundMessage(
                channel=self.name,
                sender_id="gitlab-review",
                chat_id=chat_id,
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
            except Exception as exc:
                self.logger.warning("Telegram polling failed: {}", exc)
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
        """Act on a message from the approver; ignore every other chat and sender.

        Commands are recognised in code and run at once. Anything else is a
        question or a request to the reviewer and goes to the agent.
        """
        if not self.config.is_approver(message.chat_id, message.sender_id):
            return
        text = message.text or ""
        if not text.strip():
            return
        code = verification_code(text) if self.config.vpn_control else None
        if code is not None:
            await self._submit_vpn_code(code)
            return
        if self.config.vpn_control and asks_reconnect(text):
            await self._reconnect_vpn("по вашей просьбе")
            return
        if _CATCH_UP_RE.match(text):
            assert self._queue is not None
            if self._pending:
                await self._tell(["Проверю новые MR после текущей работы."])
            self._queue.put_nowait(_CatchUpRequest(asked=True))
            return
        requested = _REVIEW_REQUEST_RE.match(text)
        if requested is not None:
            assert self._queue is not None
            iid = int(requested.group(1))
            self._queue.put_nowait(ReviewCandidate(kind="merge_request", iid=iid, requested=True))
            busy = " после текущей работы" if self._pending else ""
            await self._tell([f"!{iid}: запускаю ревью{busy}."])
            return
        command = self._command_for(text, message.reply_to)
        if command is None and message.reply_to is None:
            bare = parse_bare_command(text)
            if bare is not None:
                command = await self._command_for_only_draft(*bare)
                if command is None:
                    return
        if command is not None:
            await self.apply_command(command, text)
            return
        assert self._queue is not None
        if self._pending or not self._queue.empty():
            await self._tell(["Принял. Отвечу, как закончу текущую работу."])
        self._queue.put_nowait(_ChatRequest(text, message.reply_to))

    def _command_for(self, text: str, reply_to: int | None) -> ApprovalCommand | None:
        """A command naming its draft: by reply to a draft or its review, or by number."""
        if reply_to is not None:
            ref = self._state.draft_for_message(reply_to)
            if ref is not None:
                return parse_reply_command(text, *ref) or parse_command(text)
            review = self._state.review_for_message(reply_to)
            if review is not None and review.iid is not None:
                stored = self._state.load_draft(review.iid)
                command = parse_reply_command(
                    text, review.iid, stored.version if stored is not None else None
                )
                if command is not None:
                    return command
        return parse_command(text)

    async def _command_for_only_draft(
        self, publish: bool, items: tuple[int, ...]
    ) -> ApprovalCommand | None:
        """Apply a bare «публикуй»/«отмена» to the only waiting draft, or say why not."""
        drafts = self._state.pending_drafts()
        if len(drafts) == 1:
            iid, version = drafts[0]
            return ApprovalCommand(publish=publish, iid=iid, items=items, version=version)
        if not drafts:
            await self._tell(["Нет черновиков, ждущих решения."])
            return None
        await self._tell([
            "Черновиков несколько — ответьте на сообщение нужного или укажите номер "
            "(«публикуй !N/V»):\n" + "\n".join(self._describe_draft(iid, v) for iid, v in drafts)
        ])
        return None

    def _describe_draft(self, iid: int, version: int) -> str:
        review = self._state.latest_review(iid)
        title = f" {review.title}" if review is not None and review.title else ""
        return f"!{iid}/{version}{title}"

    async def apply_command(self, command: ApprovalCommand, text: str = "") -> None:
        """Publish or cancel a draft, and file the decision in the archive."""
        review = self._state.latest_review(command.iid)
        if review is not None and text:
            self._state.add_event(review.id, "human", text)
        if not command.publish and command.version is not None:
            stored = self._state.load_draft(command.iid)
            if stored is not None and stored.version != command.version:
                await self._tell([
                    f"!{command.iid}: черновик {command.iid}/{command.version} уже заменён, "
                    f"актуальный — !{command.iid}/{stored.version}. Ничего не отменено."
                ])
                return
        if not command.publish:
            if self._state.load_draft(command.iid) is None:
                await self._tell([f"!{command.iid}: нет черновика для отмены."])
                return
            self._state.delete_draft(command.iid)
            message_ids = await self._tell([f"!{command.iid}: черновик отменён, ничего не опубликовано."])
            if review is not None:
                self._state.set_review_status(review.id, "draft cancelled")
                self._state.record_review_messages(review.id, message_ids)
            return
        await self.publish(command)

    async def converse(self, request: _ChatRequest) -> None:
        """Answer one message of the approver in the session of the review it is about."""
        assert self._loop is not None
        target = self._state.review_for_message(request.reply_to) if request.reply_to else None
        drafts = self._state.pending_drafts()
        iid: int | None = None
        if target is None and request.reply_to is not None:
            ref = self._state.draft_for_message(request.reply_to)
            iid = ref[0] if ref is not None else None
        elif target is None:
            target = self._state.latest_active_review(datetime.now() - CONTINUE_WINDOW)
            if target is None and len(drafts) == 1:
                iid = drafts[0][0]
        if target is None and iid is not None:
            target = self._state.latest_review(iid)
        archive = [
            review
            for review in self._state.find_reviews(
                [int(number) for number in _MR_REF_RE.findall(request.text)],
                find_task_keys(self.config.task_key_pattern, request.text),
                ARCHIVE_LIMIT,
            )
            if target is None or review.id != target.id
        ]
        resume = target is not None
        if target is None and iid is not None:
            target = await self._review_without_session(iid)
        elif target is None:
            target = self._state.start_review(
                iid=None,
                title="",
                web_url="",
                task_keys=find_task_keys(self.config.task_key_pattern, request.text),
                kind="chat",
                session_id=str(uuid.uuid4()),
                head_sha="",
            )
        self._state.add_event(target.id, "human", request.text)
        named: list[str] = [target.author] if target.author else []
        authors = self._profiled([*named, *mentioned_usernames(request.text)])
        prompt = chat_prompt(
            request.text,
            target=self._describe_review(target) if target.iid is not None else "",
            draft=self._current_draft_text(target),
            pending=[self._describe_draft(iid, version) for iid, version in drafts],
            archive=[
                self._state.render_review(review)[:ARCHIVE_ENTRY_CHARS] for review in archive
            ],
            archive_dir=str(self._state.archive_dir),
            people=self._people_for(authors),
            authors=authors,
            attachments_dir=self.config.attachments_dir,
            iid=target.iid,
        )
        pending = _PendingRun(
            self._loop.create_future(),
            target.head_sha,
            target.title,
            target.web_url,
            kind="chat",
            review_id=target.id,
            iid=target.iid,
            user_text=request.text,
            authors=tuple(authors),
        )
        chat_id = (
            self.config.review_chat_id(target.iid)
            if target.iid is not None
            else f"{self.config.chat_id}:chat-{target.id}"
        )
        finished = await self._ask_agent(
            chat_id, prompt, pending, session=target.session_id, resume=resume
        )
        if not finished:
            await self._tell(["Ответ не уложился в отведённое время и остановлен."])

    async def _review_without_session(self, iid: int) -> ReviewRecord:
        """An archive entry for a draft whose review left none behind.

        The conversation then starts a fresh session; the draft in the prompt
        is all the agent knows of the review.
        """
        info = await self._merge_request(iid)
        stored = self._state.load_draft(iid)
        return self._state.start_review(
            iid=iid,
            title=info.title,
            web_url=info.web_url,
            task_keys=find_task_keys(self.config.task_key_pattern, info.title, info.description),
            kind="review",
            session_id=str(uuid.uuid4()),
            head_sha=stored.head_sha if stored is not None else info.head_sha,
            author=info.author or "",
        )

    def _describe_review(self, review: ReviewRecord) -> str:
        parts = [f"MR !{review.iid} «{review.title}»", f"started {review.started_at}", review.status]
        if review.task_keys:
            parts.append("tasks " + ", ".join(review.task_keys))
        if review.web_url:
            parts.append(review.web_url)
        return "; ".join(parts)

    def _current_draft_text(self, review: ReviewRecord) -> str:
        if review.iid is None:
            return ""
        stored = self._state.load_draft(review.iid)
        if stored is None:
            return ""
        lines = [f"Draft {review.iid}/{stored.version}:"]
        for number, action in enumerate(stored.actions, start=1):
            lines.append(f"{number}. [{action.label()}] {action.body}".rstrip())
        return "\n".join(lines)

    async def _bind_draft(self, iid: int | None, pending: _PendingRun) -> str:
        """Tie a draft from a conversation to its MR; return why not, or ``""``."""
        if pending.iid is not None:
            if iid is None or iid == pending.iid:
                return ""
            return f"Черновик назван для !{iid}, а разговор о !{pending.iid}. Не сохранён — уточните MR."
        if iid is None:
            return "Предложенные действия не сохранены: не указан MR. Напишите, к какому MR они относятся."
        info = await self._merge_request(iid)
        if pending.review_id is not None:
            self._state.bind_review(
                pending.review_id,
                iid=iid,
                title=info.title,
                web_url=info.web_url,
                head_sha=info.head_sha,
                author=info.author or "",
            )
        pending.iid = iid
        pending.head_sha = info.head_sha
        pending.title = info.title
        pending.web_url = info.web_url
        pending.task = await self._task_line(info)
        return ""

    async def _deliver_chat(self, answer: str, pending: _PendingRun) -> None:
        """Show the agent's reply; turn a revised draft or a decision into action."""
        parsed = parse_chat_answer(answer, pending.iid)
        problem = await self._bind_draft(parsed.draft_iid, pending) if parsed.draft is not None else ""
        message_ids = await self._tell(render_chat(pending.iid, parsed.text))
        if pending.review_id is not None:
            self._state.record_review_messages(pending.review_id, message_ids)
            self._state.add_event(pending.review_id, "reviewer", parsed.text)
        if problem:
            await self._tell([problem])
            parsed = ChatAnswer(parsed.text, None, parsed.decision)
        if parsed.draft is not None and pending.iid is not None:
            if parsed.draft.actions:
                revised = self._state.load_draft(pending.iid) is not None
                await self._send_draft(
                    pending.iid,
                    ReviewDraft(
                        "Исправленный черновик." if revised else "Черновик из разговора.",
                        parsed.draft.actions,
                        parsed.draft.errors,
                    ),
                    pending,
                )
            elif self._state.load_draft(pending.iid) is not None:
                self._state.delete_draft(pending.iid)
                if pending.review_id is not None:
                    self._state.set_review_status(pending.review_id, "draft withdrawn")
                await self._tell([f"!{pending.iid}: черновик снят, публиковать нечего."])

        decision = parsed.decision
        if decision is None:
            return
        if parsed.draft is not None:
            await self._tell([
                f"!{decision.iid}: сначала посмотрите новую версию черновика, потом ответьте на неё."
            ])
            return
        if decision.version is None:
            stored = self._state.load_draft(decision.iid)
            if stored is not None:
                decision = ApprovalCommand(
                    decision.publish, decision.iid, decision.items, stored.version
                )
        action = "публикую" if decision.publish else "отменяю"
        await self._tell([f"!{decision.iid}: {action} по вашему «{pending.user_text[:200]}»."])
        await self.apply_command(decision)

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
        moved = info.head_sha != reviewed_sha
        review = self._state.latest_review(command.iid)
        if moved and all(action.type in _TIED_TO_CODE for _, action in selected):
            self._state.delete_draft(command.iid)
            notice = (
                f"!{command.iid}: ветка изменилась после ревью "
                f"({reviewed_sha[:10]} → {info.head_sha[:10]}). "
                "Черновик снят, ничего не опубликовано."
            )
            if review is not None:
                self._state.add_event(review.id, "system", notice)
                self._state.set_review_status(review.id, "draft withdrawn: branch moved")
            message_ids = await self._tell([notice])
            if review is not None:
                self._state.record_review_messages(review.id, message_ids)
            return

        report: list[str] = []
        published = 0
        failed: list[ProposedAction] = []
        for number, action in selected:
            if action.type == "approve" and self.config.is_reviewer(info.author):
                report.append(f"{number}. {action.label()}: пропущено — это ваш MR")
                continue
            if action.type == "approve" and not info.open:
                report.append(f"{number}. {action.label()}: пропущено — MR уже {info.state_label}")
                continue
            if moved and action.type in _TIED_TO_CODE:
                report.append(f"{number}. {action.label()}: пропущено — ветка изменилась после ревью")
                continue
            try:
                outcome = await self._publish_action(info, action)
            except GitLabApiError as exc:
                failed.append(action)
                report.append(f"{number}. {action.label()}: ошибка — {exc}")
            else:
                published += 1
                report.append(f"{number}. {action.label()}: {outcome}")
        self._state.delete_draft(command.iid)
        if failed:
            version = self._state.next_draft_version(command.iid)
            self._state.save_draft(command.iid, reviewed_sha, tuple(failed), version)
            report.append(
                f"Неопубликованное осталось в черновике {command.iid}/{version} — "
                f"повторить: «публикуй !{command.iid}/{version}»."
            )
        if len(selected) < len(actions):
            report.append("Остальные пункты черновика сняты.")
        summary = f"!{command.iid}:\n" + "\n".join(report)
        if review is not None:
            self._state.add_event(review.id, "system", summary)
            self._state.add_published(review.id, published)
            self._state.set_review_status(review.id, f"published (draft {current})")
        message_ids = await self._tell([summary])
        if review is not None:
            self._state.record_review_messages(review.id, message_ids)

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

    async def _publish_action(self, info: _MergeRequestInfo, action: ProposedAction) -> str:
        """Post one approved action; return how it went out."""
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
            try:
                await self._gitlab.create_discussion(info.iid, action.body, position)
            except GitLabApiError as exc:
                if exc.status != 400 or "line_code" not in str(exc):
                    raise
                line = action.line if action.line is not None else action.old_line
                await self._gitlab.create_note(info.iid, f"`{action.path}:{line}`\n\n{action.body}")
                return "опубликовано общим комментарием — GitLab не показывает эту строку в диффе"
        elif action.type == "reply":
            assert action.discussion_id
            await self._gitlab.reply(info.iid, action.discussion_id, action.body)
        elif action.type == "approve":
            await self._gitlab.approve(info.iid, info.head_sha)
        else:
            await self._gitlab.create_note(info.iid, action.body)
        return "опубликовано"

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
            description=str(mr.get("description") or ""),
            state=str(mr.get("state") or ""),
        )

    async def _tell(self, chunks: list[str]) -> list[int]:
        assert self._telegram is not None
        message_ids: list[int] = []
        for chunk in chunks:
            message_id = await self._telegram.send_message(str(self.config.telegram_chat_id), chunk)
            if message_id is not None:
                message_ids.append(message_id)
        return message_ids

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


def _local_time(value: object) -> str:
    """GitLab's UTC timestamp as local time, comparable with the channel's own records."""
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return ""
    if moment.tzinfo is not None:
        moment = moment.astimezone().replace(tzinfo=None)
    return moment.isoformat(timespec="seconds")


def _note_author(note: dict[str, Any]) -> str | None:
    author = note.get("author")
    username = cast("dict[str, Any]", author).get("username") if isinstance(author, dict) else None
    return username if isinstance(username, str) else None


__all__ = ["GitLabReviewChannel", "GitLabWebhookError"]
