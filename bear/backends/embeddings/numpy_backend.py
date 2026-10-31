"""NumPy-based embedding backend — no external dependencies beyond numpy."""

from __future__ import annotations

from collections.abc import Callable

import numpy as np

from bear.backends.embeddings.base import EmbeddingBackendBase, MetadataFilter


def _salience_of(inst) -> tuple[float, float, float, float, float]:
    """An item's columns: stored, reinforced, half-life, item, importance.

    A half-life of NaN means the row carries none and takes whatever the query
    supplies. A non-item gets a salience of 1.0 and is marked so, since
    multiplying by it must leave an instruction's score untouched, and an
    importance of 0.0, which is the floor's identity.
    """
    from bear.models import InstructionType

    if inst.type != InstructionType.ITEM:
        return 1.0, 0.0, float("nan"), 0.0, 0.0
    meta = inst.metadata or {}
    half = meta.get("half_life")
    return (float(meta.get("salience", 1.0)),
            float(meta.get("reinforced", 0.0)),
            float(half) if half is not None else float("nan"),
            1.0,
            float(meta.get("importance", 0.0)))


def _top_k(values: np.ndarray, k: int,
           tie_order: Callable[[list[int]], list[int]] | None = None) -> np.ndarray:
    """Indices of the ``k`` largest values, highest first.

    With no ``tie_order``, exact ties come in whatever order ``argpartition``
    and the default ``argsort`` leave them, which differs between numpy
    versions, so two machines could rank one query differently. With one,
    which of the values equal to the k-th largest are kept, and the order of
    every run of equal values, are what it says. Only tied indices are passed
    to it, so this stays linear in the corpus, as ``argpartition`` was.
    """
    if k <= 0:
        return np.empty(0, dtype=np.intp)
    kth = np.partition(values, -k)[-k] if tie_order is not None else np.nan
    if np.isnan(kth):
        # NaN compares equal to nothing, so it falls back to the plain order.
        top = np.argpartition(values, -k)[-k:]
        return top[np.argsort(values[top])[::-1]]
    above = np.flatnonzero(values > kth)
    tied = np.flatnonzero(values == kth)
    if len(tied) > k - len(above):
        tied = np.asarray(tie_order(tied.tolist())[:k - len(above)], dtype=np.intp)
    top = np.concatenate([above, tied])
    top = top[np.argsort(-values[top], kind="stable")]
    ranked = values[top]
    # Runs of equal values, each put in the tie order.
    same = np.flatnonzero(ranked[1:] == ranked[:-1]).tolist()
    start = None
    for j, i in enumerate(same):
        if start is None:
            start = i
        if j + 1 == len(same) or same[j + 1] != i + 1:
            top[start:i + 2] = tie_order(top[start:i + 2].tolist())
            start = None
    return top


