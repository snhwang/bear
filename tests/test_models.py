"""Tests for bear.models."""

import pytest

from bear.models import (
    ActionSet,
    Context,
    Instruction,
    InstructionType,
    ScopeCondition,
    ScoredInstruction,
    SessionState,
    collect_actions,
)


class TestScopeCondition:
    def test_empty_scope_matches_everything(self):
        scope = ScopeCondition()
        context = Context(user_role="admin", domain="medical")
        assert scope.matches(context)

    def test_user_role_match(self):
        scope = ScopeCondition(user_roles=["admin", "editor"])
        assert scope.matches(Context(user_role="admin"))
        assert scope.matches(Context(user_role="editor"))
        assert not scope.matches(Context(user_role="viewer"))

    def test_task_type_match(self):
        scope = ScopeCondition(task_types=["review", "edit"])
        assert scope.matches(Context(task_type="review"))
        assert not scope.matches(Context(task_type="delete"))

    def test_domain_match(self):
        scope = ScopeCondition(domains=["medical", "legal"])
        assert scope.matches(Context(domain="medical"))
        assert not scope.matches(Context(domain="finance"))

    def test_tag_match(self):
        scope = ScopeCondition(tags=["urgent", "safety"])
        assert scope.matches(Context(tags=["urgent"]))
        assert scope.matches(Context(tags=["other", "safety"]))
        assert not scope.matches(Context(tags=["other"]))

    def test_trigger_pattern_match(self):
        scope = ScopeCondition(trigger_patterns=[r"urgent|asap"])
        assert scope.matches(Context(query="This is urgent please"))
        assert scope.matches(Context(query="need this asap"))
        assert not scope.matches(Context(query="take your time"))

    def test_session_phase_match(self):
        scope = ScopeCondition(session_phase=["active", "closing"])
        assert scope.matches(Context(session_phase="active"))
        assert not scope.matches(Context(session_phase="early"))

    def test_required_tags_and_logic(self):
        scope = ScopeCondition(required_tags=["must-have", "critical"])
        # Must have ALL required tags
        assert scope.matches(Context(tags=["must-have", "critical", "other"]))
        assert not scope.matches(Context(tags=["must-have"]))
        assert not scope.matches(Context(tags=["other"]))

    def test_required_tags_with_other_conditions(self):
        scope = ScopeCondition(
            required_tags=["must-have"],
            user_roles=["admin"],
        )
        # required_tags must match AND at least one other condition
        assert scope.matches(Context(user_role="admin", tags=["must-have"]))
        assert not scope.matches(Context(user_role="admin", tags=["other"]))
        assert not scope.matches(Context(user_role="viewer", tags=["must-have"]))

    def test_or_logic_across_fields(self):
        scope = ScopeCondition(
            user_roles=["admin"],
            domains=["medical"],
        )
        # Either field matching is enough
        assert scope.matches(Context(user_role="admin"))
        assert scope.matches(Context(domain="medical"))
        assert not scope.matches(Context(user_role="viewer", domain="finance"))


class TestInstruction:
    def test_create_instruction(self):
        inst = Instruction(
            id="test-1",
            type=InstructionType.CONSTRAINT,
            priority=95,
            content="Do not do X",
        )
        assert inst.id == "test-1"
        assert inst.type == InstructionType.CONSTRAINT
        assert inst.priority == 95

    def test_instruction_equality(self):
        a = Instruction(id="test-1", type=InstructionType.CONSTRAINT, content="X")
        b = Instruction(id="test-1", type=InstructionType.PERSONA, content="Y")
        assert a == b  # Same ID = equal

    def test_instruction_hash(self):
        a = Instruction(id="test-1", type=InstructionType.CONSTRAINT, content="X")
        b = Instruction(id="test-1", type=InstructionType.PERSONA, content="Y")
        assert hash(a) == hash(b)

    def test_priority_bounds(self):
        with pytest.raises(Exception):
            Instruction(id="x", type=InstructionType.CONSTRAINT, content="X", priority=101)
        with pytest.raises(Exception):
            Instruction(id="x", type=InstructionType.CONSTRAINT, content="X", priority=-1)

    def test_instruction_types(self):
        for t in InstructionType:
            inst = Instruction(id=f"test-{t.value}", type=t, content="test")
            assert inst.type == t


