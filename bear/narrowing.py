"""Tool narrowing: which of a host's tools to offer a model, and why.

Given the tools a host would send (names and descriptions), a
:class:`~bear.models.Context` and the policy corpus, this chooses the tools the
context may use, ranked by relevance to the query. The rules are the gate's
(:mod:`bear.gate`). A tool is withheld exactly when the gate would block every
call to it, by scope or by a must-have. Ranking is BEAR retrieval over the
host's own tool descriptions, so the corpus contributes only scope, priority
and tags, never the text the ranking reads.

Selection runs in two stages. :func:`rank_tools` retrieves. :func:`cut` applies
the policy to that ranking and is pure, so a recorded ranking can be checked
again even where the embedder that produced it cannot be rerun.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from bear.gate import MUST_HAVE_TAG, ToolCall, evaluate
from bear.models import Context, Instruction, InstructionType
from bear.scope import passes_gate

# How a tool came to be offered.
PATHS = ("mandatory", "always-on", "bridge", "gate", "soft", "unlisted")
# Why a tool was withheld.
WITHHELD_REASONS = ("out_of_scope", "forbidden", "unlisted")
# Id prefix of the instruction standing in for a tool no rule names.
UNLISTED_PREFIX = "unlisted:"
# Retrieve below any similarity a backend gives (cosines >= -1, BM25 >= 0), so
# the retriever's threshold removes nothing and cut() applies its own.
_NO_THRESHOLD = -1.0


@dataclass(frozen=True)
class ToolSpec:
    """A tool as the host describes it."""

    name: str
    description: str = ""


def read_tools(tools: Any) -> list[ToolSpec] | None:
    """Names and descriptions from a provider request's ``tools`` list.

    Reads chat-completions entries (``{"type": "function", "function":
    {...}}``), Responses entries (``{"type": "function", "name": ...}``) and
    Anthropic entries (``{"name": ..., "input_schema": ...}``). Returns None if
    any entry has no readable name, so an unknown shape is never half read.
    """
    if not isinstance(tools, list):
        return None
    specs: list[ToolSpec] = []
    for t in tools:
        if not isinstance(t, Mapping):
            return None
        fn = t["function"] if isinstance(t.get("function"), Mapping) else t
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            return None
        desc = fn.get("description")
        specs.append(ToolSpec(name, desc if isinstance(desc, str) else ""))
    return specs


def catalog_digest(catalog: Sequence[ToolSpec]) -> str:
    """A hash of the tools' names and descriptions, in order."""
    blob = json.dumps([[t.name, t.description] for t in catalog], ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _rules_for(policy: Iterable[Instruction], name: str) -> list[Instruction]:
    return sorted((i for i in policy if i.type == InstructionType.TOOL
                   and (i.actions or {}).get("function") == name),
                  key=lambda i: (-i.priority, i.id))


def tool_corpus(catalog: Sequence[ToolSpec], policy: Iterable[Instruction], *,
                unlisted: str = "approve", bridge: Iterable[str] = ()) -> list[Instruction]:
    """The instructions the ranking retrieves over, one per allow rule per tool.

    Each one's content is the tool's name and the host's description. Its
    scope, priority and tags are the allow rule's. A tool no rule names gets
    one unscoped instruction, unless ``unlisted`` is ``block``. Bridge tools
    are left out, since they are always offered.
    """
    policy = list(policy)
    skip = set(bridge)
    out: list[Instruction] = []
    seen: set[str] = set()
    for spec in catalog:
        if spec.name in skip or spec.name in seen:
            continue
        seen.add(spec.name)
        text = f"{spec.name}: {spec.description}" if spec.description else spec.name
        rules = _rules_for(policy, spec.name)
        if not rules:
            if unlisted != "block":
                out.append(Instruction(id=UNLISTED_PREFIX + spec.name,
                                       type=InstructionType.TOOL, content=text,
                                       actions={"function": spec.name}))
            continue
        for rule in rules:
            out.append(Instruction(id=rule.id, type=InstructionType.TOOL,
                                   priority=rule.priority, content=text,
                                   scope=rule.scope.model_copy(deep=True),
                                   tags=list(rule.tags), actions={"function": spec.name}))
    return out


@dataclass(frozen=True)
class Withheld:
    """A tool the context may not use, and why."""

    name: str
    reason: str
    rule_ids: tuple[str, ...] = ()
    missing_tags: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"name": self.name, "reason": self.reason, "rule_ids": list(self.rule_ids),
                "missing_tags": list(self.missing_tags)}


