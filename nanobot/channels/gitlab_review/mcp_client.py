"""One long-lived stdio MCP server the channel calls tools on.

The channel holds no credentials of its own: GitLab and ClickUp are reached
only through their MCP servers, which read their tokens from their own
``.env``. The server process runs in a task that owns its context managers;
callers share the session, and a call that finds the process gone starts a
new one.
"""
from __future__ import annotations

import asyncio
import os
from contextlib import AsyncExitStack
from typing import Any, Protocol

from loguru import logger
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

_PASSED_ENV = ("PATH", "HOME", "LANG", "TZ")


class ToolCaller(Protocol):
    async def call(self, tool: str, arguments: dict[str, Any]) -> str: ...

    async def aclose(self) -> None: ...


class McpToolError(RuntimeError):
    """A tool answered with an error; the message is the server's text."""


class McpServer:
    def __init__(self, name: str, command: list[str], *, timeout_s: float = 60.0) -> None:
        if not command:
            raise ValueError(f"{name}: empty MCP command")
        self._name = name
        self._params = StdioServerParameters(
            command=command[0],
            args=command[1:],
            env={key: os.environ[key] for key in _PASSED_ENV if key in os.environ},
        )
        self._timeout_s = timeout_s
        self._session: ClientSession | None = None
        self._owner: asyncio.Task[None] | None = None
        self._stop: asyncio.Event | None = None
        self._lock = asyncio.Lock()

    async def call(self, tool: str, arguments: dict[str, Any]) -> str:
        """The tool's text answer; :class:`McpToolError` when the tool reports an error."""
        session = await self._ready()
        try:
            result = await asyncio.wait_for(session.call_tool(tool, arguments), self._timeout_s)
        except (OSError, EOFError, asyncio.TimeoutError, RuntimeError) as exc:
            await self._drop()
            raise McpToolError(f"{self._name} MCP: {type(exc).__name__}: {exc}") from exc
        text = "".join(getattr(block, "text", "") for block in result.content)
        if result.isError:
            raise McpToolError(text)
        return text

    async def aclose(self) -> None:
        await self._drop()

    async def _ready(self) -> ClientSession:
        async with self._lock:
            if self._session is not None and self._owner is not None and not self._owner.done():
                return self._session
            await self._drop_locked()
            started: asyncio.Future[ClientSession] = asyncio.get_running_loop().create_future()
            self._stop = asyncio.Event()
            self._owner = asyncio.create_task(self._run(started, self._stop), name=f"mcp-{self._name}")
            self._session = await asyncio.wait_for(started, self._timeout_s)
            return self._session

    async def _run(self, started: asyncio.Future[ClientSession], stop: asyncio.Event) -> None:
        try:
            async with AsyncExitStack() as stack:
                read, write = await stack.enter_async_context(stdio_client(self._params))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                started.set_result(session)
                await stop.wait()
        except Exception as exc:
            if not started.done():
                started.set_exception(McpToolError(f"{self._name} MCP did not start: {exc}"))
            else:
                logger.warning("{} MCP server stopped: {}", self._name, exc)

    async def _drop(self) -> None:
        async with self._lock:
            await self._drop_locked()

    async def _drop_locked(self) -> None:
        owner, stop = self._owner, self._stop
        self._session = self._owner = self._stop = None
        if owner is None or stop is None:
            return
        stop.set()
        try:
            await asyncio.wait_for(owner, 10)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            owner.cancel()
