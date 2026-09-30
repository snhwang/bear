"""Tests for bear.marker_code: markers expressed from the meaning of text.

The stub embedder counts keyword hits in three hand-chosen dimensions, so every
cosine similarity here can be worked out on paper. No model is downloaded.
"""

from __future__ import annotations

import asyncio
import sys
import types
from types import SimpleNamespace

import numpy as np
import pytest

from bear.marker_code import (
    CachedInterpreter,
    EmbeddingInterpreter,
    EntailmentInterpreter,
    InsertedMarker,
    LLMInterpreter,
    MarkerCode,
    MarkerInterpreter,
    MarkerMeaning,
    cross_encoder_nli,
)

FLEE, FIGHT, FOOD = ("flee", "run", "escape"), ("fight", "challenge", "rival"), ("food", "eat", "graze")


class KeywordEmbedder:
    """One dimension per keyword group, counting hits, so similarities are set by hand."""

    def __init__(self, groups=(FLEE, FIGHT, FOOD)):
        self.groups = groups
        self.calls = 0

    def embed(self, texts, is_query=False):
        self.calls += 1
        rows = [[sum(t.lower().count(w) for w in group) for group in self.groups] for t in texts]
        return np.array(rows, dtype=np.float32)


# flee -> (2, 0, 0), challenge -> (0, 2, 0), food -> (0, 0, 2)
CODE = MarkerCode.from_dict({
    "[!flee]": "run away and escape",
    "[!challenge(nearest)]": "fight the nearest rival",
    "[!approach(item=food)]": "go and eat food",
})


def interpret(interpreter, text):
    return asyncio.run(interpreter.interpret(text))


def embedding(radius=0.5, **kw):
    return EmbeddingInterpreter(CODE, KeywordEmbedder(), radius=radius, **kw)


# --- the code -----------------------------------------------------------------


def test_meaning_requires_exactly_one_marker():
    with pytest.raises(ValueError):
        MarkerMeaning("flee", "run away")
    with pytest.raises(ValueError):
        MarkerMeaning("[!flee] [!rally]", "run away")
    with pytest.raises(ValueError):
        MarkerMeaning("[!flee]", "   ")


def test_meaning_trims_whitespace():
    entry = MarkerMeaning("  [!flee] ", "  run away ")
    assert entry.signature == "[!flee]" and entry.meaning == "run away"


def test_code_rejects_duplicates_and_empty():
    with pytest.raises(ValueError):
        MarkerCode((MarkerMeaning("[!flee]", "a"), MarkerMeaning("[!flee]", "b")))
    with pytest.raises(ValueError):
        MarkerCode(())


def test_from_dict_keeps_order():
    assert CODE.signatures == ("[!flee]", "[!challenge(nearest)]", "[!approach(item=food)]")


def test_shuffled_permutes_meanings_with_no_fixed_points():
    shuffled = CODE.shuffled(seed=7)
    assert shuffled.signatures == CODE.signatures
    original = [m.meaning for m in CODE.meanings]
    permuted = [m.meaning for m in shuffled.meanings]
    assert sorted(permuted) == sorted(original)
    assert all(a != b for a, b in zip(original, permuted))


def test_shuffled_is_reproducible():
    assert CODE.shuffled(seed=3) == CODE.shuffled(seed=3)


def test_shuffling_a_single_entry_changes_nothing():
    single = MarkerCode.from_dict({"[!flee]": "run away"})
    assert single.shuffled(seed=1) == single


# --- the embedding interpreter ---------------------------------------------------


def test_existing_markers_are_stripped_first():
    result = interpret(embedding(), "When wolves come, you run [!rally].")
    assert result.text == "When wolves come, you run [!flee]."
    assert "[!rally]" not in result.text


def test_nul_never_reaches_interpreted_text():
    result = interpret(embedding(), "You\x00 run.")
    assert result.text == "You run [!flee]."


def test_marker_goes_before_the_clause_punctuation():
    result = interpret(embedding(), "Wolves come! You run. You graze on food.")
    assert result.text == "Wolves come! You run [!flee]. You graze on food [!approach(item=food)]."
    assert [(m.clause_index, m.signature) for m in result.inserted] == [
        (1, "[!flee]"), (2, "[!approach(item=food)]"),
    ]


def test_clause_outside_the_radius_gets_nothing():
    result = interpret(embedding(), "You nap in the sun.")
    assert result.text == "You nap in the sun."
    assert result.inserted == ()


def test_only_the_best_marker_by_default():
    # (1, 0, 2): cosine 0.447 with flee and 0.894 with food
    result = interpret(embedding(radius=0.4), "You run to eat food.")
    assert result.text == "You run to eat food [!approach(item=food)]."


def test_max_per_clause_admits_the_runner_up():
    result = interpret(embedding(radius=0.4, max_per_clause=2), "You run to eat food.")
    assert result.text == "You run to eat food [!approach(item=food)] [!flee]."


def test_radius_boundary():
    # cosine with food is 2 / sqrt(5) = 0.8944
    assert interpret(embedding(radius=0.894), "You run to eat food.").inserted
    assert not interpret(embedding(radius=0.895), "You run to eat food.").inserted


