"""BEAR Parlor's access gate combines several policies for one role restrictively."""
import sys
from pathlib import Path

import pytest

PARLOR = Path(__file__).resolve().parents[1] / "examples" / "bear_parlor"
sys.path.insert(0, str(PARLOR))
knowledge_rag = pytest.importorskip("knowledge_rag")

from bear import Corpus  # noqa: E402
from bear.evolution import BreedingConfig, breed  # noqa: E402

KD = knowledge_rag.CrossHatDiffuser
INCIDENT = PARLOR / "instructions" / "incident"
ROLES = ["commander", "comms", "legal", "oncall-engineer", "security-lead", "support"]
CLASSES = ["public", "timeline", "exploit-detail", "internal-identifier",
           "privileged", "exposure", "remediation", "customer-impact"]


def _corpus(role: str) -> Corpus:
    return Corpus.from_file(INCIDENT / f"{role.replace('-', '_')}.yaml")


def _policies(corpus: Corpus) -> list:
    return [i for i in corpus if "access" in i.tags]


def _gate(access: dict):
    g = KD.__new__(KD)
    g._gate, g._access, g._warned_no_policy = True, access, set()
    return g


def _tags(inst, prefix):
    return {t.split(":", 1)[1] for t in inst.tags if t.startswith(prefix)}


@pytest.mark.parametrize("role", ROLES)
def test_single_policy_reads_as_before(role):
    """Each published role has one policy, and the gate reads it unchanged."""
    (inst,) = _policies(_corpus(role))
    pol = KD.combine_access_policies([inst], [role], KD._belongs_to)[role]
    assert pol["allow"] == (_tags(inst, "allow:") or None)
    assert pol["deny"] == _tags(inst, "deny:")
    # the previous union rule, applied to one instruction
    old_allow, old_deny = _tags(inst, "allow:"), _tags(inst, "deny:")
    g = _gate({role: pol})
    for c in CLASSES:
        old = not ({c} & old_deny) and (not old_allow or {c} <= old_allow)
        assert g.permitted(role, c) == old


def test_bred_child_gets_the_stricter_combination():
    child = breed(_corpus("comms"), _corpus("legal"), "liaison", "comms", "legal",
                  config=BreedingConfig(crossover_rate=0.0, seed=1)).child
    pols = _policies(child)
    assert len(pols) == 2
    pol = KD.combine_access_policies(pols, ["liaison"], KD._belongs_to)["liaison"]
    assert pol["allow"] == {"public", "timeline", "customer-impact"}
    g = _gate({"liaison": pol})
    allowed = {c for c in CLASSES if g.permitted("liaison", c)}
    # only what both parents may hold
    comms = {c for c in CLASSES if _gate(KD.combine_access_policies(
        _policies(_corpus("comms")), ["comms"], KD._belongs_to)).permitted("comms", c)}
    legal = {c for c in CLASSES if _gate(KD.combine_access_policies(
        _policies(_corpus("legal")), ["legal"], KD._belongs_to)).permitted("legal", c)}
    assert allowed == comms & legal


def test_disjoint_allow_lists_refuse_everything_classified():
    from bear import Instruction, InstructionType, ScopeCondition

    def pol(i, allow):
        return Instruction(id=f"p{i}", type=InstructionType.CONSTRAINT, content="x",
                           scope=ScopeCondition(required_tags=["x"]),
                           tags=["x", "access", *[f"allow:{a}" for a in allow]])
    access = KD.combine_access_policies([pol(1, ["public"]), pol(2, ["timeline"])],
                                        ["x"], KD._belongs_to)
    g = _gate(access)
    assert not any(g.permitted("x", c) for c in CLASSES + ["unlisted-class"])
    assert g.permitted("x", "")  # unclassified items pass, as always
    assert g.access_policy()["x"] == {"allow": [], "deny": [], "allow_none": True}


def test_deny_only_policy_does_not_widen_an_allow_list():
    from bear import Instruction, InstructionType, ScopeCondition
    allow = Instruction(id="a", type=InstructionType.CONSTRAINT, content="x",
                        scope=ScopeCondition(required_tags=["x"]),
                        tags=["x", "access", "allow:public", "allow:timeline"])
    deny = Instruction(id="d", type=InstructionType.CONSTRAINT, content="x",
                       scope=ScopeCondition(required_tags=["x"]),
                       tags=["x", "access", "deny:timeline"])
    g = _gate(KD.combine_access_policies([allow, deny], ["x"], KD._belongs_to))
    assert [c for c in CLASSES if g.permitted("x", c)] == ["public"]
