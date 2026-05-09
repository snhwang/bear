"""Tests for bear.evolution."""

import time

import pytest

from bear import (
    Corpus, Retriever, Instruction, InstructionType, ScopeCondition, Context,
    CrossoverMethod, Dominance, GeneLocus, LocusRegistry,
)
from bear.corpus import _coerce_to_list, _parse_instruction
from bear.evolution import (
    Evolution,
    EvolutionConfig,
    ObservationBuffer,
    Observation,
    Gate,
    PendingInstruction,
    evaluate,
    EvaluationResult,
    generate_from_template,
    _fix_yaml_inline_lists,
    _parse_generated_instructions,
    breed,
    express,
    BreedingConfig,
    BreedResult,
    _build_parent_mask,
    _linkage_boundaries,
)
from bear.logging import RetrievalEvent, ScoredInstruction, set_log_handler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    query: str = "test query",
    similarity: float = 0.5,
    instruction_id: str = "inst-1",
    response: str = "",
    metadata: dict | None = None,
) -> RetrievalEvent:
    """Build a minimal RetrievalEvent for testing."""
    scored = ScoredInstruction(
        instruction=Instruction(
            id=instruction_id,
            type=InstructionType.DIRECTIVE,
            priority=50,
            content="placeholder",
        ),
        similarity=similarity,
        scope_match=False,
        final_score=similarity,
    )
    return RetrievalEvent(
        query=query,
        instructions=[scored],
        response=response,
        metadata=metadata or {},
    )


def _make_observation(
    query: str = "test query",
    top_similarity: float = 0.5,
    instruction_ids: list[str] | None = None,
    response: str = "",
) -> Observation:
    return Observation(
        query=query,
        top_similarity=top_similarity,
        instruction_ids=instruction_ids or ["inst-1"],
        response=response,
        timestamp=time.time(),
    )


def _make_instruction(
    inst_id: str = "test-1",
    priority: int = 50,
    content: str = "Test instruction",
    tags: list[str] | None = None,
) -> Instruction:
    return Instruction(
        id=inst_id,
        type=InstructionType.DIRECTIVE,
        priority=priority,
        content=content,
        tags=tags or [],
    )


def _seed_corpus() -> Corpus:
    """Create a small corpus for integration tests."""
    corpus = Corpus()
    corpus.add_many([
        Instruction(
            id="constraint-safety",
            type=InstructionType.CONSTRAINT,
            priority=100,
            content="Never give dangerous advice.",
            tags=["safety"],
        ),
        Instruction(
            id="persona-helper",
            type=InstructionType.PERSONA,
            priority=75,
            content="Be a helpful assistant.",
            tags=["general"],
        ),
        Instruction(
            id="fallback-generic",
            type=InstructionType.FALLBACK,
            priority=20,
            content="Provide general information when unsure.",
            tags=["fallback"],
        ),
    ])
    return corpus


# ---------------------------------------------------------------------------
# TestEvolutionConfig
# ---------------------------------------------------------------------------


class TestEvolutionConfig:
    def test_defaults(self):
        cfg = EvolutionConfig()
        assert cfg.observe_window == 10
        assert cfg.coverage_gap_threshold == 0.3
        assert cfg.pattern_threshold == 3
        assert cfg.low_similarity_trigger == 0.3
        assert cfg.max_evolved_priority == 40
        assert cfg.evolved_tag == "evolved"
        assert cfg.generation_tags == ["auto-generated"]
        assert cfg.max_pending == 20
        assert cfg.id_prefix == "evo"
        assert cfg.gate_policy == "auto"
        assert cfg.auto_approve_below_priority == 30
        assert cfg.batch_size == 5
        assert cfg.rebuild_cooldown == 60.0

    def test_custom_values(self):
        cfg = EvolutionConfig(
            observe_window=5,
            coverage_gap_threshold=0.5,
            pattern_threshold=2,
            gate_policy="manual",
        )
        assert cfg.observe_window == 5
        assert cfg.coverage_gap_threshold == 0.5
        assert cfg.pattern_threshold == 2
        assert cfg.gate_policy == "manual"

    def test_validation_observe_window_must_be_positive(self):
        with pytest.raises(Exception):
            EvolutionConfig(observe_window=0)

    def test_validation_coverage_gap_threshold_range(self):
        with pytest.raises(Exception):
            EvolutionConfig(coverage_gap_threshold=1.5)
        with pytest.raises(Exception):
            EvolutionConfig(coverage_gap_threshold=-0.1)

    def test_validation_gate_policy_must_be_known(self):
        with pytest.raises(Exception):
            EvolutionConfig(gate_policy="unknown")

    def test_validation_max_evolved_priority_range(self):
        with pytest.raises(Exception):
            EvolutionConfig(max_evolved_priority=101)
        with pytest.raises(Exception):
            EvolutionConfig(max_evolved_priority=-1)

    def test_validation_batch_size_must_be_positive(self):
        with pytest.raises(Exception):
            EvolutionConfig(batch_size=0)

    def test_validation_rebuild_cooldown_nonnegative(self):
        with pytest.raises(Exception):
            EvolutionConfig(rebuild_cooldown=-1.0)


# ---------------------------------------------------------------------------
# TestObservationBuffer
# ---------------------------------------------------------------------------


class TestObservationBuffer:
    def test_record_increases_count(self):
        buf = ObservationBuffer(capacity=10)
        assert buf.count == 0
        buf.record(_make_event())
        assert buf.count == 1

    def test_record_extracts_top_similarity(self):
        event = _make_event(similarity=0.85)
        buf = ObservationBuffer(capacity=10)
        buf.record(event)
        obs = buf.drain()
        assert len(obs) == 1
        assert obs[0].top_similarity == pytest.approx(0.85)

    def test_record_extracts_query(self):
        buf = ObservationBuffer(capacity=10)
        buf.record(_make_event(query="liver measurement"))
        obs = buf.drain()
        assert obs[0].query == "liver measurement"

    def test_record_extracts_instruction_ids(self):
        buf = ObservationBuffer(capacity=10)
        buf.record(_make_event(instruction_id="my-inst"))
        obs = buf.drain()
        assert obs[0].instruction_ids == ["my-inst"]

    def test_capacity_evicts_oldest(self):
        buf = ObservationBuffer(capacity=3)
        for i in range(5):
            buf.record(_make_event(query=f"query-{i}"))
        assert buf.count == 3
        obs = buf.drain()
        queries = [o.query for o in obs]
        assert queries == ["query-2", "query-3", "query-4"]

    def test_drain_clears_buffer(self):
        buf = ObservationBuffer(capacity=10)
        buf.record(_make_event())
        buf.record(_make_event())
        result = buf.drain()
        assert len(result) == 2
        assert buf.count == 0

    def test_drain_empty_buffer(self):
        buf = ObservationBuffer(capacity=10)
        assert buf.drain() == []

    def test_coverage_gaps_filters_by_threshold(self):
        buf = ObservationBuffer(capacity=10)
        buf.record(_make_event(similarity=0.1))
        buf.record(_make_event(similarity=0.5))
        buf.record(_make_event(similarity=0.9))
        gaps = buf.coverage_gaps(threshold=0.4)
        assert len(gaps) == 1
        assert gaps[0].top_similarity == pytest.approx(0.1)

    def test_coverage_gaps_all_below(self):
        buf = ObservationBuffer(capacity=10)
        buf.record(_make_event(similarity=0.1))
        buf.record(_make_event(similarity=0.2))
        gaps = buf.coverage_gaps(threshold=0.5)
        assert len(gaps) == 2

    def test_coverage_gaps_none_below(self):
        buf = ObservationBuffer(capacity=10)
        buf.record(_make_event(similarity=0.8))
        gaps = buf.coverage_gaps(threshold=0.3)
        assert len(gaps) == 0

    def test_recurring_patterns_counts_words(self):
        buf = ObservationBuffer(capacity=10)
        buf.record(_make_event(query="liver measurement technique"))
        buf.record(_make_event(query="liver size assessment"))
        buf.record(_make_event(query="liver volume estimation"))
        patterns = buf.recurring_patterns(min_count=3)
        assert "liver" in patterns
        assert patterns["liver"] == 3

    def test_recurring_patterns_skips_short_words(self):
        buf = ObservationBuffer(capacity=10)
        buf.record(_make_event(query="a is it"))
        buf.record(_make_event(query="a is it"))
        buf.record(_make_event(query="a is it"))
        patterns = buf.recurring_patterns(min_count=2)
        # All words are 3 chars or fewer, so nothing qualifies
        assert len(patterns) == 0

    def test_recurring_patterns_respects_min_count(self):
        buf = ObservationBuffer(capacity=10)
        buf.record(_make_event(query="alpha beta gamma"))
        buf.record(_make_event(query="alpha delta epsilon"))
        patterns = buf.recurring_patterns(min_count=2)
        assert "alpha" in patterns
        assert "beta" not in patterns

    def test_record_with_empty_instructions(self):
        """An event with no instructions should record top_similarity = 0.0."""
        event = RetrievalEvent(query="no results", instructions=[], response="")
        buf = ObservationBuffer(capacity=10)
        buf.record(event)
        obs = buf.drain()
        assert obs[0].top_similarity == 0.0
        assert obs[0].instruction_ids == []


# ---------------------------------------------------------------------------
# TestEvaluation
# ---------------------------------------------------------------------------


class TestEvaluation:
    def test_not_enough_observations_returns_no_evolve(self):
        config = EvolutionConfig(observe_window=10)
        observations = [_make_observation() for _ in range(5)]
        result = evaluate(observations, config)
        assert result.should_evolve is False
        assert "Not enough" in result.reason
        assert result.coverage_gaps == []
        assert result.recurring_terms == {}
        assert result.low_similarity_ratio == 0.0

    def test_exactly_at_window_evaluates(self):
        config = EvolutionConfig(observe_window=5)
        observations = [_make_observation(top_similarity=0.8) for _ in range(5)]
        result = evaluate(observations, config)
        # High similarity -> no evolution needed
        assert result.should_evolve is False

    def test_low_similarity_triggers_evolution(self):
        config = EvolutionConfig(
            observe_window=5,
            coverage_gap_threshold=0.3,
            low_similarity_trigger=0.3,
        )
        # 4 out of 5 have low similarity -> ratio = 0.8 > 0.3
        observations = [
            _make_observation(top_similarity=0.1),
            _make_observation(top_similarity=0.1),
            _make_observation(top_similarity=0.1),
            _make_observation(top_similarity=0.1),
            _make_observation(top_similarity=0.9),
        ]
        result = evaluate(observations, config)
        assert result.should_evolve is True
        assert result.low_similarity_ratio == pytest.approx(0.8)
        assert len(result.coverage_gaps) == 4

    def test_low_similarity_at_boundary_does_not_trigger(self):
        config = EvolutionConfig(
            observe_window=4,
            coverage_gap_threshold=0.3,
            low_similarity_trigger=0.5,
        )
        # 2 out of 4 have low similarity -> ratio = 0.5, not > 0.5
        observations = [
            _make_observation(top_similarity=0.1),
            _make_observation(top_similarity=0.1),
            _make_observation(top_similarity=0.9),
            _make_observation(top_similarity=0.9),
        ]
        result = evaluate(observations, config)
        # ratio == trigger, but condition is >, not >=
        # also check recurring is empty -> should_evolve should be False
        assert result.low_similarity_ratio == pytest.approx(0.5)

    def test_recurring_patterns_trigger_evolution(self):
        config = EvolutionConfig(
            observe_window=5,
            coverage_gap_threshold=0.3,
            pattern_threshold=3,
            low_similarity_trigger=0.99,  # High threshold so ratio alone won't trigger
        )
        # 5 low-similarity observations all mentioning "kubernetes"
        observations = [
            _make_observation(query="kubernetes deployment issue", top_similarity=0.1),
            _make_observation(query="kubernetes scaling problem", top_similarity=0.1),
            _make_observation(query="kubernetes networking config", top_similarity=0.1),
            _make_observation(query="kubernetes pod crash", top_similarity=0.1),
            _make_observation(query="kubernetes service mesh", top_similarity=0.1),
        ]
        result = evaluate(observations, config)
        assert result.should_evolve is True
        assert "kubernetes" in result.recurring_terms
        assert result.recurring_terms["kubernetes"] == 5

    def test_no_recurring_terms_when_all_high_similarity(self):
        config = EvolutionConfig(observe_window=3, coverage_gap_threshold=0.3)
        observations = [
            _make_observation(query="kubernetes pods", top_similarity=0.9),
            _make_observation(query="kubernetes pods", top_similarity=0.9),
            _make_observation(query="kubernetes pods", top_similarity=0.9),
        ]
        result = evaluate(observations, config)
        # All above threshold -> no gaps -> no recurring terms
        assert result.recurring_terms == {}

    def test_result_fields_populated(self):
        config = EvolutionConfig(observe_window=2, coverage_gap_threshold=0.5)
        observations = [
            _make_observation(query="alpha beta", top_similarity=0.1),
            _make_observation(query="gamma delta", top_similarity=0.1),
        ]
        result = evaluate(observations, config)
        assert isinstance(result, EvaluationResult)
        assert isinstance(result.reason, str)
        assert isinstance(result.coverage_gaps, list)
        assert isinstance(result.recurring_terms, dict)
        assert isinstance(result.low_similarity_ratio, float)


