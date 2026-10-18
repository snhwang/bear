"""Tests for bear.item_digest — the incremental digest over a set of items."""

import hashlib

import pytest

from bear.item_digest import (
    DEFAULT_BUCKETS,
    ItemDigest,
    ItemReplayError,
    canonical_json,
    item_digest,
    replay_items,
)
from bear.items import item, reinforce
from bear.models import Instruction, InstructionType


def _held(n, created=0.0, owner=None):
    return item(f"Fact {n} about the river and the weather.",
                owner or f"h{n % 7}", id=f"i{n}", tags=["news"], created=created)


def _from_scratch(instructions, **kw):
    d = ItemDigest(**kw)
    d.rebuild(instructions)
    return d


class TestIncrementalEqualsFromScratch:
    """The property everything else rests on. If these ever disagree, a
    replay would report a match on a set it had not reproduced."""

    def test_after_an_add(self):
        held = [_held(n) for n in range(10)]
        grown = _from_scratch(held)
        grown.set(_held(99))
        assert grown.version == _from_scratch([*held, _held(99)]).version

    def test_after_a_remove(self):
        held = [_held(n) for n in range(10)]
        pruned = _from_scratch(held)
        pruned.discard("i3")
        rest = [h for h in held if h.id != "i3"]
        assert pruned.version == _from_scratch(rest).version

    def test_after_a_reinforce(self):
        held = [_held(n) for n in range(10)]
        touched = _from_scratch(held)
        touched.set(reinforce(held[2], 50.0))
        expected = [reinforce(h, 50.0) if h.id == "i2" else h for h in held]
        assert touched.version == _from_scratch(expected).version

    def test_after_a_long_mixed_sequence(self):
        held = {h.id: h for h in (_held(n) for n in range(20))}
        d = _from_scratch(held.values())
        for n in range(20, 40):
            fresh = _held(n)
            held[fresh.id] = fresh
            d.set(fresh)
        for n in (1, 5, 9, 25, 31):
            held.pop(f"i{n}", None)
            d.discard(f"i{n}")
        for n in (2, 6, 21, 33):
            if f"i{n}" in held:
                held[f"i{n}"] = reinforce(held[f"i{n}"], 77.0)
                d.set(held[f"i{n}"])
        assert d.version == _from_scratch(held.values()).version

    def test_order_does_not_matter(self):
        held = [_held(n) for n in range(10)]
        forwards, backwards = ItemDigest(), ItemDigest()
        for h in held:
            forwards.set(h)
        for h in reversed(held):
            backwards.set(h)
        assert forwards.version == backwards.version


class TestItMustSeeMetadata:
    """Reinforcement changes metadata and changes ranking with it, so a digest
    blind to metadata would let a replay rebuild a set that hashed equal and
    ranked differently. The one silent failure in this area."""

    def test_a_reinforce_changes_the_version(self):
        held = _held(0)
        d = _from_scratch([held])
        before = d.version
        d.set(reinforce(held, 50.0))
        assert d.version != before

    def test_a_salience_change_changes_the_version(self):
        """Started below the cap, since boosting a salience already at 1.0 at
        the same timestamp changes nothing, and the digest is right to say so.
        """
        held = item("A rumour about the mill.", "rosa", id="r1",
                    tags=["news"], created=0.0, salience=0.4)
        d = _from_scratch([held])
        before = d.version
        d.set(reinforce(held, 0.0, boost=0.5))
        assert d.version != before

    def test_the_per_item_digest_covers_metadata(self):
        held = _held(0)
        assert item_digest(held) != item_digest(reinforce(held, 1.0))


class TestTheBucketCountIsPartOfTheVersion:
    """Resharding reassigns every item, so a version computed before one
    would be unreproducible if the count were implicit."""

    def test_the_same_items_under_different_counts_differ(self):
        held = [_held(n) for n in range(10)]
        assert (_from_scratch(held, buckets=8).version
                != _from_scratch(held, buckets=16).version)

    def test_a_replay_with_the_writers_count_agrees(self):
        held = [_held(n) for n in range(10)]
        writer = _from_scratch(held, buckets=8)
        replay = _from_scratch(held, buckets=writer.buckets)
        assert replay.version == writer.version