def withheld_tools(names: Iterable[str], context: Context | None,
                   policy: Iterable[Instruction], *, unlisted: str = "approve",
                   must_have_tag: str = MUST_HAVE_TAG,
                   bridge: Iterable[str] = ()) -> list[Withheld]:
    """The tools the gate would block on every call, in the order given.

    Each tool is decided as a call with no arguments. A must-have that forbids
    only some arguments therefore does not withhold the tool, and neither does
    an allow rule whose default is block but whose argument rules allow some
    calls. ``missing_tags`` are the required tags of the tool's
    highest-priority rule that the context lacks.
    """
    policy = list(policy)
    skip = set(bridge)
    have = set(context.all_tags) if context is not None else set()
    out: list[Withheld] = []
    seen: set[str] = set()
    for name in names:
        if name in skip or name in seen:
            continue
        seen.add(name)
        d = evaluate(ToolCall(name, {}), context, policy, unlisted=unlisted,
                     must_have_tag=must_have_tag)
        if d.reason == "must_have":
            out.append(Withheld(name, "forbidden", d.rule_ids))
        elif d.reason == "out_of_scope":
            top = _rules_for(policy, name)[0]
            missing = tuple(t for t in top.scope.required_tags if t not in have)
            out.append(Withheld(name, "out_of_scope", d.rule_ids, missing))
        elif d.reason == "unlisted" and d.action == "block":
            out.append(Withheld(name, "unlisted"))
    return out


@dataclass(frozen=True)
class Ranked:
    """One tool's place in the ranking, from its best instruction."""

    name: str
    rule_id: str = ""
    similarity: float = 0.0
    final_score: float = 0.0
    mandatory: bool = False
    gated: bool = False
    required_tags: tuple[str, ...] = ()
    # False for a tool that passed the gate but that retrieval did not return.
    # It is ranked last, after every retrieved tool.
    retrieved: bool = True


def rank_tools(retriever: Any, query: str, context: Context | None,
               names: Sequence[str] = ()) -> list[Ranked]:
    """Every tool the retriever admits for the context, best first.

    ``retriever`` is a :class:`~bear.retriever.Retriever` over
    :func:`tool_corpus`. It retrieves with ``top_k=ALL`` and no threshold, so
    the whole tool corpus is scored and the order is one fixed ranking. A
    larger cut is then always a prefix of it. A tool named by several rules
    takes the place of its best one. Any tool in ``names`` that passes the gate
    but was not returned is appended last, marked ``retrieved=False``. With no
    retriever (an empty tool corpus) the ranking is empty.
    """
    if retriever is None:
        return []
    scored = retriever.retrieve(query, context, top_k=retriever.ALL,
                                threshold=_NO_THRESHOLD)
    out: list[Ranked] = []
    seen: set[str] = set()
    for s in scored:
        name = (s.instruction.actions or {}).get("function")
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(_ranked(s.instruction, name, similarity=float(s.similarity),
                           final_score=float(s.final_score), mandatory=bool(s.mandatory)))
    if names:
        by_tool: dict[str, list[Instruction]] = {}
        for inst in retriever._instruction_list:
            by_tool.setdefault((inst.actions or {}).get("function"), []).append(inst)
        for name in names:
            if name in seen:
                continue
            passing = sorted((i for i in by_tool.get(name, []) if passes_gate(i, context)),
                             key=lambda i: (-i.priority, i.id))
            if passing:
                seen.add(name)
                out.append(_ranked(passing[0], name, mandatory=retriever._is_mandatory(passing[0]),
                                   retrieved=False))
    return out


def _ranked(inst: Instruction, name: str, **kw: Any) -> Ranked:
    required = tuple(inst.scope.required_tags)
    rule_id = "" if inst.id.startswith(UNLISTED_PREFIX) else inst.id
    return Ranked(name, rule_id, gated=bool(required), required_tags=required, **kw)


@dataclass(frozen=True)
class Placed:
    """Where one ranked or bridge tool ended up."""

    name: str
    path: str                    # one of PATHS, or "ranked_out"
    rank: int | None             # 1-based place in the ranking, None for a bridge
    rule_id: str = ""
    similarity: float | None = None
    final_score: float | None = None
    mandatory: bool = False
    gated: bool = False
    required_tags: tuple[str, ...] = ()
    retrieved: bool = True
    below_threshold: bool = False

    def to_dict(self) -> dict:
        return {"name": self.name, "path": self.path, "rank": self.rank,
                "rule_id": self.rule_id, "similarity": self.similarity,
                "final_score": self.final_score, "mandatory": self.mandatory,
                "gated": self.gated, "required_tags": list(self.required_tags),
                "retrieved": self.retrieved, "below_threshold": self.below_threshold}


