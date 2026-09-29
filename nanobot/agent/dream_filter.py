# pyright: basic
"""Jev filter for what Dream adds to MEMORY.md.

MEMORY.md is injected whole into every turn, so each line Dream appends is paid for
in every later conversation. Dream tends to append reference material looked up
once (which game supports co-op, a product's specs) next to the facts that really
shape the bot's behaviour. With ``DREAM_FILTER=jev`` every line Dream *inserted*
is scored by Jev (TypeSafe); lines that are plainly not lasting knowledge about the
user are removed before the Dream commit. Edited lines are never touched — dropping
the new version of an edited line would lose the old one too. Headings left without
content are removed with their lines. Any failure leaves Dream's result as it is.

Fork-only module; the gateway calls ``snapshot`` before the Dream run and
``apply`` after it. The Jev key is read the same way as for auto-recall.
"""

from __future__ import annotations

import difflib
import json
import os
import urllib.request
from typing import Any

from loguru import logger

from nanobot.agent.tools.brain_context import _jev_key

_JEV_URL = "https://api.typesafe.ai/v1/systemone"
_TIMEOUT_S = 20.0
_DROP_BELOW = 0.3
_BATCH = 25
_KEEP_Q = (
    "This line added to the assistant's always-loaded memory file is worth keeping there: a lasting "
    "fact about the user or their people, their preferences, rules, commitments or long-running "
    "projects, or how the assistant itself must work (its tools, commands, procedures, setup) — "
    "anything that should shape future conversations. General knowledge looked up once, one-off "
    "events, task progress, chatter or details of a single conversation are not."
)


def enabled() -> bool:
    return os.environ.get("DREAM_FILTER") == "jev"


def snapshot(store: Any) -> str | None:
    """MEMORY.md before the Dream run, or ``None`` when the filter is off."""
    if not enabled():
        return None
    try:
        return store.memory_file.read_text(encoding="utf-8")
    except OSError:
        return ""


def _heading_above(lines: list[str], j: int) -> str:
    for k in range(j - 1, -1, -1):
        if lines[k].lstrip().startswith("#"):
            return lines[k].strip("# ").strip()
    return ""


def _scores(key: str, lines: list[str], idx: list[int]) -> dict[int, float]:
    out: dict[int, float] = {}
    for start in range(0, len(idx), _BATCH):
        chunk = idx[start:start + _BATCH]
        questions = {f"l{j}": {"type": "noul", "instructions": {
            "question": _KEEP_Q, "section": _heading_above(lines, j), "line": lines[j].strip()[:600]}}
            for j in chunk}
        body = json.dumps({"model": "jev-latest", "state": "Assistant memory file edited by a consolidation run.",
                           "questions": questions}).encode()
        req = urllib.request.Request(_JEV_URL, data=body, headers={
            "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as r:
            resp = json.load(r)
        answers = resp.get("answers") or (resp.get("result") or {}).get("answers") or {}
        for j in chunk:
            a = answers.get(f"l{j}")
            if isinstance(a, dict):
                v = next((a[k] for k in ("noul", "probability", "value")
                          if isinstance(a.get(k), (int, float)) and not isinstance(a.get(k), bool)), None)
                if v is not None:
                    out[j] = float(v)
    return out


def _blocks(b: list[str], runs: list[range]) -> tuple[list[int], dict[int, list[int]]]:
    """Inserted runs → single lines to score and whole new sections (keyed by heading line).

    A run that opens with a heading is a section Dream added in one piece; it is kept or
    dropped as a whole, so half of a topic never survives on its own."""
    singles: list[int] = []
    sections: dict[int, list[int]] = {}
    parts: list[list[int]] = []
    for run in runs:
        current: list[int] = []
        for j in run:
            if b[j].lstrip().startswith("#") and current:
                parts.append(current)
                current = []
            current.append(j)
        if current:
            parts.append(current)
    for run in parts:
        body = [j for j in run if b[j].strip()]
        if body and b[body[0]].lstrip().startswith("#") and any(not b[j].lstrip().startswith("#") for j in body):
            sections[body[0]] = list(run)
        else:
            singles += [j for j in body if not b[j].lstrip().startswith("#")]
    return singles, sections


def filter_text(before: str, after: str, key: str) -> tuple[str, list[str]]:
    """New MEMORY.md text and the removed lines."""
    a, b = before.splitlines(), after.splitlines()
    runs = [range(j1, j2) for tag, _i1, _i2, j1, j2
            in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes() if tag == "insert"]
    inserted = [j for run in runs for j in run]
    singles, sections = _blocks(b, runs)
    if not singles and not sections:
        return after, []
    view = list(b)
    for head, run in sections.items():
        view[head] = " ".join(b[j].strip() for j in run if b[j].strip())[:1500]
    scores = _scores(key, view, singles + list(sections))
    drop = {j for j in singles if scores.get(j, 1.0) < _DROP_BELOW}
    for head, run in sections.items():
        if scores.get(head, 1.0) < _DROP_BELOW:
            drop.update(run)
    if not drop:
        return after, []
    added_headings = {j for j in inserted if b[j].lstrip().startswith("#")}
    kept = [(j, line) for j, line in enumerate(b) if j not in drop]
    result: list[str] = []
    for n, (j, line) in enumerate(kept):
        if j in added_headings:
            nxt = next((ln for _k, ln in kept[n + 1:] if ln.strip()), None)
            if nxt is None or nxt.lstrip().startswith("#"):
                drop.add(j)
                continue
        result.append(line)
    text = "\n".join(result) + ("\n" if after.endswith("\n") else "")
    return text, [b[j] for j in sorted(drop)]


def apply(store: Any, before: str | None) -> None:
    """Drop Dream's inserted lines that Jev does not see as lasting memory."""
    if before is None:
        return
    key = _jev_key()
    if not key:
        logger.info("dream filter: no TypeSafe key — skipped")
        return
    try:
        after = store.memory_file.read_text(encoding="utf-8")
        if after == before:
            return
        text, removed = filter_text(before, after, key)
    except Exception as e:
        logger.info("dream filter: jev unavailable ({}) — Dream result kept", e)
        return
    if not removed:
        logger.info("dream filter: all added lines kept")
        return
    store.memory_file.write_text(text, encoding="utf-8")
    logger.info("dream filter: removed {} added line(s): {}", len(removed),
                "; ".join(r.strip()[:60] for r in removed[:5]))