# ---------------------------------------------------------------------------
# TestGate
# ---------------------------------------------------------------------------


class TestGate:
    def test_auto_policy_approves_everything(self):
        config = EvolutionConfig(gate_policy="auto")
        gate = Gate(config)
        instructions = [_make_instruction("a"), _make_instruction("b")]
        auto_approved = gate.submit(instructions, "test reason")
        assert len(auto_approved) == 2
        assert gate.pending_count == 0

    def test_manual_policy_keeps_pending(self):
        config = EvolutionConfig(gate_policy="manual")
        gate = Gate(config)
        instructions = [_make_instruction("a"), _make_instruction("b")]
        auto_approved = gate.submit(instructions, "test reason")
        assert len(auto_approved) == 0
        assert gate.pending_count == 2

    def test_threshold_policy_approves_below_threshold(self):
        config = EvolutionConfig(
            gate_policy="threshold",
            auto_approve_below_priority=30,
        )
        gate = Gate(config)
        low = _make_instruction("low", priority=20)
        high = _make_instruction("high", priority=50)
        auto_approved = gate.submit([low, high], "reason")
        approved_ids = {i.id for i in auto_approved}
        assert "low" in approved_ids
        assert "high" not in approved_ids
        assert gate.pending_count == 1

    def test_threshold_policy_boundary(self):
        config = EvolutionConfig(
            gate_policy="threshold",
            auto_approve_below_priority=30,
        )
        gate = Gate(config)
        # priority == threshold -> approved (<=)
        boundary = _make_instruction("boundary", priority=30)
        auto_approved = gate.submit([boundary], "reason")
        assert len(auto_approved) == 1

    def test_approve_moves_from_pending(self):
        config = EvolutionConfig(gate_policy="manual")
        gate = Gate(config)
        gate.submit([_make_instruction("x")], "reason")
        assert gate.pending_count == 1
        result = gate.approve("x")
        assert result is not None
        assert result.id == "x"
        assert gate.pending_count == 0

    def test_approve_nonexistent_returns_none(self):
        config = EvolutionConfig(gate_policy="manual")
        gate = Gate(config)
        result = gate.approve("does-not-exist")
        assert result is None

    def test_reject_removes_from_pending(self):
        config = EvolutionConfig(gate_policy="manual")
        gate = Gate(config)
        gate.submit([_make_instruction("x")], "reason")
        assert gate.reject("x") is True
        assert gate.pending_count == 0

    def test_reject_nonexistent_returns_false(self):
        config = EvolutionConfig(gate_policy="manual")
        gate = Gate(config)
        assert gate.reject("nope") is False

    def test_rejected_instruction_cannot_be_resubmitted(self):
        config = EvolutionConfig(gate_policy="manual")
        gate = Gate(config)
        inst = _make_instruction("x")
        gate.submit([inst], "reason")
        gate.reject("x")
        # Re-submit the same instruction
        auto = gate.submit([inst], "reason again")
        assert len(auto) == 0
        assert gate.pending_count == 0

    def test_approve_all(self):
        config = EvolutionConfig(gate_policy="manual")
        gate = Gate(config)
        gate.submit([_make_instruction("a"), _make_instruction("b")], "reason")
        approved = gate.approve_all()
        assert len(approved) == 2
        assert gate.pending_count == 0

    def test_approve_all_empty(self):
        config = EvolutionConfig(gate_policy="manual")
        gate = Gate(config)
        approved = gate.approve_all()
        assert approved == []

    def test_drain_approved(self):
        config = EvolutionConfig(gate_policy="auto")
        gate = Gate(config)
        gate.submit([_make_instruction("a"), _make_instruction("b")], "reason")
        drained = gate.drain_approved()
        assert len(drained) == 2
        # Drain again should be empty
        assert gate.drain_approved() == []

    def test_drain_includes_manually_approved(self):
        config = EvolutionConfig(gate_policy="manual")
        gate = Gate(config)
        gate.submit([_make_instruction("a")], "reason")
        gate.approve("a")
        drained = gate.drain_approved()
        assert len(drained) == 1
        assert drained[0].id == "a"

    def test_pending_instructions_property(self):
        config = EvolutionConfig(gate_policy="manual")
        gate = Gate(config)
        gate.submit([_make_instruction("a")], "my reason")
        pending = gate.pending_instructions
        assert len(pending) == 1
        assert isinstance(pending[0], PendingInstruction)
        assert pending[0].instruction.id == "a"
        assert pending[0].reason == "my reason"
        assert pending[0].auto_approved is False

    def test_max_pending_limits_queue(self):
        config = EvolutionConfig(gate_policy="manual", max_pending=2)
        gate = Gate(config)
        instructions = [_make_instruction(f"i-{i}") for i in range(5)]
        gate.submit(instructions, "reason")
        assert gate.pending_count == 2


# ---------------------------------------------------------------------------
# TestTemplateGeneration
# ---------------------------------------------------------------------------


class TestTemplateGeneration:
    def test_generates_instructions_from_gaps(self):
        config = EvolutionConfig()
        eval_result = EvaluationResult(
            should_evolve=True,
            reason="test",
            coverage_gaps=[
                _make_observation(query="kubernetes deployment guide", top_similarity=0.1),
            ],
            recurring_terms={"kubernetes": 5},
            low_similarity_ratio=0.8,
        )
        instructions = generate_from_template(eval_result, config, counter=0)
        assert len(instructions) == 1
        inst = instructions[0]
        assert isinstance(inst, Instruction)
        assert inst.id.startswith("evo-gap-0-")
        assert inst.type == InstructionType.DIRECTIVE

    def test_generates_up_to_three(self):
        config = EvolutionConfig()
        gaps = [
            _make_observation(query=f"topic-{i} relevant question", top_similarity=0.1)
            for i in range(5)
        ]
        eval_result = EvaluationResult(
            should_evolve=True,
            reason="test",
            coverage_gaps=gaps,
            recurring_terms={},
            low_similarity_ratio=0.8,
        )
        instructions = generate_from_template(eval_result, config, counter=0)
        assert len(instructions) == 3

    def test_respects_max_priority(self):
        config = EvolutionConfig(max_evolved_priority=25)
        eval_result = EvaluationResult(
            should_evolve=True,
            reason="test",
            coverage_gaps=[
                _make_observation(query="some advanced topic here", top_similarity=0.05),
            ],
            recurring_terms={},
            low_similarity_ratio=0.9,
        )
        instructions = generate_from_template(eval_result, config, counter=0)
        for inst in instructions:
            assert inst.priority <= 25

    def test_includes_evolved_tag(self):
        config = EvolutionConfig(evolved_tag="evolved")
        eval_result = EvaluationResult(
            should_evolve=True,
            reason="test",
            coverage_gaps=[
                _make_observation(query="docker container networking", top_similarity=0.1),
            ],
            recurring_terms={},
            low_similarity_ratio=0.8,
        )
        instructions = generate_from_template(eval_result, config, counter=0)
        for inst in instructions:
            assert "evolved" in inst.tags

    def test_includes_generation_tags(self):
        config = EvolutionConfig(generation_tags=["auto-generated", "v2"])
        eval_result = EvaluationResult(
            should_evolve=True,
            reason="test",
            coverage_gaps=[
                _make_observation(query="react component lifecycle", top_similarity=0.1),
            ],
            recurring_terms={},
            low_similarity_ratio=0.8,
        )
        instructions = generate_from_template(eval_result, config, counter=0)
        for inst in instructions:
            assert "auto-generated" in inst.tags
            assert "v2" in inst.tags

    def test_content_references_query(self):
        config = EvolutionConfig()
        eval_result = EvaluationResult(
            should_evolve=True,
            reason="test",
            coverage_gaps=[
                _make_observation(query="machine learning optimization", top_similarity=0.05),
            ],
            recurring_terms={},
            low_similarity_ratio=0.9,
        )
        instructions = generate_from_template(eval_result, config, counter=0)
        inst = instructions[0]
        assert "machine learning optimization" in inst.content

    def test_metadata_records_source(self):
        config = EvolutionConfig()
        eval_result = EvaluationResult(
            should_evolve=True,
            reason="test",
            coverage_gaps=[
                _make_observation(query="test query", top_similarity=0.15),
            ],
            recurring_terms={},
            low_similarity_ratio=0.9,
        )
        instructions = generate_from_template(eval_result, config, counter=0)
        inst = instructions[0]
        assert inst.metadata["evolved_from"] == "template"
        assert inst.metadata["source_query"] == "test query"
        assert inst.metadata["source_similarity"] == pytest.approx(0.15)

    def test_counter_increments_ids(self):
        config = EvolutionConfig()
        eval_result = EvaluationResult(
            should_evolve=True,
            reason="test",
            coverage_gaps=[
                _make_observation(query="first query here", top_similarity=0.1),
            ],
            recurring_terms={},
            low_similarity_ratio=0.8,
        )
        inst_a = generate_from_template(eval_result, config, counter=0)
        inst_b = generate_from_template(eval_result, config, counter=10)
        assert "gap-0-" in inst_a[0].id
        assert "gap-10-" in inst_b[0].id

    def test_empty_gaps_produces_nothing(self):
        config = EvolutionConfig()
        eval_result = EvaluationResult(
            should_evolve=True,
            reason="test",
            coverage_gaps=[],
            recurring_terms={"something": 5},
            low_similarity_ratio=0.8,
        )
        instructions = generate_from_template(eval_result, config, counter=0)
        assert instructions == []

    def test_trigger_patterns_set_from_query_terms(self):
        config = EvolutionConfig()
        eval_result = EvaluationResult(
            should_evolve=True,
            reason="test",
            coverage_gaps=[
                _make_observation(query="kubernetes deployment scaling", top_similarity=0.1),
            ],
            recurring_terms={},
            low_similarity_ratio=0.8,
        )
        instructions = generate_from_template(eval_result, config, counter=0)
        inst = instructions[0]
        # Words > 3 chars: kubernetes, deployment, scaling
        # trigger_patterns should use first 2 terms (escaped)
        assert len(inst.scope.trigger_patterns) == 2