class TestResharding:
    def test_it_doubles_when_the_buckets_fill(self):
        d = ItemDigest(buckets=4, reshard_at=2)
        for n in range(9):          # 9 / 4 > 2
            d.set(_held(n))
        assert d.buckets > 4
        assert d.count / d.buckets <= 2

    def test_it_doubles_as_far_as_needed_at_once(self):
        d = ItemDigest(buckets=2, reshard_at=1)
        d.rebuild([_held(n) for n in range(64)])
        assert d.buckets >= 64

    def test_resharding_still_equals_from_scratch(self):
        held = [_held(n) for n in range(40)]
        grown = ItemDigest(buckets=4, reshard_at=2)
        for h in held:
            grown.set(h)
        assert grown.version == _from_scratch(held, buckets=grown.buckets,
                                              reshard_at=2).version

    def test_a_reshard_shows_in_the_version(self):
        d = ItemDigest(buckets=4, reshard_at=2)
        for n in range(8):
            d.set(_held(n))
        before, count = d.version, d.buckets
        d.set(_held(99))            # tips it over
        assert d.buckets > count
        assert d.version != before


class TestWhatItCovers:
    def test_a_non_item_is_ignored(self):
        """Instructions are covered by index_version, which is snapshotted
        whole. Mixing them in would put every instruction's dump back into
        the thing this exists to make cheap."""
        plain = Instruction(id="d", type=InstructionType.DIRECTIVE,
                            priority=50, content="Speak plainly.")
        d = _from_scratch([_held(0), plain])
        assert d.count == 1
        d.set(plain)
        assert d.count == 1

    def test_an_empty_set_has_an_empty_version(self):
        assert ItemDigest().version == ""
        assert _from_scratch([]).version == ""

    def test_removing_the_last_item_empties_it(self):
        d = _from_scratch([_held(0)])
        d.discard("i0")
        assert d.count == 0 and d.version == ""

    def test_a_removal_of_something_absent_does_nothing(self):
        d = _from_scratch([_held(0)])
        before = d.version
        d.discard("never")
        d.discard("never")
        assert d.version == before

    def test_setting_an_unchanged_item_does_nothing(self):
        held = _held(0)
        d = _from_scratch([held])
        before = d.version
        d.set(held)
        assert d.version == before


class TestItsShape:
    def test_buckets_must_be_a_power_of_two(self):
        for bad in (0, 3, 100, -4):
            with pytest.raises(ValueError, match="power of two"):
                ItemDigest(buckets=bad)

    def test_reshard_at_must_be_positive(self):
        with pytest.raises(ValueError, match="positive"):
            ItemDigest(reshard_at=0)

    def test_a_mutation_touches_one_bucket_and_the_path(self):
        """O(n/B + log B), which is the point of the structure. A full
        rebuild would recompute every bucket."""
        d = ItemDigest(buckets=DEFAULT_BUCKETS)
        d.rebuild([_held(n) for n in range(500)])
        computed: list[int] = []
        original = d._bucket_digest
        d._bucket_digest = lambda b: computed.append(b) or original(b)

        d.set(_held(9999))
        assert len(computed) == 1, "one bucket, not every bucket"

    def test_the_canonical_form_is_shared_with_index_version(self):
        """Two canonicalisations would be two chances for one object to hash
        differently."""
        from bear import retriever
        assert retriever.canonical_json is canonical_json

    def test_the_version_is_a_sha256_of_root_count_and_buckets(self):
        d = _from_scratch([_held(0)])
        want = hashlib.sha256(canonical_json(
            [d.root, d.count, d.buckets]).encode("utf-8")).hexdigest()[:16]
        assert d.version == want
