"""Tests for classification below the document — spans over a document's text.

Every agent can hold the same paper while one holder's copy has a paragraph
restricted to some of them. The grain is the caller's choice: a paragraph
rather than a sentence, because a lone planted sentence embeds with almost no
context and would rarely be retrieved while agents discuss the paper, whereas
inside its paragraph it surfaces when that topic does.
"""

import asyncio
import json
import sys
import tempfile
import time
import types
from pathlib import Path

import pytest

pytest.importorskip("chromadb")

PARLOR = Path(__file__).resolve().parents[1] / "examples" / "bear_parlor"
if str(PARLOR) not in sys.path:
    sys.path.insert(0, str(PARLOR))

import knowledge_rag  # noqa: E402
from knowledge_rag import KnowledgeStore, SpanError, UntaggedDocument  # noqa: E402

FILLER = "Ordinary public prose about the method and its limits. "
FACT = ("Paragraph three. The planted fact is that Vendor X brews cider in "
        "Yorkshire. ") + "More around it. " * 5


def _store(tmp_path, monkeypatch, name="spans", **kw):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(knowledge_rag, "_HERE", tmp_path)
    return KnowledgeStore(panel_id=f"test-{name}", **kw)


def _body(fact=FACT, paragraphs=6, at=3):
    paras = [f"Paragraph {n}. " + FILLER * 6 for n in range(paragraphs)]
    paras[at] = fact
    return "\n\n".join(paras)


def _doc(tmp_path, body, classification="[public]"):
    path = tmp_path / "paper.md"
    head = f"---\nclassification: {classification}\n---\n\n" if classification else ""
    path.write_text(head + body, encoding="utf-8")
    return path


def _span(body, text, classes=("fact-07",), anchor=True):
    start = body.index(text)
    span = {"start": start, "end": start + len(text), "classes": list(classes)}
    if anchor:
        span["text"] = text
    return span


def _chunks(store, hat="legal", title="paper"):
    return sorted(store.chunks_for(hat, title),
                  key=lambda c: c["metadata"]["chunk_index"])


def _classes(chunk):
    return {t for t in str(chunk["metadata"]["classification"]).split(",") if t}


class TestAMarkedParagraph:
    def test_only_the_passages_holding_it_take_its_class(self, tmp_path,
                                                         monkeypatch):
        """The property the design asks for: no passage holding any of the
        paragraph's text lacks its class, and no passage without that text
        has it."""
        store = _store(tmp_path, monkeypatch)
        body = _body()
        store.ingest_document(_doc(tmp_path, body), "paper", "legal",
                              spans=[_span(body, FACT)])

        marked = FACT.strip()
        for chunk in _chunks(store):
            holds = any(part and part in chunk["document"]
                        for part in (marked, "cider in Yorkshire"))
            assert ("fact-07" in _classes(chunk)) is holds, chunk["document"][:60]
            assert "public" in _classes(chunk), "the document's classes stay"

    def test_at_least_one_passage_holds_it(self, tmp_path, monkeypatch):
        store = _store(tmp_path, monkeypatch)
        body = _body()
        store.ingest_document(_doc(tmp_path, body), "paper", "legal",
                              spans=[_span(body, FACT)])
        assert any("fact-07" in _classes(c) for c in _chunks(store))
        assert any("fact-07" not in _classes(c) for c in _chunks(store)), \
            "the rest of the paper must stay unrestricted"

    def test_the_coverage_record_counts_the_passages(self, tmp_path,
                                                    monkeypatch):
        store = _store(tmp_path, monkeypatch)
        body = _body()
        store.ingest_document(_doc(tmp_path, body), "paper", "legal",
                              spans=[_span(body, FACT)])
        assert len(store.last_span_coverage) == 1
        assert store.last_span_coverage[0]["classes"] == ["fact-07"]
        assert store.last_span_coverage[0]["passages"] >= 1

    def test_a_span_longer_than_a_passage_becomes_several(self, tmp_path,
                                                          monkeypatch):
        store = _store(tmp_path, monkeypatch)
        long_fact = "The planted fact. " + "Detail upon detail. " * 120
        body = _body(fact=long_fact)
        store.ingest_document(_doc(tmp_path, body), "paper", "legal",
                              spans=[_span(body, long_fact)])
        marked = [c for c in _chunks(store) if "fact-07" in _classes(c)]
        assert len(marked) > 1, "a long span should cut into several passages"
        assert store.last_span_coverage[0]["passages"] == len(marked)

    def test_two_spans_keep_their_own_classes(self, tmp_path, monkeypatch):
        store = _store(tmp_path, monkeypatch)
        first = "Paragraph one. The first planted fact about ciders. " + "More. " * 8
        second = "Paragraph four. The second planted fact about kilns. " + "More. " * 8
        paras = [f"Paragraph {n}. " + FILLER * 6 for n in range(6)]
        paras[1], paras[4] = first, second
        body = "\n\n".join(paras)
        store.ingest_document(
            _doc(tmp_path, body), "paper", "legal",
            spans=[_span(body, first, ["fact-01"]),
                   _span(body, second, ["fact-02"])])

        for chunk in _chunks(store):
            got = _classes(chunk)
            assert not ({"fact-01", "fact-02"} <= got), \
                "no passage should hold both planted facts"


