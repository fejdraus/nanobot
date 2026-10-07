"""Talk to the VPN container that gives the reviewer access to Jira.

The company VPN lets traffic through only after a one-time code from the
approver's authenticator app. The container cannot ask for it itself — until
the code is in, it has no way out — so the channel does: it polls the
container's control port, says in Telegram when a code is needed, and passes
on the six digits the approver sends back.

The control protocol is one line each way: ``status`` answers ``ok``,
``needs_code`` or ``down``; ``code <digits>`` answers ``ok`` or ``error: …``;
``reconnect`` restarts the tunnel. A VPN that stays down is reconnected by the
channel on its own; the approver can ask for it too.
"""
from __future__ import annotations

import asyncio
import re

CODE_RE = re.compile(r"^\s*(\d{6})\s*$")
RECONNECT_RE = re.compile(r"^\s*(?:переподключи|перезапусти|reconnect|restart)\s+(?:vpn|впн)\s*$", re.IGNORECASE)


async def vpn_request(endpoint: str, line: str, timeout_s: float = 45.0) -> str:
    """Send one request to the control port at ``host:port``; ``down`` if it is unreachable."""
    host, _, port = endpoint.rpartition(":")
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, int(port)), timeout_s)
    except (OSError, ValueError, TimeoutError):
        return "down"
    try:
        writer.write(line.encode() + b"\n")
        await writer.drain()
        answer = await asyncio.wait_for(reader.readline(), timeout_s)
    except (OSError, TimeoutError):
        return "down"
    finally:
        writer.close()
    return answer.decode(errors="replace").strip() or "down"


def asks_reconnect(text: str) -> bool:
    return RECONNECT_RE.match(text or "") is not None


def verification_code(text: str) -> str | None:
    """The six digits of a message that is nothing but a verification code."""
    match = CODE_RE.match(text or "")
    return match.group(1) if match else None
