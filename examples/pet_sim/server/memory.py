"""Player relationship memory system.

Tracks how each player interacts with each pet over time.
Converts accumulated interaction history into relationship tags
that BEAR uses for scope-matching behavioral instructions.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


# Preferred items per pet species
PREFERRED_ITEMS = {
    "dog1": "ball",
    "cat1": "treat",
}


@dataclass
class PlayerRelationship:
    """Tracks one pet's relationship with one player."""

    player_id: str
    pet_id: str
    total_pets: int = 0
    preferred_items_given: int = 0
    non_preferred_items: int = 0
    last_interaction_time: float = 0.0
    affinity_score: float = 0.0  # -1.0 (hostile) to 1.0 (bonded)

    def record_pet(self, timestamp: float) -> None:
        self.total_pets += 1
        self.last_interaction_time = timestamp
        self.affinity_score = min(1.0, self.affinity_score + 0.05)

    def record_item(self, item_kind: str, timestamp: float) -> None:
        preferred = PREFERRED_ITEMS.get(self.pet_id)
        if item_kind == preferred:
            self.preferred_items_given += 1
            self.affinity_score = min(1.0, self.affinity_score + 0.08)
        else:
            self.non_preferred_items += 1
            self.affinity_score = max(-1.0, self.affinity_score - 0.02)
        self.last_interaction_time = timestamp

    @property
    def relationship_tag(self) -> str:
        """Convert affinity to a tag the retriever can match against."""
        if self.affinity_score >= 0.7:
            return "player_bonded"
        elif self.affinity_score >= 0.3:
            return "player_friend"
        elif self.affinity_score >= 0.0:
            return "player_neutral"
        else:
            return "player_wary"

    @property
    def summary(self) -> str:
        return (
            f"Player {self.player_id}: petted {self.total_pets}x, "
            f"gave {self.preferred_items_given} preferred / "
            f"{self.non_preferred_items} non-preferred items, "
            f"affinity={self.affinity_score:.2f} ({self.relationship_tag})"
        )


class PlayerMemoryStore:
    """Stores relationships for all player-pet pairs in a room."""

    def __init__(self):
        # Key: (player_id, pet_id) -> PlayerRelationship
        self._relationships: dict[tuple[str, str], PlayerRelationship] = {}

    def get(self, player_id: str, pet_id: str) -> PlayerRelationship:
        key = (player_id, pet_id)
        if key not in self._relationships:
            self._relationships[key] = PlayerRelationship(
                player_id=player_id, pet_id=pet_id
            )
        return self._relationships[key]

    def record_pet(self, player_id: str, pet_id: str, timestamp: float) -> None:
        self.get(player_id, pet_id).record_pet(timestamp)

    def record_item(
        self, player_id: str, pet_id: str, item_kind: str, timestamp: float
    ) -> None:
        """Record that a player placed an item near a pet."""
        self.get(player_id, pet_id).record_item(item_kind, timestamp)

    def best_relationship_for_pet(self, pet_id: str) -> PlayerRelationship | None:
        """Get the player with highest affinity for a given pet."""
        best = None
        for (pid, petid), rel in self._relationships.items():
            if petid == pet_id:
                if best is None or rel.affinity_score > best.affinity_score:
                    best = rel
        return best

    def relationship_tags_for_pet(self, pet_id: str) -> list[str]:
        """Get all unique relationship tags relevant to a pet."""
        tags = set()
        for (pid, petid), rel in self._relationships.items():
            if petid == pet_id and rel.total_pets > 0:
                tags.add(rel.relationship_tag)
        return list(tags)

    def summary(self) -> str:
        lines = []
        for rel in self._relationships.values():
            if rel.total_pets > 0 or rel.preferred_items_given > 0:
                lines.append(rel.summary)
        return "\n".join(lines) if lines else "No interactions yet."