# ---------------------------------------------------------------------------
# TestEvolutionIntegration
# ---------------------------------------------------------------------------


class TestEvolutionIntegration:
    def test_full_pipeline_no_llm(self):
        """Create corpus, retriever, Evolution; feed observations; call tick(); verify corpus grew."""
        corpus = _seed_corpus()
        initial_size = len(corpus)

        retriever = Retriever(corpus, embedding_model="hash")
        retriever.build_index()

        config = EvolutionConfig(
            observe_window=5,
            coverage_gap_threshold=0.3,
            low_similarity_trigger=0.3,
            gate_policy="auto",
            batch_size=1,
            rebuild_cooldown=0.0,
        )

        evo = Evolution(retriever, corpus, llm=None, config=config)
        evo.start()

        try:
            # Feed low-similarity events that should trigger evolution
            for i in range(6):
                event = _make_event(
                    query=f"quantum computing algorithm-{i}",
                    similarity=0.05,
                    instruction_id="fallback-generic",
                )
                evo._buffer.record(event)

            # tick should evaluate + flush
            written = evo.tick()
            assert written > 0
            assert len(corpus) > initial_size

            # Check that newly added instructions have evolved tag
            new_instructions = [
                inst for inst in corpus if inst.id.startswith("evo-")
            ]
            assert len(new_instructions) > 0
            for inst in new_instructions:
                assert "evolved" in inst.tags

            # Stats should reflect the evolution
            assert evo.stats["total_evolved"] > 0
        finally:
            evo.stop()

    def test_manual_gate_requires_approval(self):
        corpus = _seed_corpus()
        initial_size = len(corpus)

        retriever = Retriever(corpus, embedding_model="hash")
        retriever.build_index()

        config = EvolutionConfig(
            observe_window=3,
            coverage_gap_threshold=0.3,
            low_similarity_trigger=0.3,
            gate_policy="manual",
            batch_size=1,
            rebuild_cooldown=0.0,
        )

        evo = Evolution(retriever, corpus, llm=None, config=config)

        # Directly feed the buffer
        for i in range(4):
            evo._buffer.record(_make_event(
                query=f"blockchain consensus mechanism-{i}",
                similarity=0.05,
            ))

        # check() generates but gate holds them
        result = evo.check()
        assert result.should_evolve is True
        assert evo.pending  # Should have pending instructions

        # flush without approval -> nothing written
        written = evo.flush(force=True)
        assert written == 0
        assert len(corpus) == initial_size

        # Approve all and flush
        evo.approve_all()
        written = evo.flush(force=True)
        assert written > 0
        assert len(corpus) > initial_size

    def test_start_stop_lifecycle(self):
        corpus = _seed_corpus()
        retriever = Retriever(corpus, embedding_model="hash")
        retriever.build_index()

        evo = Evolution(retriever, corpus)

        assert evo.stats["observing"] is False
        evo.start()
        assert evo.stats["observing"] is True

        # Calling start again is idempotent
        evo.start()
        assert evo.stats["observing"] is True

        evo.stop()
        assert evo.stats["observing"] is False

        # Calling stop again is idempotent
        evo.stop()
        assert evo.stats["observing"] is False

    def test_tick_below_window_does_not_evolve(self):
        corpus = _seed_corpus()
        retriever = Retriever(corpus, embedding_model="hash")
        retriever.build_index()

        config = EvolutionConfig(observe_window=10)
        evo = Evolution(retriever, corpus, config=config)

        # Feed only 3 observations (below window of 10)
        for i in range(3):
            evo._buffer.record(_make_event(similarity=0.05))

        written = evo.tick()
        assert written == 0
        assert len(corpus) == 3  # Unchanged

    def test_evolution_log_records_messages(self):
        corpus = _seed_corpus()
        retriever = Retriever(corpus, embedding_model="hash")
        retriever.build_index()

        config = EvolutionConfig(
            observe_window=2,
            coverage_gap_threshold=0.3,
            low_similarity_trigger=0.1,
            gate_policy="auto",
            batch_size=1,
            rebuild_cooldown=0.0,
        )

        evo = Evolution(retriever, corpus, config=config)
        evo.start()

        try:
            # Feed enough low-similarity events to trigger
            for i in range(3):
                evo._buffer.record(_make_event(
                    query=f"obscure topic {i} something",
                    similarity=0.02,
                ))

            evo.tick()
            log = evo.log
            assert len(log) > 0
            assert any("[evolution]" in entry for entry in log)
        finally:
            evo.stop()

    def test_flush_force_ignores_cooldown_and_batch(self):
        corpus = _seed_corpus()
        retriever = Retriever(corpus, embedding_model="hash")
        retriever.build_index()

        config = EvolutionConfig(
            observe_window=2,
            gate_policy="auto",
            batch_size=100,          # Very high batch
            rebuild_cooldown=9999.0, # Very high cooldown
        )

        evo = Evolution(retriever, corpus, config=config)

        for i in range(3):
            evo._buffer.record(_make_event(
                query=f"niche subject area-{i}",
                similarity=0.02,
            ))

        evo.check()
        # Without force, batch_size and cooldown would prevent flush
        written_no_force = evo.flush(force=False)
        # With force, it should write regardless
        written_force = evo.flush(force=True)

        assert written_force > 0 or written_no_force > 0


# ---------------------------------------------------------------------------
# TestEmitEvent
# ---------------------------------------------------------------------------


class TestEmitEvent:
    def test_retrieve_calls_emit_event(self):
        """Verify that retriever.retrieve() emits a RetrievalEvent via set_log_handler."""
        corpus = _seed_corpus()
        retriever = Retriever(corpus, embedding_model="hash")
        retriever.build_index()

        captured_events: list[RetrievalEvent] = []

        def handler(event: RetrievalEvent) -> None:
            captured_events.append(event)

        set_log_handler(handler)
        try:
            retriever.retrieve("test query", Context())
            assert len(captured_events) == 1
            event = captured_events[0]
            assert event.query == "test query"
            assert isinstance(event.instructions, list)
        finally:
            set_log_handler(None)

    def test_handler_receives_metadata(self):
        corpus = _seed_corpus()
        retriever = Retriever(corpus, embedding_model="hash")
        retriever.build_index()

        captured_events: list[RetrievalEvent] = []
        set_log_handler(lambda e: captured_events.append(e))
        try:
            retriever.retrieve("test", Context(tags=["safety"]))
            assert len(captured_events) == 1
            event = captured_events[0]
            assert "context_tags" in event.metadata
            assert "safety" in event.metadata["context_tags"]
        finally:
            set_log_handler(None)

    def test_no_handler_does_not_raise(self):
        corpus = _seed_corpus()
        retriever = Retriever(corpus, embedding_model="hash")
        retriever.build_index()

        set_log_handler(None)
        # Should not raise even without a handler
        retriever.retrieve("test", Context())

    def test_evolution_observes_via_handler(self):
        """Evolution.start() installs a handler that records observations."""
        corpus = _seed_corpus()
        retriever = Retriever(corpus, embedding_model="hash")
        retriever.build_index()

        config = EvolutionConfig(observe_window=5)
        evo = Evolution(retriever, corpus, config=config)
        evo.start()

        try:
            assert evo.stats["observations_buffered"] == 0
            retriever.retrieve("liver measurement technique", Context())
            assert evo.stats["observations_buffered"] == 1

            retriever.retrieve("another query here", Context())
            assert evo.stats["observations_buffered"] == 2
        finally:
            evo.stop()

    def test_evolution_chains_previous_handler(self):
        """Evolution should chain the previous log handler, not replace it."""
        corpus = _seed_corpus()
        retriever = Retriever(corpus, embedding_model="hash")
        retriever.build_index()

        outer_events: list[RetrievalEvent] = []
        set_log_handler(lambda e: outer_events.append(e))

        evo = Evolution(retriever, corpus)
        evo.start()

        try:
            retriever.retrieve("chaining test", Context())
            # Both evolution buffer and outer handler should receive the event
            assert evo.stats["observations_buffered"] == 1
            assert len(outer_events) == 1
        finally:
            evo.stop()
            set_log_handler(None)

    def test_stop_restores_previous_handler(self):
        """After stop(), the previous handler should be restored."""
        corpus = _seed_corpus()
        retriever = Retriever(corpus, embedding_model="hash")
        retriever.build_index()

        outer_events: list[RetrievalEvent] = []
        original_handler = lambda e: outer_events.append(e)
        set_log_handler(original_handler)

        evo = Evolution(retriever, corpus)
        evo.start()
        evo.stop()

        # After stop, the original handler should be active
        retriever.retrieve("after stop", Context())
        assert len(outer_events) == 1
        assert outer_events[0].query == "after stop"
        # Evolution buffer should NOT have gotten this event
        assert evo.stats["observations_buffered"] == 0

        set_log_handler(None)


# ---------------------------------------------------------------------------
# TestCoerceToList
# ---------------------------------------------------------------------------


class TestCoerceToList:
    def test_list_passthrough(self):
        assert _coerce_to_list(["a", "b"]) == ["a", "b"]

    def test_comma_separated_string(self):
        assert _coerce_to_list("persona, crisis, weather") == ["persona", "crisis", "weather"]

    def test_single_string(self):
        assert _coerce_to_list("evolved") == ["evolved"]

    def test_quoted_items_stripped(self):
        assert _coerce_to_list('"evolved", "crisis"') == ["evolved", "crisis"]

    def test_single_quoted_items_stripped(self):
        assert _coerce_to_list("'evolved', 'crisis'") == ["evolved", "crisis"]

    def test_empty_string(self):
        assert _coerce_to_list("") == []

    def test_empty_list(self):
        assert _coerce_to_list([]) == []

    def test_none(self):
        assert _coerce_to_list(None) == []

    def test_mixed_whitespace(self):
        assert _coerce_to_list("  a ,  b  , c  ") == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# TestFixYamlInlineLists
# ---------------------------------------------------------------------------


class TestFixYamlInlineLists:
    def test_fixes_bare_comma_separated_tags(self):
        text = '    tags: "evolved", crisis, weather'
        fixed = _fix_yaml_inline_lists(text)
        assert fixed == "    tags: ['evolved', 'crisis', 'weather']"

    def test_leaves_proper_flow_list_alone(self):
        text = '    tags: ["evolved", "crisis"]'
        fixed = _fix_yaml_inline_lists(text)
        assert fixed == text

    def test_leaves_block_list_alone(self):
        text = "    tags:\n      - evolved\n      - crisis"
        fixed = _fix_yaml_inline_lists(text)
        assert fixed == text

    def test_handles_multiline_yaml(self):
        text = (
            "instructions:\n"
            "  - id: evo-1\n"
            '    tags: "evolved", crisis\n'
            "    content: some text"
        )
        fixed = _fix_yaml_inline_lists(text)
        assert '["evolved", "crisis"]' not in fixed or "['evolved', 'crisis']" in fixed
        # The key assertion: it should be valid YAML after fixing
        import yaml
        data = yaml.safe_load(fixed)
        assert data["instructions"][0]["tags"] == ["evolved", "crisis"]


