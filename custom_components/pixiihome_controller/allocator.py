"""Integration-owned Pixii message identifier allocation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .exceptions import CommandValidationError

MAX_SAFE_MESSAGE_ID = 9_007_199_254_740_991
DEFAULT_MESSAGE_ID = 1_000_000_000_000

PersistState = Callable[[dict[str, Any]], None]


def normalize_message_id(value: int | str) -> int:
    """Normalize a positive Pixii m_id without accepting lossy values."""
    if isinstance(value, bool):
        raise CommandValidationError("m_id must be a positive integer")
    if isinstance(value, int):
        normalized = value
    elif isinstance(value, str) and value.isdecimal():
        normalized = int(value)
    else:
        raise CommandValidationError("m_id must be a positive integer")
    if not 0 < normalized <= MAX_SAFE_MESSAGE_ID:
        raise CommandValidationError("m_id is outside the JavaScript-safe range")
    return normalized


def serialize_message_id(value: int, *, as_string: bool = False) -> int | str:
    """Serialize an owned identifier in the representation required by a payload."""
    normalized = normalize_message_id(value)
    return str(normalized) if as_string else normalized


@dataclass(slots=True)
class MessageIdAllocator:
    """Allocate persistent identifiers owned by this integration only."""

    next_value: int = DEFAULT_MESSAGE_ID
    owned_ids: set[int] = field(default_factory=set)
    active_ids: set[int] = field(default_factory=set, repr=False)
    persist: PersistState | None = None

    def __post_init__(self) -> None:
        self.next_value = normalize_message_id(self.next_value)
        self.owned_ids = {normalize_message_id(value) for value in self.owned_ids}

    @classmethod
    def from_state(
        cls,
        state: Mapping[str, Any] | None,
        *,
        persist: PersistState | None = None,
    ) -> MessageIdAllocator:
        """Restore only identifiers previously persisted by this integration."""
        if state is None:
            return cls(persist=persist)
        if not isinstance(state, Mapping):
            raise CommandValidationError("persisted command state must be an object")
        next_value = state.get("next_m_id", DEFAULT_MESSAGE_ID)
        owned = state.get("owned_m_ids", [])
        if not isinstance(owned, list):
            raise CommandValidationError("persisted owned m_ids must be a list")
        return cls(
            next_value=normalize_message_id(next_value),
            owned_ids=set(owned),
            persist=persist,
        )

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-safe persisted allocator state."""
        return {
            "next_m_id": self.next_value,
            "owned_m_ids": sorted(self.owned_ids),
        }

    def allocate(self, *, persistent: bool = False) -> int:
        """Allocate an ID and persist only ownership that needs lifecycle tracking."""
        candidate = self.next_value
        while candidate in self.owned_ids or candidate in self.active_ids:
            candidate = 1 if candidate == MAX_SAFE_MESSAGE_ID else candidate + 1
            if candidate == self.next_value:
                raise CommandValidationError("no unused safe m_id is available")
        self.active_ids.add(candidate)
        if persistent:
            self.owned_ids.add(candidate)
        self.next_value = 1 if candidate == MAX_SAFE_MESSAGE_ID else candidate + 1
        if self.persist is not None:
            self.persist(self.snapshot())
        return candidate

    def retire(self, value: int | str, *, release_ownership: bool = False) -> None:
        """Retire an active one-shot ID without deleting persistent ownership by default."""
        message_id = normalize_message_id(value)
        self.active_ids.discard(message_id)
        if release_ownership:
            self.owned_ids.discard(message_id)
            if self.persist is not None:
                self.persist(self.snapshot())

    def observe_external(self, value: int | str) -> None:
        """Validate external evidence without claiming ownership of its m_id."""
        normalize_message_id(value)
