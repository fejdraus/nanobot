"""Review drafts: what the agent proposes and what the human approves.

The agent never publishes. It ends its answer with a machine-readable block of
proposed GitLab actions; the channel stores them, shows them in Telegram, and
publishes only the items the human approves there. Keeping the format and its
validation here makes "what can reach GitLab" a closed, testable list.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, cast

ACTIONS_FENCE = "gitlab-review-actions"
TELEGRAM_LIMIT = 4000

ActionType = Literal["discussion", "note", "reply", "approve"]
_ACTION_TYPES: frozenset[str] = frozenset({"discussion", "note", "reply", "approve"})

_FENCE_RE = re.compile(
    r"```" + re.escape(ACTIONS_FENCE) + r"[ \t]*\r?\n(?P<body>.*?)\r?\n```",
    re.DOTALL,
)
_COMMAND_RE = re.compile(
    r"^\s*/?(?P<verb>публикуй|опубликуй|publish|отмена|отмени|cancel)\s+!?(?P<iid>\d+)"
    r"(?:/(?P<version>\d+))?(?P<items>(?:[\s,]+\d+)*)\s*$",
    re.IGNORECASE,
)
_REPLY_RE = re.compile(
    r"^\s*/?(?P<verb>публикуй|опубликуй|publish|отмена|отмени|cancel)"
    r"(?P<items>(?:[\s,]+\d+)*)\s*$",
    re.IGNORECASE,
)
_PUBLISH_VERBS = frozenset({"публикуй", "опубликуй", "publish"})


@dataclass(frozen=True)
class ProposedAction:
    """One GitLab write the human may approve."""

    type: ActionType
    body: str = ""
    path: str | None = None
    line: int | None = None
    old_line: int | None = None
    discussion_id: str | None = None

    def label(self) -> str:
        if self.type == "discussion":
            line = self.line if self.line is not None else self.old_line
            return f"инлайн {self.path}:{line}"
        if self.type == "reply":
            return f"ответ в тред {self.discussion_id}"
        if self.type == "approve":
            return "аппрув MR"
        return "общий комментарий"


@dataclass(frozen=True)
class ReviewDraft:
    """Everything parsed from one agent answer."""

    summary: str
    actions: tuple[ProposedAction, ...]
    errors: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ApprovalCommand:
    publish: bool
    iid: int
    items: tuple[int, ...]
    version: int | None = None


def parse_draft(answer: str) -> ReviewDraft:
    """Split an agent answer into its human summary and its proposed actions.

    A missing or broken block yields no actions rather than an exception: the
    summary still reaches the human, who can see that nothing is publishable.
    """
    matches = list(_FENCE_RE.finditer(answer or ""))
    if not matches:
        return ReviewDraft(summary=(answer or "").strip(), actions=())
    block = matches[-1]
    summary = (answer[: block.start()] + answer[block.end():]).strip()
    try:
        data: object = json.loads(block.group("body"))
    except json.JSONDecodeError as exc:
        return ReviewDraft(summary=summary, actions=(), errors=(f"блок действий не JSON: {exc}",))

    mapping = _as_mapping(data)
    raw_actions = mapping.get("actions") if mapping is not None else data
    if not isinstance(raw_actions, list):
        return ReviewDraft(summary=summary, actions=(), errors=("в блоке нет списка actions",))

    actions: list[ProposedAction] = []
    errors: list[str] = []
    for index, raw in enumerate(cast("list[object]", raw_actions), start=1):
        action, error = _parse_action(raw)
        if action is not None:
            actions.append(action)
        else:
            errors.append(f"действие {index}: {error}")
    return ReviewDraft(summary=summary, actions=tuple(actions), errors=tuple(errors))


def parse_command(text: str) -> ApprovalCommand | None:
    """Recognise «публикуй !6308», «публикуй !6308/2 1,3», «отмена !6308»."""
    match = _COMMAND_RE.match(text or "")
    if match is None:
        return None
    items = tuple(int(item) for item in re.findall(r"\d+", match.group("items") or ""))
    version = match.group("version")
    return ApprovalCommand(
        publish=match.group("verb").casefold() in _PUBLISH_VERBS,
        iid=int(match.group("iid")),
        items=items,
        version=int(version) if version else None,
    )


def parse_reply_command(text: str, iid: int, version: int) -> ApprovalCommand | None:
    """Recognise «публикуй», «публикуй 1,3», «отмена» sent as a reply to a draft message."""
    match = _REPLY_RE.match(text or "")
    if match is None:
        return None
    items = tuple(int(item) for item in re.findall(r"\d+", match.group("items") or ""))
    return ApprovalCommand(
        publish=match.group("verb").casefold() in _PUBLISH_VERBS,
        iid=iid,
        items=items,
        version=version,
    )


def render_draft(
    iid: int,
    title: str,
    draft: ReviewDraft,
    *,
    web_url: str = "",
    version: int = 1,
    task: str = "",
) -> list[str]:
    """Render a draft as Telegram messages, each within the message size limit."""
    ref = f"{iid}/{version}"
    header = f"!{iid} {title}".strip()
    if web_url:
        header += f"\n{web_url}"
    if task:
        header += f"\n{task}"
    header += f"\nЧерновик {ref}"
    parts = [header, draft.summary or "(без пояснений)"]
    for number, action in enumerate(draft.actions, start=1):
        text = f"{number}. [{action.label()}]"
        if action.body:
            text += f"\n{action.body}"
        parts.append(text)
    if draft.errors:
        parts.append("Не разобрано:\n" + "\n".join(draft.errors))
    if draft.actions:
        parts.append(
            "Ответьте на это сообщение: «публикуй» (всё), «публикуй 1,3» (выборочно) или «отмена». "
            f"Или командой: «публикуй !{ref}»."
        )
    else:
        parts.append("Публиковать нечего.")
    return _chunk("\n\n".join(parts))


def render_notice(iid: int, title: str, text: str, *, task: str = "") -> list[str]:
    header = f"!{iid} {title}".strip() + (f"\n{task}" if task else "")
    return _chunk(header + "\n\n" + text)


def _parse_action(raw: object) -> tuple[ProposedAction | None, str]:
    item = _as_mapping(raw)
    if item is None:
        return None, "не объект"
    kind = item.get("type")
    if kind not in _ACTION_TYPES:
        return None, f"неизвестный type {kind!r}"
    body = item.get("body")
    body = body.strip() if isinstance(body, str) else ""
    if kind != "approve" and not body:
        return None, "пустой body"
    if kind == "discussion":
        path = item.get("path")
        line = _int(item.get("line"))
        old_line = _int(item.get("old_line"))
        if not isinstance(path, str) or not path or (line is None and old_line is None):
            return None, "для инлайн-комментария нужны path и line"
        return ProposedAction("discussion", body, path=path, line=line, old_line=old_line), ""
    if kind == "reply":
        discussion_id = item.get("discussion_id")
        if not isinstance(discussion_id, str) or not discussion_id:
            return None, "для ответа нужен discussion_id"
        return ProposedAction("reply", body, discussion_id=discussion_id), ""
    if kind == "approve":
        return ProposedAction("approve"), ""
    return ProposedAction("note", body), ""


def _chunk(text: str) -> list[str]:
    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= TELEGRAM_LIMIT:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(paragraph) > TELEGRAM_LIMIT:
            chunks.append(paragraph[:TELEGRAM_LIMIT])
            paragraph = paragraph[TELEGRAM_LIMIT:]
        current = paragraph
    if current:
        chunks.append(current)
    return chunks


def _as_mapping(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    mapping = cast("Mapping[Any, Any]", value)
    return {str(key): item for key, item in mapping.items()}


def _int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip()) or None
    return None
