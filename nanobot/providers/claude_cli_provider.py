"""LLM provider backed by the Claude Code CLI instead of the Anthropic API.

Why this exists
---------------
Anthropic's subscription terms require that subscription credentials be used
through Claude Code itself. Calling the Messages API with a subscription token,
or stamping ``User-Agent: claude-code/...`` on a direct API call, is not
supported. The supported path is to invoke the official CLI, which owns the
authentication, and let it do the inference.

This provider therefore shells out to ``claude -p`` and treats the CLI as the
model. It is deliberately *not* a provider that speaks the Messages API with a
different key: no token is read, stored, or forwarded.

What this backend can and cannot do
-----------------------------------
The CLI runs its own agent loop with its own tools (``Read``, ``Bash``,
``Grep``, ...). It does not accept a tool schema from the caller, so nanobot's
tools cannot be handed to it as function-calling definitions. Rather than
pretend otherwise, this provider runs the CLI in a mode where the CLI's own
tools do the work and nanobot keeps its own tools for the parts of a turn it
can still drive natively.

That trade-off is why :meth:`ClaudeCLIProvider.chat` never returns tool calls:
the agent loop must not emit tool calls it expects the CLI to have executed.

One turn, one run
-----------------
Every call starts a CLI run with only the newest user message. A channel that
wants a conversation to continue names a Claude session in the inbound
message metadata (``claude_cli.session_id``, plus ``claude_cli.resume`` to
continue it); the CLI then keeps the history itself. The prompt goes through stdin, never through argv: argv has a size limit, a text
starting with ``--`` would be read as a CLI flag, and on Windows the npm
``claude.cmd`` shim would pass it through ``cmd.exe`` unescaped.

The CLI gets a filtered environment, so secrets the gateway holds for other
purposes never reach the tools the model runs.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sys
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from loguru import logger

from nanobot.agent.tools.context import current_request_context
from nanobot.providers.base import LLMProvider, LLMResponse, LLMUsage

DEFAULT_TIMEOUT_S = 3600.0

DEFAULT_MODEL = "claude-opus-5-5"

_PROVIDER_PREFIX = "claude_cli/"

_ENV_NAMES = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TERM", "TMPDIR", "TMP", "TEMP", "TZ",
    "NO_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "USERPROFILE", "USERNAME",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
    "HOMEDRIVE", "HOMEPATH",
})
_ENV_PREFIXES = ("LC_", "XDG_", "CLAUDE_", "ANTHROPIC_", "NODE_", "NPM_CONFIG_")

SESSION_METADATA_KEY = "claude_cli"

_MISSING_SESSION = "No conversation found"


class ClaudeCLIError(RuntimeError):
    """The Claude Code CLI failed, was missing, or produced unusable output."""


class ClaudeCLITimeoutError(ClaudeCLIError):
    """The CLI did not finish within the configured timeout."""


@dataclass(frozen=True)
class ClaudeCLIResult:
    """One parsed ``claude -p --output-format json`` response."""

    text: str
    session_id: str | None
    is_error: bool
    duration_ms: int
    usage: LLMUsage | None
    model: str | None
    stop_reason: str | None


class ClaudeCLIProvider(LLMProvider):
    """Drive the Claude Code CLI as nanobot's inference backend.

    Authentication is entirely the CLI's: nanobot never reads OAuth tokens, an
    API key, or any credential file. If the CLI is not signed in, the turn
    fails with the CLI's own message.
    """

    def __init__(
        self,
        *,
        provider_name: str = "claude_cli",
        default_model: str = DEFAULT_MODEL,
        cli_path: str = "claude",
        cwd: str | Path | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        allowed_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
        permission_mode: str | None = None,
        system_prompt: str | None = None,
        append_system_prompt: str | None = None,
        settings_file: str | Path | None = None,
        extra_args: list[str] | None = None,
        max_turns: int | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        super().__init__(provider_name=provider_name)
        self.default_model = default_model
        self.cli_path = cli_path
        self.cwd = str(cwd) if cwd is not None else None
        self.timeout_s = float(timeout_s)
        self.allowed_tools = list(allowed_tools) if allowed_tools else None
        self.disallowed_tools = list(disallowed_tools) if disallowed_tools else None
        self.permission_mode = permission_mode
        self.system_prompt = system_prompt
        self.append_system_prompt = append_system_prompt
        self.settings_file = str(settings_file) if settings_file is not None else None
        self.extra_args = list(extra_args or [])
        self.max_turns = max_turns
        self.env = dict(env) if env else None

    def supports_native_compaction(self, model: str | None = None) -> bool:
        return False

    def supports_pre_request_compaction(self, model: str | None = None) -> bool:
        return False

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        """Run one CLI turn and return its final text.

        ``tools`` is intentionally ignored: the CLI runs its own agent loop
        with its own tool set.
        """
        prompt = _newest_user_text(messages)
        if not prompt.strip():
            return LLMResponse(
                content="Claude Code CLI: no user message to send.",
                finish_reason="error",
                error_kind="invalid_request",
                error_should_retry=False,
            )

        session = _requested_session()
        try:
            result = await self._run_in_session(model, prompt, session)
        except ClaudeCLIError as exc:
            return LLMResponse(
                content=f"Claude Code CLI error: {exc}",
                finish_reason="error",
                error_kind="connection",
                error_type=type(exc).__name__,
                error_should_retry=False,
            )

        if result.is_error:
            return LLMResponse(
                content=result.text or "Claude Code CLI reported an error.",
                finish_reason="error",
                usage=result.usage,
                error_kind="server_error",
                error_should_retry=False,
            )

        return LLMResponse(
            content=result.text,
            finish_reason="stop",
            usage=result.usage,
            generation_ms=result.duration_ms,
        )

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
        on_thinking_delta: Callable[[str], Awaitable[None]] | None = None,
        on_tool_call_delta: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        """Run one CLI turn and deliver its text as a single delta.

        The base class would wrap :meth:`chat` in the stream idle timeout,
        which defaults to 90s. A review turn drives the CLI's own agent loop
        over a whole diff and legitimately runs for minutes, so the timeout
        comes from this provider instead.

        ``on_thinking_delta`` and ``on_tool_call_delta`` are unused: the CLI
        renders its own progress and runs its own tool calls.
        """
        _ = on_thinking_delta, on_tool_call_delta
        response = await self.chat(
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            tool_choice=tool_choice,
        )
        if on_content_delta and response.content and response.finish_reason != "error":
            await on_content_delta(response.content)
        return response

    def get_default_model(self) -> str:
        return self.default_model

    async def _run_in_session(
        self, model: str | None, prompt: str, session: tuple[str, bool] | None
    ) -> ClaudeCLIResult:
        """Run the CLI, continuing the requested session when there is one.

        A session the CLI no longer has (cleaned up, other machine) is started
        afresh under the same id, so later turns still find it; the caller's
        prompt is written to make sense without the lost history.
        """
        if session is None:
            return await self._run(self._build_args(model=model), prompt)
        session_id, resume = session
        if not resume:
            return await self._run(self._build_args(model=model, session_id=session_id), prompt)
        try:
            return await self._run(
                self._build_args(model=model, session_id=session_id, resume=True), prompt
            )
        except ClaudeCLIError as exc:
            if _MISSING_SESSION not in str(exc):
                raise
            logger.warning("claude_cli: session {} is gone, starting it afresh", session_id)
            return await self._run(self._build_args(model=model, session_id=session_id), prompt)

    def _build_args(
        self, *, model: str | None, session_id: str | None = None, resume: bool = False
    ) -> list[str]:
        args: list[str] = [self.cli_path, "-p", "--output-format", "json"]
        if session_id:
            args += ["--resume" if resume else "--session-id", session_id]

        selected = self._resolve_model(model)
        if selected:
            args += ["--model", selected]

        if self.allowed_tools:
            args += ["--allowedTools", *self.allowed_tools]
        if self.disallowed_tools:
            args += ["--disallowedTools", *self.disallowed_tools]
        if self.permission_mode:
            args += ["--permission-mode", self.permission_mode]
        if self.max_turns is not None:
            args += ["--max-turns", str(self.max_turns)]
        if self.system_prompt:
            args += ["--system-prompt", self.system_prompt]
        if self.append_system_prompt:
            args += ["--append-system-prompt", self.append_system_prompt]
        if self.settings_file:
            args += ["--settings", self.settings_file]
        args += self.extra_args
        return args

    def _resolve_model(self, model: str | None) -> str:
        """Return a bare ``claude-*`` alias the CLI can actually serve.

        nanobot model identifiers carry a provider namespace and may name a
        Bedrock or Vertex variant the CLI cannot serve. Passing such a value
        through makes the CLI fail with an opaque error, so anything that is
        not a bare Claude alias falls back to the configured default.
        """
        candidate = _strip_namespace(model) or _strip_namespace(self.default_model)
        if candidate.startswith("claude-"):
            return candidate
        logger.warning(
            "claude_cli: {!r} is not a Claude CLI model alias; using {}.", model, DEFAULT_MODEL
        )
        return DEFAULT_MODEL

    def _child_env(self) -> dict[str, str]:
        """Environment for the CLI: what it needs to run, nothing else the gateway holds."""
        env = {
            name: value
            for name, value in os.environ.items()
            if name.upper() in _ENV_NAMES or name.upper().startswith(_ENV_PREFIXES)
        }
        if self.env:
            env.update(self.env)
        return env

    def _resolve_executable(self) -> str:
        """Locate the CLI, falling back to an absolute path when not on PATH."""
        if os.path.isabs(self.cli_path) or os.sep in self.cli_path:
            return self.cli_path
        found = shutil.which(self.cli_path)
        if found:
            return found
        raise ClaudeCLIError(
            f"Claude Code CLI {self.cli_path!r} not found on PATH. "
            "Install Claude Code and sign in, or set providers.claude_cli.cliPath."
        )

    async def _run(self, args: list[str], prompt: str) -> ClaudeCLIResult:
        """Execute the CLI with *prompt* on stdin and parse its JSON result."""
        executable = self._resolve_executable()
        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                *args[1:],
                cwd=self.cwd,
                env=self._child_env(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=sys.platform != "win32",
            )
        except OSError as exc:
            raise ClaudeCLIError(f"cannot start Claude Code CLI: {exc}") from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(prompt.encode("utf-8")), timeout=self.timeout_s
            )
        except TimeoutError as exc:
            await _terminate(process)
            raise ClaudeCLITimeoutError(
                f"Claude Code CLI exceeded {self.timeout_s:.0f}s"
            ) from exc
        except asyncio.CancelledError:
            await _terminate(process)
            raise

        return _parse_result(stdout, stderr, process.returncode or 0)


async def _terminate(process: asyncio.subprocess.Process) -> None:
    """Kill the CLI together with the tool processes and MCP servers it started."""
    if process.returncode is None:
        if sys.platform == "win32":
            process.kill()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    await process.wait()


def _as_dict(value: object) -> dict[str, Any] | None:
    """Narrow decoded JSON to a string-keyed mapping.

    ``isinstance(value, dict)`` alone narrows to ``dict[Unknown, Unknown]``,
    which then poisons every attribute read on the result.
    """
    if not isinstance(value, dict):
        return None
    mapping = cast("Mapping[Any, Any]", value)
    return {str(key): item for key, item in mapping.items()}


def _load_json(text: str) -> object:
    """Decode *text* as JSON, or return ``None`` when it is not JSON."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _strip_namespace(model: str | None) -> str:
    """Reduce ``provider/model`` to the bare model alias.

    Both this provider's own namespace and ``anthropic/`` are stripped: a user
    may reach for either, and neither is a model name the CLI accepts.
    """
    candidate = (model or "").strip()
    for prefix in (_PROVIDER_PREFIX, "anthropic/"):
        if candidate.startswith(prefix):
            candidate = candidate[len(prefix) :]
            break
    return candidate