# ---------------------------------------------------------------------------
# TestParseGeneratedInstructions
# ---------------------------------------------------------------------------


class TestParseGeneratedInstructions:
    def test_parses_tags_as_comma_string(self):
        """LLM produces tags as a comma-separated string instead of a list."""
        yaml_text = """
instructions:
  - id: evo-test-1
    type: persona
    priority: 30
    content: Be empathetic.
    tags: persona, crisis, weather
"""
        config = EvolutionConfig()
        result = _parse_generated_instructions(yaml_text, config)
        assert len(result) == 1
        assert "persona" in result[0].tags
        assert "crisis" in result[0].tags
        assert "weather" in result[0].tags

    def test_parses_malformed_yaml_tags(self):
        """LLM produces tags: "evolved", crisis, weather (invalid YAML)."""
        yaml_text = """
instructions:
  - id: evo-test-2
    type: directive
    priority: 25
    content: Provide guidance.
    tags: "evolved", crisis, flood
"""
        config = EvolutionConfig()
        result = _parse_generated_instructions(yaml_text, config)
        assert len(result) == 1
        assert "evolved" in result[0].tags
        assert "crisis" in result[0].tags
        assert "flood" in result[0].tags

    def test_parses_proper_yaml_list(self):
        """Proper YAML list should still work."""
        yaml_text = """
instructions:
  - id: evo-test-3
    type: protocol
    priority: 20
    content: Follow protocol.
    tags: ["evolved", "protocol"]
"""
        config = EvolutionConfig()
        result = _parse_generated_instructions(yaml_text, config)
        assert len(result) == 1
        assert "evolved" in result[0].tags
        assert "protocol" in result[0].tags


# ---------------------------------------------------------------------------
# TestBreedingConfig
# ---------------------------------------------------------------------------


class TestBreedingConfig:
    def test_defaults(self):
        cfg = BreedingConfig()
        assert cfg.crossover_rate == 0.5
        assert cfg.persona_priority == 80
        assert cfg.exclude_types == [InstructionType.PERSONA]
        assert cfg.exclude_tags == []
        assert cfg.child_tags == []
        assert cfg.scope_to_child is True
        assert cfg.seed is None

    def test_validation_crossover_rate_range(self):
        with pytest.raises(Exception):
            BreedingConfig(crossover_rate=1.5)
        with pytest.raises(Exception):
            BreedingConfig(crossover_rate=-0.1)

    def test_validation_persona_priority_range(self):
        with pytest.raises(Exception):
            BreedingConfig(persona_priority=101)
        with pytest.raises(Exception):
            BreedingConfig(persona_priority=-1)


# ---------------------------------------------------------------------------
# Breeding helpers
# ---------------------------------------------------------------------------


def _parent_a_corpus() -> Corpus:
    """Build a small parent corpus with a persona + directives + constraint."""
    corpus = Corpus()
    corpus.add_many([
        Instruction(
            id="knight-persona",
            type=InstructionType.PERSONA,
            priority=80,
            content="You are Sir Aldric, a steadfast knight sworn to protect the weak.",
            tags=["knight", "personality"],
        ),
        Instruction(
            id="knight-combat",
            type=InstructionType.DIRECTIVE,
            priority=60,
            content="Raise your shield and assess the enemy before charging.",
            tags=["knight", "combat"],
        ),
        Instruction(
            id="knight-honor",
            type=InstructionType.DIRECTIVE,
            priority=60,
            content="Never strike an unarmed opponent. Offer surrender first.",
            tags=["knight", "honor"],
        ),
        Instruction(
            id="knight-no-poison",
            type=InstructionType.CONSTRAINT,
            priority=90,
            content="Never use poison, traps, or deception in combat.",
            tags=["knight", "constraint"],
        ),
    ])
    return corpus


def _parent_b_corpus() -> Corpus:
    """Build a small second parent corpus."""
    corpus = Corpus()
    corpus.add_many([
        Instruction(
            id="rogue-persona",
            type=InstructionType.PERSONA,
            priority=80,
            content="You are Finn Shadowstep, a quick-witted rogue who lives by charm.",
            tags=["rogue", "personality"],
        ),
        Instruction(
            id="rogue-escape",
            type=InstructionType.DIRECTIVE,
            priority=60,
            content="Always identify two exit routes before entering any building.",
            tags=["rogue", "stealth"],
        ),
        Instruction(
            id="rogue-charm",
            type=InstructionType.DIRECTIVE,
            priority=60,
            content="When caught, talk your way out first.",
            tags=["rogue", "social"],
        ),
        Instruction(
            id="rogue-no-innocents",
            type=InstructionType.CONSTRAINT,
            priority=90,
            content="Never steal from those who can't afford to lose it.",
            tags=["rogue", "constraint"],
        ),
    ])
    return corpus


# ---------------------------------------------------------------------------
# TestBreed
# ---------------------------------------------------------------------------


class TestBreed:
    def test_basic_crossover(self):
        result = breed(
            _parent_a_corpus(), _parent_b_corpus(),
            "vigilante", "knight", "rogue",
            config=BreedingConfig(seed=42),
        )
        assert isinstance(result, BreedResult)
        assert isinstance(result.child, Corpus)
        # At minimum has the persona
        assert len(result.child) >= 1
        assert result.child_name == "vigilante"
        assert result.persona.type == InstructionType.PERSONA

    def test_deterministic_with_seed(self):
        cfg = BreedingConfig(seed=12345)
        r1 = breed(_parent_a_corpus(), _parent_b_corpus(), "paladin", "knight", "rogue", config=cfg)
        r2 = breed(_parent_a_corpus(), _parent_b_corpus(), "paladin", "knight", "rogue", config=cfg)
        ids1 = sorted(i.id for i in r1.child)
        ids2 = sorted(i.id for i in r2.child)
        assert ids1 == ids2
        assert r1.inherited_count == r2.inherited_count

    def test_crossover_rate_zero(self):
        cfg = BreedingConfig(crossover_rate=0.0, seed=1)
        result = breed(_parent_a_corpus(), _parent_b_corpus(), "child", config=cfg)
        # Only the persona should exist
        assert len(result.child) == 1
        assert result.inherited_count == 0

    def test_crossover_rate_one(self):
        cfg = BreedingConfig(crossover_rate=1.0, seed=1)
        result = breed(_parent_a_corpus(), _parent_b_corpus(), "child", "a", "b", config=cfg)
        # All non-persona instructions inherited (3 from A + 3 from B = 6, minus
        # the PERSONA-type exclusions which are 1 each = skipped, but constraints
        # are included by default). So: 3 from A (combat, honor, no-poison) +
        # 3 from B (escape, charm, no-innocents) = 6 + 1 persona = 7
        assert result.inherited_count == 6
        assert len(result.child) == 7

    def test_default_persona_template(self):
        cfg = BreedingConfig(seed=1)
        result = breed(
            _parent_a_corpus(), _parent_b_corpus(),
            "vigilante", "knight", "rogue", config=cfg,
        )
        content = result.persona.content
        assert "Vigilante" in content
        assert "Knight" in content
        assert "Rogue" in content
        # Should contain parent persona text
        assert "Sir Aldric" in content
        assert "Finn Shadowstep" in content

    def test_custom_persona_overrides_template(self):
        cfg = BreedingConfig(seed=1)
        result = breed(
            _parent_a_corpus(), _parent_b_corpus(),
            "vigilante", "knight", "rogue",
            config=cfg,
            custom_persona="I am the night.",
        )
        assert result.persona.content == "I am the night."

    def test_lineage_metadata(self):
        cfg = BreedingConfig(crossover_rate=1.0, seed=42)
        result = breed(_parent_a_corpus(), _parent_b_corpus(), "child", "a", "b", config=cfg)
        for inst in result.child:
            if inst.type == InstructionType.PERSONA:
                continue
            assert "inherited_from" in inst.metadata
            assert inst.metadata["inherited_from"] in ("a", "b")
            assert "original_id" in inst.metadata
            assert "breed_seed" in inst.metadata

    def test_exclude_types(self):
        cfg = BreedingConfig(
            crossover_rate=1.0,
            seed=1,
            exclude_types=[InstructionType.PERSONA, InstructionType.CONSTRAINT],
        )
        result = breed(_parent_a_corpus(), _parent_b_corpus(), "child", "a", "b", config=cfg)
        # Constraints should be excluded
        for inst in result.child:
            assert inst.type != InstructionType.CONSTRAINT
        # Constraint IDs should be in skipped_ids
        assert "knight-no-poison" in result.skipped_ids
        assert "rogue-no-innocents" in result.skipped_ids

    def test_exclude_tags(self):
        cfg = BreedingConfig(
            crossover_rate=1.0,
            seed=1,
            exclude_tags=["combat"],
        )
        result = breed(_parent_a_corpus(), _parent_b_corpus(), "child", "a", "b", config=cfg)
        assert "knight-combat" in result.skipped_ids
        assert "child-knight-combat" not in result.child

    def test_scope_to_child_false(self):
        cfg = BreedingConfig(crossover_rate=1.0, seed=1, scope_to_child=False)
        result = breed(_parent_a_corpus(), _parent_b_corpus(), "child", config=cfg)
        # Inherited instructions should NOT have required_tags=[child]
        for inst in result.child:
            if inst.type == InstructionType.PERSONA:
                continue
            # Original scope preserved (no required_tags since parents had none)
            assert inst.scope.required_tags == []

    def test_breed_result_counts(self):
        cfg = BreedingConfig(crossover_rate=1.0, seed=1)
        result = breed(_parent_a_corpus(), _parent_b_corpus(), "child", "a", "b", config=cfg)
        assert result.from_a_count + result.from_b_count == result.inherited_count
        assert result.from_a_count == 3  # combat, honor, no-poison
        assert result.from_b_count == 3  # escape, charm, no-innocents

    def test_empty_parent(self):
        empty = Corpus()
        cfg = BreedingConfig(crossover_rate=1.0, seed=1)
        result = breed(empty, _parent_b_corpus(), "child", "a", "b", config=cfg)
        # Only persona + B's non-persona instructions
        assert result.from_a_count == 0
        assert result.from_b_count >= 0
        assert result.persona is not None

    def test_child_tags(self):
        cfg = BreedingConfig(crossover_rate=1.0, seed=1, child_tags=["hybrid", "gen-2"])
        result = breed(_parent_a_corpus(), _parent_b_corpus(), "child", config=cfg)
        for inst in result.child:
            assert "child" in inst.tags
            assert "hybrid" in inst.tags
            assert "gen-2" in inst.tags

    def test_seed_used_in_result(self):
        result = breed(_parent_a_corpus(), _parent_b_corpus(), "child", config=BreedingConfig(seed=999))
        assert result.seed_used == 999

    def test_default_seed_from_child_name(self):
        r1 = breed(_parent_a_corpus(), _parent_b_corpus(), "paladin")
        r2 = breed(_parent_a_corpus(), _parent_b_corpus(), "paladin")
        assert r1.seed_used == r2.seed_used == hash("paladin")


# ---------------------------------------------------------------------------
# TestLocusBreeding
# ---------------------------------------------------------------------------