class TestReplayingADeltaLog:
    """Items have no snapshot, by design, so they are verified the other way
    round: the deltas are replayed to a version and the rebuilt set
    re-digested."""

    def _log(self):
        held = [_held(n) for n in range(3)]
        d = _from_scratch(held)
        log = [{"kind": "baseline", "item_version": d.version,
                "item_buckets": d.buckets,
                "items": [h.model_dump(mode="json") for h in held]}]
        extra = _held(9)
        d.set(extra)
        log.append({"kind": "add", "item_version": d.version,
                    "item_buckets": d.buckets,
                    "items": [extra.model_dump(mode="json")]})
        touched = reinforce(extra, 50.0)
        d.set(touched)
        log.append({"kind": "reinforce", "item_version": d.version,
                    "item_buckets": d.buckets,
                    "reinforced": [{"id": touched.id, "reinforced": 50.0,
                                    "salience": 1.0}]})
        return log

    def test_it_rebuilds_every_recorded_version(self):
        log = self._log()
        for n, entry in enumerate(log):
            items = replay_items(log, entry["item_version"])
            assert len(items) == (3 if n == 0 else 4)

    def test_it_rebuilds_the_last_version_by_default(self):
        assert len(replay_items(self._log())) == 4

    def test_a_reinforcement_comes_back(self):
        """The reason the digest covers metadata: a replay that lost this
        would rebuild a set that ranked differently."""
        log = self._log()
        items = {i.id: i for i in replay_items(log, log[-1]["item_version"])}
        assert items["i9"].metadata["reinforced"] == 50.0

    def test_a_tampered_item_fails_the_check(self):
        """What the verification is for. The set still has the right ids, and
        the digest says it is not the set the log claims."""
        log = self._log()
        log[0] = {**log[0], "items": [
            {**d, "content": "Something else entirely."}
            for d in log[0]["items"]]}
        with pytest.raises(ItemReplayError, match="hash to"):
            replay_items(log, log[0]["item_version"])

    def test_the_wrong_bucket_count_fails_rather_than_passing(self):
        log = self._log()
        log[0] = {**log[0], "item_buckets": log[0]["item_buckets"] * 2}
        with pytest.raises(ItemReplayError, match="hash to"):
            replay_items(log, log[0]["item_version"])

    def test_a_version_the_log_never_produced(self):
        with pytest.raises(ItemReplayError, match="no delta produced"):
            replay_items(self._log(), "0" * 16)

    def test_no_baseline_means_the_start_was_never_recorded(self):
        log = [d for d in self._log() if d["kind"] != "baseline"]
        with pytest.raises(ItemReplayError, match="no baseline"):
            replay_items(log, log[-1]["item_version"])

    def test_an_empty_log(self):
        with pytest.raises(ItemReplayError, match="no item deltas"):
            replay_items([])

    def test_an_unknown_kind_raises_rather_than_being_skipped(self):
        """A log written by a later bear should fail to replay rather than
        replay to the wrong set quietly."""
        log = self._log()
        log.append({"kind": "something-new", "item_version": "ff" * 8,
                    "item_buckets": log[-1]["item_buckets"]})
        with pytest.raises(ItemReplayError, match="unknown item delta kind"):
            replay_items(log, "ff" * 8)

    def test_a_reinforce_for_an_item_the_log_never_added(self):
        log = self._log()
        log.append({"kind": "reinforce", "item_version": "ab" * 8,
                    "item_buckets": log[-1]["item_buckets"],
                    "reinforced": [{"id": "never", "reinforced": 1.0}]})
        with pytest.raises(ItemReplayError, match="has not added"):
            replay_items(log, "ab" * 8)

    def test_a_tamper_before_a_baseline_does_not_taint_versions_after_it(self):
        """Replay starts from the last baseline at or before its target, so a
        delta earlier than that baseline cannot affect it. Pinned because the
        Hearthwick session's tamper run reported a differs after a baseline
        and asked whether restarting was intended. It is, and it works. Their
        run had altered the first record holding items, which was the first
        baseline rather than the add they meant, so the versions that differed
        were the right ones. My first explanation was that they had edited the
        log as text and so reached every copy. That was wrong, but the
        behaviour it describes is still worth the test below.
        """
        log = self._log()
        held = [_held(1), _held(2)]
        d = _from_scratch(held)
        log.append({"kind": "baseline", "item_version": d.version,
                    "item_buckets": d.buckets,
                    "items": [h.model_dump(mode="json") for h in held]})

        # Tamper with the add, which is before the new baseline.
        log[1] = {**log[1], "items": [
            {**dump, "content": dump["content"] + " and a lie."}
            for dump in log[1]["items"]]}

        with pytest.raises(ItemReplayError):
            replay_items(log, log[1]["item_version"])
        # The baseline after it is unaffected.
        assert len(replay_items(log, log[-1]["item_version"])) == 2

    def test_a_tamper_reaching_the_baseline_too_is_caught_there(self):
        """The case that matters: altering every copy of an item, which is
        what an edit reaching the baseline as well as the delta would do, must
        not make it verify."""
        log = self._log()
        held = [_held(1), _held(2)]
        d = _from_scratch(held)
        log.append({"kind": "baseline", "item_version": d.version,
                    "item_buckets": d.buckets,
                    "items": [h.model_dump(mode="json") for h in held]})
        log[-1] = {**log[-1], "items": [
            {**dump, "content": dump["content"] + " and a lie."}
            for dump in log[-1]["items"]]}

        with pytest.raises(ItemReplayError, match="hash to"):
            replay_items(log, log[-1]["item_version"])

    def test_a_later_baseline_supersedes_an_earlier_one(self):
        log = self._log()
        held = [_held(99)]
        d = _from_scratch(held)
        log.append({"kind": "baseline", "item_version": d.version,
                    "item_buckets": d.buckets,
                    "items": [h.model_dump(mode="json") for h in held]})
        items = replay_items(log, d.version)
        assert [i.id for i in items] == ["i99"]


