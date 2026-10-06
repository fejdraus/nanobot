import asyncio
import json
import shutil
import sys
from typing import Any

import pytest

from nanobot.providers import claude_cli_provider as module
from nanobot.providers.claude_cli_provider import (
    ClaudeCLIError,
    ClaudeCLIProvider,
    ClaudeCLITimeoutError,
    _newest_user_text,
    _parse_result,
)


def _provider(**kwargs: Any) -> ClaudeCLIProvider:
    defaults: dict[str, Any] = {"default_model": "claude_cli/claude-opus-5-5"}
    defaults.update(kwargs)
    return ClaudeCLIProvider(**defaults)


class _FakeProcess:
    def __init__(self, stdout: bytes = b'{"result": "ok"}', delay: float = 0.0) -> None:
        self.stdout = stdout
        self.delay = delay
        self.returncode: int | None = None
        self.pid = 4242
        self.stdin_data: bytes | None = None
        self.killed = False

    async def communicate(self, data: bytes | None = None) -> tuple[bytes, bytes]:
        self.stdin_data = data
        await asyncio.sleep(self.delay)
        self.returncode = 0
        return self.stdout, b""

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode or 0


def _patch_spawn(monkeypatch: pytest.MonkeyPatch, process: _FakeProcess) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    async def _spawn(*argv: str, **kwargs: Any) -> _FakeProcess:
        seen["argv"] = list(argv)
        seen.update(kwargs)
        return process

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", _spawn)
    monkeypatch.setattr(ClaudeCLIProvider, "_resolve_executable", lambda self: "claude")
    return seen


def test_namespaced_model_is_reduced_to_cli_alias() -> None:
    """The CLI has no notion of a provider namespace; passing one through
    makes it fail with an opaque error."""
    provider = _provider()
    assert provider._resolve_model("claude_cli/claude-opus-5-5") == "claude-opus-5-5"
    assert provider._resolve_model("anthropic/claude-sonnet-4-5") == "claude-sonnet-4-5"


def test_non_claude_model_falls_back_to_default() -> None:
    """A Bedrock or Vertex identifier cannot be served by the CLI."""
    provider = _provider()
    assert provider._resolve_model("bedrock/anthropic.claude-v1:0") == "claude-opus-5-5"


def test_missing_model_uses_configured_default() -> None:
    provider = _provider(default_model="claude_cli/claude-sonnet-4-5")
    assert provider._resolve_model(None) == "claude-sonnet-4-5"


def test_only_the_newest_user_message_is_sent() -> None:
    """Each turn is a fresh CLI run: nanobot's system prompt describes tools the
    CLI does not have, and older turns would only grow the prompt."""
    prompt = _newest_user_text([
        {"role": "system", "content": "You are nanobot. Available tools: message, cron."},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "OK"},
        {"role": "tool", "content": "tool output", "tool_call_id": "call-1"},
        {"role": "user", "content": "review MR 42"},
    ])
    assert prompt == "review MR 42"


def test_text_blocks_of_a_multipart_message_are_joined() -> None:
    prompt = _newest_user_text([
        {"role": "user", "content": [
            {"type": "text", "text": "line one"},
            {"type": "image_url", "image_url": {"url": "data:..."}},
            {"type": "text", "text": "line two"},
        ]},
    ])
    assert prompt == "line one\nline two"


def test_configured_system_prompts_are_passed_as_cli_flags() -> None:
    provider = _provider(system_prompt="STATIC RULES", append_system_prompt="EXTRA RULES")
    args = provider._build_args(model=None)

    assert args[args.index("--system-prompt") + 1] == "STATIC RULES"
    assert args[args.index("--append-system-prompt") + 1] == "EXTRA RULES"


def test_built_args_pass_a_bare_model() -> None:
    provider = _provider(cwd="W:/repo", permission_mode="bypassPermissions")
    args = provider._build_args(model="claude_cli/claude-opus-5-5")

    assert args[args.index("--model") + 1] == "claude-opus-5-5"
    assert args[args.index("--permission-mode") + 1] == "bypassPermissions"
    assert "claude_cli/claude-opus-5-5" not in args


