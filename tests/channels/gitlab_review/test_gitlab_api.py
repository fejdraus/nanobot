import json
from typing import Any

import pytest

from nanobot.channels.gitlab_review.gitlab_api import GitLabApi, GitLabApiError
from nanobot.channels.gitlab_review.mcp_client import McpToolError

PROJECT = "group/project"


class FakeMcp:
    def __init__(self, answer) -> None:
        self.answer = answer
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, tool: str, arguments: dict[str, Any]) -> str:
        self.calls.append((tool, arguments))
        return self.answer(tool, arguments)

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_pages_are_read_through_the_mcp_server_until_a_short_one() -> None:
    def answer(tool: str, arguments: dict[str, Any]) -> str:
        page = arguments["params"]["page"]
        return json.dumps([{"id": f"{page}-{n}"} for n in range(100 if page == 1 else 3)])

    mcp = FakeMcp(answer)
    discussions = await GitLabApi(mcp, PROJECT).list_discussions(42)
    assert len(discussions) == 103
    assert [call[1]["params"]["page"] for call in mcp.calls] == [1, 2]
    assert all(
        tool == "gitlab_api_get" and args["project"] == PROJECT
        and args["path"] == "/project/merge_requests/42/discussions"
        for tool, args in mcp.calls
    )


@pytest.mark.asyncio
async def test_gitlab_status_survives_the_mcp_error_text() -> None:
    def answer(tool: str, arguments: dict[str, Any]) -> str:
        raise McpToolError('GitLab 400 Bad Request: {"message":"400 Bad request - Note {:line_code=>["can\'t be blank"]}"}')

    with pytest.raises(GitLabApiError) as caught:
        await GitLabApi(FakeMcp(answer), PROJECT).create_discussion(
            42, "text", {"base_sha": "a", "start_sha": "b", "head_sha": "c", "new_path": "f", "new_line": 3}
        )
    assert caught.value.status == 400
    assert "line_code" in str(caught.value)