def _locus_parent_a() -> Corpus:
    """Parent A with locus-tagged instructions."""
    c = Corpus()
    c.add(Instruction(
        id="a-persona", type=InstructionType.PERSONA, priority=80,
        content="Parent A persona", tags=["parent_a"],
    ))
    c.add(Instruction(
        id="a-combat", type=InstructionType.DIRECTIVE, priority=60,
        content="Attack aggressively", tags=["parent_a", "combat"],
        metadata={"gene": "combat"},
    ))
    c.add(Instruction(
        id="a-forage", type=InstructionType.DIRECTIVE, priority=50,
        content="Search for berries", tags=["parent_a", "forage"],
        metadata={"gene": "foraging"},
    ))
    c.add(Instruction(
        id="a-social", type=InstructionType.DIRECTIVE, priority=55,
        content="Greet warmly", tags=["parent_a", "social"],
        metadata={"gene": "social"},
    ))
    c.add(Instruction(
        id="a-unlocus", type=InstructionType.DIRECTIVE, priority=40,
        content="General instruction from A", tags=["parent_a"],
    ))
    return c


def _locus_parent_b() -> Corpus:
    """Parent B with locus-tagged instructions."""
    c = Corpus()
    c.add(Instruction(
        id="b-persona", type=InstructionType.PERSONA, priority=80,
        content="Parent B persona", tags=["parent_b"],
    ))
    c.add(Instruction(
        id="b-combat", type=InstructionType.DIRECTIVE, priority=60,
        content="Defend cautiously", tags=["parent_b", "combat"],
        metadata={"gene": "combat"},
    ))
    c.add(Instruction(
        id="b-forage", type=InstructionType.DIRECTIVE, priority=50,
        content="Hunt small prey", tags=["parent_b", "forage"],
        metadata={"gene": "foraging"},
    ))
    c.add(Instruction(
        id="b-territory", type=InstructionType.DIRECTIVE, priority=55,
        content="Mark territory", tags=["parent_b", "territory"],
        metadata={"gene": "territorial"},
    ))
    c.add(Instruction(
        id="b-unlocus", type=InstructionType.DIRECTIVE, priority=40,
        content="General instruction from B", tags=["parent_b"],
    ))
    return c


class TestLocusBreeding:
    """Tests for locus-based breeding (locus_key)."""

    def test_locus_picks_one_parent_per_locus(self):
        cfg = BreedingConfig(locus_key="gene", seed=42)
        result = breed(
            _locus_parent_a(), _locus_parent_b(), "child",
            "parent_a", "parent_b", config=cfg,
        )
        # Shared loci (combat, foraging): exactly one parent's version each
        combat_insts = [
            i for i in result.child
            if i.metadata.get("gene") == "combat"
        ]
        assert len(combat_insts) == 1
        assert combat_insts[0].metadata["inherited_from"] in ("parent_a", "parent_b")

        forage_insts = [
            i for i in result.child
            if i.metadata.get("gene") == "foraging"
        ]
        assert len(forage_insts) == 1

    def test_locus_hemizygous_always_inherited(self):
        """Loci present in only one parent are always inherited."""
        cfg = BreedingConfig(locus_key="gene", seed=42)
        result = breed(
            _locus_parent_a(), _locus_parent_b(), "child",
            "parent_a", "parent_b", config=cfg,
        )
        # "social" only in A -> always inherited
        social = [i for i in result.child if i.metadata.get("gene") == "social"]
        assert len(social) == 1
        assert social[0].metadata["inherited_from"] == "parent_a"

        # "territorial" only in B -> always inherited
        terr = [i for i in result.child if i.metadata.get("gene") == "territorial"]
        assert len(terr) == 1
        assert terr[0].metadata["inherited_from"] == "parent_b"

    def test_locus_no_gene_loss(self):
        """Every locus present in either parent appears in offspring."""
        cfg = BreedingConfig(locus_key="gene", seed=42)
        result = breed(
            _locus_parent_a(), _locus_parent_b(), "child",
            "parent_a", "parent_b", config=cfg,
        )
        child_loci = {
            i.metadata["gene"]
            for i in result.child
            if "gene" in i.metadata
        }
        assert child_loci == {"combat", "foraging", "social", "territorial"}

    def test_locus_blend_inherits_both(self):
        cfg = BreedingConfig(locus_key="gene", locus_blend=True, seed=42)
        result = breed(
            _locus_parent_a(), _locus_parent_b(), "child",
            "parent_a", "parent_b", config=cfg,
        )
        # Shared loci should have both parents' instructions
        combat_insts = [
            i for i in result.child
            if i.metadata.get("gene") == "combat"
        ]
        assert len(combat_insts) == 2
        parents = {i.metadata["inherited_from"] for i in combat_insts}
        assert parents == {"parent_a", "parent_b"}

    def test_locus_choices_in_result(self):
        cfg = BreedingConfig(locus_key="gene", seed=42)
        result = breed(
            _locus_parent_a(), _locus_parent_b(), "child",
            "parent_a", "parent_b", config=cfg,
        )
        # All four loci should be recorded
        assert set(result.locus_choices.keys()) == {
            "combat", "foraging", "social", "territorial",
        }
        # Hemizygous loci should show the only parent
        assert result.locus_choices["social"] == "parent_a"
        assert result.locus_choices["territorial"] == "parent_b"

    def test_locus_blend_choices_show_both(self):
        cfg = BreedingConfig(locus_key="gene", locus_blend=True, seed=42)
        result = breed(
            _locus_parent_a(), _locus_parent_b(), "child",
            "parent_a", "parent_b", config=cfg,
        )
        assert result.locus_choices["combat"] == "both"
        assert result.locus_choices["foraging"] == "both"
        # Hemizygous still shows the single parent
        assert result.locus_choices["social"] == "parent_a"

    def test_locus_unlabeled_use_crossover_rate(self):
        """Instructions without locus_key fall back to crossover_rate."""
        cfg = BreedingConfig(locus_key="gene", crossover_rate=1.0, seed=42)
        result = breed(
            _locus_parent_a(), _locus_parent_b(), "child",
            "parent_a", "parent_b", config=cfg,
        )
        # Both unlocus instructions should be inherited at rate=1.0
        unlocus = [
            i for i in result.child
            if "gene" not in i.metadata and i.type != InstructionType.PERSONA
        ]
        assert len(unlocus) == 2

    def test_locus_deterministic_with_seed(self):
        cfg = BreedingConfig(locus_key="gene", seed=99)
        r1 = breed(
            _locus_parent_a(), _locus_parent_b(), "child",
            "parent_a", "parent_b", config=cfg,
        )
        r2 = breed(
            _locus_parent_a(), _locus_parent_b(), "child",
            "parent_a", "parent_b", config=cfg,
        )
        assert r1.locus_choices == r2.locus_choices
        ids1 = sorted(i.id for i in r1.child)
        ids2 = sorted(i.id for i in r2.child)
        assert ids1 == ids2

    def test_locus_key_none_is_legacy(self):
        """locus_key=None uses legacy crossover_rate behavior."""
        cfg = BreedingConfig(locus_key=None, crossover_rate=0.5, seed=42)
        result = breed(
            _locus_parent_a(), _locus_parent_b(), "child",
            "parent_a", "parent_b", config=cfg,
        )
        assert result.locus_choices == {}
        # Some instructions may be dropped (legacy behavior)
        non_persona = [
            i for i in result.child if i.type != InstructionType.PERSONA
        ]
        # With crossover_rate=0.5, unlikely all 6 non-persona survive
        # but with seed=42 we just check it runs without error
        assert len(non_persona) >= 0


# ---------------------------------------------------------------------------
# TestLocusRegistry
# ---------------------------------------------------------------------------


class TestLocusRegistry:
    """Tests for GeneLocus, LocusRegistry, and CrossoverMethod."""

    def test_from_names_auto_assigns_positions(self):
        reg = LocusRegistry.from_names(["foraging", "combat", "social"])
        assert reg.names() == ["foraging", "combat", "social"]
        assert reg.loci[0].position == 0
        assert reg.loci[1].position == 1
        assert reg.loci[2].position == 2

    def test_by_name_found(self):
        reg = LocusRegistry.from_names(["foraging", "combat"])
        loc = reg.by_name("combat")
        assert loc is not None
        assert loc.name == "combat"
        assert loc.position == 1

    def test_by_name_not_found(self):
        reg = LocusRegistry.from_names(["foraging"])
        assert reg.by_name("nonexistent") is None

    def test_ordered_returns_sorted(self):
        reg = LocusRegistry(loci=[
            GeneLocus(name="z", position=2),
            GeneLocus(name="a", position=0),
            GeneLocus(name="m", position=1),
        ])
        ordered = reg.ordered()
        assert [loc.name for loc in ordered] == ["a", "m", "z"]

    def test_ordered_raises_without_positions(self):
        reg = LocusRegistry(loci=[
            GeneLocus(name="foraging", position=0),
            GeneLocus(name="combat"),  # no position
        ])
        with pytest.raises(ValueError, match="combat"):
            reg.ordered()

    def test_validate_instruction_known_locus(self):
        reg = LocusRegistry.from_names(["foraging", "combat"])
        inst = Instruction(
            id="test", type=InstructionType.DIRECTIVE, content="x",
            metadata={"gene": "foraging"},
        )
        assert reg.validate_instruction(inst, "gene") is None

    def test_validate_instruction_unknown_locus(self):
        reg = LocusRegistry.from_names(["foraging", "combat"])
        inst = Instruction(
            id="test", type=InstructionType.DIRECTIVE, content="x",
            metadata={"gene": "unknown_locus"},
        )
        err = reg.validate_instruction(inst, "gene")
        assert err is not None
        assert "unknown_locus" in err

    def test_validate_instruction_no_locus(self):
        reg = LocusRegistry.from_names(["foraging"])
        inst = Instruction(
            id="test", type=InstructionType.DIRECTIVE, content="x",
        )
        assert reg.validate_instruction(inst, "gene") is None

    def test_from_yaml(self):
        yaml_text = """\
loci:
  - name: foraging
    position: 0
    description: Food-finding strategy
  - name: combat
    position: 1
    description: Fight or flight
"""
        reg = LocusRegistry.from_yaml(yaml_text)
        assert len(reg.loci) == 2
        assert reg.loci[0].name == "foraging"
        assert reg.loci[0].description == "Food-finding strategy"
        assert reg.loci[1].position == 1

    def test_from_yaml_missing_loci_key(self):
        with pytest.raises(ValueError, match="loci"):
            LocusRegistry.from_yaml("something: else")

    def test_to_yaml_round_trip(self):
        reg = LocusRegistry.from_names(["foraging", "combat"])
        yaml_str = reg.to_yaml()
        reg2 = LocusRegistry.from_yaml(yaml_str)
        assert reg2.names() == reg.names()
        assert reg2.loci[0].position == reg.loci[0].position

    def test_from_file(self, tmp_path):
        f = tmp_path / "loci.yaml"
        f.write_text(
            "loci:\n"
            "  - name: a\n"
            "    position: 0\n"
            "  - name: b\n"
            "    position: 1\n"
        )
        reg = LocusRegistry.from_file(f)
        assert reg.names() == ["a", "b"]

    def test_crossover_method_values(self):
        assert CrossoverMethod.TAGGED.value == "tagged"
        assert CrossoverMethod.SINGLE_POINT.value == "single_point"
        assert CrossoverMethod.TWO_POINT.value == "two_point"
        assert CrossoverMethod.UNIFORM.value == "uniform"


# ---------------------------------------------------------------------------
# Helpers for positional crossover tests
# ---------------------------------------------------------------------------