def test_scores_are_recorded():
    result = interpret(embedding(), "You run.")
    assert result.inserted == (InsertedMarker(0, "[!flee]", 1.0),)


def test_ties_go_to_the_first_marker_in_the_code():
    code = MarkerCode.from_dict({"[!flee]": "run away", "[!sprint]": "run fast"})
    result = interpret(EmbeddingInterpreter(code, KeywordEmbedder(), radius=0.5), "You run.")
    assert [m.signature for m in result.inserted] == ["[!flee]"]


def test_newlines_separate_clauses():
    result = interpret(embedding(), "You run\nYou eat food")
    assert result.text == "You run [!flee]\nYou eat food [!approach(item=food)]"


def test_empty_text():
    result = interpret(embedding(), "")
    assert result.text == "" and result.inserted == ()


def test_meanings_are_embedded_once():
    embedder = KeywordEmbedder()
    interpreter = EmbeddingInterpreter(CODE, embedder, radius=0.5)
    interpret(interpreter, "You run.")
    interpret(interpreter, "You eat food.")
    assert embedder.calls == 3  # meanings once, then one call per text


def test_bad_radius_and_max_per_clause():
    with pytest.raises(ValueError):
        embedding(radius=1.5)
    with pytest.raises(ValueError):
        embedding(max_per_clause=0)


def test_scrambled_code_attaches_the_wrong_marker():
    interpreter = EmbeddingInterpreter(CODE.shuffled(seed=7), KeywordEmbedder(), radius=0.5)
    result = interpret(interpreter, "You run and escape.")
    assert len(result.inserted) == 1
    assert result.inserted[0].signature != "[!flee]"


def test_interpreters_satisfy_the_protocol():
    assert isinstance(embedding(), MarkerInterpreter)
    assert isinstance(LLMInterpreter(CODE, StubLLM("")), MarkerInterpreter)
    assert isinstance(CachedInterpreter(embedding()), MarkerInterpreter)
    assert isinstance(EntailmentInterpreter(CODE, lambda pairs: []), MarkerInterpreter)


# --- the model interpreter -------------------------------------------------------


class StubLLM:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    async def generate(self, *, system, user, temperature, max_tokens):
        self.calls.append({"system": system, "user": user, "temperature": temperature})
        return SimpleNamespace(content=self.reply)


PROSE = "When wolves come, you run away."


def model(reply):
    llm = StubLLM(reply)
    return LLMInterpreter(CODE, llm), llm


def test_model_sees_the_vocabulary_and_stripped_prose():
    interpreter, llm = model(PROSE)
    interpret(interpreter, "When wolves come, you run away [!rally].")
    prompt = llm.calls[0]["user"]
    assert "[!flee]: run away and escape" in prompt
    assert PROSE in prompt and "[!rally]" not in prompt
    assert llm.calls[0]["temperature"] == 0.0


def test_faithful_reply_is_accepted():
    interpreter, _ = model("When wolves come, you run away [!flee].")
    result = interpret(interpreter, PROSE)
    assert result.text == "When wolves come, you run away [!flee]."
    assert result.inserted == (InsertedMarker(0, "[!flee]", None),)
    assert result.discarded is None


def test_marker_after_the_punctuation_is_moved_before_it():
    interpreter, _ = model("When wolves come, you run away. [!flee]")
    assert interpret(interpreter, PROSE).text == "When wolves come, you run away [!flee]."


def test_marker_outside_the_vocabulary_is_removed():
    interpreter, _ = model("When wolves come, you run away [!flee] [!teleport].")
    result = interpret(interpreter, PROSE)
    assert result.text == "When wolves come, you run away [!flee]."
    assert result.rejected == ("[!teleport]",)


def test_reply_that_changed_the_prose_is_discarded():
    interpreter, _ = model("When wolves arrive, you run away [!flee].")
    result = interpret(interpreter, PROSE)
    assert result.text == PROSE
    assert result.inserted == ()
    assert result.discarded


def test_thinking_and_code_fences_are_removed():
    interpreter, _ = model("<think>it runs</think>```text\nWhen wolves come, you run away [!flee].\n```")
    assert interpret(interpreter, PROSE).text == "When wolves come, you run away [!flee]."


def test_repeated_marker_in_one_clause_collapses():
    interpreter, _ = model("When wolves come, you run away [!flee] [!flee].")
    result = interpret(interpreter, PROSE)
    assert result.text.count("[!flee]") == 1


def test_markers_land_in_their_own_clauses():
    prose = "Wolves come. You run away. You eat food."
    interpreter, _ = model("Wolves come. You run away [!flee]. You eat food [!approach(item=food)].")
    result = interpret(interpreter, prose)
    assert result.text == "Wolves come. You run away [!flee]. You eat food [!approach(item=food)]."
    assert [m.clause_index for m in result.inserted] == [1, 2]


def test_model_with_no_prose_is_not_called():
    interpreter, llm = model("anything")
    assert interpret(interpreter, "[!flee]").text == ""
    assert llm.calls == []


# --- the cache ---------------------------------------------------------------


