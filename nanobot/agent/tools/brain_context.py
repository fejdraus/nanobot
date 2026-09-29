# pyright: basic
"""Auto-recall: inject relevant brain memory into each turn's context.

Fork-only tool. It owns a ``RuntimeContextProvider`` that runs
``brain recall <user_text>`` before every turn and appends the top facts to
the current prompt, so the model always has relevant long-term memory without
having to decide to call the brain tool itself (GraphRAG at runtime).

Enabled only when the brain MCP server is configured for the bot AND the
environment variable ``BRAIN_AUTO_RECALL=1`` is set (per-bot opt-in via the
systemd unit). Everything else is tunable through env vars with safe defaults.

With ``BRAIN_RECALL_FILTER=jev`` the recalled facts are first scored by Jev
(TypeSafe): a fact that is plainly not needed for the user's message is dropped
before it reaches the prompt. The key comes from ``TYPESAFE_API_KEY`` or from
the ``.env`` file named by ``TYPESAFE_ENV_FILE``. Any failure keeps the recall
unfiltered.

This adds a NEW file only — it does not modify any upstream module, so it does
not conflict on ``git merge upstream/main``. It relies on the upstream
``RuntimeContextProvider`` mechanism (Tool.runtime_context_provider()).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.context import RequestContext
from nanobot.runtime_context import RuntimeContextBlock, RuntimeContextProvider

_DEFAULT_CLI_DIR = "/home/dietpi/clawd/brain"
_RECALL_TIMEOUT_S = 8.0
_MIN_QUERY_LEN = 4
_MAX_CONTEXT_LINES = 24
_JEV_URL = "https://api.typesafe.ai/v1/systemone"
_JEV_TIMEOUT_S = 12.0
_JEV_DROP_BELOW = 0.2
_JEV_FACT_Q = (
    "This remembered fact is needed to answer or act on the user's message correctly "
    "(it is about the same subject and changes or supports the reply)."
)


def _jev_key() -> str | None:
    key = os.environ.get("TYPESAFE_API_KEY")
    if key:
        return key
    path = os.environ.get("TYPESAFE_ENV_FILE")
    if not path:
        return None
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r"^TYPESAFE_API_KEY=[\"']?([^\"'\s]+)", text, re.M)
    return m.group(1) if m else None


def parse_recall(raw: str) -> list[dict[str, Any]]:
    """``cli.py recall`` output -> section headers and facts with their continuation lines."""
    entries: list[dict[str, Any]] = []
    for line in (raw or "").splitlines():
        s = line.strip()
        if not s or s.startswith("🧠"):
            continue
        if s.startswith("--") and s.endswith("--"):
            entries.append({"header": s.strip("- ").strip()})
        elif s.startswith("[") or (s.startswith("- ") and entries):
            entries.append({"fact": s if s.startswith("- ") else "- " + s, "more": []})
        elif entries and "fact" in entries[-1]:
            entries[-1]["more"].append(s)
    return entries


def fact_text(entry: dict[str, Any]) -> str:
    head = re.sub(r"^- (?:\[[0-9a-f]+\]\s*)?(?:\[[\d.]+\]\s*)?", "", entry["fact"])
    return " ".join([head, *entry["more"]]).strip()


def filter_recall(entries: list[dict[str, Any]], probs: dict[int, float]) -> list[dict[str, Any]]:
    """Drop facts Jev scored below the floor; keep headers only when a fact follows them.

    Jev probabilities are compressed (needed facts land around 0.4-0.7), so only the
    obvious noise is cut rather than keeping just the confident ones."""
    kept = [e for i, e in enumerate(entries)
            if "header" in e or (probs.get(i) is None or probs[i] >= _JEV_DROP_BELOW)]
    return [e for j, e in enumerate(kept)
            if "fact" in e or any("fact" in n for n in kept[j + 1:j + 2])]


def render_recall(entries: list[dict[str, Any]], limit_lines: int) -> str:
    lines: list[str] = []
    for e in entries:
        if "header" in e:
            lines.append(e["header"])
        else:
            lines.append(e["fact"])
            lines.extend("  " + m for m in e["more"])
        if len(lines) >= limit_lines:
            break
    return "\n".join(lines) if any("fact" in e for e in entries) else ""


def _jev_scores(key: str, message: str, entries: list[dict[str, Any]]) -> dict[int, float]:
    questions = {f"f{i}": {"type": "noul", "instructions": {"question": _JEV_FACT_Q, "fact": fact_text(e)[:600]}}
                 for i, e in enumerate(entries) if "fact" in e}
    body = json.dumps({"model": "jev-latest", "state": message[:4000], "questions": questions}).encode()
    req = urllib.request.Request(_JEV_URL, data=body, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=_JEV_TIMEOUT_S) as r:
        resp = json.load(r)
    answers = resp.get("answers") or (resp.get("result") or {}).get("answers") or {}
    out: dict[int, float] = {}
    for i in range(len(entries)):
        a = answers.get(f"f{i}")
        if isinstance(a, dict):
            v = next((a[k] for k in ("noul", "probability", "value")
                      if isinstance(a.get(k), (int, float)) and not isinstance(a.get(k), bool)), None)
            if v is not None:
                out[i] = float(v)
    return out


def _brain_db_from_config(config: Any) -> str | None:
    """Read BRAIN_DB from the bot's brain MCP server config, if present.

    ``ctx.config`` in ToolContext is the ToolsConfig object directly (i.e.
    already ``config.tools``), not the full Config. Handle both shapes: look
    for ``mcp_servers`` on config itself first, then fall back to
    ``config.tools.mcp_servers`` in case a full Config is ever passed.
    """
    try:
        if config is None:
            return None
        mcp = getattr(config, "mcp_servers", None)
        if mcp is None:
            tools = getattr(config, "tools", None)
            mcp = getattr(tools, "mcp_servers", None) if tools is not None else None
        if not mcp:
            return None
        brain = mcp.get("brain") if hasattr(mcp, "get") else getattr(mcp, "brain", None)
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

        key = _jev_key() if os.environ.get("BRAIN_RECALL_FILTER") == "jev" else None
        if key:
            entries = parse_recall(content)
            try:
                probs = await asyncio.to_thread(_jev_scores, key, text, entries)
            except Exception as e:
                logger.info("brain recall filter: jev unavailable ({}) — unfiltered", e)
                probs = None
            if probs is not None:
                kept = filter_recall(entries, probs)
                total = sum(1 for e in entries if "fact" in e)
                logger.info("brain recall filter: kept {}/{} facts",
                            sum(1 for e in kept if "fact" in e), total)
                filtered = render_recall(kept, _MAX_CONTEXT_LINES)
                if not filtered:
                    return None
                return RuntimeContextBlock(source="brain_auto_recall", content=filtered)

        lines = content.splitlines()
        trimmed = "\n".join(lines[:_MAX_CONTEXT_LINES])
        return RuntimeContextBlock(source="brain_auto_recall", content=trimmed)

    async def execute(self, **kwargs: Any) -> str:
        return (
            "brain_auto_recall runs automatically each turn and injects relevant "
            "memory into your context. Use the `brain` tool for explicit memory ops."
        )
