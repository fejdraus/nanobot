"""Minimal Telegram Bot API client for review drafts and approvals.

Only one chat is ever served, and only approved senders in it: messages from
any other chat or sender are dropped before they are parsed, so nobody else
can approve a publication under the reviewer's name.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import httpx


class TelegramApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class TelegramMessage:
    update_id: int
    chat_id: str
    text: str
    sender_id: str = ""
    reply_to: int | None = None


class TelegramApi:
    def __init__(
        self,
        bot_token: str,
        *,
        client: httpx.AsyncClient | None = None,
        api_base: str = "https://api.telegram.org",
    ) -> None:
        self._client = client or httpx.AsyncClient(
            base_url=f"{api_base.rstrip('/')}/bot{bot_token}", timeout=60.0
        )
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def send_message(self, chat_id: str, text: str) -> int | None:
        """Send *text*; return the Telegram message id so replies can be traced back."""
        response = await self._client.post(
            "/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        )
        result = self._result(response)
        message_id = cast("dict[str, Any]", result).get("message_id") if isinstance(result, dict) else None
        return message_id if isinstance(message_id, int) else None

    async def get_updates(self, offset: int | None, timeout_s: int = 30) -> list[TelegramMessage]:
        params: dict[str, Any] = {"timeout": timeout_s, "allowed_updates": '["message"]'}
        if offset is not None:
            params["offset"] = offset
        response = await self._client.get(
            "/getUpdates", params=params, timeout=timeout_s + 15
        )
        result = self._result(response)
        messages: list[TelegramMessage] = []
        for raw in cast("list[object]", result if isinstance(result, list) else []):
            if not isinstance(raw, dict):
                continue
            update = cast("dict[str, Any]", raw)
            update_id = update.get("update_id")
            message = update.get("message")
            if not isinstance(update_id, int) or not isinstance(message, dict):
                continue
            message = cast("dict[str, Any]", message)
            chat = message.get("chat")
            sender = message.get("from")
            text = message.get("text")
            chat_id: object = cast("dict[str, Any]", chat).get("id") if isinstance(chat, dict) else None
            sender_id: object = (
                cast("dict[str, Any]", sender).get("id") if isinstance(sender, dict) else None
            )
            replied = message.get("reply_to_message")
            reply_to: object = (
                cast("dict[str, Any]", replied).get("message_id") if isinstance(replied, dict) else None
            )
            messages.append(
                TelegramMessage(
                    update_id=update_id,
                    chat_id=str(chat_id) if chat_id is not None else "",
                    text=text if isinstance(text, str) else "",
                    sender_id=str(sender_id) if sender_id is not None else "",
                    reply_to=reply_to if isinstance(reply_to, int) else None,
                )
            )
        return messages

    @staticmethod
    def _result(response: httpx.Response) -> object:
        try:
            data: object = response.json()
        except ValueError as exc:
            raise TelegramApiError(f"Telegram {response.status_code}: not JSON") from exc
        if not isinstance(data, dict) or not cast("dict[str, Any]", data).get("ok"):
            raise TelegramApiError(f"Telegram {response.status_code}: {str(cast(object, data))[:300]}")
        return cast("dict[str, Any]", data).get("result")