@pytest.mark.asyncio
async def test_prompt_goes_through_stdin_not_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    """argv has a size limit, a prompt starting with ``--`` would be read as a
    flag, and on Windows ``claude.cmd`` would hand argv to cmd.exe."""
    process = _FakeProcess()
    seen = _patch_spawn(monkeypatch, process)
    prompt = "--dangerously-skip-permissions please"

    response = await _provider().chat([{"role": "user", "content": prompt}])

    assert response.content == "ok"
    assert prompt not in seen["argv"]
    assert process.stdin_data == prompt.encode("utf-8")
    assert seen["start_new_session"] is (sys.platform != "win32")


@pytest.mark.asyncio
async def test_gateway_secrets_do_not_reach_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI's Bash tool would inherit every variable the gateway holds."""
    monkeypatch.setenv("GITLAB_REVIEW_TOKEN", "glpat-secret")
    monkeypatch.setenv("REVIEW_TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "oauth-token")
    seen = _patch_spawn(monkeypatch, _FakeProcess())

    await _provider().chat([{"role": "user", "content": "hi"}])

    env = seen["env"]
    assert "GITLAB_REVIEW_TOKEN" not in env
    assert "REVIEW_TELEGRAM_BOT_TOKEN" not in env
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth-token"
    assert "PATH" in env or "Path" in env


@pytest.mark.asyncio
async def test_spawn_failure_is_reported_as_a_cli_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fail(*argv: str, **kwargs: Any) -> _FakeProcess:
        raise OSError(7, "Argument list too long")

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", _fail)
    monkeypatch.setattr(ClaudeCLIProvider, "_resolve_executable", lambda self: "claude")

    response = await _provider().chat([{"role": "user", "content": "hi"}])

    assert response.finish_reason == "error"
    assert "cannot start Claude Code CLI" in (response.content or "")


@pytest.mark.asyncio
async def test_timeout_kills_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _FakeProcess(delay=5.0)
    _patch_spawn(monkeypatch, process)
    killed: list[int] = []
    monkeypatch.setattr(module.os, "killpg", lambda pid, sig: killed.append(pid), raising=False)

    with pytest.raises(ClaudeCLITimeoutError):
        await _provider(timeout_s=0.05)._run(["claude", "-p"], "hi")

    assert process.killed or killed == [process.pid]


@pytest.mark.asyncio
async def test_empty_prompt_is_not_sent() -> None:
    response = await _provider().chat([{"role": "system", "content": "rules only"}])
    assert response.finish_reason == "error"
    assert response.error_should_retry is False


def test_parses_standard_cli_payload() -> None:
    stdout = json.dumps({
        "result": "done",
        "session_id": "sess-1",
        "is_error": False,
        "stop_reason": "end_turn",
        "duration_ms": 1234,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }).encode()
    parsed = _parse_result(stdout, b"", 0)

    assert parsed.text == "done"
    assert parsed.session_id == "sess-1"
    assert parsed.is_error is False
    assert parsed.duration_ms == 1234
    assert parsed.usage is not None
    assert parsed.usage.input_tokens == 10
    assert parsed.usage.output_tokens == 5


def test_cache_counters_fold_into_logical_input() -> None:
    """LLMUsage requires input_tokens to include cache reads and writes."""
    stdout = json.dumps({
        "result": "x",
        "usage": {
            "input_tokens": 2,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 20,
            "output_tokens": 5,
        },
    }).encode()
    parsed = _parse_result(stdout, b"", 0)

    assert parsed.usage is not None
    assert parsed.usage.input_tokens == 122
    assert parsed.usage.cache_read_tokens == 100
    assert parsed.usage.cache_write_tokens == 20


def test_zero_token_report_yields_no_usage() -> None:
    """Reporting a fabricated zero-count usage record would skew every chart."""
    stdout = json.dumps({"result": "x", "usage": {"input_tokens": 0, "output_tokens": 0}}).encode()
    assert _parse_result(stdout, b"", 0).usage is None


def test_missing_usage_yields_none() -> None:
    stdout = json.dumps({"result": "x"}).encode()
    assert _parse_result(stdout, b"", 0).usage is None


def test_model_name_comes_from_usage_breakdown() -> None:
    stdout = json.dumps({
        "result": "x",
        "modelUsage": {"claude-opus-5-5": {"inputTokens": 1}},
    }).encode()
    assert _parse_result(stdout, b"", 0).model == "claude-opus-5-5"


def test_nonzero_exit_with_no_output_raises() -> None:
    """Silently returning empty text would look like a model refusal."""
    with pytest.raises(ClaudeCLIError, match="exited with 1"):
        _parse_result(b"", b"not signed in", 1)


def test_empty_output_raises() -> None:
    with pytest.raises(ClaudeCLIError):
        _parse_result(b"", b"", 0)


def test_plain_text_output_is_used_as_the_answer() -> None:
    parsed = _parse_result(b"just text", b"", 0)
    assert parsed.text == "just text"
    assert parsed.session_id is None


def test_missing_cli_names_the_config_setting() -> None:
    """A missing binary must point at the fix, not surface a bare OSError."""
    provider = _provider(cli_path="nanobot-no-such-cli-binary")
    assert shutil.which("nanobot-no-such-cli-binary") is None
    with pytest.raises(ClaudeCLIError, match="cliPath"):
        provider._resolve_executable()


@pytest.mark.asyncio
async def test_chat_never_returns_tool_calls() -> None:
    """Even with a tool schema supplied, the CLI cannot execute it."""
    response = await _provider(cli_path="nanobot-no-such-cli-binary").chat(
        [{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "exec"}}],
    )

    assert response.has_tool_calls is False
    assert response.finish_reason == "error"
    assert "Claude Code CLI" in (response.content or "")