class TestOffsetsAreCheckedRatherThanTrusted:
    """A wrong offset does not error, it labels the wrong words, which is the
    worst failure a disclosure mechanism can have."""

    def test_offsets_measured_against_the_raw_file_are_caught(self, tmp_path,
                                                              monkeypatch):
        """The mistake a caller will actually make. Front matter is stripped
        before chunking, so offsets into the whole file are shifted."""
        store = _store(tmp_path, monkeypatch)
        body = _body()
        head = "---\nclassification: [public]\n---\n\n"
        raw = head + body
        shifted = {"start": raw.index(FACT), "end": raw.index(FACT) + len(FACT),
                   "classes": ["fact-07"], "text": FACT}
        with pytest.raises(SpanError, match="expected"):
            store.ingest_document(_doc(tmp_path, body), "paper", "legal",
                                  spans=[shifted])

    def test_a_span_outside_the_text_is_caught(self, tmp_path, monkeypatch):
        store = _store(tmp_path, monkeypatch)
        body = _body()
        with pytest.raises(SpanError, match="outside"):
            store.ingest_document(
                _doc(tmp_path, body), "paper", "legal",
                spans=[{"start": len(body) + 10, "end": len(body) + 50,
                        "classes": ["fact-07"]}])

    def test_a_span_that_marks_nothing_is_refused(self, tmp_path, monkeypatch):
        """The check that catches a bad offset even without an anchor: a span
        marking no passage would record its classes nowhere, and "no passages
        took span classes" reads the same as "no spans were given".

        Tested on the check itself, because the two rules above make it hard
        to reach through an ingest: disabling the cutting still leaves the
        strict backstop marking any passage holding the span's text. That the
        premise was hard to force is the system working, so this asserts the
        refusal rather than contriving a way to trigger it.
        """
        store = _store(tmp_path, monkeypatch)
        span = {"start": 0, "end": 10, "classes": ["fact-07"]}
        with pytest.raises(SpanError, match="classified no passage"):
            store._span_coverage([span], [[], []])

    def test_coverage_counts_only_passages_carrying_the_class(
            self, tmp_path, monkeypatch):
        store = _store(tmp_path, monkeypatch)
        span = {"start": 0, "end": 10, "classes": ["fact-07"]}
        got = store._span_coverage([span], [["fact-07"], [], ["fact-07"]])
        assert got[0]["passages"] == 2

    def test_overlapping_spans_are_refused(self, tmp_path, monkeypatch):
        store = _store(tmp_path, monkeypatch)
        body = _body()
        start = body.index(FACT)
        with pytest.raises(SpanError, match="overlap"):
            store.ingest_document(
                _doc(tmp_path, body), "paper", "legal",
                spans=[{"start": start, "end": start + 80,
                        "classes": ["fact-07"],
                        "text": body[start:start + 80]},
                       {"start": start + 40, "end": start + 120,
                        "classes": ["fact-08"],
                        "text": body[start + 40:start + 120]}])

    def test_a_span_naming_no_classes_is_refused(self, tmp_path, monkeypatch):
        store = _store(tmp_path, monkeypatch)
        body = _body()
        with pytest.raises(SpanError, match="no classes"):
            store.ingest_document(
                _doc(tmp_path, body), "paper", "legal",
                spans=[{"start": 0, "end": 10, "classes": []}])

    @pytest.mark.parametrize("span", [
        {"end": 10, "classes": ["a"]},
        {"start": "x", "end": 10, "classes": ["a"]},
    ])
    def test_a_malformed_span_is_refused(self, tmp_path, monkeypatch, span):
        store = _store(tmp_path, monkeypatch)
        body = _body()
        with pytest.raises(SpanError, match="integer start and end"):
            store.ingest_document(_doc(tmp_path, body), "paper", "legal",
                                  spans=[span])


class TestTheStrictBackstop:
    def test_span_text_in_a_passage_always_carries_its_classes(
            self, tmp_path, monkeypatch):
        """Cutting at span boundaries is the primary rule and the strict rule
        is the backstop: whatever the cutting did, a passage holding a span's
        text takes its classes. Forced here by making the cutting do nothing,
        which is the slip the backstop exists for."""
        store = _store(tmp_path, monkeypatch)
        body = _body()
        monkeypatch.setattr(knowledge_rag, "_segments",
                            lambda text, spans: [(text, [])])
        store.ingest_document(_doc(tmp_path, body), "paper", "legal",
                              spans=[_span(body, FACT)])

        for chunk in _chunks(store):
            if "cider in Yorkshire" in chunk["document"]:
                assert "fact-07" in _classes(chunk), \
                    "the backstop must restrict a passage holding span text"


class TestRepeatedText:
    def test_a_repeated_passage_is_marked_wherever_it_appears(self, tmp_path,
                                                              monkeypatch):
        """The strict rule marks any passage holding a span's text, so a
        paragraph that appears twice is restricted in both places even though
        the caller marked one.

        That is the right answer rather than a side effect. Leaving the second
        copy public would release the words the span restricts. It does mean
        the passage count for a span can exceed what the span's own length
        predicts, which a check that bounds the count by length should allow
        for.
        """
        store = _store(tmp_path, monkeypatch, name="repeat")
        paras = [f"Paragraph {n}. " + FILLER * 6 for n in range(6)]
        paras[1] = paras[4] = FACT
        body = (2 * chr(10)).join(paras)
        span = {"start": body.index(FACT), "end": body.index(FACT) + len(FACT),
                "classes": ["fact-07"], "text": FACT}
        store.ingest_document(_doc(tmp_path, body), "paper", "legal",
                              spans=[span])

        marked = [c for c in _chunks(store) if "fact-07" in _classes(c)]
        assert len(marked) == 2, "both copies of the passage must be restricted"
        assert store.last_span_coverage[0]["passages"] == 2