class NumpyBackend(EmbeddingBackendBase):
    """Cosine-similarity search using plain NumPy.

    Suitable for small to medium corpora, and for larger ones when a query
    narrows by tag: :meth:`search_with_filter` scores only the rows a filter
    admits, through an inverted index from tag to rows, so a question over
    many items costs the few that pass rather than all of them.
    """

    def __init__(self) -> None:
        self._embeddings: np.ndarray | None = None
        self._norms: np.ndarray | None = None
        # Inverted indexes for query-time narrowing, built only when the
        # caller supplies instructions. Empty means no narrowing is possible,
        # which the retriever's own filtering still covers.
        self._tag_rows: dict[str, set[int]] = {}
        self._type_rows: dict[str, set[int]] = {}
        self._priorities: np.ndarray | None = None
        # Salience columns, so fading thousands of rows is one array
        # operation rather than a Python loop over Instruction metadata.
        # NaN half-life means the row carries none and takes the query's.
        self._salience: np.ndarray | None = None
        self._reinforced: np.ndarray | None = None
        self._half_life: np.ndarray | None = None
        self._is_item: np.ndarray | None = None
        self._importance: np.ndarray | None = None
        self._rows: int = 0

    @property
    def supports_metadata_filtering(self) -> bool:  # noqa: D401
        """Whether this backend can narrow by metadata, which it can.

        A capability, not a readiness. The retriever reads it to decide
        whether to call :meth:`build_index_with_metadata`, so reporting
        readiness here would mean the index was never built and the narrowing
        never happened, silently. Readiness is handled in
        :meth:`search_with_filter`, which searches everything when no metadata
        was supplied.
        """
        return True

    def build_index(self, embeddings: np.ndarray) -> None:
        self._embeddings = embeddings.astype(np.float32)
        # Pre-compute norms for cosine similarity
        self._norms = np.linalg.norm(self._embeddings, axis=1, keepdims=True)
        # Avoid division by zero
        self._norms = np.maximum(self._norms, 1e-10)
        self._rows = int(self._embeddings.shape[0])

    @property
    def supports_incremental(self) -> bool:  # noqa: D401
        """Rows are appended to the array, so existing row numbers hold."""
        return True

    def add_rows(self, embeddings: np.ndarray, instructions: list) -> None:
        """Append these rows, extending the inverted indexes with them."""
        if self._embeddings is None:
            self.build_index_with_metadata(embeddings, instructions)
            return
        new = embeddings.astype(np.float32)
        if new.shape[0] == 0:
            return
        if new.shape[1] != self._embeddings.shape[1]:
            raise ValueError(
                f"cannot append width {new.shape[1]} to an index of width "
                f"{self._embeddings.shape[1]}")
        first = self._rows
        self._embeddings = np.vstack([self._embeddings, new])
        norms = np.maximum(np.linalg.norm(new, axis=1, keepdims=True), 1e-10)
        self._norms = np.vstack([self._norms, norms])
        self._rows = int(self._embeddings.shape[0])
        if self._priorities is not None:
            self._priorities = np.concatenate(
                [self._priorities,
                 np.array([i.priority for i in instructions], dtype=np.int32)])
        if self._salience is not None:
            cols = np.array([_salience_of(i) for i in instructions],
                            dtype=np.float64)
            self._salience = np.concatenate([self._salience, cols[:, 0]])
            self._reinforced = np.concatenate([self._reinforced, cols[:, 1]])
            self._half_life = np.concatenate([self._half_life, cols[:, 2]])
            self._is_item = np.concatenate(
                [self._is_item, cols[:, 3].astype(bool)])
            self._importance = np.concatenate([self._importance, cols[:, 4]])
        for offset, inst in enumerate(instructions):
            row = first + offset
            for tag in (*inst.tags, *inst.scope.tags, *inst.scope.required_tags):
                self._tag_rows.setdefault(tag, set()).add(row)
            self._type_rows.setdefault(inst.type.value, set()).add(row)

    def build_index_with_metadata(
        self, embeddings: np.ndarray, instructions: list
    ) -> None:
        """Build the index and the inverted indexes a filter narrows with.

        Every tag an instruction carries is indexed: ``tags``, ``scope.tags``
        and ``scope.required_tags``. All three, because the retriever's own
        selection rule counts all three, and a prefilter that indexes fewer
        narrows more than the rule does and runs first, so it wins. That is
        how selecting by owner came to return nothing: an owner tag lives in
        ``required_tags``, which the index did not hold.
        """
        self.build_index(embeddings)
        tag_rows: dict[str, set[int]] = {}
        type_rows: dict[str, set[int]] = {}
        priorities = np.zeros(len(instructions), dtype=np.int32)
        self._salience = np.ones(len(instructions), dtype=np.float64)
        self._reinforced = np.zeros(len(instructions), dtype=np.float64)
        self._half_life = np.full(len(instructions), np.nan, dtype=np.float64)
        self._is_item = np.zeros(len(instructions), dtype=bool)
        self._importance = np.zeros(len(instructions), dtype=np.float64)
        for row, inst in enumerate(instructions):
            self._salience[row], self._reinforced[row], self._half_life[row],                 self._is_item[row], self._importance[row] = _salience_of(inst)
            for tag in (*inst.tags, *inst.scope.tags, *inst.scope.required_tags):
                tag_rows.setdefault(tag, set()).add(row)
            type_rows.setdefault(inst.type.value, set()).add(row)
            priorities[row] = inst.priority
        self._tag_rows = tag_rows
        self._type_rows = type_rows
        self._priorities = priorities

    def _admitted(self, mf: MetadataFilter) -> set[int] | None:
        """The rows this filter admits, or None when it narrows nothing."""
        admitted: set[int] | None = None

        def narrow(rows: set[int]) -> None:
            nonlocal admitted
            admitted = rows if admitted is None else (admitted & rows)

        if mf.tags_all:
            for tag in mf.tags_all:
                narrow(self._tag_rows.get(tag, set()))
        if mf.tags_any:
            union: set[int] = set()
            for tag in mf.tags_any:
                union |= self._tag_rows.get(tag, set())
            narrow(union)
        if mf.type_in:
            union = set()
            for name in mf.type_in:
                union |= self._type_rows.get(name, set())
            narrow(union)
        if mf.min_priority is not None and self._priorities is not None:
            narrow({int(r) for r in
                    np.nonzero(self._priorities >= mf.min_priority)[0]})
        if (mf.min_salience is not None and mf.now is not None
                and self._salience is not None):
            # Faded items are dropped before scoring, which is what keeps a
            # large store cheap. An instruction carries no salience and is
            # never dropped, so only item rows are tested.
            #
            # Over the rows already narrowed to, where there are any. One
            # agent's memory is a few hundred rows of a corpus that may be
            # a hundred thousand, and fading all of them to then intersect
            # was 1.9 ms of a 3.4 ms query at 86,944 items, most of it
            # building a Python set of every passing row. The tags narrow
            # first because that is cheap, so by here the work is usually
            # small. See design/retrieval-candidates.md.
            from bear.items import fade
            rows = (np.fromiter(sorted(admitted), dtype=np.int64,
                                count=len(admitted))
                    if admitted is not None else None)
            salience, reinforced, importance, is_item, half_life = (
                (self._salience, self._reinforced, self._importance,
                 self._is_item, self._half_life) if rows is None else
                (self._salience[rows], self._reinforced[rows],
                 self._importance[rows], self._is_item[rows],
                 self._half_life[rows]))
            half = np.where(np.isnan(half_life),
                            np.inf if mf.half_life is None else mf.half_life,
                            half_life)
            # With importance as a floor, an item whose importance clears
            # the threshold is always a candidate, however long it has been
            # ignored. Without that, an item pushed below this line could
            # never be retrieved, so never be observed being useful, so never
            # be raised again: lowering salience was a one-way door that
            # switched off its own correction.
            faded = fade(salience, reinforced, half, mf.now, mf.recent,
                         importance)
            keep = (~is_item) | (faded >= mf.min_salience)
            kept = np.nonzero(keep)[0] if rows is None else rows[keep]
            narrow({int(r) for r in kept})
        return admitted

    def search_with_filter(
        self,
        query_embedding: np.ndarray,
        top_k: int,
        metadata_filter: MetadataFilter | None = None,
    ) -> list[tuple[int, float]]:
        """Search the rows a filter admits, scoring no others.

        A filter admitting nothing returns nothing. It does not fall back to
        an unfiltered search: a question that asked for only an agent's family
        memories and has none wants none, and widening would answer a
        different question from the one asked.
        """
        if metadata_filter is None:
            return self.search(query_embedding, top_k)
        if not (self._tag_rows or self._type_rows or self._priorities is not None):
            # Built without metadata, so there is nothing to narrow by. The
            # retriever's own selection pass still applies.
            return self.search(query_embedding, top_k)
        admitted = self._admitted(metadata_filter)
        if admitted is None:
            return self.search(query_embedding, top_k)
        if not admitted:
            return []
        return self._search_rows(query_embedding, top_k, sorted(admitted))

    def search(self, query_embedding: np.ndarray, top_k: int) -> list[tuple[int, float]]:
        if self._embeddings is None:
            raise RuntimeError("Index not built. Call build_index() first.")
        return self._search_rows(query_embedding, top_k, None)

    def _search_rows(
        self,
        query_embedding: np.ndarray,
        top_k: int,
        rows: list[int] | None,
    ) -> list[tuple[int, float]]:
        if self._embeddings is None:
            raise RuntimeError("Index not built. Call build_index() first.")

        query = query_embedding.astype(np.float32).reshape(1, -1)
        query_norm = np.linalg.norm(query)
        if query_norm < 1e-10:
            return []

        if rows is None:
            vectors, norms = self._embeddings, self._norms
        else:
            vectors = self._embeddings[rows]
            norms = self._norms[rows]
        if vectors.shape[0] == 0:
            return []

        # Cosine similarity
        similarities = (vectors @ query.T).flatten() / (
            norms.flatten() * query_norm
        )

        # Get top-k indices
        k = min(top_k, len(similarities))
        order = self.tie_order
        if order is not None and rows is not None:
            # The search ran over a subset, so its indices are positions in
            # rows, and the tie order is over the corpus's own rows.
            def order(local: list[int], _order=order) -> list[int]:
                back = {rows[i]: i for i in local}
                return [back[row] for row in _order(list(back))]
        top_indices = _top_k(similarities, k, order)

        if rows is None:
            return [(int(idx), float(similarities[idx])) for idx in top_indices]
        return [(rows[int(idx)], float(similarities[idx])) for idx in top_indices]

    def update_salience(self, rows: list[int], instructions: list) -> None:
        """Rewrite the salience columns for these rows, and nothing else.

        For a reinforcement, which changes an item's metadata and leaves its
        content, tags and priority alone. Named for what it touches so it
        cannot be reached for when something else changed: a new text needs a
        new vector and a new tag needs the inverted index rebuilt, neither of
        which this does.
        """
        if self._salience is None:
            return
        for row, inst in zip(rows, instructions):
            (self._salience[row], self._reinforced[row],
             self._half_life[row], is_item,
             self._importance[row]) = _salience_of(inst)
            self._is_item[row] = bool(is_item)

    def salience_at(self, now: float, half_life: float | None,
                    recent: float = 0.0) -> "np.ndarray | None":
        """Faded salience per row, or None when no metadata was built.

        One array operation over every row, which is why the columns exist.
        A row that is not an item gets 1.0, so multiplying by this leaves an
        instruction's score alone.
        """
        if self._salience is None:
            return None
        half = np.where(np.isnan(self._half_life),
                        np.inf if half_life is None else half_life,
                        self._half_life)
        from bear.items import fade
        faded = fade(self._salience, self._reinforced, half, now, recent,
                     self._importance)
        return np.where(self._is_item, faded, 1.0)

    def reset(self) -> None:
        self._embeddings = None
        self._norms = None
        self._tag_rows = {}
        self._type_rows = {}
        self._priorities = None
        self._salience = None
        self._reinforced = None
        self._half_life = None
        self._is_item = None
        self._importance = None
        self._rows = 0
