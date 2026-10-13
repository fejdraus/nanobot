from typing import Any

import pytest

from nanobot.channels.gitlab_review.mcp_client import McpToolError
from nanobot.channels.gitlab_review.tasks import TaskInfo, TaskLookup, find_task_keys


def test_keys_come_from_title_then_description_without_repeats() -> None:
    assert find_task_keys("", "feat: #AMCRM-16127 thing", "Closes AMCRM-16127") == ["AMCRM-16127"]
    assert find_task_keys("", "fix: AMCRM-1 and AMCRM-2", "Closes AMCRM-3, AMCRM-1") == [
        "AMCRM-1", "AMCRM-2", "AMCRM-3",
    ]
    assert find_task_keys("", "fix typo", "") == []


class FakeClickUp:
    def __init__(self, answer: str | Exception) -> None:
        self.answer = answer
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, tool: str, arguments: dict[str, Any]) -> str:
        self.calls.append((tool, arguments))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    async def aclose(self) -> None:
        return None


JIRA = "https://boards.example.com/"


@pytest.mark.asyncio
async def test_clickup_task_gives_name_and_link_and_is_cached() -> None:
    clickup = FakeClickUp(
        "# AMCRM-16127: Доработать интеграцию: этап 2\n\n- URL: https://app.clickup.com/t/abc\n- Status: open\n"
    )
    lookup = TaskLookup(clickup=clickup, jira_url=JIRA)
    first = await lookup.lookup("AMCRM-16127")
    second = await lookup.lookup("AMCRM-16127")
    assert first == second == TaskInfo("AMCRM-16127", "Доработать интеграцию: этап 2", "https://app.clickup.com/t/abc")
    assert clickup.calls == [("clickup_get_task", {"taskId": "AMCRM-16127"})]


@pytest.mark.asyncio
async def test_key_unknown_to_clickup_gets_a_jira_link() -> None:
    clickup = FakeClickUp(McpToolError("ClickUp GET /v2/task/AMCRM-14421 failed: HTTP 401 [OAUTH_027]"))
    info = await TaskLookup(clickup=clickup, jira_url=JIRA).lookup("AMCRM-14421")
    assert info == TaskInfo("AMCRM-14421", "", "https://boards.example.com/browse/AMCRM-14421")
    assert info.line() == "Задача: AMCRM-14421\nhttps://boards.example.com/browse/AMCRM-14421"


@pytest.mark.asyncio
async def test_without_clickup_server_only_the_jira_link_is_given() -> None:
    info = await TaskLookup(clickup=None, jira_url=JIRA).lookup("AMCRM-1")
    assert info == TaskInfo("AMCRM-1", "", "https://boards.example.com/browse/AMCRM-1")