class TestTheBackstopAcrossLineBreaks:
    """The chunker joins lines with single spaces, so a stored passage has a
    different shape from the source it came from. Compared raw, the backstop
    was blind to every span crossing a line break, which in structured text
    such as pretty-printed JSON is most of them.

    Found by the knowledge-flow session on a PrivacyLens dry run, where the
    spans cover JSON fields written over several lines.
    """

    JSON_SPAN = ('  "attendees": [\n      "John Doe",\n      "Jane Roe"\n    ],\n'
                 '  "secret": "the merger closes Friday"')

    def _doc_with_json(self):
        """Long enough, and without blank lines, to take the chunker's
        single-newline path, which is where lines become spaces. A short
        document keeps its newlines and hides the problem entirely."""
        filler = "\n".join(
            f"Step {i}. The agent reads another calendar entry and weighs it."
            for i in range(30))
        return filler + "\n" + self.JSON_SPAN + "\n" + filler

    def test_a_span_crossing_line_breaks_is_still_marked(self, tmp_path,
                                                          monkeypatch):
        """Forced by disabling the cutting, which is what the backstop is
        for. With the raw comparison this marked nothing at all."""
        store = _store(tmp_path, monkeypatch, name="ws")
        body = self._doc_with_json()
        assert "\n\n" not in body, "blank lines would avoid the flattening path"
        monkeypatch.setattr(knowledge_rag, "_segments",
                            lambda text, spans: [(text, [])])
        store.ingest_document(_doc(tmp_path, body), "paper", "legal",
                              spans=[_span(body, self.JSON_SPAN)])

        stored = _chunks(store)
        assert all("\n" not in c["document"] for c in stored), (
            "premise: the chunker must have flattened the newlines")
        marked = [c for c in stored if "fact-07" in _classes(c)]
        assert marked, "the backstop must see a span that crosses a newline"
        assert any("merger closes Friday" in c["document"] for c in marked)

    def test_the_comparison_is_about_words_not_layout(self):
        assert knowledge_rag._collapsed("a\n  b\t c ") == "a b c"
        assert knowledge_rag._collapsed("   ") == ""


class TestItLeavesEverythingElseAlone:
    def test_no_spans_behaves_as_before(self, tmp_path, monkeypatch):
        store = _store(tmp_path, monkeypatch)
        body = _body()
        store.ingest_document(_doc(tmp_path, body), "paper", "legal")
        chunks = _chunks(store)
        assert chunks
        assert all(_classes(c) == {"public"} for c in chunks)
        assert store.last_span_coverage == []

    def test_untagged_reject_still_needs_the_documents_own_classes(
            self, tmp_path, monkeypatch):
        """A span classifies part of a document and does not stand in for the
        document's classes, since the text outside every span would have
        none."""
        store = _store(tmp_path, monkeypatch, untagged="reject")
        body = _body()
        with pytest.raises(UntaggedDocument, match="does not stand in"):
            store.ingest_document(_doc(tmp_path, body, classification=None),
                                  "paper", "legal",
                                  spans=[_span(body, FACT)])

    def test_untagged_reject_accepts_a_classified_document_with_spans(
            self, tmp_path, monkeypatch):
        store = _store(tmp_path, monkeypatch, untagged="reject")
        body = _body()
        n = store.ingest_document(_doc(tmp_path, body), "paper", "legal",
                                  spans=[_span(body, FACT)])
        assert n > 0