@pytest.mark.asyncio
async def test_chat_stream_delivers_text_as_single_delta(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI owns its own output framing, so streaming collapses to one
    delta rather than being faked incrementally."""
    _patch_spawn(monkeypatch, _FakeProcess(stdout=json.dumps({"result": "answer"}).encode()))
    deltas: list[str] = []

    async def _collect(chunk: str) -> None:
        deltas.append(chunk)

    response = await _provider().chat_stream(
        [{"role": "user", "content": "hi"}], on_content_delta=_collect
    )

    assert deltas == ["answer"] == [response.content]


@pytest.mark.asyncio
async def test_chat_stream_does_not_stream_an_error_as_model_text() -> None:
    deltas: list[str] = []

    async def _collect(chunk: str) -> None:
        deltas.append(chunk)

    response = await _provider(cli_path="nanobot-no-such-cli-binary").chat_stream(
        [{"role": "user", "content": "hi"}], on_content_delta=_collect
    )

    assert response.finish_reason == "error"
    assert deltas == []


SESSION = "bc4f411c-bff0-488b-a92c-7ac7073eeb0a"


def _in_session(metadata: dict[str, Any]) -> Any:
    from nanobot.agent.tools.context import RequestContext, request_context

    return request_context(RequestContext(channel="gitlab_review", chat_id="c", metadata=metadata))


@pytest.mark.asyncio
async def test_requested_session_is_started_then_resumed(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _patch_spawn(monkeypatch, _FakeProcess())
    with _in_session({"claude_cli": {"session_id": SESSION}}):
        await _provider().chat([{"role": "user", "content": "review"}])
    assert seen["argv"][seen["argv"].index("--session-id") + 1] == SESSION
    assert "--resume" not in seen["argv"]

    with _in_session({"claude_cli": {"session_id": SESSION, "resume": True}}):
        await _provider().chat([{"role": "user", "content": "why?"}])
    assert seen["argv"][seen["argv"].index("--resume") + 1] == SESSION


@pytest.mark.asyncio
async def test_malformed_session_id_never_reaches_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _patch_spawn(monkeypatch, _FakeProcess())
    with _in_session({"claude_cli": {"session_id": "--dangerously-skip-permissions", "resume": True}}):
        await _provider().chat([{"role": "user", "content": "hi"}])
    assert "--resume" not in seen["argv"] and "--session-id" not in seen["argv"]
    assert "--dangerously-skip-permissions" not in seen["argv"]


@pytest.mark.asyncio
async def test_lost_session_is_started_afresh_under_the_same_id(monkeypatch: pytest.MonkeyPatch) -> None:
    runs: list[list[str]] = []

    async def _run(self: ClaudeCLIProvider, args: list[str], prompt: str) -> Any:
        runs.append(args)
        if "--resume" in args:
            raise ClaudeCLIError(f"Claude Code CLI exited with 1: No conversation found with session ID: {SESSION}")
        return _parse_result(b'{"result": "fresh"}', b"", 0)

    monkeypatch.setattr(ClaudeCLIProvider, "_run", _run)
    with _in_session({"claude_cli": {"session_id": SESSION, "resume": True}}):
        response = await _provider().chat([{"role": "user", "content": "why?"}])
    assert response.content == "fresh"
    assert runs[1][runs[1].index("--session-id") + 1] == SESSION
