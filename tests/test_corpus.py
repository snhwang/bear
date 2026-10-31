"""Tests for bear.corpus."""

import tempfile
from pathlib import Path

import pytest

from bear.corpus import Corpus, CorpusLoadError, ValidationError
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


GOOD = """
  - id: rule-a
    type: constraint
    content: Rule A.
    scope: {required_tags: ['role:operator']}
"""


class TestLoadingFailsClosed:
    """A rule that cannot be loaded as written raises rather than loading as
    a weaker policy. Before 0.4.3 each of these loaded what parsed and logged
    the rest. tests/test_gate.py checks the same faults through the gate."""

    def write(self, d, **files):
        for name, items in files.items():
            (d / f"{name}.yaml").write_text("instructions:" + items, encoding="utf-8")
        return d

    @pytest.mark.parametrize("bad, says", [
        ("  - {id: rule-b, type: constriant, content: B.}\n", "constriant"),
        ("  - {id: rule-b, type: constraint}\n", "content"),
        ("  - {id: rule-b, type: constraint, content: B., priority: 101}\n", "priority"),
        ("  - {id: rule-b, type: constraint, content: B., acitons: {}}\n", "acitons"),
        ("  - {id: rule-b, type: constraint, content: B., metdata: {deny: true}}\n", "metdata"),
        ("  - {id: rule-b, type: tool, content: B., scope: {requried_tags: [x]}}\n",
         "scope.requried_tags"),
        ("  - {id: rule-b, type: tool, content: B., scope: {required_tag: [x]}}\n",
         "scope.required_tag"),
        ("  - just a string\n", "mapping"),
    ])
    def test_an_item_that_cannot_be_loaded_as_written_raises(self, tmp_path, bad, says):
        self.write(tmp_path, rules=GOOD + bad)
        with pytest.raises(CorpusLoadError, match=says) as exc:
            Corpus.from_directory(tmp_path)
        assert "rules.yaml, instruction 1" in str(exc.value)
        with pytest.raises(CorpusLoadError, match=says):
            Corpus.from_file(tmp_path / "rules.yaml")

    def test_the_message_names_the_id(self, tmp_path):
        self.write(tmp_path, rules=GOOD + "  - {id: rule-b, type: constriant, content: B.}\n")
        with pytest.raises(CorpusLoadError, match=r"instruction 1 \(rule-b\)"):
            Corpus.from_directory(tmp_path)

    def test_instructions_that_are_not_a_list_raise(self, tmp_path):
        (tmp_path / "rules.yaml").write_text("instructions:\n  rule-a: {type: constraint}\n",
                                            encoding="utf-8")
        with pytest.raises(CorpusLoadError, match="should be a list, not dict"):
            Corpus.from_directory(tmp_path)

    def test_an_id_defined_in_two_files_raises_naming_both(self, tmp_path):
        self.write(tmp_path, a=GOOD,
                   b="\n  - {id: rule-a, type: directive, content: Be careful.}\n")
        with pytest.raises(CorpusLoadError) as exc:
            Corpus.from_directory(tmp_path)
        assert "b.yaml, instruction 0 (rule-a)" in str(exc.value)
        assert "already defined at" in str(exc.value) and "a.yaml" in str(exc.value)

    def test_an_id_defined_twice_in_one_file_raises(self, tmp_path):
        self.write(tmp_path, rules=GOOD + GOOD)
        with pytest.raises(CorpusLoadError, match="already defined"):
            Corpus.from_file(tmp_path / "rules.yaml")

    def test_from_dicts_rejects_duplicates_and_unknown_keys(self):
        good = {"id": "a", "type": "constraint", "content": "A."}
        with pytest.raises(CorpusLoadError, match=r"item 1 \(a\): id 'a' is already defined at item 0"):
            Corpus.from_dicts([good, good])
        with pytest.raises(CorpusLoadError, match="scope.requried_tags"):
            Corpus.from_dicts([{**good, "scope": {"requried_tags": ["x"]}}])
        with pytest.raises(CorpusLoadError, match="constriant"):
            Corpus.from_dicts([{**good, "type": "constriant"}])

    def test_a_dumped_corpus_still_round_trips(self, sample_instructions):
        corpus = Corpus()
        corpus.add_many(sample_instructions)
        assert Corpus.from_dicts(corpus.to_dicts()).to_dicts() == corpus.to_dicts()

    def test_other_yaml_in_the_tree_is_still_skipped(self, tmp_path):
        self.write(tmp_path, rules=GOOD)
        (tmp_path / "settings.yaml").write_text("top_k: 5\n", encoding="utf-8")
        (tmp_path / "empty.yaml").write_text("", encoding="utf-8")
        assert [i.id for i in Corpus.from_directory(tmp_path)] == ["rule-a"]

    def test_add_still_replaces(self):
        corpus = Corpus()
        corpus.add(Instruction(id="a", type=InstructionType.DIRECTIVE, content="Old."))
        corpus.add(Instruction(id="a", type=InstructionType.DIRECTIVE, content="New."))
        assert len(corpus) == 1 and corpus.get("a").content == "New."


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
