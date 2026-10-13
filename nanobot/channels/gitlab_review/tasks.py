"""The tracker task behind a merge request: its key, name and link.

Keys are taken from the MR's title and description, not from the source
branch: a branch can carry commits of several tasks, while the title and the
description name the tasks the MR is for. ClickUp is
asked first by custom task id, through the ClickUp MCP server (the channel holds
no ClickUp token); a key ClickUp does not know is treated as a task that stayed
in Jira and gets a Jira link without a name. Any failure leaves the task out
rather than holding up a review.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from loguru import logger

from nanobot.channels.gitlab_review.mcp_client import McpToolError, ToolCaller

DEFAULT_KEY_PATTERN = r"[A-Z][A-Z0-9]+-\d+"
_HEADING_RE = re.compile(r"^# [^:\n]+: (.+)$", re.MULTILINE)
_URL_RE = re.compile(r"^- URL: (\S+)$", re.MULTILINE)


@dataclass(frozen=True)
class TaskInfo:
    key: str
    name: str
    url: str

    def line(self) -> str:
        head = f"Задача: {self.key}" + (f" — {self.name}" if self.name else "")
        return f"{head}\n{self.url}" if self.url else head


def find_task_keys(pattern: str, *texts: str) -> list[str]:
    """Every distinct task key in *texts*, in order of first mention."""
    regex = re.compile(pattern or DEFAULT_KEY_PATTERN)
    keys: list[str] = []
    for text in texts:
        for match in regex.finditer(text or ""):
            if match.group(0) not in keys:
                keys.append(match.group(0))
    return keys


class TaskLookup:
    def __init__(self, *, clickup: ToolCaller | None, jira_url: str) -> None:
        self._clickup = clickup
        self._jira = jira_url.strip().rstrip("/")
        self._cache: dict[str, TaskInfo] = {}

    async def aclose(self) -> None:
        if self._clickup is not None:
            await self._clickup.aclose()

    async def lookup(self, key: str) -> TaskInfo:
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        info = await self._from_clickup(key) or TaskInfo(key, "", self._jira_link(key))
        self._cache[key] = info
        return info

    async def _from_clickup(self, key: str) -> TaskInfo | None:
        if self._clickup is None:
            return None
        try:
            text = await self._clickup.call("clickup_get_task", {"taskId": key})
        except McpToolError as exc:
            logger.info("ClickUp has no task {}: {}", key, str(exc)[:200])
            return None
        heading, url = _HEADING_RE.search(text), _URL_RE.search(text)
        if heading is None or not heading.group(1).strip():
            logger.warning("ClickUp answer for {} has no task name", key)
            return None
        return TaskInfo(key, heading.group(1).strip(), url.group(1) if url else "")

    def _jira_link(self, key: str) -> str:
        return f"{self._jira}/browse/{key}" if self._jira else ""