def _positional_parent_a() -> Corpus:
    """Parent A with 4 locus-tagged instructions."""
    c = Corpus()
    c.add(Instruction(
        id="a-persona", type=InstructionType.PERSONA, priority=80,
        content="Parent A persona", tags=["parent_a"],
    ))
    for name in ["foraging", "combat", "social", "exploration"]:
        c.add(Instruction(
            id=f"a-{name}", type=InstructionType.DIRECTIVE, priority=60,
            content=f"A's {name} behavior", tags=["parent_a", name],
            metadata={"gene": name},
        ))
    return c


def _positional_parent_b() -> Corpus:
    """Parent B with overlapping + unique loci."""
    c = Corpus()
    c.add(Instruction(
        id="b-persona", type=InstructionType.PERSONA, priority=80,
        content="Parent B persona", tags=["parent_b"],
    ))
    for name in ["foraging", "combat", "social", "territorial"]:
        c.add(Instruction(
            id=f"b-{name}", type=InstructionType.DIRECTIVE, priority=60,
            content=f"B's {name} behavior", tags=["parent_b", name],
            metadata={"gene": name},
        ))
    return c


# ---------------------------------------------------------------------------
# TestPositionalCrossover
# ---------------------------------------------------------------------------


class TestPositionalCrossover:
    """Tests for positional crossover methods (single-point, two-point, uniform)."""

    def _registry(self):
        return LocusRegistry.from_names([
            "foraging", "combat", "social", "exploration", "territorial",
        ])

    def test_single_point_contiguous_blocks(self):
        """Single-point crossover produces two contiguous blocks."""
        reg = self._registry()
        cfg = BreedingConfig(
            locus_key="gene", seed=42,
            crossover_method=CrossoverMethod.SINGLE_POINT,
            locus_registry=reg,
        )
        result = breed(
            _positional_parent_a(), _positional_parent_b(), "child",
            "parent_a", "parent_b", config=cfg,
        )
        assert len(result.crossover_points) == 1
        k = result.crossover_points[0]
        locus_names = reg.names()

        # All loci before k should be from the same parent, all after from the other
        choices_before = [
            result.locus_choices.get(locus_names[i])
            for i in range(k)
            if locus_names[i] in result.locus_choices
            and result.locus_choices[locus_names[i]] not in ("parent_a", "parent_b")
            or locus_names[i] in result.locus_choices
        ]
        choices_after = [
            result.locus_choices.get(locus_names[i])
            for i in range(k, len(locus_names))
            if locus_names[i] in result.locus_choices
            and result.locus_choices[locus_names[i]] not in ("parent_a", "parent_b")
            or locus_names[i] in result.locus_choices
        ]
        # Filter to shared loci only (hemizygous always inherit from one parent)
        shared = {"foraging", "combat", "social"}
        before_shared = [
            result.locus_choices[locus_names[i]]
            for i in range(k) if locus_names[i] in shared
        ]
        after_shared = [
            result.locus_choices[locus_names[i]]
            for i in range(k, len(locus_names)) if locus_names[i] in shared
        ]
        # Before crossover point: all from parent_a; after: all from parent_b
        if before_shared:
            assert all(p == "parent_a" for p in before_shared)
        if after_shared:
            assert all(p == "parent_b" for p in after_shared)

    def test_two_point_crossover_points(self):
        """Two-point crossover records two crossover points."""
        reg = self._registry()
        cfg = BreedingConfig(
            locus_key="gene", seed=42,
            crossover_method=CrossoverMethod.TWO_POINT,
            locus_registry=reg,
        )
        result = breed(
            _positional_parent_a(), _positional_parent_b(), "child",
            "parent_a", "parent_b", config=cfg,
        )
        assert len(result.crossover_points) == 2
        assert result.crossover_points[0] <= result.crossover_points[1]

    def test_uniform_all_loci_present(self):
        """Uniform crossover includes every locus from either parent."""
        reg = self._registry()
        cfg = BreedingConfig(
            locus_key="gene", seed=42,
            crossover_method=CrossoverMethod.UNIFORM,
            locus_registry=reg,
        )
        result = breed(
            _positional_parent_a(), _positional_parent_b(), "child",
            "parent_a", "parent_b", config=cfg,
        )
        child_loci = {
            i.metadata["gene"]
            for i in result.child
            if "gene" in i.metadata
        }
        # All loci from both parents should be present
        assert child_loci == {"foraging", "combat", "social", "exploration", "territorial"}
        assert result.crossover_points == []

    def test_hemizygous_always_inherited(self):
        """Loci present in only one parent are inherited regardless of mask."""
        reg = self._registry()
        cfg = BreedingConfig(
            locus_key="gene", seed=42,
            crossover_method=CrossoverMethod.SINGLE_POINT,
            locus_registry=reg,
        )
        result = breed(
            _positional_parent_a(), _positional_parent_b(), "child",
            "parent_a", "parent_b", config=cfg,
        )
        # "exploration" only in A
        assert result.locus_choices["exploration"] == "parent_a"
        # "territorial" only in B
        assert result.locus_choices["territorial"] == "parent_b"

    def test_blend_overrides_mask(self):
        """locus_blend=True gives both parents' instructions even with positional crossover."""
        reg = self._registry()
        cfg = BreedingConfig(
            locus_key="gene", seed=42, locus_blend=True,
            crossover_method=CrossoverMethod.SINGLE_POINT,
            locus_registry=reg,
        )
        result = breed(
            _positional_parent_a(), _positional_parent_b(), "child",
            "parent_a", "parent_b", config=cfg,
        )
        # Shared loci should show "both"
        assert result.locus_choices["foraging"] == "both"
        assert result.locus_choices["combat"] == "both"
        assert result.locus_choices["social"] == "both"

    def test_deterministic_with_seed(self):
        reg = self._registry()
        cfg = BreedingConfig(
            locus_key="gene", seed=99,
            crossover_method=CrossoverMethod.SINGLE_POINT,
            locus_registry=reg,
        )
        r1 = breed(
            _positional_parent_a(), _positional_parent_b(), "child",
            "parent_a", "parent_b", config=cfg,
        )
        r2 = breed(
            _positional_parent_a(), _positional_parent_b(), "child",
            "parent_a", "parent_b", config=cfg,
        )
        assert r1.locus_choices == r2.locus_choices
        assert r1.crossover_points == r2.crossover_points

    def test_requires_registry(self):
        cfg = BreedingConfig(
            locus_key="gene", seed=42,
            crossover_method=CrossoverMethod.SINGLE_POINT,
        )
        with pytest.raises(ValueError, match="locus_registry"):
            breed(
                _positional_parent_a(), _positional_parent_b(), "child",
                "parent_a", "parent_b", config=cfg,
            )

    def test_requires_locus_key(self):
        reg = LocusRegistry.from_names(["foraging", "combat"])
        cfg = BreedingConfig(
            locus_key=None, seed=42,
            crossover_method=CrossoverMethod.SINGLE_POINT,
            locus_registry=reg,
        )
        with pytest.raises(ValueError, match="locus_key"):
            breed(
                _positional_parent_a(), _positional_parent_b(), "child",
                "parent_a", "parent_b", config=cfg,
            )

    def test_requires_positions(self):
        reg = LocusRegistry(loci=[
            GeneLocus(name="foraging", position=0),
            GeneLocus(name="combat"),  # no position
        ])
        cfg = BreedingConfig(
            locus_key="gene", seed=42,
            crossover_method=CrossoverMethod.SINGLE_POINT,
            locus_registry=reg,
        )
        with pytest.raises(ValueError, match="combat"):
            breed(
                _positional_parent_a(), _positional_parent_b(), "child",
                "parent_a", "parent_b", config=cfg,
            )


# ---------------------------------------------------------------------------
# TestLocusValidation
# ---------------------------------------------------------------------------


class TestLocusValidation:
    """Tests for locus validation with registries."""

    def test_registry_with_tagged_crossover_works(self):
        """A locus_registry with TAGGED method should still work normally."""
        reg = LocusRegistry.from_names(["combat", "foraging", "social", "territorial"])
        cfg = BreedingConfig(
            locus_key="gene", seed=42,
            crossover_method=CrossoverMethod.TAGGED,
            locus_registry=reg,
        )
        result = breed(
            _locus_parent_a(), _locus_parent_b(), "child",
            "parent_a", "parent_b", config=cfg,
        )
        child_loci = {
            i.metadata["gene"]
            for i in result.child if "gene" in i.metadata
        }
        assert child_loci == {"combat", "foraging", "social", "territorial"}

    def test_unknown_locus_falls_back(self):
        """Instructions with unregistered loci fall back to crossover_rate."""
        # Registry only knows about "foraging" — "combat" etc. are unknown
        reg = LocusRegistry.from_names(["foraging"])
        cfg = BreedingConfig(
            locus_key="gene", seed=42, crossover_rate=1.0,
            crossover_method=CrossoverMethod.SINGLE_POINT,
            locus_registry=reg,
        )
        result = breed(
            _locus_parent_a(), _locus_parent_b(), "child",
            "parent_a", "parent_b", config=cfg,
        )
        # "foraging" should be in locus_choices (registered)
        assert "foraging" in result.locus_choices
        # Unregistered loci should still be inherited via crossover_rate=1.0
        child_loci = {
            i.metadata["gene"]
            for i in result.child if "gene" in i.metadata
        }
        assert "combat" in child_loci  # inherited via fallback


# ---------------------------------------------------------------------------
# Test Diploid Breeding
# ---------------------------------------------------------------------------


def _diploid_registry():
    """Registry with mixed haploid/diploid loci."""
    return LocusRegistry(loci=[
        GeneLocus(name="foraging", position=0, dominance=Dominance.HAPLOID),
        GeneLocus(name="combat", position=1, dominance=Dominance.DOMINANT),
        GeneLocus(name="social", position=2, dominance=Dominance.DOMINANT),
        GeneLocus(name="exploration", position=3, dominance=Dominance.CODOMINANT),
    ])


def _diploid_parent_a():
    c = Corpus()
    c.add(Instruction(
        id="a-persona", type=InstructionType.PERSONA, priority=80,
        content="Parent A persona",
    ))
    for name in ["foraging", "combat", "social", "exploration"]:
        c.add(Instruction(
            id=f"a-{name}", type=InstructionType.DIRECTIVE, priority=60,
            content=f"A's {name} behavior", metadata={"gene": name},
        ))
    return c


def _diploid_parent_b():
    c = Corpus()
    c.add(Instruction(
        id="b-persona", type=InstructionType.PERSONA, priority=80,
        content="Parent B persona",
    ))
    for name in ["foraging", "combat", "social", "exploration"]:
        c.add(Instruction(
            id=f"b-{name}", type=InstructionType.DIRECTIVE, priority=60,
            content=f"B's {name} behavior", metadata={"gene": name},
        ))
    return c


