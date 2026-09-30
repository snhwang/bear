"""bear.culture.adopt: an instruction written through a learner's lens, behaviour conserved."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from bear.culture import adopt
from bear.markers import parse_actions
from bear.models import Instruction, InstructionType, ScopeCondition

SOURCE = Instruction(
    id="shelter-from-rain", type=InstructionType.DIRECTIVE, priority=58,
    content="It is raining and there is no sense getting soaked. Step inside the inn "
            "until it passes. [!go(to=inn)] [!rest(ticks=4)]",
    scope=ScopeCondition(required_tags=["daytime", "weather_rain"], tags=["temp_easygoing"]),
    metadata={"practice": "shelter-from-rain"},
)


@dataclass
class Reply:
    content: str


class ScriptedLLM:
    """Returns a scripted rewrite of whatever pinned text it is given."""

    def __init__(self, rewrite):
        self.rewrite = rewrite
        self.calls = []

    async def generate(self, system="", user="", **kwargs):
        self.calls.append({"system": system, "user": user, **kwargs})
        return Reply(self.rewrite(user))


def run(coro):
    return asyncio.run(coro)


def markers(text):
    return [(m.name, m.args_raw) for m in parse_actions(text)]


def test_the_wording_is_the_learners_and_the_behaviour_is_the_teachers():
    def rewrite(pinned):
        anchors = [part for part in pinned.split() if part.startswith("<<A")]
        return "When the sky opens, you get under a roof and wait. " + " ".join(anchors)

    llm = ScriptedLLM(rewrite)
    result = run(adopt(SOURCE, llm, learner="Nell", lens="You keep anything that bears on safety.",
                       how="watched Ada do", new_id="nell-shelter-from-rain", thinking=False))
    assert result.rewritten
    assert result.instruction.id == "nell-shelter-from-rain"
    assert result.instruction.content.startswith("When the sky opens")
    assert markers(result.instruction.content) == markers(SOURCE.content)
    assert result.instruction.scope == SOURCE.scope
    assert result.instruction.priority == SOURCE.priority
    meta = result.instruction.metadata
    assert (meta["practice"], meta["adopted_from"], meta["adopted_by"], meta["adopted_how"]) == \
        ("shelter-from-rain", "shelter-from-rain", "Nell", "watched Ada do")
    call = llm.calls[0]
    assert "You keep anything that bears on safety." in call["system"]
    assert "[!go" not in call["user"]            # the model never sees the markers
    assert call["thinking"] is False             # passed through to the model


def test_a_dropped_marker_is_put_back():
    llm = ScriptedLLM(lambda pinned: "You wait out the rain indoors.")
    result = run(adopt(SOURCE, llm, learner="Bryn"))
    assert markers(result.instruction.content) == markers(SOURCE.content)
    assert result.repair.reinserted


def test_an_invented_marker_is_removed():
    def rewrite(pinned):
        return pinned.split(".")[0] + ". Dance in the puddles. [!play(target=nearby)] " + \
            " ".join(p for p in pinned.split() if p.startswith("<<A"))

    result = run(adopt(SOURCE, ScriptedLLM(rewrite), learner="Bryn"))
    assert "play" not in [name for name, _ in markers(result.instruction.content)]
    assert result.repair.rejected


def test_without_a_model_it_is_adopted_word_for_word():
    result = run(adopt(SOURCE, None, learner="Nell", how="been taught"))
    assert not result.rewritten and result.repair is None
    assert result.instruction.content == SOURCE.content
    assert result.instruction.id == "nell-shelter-from-rain"
    assert result.instruction.metadata["adopted_how"] == "been taught"
