import httpx
import pytest

from nanobot.channels.gitlab_review.telegram_api import TelegramApi, TelegramApiError

TOKEN = "123:SECRET"


@pytest.mark.asyncio
async def test_transport_error_carries_no_request_and_no_token() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    client = httpx.AsyncClient(
        base_url=f"https://api.telegram.org/bot{TOKEN}", transport=httpx.MockTransport(fail)
    )
    api = TelegramApi(TOKEN, client=client)
    with pytest.raises(TelegramApiError) as caught:
        await api.get_updates(None)
    assert TOKEN not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None
    await client.aclose()