@dataclass
class Selection:
    """What a context is offered, what it is not, and why."""

    offered: list[Placed]        # in the host's order
    withheld: list[Withheld]     # in the host's order
    ranked_out: list[Placed]     # in rank order
    admissible: int              # tools eligible for the ranked cut
    k: int | None                # None offers every eligible tool
    threshold: float
    always_offer: tuple[str, ...] = ()
    bridge: tuple[str, ...] = field(default_factory=tuple)

    @property
    def bound(self) -> bool:
        """Whether the cut left out an eligible tool."""
        return self.k is not None and self.admissible > self.k

    @property
    def offered_names(self) -> list[str]:
        return [p.name for p in self.offered]

    def to_dict(self) -> dict:
        return {"offered": [p.to_dict() for p in self.offered],
                "withheld": [w.to_dict() for w in self.withheld],
                "ranked_out": [p.to_dict() for p in self.ranked_out],
                "admissible": self.admissible, "bound": self.bound,
                "k": self.k, "threshold": self.threshold}


def cut(names: Sequence[str], ranking: Sequence[Ranked], withheld: Sequence[Withheld], *,
        k: int | None, threshold: float = 0.0, always_offer: Iterable[str] = (),
        bridge: Iterable[str] = ()) -> Selection:
    """Apply the policy to a ranking. Pure: the same inputs give the same selection.

    Offered, in this order of precedence: mandatory tools, then tools in
    ``always_offer``, then the best-ranked others whose similarity reaches
    ``threshold``, up to ``k``. With ``k=None`` there is no relevance cut,
    and every tool the gate admits is offered. Withheld tools are never offered. Bridge tools
    present in ``names`` are always offered. The offered list keeps the order
    of ``names``, so it is a subsequence of what the host would have sent.
    """
    always = set(always_offer)
    bridge_set = set(bridge)
    blocked = {w.name for w in withheld}
    placed: dict[str, Placed] = {}
    ranked_out: list[Placed] = []
    admissible = 0
    taken = 0
    ranked = [r for r in ranking if r.name not in blocked and r.name not in bridge_set]
    for rank, r in enumerate(ranked, 1):
        if r.mandatory:
            path = "mandatory"
        elif r.name in always:
            path = "always-on"
        elif r.similarity < threshold:
            ranked_out.append(_place(r, "ranked_out", rank, below_threshold=True))
            continue
        else:
            admissible += 1
            if k is not None and taken >= k:
                ranked_out.append(_place(r, "ranked_out", rank))
                continue
            taken += 1
            path = "gate" if r.gated else ("soft" if r.rule_id else "unlisted")
        placed[r.name] = _place(r, path, rank)
    for name in names:
        if name in bridge_set and name not in placed:
            placed[name] = Placed(name, "bridge", None)
    offered: list[Placed] = []
    seen: set[str] = set()
    for name in names:
        if name in placed and name not in seen:
            seen.add(name)
            offered.append(placed[name])
    order = {n: i for i, n in enumerate(names)}
    kept_withheld = sorted(withheld, key=lambda w: order.get(w.name, len(order)))
    return Selection(offered=offered, withheld=kept_withheld, ranked_out=ranked_out,
                     admissible=admissible, k=k, threshold=threshold,
                     always_offer=tuple(sorted(always)), bridge=tuple(sorted(bridge_set)))


def _place(r: Ranked, path: str, rank: int, *, below_threshold: bool = False) -> Placed:
    return Placed(r.name, path, rank, r.rule_id, r.similarity, r.final_score, r.mandatory,
                  r.gated, r.required_tags, r.retrieved, below_threshold)


def ranking_from_record(offered: Iterable[Mapping], ranked_out: Iterable[Mapping]) -> list[Ranked]:
    """Rebuild the ranking :func:`cut` read, from a recorded selection.

    Every ranked tool is in one of the two lists with its rank, so sorting by
    rank restores the order. Bridge tools have no rank and are left out.
    """
    rows = [d for d in [*offered, *ranked_out] if d.get("rank") is not None]
    rows.sort(key=lambda d: d["rank"])
    return [Ranked(d["name"], d.get("rule_id", ""), float(d.get("similarity") or 0.0),
                   float(d.get("final_score") or 0.0), bool(d.get("mandatory")),
                   bool(d.get("gated")), tuple(d.get("required_tags") or ()),
                   bool(d.get("retrieved", True))) for d in rows]


