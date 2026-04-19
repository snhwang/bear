"""Tests for bear.twin — TwinBuilder incremental accumulation."""

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from bear import Instruction, InstructionType
from bear.twin import (
    ObservationKind,
    TwinBuilder,
    _chunk_text,
    _default_priority,
    _instruction_to_dict,
    _parse_type,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_llm(extract_response: str = "[]") -> MagicMock:
    """Create a mock LLM that returns the given extraction response."""
    llm = MagicMock()
    resp = MagicMock()
    resp.content = extract_response
    llm.generate = AsyncMock(return_value=resp)
    llm.is_available = MagicMock(return_value=True)
    return llm


def _extract_response(items: list[dict]) -> str:
    """Build a mock LLM response for the extraction prompt."""
    return json.dumps(items)


# ---------------------------------------------------------------------------
# Unit tests: helpers
# ---------------------------------------------------------------------------


class TestParseType:
    def test_valid_types(self):
        assert _parse_type("persona") == InstructionType.PERSONA
        assert _parse_type("constraint") == InstructionType.CONSTRAINT
        assert _parse_type("directive") == InstructionType.DIRECTIVE
        assert _parse_type("protocol") == InstructionType.PROTOCOL
        assert _parse_type("fallback") == InstructionType.FALLBACK

    def test_invalid_defaults_to_directive(self):
        assert _parse_type("unknown") == InstructionType.DIRECTIVE
        assert _parse_type("") == InstructionType.DIRECTIVE


class TestDefaultPriority:
    def test_priorities_by_type(self):
        assert _default_priority(InstructionType.CONSTRAINT) == 90
        assert _default_priority(InstructionType.PERSONA) == 80
        assert _default_priority(InstructionType.PROTOCOL) == 70
        assert _default_priority(InstructionType.DIRECTIVE) == 60
        assert _default_priority(InstructionType.FALLBACK) == 20


class TestInstructionToDict:
    def test_minimal(self):
        inst = Instruction(
            id="test-1",
            type=InstructionType.DIRECTIVE,
            priority=60,
            content="Test content",
        )
        d = _instruction_to_dict(inst)
        assert d["id"] == "test-1"
        assert d["type"] == "directive"
        assert d["priority"] == 60
        assert d["content"] == "Test content"
        # No empty optional fields
        assert "tags" not in d
        assert "metadata" not in d

    def test_with_tags_and_metadata(self):
        inst = Instruction(
            id="test-2",
            type=InstructionType.PERSONA,
            priority=80,
            content="Persona content",
            tags=["alice", "twin"],
            metadata={"source": "observation"},
        )
        d = _instruction_to_dict(inst)
        assert d["tags"] == ["alice", "twin"]
        assert d["metadata"]["source"] == "observation"


# ---------------------------------------------------------------------------
# TwinBuilder: initialization
# ---------------------------------------------------------------------------


class TestTwinBuilderInit:
    def test_creates_directory(self, tmp_path):
        twin_dir = tmp_path / "twins" / "alice"
        twin = TwinBuilder(twin_dir, name="Alice")
        assert twin_dir.exists()
        assert twin.name == "Alice"
        assert twin.instruction_count == 0

    def test_creates_meta_file(self, tmp_path):
        twin_dir = tmp_path / "alice"
        twin = TwinBuilder(twin_dir, name="Alice")
        meta_path = twin_dir / "meta.yaml"
        assert meta_path.exists()
        with open(meta_path) as f:
            meta = yaml.safe_load(f)
        assert meta["name"] == "Alice"
        assert "created" in meta

    def test_loads_existing_corpus(self, tmp_path):
        twin_dir = tmp_path / "alice"
        twin_dir.mkdir(parents=True)
        # Write a pre-existing instructions file
        instructions = {
            "instructions": [{
                "id": "twin-alice-1",
                "type": "directive",
                "priority": 60,
                "content": "You speak softly.",
                "tags": ["alice", "twin"],
                "scope": {"required_tags": ["alice"]},
            }]
        }
        with open(twin_dir / "instructions.yaml", "w") as f:
            yaml.dump(instructions, f)
        # Also need meta
        with open(twin_dir / "meta.yaml", "w") as f:
            yaml.dump({"name": "Alice", "instruction_count": 1, "observation_count": 0}, f)

        twin = TwinBuilder(twin_dir, name="Alice")
        assert twin.instruction_count == 1


# ---------------------------------------------------------------------------
# TwinBuilder: observe
# ---------------------------------------------------------------------------


class TestTwinBuilderObserve:
    @pytest.mark.asyncio
    async def test_observe_adds_instructions(self, tmp_path):
        llm = _mock_llm(_extract_response([
            {
                "type": "persona",
                "content": "You are warm and approachable.",
                "topics": ["personality", "warmth"],
                "action": "add",
            },
        ]))
        twin = TwinBuilder(tmp_path / "alice", name="Alice", llm=llm)

        added = await twin.observe("Alice is warm and approachable")
        assert len(added) == 1
        assert added[0].type == InstructionType.PERSONA
        assert "warm and approachable" in added[0].content
        assert twin.instruction_count == 1

    @pytest.mark.asyncio
    async def test_observe_skips_duplicates(self, tmp_path):
        llm = _mock_llm(_extract_response([
            {"type": "directive", "content": "Already covered.", "topics": [], "action": "skip"},
        ]))
        twin = TwinBuilder(tmp_path / "alice", name="Alice", llm=llm)
        added = await twin.observe("something already known")
        assert len(added) == 0

    @pytest.mark.asyncio
    async def test_observe_persists_to_disk(self, tmp_path):
        llm = _mock_llm(_extract_response([
            {
                "type": "directive",
                "content": "You use bullet points when stressed.",
                "topics": ["stress", "communication"],
                "action": "add",
            },
        ]))
        twin_dir = tmp_path / "alice"
        twin = TwinBuilder(twin_dir, name="Alice", llm=llm)
        await twin.observe("When stressed she uses bullet points")

        # Verify instructions file
        assert (twin_dir / "instructions.yaml").exists()
        with open(twin_dir / "instructions.yaml") as f:
            data = yaml.safe_load(f)
        assert len(data["instructions"]) == 1

        # Verify observation log
        assert (twin_dir / "observations.yaml").exists()
        with open(twin_dir / "observations.yaml") as f:
            obs = yaml.safe_load(f)
        assert len(obs) == 1
        assert obs[0]["kind"] == "note"

    @pytest.mark.asyncio
    async def test_observe_with_kind(self, tmp_path):
        llm = _mock_llm(_extract_response([
            {
                "type": "protocol",
                "content": "When discussing budgets, you present numbers first.",
                "topics": ["budget", "presentation"],
                "action": "add",
            },
        ]))
        twin = TwinBuilder(tmp_path / "alice", name="Alice", llm=llm)
        added = await twin.observe(
            "Q: How do you present budgets? A: I always lead with the numbers.",
            kind="transcript",
        )
        assert len(added) == 1
        assert added[0].type == InstructionType.PROTOCOL

    @pytest.mark.asyncio
    async def test_observe_refine_existing(self, tmp_path):
        twin_dir = tmp_path / "alice"
        # First observation
        llm = _mock_llm(_extract_response([
            {
                "type": "directive",
                "content": "You are sarcastic.",
                "topics": ["humor", "sarcasm"],
                "action": "add",
            },
        ]))
        twin = TwinBuilder(twin_dir, name="Alice", llm=llm)
        added = await twin.observe("She's sarcastic")
        old_id = added[0].id
        assert twin.instruction_count == 1

        # Second observation refines the first
        llm2 = _mock_llm(_extract_response([
            {
                "type": "directive",
                "content": "You are sarcastic with strangers but warm with friends.",
                "topics": ["humor", "sarcasm", "relationships"],
                "action": "refine",
                "refines_id": old_id,
            },
        ]))
        twin.llm = llm2
        added2 = await twin.observe("Actually she's warm with friends, only sarcastic with strangers")
        assert len(added2) == 1
        # Same ID, updated content
        assert added2[0].id == old_id
        assert "warm with friends" in added2[0].content
        # Still only 1 instruction (refined, not duplicated)
        assert twin.instruction_count == 1

    @pytest.mark.asyncio
    async def test_observe_multiple_instructions(self, tmp_path):
        llm = _mock_llm(_extract_response([
            {
                "type": "persona",
                "content": "You are a careful listener.",
                "topics": ["personality"],
                "action": "add",
            },
            {
                "type": "constraint",
                "content": "You never interrupt others.",
                "topics": ["conversation"],
                "action": "add",
            },
        ]))
        twin = TwinBuilder(tmp_path / "alice", name="Alice", llm=llm)
        added = await twin.observe("She listens carefully and never interrupts")
        assert len(added) == 2
        assert twin.instruction_count == 2

    @pytest.mark.asyncio
    async def test_observe_no_llm_raises(self, tmp_path):
        twin = TwinBuilder(tmp_path / "alice", name="Alice", llm=None)
        with pytest.raises(RuntimeError, match="No LLM configured"):
            await twin.observe("test")

    @pytest.mark.asyncio
    async def test_observe_llm_failure_returns_empty(self, tmp_path):
        llm = MagicMock()
        llm.generate = AsyncMock(side_effect=Exception("API error"))
        twin = TwinBuilder(tmp_path / "alice", name="Alice", llm=llm)
        added = await twin.observe("test observation")
        assert added == []

    @pytest.mark.asyncio
    async def test_observe_bad_json_returns_empty(self, tmp_path):
        llm = _mock_llm("not valid json at all")
        twin = TwinBuilder(tmp_path / "alice", name="Alice", llm=llm)
        added = await twin.observe("test")
        assert added == []


# ---------------------------------------------------------------------------
# TwinBuilder: chat
# ---------------------------------------------------------------------------


class TestTwinBuilderChat:
    @pytest.mark.asyncio
    async def test_chat_requires_llm(self, tmp_path):
        twin = TwinBuilder(tmp_path / "alice", name="Alice", llm=None)
        with pytest.raises(RuntimeError, match="No LLM configured"):
            await twin.chat("hello")

    @pytest.mark.asyncio
    async def test_chat_uses_retriever_and_composer(self, tmp_path):
        # Build a twin with one instruction
        llm = _mock_llm(_extract_response([
            {
                "type": "persona",
                "content": "You are Alice, a thoughtful person.",
                "topics": ["personality"],
                "action": "add",
            },
        ]))
        twin = TwinBuilder(tmp_path / "alice", name="Alice", llm=llm)
        await twin.observe("Alice is thoughtful")

        # Now set up chat response
        chat_resp = MagicMock()
        chat_resp.content = "That's a great question! Let me think about it."
        llm.generate = AsyncMock(return_value=chat_resp)

        response = await twin.chat("How would you handle this?")
        assert response == "That's a great question! Let me think about it."
        # Verify generate was called with system guidance
        call_kwargs = llm.generate.call_args
        assert call_kwargs.kwargs.get("system") or call_kwargs[1].get("system", "")


# ---------------------------------------------------------------------------
# TwinBuilder: persistence across reloads
# ---------------------------------------------------------------------------


class TestTwinBuilderPersistence:
    @pytest.mark.asyncio
    async def test_reload_preserves_instructions(self, tmp_path):
        twin_dir = tmp_path / "alice"
        llm = _mock_llm(_extract_response([
            {
                "type": "directive",
                "content": "You speak in short sentences.",
                "topics": ["speech"],
                "action": "add",
            },
        ]))
        twin1 = TwinBuilder(twin_dir, name="Alice", llm=llm)
        await twin1.observe("She speaks in short sentences")
        assert twin1.instruction_count == 1

        # Reload from disk
        twin2 = TwinBuilder(twin_dir, name="Alice", llm=llm)
        assert twin2.instruction_count == 1
        instructions = twin2.list_instructions()
        assert "short sentences" in instructions[0].content

    @pytest.mark.asyncio
    async def test_incremental_accumulation(self, tmp_path):
        twin_dir = tmp_path / "alice"

        # Session 1
        llm1 = _mock_llm(_extract_response([
            {"type": "persona", "content": "You are optimistic.", "topics": ["personality"], "action": "add"},
        ]))
        twin1 = TwinBuilder(twin_dir, name="Alice", llm=llm1)
        await twin1.observe("She's always optimistic")
        assert twin1.instruction_count == 1

        # Session 2 (days later)
        llm2 = _mock_llm(_extract_response([
            {"type": "directive", "content": "You prefer tea over coffee.", "topics": ["preferences"], "action": "add"},
        ]))
        twin2 = TwinBuilder(twin_dir, name="Alice", llm=llm2)
        await twin2.observe("She drinks tea, never coffee")
        assert twin2.instruction_count == 2


# ---------------------------------------------------------------------------
# TwinBuilder: summary and inspection
# ---------------------------------------------------------------------------


class TestTwinBuilderInspection:
    def test_list_instructions_empty(self, tmp_path):
        twin = TwinBuilder(tmp_path / "alice", name="Alice")
        assert twin.list_instructions() == []

    @pytest.mark.asyncio
    async def test_summary_no_instructions(self, tmp_path):
        llm = _mock_llm()
        twin = TwinBuilder(tmp_path / "alice", name="Alice", llm=llm)
        text = await twin.summary()
        assert "No behavioral instructions" in text

    @pytest.mark.asyncio
    async def test_get_corpus_returns_corpus(self, tmp_path):
        llm = _mock_llm(_extract_response([
            {"type": "persona", "content": "You are brave.", "topics": ["personality"], "action": "add"},
        ]))
        twin = TwinBuilder(tmp_path / "alice", name="Alice", llm=llm)
        await twin.observe("She's brave")
        corpus = twin.get_corpus()
        assert len(corpus) == 1


# ---------------------------------------------------------------------------
# Text chunking
# ---------------------------------------------------------------------------


class TestChunkText:
    def test_short_text_single_chunk(self):
        chunks = _chunk_text("Hello world, this is a short text.")
        assert len(chunks) == 1

    def test_long_text_splits(self):
        # Create text with multiple paragraphs exceeding chunk size
        paras = [f"Paragraph {i}. " + "x" * 200 for i in range(10)]
        text = "\n\n".join(paras)
        chunks = _chunk_text(text, size=300, overlap=50)
        assert len(chunks) > 1

    def test_empty_text(self):
        chunks = _chunk_text("")
        assert chunks == []

    def test_line_based_fallback(self):
        text = "line one\nline two\nline three"
        chunks = _chunk_text(text)
        assert len(chunks) >= 1
        assert "line one" in chunks[0]


# ---------------------------------------------------------------------------
# TwinBuilder: knowledge
# ---------------------------------------------------------------------------


class TestTwinBuilderKnowledge:
    def test_add_knowledge_basic(self, tmp_path):
        twin = TwinBuilder(tmp_path / "alice", name="Alice")
        text = "Alice specializes in pediatric cardiology. She has published 50 papers on congenital heart defects."
        added = twin.add_knowledge(text, source="bio")
        assert len(added) >= 1
        assert twin.knowledge_count >= 1
        # Knowledge instructions should be tagged
        for inst in added:
            assert "knowledge" in inst.tags
            assert "bio" in inst.tags

    def test_add_knowledge_chunks_long_text(self, tmp_path):
        twin = TwinBuilder(tmp_path / "alice", name="Alice")
        # Create long text that should produce multiple chunks
        text = "\n\n".join([f"Knowledge paragraph {i}. " + "x" * 400 for i in range(10)])
        added = twin.add_knowledge(text, source="document")
        assert len(added) > 1

    def test_add_knowledge_persists(self, tmp_path):
        twin_dir = tmp_path / "alice"
        twin = TwinBuilder(twin_dir, name="Alice")
        twin.add_knowledge("Alice knows about heart surgery.", source="manual")

        # Reload
        twin2 = TwinBuilder(twin_dir, name="Alice")
        knowledge_insts = twin2.list_knowledge()
        assert len(knowledge_insts) >= 1
        # Knowledge should NOT appear in behavioral list
        assert len(twin2.list_instructions()) == 0

    def test_add_knowledge_file_txt(self, tmp_path):
        twin = TwinBuilder(tmp_path / "alice", name="Alice")
        txt_file = tmp_path / "notes.txt"
        txt_file.write_text("Alice is an expert in machine learning and neural networks.")
        added = twin.add_knowledge_file(txt_file)
        assert len(added) >= 1

    def test_add_knowledge_skips_short_chunks(self, tmp_path):
        twin = TwinBuilder(tmp_path / "alice", name="Alice")
        added = twin.add_knowledge("short", source="test")
        assert len(added) == 0  # too short to be useful

    @pytest.mark.asyncio
    async def test_add_knowledge_llm(self, tmp_path):
        llm = _mock_llm(json.dumps([
            {"content": "Alice has expertise in cardiac imaging.", "topics": ["cardiology", "imaging"]},
            {"content": "She pioneered a new valve repair technique.", "topics": ["surgery", "innovation"]},
        ]))
        twin = TwinBuilder(tmp_path / "alice", name="Alice", llm=llm)
        added = await twin.add_knowledge_llm("Alice is a cardiac surgeon who...", source="cv")
        assert len(added) == 2
        assert "cardiac imaging" in added[0].content

    @pytest.mark.asyncio
    async def test_add_knowledge_llm_fallback_on_bad_json(self, tmp_path):
        llm = _mock_llm("not valid json")
        twin = TwinBuilder(tmp_path / "alice", name="Alice", llm=llm)
        text = "Alice knows about many things in the field of medicine and surgery."
        added = await twin.add_knowledge_llm(text, source="test")
        # Should fall back to chunking
        assert len(added) >= 1

    def test_knowledge_count_property(self, tmp_path):
        twin = TwinBuilder(tmp_path / "alice", name="Alice")
        assert twin.knowledge_count == 0
        twin.add_knowledge("Alice knows about pediatric cardiology in depth.", source="bio")
        assert twin.knowledge_count > 0

    def test_knowledge_separate_from_behavioral(self, tmp_path):
        """Knowledge corpus is separate — doesn't dilute behavioral retrieval."""
        llm = _mock_llm(_extract_response([
            {"type": "persona", "content": "You are warm and empathetic.", "topics": ["personality"], "action": "add"},
        ]))
        twin = TwinBuilder(tmp_path / "alice", name="Alice", llm=llm)

        # Add behavioral instruction
        import asyncio
        asyncio.get_event_loop().run_until_complete(
            twin.observe("Alice is warm and empathetic")
        )
        # Add knowledge
        twin.add_knowledge("The heart has four chambers. " * 20, source="textbook")

        # Behavioral corpus should only have behavioral
        assert twin.instruction_count == 1
        assert all("knowledge" not in i.tags for i in twin.list_instructions())

        # Knowledge corpus should only have knowledge
        assert twin.knowledge_count > 0
        assert all("knowledge" in i.tags for i in twin.list_knowledge())

        # Separate retrievers
        behavioral_retriever = twin.get_retriever()
        knowledge_retriever = twin.get_knowledge_retriever()
        assert behavioral_retriever is not knowledge_retriever

    def test_list_all_instructions(self, tmp_path):
        twin = TwinBuilder(tmp_path / "alice", name="Alice")
        twin.add_knowledge("Alice knows about surgery techniques in great detail.", source="bio")
        all_insts = twin.list_all_instructions()
        assert len(all_insts) >= 1
        # list_instructions should be empty (no behavioral)
        assert len(twin.list_instructions()) == 0
        # list_knowledge should have the knowledge
        assert len(twin.list_knowledge()) >= 1


# ---------------------------------------------------------------------------
# TwinBuilder: multimodal ingestion
# ---------------------------------------------------------------------------


class TestTwinBuilderIngestFile:
    @pytest.mark.asyncio
    async def test_ingest_text_file(self, tmp_path):
        llm = _mock_llm(_extract_response([
            {"type": "directive", "content": "You prefer concise writing.", "topics": ["writing"], "action": "add"},
        ]))
        twin = TwinBuilder(tmp_path / "alice", name="Alice", llm=llm)
        txt_file = tmp_path / "notes.txt"
        txt_file.write_text("Alice always writes concisely and avoids filler words.")
        added = await twin.ingest_file(txt_file)
        assert len(added) >= 1

    @pytest.mark.asyncio
    async def test_ingest_json_file(self, tmp_path):
        llm = _mock_llm(_extract_response([
            {"type": "directive", "content": "You value precision.", "topics": ["style"], "action": "add"},
        ]))
        twin = TwinBuilder(tmp_path / "alice", name="Alice", llm=llm)
        json_file = tmp_path / "data.json"
        json_file.write_text(json.dumps({"trait": "precise"}))
        added = await twin.ingest_file(json_file)
        assert len(added) >= 1

    @pytest.mark.asyncio
    async def test_ingest_image_calls_llm(self, tmp_path):
        # Create a minimal PNG file (1x1 pixel)
        import struct
        import zlib
        png_header = b'\x89PNG\r\n\x1a\n'
        ihdr_data = struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0)
        ihdr_crc = zlib.crc32(b'IHDR' + ihdr_data)
        ihdr = struct.pack('>I', 13) + b'IHDR' + ihdr_data + struct.pack('>I', ihdr_crc)
        raw = b'\x00\x00\x00\x00'
        idat_data = zlib.compress(raw)
        idat_crc = zlib.crc32(b'IDAT' + idat_data)
        idat = struct.pack('>I', len(idat_data)) + b'IDAT' + idat_data + struct.pack('>I', idat_crc)
        iend_crc = zlib.crc32(b'IEND')
        iend = struct.pack('>I', 0) + b'IEND' + struct.pack('>I', iend_crc)
        png_bytes = png_header + ihdr + idat + iend

        img_file = tmp_path / "photo.png"
        img_file.write_bytes(png_bytes)

        # First call: image description, second call: observation extraction
        desc_resp = MagicMock()
        desc_resp.content = "A person smiling in a professional setting."
        extract_resp = MagicMock()
        extract_resp.content = _extract_response([
            {"type": "persona", "content": "You have a warm, professional demeanor.", "topics": ["personality"], "action": "add"},
        ])
        llm = MagicMock()
        llm.generate = AsyncMock(side_effect=[desc_resp, extract_resp])

        twin = TwinBuilder(tmp_path / "alice", name="Alice", llm=llm)
        added = await twin.ingest_file(img_file)
        assert len(added) >= 1
        # LLM should have been called twice (describe + extract)
        assert llm.generate.call_count == 2

    @pytest.mark.asyncio
    async def test_ingest_unknown_tries_text(self, tmp_path):
        llm = _mock_llm(_extract_response([
            {"type": "directive", "content": "You like YAML.", "topics": ["preferences"], "action": "add"},
        ]))
        twin = TwinBuilder(tmp_path / "alice", name="Alice", llm=llm)
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text("preference: yaml")
        added = await twin.ingest_file(yaml_file)
        assert len(added) >= 1