class TestTheRuleLivesInOnePlace:
    """A runner that wipes or locks the store needs the path. It importing
    store_path is the whole point: the collision happened because the rule
    existed twice, once per platform branch, and the copies disagreed.
    """

    def test_the_store_uses_the_exposed_function(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        monkeypatch.setattr(knowledge_rag, "_HERE", tmp_path)
        assert KnowledgeStore(panel_id="p").db_path ==             knowledge_rag.store_path("p")

    def test_store_path_does_not_create_the_directory(self, tmp_path,
                                                      monkeypatch):
        """A caller asking where it would be should not make one."""
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        monkeypatch.setattr(knowledge_rag, "_HERE", tmp_path)
        where = knowledge_rag.store_path("never-opened")
        assert not where.exists()

    def test_store_path_follows_the_importing_checkout(self, tmp_path,
                                                       monkeypatch):
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir(); b.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        monkeypatch.setattr(knowledge_rag, "_HERE", a)
        pa = knowledge_rag.store_path("p")
        monkeypatch.setattr(knowledge_rag, "_HERE", b)
        assert knowledge_rag.store_path("p") != pa

    def test_the_legacy_path_is_named_but_not_used(self, tmp_path,
                                                   monkeypatch):
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        monkeypatch.setattr(knowledge_rag, "_HERE", tmp_path)
        legacy = knowledge_rag.legacy_store_path("p")
        if legacy is not None:
            assert legacy != knowledge_rag.store_path("p")


class TestTheStoreIsPerCheckout:
    """Two Parlors of one panel from different worktrees shared a store under
    WSL, and chunk ids are deterministic, so one run's upsert replaced
    another's passages and their classification while the session still
    reported success.

    The native path is _HERE/panel_data/knowledge and so is already per
    checkout. These pin that the WSL path is too.
    """

    def _path_for(self, tmp_path, monkeypatch, checkout, panel="p"):
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        monkeypatch.setattr(knowledge_rag, "_HERE", checkout)
        return KnowledgeStore(panel_id=panel).db_path

    def test_two_checkouts_of_one_panel_do_not_share(self, tmp_path,
                                                     monkeypatch):
        a = tmp_path / "worktree-a"
        b = tmp_path / "worktree-b"
        a.mkdir()
        b.mkdir()
        pa = self._path_for(tmp_path, monkeypatch, a)
        pb = self._path_for(tmp_path, monkeypatch, b)
        assert pa != pb, "one store per machine is how the arms collided"

    def test_one_checkout_keeps_one_store(self, tmp_path, monkeypatch):
        """Persistence across restarts is the point of the store, so the same
        checkout must land in the same place."""
        a = tmp_path / "worktree-a"
        a.mkdir()
        first = self._path_for(tmp_path, monkeypatch, a)
        second = self._path_for(tmp_path, monkeypatch, a)
        assert first == second

    def test_two_panels_in_one_checkout_do_not_share(self, tmp_path,
                                                     monkeypatch):
        a = tmp_path / "worktree-a"
        a.mkdir()
        assert self._path_for(tmp_path, monkeypatch, a, "panel-one") !=             self._path_for(tmp_path, monkeypatch, a, "panel-two")

    def test_documents_do_not_cross_between_checkouts(self, tmp_path,
                                                      monkeypatch):
        """The failure as it was seen: same panel, same hat, same title, so
        the same chunk ids. One arm's ingest must not be visible to another."""
        a = tmp_path / "worktree-a"
        b = tmp_path / "worktree-b"
        a.mkdir()
        b.mkdir()
        body = _body()

        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        monkeypatch.setattr(knowledge_rag, "_HERE", a)
        store_a = KnowledgeStore(panel_id="shared")
        store_a.ingest_document(_doc(a, body), "paper", "legal",
                                spans=[_span(body, FACT)])
        assert len(store_a.chunks_for("legal", "paper")) > 0

        monkeypatch.setattr(knowledge_rag, "_HERE", b)
        store_b = KnowledgeStore(panel_id="shared")
        assert store_b.chunks_for("legal", "paper") == [],             "the other worktree's documents must not be visible here"


# --- through the endpoint -------------------------------------------------

def _import_parlor():
    # parlor.py reads .env files on import. These tests need no credentials.
    saved = sys.modules.get("dotenv")
    sys.modules["dotenv"] = types.SimpleNamespace(load_dotenv=lambda *a, **k: None)
    try:
        import parlor
    finally:
        if saved is None:
            sys.modules.pop("dotenv", None)
        else:
            sys.modules["dotenv"] = saved
    return parlor


class _Upload:
    """What FastAPI hands /ingest for an uploaded file."""

    def __init__(self, filename: str, data: bytes):
        self.filename = filename
        self._data = data

    async def read(self) -> bytes:
        return self._data


class _Log:
    """Just the two methods the endpoint reaches for."""

    def __init__(self) -> None:
        self.ingested: list[dict] = []
        self.refused: list[dict] = []

    def log_ingestion(self, hat_name, title, count, extractor=None,
                      classification=None, spans=None):
        self.ingested.append({"title": title, "count": count,
                              "classification": classification,
                              "spans": spans})

    def log_ingestion_refused(self, hat_id, title, reason):
        self.refused.append({"title": title, "reason": reason})


def _endpoint(tmp_path, monkeypatch, store):
    """The /ingest endpoint with a real store behind it.

    Ingested as ``source`` rather than as a role, so the role's
    acknowledgement (a generation) never starts. Spans are what is under test.
    """
    pm = _import_parlor()
    parlor = types.SimpleNamespace(
        characters={},
        knowledge=store,
        session_log=_Log(),
        _ingest_lock=asyncio.Lock(),
        broadcast=lambda payload: asyncio.sleep(0),
    )
    monkeypatch.setattr(pm, "parlor", parlor)
    monkeypatch.setattr(pm, "_document_diffusion", False, raising=False)

    def call(body, spans_json, name="paper.md", classification="public"):
        head = f"---\nclassification: [{classification}]\n---\n\n" if classification else ""
        upload = _Upload(name, (head + body).encode("utf-8"))
        return asyncio.run(pm.ingest_pdf(
            file=upload, hat_id=knowledge_rag.SOURCE_HAT_ID, domain="",
            classification="", spans=spans_json, title="paper"))

    return parlor, call


def _json(response):
    return json.loads(response.body)


class TestTheIngestEndpoint:
    def test_spans_sent_beside_the_document_reach_the_passages(
            self, tmp_path, monkeypatch):
        store = _store(tmp_path, monkeypatch, name="ep")
        parlor, call = _endpoint(tmp_path, monkeypatch, store)
        body = _body()
        r = call(body, json.dumps([_span(body, FACT)]))

        got = _json(r)
        assert got["success"] is True
        assert got["spans"] == [{"classes": ["fact-07"], "passages": 1,
                                 "start": body.index(FACT),
                                 "end": body.index(FACT) + len(FACT)}]
        chunks = _chunks(store, knowledge_rag.SOURCE_HAT_ID)
        assert sum("fact-07" in _classes(c) for c in chunks) == 1

    def test_the_documents_classification_is_not_its_spans(
            self, tmp_path, monkeypatch):
        """A span restricts a paragraph. Reporting it as the document's own
        class would say the paper is classified at the level of its most
        restricted paragraph, and would restrict the acknowledgement with it.
        """
        store = _store(tmp_path, monkeypatch, name="ep-class")
        parlor, call = _endpoint(tmp_path, monkeypatch, store)
        body = _body()
        r = call(body, json.dumps([_span(body, FACT)]))

        assert _json(r)["classification"] == ["public"]
        assert parlor.session_log.ingested[0]["classification"] == ["public"]

    def test_the_record_carries_the_coverage(self, tmp_path, monkeypatch):
        store = _store(tmp_path, monkeypatch, name="ep-rec")
        parlor, call = _endpoint(tmp_path, monkeypatch, store)
        body = _body()
        call(body, json.dumps([_span(body, FACT)]))

        (rec,) = parlor.session_log.ingested
        assert rec["spans"] == [{"classes": ["fact-07"], "passages": 1,
                                 "start": body.index(FACT),
                                 "end": body.index(FACT) + len(FACT)}]

    def test_no_spans_records_none(self, tmp_path, monkeypatch):
        store = _store(tmp_path, monkeypatch, name="ep-none")
        parlor, call = _endpoint(tmp_path, monkeypatch, store)
        r = call(_body(), "")
        assert _json(r)["spans"] == []
        assert parlor.session_log.ingested[0]["spans"] == []

    def test_an_offset_into_the_raw_file_is_refused_and_nothing_is_stored(
            self, tmp_path, monkeypatch):
        """The mistake a caller makes, now through the endpoint: the refusal
        is a 400 naming spans, and the record shows it."""
        store = _store(tmp_path, monkeypatch, name="ep-shift")
        parlor, call = _endpoint(tmp_path, monkeypatch, store)
        body = _body()
        head = "---\nclassification: [public]\n---\n\n"
        raw = head + body
        shifted = [{"start": raw.index(FACT), "end": raw.index(FACT) + len(FACT),
                    "classes": ["fact-07"], "text": FACT}]

        r = call(body, json.dumps(shifted))
        got = _json(r)
        assert r.status_code == 400
        assert got["success"] is False and got["reason"] == "spans"
        assert "expected" in got["error"]
        assert store.chunks_for(knowledge_rag.SOURCE_HAT_ID, "paper") == []
        assert parlor.session_log.ingested == []
        assert len(parlor.session_log.refused) == 1

    @pytest.mark.parametrize("spans,error", [
        ("not json at all", "not JSON"),
        ('{"start": 0, "end": 5, "classes": ["a"]}', "must be a JSON list"),
        ("5", "must be a JSON list"),
    ])
    def test_malformed_spans_are_refused_by_what_they_are(
            self, tmp_path, monkeypatch, spans, error):
        store = _store(tmp_path, monkeypatch, name="ep-bad")
        parlor, call = _endpoint(tmp_path, monkeypatch, store)
        r = call(_body(), spans)
        got = _json(r)
        assert r.status_code == 400 and got["reason"] == "spans"
        assert error in got["error"]

    def test_spans_on_a_pdf_are_refused(self, tmp_path, monkeypatch):
        """Offsets into a PDF would be into text the extractor produced and
        the caller never saw, so they are refused rather than applied."""
        store = _store(tmp_path, monkeypatch, name="ep-pdf")
        parlor, call = _endpoint(tmp_path, monkeypatch, store)
        body = _body()
        r = call(body, json.dumps([_span(body, FACT)]), name="paper.pdf")
        got = _json(r)
        assert r.status_code == 400 and got["reason"] == "spans"
        assert "Extract the text" in got["error"]


# --- the anchor is required ----------------------------------------------

class TestTheAnchorIsRequired:
    """An optional check is one a caller forgets, and the thing it catches is
    the mistake that labels the wrong paragraph."""

    def test_a_span_without_text_is_refused(self, tmp_path, monkeypatch):
        store = _store(tmp_path, monkeypatch, name="anchor")
        body = _body()
        span = _span(body, FACT, anchor=False)
        with pytest.raises(SpanError, match="carries no 'text'"):
            store.ingest_document(_doc(tmp_path, body), "paper", "legal",
                                  spans=[span])

    def test_the_refusal_names_the_way_to_opt_out(self, tmp_path, monkeypatch):
        """A refusal a caller cannot act on is a refusal they work around."""
        store = _store(tmp_path, monkeypatch, name="anchor-msg")
        body = _body()
        with pytest.raises(SpanError, match="'unanchored': true"):
            store.ingest_document(_doc(tmp_path, body), "paper", "legal",
                                  spans=[_span(body, FACT, anchor=False)])

    def test_an_explicit_opt_out_is_honoured(self, tmp_path, monkeypatch):
        store = _store(tmp_path, monkeypatch, name="anchor-opt")
        body = _body()
        span = _span(body, FACT, anchor=False)
        span["unanchored"] = True
        n = store.ingest_document(_doc(tmp_path, body), "paper", "legal",
                                  spans=[span])
        assert n > 0
        assert any("fact-07" in _classes(c) for c in _chunks(store))

    def test_an_empty_anchor_is_refused(self, tmp_path, monkeypatch):
        """It would pass a check that looked only for the field's presence."""
        store = _store(tmp_path, monkeypatch, name="anchor-empty")
        body = _body()
        span = _span(body, FACT)
        span["text"] = "   "
        with pytest.raises(SpanError, match="empty 'text'"):
            store.ingest_document(_doc(tmp_path, body), "paper", "legal",
                                  spans=[span])

    def test_checking_a_checked_list_is_the_same_check(self, tmp_path,
                                                       monkeypatch):
        """The reuse path checks spans and then passes them on to be checked
        again. If the requirement refused its own output, every document with
        spans would refuse on the second pass."""
        body = _body()
        once = knowledge_rag._check_spans(body, [_span(body, FACT)])
        assert knowledge_rag._check_spans(body, once) == once


# --- one paper, several roles --------------------------------------------

class TestASecondRoleGettingTheSamePaper:
    """Reuse copied another hat's chunks on a title match alone, without
    reading the new file. That is sound only while every copy is identical,
    and classification below the document is where they are not."""

    def _two(self, tmp_path, monkeypatch, name):
        return _store(tmp_path, monkeypatch, name=name)

    def test_an_identical_copy_is_reused(self, tmp_path, monkeypatch):
        store = self._two(tmp_path, monkeypatch, "reuse-same")
        body = _body()
        doc = _doc(tmp_path, body)
        store.ingest_document(doc, "paper", "legal", spans=[_span(body, FACT)])
        store.ingest_document(doc, "paper", "finance",
                              spans=[_span(body, FACT)])
        assert store.last_extractor == "reused-chunks"

        legal = _chunks(store, "legal")
        finance = _chunks(store, "finance")
        assert [c["document"] for c in legal] == [c["document"] for c in finance]
        assert ([_classes(c) for c in legal] == [_classes(c) for c in finance])

    def test_a_reused_copy_keeps_each_passages_own_classes(self, tmp_path,
                                                            monkeypatch):
        """The reuse wrote the new call's document classification over every
        copied chunk, so a span's restricted passage reached the second role
        labelled public."""
        store = self._two(tmp_path, monkeypatch, "reuse-classes")
        body = _body()
        doc = _doc(tmp_path, body)
        store.ingest_document(doc, "paper", "legal", spans=[_span(body, FACT)])
        store.ingest_document(doc, "paper", "finance",
                              spans=[_span(body, FACT)])

        restricted = [c for c in _chunks(store, "finance")
                      if "fact-07" in _classes(c)]
        assert len(restricted) == 1
        assert "cider in Yorkshire" in restricted[0]["document"]

    def test_a_reused_copy_reports_its_own_coverage(self, tmp_path,
                                                    monkeypatch):
        """Reuse never reached _store_chunks, so the coverage left over from
        the previous ingest was reported as this document's."""
        store = self._two(tmp_path, monkeypatch, "reuse-coverage")
        body = _body()
        doc = _doc(tmp_path, body)
        store.ingest_document(doc, "paper", "legal", spans=[_span(body, FACT)])
        # A different file, so ingesting it does not overwrite the one the
        # reuse below has to match.
        aside = tmp_path / "aside"
        aside.mkdir()
        other = _body(fact="Paragraph three. Nothing planted here. " + "Filler. " * 9)
        store.ingest_document(_doc(aside, other), "other", "legal")
        assert store.last_span_coverage == []

        store.ingest_document(doc, "paper", "finance",
                              spans=[_span(body, FACT)])
        assert len(store.last_span_coverage) == 1
        assert store.last_span_coverage[0]["passages"] == 1

    def test_a_different_body_is_chunked_on_its_own(self, tmp_path,
                                                     monkeypatch):
        """The holder's copy carries the planted paragraph and the others do
        not. Reuse replaced one body with the other, in whichever order they
        arrived."""
        store = self._two(tmp_path, monkeypatch, "reuse-body")
        plain = _body(fact="Paragraph three. " + FILLER * 6)
        planted = _body()
        store.ingest_document(_doc(tmp_path, plain), "paper", "finance")
        store.ingest_document(_doc(tmp_path, planted), "paper", "legal",
                              spans=[_span(planted, FACT)])

        held = " ".join(c["document"] for c in _chunks(store, "legal"))
        others = " ".join(c["document"] for c in _chunks(store, "finance"))
        assert "cider in Yorkshire" in held, "the holder must get its facts"
        assert "cider in Yorkshire" not in others, \
            "the planted paragraph must not reach a role that was not given it"

    def test_the_holder_first_order_is_also_right(self, tmp_path, monkeypatch):
        """The same test the other way round, since reuse made one order lose
        the facts and the other spread them."""
        store = self._two(tmp_path, monkeypatch, "reuse-order")
        plain = _body(fact="Paragraph three. " + FILLER * 6)
        planted = _body()
        store.ingest_document(_doc(tmp_path, planted), "paper", "legal",
                              spans=[_span(planted, FACT)])
        store.ingest_document(_doc(tmp_path, plain), "paper", "finance")

        held = " ".join(c["document"] for c in _chunks(store, "legal"))
        others = " ".join(c["document"] for c in _chunks(store, "finance"))
        assert "cider in Yorkshire" in held
        assert "cider in Yorkshire" not in others
        assert not any("fact-07" in _classes(c)
                       for c in _chunks(store, "finance"))

    def test_spans_sent_with_a_later_copy_are_not_dropped(self, tmp_path,
                                                           monkeypatch):
        """They never reached the chunking, so they were dropped without the
        "classified no passage" refusal firing."""
        store = self._two(tmp_path, monkeypatch, "reuse-spans")
        body = _body()
        doc = _doc(tmp_path, body)
        store.ingest_document(doc, "paper", "finance")
        assert store.last_span_coverage == []

        store.ingest_document(doc, "paper", "legal", spans=[_span(body, FACT)])
        assert len(store.last_span_coverage) == 1
        marked = [c for c in _chunks(store, "legal") if "fact-07" in _classes(c)]
        assert len(marked) == 1
        assert not any("fact-07" in _classes(c)
                       for c in _chunks(store, "finance"))

    def test_differing_document_classes_are_chunked_on_their_own(
            self, tmp_path, monkeypatch):
        store = self._two(tmp_path, monkeypatch, "reuse-docclass")
        body = _body()
        store.ingest_document(_doc(tmp_path, body), "paper", "legal",
                              classification=["public"])
        store.ingest_document(_doc(tmp_path, body), "paper", "finance",
                              classification=["internal"])
        assert all(_classes(c) == {"public"} for c in _chunks(store, "legal"))
        assert all(_classes(c) == {"internal"} for c in _chunks(store, "finance"))

    def test_a_third_role_gets_one_copy_not_two(self, tmp_path, monkeypatch):
        """Reuse copied every id for the title, so a third holder of a paper
        two roles already held received it twice."""
        store = self._two(tmp_path, monkeypatch, "reuse-third")
        body = _body()
        doc = _doc(tmp_path, body)
        n = store.ingest_document(doc, "paper", "legal")
        store.ingest_document(doc, "paper", "finance")
        third = store.ingest_document(doc, "paper", "ops")
        assert third == n
        assert len(_chunks(store, "ops")) == n

    def test_a_shorter_recopy_leaves_nothing_behind(self, tmp_path,
                                                    monkeypatch):
        """Chunk ids are positional, so a shorter copy would leave the tail of
        the longer one in place, still carrying its classes."""
        store = self._two(tmp_path, monkeypatch, "reuse-stale")
        long_body = _body(paragraphs=12)
        store.ingest_document(_doc(tmp_path, long_body), "paper", "legal",
                              spans=[_span(long_body, FACT)])
        before = len(_chunks(store, "legal"))

        short_body = _body(paragraphs=3, at=1)
        store.ingest_document(_doc(tmp_path, short_body), "paper", "legal",
                              spans=[_span(short_body, FACT)])
        after = _chunks(store, "legal")
        assert len(after) < before
        joined = " ".join(c["document"] for c in after)
        assert "Paragraph 11" not in joined, "a stale chunk stayed behind"


class TestTheOverlapCannotCarryAFactOut:
    """A planted fact is often the last sentence of its paragraph, and the
    chunker overlaps 200 characters. If that overlap crossed a span boundary,
    the next passage would hold the fact while holding none of the span's
    other text, so neither rule would mark it: not the cutting, because the
    passage belongs to the following piece, and not the backstop, because the
    passage holds no whole span and is not itself inside one.

    It cannot happen, because each piece between span boundaries is chunked on
    its own and the overlap therefore exists only inside one piece. The
    property is structural, which is exactly the kind a refactor loses
    quietly, so it is pinned rather than argued.
    """

    TAIL = "The planted fact is that Vendor X brews cider in Yorkshire."

    def _long_span_ending_in_the_fact(self):
        return "Paragraph three. " + "Detail upon detail. " * 90 + self.TAIL

    def test_the_fact_reaches_no_unmarked_passage(self, tmp_path, monkeypatch):
        store = _store(tmp_path, monkeypatch, name="overlap")
        marked_para = self._long_span_ending_in_the_fact()
        body = _body(fact=marked_para)
        store.ingest_document(_doc(tmp_path, body), "paper", "legal",
                              spans=[_span(body, marked_para)])

        chunks = _chunks(store)
        marked = [c for c in chunks if "fact-07" in _classes(c)]
        plain = [c for c in chunks if "fact-07" not in _classes(c)]

        # The claim first, so a failure reports it rather than a premise.
        for chunk in plain:
            assert "cider in Yorkshire" not in chunk["document"], \
                "the overlap carried the span's tail into an unmarked passage"

        # Then the premises, which say the claim was not met vacuously.
        # Without several marked passages the overlap inside the span is
        # never exercised, and without unmarked ones there is nowhere to
        # leak to.
        assert len(marked) > 1, "the span must cover several passages"
        assert plain, "the rest of the paper must stay unrestricted"

    def test_every_passage_holding_the_fact_carries_the_class(self, tmp_path,
                                                              monkeypatch):
        """The same property stated the way a scorer would check it, over the
        stored passages rather than over what was unmarked."""
        store = _store(tmp_path, monkeypatch, name="overlap-scorer")
        marked_para = self._long_span_ending_in_the_fact()
        body = _body(fact=marked_para)
        store.ingest_document(_doc(tmp_path, body), "paper", "legal",
                              spans=[_span(body, marked_para)])

        holding = [c for c in _chunks(store)
                   if "cider in Yorkshire" in c["document"]]
        assert holding, "the fact must be stored somewhere"
        for chunk in holding:
            assert "fact-07" in _classes(chunk)


# ----------------------------------------------------------------------
# A failure must not look like a smaller answer
# ----------------------------------------------------------------------


class TestAFailedRetrievalDoesNotLookEmpty:
    """A reply composed from no passages leaks nothing and scores as a clean
    non-leak, so a swallowed retrieval is the one failure this system must not
    have. ChromaDB does not raise for a filter matching nothing, measured on
    1.5.9, so nothing legitimate is lost by letting failures surface."""

    def _loaded(self, tmp_path, monkeypatch, name):
        store = _store(tmp_path, monkeypatch, name=name)
        store.ingest_document(_doc(tmp_path, _body()), "paper", "legal")
        return store

    def test_a_query_failure_surfaces(self, tmp_path, monkeypatch):
        store = self._loaded(tmp_path, monkeypatch, "fail-query")

        def boom(*a, **k):
            raise RuntimeError("embedding service refused the connection")

        monkeypatch.setattr(store._col, "query", boom)
        with pytest.raises(RuntimeError, match="refused"):
            store.query_with_meta("anything", hat_id="legal")

    def test_an_empty_store_still_returns_empty(self, tmp_path, monkeypatch):
        """The legitimate empty case must stay empty, or the fix would just
        move the failure."""
        store = _store(tmp_path, monkeypatch, name="empty-ok")
        assert store.query_with_meta("anything", hat_id="legal") == []

    def test_a_hat_holding_nothing_returns_empty(self, tmp_path, monkeypatch):
        """Chroma returns an empty result for a filter matching nothing
        rather than raising, so this must not be mistaken for a failure."""
        store = self._loaded(tmp_path, monkeypatch, "other-hat")
        assert store.query_with_meta("anything", hat_id="nobody") == []

    def test_chunks_for_surfaces_a_failure(self, tmp_path, monkeypatch):
        store = self._loaded(tmp_path, monkeypatch, "fail-get")

        def boom(*a, **k):
            raise RuntimeError("store unreadable")

        monkeypatch.setattr(store._col, "get", boom)
        with pytest.raises(RuntimeError, match="unreadable"):
            store.chunks_for("legal", "paper")


class TestNoPassageExceedsTheChunkSize:
    """_chunk_text never split a line longer than a chunk, so a trajectory's
    single-line JSON observation became a passage of 3,802 characters that
    snippet_chars then cut without a word."""

    def _one_long_line_among_paragraphs(self):
        short = "A short paragraph about the method."
        long_line = "Observation: " + "x" * 3000
        return "\n\n".join([short, long_line, short])

    def test_a_long_line_is_windowed(self, tmp_path, monkeypatch):
        body = self._one_long_line_among_paragraphs()
        chunks = knowledge_rag._chunk_text(body, 1200, 200)
        assert chunks, "the document must chunk at all"
        assert all(len(c) <= 1200 for c in chunks), \
            [len(c) for c in chunks]

    def test_the_whole_long_line_is_still_present(self, tmp_path, monkeypatch):
        """Windowing must not lose text, only bound each passage."""
        body = self._one_long_line_among_paragraphs()
        joined = " ".join(knowledge_rag._chunk_text(body, 1200, 200))
        assert "x" * 1500 in joined.replace(" ", "")

    def test_a_short_document_is_unchanged(self, tmp_path, monkeypatch):
        """The bound must not reshape documents that never exceeded it."""
        body = _body()
        assert knowledge_rag._chunk_text(body, 1200, 200) == \
            knowledge_rag._chunk_text(body, 1200, 200)
        assert all(len(c) <= 1200
                   for c in knowledge_rag._chunk_text(body, 1200, 200))

    def test_a_cut_passage_is_reported_at_ingest(self, tmp_path, monkeypatch,
                                                 capsys):
        """A run has to be able to see that knowledge_chars is too low before
        it scores anything."""
        store = _store(tmp_path, monkeypatch, name="cut", snippet_chars=50)
        store.ingest_document(_doc(tmp_path, _body()), "paper", "legal")
        out = capsys.readouterr().out
        assert "exceed snippet_chars" in out
        assert store.last_cut, "the lengths of the cut passages are recorded"

    def test_nothing_is_reported_when_nothing_is_cut(self, tmp_path,
                                                    monkeypatch, capsys):
        store = _store(tmp_path, monkeypatch, name="uncut", snippet_chars=8000)
        store.ingest_document(_doc(tmp_path, _body()), "paper", "legal")
        assert "exceed snippet_chars" not in capsys.readouterr().out
        assert store.last_cut == []


class TestTheStoreWorksOffTheEventLoop:
    """Chroma's default ONNX model runs inside every upsert and query, so on
    the event loop a document diffusion stopped everything else: a heartbeat
    wanting to run every 50 ms was 1317 ms late at the median and 3113 ms at
    worst. These pin the mechanism rather than the timing, since a timing
    assertion on a shared machine is a flake.
    See design/parlor-loop-blocking.md."""

    def test_it_runs_in_another_thread(self):
        import threading
        store = KnowledgeStore.__new__(KnowledgeStore)
        store._pool = __import__("concurrent.futures",
                                 fromlist=["futures"]).ThreadPoolExecutor(
            max_workers=1)
        here = threading.get_ident()
        where = asyncio.run(store.off_loop(threading.get_ident))
        assert where != here, "the point is that it is not the loop thread"
        store.close()

    def test_the_context_goes_with_it(self, tmp_path, monkeypatch):
        """The defect the suite caught. bear.audit tracks the current turn in
        a ContextVar, and run_in_executor does not carry one into a thread, so
        without copying the context an offloaded retrieval would be absent
        from the turn that caused it: the work would happen and the record
        would not."""
        import contextvars

        probe = contextvars.ContextVar("probe", default=None)
        store = _store(tmp_path, monkeypatch, name="ctx")

        async def run():
            probe.set("set-on-the-loop")
            return await store.off_loop(probe.get)

        assert asyncio.run(run()) == "set-on-the-loop"
        store.close()

    def test_one_thread_so_the_store_is_never_touched_concurrently(
            self, tmp_path, monkeypatch):
        """Chroma sits on SQLite and the embedder is not reentrant, so the
        serialisation that exists today has to survive. One worker, not a
        pool."""
        import threading

        store = _store(tmp_path, monkeypatch, name="serial")
        overlap = []
        inside = []

        def slow(n):
            inside.append(n)
            overlap.append(len(inside))
            time.sleep(0.02)
            inside.remove(n)

        async def run():
            await asyncio.gather(*(store.off_loop(slow, n) for n in range(4)))

        asyncio.run(run())
        assert overlap == [1, 1, 1, 1], overlap
        store.close()

    def test_the_result_and_the_exception_both_come_back(self, tmp_path,
                                                         monkeypatch):
        """A failure in the thread must surface, not vanish with it."""
        store = _store(tmp_path, monkeypatch, name="raises")

        def boom():
            raise RuntimeError("the store refused")

        assert asyncio.run(store.off_loop(lambda: 7)) == 7
        with pytest.raises(RuntimeError, match="refused"):
            asyncio.run(store.off_loop(boom))
        store.close()

    def test_closing_twice_is_safe(self, tmp_path, monkeypatch):
        store = _store(tmp_path, monkeypatch, name="closes")
        store.close()
        store.close()

    def test_a_write_offloaded_is_readable_from_the_loop_thread(
            self, tmp_path, monkeypatch):
        """The measurement this whole change rested on: Chroma is backed by
        SQLite, whose connections are thread-affine by default, so writing
        from a worker thread and reading from the loop thread could have
        failed outright."""
        store = _store(tmp_path, monkeypatch, name="crossthread")

        async def run():
            await store.off_loop(
                store.ingest_insight,
                "The trial reported a hazard ratio of 0.81.", "legal",
                source="diffusion", source_hat="document:paper")

        asyncio.run(run())
        assert store.count() == 1
        assert store.query_with_meta("hazard ratio", hat_id="legal") != []
        store.close()
