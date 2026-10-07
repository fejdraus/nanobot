import asyncio

import pytest

from nanobot.channels.gitlab_review.vpn import verification_code, vpn_request


async def _control(answers: dict[str, str]) -> tuple[asyncio.AbstractServer, str, list[str]]:
    seen: list[str] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = (await reader.readline()).decode().strip()
        seen.append(line)
        writer.write((answers.get(line.split()[0], "error: ?") + "\n").encode())
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"127.0.0.1:{port}", seen


@pytest.mark.asyncio
async def test_request_gets_one_line_answer() -> None:
    server, endpoint, seen = await _control({"status": "needs_code"})
    async with server:
        assert await vpn_request(endpoint, "status") == "needs_code"
    assert seen == ["status"]


@pytest.mark.asyncio
async def test_unreachable_control_port_is_down() -> None:
    server, endpoint, _ = await _control({})
    server.close()
    await server.wait_closed()
    assert await vpn_request(endpoint, "status", timeout_s=2) == "down"


def test_only_a_bare_six_digit_message_is_a_code() -> None:
    assert verification_code(" 123456 ") == "123456"
    assert verification_code("12345") is None
    assert verification_code("публикуй 123456") is None
    assert verification_code("!6320") is None


def test_reconnect_request_is_recognised_in_both_languages() -> None:
    from nanobot.channels.gitlab_review.vpn import asks_reconnect

    assert asks_reconnect("переподключи vpn")
    assert asks_reconnect(" Reconnect VPN ")
    assert not asks_reconnect("переподключи vpn и проверь !6320")

