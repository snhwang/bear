"""Tests for bear.memory."""

import json
from unittest.mock import AsyncMock, MagicMock

from bear.memory import ExperienceEvent, LLMMemoryExtractor


def _llm(content: str) -> MagicMock:
    llm = MagicMock()
    resp = MagicMock()
    resp.content = content
    llm.generate = AsyncMock(return_value=resp)
    return llm


async def _extract(content: str, **kwargs):
    extractor = LLMMemoryExtractor(batch_size=1, **kwargs)
    event = ExperienceEvent(agent_id="alice", query="q", response="r")
    return await extractor.process(event, _llm(content))


async def test_extracts_a_memory():
    out = await _extract(json.dumps([{"content": "Alice likes tea.", "topics": ["tea"]}]))
    assert len(out) == 1
    assert "Alice likes tea." in out[0].content
    assert "tea" in out[0].tags


async def test_bare_strings_are_skipped_not_raised():
    assert await _extract(json.dumps(["the user likes tea"])) == []


async def test_string_topics_are_ignored():
    out = await _extract(json.dumps([{"content": "Alice likes tea.", "topics": "tea"}]))
    assert len(out) == 1
    assert "t" not in out[0].tags


async def test_mandatory_tags_are_reserved_by_default():
    out = await _extract(json.dumps([{"content": "Alice worries about safety.",
                                      "topics": ["safety", "worry"]}]))
    assert "safety" not in out[0].tags
    assert "safety" not in out[0].scope.tags
    assert "worry" in out[0].tags


async def test_env_mandatory_tags_are_reserved(monkeypatch):
    monkeypatch.setenv("BEAR_MANDATORY_TAGS", "crisis")
    out = await _extract(json.dumps([{"content": "x", "topics": ["crisis", "safety"]}]))
    assert "crisis" not in out[0].tags
    assert "safety" in out[0].tags


async def test_explicit_reserved_tags_still_apply():
    out = await _extract(json.dumps([{"content": "x", "topics": ["alice", "bob"]}]),
                         reserved_tags=["bob"])
    assert "bob" not in out[0].scope.tags