def _parse_result(stdout: bytes, stderr: bytes, returncode: int) -> ClaudeCLIResult:
    """Parse ``--output-format json`` output from the CLI.

    The CLI prints the JSON object on stdout; diagnostics and auth failures go
    to stderr. A non-zero exit with no parsable stdout is an error, not an empty
    answer -- silently returning empty text would look like a model refusal.
    """
    text = stdout.decode("utf-8", errors="replace").strip()

    payload = _as_dict(_load_json(text)) if text else None

    if payload is None:
        detail = stderr.decode("utf-8", errors="replace").strip()
        if returncode != 0:
            raise ClaudeCLIError(
                f"Claude Code CLI exited with {returncode}: {detail or text[:500]}"
            )
        if not text:
            raise ClaudeCLIError("Claude Code CLI produced no output")
        return ClaudeCLIResult(
            text=text,
            session_id=None,
            is_error=False,
            duration_ms=0,
            usage=None,
            model=None,
            stop_reason=None,
        )

    result_text = payload.get("result")
    if not isinstance(result_text, str):
        result_text = ""

    session_id = payload.get("session_id")
    stop_reason = payload.get("stop_reason")
    return ClaudeCLIResult(
        text=result_text,
        session_id=session_id if isinstance(session_id, str) and session_id else None,
        is_error=bool(payload.get("is_error")),
        duration_ms=int(payload.get("duration_ms") or 0),
        usage=_parse_usage(payload.get("usage")),
        model=_model_name(payload),
        stop_reason=stop_reason if isinstance(stop_reason, str) else None,
    )


