"""Minimal GitLab REST client for the review channel.

Reads what triage needs (merge request, thread history, author) and performs
only the writes a human approved in Telegram.

Requests carry their own ``User-Agent``: a Cloudflare rule in front of the
GitLab instance blocks the default ``python-httpx`` agent with 403.
"""
from __future__ import annotations

from typing import Any, cast
from urllib.parse import quote

import httpx

USER_AGENT = "nanobot-gitlab-review"


class GitLabApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class GitLabApi:
    def __init__(
        self,
        base_url: str,
        token: str,
        project_path: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self._project = quote(project_path, safe="")
        self._client = client or httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/api/v4",
            headers={
                "PRIVATE-TOKEN": token,
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
            timeout=timeout_s,
        )
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_merge_request(self, iid: int) -> dict[str, Any]:
        return await self._get_object(f"/projects/{self._project}/merge_requests/{iid}")

    async def get_username(self, user_id: int) -> str | None:
        user = await self._get_object(f"/users/{user_id}")
        username = user.get("username")
        return username if isinstance(username, str) else None

    async def get_discussion(self, iid: int, discussion_id: str) -> dict[str, Any]:
        return await self._get_object(
            f"/projects/{self._project}/merge_requests/{iid}/discussions/{quote(discussion_id, safe='')}"
        )

    async def get_commits(self, iid: int) -> list[dict[str, Any]]:
        response = await self._client.get(
            f"/projects/{self._project}/merge_requests/{iid}/commits", params={"per_page": 100}
        )
        if response.status_code >= 400:
            raise GitLabApiError(
                f"GitLab {response.status_code}: {response.text[:300]}", response.status_code
            )
        data: object = response.json()
        if not isinstance(data, list):
            raise GitLabApiError("GitLab returned a non-list response")
        return [cast("dict[str, Any]", item) for item in cast("list[object]", data) if isinstance(item, dict)]

    async def list_discussions(self, iid: int, max_pages: int = 5) -> list[dict[str, Any]]:
        discussions: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            response = await self._client.get(
                f"/projects/{self._project}/merge_requests/{iid}/discussions",
                params={"per_page": 100, "page": page},
            )
            if response.status_code >= 400:
                raise GitLabApiError(
                    f"GitLab {response.status_code}: {response.text[:300]}", response.status_code
                )
            batch: object = response.json()
            if not isinstance(batch, list) or not batch:
                break
            discussions.extend(
                cast("dict[str, Any]", item) for item in cast("list[object]", batch) if isinstance(item, dict)
            )
            if len(cast("list[object]", batch)) < 100:
                break
        return discussions

    async def get_changes(self, iid: int, max_pages: int = 10) -> list[dict[str, Any]]:
        """Changed files of a merge request with their diffs, page by page."""
        changes: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            response = await self._client.get(
                f"/projects/{self._project}/merge_requests/{iid}/diffs",
                params={"per_page": 100, "page": page},
            )
            if response.status_code >= 400:
                raise GitLabApiError(
                    f"GitLab {response.status_code}: {response.text[:300]}", response.status_code
                )
            data: object = response.json()
            if not isinstance(data, list):
                raise GitLabApiError("GitLab returned a non-list diff response")
            items = [cast("dict[str, Any]", item) for item in cast("list[object]", data)
                     if isinstance(item, dict)]
            changes.extend(items)
            if len(items) < 100:
                break
        return changes

    async def create_discussion(
        self, iid: int, body: str, position: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._post(
            f"/projects/{self._project}/merge_requests/{iid}/discussions",
            {"body": body, "position": {**position, "position_type": "text"}},
        )

    async def create_note(self, iid: int, body: str) -> dict[str, Any]:
        return await self._post(
            f"/projects/{self._project}/merge_requests/{iid}/notes", {"body": body}
        )

    async def reply(self, iid: int, discussion_id: str, body: str) -> dict[str, Any]:
        return await self._post(
            f"/projects/{self._project}/merge_requests/{iid}/discussions/"
            f"{quote(discussion_id, safe='')}/notes",
            {"body": body},
        )

    async def approve(self, iid: int, sha: str) -> dict[str, Any]:
        return await self._post(
            f"/projects/{self._project}/merge_requests/{iid}/approve", {"sha": sha}
        )

    async def _get_object(self, path: str) -> dict[str, Any]:
        response = await self._client.get(path)
        return self._object(response)

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self._client.post(path, json=payload)
        return self._object(response)

    @staticmethod
    def _object(response: httpx.Response) -> dict[str, Any]:
        if response.status_code >= 400:
            raise GitLabApiError(
                f"GitLab {response.status_code}: {response.text[:300]}", response.status_code
            )
        data: object = response.json()
        if not isinstance(data, dict):
            raise GitLabApiError("GitLab returned a non-object response")
        return cast("dict[str, Any]", data)
