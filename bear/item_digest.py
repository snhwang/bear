"""An incremental digest over a set of items, with SHA-256's strength.

``Retriever.index_version`` is a SHA-256 over the canonical dump of every
instruction. It detects any change to any field, which is the guarantee the
audit needs, but it costs O(corpus) to compute. Hashing 87,000 items on every
add or reinforcement would trade the audit's log-size problem for a CPU problem
of the same size.

A sum of per-item digests would be O(1) and is not good enough. Its collision
resistance rests on a knapsack assumption about the modulus rather than on the
hash, and subset-sum and generalised-birthday techniques weaken it well below
what the bit width suggests (Bellare and Micciancio on incremental hashing).
That would detect accidental divergence and nothing stronger, so an audit built
on it could not stand as evidence that a recorded set was not altered.

So: a Merkle tree over buckets. Each item has a SHA-256 of its canonical dump,
including metadata. Items fall into a fixed number of buckets by a hash of
their id, and a bucket's digest covers its sorted ``(id, digest)`` pairs. A
Merkle tree over the buckets gives a root, and the version is a SHA-256 of the
root, the item count and the bucket count. Every hash in that chain is
SHA-256, so the structure inherits its collision resistance.

A mutation recomputes one bucket and walks up the tree: O(n/B + log B), about
85 hashes plus ten at 87,000 items over 1,024 buckets.

**The bucket count is part of the version.** An open-ended run grows, and at
ten times the items the same bucket count makes a mutation ten times dearer,
so the count doubles when the buckets grow past a threshold. Resharding
reassigns every item, which would make every version computed before it
unreproducible if the count were implicit. It is in the version instead, and
recorded beside it, so a replay recomputes with the count the writer used and
a mismatch shows as a mismatch rather than silently.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from bear.models import Instruction, InstructionType

DEFAULT_BUCKETS = 1024
"""Buckets a digest starts with, suiting a few hundred thousand items before
the first reshard."""

DEFAULT_RESHARD_AT = 256
"""Items per bucket above which the bucket count doubles."""


def canonical_json(value) -> str:
    """The one serialisation every digest and version here is taken over.

    Sorted keys, no incidental whitespace, and no escaping of non-ASCII, so
    two processes agree on the bytes. Shared with
    ``bear.retriever._index_version`` deliberately: two canonicalisations
    would be two chances for one object to hash differently.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def item_digest(instruction: Instruction) -> str:
    """A SHA-256 over one item's canonical dump, metadata included.

    Metadata included because reinforcement changes it, and changes ranking
    with it. A digest over the id alone would let a replay rebuild a set that
    hashed equal and ranked differently, and report a match on a retrieval it
    had not reproduced. That is the one silent failure in this area.
    """
    return hashlib.sha256(
        canonical_json(instruction.model_dump(mode="json")).encode("utf-8")
    ).hexdigest()