class TestReplayingARevision:
    """Significance is a function of the whole history, so a revision that
    does not replay makes a reconstructed store diverge from the real one."""

    def _log_with(self, entry_fields):
        from bear.items import item as _mk
        held = [_held(n) for n in range(2)]
        d = _from_scratch(held)
        log = [{"kind": "baseline", "item_version": d.version,
                "item_buckets": d.buckets,
                "items": [h.model_dump(mode="json") for h in held]}]
        touched = held[0].model_copy(update={"metadata": {
            **held[0].metadata, **entry_fields["applied"]}})
        d.set(touched)
        log.append({"kind": "revise", "item_version": d.version,
                    "item_buckets": d.buckets,
                    "revised": [{"id": touched.id, **entry_fields["recorded"]}]})
        return log, touched.id

    def test_a_lowered_salience_comes_back(self):
        log, item_id = self._log_with(
            {"applied": {"salience": 0.25},
             "recorded": {"salience": 0.25, "salience_before": 1.0}})
        items = {i.id: i for i in replay_items(log)}
        assert items[item_id].metadata["salience"] == 0.25

    def test_importance_comes_back(self):
        log, item_id = self._log_with(
            {"applied": {"importance": 0.8},
             "recorded": {"importance": 0.8, "importance_before": 0.0}})
        items = {i.id: i for i in replay_items(log)}
        assert items[item_id].metadata["importance"] == 0.8

    def test_a_revision_without_a_clock_leaves_the_clock_alone(self):
        """The hazard this kind exists for. Under the reinforce kind the clock
        would have been overwritten with None and then used as an age."""
        log, item_id = self._log_with(
            {"applied": {"salience": 0.5},
             "recorded": {"salience": 0.5, "salience_before": 1.0}})
        before = {i["id"]: (i.get("metadata") or {}).get("reinforced")
                  for i in log[0]["items"]}
        items = {i.id: i for i in replay_items(log)}
        assert items[item_id].metadata["reinforced"] == before[item_id]
        assert items[item_id].metadata["reinforced"] is not None

    def test_a_revision_naming_an_unadded_item_raises(self):
        log, _ = self._log_with(
            {"applied": {"salience": 0.5},
             "recorded": {"salience": 0.5, "salience_before": 1.0}})
        log[-1]["revised"] = [{"id": "never-added", "salience": 0.5}]
        with pytest.raises(ItemReplayError, match="never-added"):
            replay_items(log)

    def test_a_reinforcement_without_a_clock_no_longer_erases_it(self):
        """The same shape from the other direction, hardened at the same
        time: an entry that carries no clock must leave it alone rather than
        set it to None."""
        held = [_held(0)]
        d = _from_scratch(held)
        log = [{"kind": "baseline", "item_version": d.version,
                "item_buckets": d.buckets,
                "items": [h.model_dump(mode="json") for h in held]}]
        touched = held[0].model_copy(update={"metadata": {
            **held[0].metadata, "salience": 0.4}})
        d.set(touched)
        log.append({"kind": "reinforce", "item_version": d.version,
                    "item_buckets": d.buckets,
                    "reinforced": [{"id": touched.id, "salience": 0.4}]})
        items = {i.id: i for i in replay_items(log)}
        assert items[touched.id].metadata["reinforced"] is not None
