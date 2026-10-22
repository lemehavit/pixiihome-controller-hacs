from __future__ import annotations

import asyncio
import ssl
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from homeassistant.core import CoreState

import custom_components.pixiihome_controller as integration_module
import custom_components.pixiihome_controller.command as command_module
import custom_components.pixiihome_controller.idle_phase_controller as idle_phase_module
import custom_components.pixiihome_controller.meter_limit_controller as meter_limit_module
import custom_components.pixiihome_controller.mqtt as mqtt_module
import custom_components.pixiihome_controller.runtime as runtime_module
from custom_components.pixiihome_controller import (
    _async_options_updated,
    _scheduler_options_signature,
)
from custom_components.pixiihome_controller.calendar import (
    async_setup_entry as async_calendar_setup_entry,
)
from custom_components.pixiihome_controller.const import (
    CONF_COMMAND_STATE,
    CONF_CONTROL_JOURNAL_INITIALIZED,
    CONF_SCHEDULER_BOOTSTRAP_ID,
    CONF_SCHEDULER_CA_COMPATIBILITY,
    CONF_SCHEDULER_CA_FILENAME,
    CONF_SCHEDULER_ENABLED,
    CONF_SCHEDULER_OWNERSHIP_STATE,
    CONF_SCHEDULER_PASSWORD,
    CONF_SCHEDULER_TIMEOUT,
    CONF_SCHEDULER_URL,
    CONF_SCHEDULER_USERNAME,
    DOMAIN,
)
from custom_components.pixiihome_controller.control.journal import (
    ControlJournal,
    JournalPhase,
    JournalTargetIdentity,
)
from custom_components.pixiihome_controller.control.models import (
    ControlOperation,
    ReleaseState,
)
from custom_components.pixiihome_controller.runtime import RuntimeData
from custom_components.pixiihome_controller.scheduler.mutations import SchedulerCreateRequest
from custom_components.pixiihome_controller.scheduler.ownership import (
    OwnershipLifecycle,
    SchedulerOwnershipRecord,
    SchedulerOwnershipStore,
)

BOOTSTRAP_ID = "A" * 32