def test_cache_serves_repeats_keyed_on_prose():
    embedder = KeywordEmbedder()
    cached = CachedInterpreter(EmbeddingInterpreter(CODE, embedder, radius=0.5))
    first = interpret(cached, "You run.")
    again = interpret(cached, "You run.")
    same_prose = interpret(cached, "You run [!rally].")
    assert first is again is same_prose
    assert (cached.misses, cached.hits) == (1, 2)
    assert embedder.calls == 2  # meanings, then the one distinct text


# --- the entailment interpreter ------------------------------------------------------


def stub_nli(table, default=0.05):
    """Entailment probabilities looked up by (premise phrase, hypothesis phrase)."""
    calls = []

    def score(pairs):
        calls.append(list(pairs))
        out = []
        for premise, hypothesis in pairs:
            p = default
            for (in_premise, in_hypothesis), prob in table.items():
                if in_premise in premise.lower() and in_hypothesis in hypothesis.lower():
                    p = prob
            out.append(p)
        return out

    score.calls = calls
    return score


# CODE's meanings: "run away and escape", "fight the nearest rival", "go and eat food"
NLI = {
    ("you run", "run away"): 0.95,
    ("rather than fleeing", "fight"): 0.90,
    ("eat", "eat food"): 0.90,
}


def entailment(threshold=0.5, table=NLI, **kw):
    return EntailmentInterpreter(CODE, stub_nli(table), threshold=threshold, **kw)


def test_entailment_inserts_the_most_probable_marker():
    result = interpret(entailment(), "When wolves come, you run away.")
    assert result.text == "When wolves come, you run away [!flee]."
    assert result.inserted == (InsertedMarker(0, "[!flee]", 0.95),)


def test_entailment_follows_the_model_on_negation():
    # the stub plays a model that reads "rather than fleeing" as not fleeing
    result = interpret(entailment(), "Charges at threats rather than fleeing.")
    assert result.text == "Charges at threats rather than fleeing [!challenge(nearest)]."


def test_entailment_below_threshold_gets_nothing():
    assert interpret(entailment(threshold=0.96), "You run away.").inserted == ()


def test_entailment_scores_all_pairs_in_one_call():
    nli = stub_nli(NLI)
    interpret(EntailmentInterpreter(CODE, nli), "You run away. You eat food.")
    assert len(nli.calls) == 1
    assert len(nli.calls[0]) == 2 * len(CODE.meanings)


def test_hypothesis_template_is_applied():
    nli = stub_nli(NLI)
    interpret(EntailmentInterpreter(CODE, nli, hypothesis="The creature will {meaning}."), "You run.")
    assert all(h.startswith("The creature will ") for _, h in nli.calls[0])


def test_entailment_bad_arguments():
    with pytest.raises(ValueError):
        entailment(threshold=1.5)
    with pytest.raises(ValueError):
        entailment(max_per_clause=0)
    with pytest.raises(ValueError):
        EntailmentInterpreter(CODE, stub_nli(NLI), hypothesis="no placeholder")


# --- the cross-encoder adapter ------------------------------------------------------


def fake_cross_encoder(monkeypatch, labels, output):
    """Install a stand-in sentence_transformers module and record model loads."""
    loads = []

    class FakeCrossEncoder:
        def __init__(self, name, device=None):
            loads.append((name, device))
            self.config = SimpleNamespace(id2label=labels)

        def predict(self, pairs, show_progress_bar=False):
            return np.tile(np.asarray(output, dtype=np.float32), (len(pairs), 1))

    module = types.ModuleType("sentence_transformers")
    module.CrossEncoder = FakeCrossEncoder
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    return loads


def test_cross_encoder_reads_the_entailment_column(monkeypatch):
    fake_cross_encoder(monkeypatch, {0: "CONTRADICTION", 1: "NEUTRAL", 2: "ENTAILMENT"}, [0.0, 0.0, 2.0])
    probs = cross_encoder_nli("fake")([("a", "b"), ("c", "d")])
    assert np.allclose(probs, np.exp(2) / (2 + np.exp(2)))


def test_cross_encoder_falls_back_to_the_standard_order(monkeypatch):
    fake_cross_encoder(monkeypatch, {0: "LABEL_0", 1: "LABEL_1", 2: "LABEL_2"}, [0.0, 2.0, 0.0])
    probs = cross_encoder_nli("fake")([("a", "b")])
    assert np.allclose(probs, np.exp(2) / (2 + np.exp(2)))


def test_cross_encoder_does_not_softmax_probabilities_twice(monkeypatch):
    fake_cross_encoder(monkeypatch, {0: "contradiction", 1: "entailment", 2: "neutral"}, [0.2, 0.7, 0.1])
    assert np.allclose(cross_encoder_nli("fake")([("a", "b")]), 0.7)


def test_cross_encoder_loads_lazily_once_on_the_cpu(monkeypatch):
    loads = fake_cross_encoder(monkeypatch, {1: "entailment"}, [0.0, 1.0, 0.0])
    score = cross_encoder_nli("fake")
    assert loads == []
    score([("a", "b")])
    score([("c", "d")])
    assert loads == [("fake", "cpu")]