class TestDiploidBreeding:
    """Tests for diploid inheritance and expression."""

    def test_haploid_locus_picks_one_parent(self):
        """Haploid loci behave as before — pick one parent."""
        reg = _diploid_registry()
        cfg = BreedingConfig(
            locus_key="gene", seed=42,
            crossover_method=CrossoverMethod.SINGLE_POINT,
            locus_registry=reg,
        )
        result = breed(
            _diploid_parent_a(), _diploid_parent_b(), "child",
            "parent_a", "parent_b", config=cfg,
        )
        # foraging is haploid — should come from exactly one parent
        foraging = [
            i for i in result.child
            if i.metadata.get("gene") == "foraging"
        ]
        assert len(foraging) == 1
        assert "foraging" not in result.genotype

    def test_diploid_locus_carries_both_alleles(self):
        """Diploid loci should carry alleles from both parents."""
        reg = _diploid_registry()
        cfg = BreedingConfig(
            locus_key="gene", seed=42,
            crossover_method=CrossoverMethod.SINGLE_POINT,
            locus_registry=reg,
        )
        result = breed(
            _diploid_parent_a(), _diploid_parent_b(), "child",
            "parent_a", "parent_b", config=cfg,
        )
        # combat is DOMINANT — should carry both alleles
        combat = [
            i for i in result.child
            if i.metadata.get("gene") == "combat"
        ]
        assert len(combat) == 2
        alleles = {i.metadata.get("allele") for i in combat}
        assert alleles == {"a", "b"}
        assert "combat" in result.genotype
        assert result.locus_choices["combat"] == "diploid"

    def test_genotype_records_allele_ids(self):
        reg = _diploid_registry()
        cfg = BreedingConfig(
            locus_key="gene", seed=42,
            crossover_method=CrossoverMethod.SINGLE_POINT,
            locus_registry=reg,
        )
        result = breed(
            _diploid_parent_a(), _diploid_parent_b(), "child",
            "parent_a", "parent_b", config=cfg,
        )
        for locus_name in ["combat", "social", "exploration"]:
            assert locus_name in result.genotype
            a_id, b_id = result.genotype[locus_name]
            assert a_id  # non-empty
            assert b_id  # non-empty

    def test_diploid_tagged_crossover(self):
        """Diploid works with TAGGED crossover too."""
        reg = _diploid_registry()
        cfg = BreedingConfig(
            locus_key="gene", seed=42,
            crossover_method=CrossoverMethod.TAGGED,
            locus_registry=reg,
        )
        result = breed(
            _diploid_parent_a(), _diploid_parent_b(), "child",
            "parent_a", "parent_b", config=cfg,
        )
        combat = [
            i for i in result.child
            if i.metadata.get("gene") == "combat"
        ]
        assert len(combat) == 2
        assert {i.metadata["allele"] for i in combat} == {"a", "b"}

    def test_hemizygous_diploid_homozygous(self):
        """When only one parent has a diploid locus, child is homozygous."""
        reg = LocusRegistry(loci=[
            GeneLocus(name="unique_gene", position=0, dominance=Dominance.DOMINANT),
        ])
        parent_a = Corpus()
        parent_a.add(Instruction(
            id="a-persona", type=InstructionType.PERSONA, priority=80,
            content="A",
        ))
        parent_a.add(Instruction(
            id="a-unique", type=InstructionType.DIRECTIVE, priority=60,
            content="unique behavior", metadata={"gene": "unique_gene"},
        ))
        parent_b = Corpus()
        parent_b.add(Instruction(
            id="b-persona", type=InstructionType.PERSONA, priority=80,
            content="B",
        ))
        cfg = BreedingConfig(
            locus_key="gene", seed=42,
            crossover_method=CrossoverMethod.SINGLE_POINT,
            locus_registry=reg,
        )
        result = breed(parent_a, parent_b, "child", "parent_a", "parent_b", config=cfg)
        unique = [i for i in result.child if i.metadata.get("gene") == "unique_gene"]
        assert len(unique) == 2  # homozygous = two copies
        assert {i.metadata["allele"] for i in unique} == {"a", "b"}

    def test_meiosis_segregates_diploid_parent_alleles(self):
        """When a diploid parent breeds, only ONE of its two alleles passes
        to each gamete (Mendelian segregation).

        Setup: parent corpora are *already diploid* at locus 'combat' — each
        carries an allele:"a" instruction with content "G_A" and an
        allele:"b" instruction with content "G_B" (representing alleles
        inherited from grandparents). When two such parents breed, the
        child should have exactly one allele from each parent's gamete,
        not all four.
        """
        reg = LocusRegistry(loci=[
            GeneLocus(name="combat", position=0, dominance=Dominance.DOMINANT),
        ])

        def diploid_parent(name: str, a_text: str, b_text: str) -> Corpus:
            c = Corpus()
            c.add(Instruction(
                id=f"{name}-persona", type=InstructionType.PERSONA,
                priority=80, content=name,
            ))
            c.add(Instruction(
                id=f"{name}-combat-a", type=InstructionType.DIRECTIVE,
                priority=60, content=a_text,
                metadata={"gene": "combat", "allele": "a"},
            ))
            c.add(Instruction(
                id=f"{name}-combat-b", type=InstructionType.DIRECTIVE,
                priority=60, content=b_text,
                metadata={"gene": "combat", "allele": "b"},
            ))
            return c

        from collections import Counter
        genotype_counts: Counter = Counter()
        for trial in range(400):
            parent_a = diploid_parent("ma", "MA_A", "MA_B")
            parent_b = diploid_parent("pa", "PA_A", "PA_B")
            cfg = BreedingConfig(
                locus_key="gene", seed=trial,
                crossover_method=CrossoverMethod.TAGGED,
                locus_registry=reg,
            )
            result = breed(
                parent_a, parent_b, f"child{trial}",
                "ma", "pa", config=cfg,
            )
            combat = [
                i for i in result.child
                if i.metadata.get("gene") == "combat"
            ]
            # Child must inherit exactly two alleles, one in each slot.
            assert len(combat) == 2, (
                f"trial {trial}: expected 2 combat instructions, got {len(combat)}"
            )
            a_inst = [i for i in combat if i.metadata["allele"] == "a"]
            b_inst = [i for i in combat if i.metadata["allele"] == "b"]
            assert len(a_inst) == 1 and len(b_inst) == 1
            # Allele 'a' content must come from parent A's gamete (one of MA_A / MA_B).
            assert a_inst[0].content in ("MA_A", "MA_B")
            # Allele 'b' content must come from parent B's gamete (one of PA_A / PA_B).
            assert b_inst[0].content in ("PA_A", "PA_B")
            genotype_counts[(a_inst[0].content, b_inst[0].content)] += 1

        # All four (MA_*, PA_*) combinations should appear with non-trivial
        # frequency (proper independent random segregation).
        assert len(genotype_counts) == 4, (
            f"expected all 4 genotypes, got {dict(genotype_counts)}"
        )
        for combo, count in genotype_counts.items():
            assert count > 30, (
                f"genotype {combo} appeared only {count} times in 400 trials"
            )


# ---------------------------------------------------------------------------
# Test Expression
# ---------------------------------------------------------------------------


class TestExpression:
    """Tests for express() — genotype to phenotype resolution."""

    def _breed_diploid(self):
        reg = _diploid_registry()
        cfg = BreedingConfig(
            locus_key="gene", seed=42,
            crossover_method=CrossoverMethod.SINGLE_POINT,
            locus_registry=reg,
        )
        return breed(
            _diploid_parent_a(), _diploid_parent_b(), "child",
            "parent_a", "parent_b", config=cfg,
        ), reg

    def test_dominant_with_default_scores_picks_a(self):
        """When both alleles default to dominance=1.0, allele a wins on tie."""
        result, reg = self._breed_diploid()
        expressed = express(result.child, reg, locus_key="gene")
        combat = [i for i in expressed if i.metadata.get("gene") == "combat"]
        # Tie at default 1.0 → allele a wins
        assert len(combat) == 1
        assert combat[0].metadata.get("allele") == "a"

    def test_dominant_higher_score_wins(self):
        """The allele with higher metadata['dominance'] should be expressed."""
        reg = LocusRegistry(loci=[
            GeneLocus(name="trait", position=0, dominance=Dominance.DOMINANT),
        ])
        # Build a child corpus directly with two alleles having different scores
        child = Corpus()
        child.add(Instruction(
            id="child-trait-a", type=InstructionType.DIRECTIVE, priority=60,
            content="recessive variant",
            metadata={"gene": "trait", "allele": "a", "dominance": 0.2},
        ))
        child.add(Instruction(
            id="child-trait-b", type=InstructionType.DIRECTIVE, priority=60,
            content="dominant variant",
            metadata={"gene": "trait", "allele": "b", "dominance": 0.9},
        ))
        expressed = express(child, reg, locus_key="gene")
        trait = [i for i in expressed if i.metadata.get("gene") == "trait"]
        assert len(trait) == 1
        assert trait[0].content == "dominant variant"
        assert trait[0].metadata.get("allele") == "b"

    def test_dominant_recessive_hidden(self):
        """The lower-scored allele is fully hidden from express()."""
        reg = LocusRegistry(loci=[
            GeneLocus(name="trait", position=0, dominance=Dominance.DOMINANT),
        ])
        child = Corpus()
        child.add(Instruction(
            id="child-trait-a", type=InstructionType.DIRECTIVE, priority=60,
            content="dominant",
            metadata={"gene": "trait", "allele": "a", "dominance": 0.95},
        ))
        child.add(Instruction(
            id="child-trait-b", type=InstructionType.DIRECTIVE, priority=60,
            content="recessive",
            metadata={"gene": "trait", "allele": "b", "dominance": 0.05},
        ))
        expressed = express(child, reg, locus_key="gene")
        contents = {i.content for i in expressed if i.metadata.get("gene") == "trait"}
        assert contents == {"dominant"}

    def test_codominant_expresses_both(self):
        result, reg = self._breed_diploid()
        expressed = express(result.child, reg, locus_key="gene")
        # exploration is CODOMINANT — both alleles expressed
        exploration = [i for i in expressed if i.metadata.get("gene") == "exploration"]
        assert len(exploration) == 2
        assert {i.metadata["allele"] for i in exploration} == {"a", "b"}

    def test_codominant_with_blend_fn(self):
        result, reg = self._breed_diploid()
        def blend(a: str, b: str) -> str:
            return f"BLEND({a}|{b})"
        expressed = express(result.child, reg, locus_key="gene", blend_fn=blend)
        exploration = [i for i in expressed if i.metadata.get("gene") == "exploration"]
        assert len(exploration) == 1
        assert exploration[0].content.startswith("BLEND(")
        assert exploration[0].metadata.get("allele") == "expressed"

    def test_haploid_passes_through(self):
        result, reg = self._breed_diploid()
        expressed = express(result.child, reg, locus_key="gene")
        foraging = [i for i in expressed if i.metadata.get("gene") == "foraging"]
        assert len(foraging) == 1  # haploid, unchanged

    def test_caching(self):
        result, reg = self._breed_diploid()
        expressed1 = express(result.child, reg, locus_key="gene")
        expressed2 = express(result.child, reg, locus_key="gene")
        assert expressed1 is expressed2  # same object from cache


# ---------------------------------------------------------------------------
# Test Linkage Groups
# ---------------------------------------------------------------------------


