"""Ask Jev (TypeSafe) which memory notes matter for a merge request.

Picking lessons by path globs and keywords is cheap but blunt: broadly tagged
notes reach every review, and a note whose tags miss the change never does.
Jev reads the MR (title, description, changed files, an excerpt of the diff)
and gives each note a probability of helping the reviewer. The channel uses it
to drop tagged notes that do not concern the change and to add untagged ones
that do. Any failure returns ``None`` and the tag-based choice stands.
"""
from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any, cast

import httpx
from loguru import logger

API_URL = "https://api.typesafe.ai/v1/systemone"
BATCH = 20
PARALLEL = 4
QUESTION = (
    "A code reviewer is about to review the change described above. The memory note below records "
    "a lesson, convention or mechanism of this project. Would knowing it help judge THIS change — "
    "does it concern the same code, module, platform mechanism or kind of defect? Rules about how "
    "to write code or run tools, not about what this code does, do not count. Note: "
)


class JevScorer:
    """Score memory notes for one merge request with the TypeSafe API."""

    def __init__(self, api_key: str, *, client: httpx.AsyncClient | None = None, timeout_s: float = 60.0) -> None:
        self._key = api_key
        self._client = client or httpx.AsyncClient(timeout=timeout_s)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def score(self, state: str, notes: Sequence[tuple[str, str]]) -> dict[str, float] | None:
        """Probability per note name, or ``None`` if Jev could not be asked."""
        batches = [notes[start:start + BATCH] for start in range(0, len(notes), BATCH)]
        gate = asyncio.Semaphore(PARALLEL)

        async def ask(batch: Sequence[tuple[str, str]]) -> dict[str, float]:
            questions = {
                f"n{index}": {"type": "noul", "instructions": f"{QUESTION}{name}: {description}"}
                for index, (name, description) in enumerate(batch)
            }
            async with gate:
                response = await self._client.post(
                    API_URL,
                    headers={"Authorization": f"Bearer {self._key}"},
                    json={"model": "jev-latest", "state": state, "questions": questions},
                )
            response.raise_for_status()
            answers = _answers(response.json())
            return {name: _probability(answers.get(f"n{index}")) for index, (name, _) in enumerate(batch)}

        try:
            results = await asyncio.gather(*(ask(batch) for batch in batches))
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Jev unavailable, lessons picked by tags only: {}", type(exc).__name__)
            return None
        scores: dict[str, float] = {}
        for result in results:
            scores.update(result)
        return scores


def _answers(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    data = cast("dict[str, Any]", payload)
    answers = data.get("answers")
    if not isinstance(answers, dict):
        result = data.get("result")
        answers = cast("dict[str, Any]", result).get("answers") if isinstance(result, dict) else None
    return cast("dict[str, Any]", answers) if isinstance(answers, dict) else {}


def _probability(answer: object) -> float:
    if not isinstance(answer, dict):
        return 0.0
    fields = cast("dict[str, Any]", answer)
    for key in ("noul", "probability", "value"):
        value = fields.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool):
            return float(value)
    return 0.0
