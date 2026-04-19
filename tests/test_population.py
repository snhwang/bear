"""Tests for bear.population — Population, exam scoring, and knowledge diffusion."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bear.population import (
    AgentFitness,
    DiffusionResult,
    DiffusionStrategy,
    ExamResult,
    Population,
    extract_answer,
    score_contains,
    score_exact,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_llm(response: str = "[]") -> MagicMock:
    """Create a mock LLM that returns the given response."""
    llm = MagicMock()
    resp = MagicMock()
    resp.content = response
    llm.generate = AsyncMock(return_value=resp)
    llm.is_available = MagicMock(return_value=True)
    return llm


def _make_exam_results(
    agents: list[str],
    questions: list[tuple[str, str]],
    scores: dict[str, list[float]],
    answers: dict[str, list[str]] | None = None,
) -> list[ExamResult]:
    """Build ExamResult list from compact description.

    Args:
        agents: Agent names.
        questions: (question, expected) pairs.
        scores: agent_name -> list of scores per question.
        answers: agent_name -> list of actual answers. Defaults to expected
            for correct, "wrong" for incorrect.
    """
    results = []
    for name in agents:
        for i, (q, expected) in enumerate(questions):
            sc = scores[name][i]
            if answers and name in answers:
                actual = answers[name][i]
            else:
                actual = expected if sc > 0 else "wrong"
            results.append(ExamResult(
                agent_name=name,
                question=q,
                expected=expected,
                actual=actual,
                score=sc,
                method="contains",
            ))
    return results


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


class TestScoring:
    def test_exact_match(self):
        assert score_exact("Paris", "Paris") == 1.0
        assert score_exact("Paris", "paris") == 1.0
        assert score_exact("Paris", "London") == 0.0

    def test_contains(self):
        assert score_contains("Paris", "The answer is Paris!") == 1.0
        assert score_contains("Paris", "London") == 0.0


# ---------------------------------------------------------------------------
# AgentFitness
# ---------------------------------------------------------------------------


class TestAgentFitness:
    def test_empty_fitness(self):
        af = AgentFitness(name="x")
        assert af.average_score == 0.0
        assert af.fitness == 0.0

    def test_fitness_weights(self):
        af = AgentFitness(
            name="x",
            exam_scores=[1.0, 0.5],
            broadcast_wins=2,
            interactions=4,
        )
        # 0.7 * 0.75 + 0.3 * 0.5 = 0.525 + 0.15 = 0.675
        assert af.fitness == pytest.approx(0.675)


# ---------------------------------------------------------------------------
# extract_answer
# ---------------------------------------------------------------------------


class TestExtractAnswer:
    def test_format(self):
        r = ExamResult(
            agent_name="alice",
            question="What is 2+2?",
            expected="4",
            actual="The answer is 4.",
            score=1.0,
            method="contains",
        )
        obs = extract_answer(r)
        assert "What is 2+2?" in obs
        assert "The answer is 4." in obs


# ---------------------------------------------------------------------------
# DiffusionStrategy
# ---------------------------------------------------------------------------


class TestDiffusionStrategy:
    def test_from_string(self):
        assert DiffusionStrategy("prestige") == DiffusionStrategy.PRESTIGE
        assert DiffusionStrategy("conformist") == DiffusionStrategy.CONFORMIST
        assert DiffusionStrategy("proximity") == DiffusionStrategy.PROXIMITY


# ---------------------------------------------------------------------------
# Population basics
# ---------------------------------------------------------------------------


class TestPopulationBasics:
    def test_create_empty(self, tmp_path):
        pop = Population(tmp_path / "pop", llm=_mock_llm())
        assert pop.size == 0
        assert pop.generation == 0

    def test_add_agent(self, tmp_path):
        pop = Population(tmp_path / "pop", llm=_mock_llm())
        pop.add_agent("alice")
        assert pop.size == 1
        assert "alice" in pop.agent_names

    def test_add_duplicate_raises(self, tmp_path):
        pop = Population(tmp_path / "pop", llm=_mock_llm())
        pop.add_agent("alice")
        with pytest.raises(ValueError, match="already exists"):
            pop.add_agent("alice")

    def test_remove_agent(self, tmp_path):
        pop = Population(tmp_path / "pop", llm=_mock_llm())
        pop.add_agent("alice")
        pop.remove_agent("alice")
        assert pop.size == 0

    def test_leaderboard_ordering(self, tmp_path):
        pop = Population(tmp_path / "pop", llm=_mock_llm())
        pop.add_agent("alice")
        pop.add_agent("bob")
        pop.record_exam_score("alice", 0.9)
        pop.record_exam_score("bob", 0.3)
        board = pop.leaderboard()
        assert board[0][0] == "alice"
        assert board[1][0] == "bob"

    def test_persistence(self, tmp_path):
        pop_dir = tmp_path / "pop"
        llm = _mock_llm()
        pop = Population(pop_dir, llm=llm)
        pop.add_agent("alice")
        pop.record_exam_score("alice", 0.8)
        pop._save()  # record_exam_score doesn't auto-save

        # Reload from disk
        pop2 = Population(pop_dir, llm=llm)
        assert pop2.size == 1
        assert pop2.get_fitness("alice").exam_scores == [0.8]


# ---------------------------------------------------------------------------
# Diffusion — prestige
# ---------------------------------------------------------------------------


class TestDiffusePrestige:
    @pytest.mark.asyncio
    async def test_prestige_basic(self, tmp_path):
        llm = _mock_llm(json.dumps([]))
        pop = Population(tmp_path / "pop", llm=llm)
        pop.add_agent("alice")
        pop.add_agent("bob")
        pop.add_agent("carol")
        pop.record_exam_score("alice", 1.0)
        pop.record_exam_score("bob", 0.5)
        pop.record_exam_score("carol", 0.0)

        results = _make_exam_results(
            agents=["alice", "bob", "carol"],
            questions=[("What is 2+2?", "4")],
            scores={"alice": [1.0], "bob": [0.5], "carol": [0.0]},
            answers={"alice": ["4"], "bob": ["maybe 4"], "carol": ["wrong"]},
        )

        dr = await pop.diffuse(results, strategy="prestige")
        assert dr.strategy == DiffusionStrategy.PRESTIGE
        assert "alice" in dr.teachers
        assert dr.observations_sent > 0

    @pytest.mark.asyncio
    async def test_prestige_no_self_teaching(self, tmp_path):
        """A teacher should not also appear as a learner."""
        llm = _mock_llm(json.dumps([]))
        pop = Population(tmp_path / "pop", llm=llm)
        pop.add_agent("alice")
        pop.add_agent("bob")
        pop.record_exam_score("alice", 1.0)
        pop.record_exam_score("bob", 0.0)

        results = _make_exam_results(
            agents=["alice", "bob"],
            questions=[("Q?", "A")],
            scores={"alice": [1.0], "bob": [0.0]},
        )

        dr = await pop.diffuse(results, strategy="prestige")
        for teacher in dr.teachers:
            assert teacher not in dr.learners

    @pytest.mark.asyncio
    async def test_too_few_agents(self, tmp_path):
        """With 1 agent, diffusion returns empty result."""
        llm = _mock_llm(json.dumps([]))
        pop = Population(tmp_path / "pop", llm=llm)
        pop.add_agent("alice")
        pop.record_exam_score("alice", 1.0)

        dr = await pop.diffuse([], strategy="prestige")
        assert dr.observations_sent == 0


# ---------------------------------------------------------------------------
# Diffusion — conformist
# ---------------------------------------------------------------------------


class TestDiffuseConformist:
    @pytest.mark.asyncio
    async def test_conformist_majority_wins(self, tmp_path):
        llm = _mock_llm(json.dumps([]))
        pop = Population(tmp_path / "pop", llm=llm)
        for name in ["a", "b", "c", "d"]:
            pop.add_agent(name)
            pop.record_exam_score(name, 0.5)

        # a, b, c all say "4" (correct), d says "5" (wrong)
        results = _make_exam_results(
            agents=["a", "b", "c", "d"],
            questions=[("What is 2+2?", "4")],
            scores={"a": [1.0], "b": [1.0], "c": [1.0], "d": [0.0]},
            answers={"a": ["4"], "b": ["4"], "c": ["4"], "d": ["5"]},
        )

        dr = await pop.diffuse(results, strategy="conformist")
        assert dr.strategy == DiffusionStrategy.CONFORMIST
        assert "d" in dr.learners
        assert dr.observations_sent == 1  # only d needs teaching


# ---------------------------------------------------------------------------
# Diffusion — proximity
# ---------------------------------------------------------------------------


class TestDiffuseProximity:
    @pytest.mark.asyncio
    async def test_proximity_neighbor_teaches(self, tmp_path):
        llm = _mock_llm(json.dumps([]))
        pop = Population(tmp_path / "pop", llm=llm)
        pop.add_agent("top")
        pop.add_agent("mid")
        pop.add_agent("bot")
        pop.record_exam_score("top", 1.0)
        pop.record_exam_score("mid", 0.5)
        pop.record_exam_score("bot", 0.0)

        results = _make_exam_results(
            agents=["top", "mid", "bot"],
            questions=[("Q?", "A")],
            scores={"top": [1.0], "mid": [0.5], "bot": [0.0]},
        )

        dr = await pop.diffuse(results, strategy="proximity")
        assert dr.strategy == DiffusionStrategy.PROXIMITY
        # bot learns from mid, mid learns from top
        assert dr.observations_sent >= 1

    @pytest.mark.asyncio
    async def test_proximity_no_downward_diffusion(self, tmp_path):
        """A lower agent should not teach a higher agent."""
        llm = _mock_llm(json.dumps([]))
        pop = Population(tmp_path / "pop", llm=llm)
        pop.add_agent("top")
        pop.add_agent("bot")
        pop.record_exam_score("top", 1.0)
        pop.record_exam_score("bot", 0.0)

        # top scores 1.0, bot scores 0.0
        results = _make_exam_results(
            agents=["top", "bot"],
            questions=[("Q?", "A")],
            scores={"top": [1.0], "bot": [0.0]},
        )

        dr = await pop.diffuse(results, strategy="proximity")
        # top (rank 0) should not be a learner
        assert "top" not in dr.learners