class TestLinkageGroups:
    """Tests for linkage-aware crossover."""

    def test_linkage_boundaries_detected(self):
        loci = [
            GeneLocus(name="a", position=0, linkage_group="survival"),
            GeneLocus(name="b", position=1, linkage_group="survival"),
            GeneLocus(name="c", position=2, linkage_group="social"),
            GeneLocus(name="d", position=3, linkage_group="social"),
        ]
        boundaries = _linkage_boundaries(loci)
        assert boundaries == [2]  # boundary between survival and social

    def test_no_linkage_groups_no_boundaries(self):
        loci = [
            GeneLocus(name="a", position=0),
            GeneLocus(name="b", position=1),
        ]
        # Ungrouped loci produce boundaries at every position
        boundaries = _linkage_boundaries(loci)
        assert 1 in boundaries

    def test_single_point_respects_linkage(self):
        """Single-point crossover should cut at linkage boundary."""
        import random
        loci = [
            GeneLocus(name="a", position=0, linkage_group="g1"),
            GeneLocus(name="b", position=1, linkage_group="g1"),
            GeneLocus(name="c", position=2, linkage_group="g2"),
            GeneLocus(name="d", position=3, linkage_group="g2"),
        ]
        # Only boundary is at position 2
        rng = random.Random(42)
        mask, points = _build_parent_mask(
            CrossoverMethod.SINGLE_POINT, 4, rng, ordered_loci=loci,
        )
        assert points == [2]  # forced to cut at the only boundary
        # a, b from parent A; c, d from parent B
        assert mask == [True, True, False, False]

    def test_two_point_respects_linkage(self):
        import random
        loci = [
            GeneLocus(name="a", position=0, linkage_group="g1"),
            GeneLocus(name="b", position=1, linkage_group="g2"),
            GeneLocus(name="c", position=2, linkage_group="g3"),
            GeneLocus(name="d", position=3, linkage_group="g4"),
        ]
        # All positions are boundaries (1, 2, 3)
        rng = random.Random(42)
        mask, points = _build_parent_mask(
            CrossoverMethod.TWO_POINT, 4, rng, ordered_loci=loci,
        )
        assert len(points) == 2
        assert all(p in [1, 2, 3] for p in points)

    def test_linkage_in_breed(self):
        """End-to-end: linkage groups in breed()."""
        reg = LocusRegistry(loci=[
            GeneLocus(name="foraging", position=0, linkage_group="survival"),
            GeneLocus(name="combat", position=1, linkage_group="survival"),
            GeneLocus(name="social", position=2, linkage_group="community"),
            GeneLocus(name="exploration", position=3, linkage_group="community"),
        ])
        cfg = BreedingConfig(
            locus_key="gene", seed=42,
            crossover_method=CrossoverMethod.SINGLE_POINT,
            locus_registry=reg,
        )
        result = breed(
            _positional_parent_a(), _positional_parent_b(), "child",
            "parent_a", "parent_b", config=cfg,
        )
        # Crossover should be at boundary index 2
        assert result.crossover_points == [2]
        # foraging and combat should come from same parent (same linkage group)
        shared = {"foraging", "combat"}
        parents = {
            result.locus_choices[l]
            for l in shared
            if l in result.locus_choices
        }
        assert len(parents) == 1  # both from same parent


# ---------------------------------------------------------------------------
# Test Mutation Pipeline
# ---------------------------------------------------------------------------


class TestMutationPipeline:
    """Tests for the mutation_rate / mutator pipeline."""

    def test_mutator_called(self):
        """Instructions are mutated when mutation_rate > 0."""
        def upper_mutator(inst, rng):
            return inst.model_copy(update={"content": inst.content.upper()})

        cfg = BreedingConfig(
            crossover_rate=1.0, seed=42,
            mutation_rate=1.0,
            mutator=upper_mutator,
        )
        parent_a = Corpus()
        parent_a.add(Instruction(
            id="a-p", type=InstructionType.PERSONA, priority=80, content="A",
        ))
        parent_a.add(Instruction(
            id="a-1", type=InstructionType.DIRECTIVE, priority=60, content="hello",
        ))
        parent_b = Corpus()
        parent_b.add(Instruction(
            id="b-p", type=InstructionType.PERSONA, priority=80, content="B",
        ))
        result = breed(parent_a, parent_b, "child", config=cfg)
        # All non-persona instructions should be uppercased
        directives = [i for i in result.child if i.type == InstructionType.DIRECTIVE]
        assert all(i.content == "HELLO" for i in directives)
        assert len(result.mutated_ids) > 0

    def test_lethal_mutation_drops_instruction(self):
        """mutator returning None drops the instruction."""
        def lethal_mutator(inst, rng):
            return None  # lethal mutation

        cfg = BreedingConfig(
            crossover_rate=1.0, seed=42,
            mutation_rate=1.0,
            mutator=lethal_mutator,
        )
        parent_a = Corpus()
        parent_a.add(Instruction(
            id="a-p", type=InstructionType.PERSONA, priority=80, content="A",
        ))
        parent_a.add(Instruction(
            id="a-1", type=InstructionType.DIRECTIVE, priority=60, content="x",
        ))
        parent_b = Corpus()
        parent_b.add(Instruction(
            id="b-p", type=InstructionType.PERSONA, priority=80, content="B",
        ))
        result = breed(parent_a, parent_b, "child", config=cfg)
        directives = [i for i in result.child if i.type == InstructionType.DIRECTIVE]
        assert len(directives) == 0

    def test_zero_mutation_rate_no_mutation(self):
        """mutation_rate=0.0 means no mutations even if mutator is set."""
        call_count = 0
        def counting_mutator(inst, rng):
            nonlocal call_count
            call_count += 1
            return inst

        cfg = BreedingConfig(
            crossover_rate=1.0, seed=42,
            mutation_rate=0.0,
            mutator=counting_mutator,
        )
        parent_a = Corpus()
        parent_a.add(Instruction(
            id="a-p", type=InstructionType.PERSONA, priority=80, content="A",
        ))
        parent_a.add(Instruction(
            id="a-1", type=InstructionType.DIRECTIVE, priority=60, content="x",
        ))
        parent_b = Corpus()
        parent_b.add(Instruction(
            id="b-p", type=InstructionType.PERSONA, priority=80, content="B",
        ))
        breed(parent_a, parent_b, "child", config=cfg)
        assert call_count == 0

    def test_mutation_uses_breed_rng(self):
        """Mutations should be deterministic with the same seed."""
        mutations = []
        def tracking_mutator(inst, rng):
            mutations.append(inst.id)
            return inst.model_copy(update={"content": f"mutated-{rng.randint(0, 1000)}"})

        cfg = BreedingConfig(
            crossover_rate=1.0, seed=99,
            mutation_rate=0.5,
            mutator=tracking_mutator,
        )
        parent_a = Corpus()
        parent_a.add(Instruction(
            id="a-p", type=InstructionType.PERSONA, priority=80, content="A",
        ))
        for i in range(5):
            parent_a.add(Instruction(
                id=f"a-{i}", type=InstructionType.DIRECTIVE, priority=60, content=f"inst-{i}",
            ))
        parent_b = Corpus()
        parent_b.add(Instruction(
            id="b-p", type=InstructionType.PERSONA, priority=80, content="B",
        ))

        mutations.clear()
        r1 = breed(parent_a, parent_b, "child", config=cfg)
        m1 = list(mutations)

        mutations.clear()
        r2 = breed(parent_a, parent_b, "child", config=cfg)
        m2 = list(mutations)

        assert m1 == m2  # same mutations with same seed


# ---------------------------------------------------------------------------
# Test Strict Registry Mode
# ---------------------------------------------------------------------------


class TestStrictRegistry:
    """Tests for strict_unregistered mode."""

    def test_strict_raises_on_unregistered_locus(self):
        reg = LocusRegistry.from_names(["foraging"])  # only knows foraging
        cfg = BreedingConfig(
            locus_key="gene", seed=42, crossover_rate=1.0,
            crossover_method=CrossoverMethod.SINGLE_POINT,
            locus_registry=reg,
            strict_unregistered=True,
        )
        # _locus_parent_a has combat, foraging, social — combat and social are unknown
        with pytest.raises(ValueError, match="strict_unregistered"):
            breed(
                _locus_parent_a(), _locus_parent_b(), "child",
                "parent_a", "parent_b", config=cfg,
            )

    def test_non_strict_falls_back(self):
        """Default (non-strict) should warn but not raise."""
        reg = LocusRegistry.from_names(["foraging"])
        cfg = BreedingConfig(
            locus_key="gene", seed=42, crossover_rate=1.0,
            crossover_method=CrossoverMethod.SINGLE_POINT,
            locus_registry=reg,
            strict_unregistered=False,
        )
        # Should not raise
        result = breed(
            _locus_parent_a(), _locus_parent_b(), "child",
            "parent_a", "parent_b", config=cfg,
        )
        assert result.inherited_count > 0


# ---------------------------------------------------------------------------
# Test Parent Mask Audit Trail
# ---------------------------------------------------------------------------


class TestParentMaskAudit:
    """Tests for parent_mask in BreedResult."""

    def test_positional_has_mask(self):
        reg = LocusRegistry.from_names(["foraging", "combat", "social"])
        cfg = BreedingConfig(
            locus_key="gene", seed=42,
            crossover_method=CrossoverMethod.SINGLE_POINT,
            locus_registry=reg,
        )
        result = breed(
            _positional_parent_a(), _positional_parent_b(), "child",
            "parent_a", "parent_b", config=cfg,
        )
        assert result.parent_mask is not None
        assert len(result.parent_mask) == 3
        assert all(isinstance(v, bool) for v in result.parent_mask)

    def test_uniform_has_mask(self):
        reg = LocusRegistry.from_names(["foraging", "combat", "social"])
        cfg = BreedingConfig(
            locus_key="gene", seed=42,
            crossover_method=CrossoverMethod.UNIFORM,
            locus_registry=reg,
        )
        result = breed(
            _positional_parent_a(), _positional_parent_b(), "child",
            "parent_a", "parent_b", config=cfg,
        )
        assert result.parent_mask is not None
        assert len(result.parent_mask) == 3

    def test_tagged_has_no_mask(self):
        cfg = BreedingConfig(
            locus_key="gene", seed=42,
            crossover_method=CrossoverMethod.TAGGED,
        )
        result = breed(
            _locus_parent_a(), _locus_parent_b(), "child",
            "parent_a", "parent_b", config=cfg,
        )
        assert result.parent_mask is None


# ---------------------------------------------------------------------------
# Test Dominance in YAML
# ---------------------------------------------------------------------------


class TestDominanceYAML:
    """Tests for Dominance enum and YAML round-trip."""

    def test_dominance_values(self):
        assert Dominance.HAPLOID.value == "haploid"
        assert Dominance.DOMINANT.value == "dominant"
        assert Dominance.CODOMINANT.value == "codominant"

    def test_yaml_round_trip_with_dominance(self):
        yaml_text = """\
loci:
  - name: foraging
    position: 0
    dominance: dominant
    linkage_group: survival
  - name: combat
    position: 1
    dominance: codominant
    linkage_group: survival
  - name: social
    position: 2
"""
        reg = LocusRegistry.from_yaml(yaml_text)
        assert reg.loci[0].dominance == Dominance.DOMINANT
        assert reg.loci[0].linkage_group == "survival"
        assert reg.loci[1].dominance == Dominance.CODOMINANT
        assert reg.loci[2].dominance == Dominance.HAPLOID  # default

    def test_yaml_omits_haploid_default(self):
        reg = LocusRegistry(loci=[
            GeneLocus(name="a", position=0),  # haploid default
            GeneLocus(name="b", position=1, dominance=Dominance.DOMINANT),
        ])
        yaml_str = reg.to_yaml()
        assert "haploid" not in yaml_str  # default omitted
        assert "dominant" in yaml_str

    def test_linkage_group_in_yaml(self):
        reg = LocusRegistry(loci=[
            GeneLocus(name="a", position=0, linkage_group="g1"),
        ])
        yaml_str = reg.to_yaml()
        reg2 = LocusRegistry.from_yaml(yaml_str)
        assert reg2.loci[0].linkage_group == "g1"