def select(catalog: Sequence[ToolSpec], context: Context | None, query: str,
           policy: Iterable[Instruction], retriever: Any, *, k: int | None,
           threshold: float = 0.0, always_offer: Iterable[str] = (),
           bridge: Iterable[str] = (), unlisted: str = "approve",
           must_have_tag: str = MUST_HAVE_TAG) -> Selection:
    """Both stages: the gate's withheld tools, the ranking, then the cut."""
    policy = list(policy)
    names = [t.name for t in catalog]
    withheld = withheld_tools(names, context, policy, unlisted=unlisted,
                              must_have_tag=must_have_tag, bridge=bridge)
    ranking = rank_tools(retriever, query, context, names)
    return cut(names, ranking, withheld, k=k, threshold=threshold,
               always_offer=always_offer, bridge=bridge)


DISCLOSURES = ("names", "counts", "none")
_COUNT_PHRASE = {
    "out_of_scope": ("tool requires", "tools require", "a different role or channel"),
    "forbidden": ("tool is", "tools are", "forbidden by policy"),
    "unlisted": ("tool is", "tools are", "not covered by policy"),
}


def annotate(selection: Selection, disclosure: str = "counts",
             names_max: int | None = None) -> str:
    """The text that tells a model why each tool was offered and the rest were not.

    Built from fixed phrases and sorted by tool name, so the same selection
    always gives the same text. ``disclosure`` sets how much the withheld part
    says: ``names`` lists each withheld tool with its reason, ``counts`` gives
    a count per reason, and ``none`` leaves the part out. Under ``names``,
    ``names_max`` lists at most that many, the first by name, and counts the
    rest by reason, since a long list costs tokens on every request. Naming a
    withheld tool tells the reader that it exists. Tools ranked out are not
    mentioned, since they are allowed and simply not offered this time.
    """
    if disclosure not in DISCLOSURES:
        raise ValueError(f"disclosure must be one of {DISCLOSURES}, not {disclosure!r}")
    lines = ["=== TOOL ACCESS ===", "Offered in this context:"]
    for p in sorted(selection.offered, key=lambda p: p.name):
        lines.append(f"- {p.name}: {_offered_phrase(p)}")
    if selection.withheld and disclosure != "none":
        lines.append("Not available in this context:")
        withheld = sorted(selection.withheld, key=lambda w: w.name)
        counted = withheld
        if disclosure == "names":
            shown = withheld if names_max is None else withheld[:names_max]
            for w in shown:
                lines.append(f"- {w.name}: {_withheld_phrase(w)}")
            counted = withheld[len(shown):]
        more = "more " if len(counted) < len(withheld) else ""
        for reason in WITHHELD_REASONS:
            n = sum(1 for w in counted if w.reason == reason)
            if n:
                one, many, what = _COUNT_PHRASE[reason]
                lines.append(f"- {n} {more}{one if n == 1 else many} {what}.")
    lines.append("=== END TOOL ACCESS ===")
    return "\n".join(lines)


def _offered_phrase(p: Placed) -> str:
    if p.path == "mandatory":
        return f"required by policy (rule {p.rule_id})." if p.rule_id else "required by policy."
    if p.path == "always-on":
        return "always offered."
    if p.path == "bridge":
        return "always offered, to find other tools."
    if p.path == "gate":
        return f"allowed for {' and '.join(p.required_tags)} (rule {p.rule_id})."
    return "relevant to this request."


def _withheld_phrase(w: Withheld) -> str:
    if w.reason == "out_of_scope":
        if w.missing_tags:
            return f"requires {' and '.join(w.missing_tags)}."
        return "not permitted in this context."
    if w.reason == "forbidden":
        return f"forbidden by policy ({', '.join(w.rule_ids)})."
    return "not covered by policy."


def text_tokens(text: str) -> int:
    """Estimated tokens of a text, at four characters a token."""
    return -(-len(text) // 4)


def schema_tokens(tools: Any) -> int:
    """Estimated tokens of a tools list, at four characters a token."""
    return -(-len(json.dumps(tools, ensure_ascii=False, separators=(",", ":"))) // 4)