def _bucket_of(item_id: str, buckets: int) -> int:
    """Which bucket an id falls in, by a hash of it so the spread does not
    follow whatever pattern an application's ids happen to have."""
    digest = hashlib.sha256(item_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % buckets


class ItemReplayError(ValueError):
    """Raised when a delta log cannot be replayed to the version it claims."""


def replay_items(deltas: list[dict],
                 item_version: str | None = None) -> list[Instruction]:
    """Rebuild the item set at *item_version* from a delta log.

    The log is a sequence of records, each carrying the ``item_version`` and
    ``item_buckets`` it produced. A ``baseline`` states the whole set, an
    ``add`` carries whole items, and a ``reinforce`` carries ids with their
    new ``reinforced`` and ``salience``. Replay takes the last baseline at or
    before the target and applies what follows.

    The result is checked, not assumed: the rebuilt set is re-digested with
    the bucket count the writer recorded, and a version that does not match
    raises. Without the recorded count a replay after a reshard would
    recompute with the wrong one and report a mismatch on a set it had
    rebuilt correctly. Pinning the count also means the digest must not
    reshard while verifying, so it is built with resharding off.

    A delta of an unknown kind raises rather than being skipped. A log written
    by a later version of bear should fail to replay rather than replay to the
    wrong set quietly.
    """
    if not deltas:
        raise ItemReplayError("no item deltas to replay")

    if item_version is None:
        target = len(deltas) - 1
    else:
        matches = [i for i, d in enumerate(deltas)
                   if d.get("item_version") == item_version]
        if not matches:
            raise ItemReplayError(
                f"no delta produced item_version {item_version!r}")
        target = matches[-1]

    starts = [i for i in range(target, -1, -1)
              if deltas[i].get("kind") == "baseline"]
    if not starts:
        raise ItemReplayError(
            "no baseline at or before the target version, so the set it "
            "started from was never recorded")

    state: dict[str, dict] = {}
    for delta in deltas[starts[0]:target + 1]:
        kind = delta.get("kind")
        if kind == "baseline":
            state = {d["id"]: dict(d) for d in delta.get("items") or ()}
        elif kind == "add":
            for dump in delta.get("items") or ():
                state[dump["id"]] = dict(dump)
        elif kind == "reinforce":
            for touch in delta.get("reinforced") or ():
                held = state.get(touch["id"])
                if held is None:
                    raise ItemReplayError(
                        f"reinforce names {touch['id']!r}, which the log has "
                        f"not added")
                meta = dict(held.get("metadata") or {})
                # Only when the delta carries it. A clock written as None
                # would reach fade() as part of an age subtraction, so an
                # entry without one must leave the clock alone rather than
                # erase it.
                if touch.get("reinforced") is not None:
                    meta["reinforced"] = touch["reinforced"]
                if touch.get("salience") is not None:
                    meta["salience"] = touch["salience"]
                held["metadata"] = meta
        elif kind == "revise":
            # A revision that is not a reinforcement: salience, importance or
            # both, and the clock only where the caller asked for it. Each
            # entry carries the value before as well as after, and a field it
            # does not carry was not changed, so nothing here can overwrite a
            # field with None.
            for touch in delta.get("revised") or ():
                held = state.get(touch["id"])
                if held is None:
                    raise ItemReplayError(
                        f"revise names {touch['id']!r}, which the log has "
                        f"not added")
                meta = dict(held.get("metadata") or {})
                # resting is set to None on waking, not removed
                # (bear.items.wake), so None here rebuilds the same metadata.
                for field in ("salience", "importance", "reinforced", "resting"):
                    if field in touch:
                        meta[field] = touch[field]
                held["metadata"] = meta
        elif kind == "remove":
            for item_id in delta.get("ids") or ():
                state.pop(item_id, None)
        else:
            raise ItemReplayError(f"unknown item delta kind {kind!r}")

    items = [Instruction.model_validate(d) for d in state.values()]

    recorded = deltas[target]
    buckets = recorded.get("item_buckets")
    if buckets:
        # Resharding off, so the count stays the writer's.
        check = ItemDigest(buckets=buckets, reshard_at=float("inf"))
        check.rebuild(items)
        if check.version != recorded.get("item_version"):
            raise ItemReplayError(
                f"replayed {len(items)} items hash to {check.version!r}, "
                f"but the log records {recorded.get('item_version')!r}")
    return items


class ItemDigest:
    """A versioned digest over a set of items, maintained incrementally.

    Never saved. The id-to-digest map and the bucket digests are rebuilt from
    the items at load, which is one O(n) hash pass, cheaper than the embedding
    pass a build already makes. That adds no persistence surface, and saved
    digest state can never disagree with the items it claims to describe.
    """

    def __init__(self, buckets: int = DEFAULT_BUCKETS,
                 reshard_at: int = DEFAULT_RESHARD_AT) -> None:
        if buckets < 1 or buckets & (buckets - 1):
            raise ValueError(f"buckets must be a power of two, not {buckets}")
        if reshard_at < 1:
            raise ValueError(f"reshard_at must be positive, not {reshard_at}")
        self._buckets = buckets
        self._reshard_at = reshard_at
        self._digests: dict[str, str] = {}
        self._members: list[set[str]] = [set() for _ in range(buckets)]
        self._levels: list[list[str]] = []
        self._rebuild_tree()

    # -- what a caller reads ---------------------------------------------

    @property
    def buckets(self) -> int:
        """How many buckets, which is part of the version's definition."""
        return self._buckets

    @property
    def count(self) -> int:
        """How many items the digest covers."""
        return len(self._digests)

    @property
    def root(self) -> str:
        """The Merkle root over the bucket digests."""
        return self._levels[-1][0] if self._levels else ""

    @property
    def version(self) -> str:
        """The digest's version: root, item count and bucket count.

        Empty while there are no items, matching ``index_version``, so a
        caller cannot tell an empty store from an unbuilt one by its version
        alone. The count is in the hash so an empty set and a set whose
        digests happen to cancel cannot agree, and the bucket count is in it
        so a reshard is visible rather than silent.
        """
        if not self._digests:
            return ""
        payload = canonical_json([self.root, self.count, self._buckets])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    # -- maintaining it --------------------------------------------------

    def rebuild(self, instructions: Iterable[Instruction]) -> None:
        """Take the digest from these instructions, keeping only the items.

        Non-items are ignored: they are covered by ``index_version``, which is
        snapshotted whole, and mixing them in would put every instruction's
        dump back into the thing this exists to make cheap.
        """
        self._digests = {inst.id: item_digest(inst)
                         for inst in instructions
                         if inst.type == InstructionType.ITEM}
        self._reshard_if_needed(rebuild=False)
        self._refill_buckets()

    def set(self, instruction: Instruction) -> None:
        """Add an item, or replace one under the same id.

        One method for both because they are the same operation on the
        digest, and a reinforcement is the second case: the id stays and the
        dump changes.
        """
        if instruction.type != InstructionType.ITEM:
            return
        item_id = instruction.id
        new = item_digest(instruction)
        if self._digests.get(item_id) == new:
            return
        self._digests[item_id] = new
        if self._reshard_if_needed():
            return
        bucket = _bucket_of(item_id, self._buckets)
        self._members[bucket].add(item_id)
        self._recompute(bucket)

    def has(self, item_id: str) -> bool:
        """Whether this id is one of the items covered."""
        return item_id in self._digests

    def discard(self, item_id: str) -> None:
        """Remove an item by id.

        The id-to-digest map is what makes this cheap: a removal carries only
        an id, and the map supplies the digest to take out. Without it a
        removal would have to find the item, which is the O(n) this avoids.
        """
        if self._digests.pop(item_id, None) is None:
            return
        bucket = _bucket_of(item_id, self._buckets)
        self._members[bucket].discard(item_id)
        self._recompute(bucket)

    # -- internals -------------------------------------------------------

    def _reshard_if_needed(self, rebuild: bool = True) -> bool:
        """Double the bucket count while the buckets are too full.

        Returns whether anything was reshared, since the caller has then
        already had every bucket recomputed and must not recompute one again.
        """
        target = self._buckets
        while self._digests and len(self._digests) / target > self._reshard_at:
            target *= 2
        if target == self._buckets:
            return False
        self._buckets = target
        self._members = [set() for _ in range(target)]
        if rebuild:
            self._refill_buckets()
        return True

    def _refill_buckets(self) -> None:
        """Reassign every item and rebuild the tree, the O(n) path."""
        self._members = [set() for _ in range(self._buckets)]
        for item_id in self._digests:
            self._members[_bucket_of(item_id, self._buckets)].add(item_id)
        self._rebuild_tree()

    def _bucket_digest(self, bucket: int) -> str:
        """A SHA-256 over this bucket's sorted (id, digest) pairs.

        Sorted, so the digest does not depend on the order ids arrived in.
        """
        pairs = sorted((i, self._digests[i]) for i in self._members[bucket])
        return hashlib.sha256(
            canonical_json(pairs).encode("utf-8")).hexdigest()

    def _rebuild_tree(self) -> None:
        level = [self._bucket_digest(b) for b in range(self._buckets)]
        self._levels = [level]
        while len(level) > 1:
            level = [
                hashlib.sha256(
                    (level[i] + level[i + 1]).encode("utf-8")).hexdigest()
                for i in range(0, len(level), 2)
            ]
            self._levels.append(level)

    def _recompute(self, bucket: int) -> None:
        """Recompute one bucket and the path from it to the root."""
        self._levels[0][bucket] = self._bucket_digest(bucket)
        index = bucket
        for depth in range(len(self._levels) - 1):
            below = self._levels[depth]
            index //= 2
            left, right = below[index * 2], below[index * 2 + 1]
            self._levels[depth + 1][index] = hashlib.sha256(
                (left + right).encode("utf-8")).hexdigest()
