"""Auto-recall: inject relevant brain memory into each turn's context.

Fork-only tool. It owns a ``RuntimeContextProvider`` that runs
``brain recall <user_text>`` before every turn and appends the top facts to
the current prompt, so the model always has relevant long-term memory without
having to decide to call the brain tool itself (GraphRAG at runtime).

Enabled only when the brain MCP server is configured for the bot AND the
environment variable ``BRAIN_AUTO_RECALL=1`` is set (per-bot opt-in via the
systemd unit). Everything else is tunable through env vars with safe defaults.

This adds a NEW file only — it does not modify any upstream module, so it does
not conflict on ``git merge upstream/main``. It relies on the upstream
``RuntimeContextProvider`` mechanism (Tool.runtime_context_provider()).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.context import RequestContext
from nanobot.runtime_context import RuntimeContextBlock, RuntimeContextProvider

_DEFAULT_CLI_DIR = "/home/dietpi/clawd/brain"
_RECALL_TIMEOUT_S = 8.0
_MIN_QUERY_LEN = 4
_MAX_CONTEXT_LINES = 24


def _brain_db_from_config(config: Any) -> str | None:
    """Read BRAIN_DB from the bot's brain MCP server config, if present."""
    try:
        tools = getattr(config, "tools", None)
        mcp = getattr(tools, "mcp_servers", None) if tools is not None else None
        if mcp is None:
            return None
        brain = mcp.get("brain") if isinstance(mcp, dict) else getattr(mcp, "brain", None)
        if brain is None:
            return None
        env = brain.get("env") if isinstance(brain, dict) else getattr(brain, "env", None)
        if isinstance(env, dict):
            return env.get("BRAIN_DB")
    except Exception:
        pass
    return None


class BrainAutoRecallTool(Tool):
    """Injects relevant brain memory into each turn (auto-recall, internal)."""

    _plugin_discoverable = True

    def __init__(self, brain_db: str, cli_dir: str) -> None:
        self._brain_db = brain_db
        self._cli_dir = cli_dir

    @classmethod
    def enabled(cls, ctx: Any) -> bool:
        if os.environ.get("BRAIN_AUTO_RECALL", "0") != "1":
            return False
        return _brain_db_from_config(getattr(ctx, "config", None)) is not None

    @classmethod
    def create(cls, ctx: Any) -> Tool:
        brain_db = (
            _brain_db_from_config(getattr(ctx, "config", None))
            or os.environ.get("BRAIN_DB", "thufir_brain")
        )
        cli_dir = os.environ.get("BRAIN_CLI_DIR", _DEFAULT_CLI_DIR)
        return cls(brain_db=brain_db, cli_dir=cli_dir)

    @property
    def name(self) -> str:
        return "brain_auto_recall"

    @property
    def description(self) -> str:
        return (
            "Internal: relevant long-term memory is injected into your context "
            "automatically every turn. Do NOT call this directly — use the "
            "`brain` tool for explicit store/search/recall/neighbors/path."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    @property
    def read_only(self) -> bool:
        return True

    def runtime_context_provider(self) -> RuntimeContextProvider | None:
        return self._provide

    async def _provide(self, request: RequestContext) -> RuntimeContextBlock | None:
        text = (request.original_user_text or "").strip()
        if len(text) < _MIN_QUERY_LEN:
            return None

        python = str(Path(self._cli_dir) / ".venv" / "bin" / "python3")
        cli = str(Path(self._cli_dir) / "cli.py")
        env = dict(os.environ)
        env["BRAIN_DB"] = self._brain_db

        try:
            proc = await asyncio.create_subprocess_exec(
                python,
                cli,
                "recall",
                text,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                stdin=asyncio.subprocess.DEVNULL,
                env=env,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=_RECALL_TIMEOUT_S)
        except Exception:
            return None

        content = (out or b"").decode("utf-8", "replace").strip()
        if not content or "No matching facts" in content:
            return None

        lines = content.splitlines()
        trimmed = "\n".join(lines[:_MAX_CONTEXT_LINES])
        return RuntimeContextBlock(source="brain_auto_recall", content=trimmed)

    async def execute(self, **kwargs: Any) -> str:
        return (
            "brain_auto_recall runs automatically each turn and injects relevant "
            "memory into your context. Use the `brain` tool for explicit memory ops."
        )
