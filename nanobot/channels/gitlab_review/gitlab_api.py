"""GitLab for the review channel, reached only through the GitLab MCP server.

Reads what triage needs (merge request, thread history, author) and performs
only the writes a human approved in Telegram. The channel never sees a GitLab
token: the MCP server holds it.

Reads go through ``gitlab_api_get``, which hands back GitLab's own response
shape; writes use the server's dedicated write tools.
"""
from __future__ import annotations

import json
import re
from typing import Any, cast

from nanobot.channels.gitlab_review.mcp_client import McpToolError, ToolCaller

_STATUS_RE = re.compile(r"GitLab (\d{3})\b")


class GitLabApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GitLabApi:
    def __init__(self, mcp: ToolCaller, project_path: str) -> None:
        self._mcp = mcp
        self._project = project_path

    async def aclose(self) -> None:
        await self._mcp.aclose()

    async def get_merge_request(self, iid: int) -> dict[str, Any]:
        return self._as_object(await self._read(f"/project/merge_requests/{iid}"))

    async def get_username(self, user_id: int) -> str | None:
        user = self._as_object(await self._read(f"/users/{user_id}"))
        username = user.get("username")
        return username if isinstance(username, str) else None

    async def get_discussion(self, iid: int, discussion_id: str) -> dict[str, Any]:
        return self._as_object(await self._read(f"/project/merge_requests/{iid}/discussions/{discussion_id}"))

    async def list_open_merge_requests(self, updated_after: str, max_pages: int = 3) -> list[dict[str, Any]]:
        """Open merge requests updated after *updated_after* (ISO 8601), newest first."""
        return await self._pages(
            "/project/merge_requests",
            {"state": "opened", "updated_after": updated_after, "order_by": "updated_at"},
            max_pages,
        )

    async def get_commits(self, iid: int) -> list[dict[str, Any]]:
        return self._as_list(await self._read(f"/project/merge_requests/{iid}/commits", {"per_page": 100}))

    async def list_discussions(self, iid: int, max_pages: int = 5) -> list[dict[str, Any]]:
        return await self._pages(f"/project/merge_requests/{iid}/discussions", {}, max_pages)

    async def get_raw_diff(self, iid: int) -> str:
        """The full unified diff of a merge request, large files included."""
        return await self._call("gitlab_api_get", {
            "project": self._project, "path": f"/project/merge_requests/{iid}/raw_diffs",
        })

    async def get_commit(self, sha: str) -> dict[str, Any]:
        return self._as_object(await self._read(f"/project/repository/commits/{sha}"))

    async def compare(self, base: str, head: str) -> list[dict[str, Any]]:
        """Changed files between two commits, with their diffs."""
        data = self._as_object(await self._read("/project/repository/compare", {"from": base, "to": head}))
        diffs = data.get("diffs")
        return [cast("dict[str, Any]", item) for item in cast("list[object]", diffs) if isinstance(item, dict)] \
            if isinstance(diffs, list) else []

    async def get_changes(self, iid: int, max_pages: int = 10) -> list[dict[str, Any]]:
        """Changed files of a merge request with their diffs, page by page."""
        return await self._pages(f"/project/merge_requests/{iid}/diffs", {}, max_pages)

    async def create_discussion(self, iid: int, body: str, position: dict[str, Any]) -> None:
        arguments = {"project": self._project, "iid": iid, "body": body}
        arguments.update({key: value for key, value in position.items() if value is not None})
        await self._call("gitlab_create_mr_discussion", arguments)

    async def create_note(self, iid: int, body: str) -> None:
        await self._call("gitlab_create_mr_note", {"project": self._project, "iid": iid, "body": body})

    async def reply(self, iid: int, discussion_id: str, body: str) -> None:
        await self._call("gitlab_reply_to_mr_discussion", {
            "project": self._project, "iid": iid, "discussion_id": discussion_id, "body": body,
        })

    async def approve(self, iid: int, sha: str) -> None:
        await self._call("gitlab_approve_mr", {"project": self._project, "iid": iid, "sha": sha})

    async def _pages(self, path: str, params: dict[str, Any], max_pages: int) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            batch = self._as_list(await self._read(path, {**params, "per_page": 100, "page": page}))
            found.extend(batch)
            if len(batch) < 100:
                break
        return found

    async def _read(self, path: str, params: dict[str, Any] | None = None) -> object:
        text = await self._call("gitlab_api_get", {"project": self._project, "path": path, "params": params or {}})
        try:
            return json.loads(text)
        except ValueError as exc:
            raise GitLabApiError(f"GitLab returned non-JSON for {path}: {text[:200]}") from exc

    async def _call(self, tool: str, arguments: dict[str, Any]) -> str:
        try:
            return await self._mcp.call(tool, arguments)
        except McpToolError as exc:
            match = _STATUS_RE.search(str(exc))
            raise GitLabApiError(str(exc)[:500], int(match.group(1)) if match else None) from exc

    @staticmethod
    def _as_object(data: object) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise GitLabApiError("GitLab returned a non-object response")
        return cast("dict[str, Any]", data)

    @staticmethod
    def _as_list(data: object) -> list[dict[str, Any]]:
        if not isinstance(data, list):
            raise GitLabApiError("GitLab returned a non-list response")
        return [cast("dict[str, Any]", item) for item in cast("list[object]", data) if isinstance(item, dict)]