def _model_name(payload: dict[str, Any]) -> str | None:
    """Extract the served model name from the CLI's usage breakdown."""
    model_usage = _as_dict(payload.get("modelUsage"))
    if model_usage:
        first = next(iter(model_usage))
        if first.strip():
            return first
    single = payload.get("model")
    if isinstance(single, str) and single.strip():
        return single
    return None


def _parse_usage(raw: object) -> LLMUsage | None:
    """Map the CLI's token counters onto :class:`LLMUsage`.

    The CLI reports cache reads and writes separately from logical input, and
    ``LLMUsage`` requires the logical total to include them.
    """
    counters = _as_dict(raw)
    if counters is None:
        return None

    def _int(key: str) -> int:
        value = counters.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            return 0
        return max(0, value)

    output = _int("output_tokens")
    input_tokens = _int("input_tokens") + _int("cache_read_input_tokens") + _int(
        "cache_creation_input_tokens"
    )
    if input_tokens == 0 and output == 0:
        return None

    cache_read = _int("cache_read_input_tokens") or None
    cache_write = _int("cache_creation_input_tokens") or None
    total = max(input_tokens + output, _int("total_tokens"))
    return LLMUsage(
        input_tokens=input_tokens,
        output_tokens=output,
        total_tokens=total,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        reported_tokens=total,
        estimated_tokens=0,
        request_count=1,
    )


def _requested_session() -> tuple[str, bool] | None:
    """The Claude session the current request asks for, as ``(id, resume)``.

    Only a well-formed UUID is accepted: the value ends up in argv, and
    anything else could be read by the CLI as a flag.
    """
    ctx = current_request_context()
    raw = _as_dict(ctx.metadata.get(SESSION_METADATA_KEY)) if ctx is not None else None
    if raw is None:
        return None
    value = raw.get("session_id")
    if not isinstance(value, str):
        return None
    try:
        session_id = str(uuid.UUID(value))
    except ValueError:
        logger.warning("claude_cli: ignoring malformed session id {!r}", value)
        return None
    return session_id, bool(raw.get("resume"))


def _newest_user_text(messages: list[dict[str, Any]]) -> str:
    """Text of the newest user message: the only thing a fresh CLI run needs."""
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for block in cast("list[object]", content):
                item = _as_dict(block)
                if item is not None and item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
            return "\n".join(part for part in parts if part)
        return ""
    return ""


__all__ = [
    "ClaudeCLIError",
    "ClaudeCLITimeoutError",
    "ClaudeCLIProvider",
    "ClaudeCLIResult",
    "DEFAULT_MODEL",
    "SESSION_METADATA_KEY",
]