class TestScoredInstruction:
    def test_properties(self):
        inst = Instruction(
            id="test-1",
            type=InstructionType.PERSONA,
            priority=80,
            content="Be helpful",
        )
        scored = ScoredInstruction(
            instruction=inst,
            similarity=0.85,
            scope_match=True,
            final_score=0.87,
        )
        assert scored.id == "test-1"
        assert scored.priority == 80
        assert scored.type == InstructionType.PERSONA
        assert scored.content == "Be helpful"


class TestActions:
    def test_instruction_actions_default_empty(self):
        inst = Instruction(
            id="test-1", type=InstructionType.CONSTRAINT, content="X"
        )
        assert inst.actions == {}

    def test_instruction_actions_from_yaml(self):
        inst = Instruction(
            id="protocol-critical",
            type=InstructionType.PROTOCOL,
            priority=95,
            content="Critical finding detected.",
            actions={
                "alert_level": "critical",
                "notify": ["attending", "referring_physician"],
                "flag_worklist": True,
            },
        )
        assert inst.actions["alert_level"] == "critical"
        assert inst.actions["notify"] == ["attending", "referring_physician"]
        assert inst.actions["flag_worklist"] is True

    def test_scored_instruction_actions_property(self):
        inst = Instruction(
            id="test-1",
            type=InstructionType.PROTOCOL,
            content="X",
            actions={"alert_level": "routine"},
        )
        scored = ScoredInstruction(instruction=inst, similarity=0.9, final_score=0.9)
        assert scored.actions["alert_level"] == "routine"

    def test_collect_actions_priority_order(self):
        """Higher-priority instruction wins for duplicate keys."""
        high = ScoredInstruction(
            instruction=Instruction(
                id="high",
                type=InstructionType.PROTOCOL,
                priority=95,
                content="High",
                actions={"alert_level": "critical", "escalate": True},
            ),
            similarity=0.9,
            final_score=0.95,
        )
        low = ScoredInstruction(
            instruction=Instruction(
                id="low",
                type=InstructionType.DIRECTIVE,
                priority=50,
                content="Low",
                actions={"alert_level": "routine", "log": True},
            ),
            similarity=0.8,
            final_score=0.7,
        )
        result = collect_actions([low, high])  # order shouldn't matter
        assert result.actions["alert_level"] == "critical"
        assert result.actions["escalate"] is True
        assert result.actions["log"] is True
        assert result.sources["alert_level"] == "high"
        assert result.sources["log"] == "low"

    def test_collect_actions_list_concatenation(self):
        """List-valued actions from multiple instructions are concatenated."""
        a = ScoredInstruction(
            instruction=Instruction(
                id="a",
                type=InstructionType.PROTOCOL,
                priority=90,
                content="A",
                actions={"notify": ["attending"]},
            ),
            similarity=0.9,
            final_score=0.9,
        )
        b = ScoredInstruction(
            instruction=Instruction(
                id="b",
                type=InstructionType.PROTOCOL,
                priority=80,
                content="B",
                actions={"notify": ["referring_physician"]},
            ),
            similarity=0.8,
            final_score=0.8,
        )
        result = collect_actions([a, b])
        assert result.actions["notify"] == ["attending", "referring_physician"]

    def test_collect_actions_empty(self):
        result = collect_actions([])
        assert not result
        assert result.actions == {}

    def test_action_set_contains_and_get(self):
        action_set = ActionSet(
            actions={"alert_level": "critical"},
            sources={"alert_level": "test-1"},
        )
        assert "alert_level" in action_set
        assert "missing" not in action_set
        assert action_set.get("alert_level") == "critical"
        assert action_set.get("missing", "default") == "default"


class TestSessionState:
    def test_to_context(self):
        state = SessionState(
            user_role="resident",
            current_task="case_review",
            domain="radiology",
            session_phase="active",
        )
        context = state.to_context(query="test query", tags=["urgent"])
        assert context.user_role == "resident"
        assert context.task_type == "case_review"
        assert context.domain == "radiology"
        assert context.session_phase == "active"
        assert context.query == "test query"
        assert context.tags == ["urgent"]
