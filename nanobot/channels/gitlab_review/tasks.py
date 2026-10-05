"""The tracker task behind a merge request: its key, name and link.

Keys are taken from the MR's title and description, not from the source
branch: a branch can carry commits of several tasks, while the title and the
description name the tasks the MR is for. ClickUp is
asked first by custom task id; a key ClickUp does not know is treated as a task
that stayed in Jira and gets a Jira link without a name. Any failure leaves the
task out rather than holding up a review.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, cast

import httpx
from loguru import logger

from nanobot.channels.gitlab_review.gitlab_api import USER_AGENT

DEFAULT_KEY_PATTERN = r"[A-Z][A-Z0-9]+-\d+"
CLICKUP_API = "https://api.clickup.com/api/v2"


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
    def __init__(
        self,
        *,
        clickup_token: str,
        clickup_team_id: str,
        jira_url: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token = clickup_token.strip()
        self._team = clickup_team_id.strip()
        self._jira = jira_url.strip().rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=20.0, headers={"User-Agent": USER_AGENT})
        self._owns_client = client is None
        self._cache: dict[str, TaskInfo] = {}

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def lookup(self, key: str) -> TaskInfo:
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        info = await self._from_clickup(key) or TaskInfo(key, "", self._jira_link(key))
        self._cache[key] = info
        return info

    async def _from_clickup(self, key: str) -> TaskInfo | None:
        if not self._token or not self._team:
            return None
        try:
            response = await self._client.get(
                f"{CLICKUP_API}/task/{key}",
                params={"custom_task_ids": "true", "team_id": self._team},
                headers={"Authorization": self._token},
            )
            data: object = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("ClickUp lookup of {} failed: {}", key, exc)
            return None
        if response.status_code >= 400 or not isinstance(data, dict):
            return None
        task = cast("dict[str, Any]", data)
        name, url = task.get("name"), task.get("url")
        if not isinstance(name, str) or not name.strip():
            return None
        return TaskInfo(key, name.strip(), url if isinstance(url, str) else "")

    def _jira_link(self, key: str) -> str:
        return f"{self._jira}/browse/{key}" if self._jira else ""
