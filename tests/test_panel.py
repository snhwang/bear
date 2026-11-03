"""Tests for bear.panel — Panel interaction modes."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bear.panel import Panel, PanelResponse
from bear.population import AgentFitness, DiffusionStrategy, ExamResult


@pytest.fixture(autouse=True)
def _hash_embeddings(monkeypatch):
    monkeypatch.setenv("BEAR_EMBEDDING_MODEL", "hash")


def _mock_llm(response: str = "[]") -> MagicMock:
    llm = MagicMock()
    resp = MagicMock()
    resp.content = response
    llm.generate = AsyncMock(return_value=resp)
    llm.is_available = MagicMock(return_value=True)
    return llm


class TestPanelBasics:
    def test_create_panel(self, tmp_path):
        panel = Panel(tmp_path / "pop", llm=_mock_llm())
        assert panel.size == 0
        assert panel.agent_names == []

    def test_add_remove(self, tmp_path):
        panel = Panel(tmp_path / "pop", llm=_mock_llm())
        panel.add_agent("alice")
        panel.add_agent("bob")
        assert panel.size == 2
        panel.remove_agent("bob")
        assert panel.size == 1
        assert panel.agent_names == ["alice"]

    def test_leaderboard(self, tmp_path):
        panel = Panel(tmp_path / "pop", llm=_mock_llm())
        panel.add_agent("alice")
        panel.add_agent("bob")
        panel.population.record_exam_score("alice", 0.9)
        panel.population.record_exam_score("bob", 0.2)
        board = panel.leaderboard()
        assert board[0][0] == "alice"

    def test_stats(self, tmp_path):
        panel = Panel(tmp_path / "pop", llm=_mock_llm())
        panel.add_agent("a")
        s = panel.stats
        assert "modes" in s
        assert set(s["modes"]) == {"single", "broadcast", "select", "exam"}


class TestSingle:
    @pytest.mark.asyncio
    async def test_single_fittest(self, tmp_path):
        llm = _mock_llm()
        panel = Panel(tmp_path / "pop", llm=llm)
        panel.add_agent("alice")
        panel.add_agent("bob")
        panel.population.record_exam_score("alice", 0.9)
        panel.population.record_exam_score("bob", 0.2)

        with patch.object(
            panel.population.get_agent("alice"),
            "chat",
            new=AsyncMock(return_value="hello from alice"),
        ):
            resp = await panel.single("hi")
        assert isinstance(resp, PanelResponse)
        assert resp.agent_name == "alice"
        assert resp.message == "hello from alice"

    @pytest.mark.asyncio
    async def test_single_named(self, tmp_path):
        llm = _mock_llm()
        panel = Panel(tmp_path / "pop", llm=llm)
        panel.add_agent("alice")
        panel.add_agent("bob")

        with patch.object(
            panel.population.get_agent("bob"),
            "chat",
            new=AsyncMock(return_value="hello from bob"),
        ):
            resp = await panel.single("hi", agent_name="bob")
        assert resp.agent_name == "bob"

    @pytest.mark.asyncio
    async def test_single_empty_raises(self, tmp_path):
        panel = Panel(tmp_path / "pop", llm=_mock_llm())
        with pytest.raises(ValueError, match="No agents"):
            await panel.single("hello")


class TestBroadcast:
    @pytest.mark.asyncio
    async def test_broadcast_all(self, tmp_path):
        llm = _mock_llm()
        panel = Panel(tmp_path / "pop", llm=llm)
        panel.add_agent("alice")
        panel.add_agent("bob")

        with patch.object(
            panel.population,
            "broadcast",
            new=AsyncMock(return_value={"alice": "a-resp", "bob": "b-resp"}),
        ):
            responses = await panel.broadcast("hey")
        assert len(responses) == 2
        assert all(isinstance(r, PanelResponse) for r in responses)

    @pytest.mark.asyncio
    async def test_broadcast_sorted_by_fitness(self, tmp_path):
        llm = _mock_llm()
        panel = Panel(tmp_path / "pop", llm=llm)
        panel.add_agent("alice")
        panel.add_agent("bob")
        panel.population.record_exam_score("bob", 0.9)
        panel.population.record_exam_score("alice", 0.1)

        with patch.object(
            panel.population,
            "broadcast",
            new=AsyncMock(return_value={"alice": "a", "bob": "b"}),
        ):
            responses = await panel.broadcast("hey")
        assert responses[0].agent_name == "bob"


class TestSelect:
    @pytest.mark.asyncio
    async def test_select_subset(self, tmp_path):
        llm = _mock_llm()
        panel = Panel(tmp_path / "pop", llm=llm)
        panel.add_agent("alice")
        panel.add_agent("bob")
        panel.add_agent("carol")

        with patch.object(
            panel.population,
            "broadcast",
            new=AsyncMock(return_value={"alice": "a", "carol": "c"}),
        ):
            responses = await panel.select("hey", ["alice", "carol"])
        assert len(responses) == 2
        names = {r.agent_name for r in responses}
        assert names == {"alice", "carol"}

    @pytest.mark.asyncio
    async def test_select_unknown_raises(self, tmp_path):
        panel = Panel(tmp_path / "pop", llm=_mock_llm())
        panel.add_agent("alice")
        with pytest.raises(KeyError):
            await panel.select("hi", ["unknown_agent"])


class TestExam:
    @pytest.mark.asyncio
    async def test_exam_no_diffuse(self, tmp_path):
        llm = _mock_llm()
        panel = Panel(tmp_path / "pop", llm=llm)
        panel.add_agent("alice")

        mock_results = [
            ExamResult(
                agent_name="alice",
                question="2+2?",
                expected="4",
                actual="4",
                score=1.0,
                method="contains",
            )
        ]
        with patch.object(
            panel.population,
            "run_exam",
            new=AsyncMock(return_value=mock_results),
        ) as mock_exam:
            results = await panel.exam([("2+2?", "4")])
        assert len(results) == 1
        assert results[0].score == 1.0

    @pytest.mark.asyncio
    async def test_exam_with_diffuse(self, tmp_path):
        llm = _mock_llm()
        panel = Panel(tmp_path / "pop", llm=llm)
        panel.add_agent("alice")
        panel.add_agent("bob")

        mock_results = [
            ExamResult("alice", "Q?", "A", "A", 1.0, "contains"),
            ExamResult("bob", "Q?", "A", "wrong", 0.0, "contains"),
        ]
        with patch.object(
            panel.population, "run_exam",
            new=AsyncMock(return_value=mock_results),
        ), patch.object(
            panel.population, "diffuse",
            new=AsyncMock(),
        ) as mock_diffuse:
            await panel.exam([("Q?", "A")], diffuse=True)
        mock_diffuse.assert_awaited_once()


class TestVote:
    def test_vote_records_win(self, tmp_path):
        panel = Panel(tmp_path / "pop", llm=_mock_llm())
        panel.add_agent("alice")
        panel.vote("alice")
        af = panel.get_fitness("alice")
        assert af.broadcast_wins == 1


class TestEvolve:
    def test_evolve_requires_two(self, tmp_path):
        panel = Panel(tmp_path / "pop", llm=_mock_llm())
        panel.add_agent("alice")
        with pytest.raises(ValueError, match="at least 2"):
            panel.evolve()

    def test_evolve_produces_offspring(self, tmp_path):
        panel = Panel(tmp_path / "pop", llm=_mock_llm())
        panel.add_agent("alice")
        panel.add_agent("bob")
        panel.population.record_exam_score("alice", 0.9)
        panel.population.record_exam_score("bob", 0.3)
        offspring = panel.evolve(n_offspring=1, cull_bottom=0, seed=42)
        assert len(offspring) == 1
        assert panel.size == 3
