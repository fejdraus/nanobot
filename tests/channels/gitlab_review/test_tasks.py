import httpx
import pytest

from nanobot.channels.gitlab_review.tasks import TaskInfo, TaskLookup, find_task_key


def test_key_is_found_in_branch_then_title_then_description() -> None:
    assert find_task_key("", "AMCRM-16127", "feat: #AMCRM-1", "") == "AMCRM-16127"
    assert find_task_key("", "feature/no-key", "feat: #AMCRM-16127 thing", "") == "AMCRM-16127"
    assert find_task_key("", "", "fix typo", "Closes AMCRM-14421") == "AMCRM-14421"
    assert find_task_key("", "main", "fix typo", "") is None


def _lookup(handler, **kwargs) -> tuple[TaskLookup, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    values = {"clickup_token": "pk", "clickup_team_id": "9015", "jira_url": "https://boards.example.com/"}
    values.update(kwargs)
    return TaskLookup(client=client, **values), seen


@pytest.mark.asyncio
async def test_clickup_task_gives_name_and_link_and_is_cached() -> None:
    lookup, seen = _lookup(lambda r: httpx.Response(
        200, json={"custom_id": "AMCRM-16127", "name": "Доработать интеграцию", "url": "https://app.clickup.com/t/abc"}
    ))
    first = await lookup.lookup("AMCRM-16127")
    second = await lookup.lookup("AMCRM-16127")
    assert first == second == TaskInfo("AMCRM-16127", "Доработать интеграцию", "https://app.clickup.com/t/abc")
    assert len(seen) == 1
    assert seen[0].url.params["custom_task_ids"] == "true"
    assert seen[0].headers["Authorization"] == "pk"


@pytest.mark.asyncio
async def test_key_unknown_to_clickup_gets_a_jira_link() -> None:
    lookup, _ = _lookup(lambda r: httpx.Response(401, json={"err": "Team not authorized", "ECODE": "OAUTH_027"}))
    info = await lookup.lookup("AMCRM-14421")
    assert info == TaskInfo("AMCRM-14421", "", "https://boards.example.com/browse/AMCRM-14421")
    assert info.line() == "Задача: AMCRM-14421\nhttps://boards.example.com/browse/AMCRM-14421"


@pytest.mark.asyncio
async def test_without_clickup_token_nothing_is_requested() -> None:
    lookup, seen = _lookup(lambda r: httpx.Response(500), clickup_token="")
    info = await lookup.lookup("AMCRM-1")
    assert seen == []
    assert info.url == "https://boards.example.com/browse/AMCRM-1"