class NoHttpSession:
    def __init__(self) -> None:
        self.calls = 0

    def get(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        self.calls += 1
        raise AssertionError("runtime setup must not perform scheduler HTTP")


class FakeConfigEntries:
    def __init__(self) -> None:
        self.reloads: list[str] = []

    async def async_reload(self, entry_id: str) -> None:
        self.reloads.append(entry_id)


class ExecutorHass:
    def __init__(self, config_path: Path | None = None) -> None:
        config_root = config_path or Path("config")
        self.config = SimpleNamespace(path=lambda *parts: str(config_root / parts[-1]))
        self.executor_jobs: list[Any] = []

    async def async_add_executor_job(self, target: Any, *args: Any) -> Any:
        self.executor_jobs.append(target)
        return target(*args)


class MemoryStore:
    data: dict[str, dict[str, Any]] = {}

    def __init__(self, hass: Any, version: int, key: str) -> None:
        del hass, version
        self.key = key

    async def async_load(self) -> dict[str, Any] | None:
        value = self.data.get(self.key)
        return deepcopy(value) if value is not None else None

    async def async_save(self, value: dict[str, Any]) -> None:
        self.data[self.key] = deepcopy(value)

    def async_delay_save(self, value: Any, delay: float = 0) -> None:
        del delay
        self.data[self.key] = deepcopy(value())


@pytest.fixture(autouse=True)
def memory_control_store(monkeypatch: pytest.MonkeyPatch) -> None:
    MemoryStore.data.clear()
    monkeypatch.setattr(runtime_module, "Store", MemoryStore)
    monkeypatch.setattr(meter_limit_module, "Store", MemoryStore)
    monkeypatch.setattr(idle_phase_module, "Store", MemoryStore)


def _entry(*, options: dict[str, object], data: dict[str, object] | None = None) -> Any:
    entry_data: dict[str, object] = {"serial": "GATEWAY"}
    if data is not None:
        entry_data.update(data)
    return SimpleNamespace(data=entry_data, options=options, entry_id="entry-a")


def _fake_hass() -> Any:
    return SimpleNamespace()


def _executor_hass(config_path: Path | None = None) -> Any:
    return ExecutorHass(config_path)


def test_serial_only_and_disabled_entries_have_no_scheduler_manager(monkeypatch):
    session = NoHttpSession()
    monkeypatch.setattr(runtime_module, "async_get_clientsession", lambda hass: session)

    serial_only = RuntimeData(_fake_hass(), "GATEWAY", entry=_entry(options={}))
    disabled = RuntimeData(
        _fake_hass(),
        "GATEWAY",
        entry=_entry(
            options={
                CONF_SCHEDULER_ENABLED: False,
                CONF_SCHEDULER_URL: "https://old-gateway.example",
                CONF_SCHEDULER_USERNAME: "old-user",
                CONF_SCHEDULER_PASSWORD: "old-secret",
            }
        ),
    )

    assert serial_only.scheduler_manager is None
    assert serial_only.scheduler_executor is None
    assert serial_only.scheduler_mutation_manager is None
    assert serial_only.scheduler_ownership is None
    assert disabled.scheduler_manager is None
    assert disabled.scheduler_executor is None
    assert disabled.scheduler_mutation_manager is None
    assert disabled.scheduler_ownership is None
    assert session.calls == 0


@pytest.mark.asyncio
async def test_configured_runtime_builds_scheduler_manager_in_executor_without_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = NoHttpSession()
    monkeypatch.setattr(runtime_module, "async_get_clientsession", lambda hass: session)
    build_calls: list[dict[str, object]] = []

    def fake_build_scheduler_ssl_context(**kwargs: object) -> ssl.SSLContext:
        build_calls.append(kwargs)
        return ssl.create_default_context()

    monkeypatch.setattr(
        runtime_module,
        "build_scheduler_ssl_context",
        fake_build_scheduler_ssl_context,
    )
    hass = _executor_hass()
    runtime = RuntimeData(
        hass,
        "GATEWAY",
        entry=_entry(
            options={
                CONF_SCHEDULER_URL: "https://gateway.example",
                CONF_SCHEDULER_BOOTSTRAP_ID: BOOTSTRAP_ID,
                CONF_SCHEDULER_USERNAME: "operator",
                CONF_SCHEDULER_PASSWORD: "password",
                CONF_SCHEDULER_TIMEOUT: 10.0,
            }
        ),
    )

    assert runtime.scheduler_manager is None
    assert build_calls == []

    await runtime.async_initialize_scheduler()

    assert runtime.scheduler_manager is not None
    assert len(hass.executor_jobs) == 1
    assert len(build_calls) == 1
    assert build_calls[0] == {
        "pixii_ca_compatibility": False,
        "ca_file": None,
    }
    diagnostics = runtime.scheduler_manager.diagnostics()
    assert diagnostics["list_refresh_attempts"] == 0
    assert diagnostics["detail_refresh_attempts"] == 0
    assert session.calls == 0


@pytest.mark.asyncio
async def test_invalid_scheduler_tls_config_keeps_runtime_available(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = NoHttpSession()
    monkeypatch.setattr(runtime_module, "async_get_clientsession", lambda hass: session)
    hass = _executor_hass(tmp_path)
    runtime = RuntimeData(
        hass,
        "GATEWAY",
        entry=_entry(
            options={
                CONF_SCHEDULER_URL: "https://gateway.example",
                CONF_SCHEDULER_BOOTSTRAP_ID: BOOTSTRAP_ID,
                CONF_SCHEDULER_USERNAME: "operator",
                CONF_SCHEDULER_PASSWORD: "password",
                CONF_SCHEDULER_CA_COMPATIBILITY: True,
                CONF_SCHEDULER_CA_FILENAME: "missing-ca.crt",
            }
        ),
    )

    await runtime.async_initialize_scheduler()

    assert runtime.scheduler_manager is None
    assert runtime.scheduler_mutation_manager is None
    assert runtime.scheduler_executor is None
    assert runtime.scheduler_ownership is None
    assert runtime.scheduler_config_error == "ca_file_missing"


@pytest.mark.skipif(
    not hasattr(ssl, "VERIFY_X509_STRICT"),
    reason="runtime has no X509 strict verification flag",
)
@pytest.mark.asyncio
async def test_pixii_compatibility_diagnostics_follow_executor_tls_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    session = NoHttpSession()
    monkeypatch.setattr(runtime_module, "async_get_clientsession", lambda hass: session)

    def fake_build_scheduler_ssl_context(**kwargs: object) -> ssl.SSLContext:
        del kwargs
        context = ssl.create_default_context()
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT
        return context

    monkeypatch.setattr(
        runtime_module,
        "build_scheduler_ssl_context",
        fake_build_scheduler_ssl_context,
    )
    hass = _executor_hass(tmp_path)
    runtime = RuntimeData(
        hass,
        "GATEWAY",
        entry=_entry(
            options={
                CONF_SCHEDULER_URL: "https://gateway.example",
                CONF_SCHEDULER_BOOTSTRAP_ID: BOOTSTRAP_ID,
                CONF_SCHEDULER_USERNAME: "operator",
                CONF_SCHEDULER_PASSWORD: "password",
                CONF_SCHEDULER_CA_COMPATIBILITY: True,
                CONF_SCHEDULER_CA_FILENAME: "sanitized-ca.crt",
            }
        ),
    )

    await runtime.async_initialize_scheduler()

    assert runtime.scheduler_tls_state == {
        "tls_mode": "pixii_ca_compatibility",
        "custom_ca_configured": True,
        "x509_strict_enabled": False,
        "cert_required": True,
        "hostname_verification": True,
    }
    assert runtime.scheduler_manager is not None
    assert session.calls == 0


@pytest.mark.asyncio
async def test_calendar_setup_receives_initialized_scheduler_manager(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = NoHttpSession()
    monkeypatch.setattr(runtime_module, "async_get_clientsession", lambda hass: session)

    def fake_build_scheduler_ssl_context(**kwargs: object) -> ssl.SSLContext:
        del kwargs
        return ssl.create_default_context()

    monkeypatch.setattr(
        runtime_module,
        "build_scheduler_ssl_context",
        fake_build_scheduler_ssl_context,
    )
    hass = _executor_hass()
    entry = _entry(
        options={
            CONF_SCHEDULER_URL: "https://gateway.example",
            CONF_SCHEDULER_BOOTSTRAP_ID: BOOTSTRAP_ID,
            CONF_SCHEDULER_USERNAME: "operator",
            CONF_SCHEDULER_PASSWORD: "password",
        }
    )
    runtime = RuntimeData(hass, "GATEWAY", entry=entry)
    await runtime.async_initialize_scheduler()
    added: list[Any] = []

    def add_entities(new_entities: Any, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        added.extend(new_entities)

    await async_calendar_setup_entry(
        cast(Any, SimpleNamespace(data={DOMAIN: {entry.entry_id: runtime}})),
        cast(Any, entry),
        add_entities,
    )

    assert len(added) == 1
    assert runtime.scheduler_manager is not None
    assert session.calls == 0


@pytest.mark.asyncio
async def test_integration_setup_initializes_scheduler_without_scheduler_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = NoHttpSession()
    monkeypatch.setattr(integration_module, "remove_retired_entities", lambda *args: None)
    monkeypatch.setattr(runtime_module, "async_get_clientsession", lambda hass: session)

    def fake_build_scheduler_ssl_context(**kwargs: object) -> ssl.SSLContext:
        del kwargs
        return ssl.create_default_context()

    monkeypatch.setattr(
        runtime_module,
        "build_scheduler_ssl_context",
        fake_build_scheduler_ssl_context,
    )

    async def mqtt_ready(hass: Any) -> bool:
        del hass
        return True

    monkeypatch.setattr(integration_module, "async_wait_for_mqtt_client", mqtt_ready)

    initialized: list[Any] = []
    original_initialize = integration_module.RuntimeData.async_initialize_scheduler

    async def tracked_initialize(runtime: Any) -> None:
        initialized.append(runtime)
        await original_initialize(runtime)

    monkeypatch.setattr(
        integration_module.RuntimeData,
        "async_initialize_scheduler",
        tracked_initialize,
    )

    async def fake_start(runtime: Any) -> None:
        del runtime

    monkeypatch.setattr(integration_module.RuntimeData, "start", fake_start)
    hass = _executor_hass()
    hass.data = {}
    forwarded: list[Any] = []

    async def forward_entry_setups(entry: Any, platforms: Any) -> None:
        forwarded.append((entry, platforms))

    def update_entry(entry: Any, *, data: dict[str, object]) -> None:
        entry.data = data

    hass.config_entries = SimpleNamespace(
        async_forward_entry_setups=forward_entry_setups,
        async_update_entry=update_entry,
    )
    entry = _entry(
        options={
            CONF_SCHEDULER_URL: "https://gateway.example",
            CONF_SCHEDULER_BOOTSTRAP_ID: BOOTSTRAP_ID,
            CONF_SCHEDULER_USERNAME: "operator",
            CONF_SCHEDULER_PASSWORD: "password",
        }
    )
    entry.async_on_unload = lambda callback: None
    entry.add_update_listener = lambda callback: callback

    assert await integration_module.async_setup_entry(hass, entry) is True

    assert len(initialized) == 1
    assert initialized[0].scheduler_manager is not None
    assert len(hass.executor_jobs) == 1
    assert session.calls == 0
    assert forwarded


@pytest.mark.asyncio
async def test_changed_scheduler_options_reload_but_identical_entry_updates_do_not():
    config_entries = FakeConfigEntries()
    entry = _entry(
        options={
            CONF_SCHEDULER_URL: "https://gateway.example",
            CONF_SCHEDULER_BOOTSTRAP_ID: BOOTSTRAP_ID,
            CONF_SCHEDULER_USERNAME: "operator",
            CONF_SCHEDULER_PASSWORD: "password",
            CONF_SCHEDULER_TIMEOUT: 10.0,
        }
    )
    runtime = SimpleNamespace(scheduler_options_signature=(None, None, None, None, None))
    hass = _fake_hass()
    hass.data = {DOMAIN: {entry.entry_id: runtime}}
    hass.config_entries = config_entries

    await _async_options_updated(hass, entry)
    assert config_entries.reloads == [entry.entry_id]

    runtime.scheduler_options_signature = _scheduler_options_signature(entry)
    await _async_options_updated(hass, entry)
    assert config_entries.reloads == [entry.entry_id]


def _ownership_state(
    lifecycle: OwnershipLifecycle = OwnershipLifecycle.PROVISIONAL,
) -> dict[str, object]:
    store = SchedulerOwnershipStore(config_entry_id="entry-a", gateway_serial="GATEWAY")
    store.register(
        SchedulerOwnershipRecord(
            config_entry_id="entry-a",
            gateway_serial="GATEWAY",
            m_id=240000001,
            service="targetsoc",
            intended_state={
                "socref": 90,
                "starttime": "2030-01-01T00:00:00+00:00",
                "duration": 3600,
            },
            lifecycle=lifecycle,
        )
    )
    return store.snapshot()


async def _configured_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    data: dict[str, object] | None = None,
) -> tuple[Any, Any, Any, list[dict[str, object]]]:
    session = NoHttpSession()
    monkeypatch.setattr(runtime_module, "async_get_clientsession", lambda hass: session)
    monkeypatch.setattr(
        runtime_module,
        "build_scheduler_ssl_context",
        lambda **kwargs: ssl.create_default_context(),
    )
    hass = _executor_hass()
    updates: list[dict[str, object]] = []

    def update_entry(entry: Any, *, data: dict[str, object]) -> None:
        entry.data = data
        updates.append(data)

    hass.config_entries = SimpleNamespace(async_update_entry=update_entry)
    entry = _entry(
        options={
            CONF_SCHEDULER_URL: "https://gateway.example",
            CONF_SCHEDULER_BOOTSTRAP_ID: BOOTSTRAP_ID,
            CONF_SCHEDULER_USERNAME: "operator",
            CONF_SCHEDULER_PASSWORD: "password",
        },
        data=data,
    )
    runtime = RuntimeData(hass, "GATEWAY", entry=entry)
    await runtime.async_initialize_control_state()
    await runtime.async_initialize_scheduler()
    return runtime, entry, hass, updates


@pytest.mark.asyncio
async def test_configured_runtime_wires_without_legacy_ha_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _, hass, _ = await _configured_runtime(monkeypatch)
    assert not hasattr(hass, "states")

    assert runtime.scheduler_manager is not None
    assert runtime.scheduler_ownership is not None
    assert runtime.scheduler_executor is not None
    assert runtime.scheduler_mutation_manager is not None
    assert runtime.scheduler_mutation_manager.allocator is runtime.command_manager.allocator
    assert runtime.scheduler_executor.command_manager is runtime.command_manager
    assert runtime.scheduler_ownership.records == {}
    assert runtime.scheduler_mutation_manager.diagnostics_snapshot()["create_attempts"] == 0


@pytest.mark.asyncio
async def test_scheduler_ownership_persistence_preserves_allocator_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ownership_state = _ownership_state(OwnershipLifecycle.OWNED)
    runtime, entry, _, updates = await _configured_runtime(
        monkeypatch,
        data={
            CONF_COMMAND_STATE: {"next_m_id": 240000010, "owned_m_ids": [240000001]},
            CONF_SCHEDULER_OWNERSHIP_STATE: ownership_state,
            "scheduler_url_extra": "preserve",
        },
    )

    runtime.command_manager.allocator.allocate(persistent=True)
    await runtime.async_flush_control_journal()
    assert updates[-1][CONF_CONTROL_JOURNAL_INITIALIZED] is True
    assert CONF_COMMAND_STATE not in updates[-1]
    assert CONF_SCHEDULER_OWNERSHIP_STATE not in updates[-1]
    assert updates[-1]["scheduler_url_extra"] == "preserve"

    primary_key = f"{DOMAIN}.entry-a.control_journal"
    persisted = ControlJournal.from_state(
        MemoryStore.data[primary_key],
        config_entry_id="entry-a",
        gateway_serial="GATEWAY",
    )
    assert persisted.command_state == runtime.command_manager.allocator.snapshot()
    assert persisted.ownership_state == ownership_state

    replacement = dict(ownership_state)
    await runtime._persist_scheduler_ownership(replacement)
    persisted = ControlJournal.from_state(
        MemoryStore.data[primary_key],
        config_entry_id="entry-a",
        gateway_serial="GATEWAY",
    )
    assert persisted.command_state == runtime.command_manager.allocator.snapshot()
    assert persisted.ownership_state == replacement
    assert CONF_COMMAND_STATE not in entry.data
    assert CONF_SCHEDULER_OWNERSHIP_STATE not in entry.data

    restarted = RuntimeData(runtime.hass, "GATEWAY", entry=entry)
    await restarted.async_initialize_control_state()
    assert restarted.command_manager.allocator.snapshot() == persisted.command_state


@pytest.mark.asyncio
async def test_legacy_state_is_preserved_when_journal_commit_is_not_durable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DroppingStore(MemoryStore):
        async def async_save(self, value: dict[str, Any]) -> None:
            del value

    monkeypatch.setattr(runtime_module, "Store", DroppingStore)
    legacy_command = {"next_m_id": 240000010, "owned_m_ids": [240000001]}
    legacy_ownership = _ownership_state(OwnershipLifecycle.OWNED)
    entry = _entry(
        options={},
        data={
            CONF_COMMAND_STATE: legacy_command,
            CONF_SCHEDULER_OWNERSHIP_STATE: legacy_ownership,
        },
    )
    updates: list[dict[str, object]] = []
    hass = _executor_hass()
    hass.config_entries = SimpleNamespace(
        async_update_entry=lambda entry, *, data: updates.append(data)
    )
    runtime = RuntimeData(hass, "GATEWAY", entry=entry)

    await runtime.async_initialize_control_state()

    assert runtime.control_state_error == "control_journal_invalid"
    assert entry.data[CONF_COMMAND_STATE] == legacy_command
    assert entry.data[CONF_SCHEDULER_OWNERSHIP_STATE] == legacy_ownership
    assert CONF_CONTROL_JOURNAL_INITIALIZED not in entry.data
    assert updates == []


@pytest.mark.asyncio
async def test_initialized_missing_primary_and_backup_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _entry(
        options={},
        data={CONF_CONTROL_JOURNAL_INITIALIZED: True},
    )
    hass = _executor_hass()
    hass.config_entries = SimpleNamespace(async_update_entry=lambda *args, **kwargs: None)
    runtime = RuntimeData(hass, "GATEWAY", entry=entry)

    await runtime.async_initialize_control_state()

    assert runtime.control_journal is None
    assert runtime.control_state_error == "control_journal_invalid"
    assert runtime.command_manager.allocator.owned_ids == set()


@pytest.mark.asyncio
async def test_independent_backup_recovers_missing_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = ControlJournal.migrate_legacy(
        config_entry_id="entry-a",
        gateway_serial="GATEWAY",
        command_state={"next_m_id": 240000010, "owned_m_ids": [240000001]},
        ownership_state=_ownership_state(OwnershipLifecycle.OWNED),
    )
    journal = journal.with_product_state(
        controller_state={
            "production_control_authority": {"enabled": True, "revision": 1},
        }
    )
    backup_key = f"{DOMAIN}.entry-a.control_journal_backup"
    primary_key = f"{DOMAIN}.entry-a.control_journal"
    MemoryStore.data[backup_key] = journal.snapshot()
    entry = _entry(
        options={},
        data={CONF_CONTROL_JOURNAL_INITIALIZED: True},
    )
    hass = _executor_hass()
    hass.config_entries = SimpleNamespace(async_update_entry=lambda *args, **kwargs: None)
    runtime = RuntimeData(hass, "GATEWAY", entry=entry)

    await runtime.async_initialize_control_state()

    assert runtime.control_state_error is None
    assert runtime.production_control_enabled() is False
    assert runtime.release_gate.live_writes_enabled is False
    assert runtime.control_journal is not None
    assert runtime.command_manager.allocator.owned_ids == {240000001}
    assert MemoryStore.data[primary_key] == runtime.control_journal.snapshot()


@pytest.mark.asyncio
async def test_independent_backup_recovers_invalid_primary() -> None:
    journal = ControlJournal.migrate_legacy(
        config_entry_id="entry-a",
        gateway_serial="GATEWAY",
        command_state={"next_m_id": 240000010, "owned_m_ids": [240000001]},
        ownership_state=_ownership_state(OwnershipLifecycle.OWNED),
    )
    primary_key = f"{DOMAIN}.entry-a.control_journal"
    backup_key = f"{DOMAIN}.entry-a.control_journal_backup"
    corrupted = journal.snapshot()
    corrupted["checksum"] = "invalid"
    MemoryStore.data[primary_key] = corrupted
    MemoryStore.data[backup_key] = journal.snapshot()
    entry = _entry(
        options={},
        data={CONF_CONTROL_JOURNAL_INITIALIZED: True},
    )
    hass = _executor_hass()
    hass.config_entries = SimpleNamespace(async_update_entry=lambda *args, **kwargs: None)
    runtime = RuntimeData(hass, "GATEWAY", entry=entry)

    await runtime.async_initialize_control_state()

    assert runtime.control_state_error is None
    assert runtime.control_journal is not None
    assert runtime.command_manager.allocator.owned_ids == {240000001}
    assert MemoryStore.data[primary_key] == runtime.control_journal.snapshot()


@pytest.mark.asyncio
async def test_independent_backup_precedes_embedded_primary_rollback() -> None:
    old = ControlJournal.migrate_legacy(
        config_entry_id="entry-a",
        gateway_serial="GATEWAY",
        command_state={"next_m_id": 240000010, "owned_m_ids": [240000001]},
        ownership_state=_ownership_state(OwnershipLifecycle.OWNED),
    )
    newest = old.begin_transaction(
        transaction_id="newest-prepared",
        operation=ControlOperation.UPDATE.value,
        target_identity=JournalTargetIdentity(
            m_id=240000001,
            sch_index=12,
            service_no=110,
            source_id=7,
            asset_id="battery-1",
        ),
        intent={"duration_seconds": 1800, "socref": 70},
    )
    primary_key = f"{DOMAIN}.entry-a.control_journal"
    backup_key = f"{DOMAIN}.entry-a.control_journal_backup"
    corrupted_primary = newest.snapshot()
    corrupted_primary["checksum"] = "invalid"
    MemoryStore.data[primary_key] = corrupted_primary
    MemoryStore.data[backup_key] = newest.snapshot()
    entry = _entry(
        options={},
        data={CONF_CONTROL_JOURNAL_INITIALIZED: True},
    )
    hass = _executor_hass()
    hass.config_entries = SimpleNamespace(async_update_entry=lambda *args, **kwargs: None)
    runtime = RuntimeData(hass, "GATEWAY", entry=entry)

    await runtime.async_initialize_control_state()

    assert runtime.control_state_error is None
    assert runtime.control_journal is not None
    transaction = runtime.control_journal.transaction("newest-prepared")
    assert transaction is not None
    assert transaction.phase is JournalPhase.RECOVERY_REQUIRED
    assert transaction.reason == "restart_recovery_required"
    assert MemoryStore.data[primary_key] == runtime.control_journal.snapshot()


@pytest.mark.asyncio
async def test_journal_commit_is_rejected_during_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _, hass, _ = await _configured_runtime(monkeypatch)
    primary_key = f"{DOMAIN}.entry-a.control_journal"
    before = deepcopy(MemoryStore.data[primary_key])
    hass.state = CoreState.stopping

    with pytest.raises(RuntimeError, match="shutdown"):
        await runtime._persist_scheduler_ownership(
            runtime.scheduler_ownership.snapshot()
            if runtime.scheduler_ownership is not None
            else {}
        )

    assert MemoryStore.data[primary_key] == before


@pytest.mark.parametrize(
    "interrupted_phase",
    [
        JournalPhase.PREPARED,
        JournalPhase.COMMAND_SENT,
        JournalPhase.ACKNOWLEDGED,
    ],
)
@pytest.mark.asyncio
async def test_runtime_transaction_journal_survives_restart(
    monkeypatch: pytest.MonkeyPatch,
    interrupted_phase: JournalPhase,
) -> None:
    runtime, entry, _, _ = await _configured_runtime(monkeypatch)
    identity = JournalTargetIdentity(
        m_id=240000001,
        sch_index=4,
        service_no=110,
        source_id=42,
        asset_id="battery-a",
    )
    await runtime.begin_control_transaction(
        transaction_id="tx-1",
        operation="create",
        target_identity=identity,
    )
    if interrupted_phase in {JournalPhase.COMMAND_SENT, JournalPhase.ACKNOWLEDGED}:
        await runtime.transition_control_transaction("tx-1", JournalPhase.COMMAND_SENT)
    if interrupted_phase is JournalPhase.ACKNOWLEDGED:
        await runtime.transition_control_transaction("tx-1", JournalPhase.ACKNOWLEDGED)

    restarted = RuntimeData(runtime.hass, "GATEWAY", entry=entry)
    await restarted.async_initialize_control_state()

    assert restarted.control_journal is not None
    unresolved = restarted.control_journal.unresolved_transactions
    assert len(unresolved) == 1
    assert unresolved[0].target_identity == identity
    assert unresolved[0].phase is JournalPhase.RECOVERY_REQUIRED
    await restarted.transition_control_transaction("tx-1", JournalPhase.RECONCILED)
    assert restarted.control_journal.unresolved_transactions == ()


@pytest.mark.asyncio
async def test_concurrent_journal_commits_preserve_both_transactions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _, _, _ = await _configured_runtime(monkeypatch)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_save = RuntimeData._save_control_journal_verified
    delayed = True

    async def delayed_save(self: RuntimeData, journal: ControlJournal) -> None:
        nonlocal delayed
        if self is runtime and delayed:
            delayed = False
            entered.set()
            await release.wait()
        await original_save(self, journal)

    monkeypatch.setattr(RuntimeData, "_save_control_journal_verified", delayed_save)
    first = asyncio.create_task(
        runtime.begin_control_transaction(
            transaction_id="tx-1",
            operation="create",
            target_identity=JournalTargetIdentity(m_id=240000001, source_id=1),
        )
    )
    await entered.wait()
    allocated = runtime.command_manager.allocator.allocate(persistent=True)
    second = asyncio.create_task(
        runtime.begin_control_transaction(
            transaction_id="tx-2",
            operation="update",
            target_identity=JournalTargetIdentity(m_id=240000002, source_id=2),
        )
    )
    await asyncio.sleep(0)
    assert not second.done()

    release.set()
    await asyncio.gather(first, second)
    await runtime.async_flush_control_journal()

    assert runtime.control_journal is not None
    assert {item.transaction_id for item in runtime.control_journal.transactions} == {
        "tx-1",
        "tx-2",
    }
    persisted = ControlJournal.from_state(
        MemoryStore.data[f"{DOMAIN}.entry-a.control_journal"],
        config_entry_id="entry-a",
        gateway_serial="GATEWAY",
    )
    assert {item.transaction_id for item in persisted.transactions} == {"tx-1", "tx-2"}
    assert persisted.command_state == runtime.command_manager.allocator.snapshot()
    assert allocated in persisted.command_state["owned_m_ids"]


@pytest.mark.asyncio
async def test_atomic_recovery_preserves_newer_allocator_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _, _, _ = await _configured_runtime(monkeypatch)
    allocator = runtime.command_manager.allocator
    target_m_id = allocator.allocate(persistent=True)
    assert runtime.scheduler_ownership is not None
    await runtime.async_flush_control_journal()
    await runtime.begin_control_transaction(
        transaction_id="tx-recovery",
        operation="create",
        target_identity=JournalTargetIdentity(m_id=target_m_id),
    )
    await runtime.transition_control_transaction(
        "tx-recovery",
        JournalPhase.RECOVERY_REQUIRED,
    )
    desired_state = allocator.snapshot()
    desired_state["owned_m_ids"] = [
        value for value in desired_state["owned_m_ids"] if value != target_m_id
    ]

    await runtime._control_journal_lock.acquire()
    recovery = asyncio.create_task(
        runtime.reconcile_control_transaction(
            "tx-recovery",
            command_state=desired_state,
            ownership_state=runtime.scheduler_ownership.snapshot(),
        )
    )
    await asyncio.sleep(0)
    newer_m_id = allocator.allocate(persistent=True)
    runtime._control_journal_lock.release()

    await recovery
    await runtime.async_flush_control_journal()

    assert runtime.control_journal is not None
    assert target_m_id not in runtime.control_journal.command_state["owned_m_ids"]
    transaction = runtime.control_journal.transaction("tx-recovery")
    assert transaction is not None
    assert transaction.phase is JournalPhase.RECONCILED
    assert newer_m_id in runtime.control_journal.command_state["owned_m_ids"]
    assert runtime.control_journal.command_state == allocator.snapshot()


@pytest.mark.asyncio
async def test_invalid_scheduler_ownership_fails_closed_without_disabling_monitoring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _, _, updates = await _configured_runtime(
        monkeypatch,
        data={
            CONF_SCHEDULER_OWNERSHIP_STATE: {
                "config_entry_id": "other-entry",
                "gateway_serial": "GATEWAY",
                "records": [],
            }
        },
    )

    assert runtime.scheduler_manager is not None
    assert runtime.control_state_error == "control_journal_invalid"
    assert runtime.scheduler_mutation_config_error == "control_journal_invalid"
    assert runtime.scheduler_ownership is None
    assert runtime.scheduler_executor is None
    assert runtime.scheduler_mutation_manager is None
    assert updates == []


@pytest.mark.asyncio
async def test_provisional_ownership_restore_is_passive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _, _, updates = await _configured_runtime(
        monkeypatch,
        data={CONF_SCHEDULER_OWNERSHIP_STATE: _ownership_state()},
    )

    assert runtime.scheduler_ownership is not None
    assert (
        runtime.scheduler_ownership.records[240000001].lifecycle is OwnershipLifecycle.PROVISIONAL
    )
    assert runtime.scheduler_mutation_manager is not None
    assert runtime.scheduler_mutation_manager.diagnostics_snapshot() == {
        "create_attempts": 0,
        "update_attempts": 0,
        "delete_attempts": 0,
        "recovery_attempts": 0,
        "blocked_transactions": 0,
        "mutation_executor_calls": 0,
        "keyed_get_calls": 0,
        "successful_reconciliations": 0,
        "conflicted_transactions": 0,
        "persistence_failures": 0,
        "last_result": None,
    }
    assert updates[-1][CONF_CONTROL_JOURNAL_INITIALIZED] is True


@pytest.mark.asyncio
async def test_configured_runtime_start_has_one_ack_subscription_and_no_scheduler_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subscriptions: list[tuple[str, str | None]] = []

    async def wait_for_mqtt(hass: Any) -> bool:
        del hass
        return True

    async def subscribe(
        hass: Any,
        topic: str,
        callback: Any,
        *,
        qos: int,
        encoding: str | None,
    ) -> Any:
        del hass, callback, qos
        subscriptions.append((topic, encoding))
        return lambda: None

    monkeypatch.setattr(mqtt_module, "async_wait_for_mqtt_client", wait_for_mqtt)
    monkeypatch.setattr(command_module, "async_wait_for_mqtt_client", wait_for_mqtt)
    monkeypatch.setattr(mqtt_module, "async_subscribe", subscribe)
    monkeypatch.setattr(command_module, "async_subscribe", subscribe)
    runtime, _, _, _ = await _configured_runtime(
        monkeypatch,
        data={CONF_SCHEDULER_OWNERSHIP_STATE: _ownership_state()},
    )

    await runtime.start()
    assert subscriptions.count(("pixii/ack/GATEWAY", None)) == 1
    assert not any(topic.endswith("/control") for topic, _ in subscriptions)
    assert all(encoding is None for _, encoding in subscriptions)
    assert runtime.command_manager.ready
    assert runtime.scheduler_manager is not None
    assert runtime.scheduler_mutation_manager is not None
    assert runtime.scheduler_mutation_manager.diagnostics_snapshot()["recovery_attempts"] == 0
    assert runtime.scheduler_ownership is not None
    assert (
        runtime.scheduler_ownership.records[240000001].lifecycle is OwnershipLifecycle.PROVISIONAL
    )
    assert runtime.scheduler_mutation_manager.diagnostics_snapshot()["mutation_executor_calls"] == 0
    assert runtime.command_manager.diagnostics()["scheduler_mutation_attempts"] == 0
    await runtime.stop()


@pytest.mark.asyncio
async def test_scheduler_control_context_is_pure_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _, _, _ = await _configured_runtime(monkeypatch)

    context = runtime.scheduler_control_context(ControlOperation.CREATE)
    assert context.release_state is ReleaseState.LIVE_RELEASE_CLOSED
    assert context.ownership_store_valid is True
    assert context.plant_health.healthy is False
    assert context.ownership_ambiguous is False
    assert context.mqtt_connected is False
    assert context.plant_health.system_safe is False
    assert context.plant_health.battery_safe is False
    assert context.plant_health.grid_safe is False


@pytest.mark.asyncio
async def test_direct_runtime_manager_create_remains_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _, _, _ = await _configured_runtime(monkeypatch)
    assert runtime.scheduler_mutation_manager is not None
    assert runtime.scheduler_executor is not None

    result = await runtime.scheduler_mutation_manager.create(
        SchedulerCreateRequest(
            starttime=datetime(2030, 1, 1, tzinfo=UTC),
            duration_seconds=3600,
            values={"socref": 90},
        )
    )

    assert result.status.value == "blocked"
    assert runtime.command_manager.allocator.owned_ids == set()
    assert runtime.scheduler_ownership is not None
    assert runtime.scheduler_ownership.records == {}
    diagnostics = runtime.scheduler_mutation_diagnostics()
    transaction = diagnostics["transaction_manager"]
    assert isinstance(transaction, dict)
    assert transaction["last_result"] is None
    assert diagnostics["validation"]["attempt_consumed"] is False
    assert "m_id" not in str(diagnostics)


@pytest.mark.asyncio
async def test_scheduler_options_reload_restores_both_persistent_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = {
        CONF_COMMAND_STATE: {"next_m_id": 240000010, "owned_m_ids": [240000001]},
        CONF_SCHEDULER_OWNERSHIP_STATE: _ownership_state(OwnershipLifecycle.OWNED),
    }
    first, entry, _, _ = await _configured_runtime(monkeypatch, data=data)
    assert first.scheduler_mutation_manager is not None

    entry.options[CONF_SCHEDULER_TIMEOUT] = 10.0
    second = RuntimeData(first.hass, "GATEWAY", entry=entry)
    await second.async_initialize_control_state()
    await second.async_initialize_scheduler()

    assert second.scheduler_mutation_manager is not None
    assert second.command_manager.allocator is not first.command_manager.allocator
    assert second.command_manager.allocator.owned_ids == {240000001}
    assert second.scheduler_ownership is not None
    assert second.scheduler_ownership.records[240000001].lifecycle is OwnershipLifecycle.OWNED
    assert second.scheduler_mutation_manager.diagnostics_snapshot()["recovery_attempts"] == 0


@pytest.mark.asyncio
async def test_switches_held_off_by_an_unfinished_command_return_once_it_is_settled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, entry, _, _ = await _configured_runtime(monkeypatch)
    journal = runtime.control_journal
    assert journal is not None
    on = {
        "automatic_solar_charging_enabled": False,
        "adaptive_peak_shaving_enabled": False,
        "load_balancing_enabled": False,
        "automatic_price_optimization_enabled": True,
    }
    runtime.control_journal = journal.with_product_state(controller_state={
        **journal.controller_state,
        "production_control_authority": {"enabled": True, "revision": 3},
        "controller_state": on,
    })
    await runtime._save_control_journal_verified(runtime.control_journal)
    await runtime.begin_control_transaction(
        transaction_id="tx-1", operation="delete",
        target_identity=JournalTargetIdentity(m_id=240000001),
    )
    await runtime.transition_control_transaction("tx-1", JournalPhase.COMMAND_SENT)

    restarted = RuntimeData(runtime.hass, "GATEWAY", entry=entry)
    await restarted.async_initialize_control_state()
    # Held off while the outcome is unknown, and nothing resumes early.
    assert restarted.production_control_enabled() is False
    assert restarted.controller_state.automatic_price_optimization_enabled is False
    assert await restarted.async_resume_after_recovery() is False

    # A second restart before settling still remembers the switches.
    again = RuntimeData(runtime.hass, "GATEWAY", entry=entry)
    await again.async_initialize_control_state()
    await again.transition_control_transaction("tx-1", JournalPhase.RECONCILED)
    # Each switch comes back through its normal path and its checks.
    switched: list[tuple[str, bool]] = []

    async def set_enabled(self: RuntimeData, key: str, enabled: bool) -> None:
        assert self.production_control_enabled() is True
        switched.append((key, enabled))

    monkeypatch.setattr(RuntimeData, "_async_set_controller_enabled_locked", set_enabled)

    assert await again.async_resume_after_recovery() is True
    assert again.production_control_enabled() is True
    assert again.release_gate.live_writes_enabled is True
    assert switched == [("automatic_price_optimization_enabled", True)]
    assert again.control_journal is not None
    assert "resume_after_recovery" not in again.control_journal.controller_state
    assert await again.async_resume_after_recovery() is False


@pytest.mark.asyncio
async def test_switches_are_not_remembered_when_authority_was_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, entry, _, _ = await _configured_runtime(monkeypatch)
    await runtime.begin_control_transaction(
        transaction_id="tx-1", operation="delete",
        target_identity=JournalTargetIdentity(m_id=240000001),
    )
    restarted = RuntimeData(runtime.hass, "GATEWAY", entry=entry)
    await restarted.async_initialize_control_state()
    await restarted.transition_control_transaction("tx-1", JournalPhase.RECONCILED)

    assert await restarted.async_resume_after_recovery() is False
    assert restarted.production_control_enabled() is False
