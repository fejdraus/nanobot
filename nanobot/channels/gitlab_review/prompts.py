"""Instructions handed to the reviewing agent.

The review logic itself lives in the ``review-gitlab-mrs`` skill; these
prompts only set the mode: draft, never publish, and end with the actions block
that :mod:`proposals` parses.

A prompt must not start with ``/``: nanobot's command router would take it for
an unknown slash command and answer it without running the model.
"""
from __future__ import annotations

from nanobot.channels.gitlab_review.proposals import ACTIONS_FENCE, DECISION_FENCE

_DRAFT_RULES = f"""\
Режим черновика. Ничего не публикуй в GitLab сам: не создавай комментарии и треды, \
не отвечай в треды, не ставь аппрув, не резолвь треды. Подтверждения не жди — \
публикацию выполнит человек отдельно, после проверки в Telegram.

В конце ответа выведи ровно один блок с предлагаемыми действиями:

```{ACTIONS_FENCE}
{{"actions": [
  {{"type": "discussion", "path": "Pkg/.../File.cs", "line": 42, "body": "текст замечания"}},
  {{"type": "reply", "discussion_id": "<id треда>", "body": "текст ответа"}},
  {{"type": "note", "body": "общий комментарий без привязки к строке"}},
  {{"type": "approve"}}
]}}
```

- discussion: line — номер строки в новой версии файла из диффа MR (для удалённой строки — old_line).
- reply: только в тред, который начал ревьюер.
- approve: только если по правилам шага 8 аппрув уместен.
Если предлагать нечего, выведи блок с пустым списком actions. Текст до блока — краткая сводка для человека."""


_OWN_MR = (
    "Это merge request самого ревьюера. Он просит проверить свой код так же строго, как чужой: "
    "правило скилла «не ревьюить свои MR» здесь не действует. Аппрув не предлагай."
)


def review_prompt(iid: int, *, own: bool = False, lessons: str = "") -> str:
    own_note = f"{_OWN_MR}\n\n" if own else ""
    lessons_note = f"{lessons}\n\n" if lessons else ""
    return (
        f"Выполни скилл review-gitlab-mrs для merge request !{iid}.\n\n"
        f"Открыт merge request !{iid}. Проведи ревью.\n\n{own_note}{lessons_note}{_DRAFT_RULES}"
    )


def reply_prompt(
    iid: int, discussion_id: str, note_author: str | None, note_body: str, *, lessons: str = ""
) -> str:
    quoted = "\n".join(f"> {line}" for line in (note_body or "").splitlines()) or "> (пусто)"
    return (
        f"Выполни скилл review-gitlab-mrs для merge request !{iid}.\n\n"
        f"В треде {discussion_id} merge request !{iid}, который начал ревьюер, "
        f"{note_author or 'участник'} ответил:\n{quoted}\n\n"
        "Работай только с этим тредом: проверь ответ по коду и задаче (шаг 9), "
        "реши, нужен ли ответ в тред, и можно ли предложить аппрув (шаг 8). "
        f"Другие треды не трогай.\n\n{lessons + chr(10) * 2 if lessons else ''}{_DRAFT_RULES}"
    )


_CHAT_RULES = f"""\
Ничего не публикуй в GitLab сам и не меняй задачи. Публикует код, и только по слову человека.

- Отвечай по существу и коротко: это Telegram. Если для ответа нужно проверить код MR или задачу — проверь.
- Если человек просит изменить замечания (убрать, переписать, добавить), выведи новый блок \
```{ACTIONS_FENCE}``` с полным списком действий — не разницей. Он станет новой версией черновика. \
Формат тот же, что в ревью: {{"actions": [{{"type": "discussion", "path": "...", "line": 42, "body": "..."}}, \
{{"type": "note", "body": "..."}}, {{"type": "reply", "discussion_id": "...", "body": "..."}}]}}.
- Если человек явно просит опубликовать или отменить черновик, выведи в конце блок

```{DECISION_FENCE}
{{"decision": "publish", "iid": 6318, "items": [1]}}
```

  decision — publish или cancel; items — номера пунктов, пустой список — все. \
Если не ясно, какой черновик или какие пункты имеются в виду, спроси и блок не выводи. \
Не выводи этот блок вместе с новой версией черновика: человек должен сначала её увидеть."""


def chat_prompt(
    text: str,
    *,
    target: str = "",
    draft: str = "",
    pending: list[str] | None = None,
    archive: list[str] | None = None,
    archive_dir: str = "",
) -> str:
    """One message of the approver's conversation with the reviewer.

    The conversation about a review runs in that review's own Claude session,
    so the agent remembers what it read. The draft is repeated anyway: the
    session may be gone, and the item numbers must match what the human saw.
    """
    parts = ["Это разговор в Telegram с человеком, который проверяет и одобряет твои ревью."]
    if target:
        parts.append(f"Речь о ревью: {target}")
    if draft:
        parts.append(f"Текущий черновик этого ревью:\n{draft}")
    if pending:
        parts.append("Черновики, ждущие решения:\n" + "\n".join(f"- {line}" for line in pending))
    if archive:
        parts.append(
            "Из архива — прошлые ревью, на которые ссылается человек:\n\n" + "\n\n---\n\n".join(archive)
        )
    if archive_dir:
        parts.append(
            f"Полный архив ревью — каталог {archive_dir}, по файлу на ревью. Если человек ссылается на "
            "ревью, которого здесь нет, найди его там (Grep по номеру задачи, MR или теме). "
            "Протокол сессии каждого ревью — ~/.claude/projects/*/<Сессия Claude>.jsonl."
        )
    quoted = "\n".join(f"> {line}" for line in (text or "").splitlines()) or "> (пусто)"
    parts.append(f"Сообщение человека:\n{quoted}")
    parts.append(_CHAT_RULES)
    return "\n\n".join(parts)
