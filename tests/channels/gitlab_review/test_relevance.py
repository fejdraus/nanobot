import json

import httpx
import pytest

from nanobot.channels.gitlab_review.relevance import JevScorer


@pytest.mark.asyncio
async def test_notes_are_scored_in_batches_and_answers_read() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        answers = {
            key: {"noul": 0.9 if "relevant" in question["instructions"] else 0.1}
            for key, question in body["questions"].items()
        }
        return httpx.Response(200, json={"answers": answers})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    notes = [(f"note{i}.md", "relevant" if i == 21 else "other") for i in range(25)]
    scores = await JevScorer("key", client=client).score("MR state", notes)
    await client.aclose()
    assert scores is not None and len(scores) == 25
    assert scores["note21.md"] == 0.9 and scores["note0.md"] == 0.1
    assert sorted(len(batch["questions"]) for batch in seen) == [5, 20]
    assert all(batch["state"] == "MR state" for batch in seen)


@pytest.mark.asyncio
async def test_failure_gives_none_so_tags_stand() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503)))
    assert await JevScorer("key", client=client).score("s", [("a.md", "x")]) is None
    await client.aclose()
