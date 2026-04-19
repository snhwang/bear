"""Tests for bear.corpus."""

import tempfile
from pathlib import Path

import pytest

from bear.corpus import Corpus, ValidationError
from bear.models import Instruction, InstructionType, ScopeCondition


@pytest.fixture
def sample_instructions():
    return [
        Instruction(
            id="constraint-safety",
            type=InstructionType.CONSTRAINT,
            priority=100,
            content="Always prioritize safety.",
            tags=["safety"],
        ),
        Instruction(
            id="persona-teacher",
            type=InstructionType.PERSONA,
            priority=75,
            content="Adopt a teaching approach.",
            scope=ScopeCondition(user_roles=["student"]),
        ),
        Instruction(
            id="directive-concise",
            type=InstructionType.DIRECTIVE,
            priority=60,
            content="Be concise in responses.",
            scope=ScopeCondition(tags=["brevity"]),
        ),
    ]


class TestCorpus:
    def test_add_and_get(self, sample_instructions):
        corpus = Corpus()
        corpus.add(sample_instructions[0])
        assert len(corpus) == 1
        assert corpus.get("constraint-safety") is not None
        assert corpus.get("nonexistent") is None

    def test_add_many(self, sample_instructions):
        corpus = Corpus()
        corpus.add_many(sample_instructions)
        assert len(corpus) == 3

    def test_deduplication(self, sample_instructions):
        corpus = Corpus()
        corpus.add(sample_instructions[0])
        corpus.add(sample_instructions[0])  # Same ID
        assert len(corpus) == 1

    def test_remove(self, sample_instructions):
        corpus = Corpus()
        corpus.add(sample_instructions[0])
        assert corpus.remove("constraint-safety")
        assert len(corpus) == 0
        assert not corpus.remove("nonexistent")

    def test_contains(self, sample_instructions):
        corpus = Corpus()
        corpus.add(sample_instructions[0])
        assert "constraint-safety" in corpus
        assert "nonexistent" not in corpus

    def test_iter(self, sample_instructions):
        corpus = Corpus()
        corpus.add_many(sample_instructions)
        ids = [i.id for i in corpus]
        assert len(ids) == 3

    def test_filter_by_type(self, sample_instructions):
        corpus = Corpus()
        corpus.add_many(sample_instructions)
        constraints = corpus.filter(type=InstructionType.CONSTRAINT)
        assert len(constraints) == 1
        assert constraints[0].id == "constraint-safety"

    def test_filter_by_tags(self, sample_instructions):
        corpus = Corpus()
        corpus.add_many(sample_instructions)
        safety = corpus.filter(tags=["safety"])
        assert len(safety) == 1

    def test_filter_by_min_priority(self, sample_instructions):
        corpus = Corpus()
        corpus.add_many(sample_instructions)
        high_priority = corpus.filter(min_priority=70)
        assert len(high_priority) == 2

    def test_from_yaml_directory(self):
        yaml_content = """
instructions:
  - id: test-constraint
    type: constraint
    priority: 95
    content: "Test constraint content"
    scope:
      tags: [safety]
  - id: test-persona
    type: persona
    priority: 75
    content: "Test persona content"
    scope:
      user_roles: [admin]
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            yaml_file = Path(tmpdir) / "test.yaml"
            yaml_file.write_text(yaml_content)

            corpus = Corpus.from_directory(tmpdir)
            assert len(corpus) == 2
            assert corpus.get("test-constraint") is not None
            assert corpus.get("test-persona") is not None

    def test_from_yaml_file(self):
        yaml_content = """
instructions:
  - id: single-inst
    type: directive
    priority: 55
    content: "Single instruction"
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            f.flush()
            corpus = Corpus.from_file(f.name)
            assert len(corpus) == 1

    def test_from_nonexistent_directory(self):
        with pytest.raises(FileNotFoundError):
            Corpus.from_directory("/nonexistent/path")


class TestValidation:
    def test_validate_missing_requires(self):
        corpus = Corpus()
        corpus.add(Instruction(
            id="test-1",
            type=InstructionType.PROTOCOL,
            priority=70,
            content="Test",
            requires=["nonexistent"],
        ))
        errors = corpus.validate()
        assert len(errors) >= 1
        assert any("nonexistent" in e.message for e in errors)

    def test_validate_atypical_priority(self):
        corpus = Corpus()
        corpus.add(Instruction(
            id="low-constraint",
            type=InstructionType.CONSTRAINT,
            priority=10,  # Low for a constraint
            content="Test",
        ))
        errors = corpus.validate()
        warnings = [e for e in errors if e.severity == "warning"]
        assert len(warnings) >= 1

    def test_validate_empty_content(self):
        corpus = Corpus()
        corpus.add(Instruction(
            id="empty",
            type=InstructionType.DIRECTIVE,
            priority=50,
            content="   ",
        ))
        errors = corpus.validate()
        assert any("empty content" in e.message for e in errors)
