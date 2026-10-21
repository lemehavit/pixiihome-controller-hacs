"""Versioned, checksummed control identity and transaction journal."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from ..allocator import normalize_message_id
from ..exceptions import CommandValidationError

JOURNAL_SCHEMA_VERSION = 3
JOURNAL_BACKUP_LIMIT = 3
JOURNAL_AUDIT_LIMIT = 100
JOURNAL_TRANSACTION_LIMIT = 256


class JournalPhase(StrEnum):
    """Durable transaction phases used for deterministic restart recovery."""

    PREPARED = "prepared"
    COMMAND_SENT = "command_sent"
    ACKNOWLEDGED = "acknowledged"
    RECOVERY_REQUIRED = "recovery_required"
    RECONCILED = "reconciled"
    FAILED = "failed"


_TERMINAL_PHASES = {JournalPhase.RECONCILED, JournalPhase.FAILED}
_ALLOWED_TRANSITIONS = {
    JournalPhase.PREPARED: {
        JournalPhase.COMMAND_SENT,
        JournalPhase.RECOVERY_REQUIRED,
        JournalPhase.FAILED,
    },
    JournalPhase.COMMAND_SENT: {
        JournalPhase.ACKNOWLEDGED,
        JournalPhase.RECOVERY_REQUIRED,
        JournalPhase.FAILED,
    },
    JournalPhase.ACKNOWLEDGED: {
        JournalPhase.RECONCILED,
        JournalPhase.RECOVERY_REQUIRED,
        JournalPhase.FAILED,
    },
    JournalPhase.RECOVERY_REQUIRED: {
        JournalPhase.RECONCILED,
        JournalPhase.FAILED,
    },
    JournalPhase.RECONCILED: set(),
    JournalPhase.FAILED: set(),
}


@dataclass(frozen=True, slots=True)
class JournalTargetIdentity:
    """Typed identity tuple; protocol identifiers are never interchangeable."""

    m_id: int | None = None
    sch_index: int | None = None
    service_no: int | None = None
    source_id: int | None = None
    asset_id: str | None = None

    def __post_init__(self) -> None:
        if self.m_id is not None:
            object.__setattr__(self, "m_id", normalize_message_id(self.m_id))
        for name in ("sch_index", "service_no", "source_id"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise CommandValidationError(f"journal target {name} is invalid")
        if self.asset_id is not None:
            if not isinstance(self.asset_id, str) or not self.asset_id.strip():
                raise CommandValidationError("journal target asset_id is invalid")
            object.__setattr__(self, "asset_id", self.asset_id.strip())
        if all(
            value is None
            for value in (
                self.m_id,
                self.sch_index,
                self.service_no,
                self.source_id,
                self.asset_id,
            )
        ):
            raise CommandValidationError("journal target identity is empty")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> JournalTargetIdentity:
        if not isinstance(value, Mapping):
            raise CommandValidationError("journal target identity must be an object")
        return cls(
            m_id=value.get("m_id"),
            sch_index=value.get("sch_index"),
            service_no=value.get("service_no"),
            source_id=value.get("source_id"),
            asset_id=value.get("asset_id"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "m_id": self.m_id,
            "sch_index": self.sch_index,
            "service_no": self.service_no,
            "source_id": self.source_id,
            "asset_id": self.asset_id,
        }


@dataclass(frozen=True, slots=True)
class JournalTransaction:
    """Sanitized durable record for one state-changing operation."""

    transaction_id: str
    operation: str
    target_identity: JournalTargetIdentity
    intent: Mapping[str, Any] = field(default_factory=dict)
    phase: JournalPhase = JournalPhase.PREPARED
    reason: str | None = None

    def __post_init__(self) -> None:
        for name in ("transaction_id", "operation"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise CommandValidationError(f"journal transaction requires {name}")
            object.__setattr__(self, name, value.strip())
        if not isinstance(self.target_identity, JournalTargetIdentity):
            raise CommandValidationError("journal transaction requires typed target identity")
        if not isinstance(self.phase, JournalPhase):
            object.__setattr__(self, "phase", JournalPhase(self.phase))
        if not isinstance(self.intent, Mapping):
            raise CommandValidationError("journal transaction intent must be an object")
        intent = deepcopy(dict(self.intent))
        _checksum(intent)
        object.__setattr__(self, "intent", intent)
        if self.reason is not None and not isinstance(self.reason, str):
            raise CommandValidationError("journal transaction reason must be a string")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> JournalTransaction:
        try:
            return cls(
                transaction_id=value["transaction_id"],
                operation=value["operation"],
                target_identity=JournalTargetIdentity.from_mapping(value["target_identity"]),
                phase=JournalPhase(value["phase"]),
                intent=value.get("intent", {}),
                reason=value.get("reason"),
            )
        except (KeyError, TypeError, ValueError) as err:
            raise CommandValidationError("journal transaction is invalid") from err

    def as_dict(self) -> dict[str, Any]:
        result = {
            "transaction_id": self.transaction_id,
            "operation": self.operation,
            "target_identity": self.target_identity.as_dict(),
            "phase": self.phase.value,
            "reason": self.reason,
        }
        if self.intent:
            result["intent"] = deepcopy(dict(self.intent))
        return result


@dataclass(frozen=True, slots=True)
class ControlJournal:
    """Immutable candidate state persisted atomically by the runtime adapter."""

    config_entry_id: str
    gateway_serial: str
    revision: int = 0
    command_state: Mapping[str, Any] = field(default_factory=dict)
    ownership_state: Mapping[str, Any] = field(default_factory=dict)
    controller_state: Mapping[str, Any] = field(default_factory=dict)
    planner_state: Mapping[str, Any] = field(default_factory=dict)
    replacement_state: Mapping[str, Any] = field(default_factory=dict)
    audit_history: tuple[Mapping[str, Any], ...] = ()
    transactions: tuple[JournalTransaction, ...] = ()
    backups: tuple[Mapping[str, Any], ...] = ()
    recovered_from_backup: bool = False
    migrated_from_legacy: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.config_entry_id, str) or not self.config_entry_id.strip():
            raise CommandValidationError("control journal requires a config entry ID")
        if not isinstance(self.gateway_serial, str) or not self.gateway_serial.strip():
            raise CommandValidationError("control journal requires a Gateway serial")
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 0
        ):
            raise CommandValidationError("control journal revision is invalid")
        for name in (
            "command_state",
            "ownership_state",
            "controller_state",
            "planner_state",
            "replacement_state",
        ):
            if not isinstance(getattr(self, name), Mapping):
                raise CommandValidationError(f"control journal {name} must be an object")
        if not isinstance(self.audit_history, tuple) or any(
            not isinstance(item, Mapping) for item in self.audit_history
        ):
            raise CommandValidationError("control journal audit history must contain objects")
        if len(self.transactions) > JOURNAL_TRANSACTION_LIMIT:
            raise CommandValidationError("control journal transaction limit exceeded")
        object.__setattr__(self, "config_entry_id", self.config_entry_id.strip())
        object.__setattr__(self, "gateway_serial", self.gateway_serial.strip())
        for name in (
            "command_state",
            "ownership_state",
            "controller_state",
            "planner_state",
            "replacement_state",
        ):
            value = deepcopy(dict(getattr(self, name)))
            _checksum(value)
            object.__setattr__(self, name, value)
        audit_history = tuple(deepcopy(dict(item)) for item in self.audit_history)
        for item in audit_history:
            _checksum(item)
        object.__setattr__(self, "audit_history", audit_history[-JOURNAL_AUDIT_LIMIT:])
        object.__setattr__(self, "transactions", tuple(self.transactions))
        object.__setattr__(self, "backups", tuple(deepcopy(dict(item)) for item in self.backups))

    @classmethod
    def empty(cls, *, config_entry_id: str, gateway_serial: str) -> ControlJournal:
        return cls(config_entry_id=config_entry_id, gateway_serial=gateway_serial)

    @classmethod
    def migrate_legacy(
        cls,
        *,
        config_entry_id: str,
        gateway_serial: str,
        command_state: Mapping[str, Any] | None,
        ownership_state: Mapping[str, Any] | None,
    ) -> ControlJournal:
        return cls(
            config_entry_id=config_entry_id,
            gateway_serial=gateway_serial,
            command_state=command_state or {},
            ownership_state=ownership_state or {},
            migrated_from_legacy=bool(command_state or ownership_state),
        )

    @classmethod
    def from_state(
        cls,
        state: Mapping[str, Any],
        *,
        config_entry_id: str,
        gateway_serial: str,
    ) -> ControlJournal:
        if not isinstance(state, Mapping):
            raise CommandValidationError("control journal state must be an object")
        version = state.get("schema_version")
        if version == 1:
            journal = cls(
                config_entry_id=config_entry_id,
                gateway_serial=gateway_serial,
                revision=state.get("revision", 0),
                command_state=state.get("command_state", {}),
                ownership_state=state.get("ownership_state", {}),
                transactions=tuple(
                    JournalTransaction.from_mapping(item)
                    for item in state.get("transactions", [])
                ),
                migrated_from_legacy=True,
            )
            journal._validate_scope(config_entry_id, gateway_serial)
            return journal
        if version not in {2, JOURNAL_SCHEMA_VERSION}:
            raise CommandValidationError("control journal schema version is unsupported")

        candidates = [state]
        raw_backups = state.get("backups", [])
        if isinstance(raw_backups, list):
            candidates.extend(reversed(raw_backups))
        primary_error: CommandValidationError | None = None
        for index, candidate in enumerate(candidates):
            try:
                journal = cls._from_checksummed(candidate, backups=raw_backups)
                journal._validate_scope(config_entry_id, gateway_serial)
                if index:
                    journal = replace(journal, recovered_from_backup=True, backups=())
                return journal
            except CommandValidationError as err:
                primary_error = primary_error or err
        raise primary_error or CommandValidationError("control journal is invalid")

    @classmethod
    def _from_checksummed(
        cls,
        state: Mapping[str, Any],
        *,
        backups: object,
    ) -> ControlJournal:
        core = _core_from_mapping(state)
        if core["schema_version"] not in {2, JOURNAL_SCHEMA_VERSION}:
            raise CommandValidationError("control journal backup schema is unsupported")
        checksum = state.get("checksum")
        if not isinstance(checksum, str) or checksum != _checksum(core):
            raise CommandValidationError("control journal checksum mismatch")
        raw_transactions = core["transactions"]
        if not isinstance(raw_transactions, list):
            raise CommandValidationError("control journal transactions must be a list")
        return cls(
            config_entry_id=core["config_entry_id"],
            gateway_serial=core["gateway_serial"],
            revision=core["revision"],
            command_state=core["command_state"],
            ownership_state=core["ownership_state"],
            controller_state=core.get("controller_state", {}),
            planner_state=core.get("planner_state", {}),
            replacement_state=core.get("replacement_state", {}),
            audit_history=tuple(core.get("audit_history", [])),
            transactions=tuple(
                JournalTransaction.from_mapping(item) for item in raw_transactions
            ),
            backups=tuple(backups) if isinstance(backups, list) else (),
            migrated_from_legacy=core["schema_version"] < JOURNAL_SCHEMA_VERSION,
        )

    def _validate_scope(self, config_entry_id: str, gateway_serial: str) -> None:
        if (
            self.config_entry_id != config_entry_id
            or self.gateway_serial != gateway_serial
        ):
            raise CommandValidationError("control journal scope does not match")

    def with_states(
        self,
        *,
        command_state: Mapping[str, Any] | None = None,
        ownership_state: Mapping[str, Any] | None = None,
        audit_event: Mapping[str, Any] | None = None,
    ) -> ControlJournal:
        if audit_event is not None and not isinstance(audit_event, Mapping):
            raise CommandValidationError("control journal audit event must be an object")
        return self._next(
            command_state=command_state if command_state is not None else self.command_state,
            ownership_state=(
                ownership_state if ownership_state is not None else self.ownership_state
            ),
            transactions=self.transactions,
            audit_history=(*self.audit_history, deepcopy(dict(audit_event)))[-JOURNAL_AUDIT_LIMIT:]
            if audit_event is not None else self.audit_history,
        )

    def with_product_state(
        self,
        *,
        controller_state: Mapping[str, Any] | None = None,
        planner_state: Mapping[str, Any] | None = None,
        replacement_state: Mapping[str, Any] | None = None,
        audit_event: Mapping[str, Any] | None = None,
    ) -> ControlJournal:
        """Persist controller, planner, replacement, and bounded audit state."""
        audit_history = self.audit_history
        if audit_event is not None:
            if not isinstance(audit_event, Mapping):
                raise CommandValidationError("control journal audit event must be an object")
            audit_history = (*audit_history, deepcopy(dict(audit_event)))[
                -JOURNAL_AUDIT_LIMIT:
            ]
        return self._next(
            controller_state=(
                controller_state if controller_state is not None else self.controller_state
            ),
            planner_state=planner_state if planner_state is not None else self.planner_state,
            replacement_state=(
                replacement_state
                if replacement_state is not None
                else self.replacement_state
            ),
            audit_history=audit_history,
        )

    def begin_transaction(
        self,
        *,
        transaction_id: str,
        operation: str,
        target_identity: JournalTargetIdentity,
        intent: Mapping[str, Any] | None = None,
        command_state: Mapping[str, Any] | None = None,
        ownership_state: Mapping[str, Any] | None = None,
    ) -> ControlJournal:
        if self.transaction(transaction_id) is not None:
            raise CommandValidationError("journal transaction already exists")
        transaction = JournalTransaction(
            transaction_id,
            operation,
            target_identity,
            intent=intent or {},
        )
        transactions = _bounded_transactions(
            (*self.transactions, transaction),
            replacement_state=self.replacement_state,
        )
        return self._next(
            command_state=command_state if command_state is not None else self.command_state,
            ownership_state=(
                ownership_state if ownership_state is not None else self.ownership_state
            ),
            transactions=transactions,
        )

    def transition_transaction(
        self,
        transaction_id: str,
        phase: JournalPhase,
        *,
        reason: str | None = None,
    ) -> ControlJournal:
        phase = JournalPhase(phase)
        current = self.transaction(transaction_id)
        if current is None:
            raise CommandValidationError("journal transaction does not exist")
        if phase not in _ALLOWED_TRANSITIONS[current.phase]:
            raise CommandValidationError("journal transaction transition is invalid")
        replacement = replace(current, phase=phase, reason=reason)
        transactions = tuple(
            replacement if item.transaction_id == transaction_id else item
            for item in self.transactions
        )
        return self._next(transactions=transactions)

    def reconcile_transaction(
        self,
        transaction_id: str,
        *,
        command_state: Mapping[str, Any],
        ownership_state: Mapping[str, Any],
        reason: str | None = None,
    ) -> ControlJournal:
        """Commit recovered identity state and the terminal phase in one revision."""
        current = self.transaction(transaction_id)
        if current is None:
            raise CommandValidationError("journal transaction does not exist")
        if JournalPhase.RECONCILED not in _ALLOWED_TRANSITIONS[current.phase]:
            raise CommandValidationError("journal transaction transition is invalid")
        replacement = replace(
            current,
            phase=JournalPhase.RECONCILED,
            reason=reason,
        )
        transactions = tuple(
            replacement if item.transaction_id == transaction_id else item
            for item in self.transactions
        )
        return self._next(
            command_state=command_state,
            ownership_state=ownership_state,
            transactions=transactions,
        )

    def mark_unfinished_recovery_required(self) -> ControlJournal:
        """Convert interrupted phases to an explicit persisted recovery state."""
        changed = False
        transactions: list[JournalTransaction] = []
        for transaction in self.transactions:
            if transaction.phase in _TERMINAL_PHASES or (
                transaction.phase is JournalPhase.RECOVERY_REQUIRED
            ):
                transactions.append(transaction)
                continue
            transactions.append(
                replace(
                    transaction,
                    phase=JournalPhase.RECOVERY_REQUIRED,
                    reason=transaction.reason or "restart_recovery_required",
                )
            )
            changed = True
        return self._next(transactions=tuple(transactions)) if changed else self

    def transaction(self, transaction_id: str) -> JournalTransaction | None:
        return next(
            (item for item in self.transactions if item.transaction_id == transaction_id),
            None,
        )

    @property
    def unresolved_transactions(self) -> tuple[JournalTransaction, ...]:
        return tuple(item for item in self.transactions if item.phase not in _TERMINAL_PHASES)

    def snapshot(self) -> dict[str, Any]:
        core = self._core()
        return {
            **core,
            "checksum": _checksum(core),
            "backups": [deepcopy(dict(item)) for item in self.backups],
        }

    def _next(
        self,
        *,
        command_state: Mapping[str, Any] | None = None,
        ownership_state: Mapping[str, Any] | None = None,
        controller_state: Mapping[str, Any] | None = None,
        planner_state: Mapping[str, Any] | None = None,
        replacement_state: Mapping[str, Any] | None = None,
        audit_history: tuple[Mapping[str, Any], ...] | None = None,
        transactions: tuple[JournalTransaction, ...] | None = None,
    ) -> ControlJournal:
        backup = {**self._core(), "checksum": _checksum(self._core())}
        backups = (*self.backups, backup)[-JOURNAL_BACKUP_LIMIT:]
        return ControlJournal(
            config_entry_id=self.config_entry_id,
            gateway_serial=self.gateway_serial,
            revision=self.revision + 1,
            command_state=command_state if command_state is not None else self.command_state,
            ownership_state=(
                ownership_state if ownership_state is not None else self.ownership_state
            ),
            controller_state=(
                controller_state if controller_state is not None else self.controller_state
            ),
            planner_state=planner_state if planner_state is not None else self.planner_state,
            replacement_state=(
                replacement_state
                if replacement_state is not None
                else self.replacement_state
            ),
            audit_history=(
                audit_history if audit_history is not None else self.audit_history
            ),
            transactions=_bounded_transactions(
                transactions if transactions is not None else self.transactions,
                replacement_state=(
                    replacement_state
                    if replacement_state is not None
                    else self.replacement_state
                ),
            ),
            backups=backups,
        )

    def _core(self) -> dict[str, Any]:
        return {
            "schema_version": JOURNAL_SCHEMA_VERSION,
            "revision": self.revision,
            "config_entry_id": self.config_entry_id,
            "gateway_serial": self.gateway_serial,
            "command_state": deepcopy(dict(self.command_state)),
            "ownership_state": deepcopy(dict(self.ownership_state)),
            "controller_state": deepcopy(dict(self.controller_state)),
            "planner_state": deepcopy(dict(self.planner_state)),
            "replacement_state": deepcopy(dict(self.replacement_state)),
            "audit_history": [deepcopy(dict(item)) for item in self.audit_history],
            "transactions": [item.as_dict() for item in self.transactions],
        }


def _bounded_transactions(
    transactions: tuple[JournalTransaction, ...],
    *,
    replacement_state: Mapping[str, Any],
) -> tuple[JournalTransaction, ...]:
    """Retain recent terminal records without ever evicting live recovery state."""
    protected_ids = {
        item.transaction_id
        for item in transactions
        if item.phase not in _TERMINAL_PHASES
    }
    replacement_phase = replacement_state.get("phase")
    replacement_transaction_id = replacement_state.get("transaction_id")
    if (
        replacement_phase
        in {"prepared", "predecessor_deleted", "replacement_created", "partial"}
        and isinstance(replacement_transaction_id, str)
    ):
        protected_ids.add(replacement_transaction_id)
    if len(protected_ids) > JOURNAL_TRANSACTION_LIMIT:
        raise CommandValidationError("control journal unresolved transaction limit exceeded")
    terminal_slots = JOURNAL_TRANSACTION_LIMIT - len(protected_ids)
    terminal_ids = [
        item.transaction_id
        for item in transactions
        if item.transaction_id not in protected_ids
    ]
    retained_terminal_ids = set(
        terminal_ids[-terminal_slots:] if terminal_slots else ()
    )
    return tuple(
        item
        for item in transactions
        if item.transaction_id in protected_ids
        or item.transaction_id in retained_terminal_ids
    )


def _core_from_mapping(state: Mapping[str, Any]) -> dict[str, Any]:
    try:
        version = state["schema_version"]
        core = {
            "schema_version": version,
            "revision": state["revision"],
            "config_entry_id": state["config_entry_id"],
            "gateway_serial": state["gateway_serial"],
            "command_state": state["command_state"],
            "ownership_state": state["ownership_state"],
            "transactions": state["transactions"],
        }
        if version == JOURNAL_SCHEMA_VERSION:
            core.update(
                {
                    "controller_state": state["controller_state"],
                    "planner_state": state["planner_state"],
                    "replacement_state": state["replacement_state"],
                    "audit_history": state["audit_history"],
                }
            )
        return core
    except KeyError as err:
        raise CommandValidationError(f"control journal is missing {err.args[0]}") from err


def _checksum(value: Mapping[str, Any]) -> str:
    try:
        payload = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as err:
        raise CommandValidationError("control journal contains non-JSON state") from err
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "ControlJournal",
    "JOURNAL_BACKUP_LIMIT",
    "JOURNAL_AUDIT_LIMIT",
    "JOURNAL_TRANSACTION_LIMIT",
    "JOURNAL_SCHEMA_VERSION",
    "JournalPhase",
    "JournalTargetIdentity",
    "JournalTransaction",
]
