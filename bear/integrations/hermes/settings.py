"""Plugin settings, read from Hermes's ``plugins.entries.bear.settings``."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Mapping

MODES = ("observe", "enforce")
EMBEDDINGS = ("bm25", "default")
NARROW_MODES = ("off", "observe", "on")
NARROW_SCOPES = ("turn", "session")
NARROW_QUERIES = ("message", "first_plus_message")
DISCLOSURES = ("names", "counts", "none")
_NULL_IS_A_VALUE = frozenset({"narrow_top_k"})


def hermes_home() -> Path:
    """Hermes's home directory, as Hermes itself resolves it when importable."""
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


@dataclass
class Settings:
    """Every setting the plugin reads, with its default."""

    corpus_dir: str = ""
    mode: str = "observe"
    unlisted: str = "approve"
    users: dict[str, list[str]] = field(default_factory=dict)
    default_tags: list[str] = field(default_factory=lambda: ["role:guest"])
    unattended_tags: list[str] = field(default_factory=list)
    domain: str = ""
    top_k: int = 10
    threshold: float = 0.3
    max_guidance_chars: int = 6000
    embedding: str = "bm25"
    audit_path: str = ""
    redact_arg_keys: list[str] = field(
        default_factory=lambda: ["*key*", "*token*", "*secret*", "*password*"])
    turn_ttl_s: int = 3600
    must_have_tag: str = "must-have"
    # Tool narrowing.
    narrow: str = "off"
    # None offers every admitted tool, a gate-only cut with no relevance cut.
    narrow_top_k: int | None = 10
    narrow_threshold: float = 0.0
    narrow_scope: str = "turn"
    narrow_query: str = "message"
    always_offer: list[str] = field(default_factory=lambda: ["clarify"])
    # How much an allow rule's priority counts against similarity when tools
    # are ranked. None keeps the retriever's own weight.
    narrow_priority_weight: float | None = None
    # Gate with reason: tell the model why each tool was offered or withheld.
    annotate_tools: bool = False
    withheld_disclosure: str = "counts"
    # Under "names", list at most this many withheld tools and count the rest
    # by reason. None lists them all.
    withheld_names_max: int | None = None
    # Under Hermes's tool search the request holds only the direct tools, so
    # observe mode ranks Hermes's registry instead, built from these toolsets
    # (None for all of them). The middleware is not told the session's own.
    registry_toolsets: list[str] | None = None
    registry_disabled_toolsets: list[str] = field(default_factory=list)
    embedding_revision: str = ""

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Settings:
        """Build from a plain mapping, checking every value. Unknown keys raise."""
        names = {f.name for f in fields(cls)}
        unknown = sorted(set(data) - names)
        if unknown:
            raise ValueError(f"unknown bear plugin settings: {unknown}")
        # A null value means "use the default", except where null is itself
        # a setting (narrow_top_k: null offers every admitted tool).
        s = cls(**{k: v for k, v in data.items() if v is not None or k in _NULL_IS_A_VALUE})
        s.validate()
        return s

    @classmethod
    def from_ctx(cls, ctx: Any) -> Settings:
        """Read each setting through the plugin context, defaults for the rest."""
        defaults = cls()
        data = {f.name: ctx.get_config(f.name, getattr(defaults, f.name))
                for f in fields(cls)}
        return cls.from_mapping(data)

    def validate(self) -> None:
        if not self.corpus_dir:
            raise ValueError("corpus_dir is required")
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, not {self.mode!r}")
        if self.unlisted not in ("allow", "approve", "block"):
            raise ValueError(f"unlisted must be allow, approve or block, not {self.unlisted!r}")
        if self.embedding not in EMBEDDINGS and not self.remote_embedding_url:
            raise ValueError(f"embedding must be one of {EMBEDDINGS} or remote:<url>, "
                             f"not {self.embedding!r}")
        if not isinstance(self.users, Mapping):
            raise ValueError("users must map a sender id to a list of gate tags")
        self.users = {str(k): [str(t) for t in v] for k, v in self.users.items()}
        self.default_tags = [str(t) for t in self.default_tags]
        self.unattended_tags = [str(t) for t in self.unattended_tags]
        self.redact_arg_keys = [str(p) for p in self.redact_arg_keys]
        self.top_k = int(self.top_k)
        self.threshold = float(self.threshold)
        self.max_guidance_chars = int(self.max_guidance_chars)
        self.turn_ttl_s = int(self.turn_ttl_s)
        if self.narrow not in NARROW_MODES:
            raise ValueError(f"narrow must be one of {NARROW_MODES}, not {self.narrow!r}")
        if self.narrow_scope not in NARROW_SCOPES:
            raise ValueError(f"narrow_scope must be one of {NARROW_SCOPES}, "
                             f"not {self.narrow_scope!r}")
        if self.narrow_query not in NARROW_QUERIES:
            raise ValueError(f"narrow_query must be one of {NARROW_QUERIES}, "
                             f"not {self.narrow_query!r}")
        if isinstance(self.narrow_top_k, str) and self.narrow_top_k.strip().lower() == "all":
            self.narrow_top_k = None
        if self.narrow_top_k is not None:
            self.narrow_top_k = int(self.narrow_top_k)
            if self.narrow_top_k < 0:
                raise ValueError("narrow_top_k must be 0 or more, or null for no cut")
        if self.registry_toolsets is not None:
            self.registry_toolsets = [str(t) for t in self.registry_toolsets]
        self.registry_disabled_toolsets = [str(t) for t in self.registry_disabled_toolsets]
        self.narrow_threshold = float(self.narrow_threshold)
        if self.narrow_priority_weight is not None:
            self.narrow_priority_weight = float(self.narrow_priority_weight)
            if not 0.0 <= self.narrow_priority_weight <= 1.0:
                raise ValueError("narrow_priority_weight must be between 0 and 1")
        self.always_offer = [str(t) for t in self.always_offer]
        if not isinstance(self.annotate_tools, bool):
            raise ValueError("annotate_tools must be true or false")
        if self.withheld_disclosure not in DISCLOSURES:
            raise ValueError(f"withheld_disclosure must be one of {DISCLOSURES}, "
                             f"not {self.withheld_disclosure!r}")
        if self.withheld_names_max is not None:
            self.withheld_names_max = int(self.withheld_names_max)
            if self.withheld_names_max < 0:
                raise ValueError("withheld_names_max must be 0 or more, or null for no cap")
        self.embedding_revision = str(self.embedding_revision)

    @property
    def remote_embedding_url(self) -> str:
        """The URL in ``remote:<url>``, or empty for the other settings."""
        if isinstance(self.embedding, str) and self.embedding.startswith("remote:"):
            return self.embedding[len("remote:"):].strip()
        return ""

    @property
    def corpus_path(self) -> Path:
        return Path(os.path.expandvars(self.corpus_dir)).expanduser()

    @property
    def audit_file(self) -> Path:
        if self.audit_path:
            return Path(os.path.expandvars(self.audit_path)).expanduser()
        return hermes_home() / "bear" / "audit.jsonl"
