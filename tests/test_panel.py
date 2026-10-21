from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
import voluptuous as vol

from custom_components.pixiihome_controller import panel as panel_module
from custom_components.pixiihome_controller import simulator_entry as simulator_entry_module
from custom_components.pixiihome_controller.const import DOMAIN
from custom_components.pixiihome_controller.control.journal import (
    ControlJournal,
    JournalTargetIdentity,
)
from custom_components.pixiihome_controller.control.models import (
    ControlOperation,
    ControlPolicyInput,
    OperationImpact,
    OperatorMode,
    PlantHealth,
    ReleaseState,
)
from custom_components.pixiihome_controller.panel import (
    apply_simulator_preview,
    async_panel_state,
    panel_state,
    preview_operation,
)
from custom_components.pixiihome_controller.runtime import RuntimeData
from custom_components.pixiihome_controller.scheduler.models import (
    NormalizedRecurrence,
    NormalizedSchedule,
    Ownership,
    ScheduleCompleteness,
    ScheduleConsistency,
    SchedulerOwnership,
)
from custom_components.pixiihome_controller.simulator_entry import (
    SimulatorEntryRuntime,
)


def _creation_runtime() -> Any:
    runtime = cast(Any, PanelRuntime())
    context = runtime.scheduler_control_context(ControlOperation.CREATE)
    runtime.scheduler_control_context = lambda op: replace(
        context, operation=op, release_state=ReleaseState.LIVE_RELEASE_OPEN,
    )
    runtime.production_control_enabled = lambda: True
    runtime.production_authority = SimpleNamespace(revision=1)
    runtime._production_previews = {}
    runtime._require_admin = AsyncMock()
    runtime.scheduler_manager.list_visibility_diagnostics = lambda: {
        "list_visibility_fresh": True, "from_epoch": 1_893_455_000, "to_epoch": 1_893_470_000,
    }
    created = SimpleNamespace(
        status=SimpleNamespace(value="success"), operation=SimpleNamespace(value="create"),
        m_id=240000002, reason="readback_verified", blocked_reasons=(),
    )
    runtime.scheduler_mutation_manager = SimpleNamespace(create=AsyncMock(return_value=created))
    return runtime


@pytest.mark.asyncio
async def test_production_create_uses_typed_transaction_and_exact_preview() -> None:
    runtime = _creation_runtime()
    preview = preview_operation(runtime, operation=ControlOperation.CREATE, service="targetsoc",
        target={}, intent={"start_epoch": 1_893_460_000, "duration_seconds": 900, "socref": 75})
    assert preview["can_apply"]
    assert runtime.scheduler_mutation_manager.create.await_count == 0
    runtime._production_previews[preview["preview_id"]].update(user_id="admin", expires_at=4e9)
    result = await RuntimeData.async_apply_production_preview(
        runtime, preview["preview_id"], "admin",
    )
    assert result["status"] == "success"
    request = runtime.scheduler_mutation_manager.create.await_args.args[0]
    assert request.starttime.timestamp() == 1_893_460_000
    assert request.duration_seconds == 900 and request.values == {"socref": 75}
    with pytest.raises(Exception, match="preview_missing_or_consumed"):
        await RuntimeData.async_apply_production_preview(runtime, preview["preview_id"], "admin")


@pytest.mark.asyncio
@pytest.mark.parametrize(("user", "expiry"), [("other-admin", 4e9), ("admin", 0)])
async def test_production_create_rejects_wrong_user_or_expired_preview(
    user: str, expiry: float,
) -> None:
    runtime = _creation_runtime()
    runtime._production_previews["token"] = {"user_id": "admin", "expires_at": expiry}
    with pytest.raises(Exception, match="preview_missing_or_consumed"):
        await RuntimeData.async_apply_production_preview(runtime, "token", user)
    assert runtime.scheduler_mutation_manager.create.await_count == 0


def test_create_overlap_is_blocked_and_recurrence_survives_snapshot() -> None:
    runtime = _creation_runtime()
    schedule = runtime.scheduler_manager.normalized_schedules[0]
    runtime.scheduler_manager.normalized_schedules = (replace(schedule,
        recurrence=NormalizedRecurrence(True, (("rec_type", 2), ("recurrence1", 3)))),)
    preview = preview_operation(runtime, operation=ControlOperation.CREATE, service="targetsoc",
        target={},
        intent={"start_epoch": schedule.start_epoch, "duration_seconds": 900, "socref": 75},
    )
    assert "scheduler_external_conflict" in preview["blocked_reasons"]
    assert not preview["can_apply"]
    assert preview["scheduler_snapshot"][0]["recurrence"]["details"] == {
        "rec_type": 2, "recurrence1": 3,
    }


@pytest.mark.parametrize("changes", [
    {"start_epoch": 0}, {"duration_seconds": 604801}, {"socref": float("nan")},
    {"socref": True}, {"start_epoch": 2**31}, {"duration_seconds": 0},
])
def test_create_rejects_invalid_input_before_token(changes: dict[str, Any]) -> None:
    runtime = _creation_runtime()
    with pytest.raises(vol.Invalid, match="scheduler_create_values_invalid"):
        preview_operation(runtime, operation=ControlOperation.CREATE, service="targetsoc",
            target={},
            intent={"start_epoch": 1_893_460_000, "duration_seconds": 900, "socref": 75, **changes})
    assert not runtime._production_previews


class _PanelSchedulerManager:
    def __init__(self) -> None:
        self.list_calls: list[tuple[int, int]] = []
        self.detail_calls: list[int] = []
        self.normalized_schedules = (
            NormalizedSchedule(
                sch_index=12,
                start_epoch=1_893_456_000,
                duration_seconds=900,
                end_epoch=1_893_456_900,
                service_no=8,
                service_type=107,
                enabled=True,
                service_priority=None,
                asset_id=None,
                source_id=4,
                recurrence=NormalizedRecurrence(False, None),
                ownership=SchedulerOwnership(
                    ownership=Ownership.INTEGRATION_OWNED,
                    source_label="Pixii Home Controller",
                    source_confidence="durable",
                    editable=True,
                    deletable=True,
                    protected=False,
                ),
                completeness=ScheduleCompleteness.COMPLETE,
                consistency=ScheduleConsistency.CONSISTENT,
            ),
        )

    def list_visibility_diagnostics(self) -> dict[str, object]:
        return {"list_visibility_fresh": True, "list_entry_count": 1}

    async def refresh_list(self, from_epoch: int, to_epoch: int) -> tuple[Any, ...]:
        self.list_calls.append((from_epoch, to_epoch))
        return (SimpleNamespace(sch_index=12),)

    async def refresh_detail(self, sch_index: int) -> None:
        self.detail_calls.append(sch_index)



class PanelRuntime:
    def __init__(self) -> None:
        self.serial = "PX-100"
        self.entry = SimpleNamespace(entry_id="entry-a")
        self.release_gate = SimpleNamespace(
            live_writes_enabled=False,
            infrastructure_status="partially_live_validated",
            blocked_reasons={
                "release_writes_disabled",
                "state_changing_control_not_live_validated",
            },
        )
        self.control_journal = ControlJournal.empty(
            config_entry_id="entry-a",
            gateway_serial=self.serial,
        ).begin_transaction(
            transaction_id="recover-1",
            operation="update",
            target_identity=JournalTargetIdentity(sch_index=12),
        ).mark_unfinished_recovery_required()
        self.scheduler_ownership: Any = None
        self.control_state_error = None
        self.scheduler_manager = _PanelSchedulerManager()

    def plant_health(self) -> PlantHealth:
        return PlantHealth(
            telemetry_fresh=True,
            system_safe=True,
            battery_safe=True,
            grid_safe=True,
            system_events_clear=True,
            reasons=(),
        )

    def operation_impact(self) -> OperationImpact:
        return OperationImpact(active_service=0, immediate_effect_known=True)

    def scheduler_control_context(
        self,
        operation: ControlOperation,
    ) -> ControlPolicyInput:
        return ControlPolicyInput(
            operation=operation,
            operator_mode=OperatorMode.LOCAL_CONTROL,
            plant_health=self.plant_health(),
            operation_impact=self.operation_impact(),
            scheduler_visibility_fresh=True,
            ownership_store_valid=True,
            ownership_ambiguous=False,
            mqtt_connected=True,
        )

    def scheduler_mutation_diagnostics(self) -> dict[str, object]:
        return {
            "status": "wired_disabled",
            "live_writes_enabled": False,
        }


def test_panel_state_separates_identity_and_exposes_recovery_without_action() -> None:
    runtime = PanelRuntime()
    hass = SimpleNamespace(data={DOMAIN: {"entry-a": runtime}})

    state = panel_state(cast(Any, hass))

    entry = state["entries"][0]
    assert entry["entry_id"] == "entry-a"
    assert entry["gateway_serial"] == "PX-100"
    assert entry["release_gate"]["live_writes_enabled"] is False
    assert entry["recovery"] == [
        {
            "transaction_id": "recover-1",
            "operation": "update",
            "target_identity": {
                "m_id": None,
                "sch_index": 12,
                "service_no": None,
                "source_id": None,
                "asset_id": None,
            },
            "phase": "recovery_required",
            "reason": "restart_recovery_required",
        }
    ]

@pytest.mark.asyncio
async def test_panel_state_refreshes_bounded_scheduler_evidence() -> None:
    runtime = PanelRuntime()
    hass = SimpleNamespace(data={DOMAIN: {"entry-a": runtime}})

    state = await async_panel_state(cast(Any, hass))

    assert state["entries"][0]["schedules"][0]["sch_index"] == 12
    assert len(runtime.scheduler_manager.list_calls) == 1
    from_epoch, to_epoch = runtime.scheduler_manager.list_calls[0]
    assert to_epoch - from_epoch == 8 * 24 * 60 * 60
    assert runtime.scheduler_manager.detail_calls == [12]


def test_preview_is_stable_and_cannot_open_production_apply() -> None:
    runtime = PanelRuntime()
    before = runtime.control_journal.snapshot()
    target = {"sch_index": 12, "service_no": 8, "source_id": 4}
    intent = {"duration_seconds": 1800}

    first = preview_operation(
        cast(Any, runtime),
        operation=ControlOperation.UPDATE,
        service=None,
        target=target,
        intent=intent,
    )
    second = preview_operation(
        cast(Any, runtime),
        operation=ControlOperation.UPDATE,
        service=None,
        target=target,
        intent=intent,
    )

    assert first == second
    assert first["can_apply"] is False
    assert "release_gate_closed" in first["blocked_reasons"]
    assert first["target"] == {**target, "asset_id": None}
    assert first["before"]["duration_seconds"] == 900
    assert first["after"]["duration_seconds"] == 1800
    assert first["preview_id"] == first["fingerprint"][:16]
    assert runtime.control_journal.snapshot() == before


@pytest.mark.parametrize(
    ("operation", "intent"),
    [
        (ControlOperation.STOP, {}),
        (ControlOperation.DIRECT_TARGET_SOC, {"socref": 75}),
        (ControlOperation.DIRECT_DEMAND_RESPONSE, {"power_w": -1000}),
        (
            ControlOperation.CREATE,
            {
                "start_epoch": 1_893_456_000,
                "duration_seconds": 900,
                "socref": 75,
            },
        ),
        (
            ControlOperation.PLANNER_APPLY,
            {
                "schedules": [
                    {
                        "m_id": 240000001,
                        "start_epoch": 1_893_456_000,
                        "duration_seconds": 900,
                        "socref": 75,
                    }
                ]
            },
        ),
        (
            ControlOperation.RECOVERY_APPLY,
            {"transaction_id": "recover-1"},
        ),
    ],
)
def test_production_preview_supports_every_nontarget_operation(
    operation: ControlOperation,
    intent: dict[str, Any],
) -> None:
    result = preview_operation(
        cast(Any, PanelRuntime()),
        operation=operation,
        service=None,
        target={},
        intent=intent,
    )

    assert result["operation"] == operation.value
    assert result["can_apply"] is False
    assert "target_origin_unknown" not in result["blocked_reasons"]
    assert "requested_service_mismatch" not in result["blocked_reasons"]



def test_production_recovery_uses_scoped_ownership_origin() -> None:
    runtime = PanelRuntime()
    runtime.control_journal = (
        ControlJournal.empty(
            config_entry_id="entry-a",
            gateway_serial=runtime.serial,
        )
        .begin_transaction(
            transaction_id="recover-owned",
            operation=ControlOperation.UPDATE.value,
            target_identity=JournalTargetIdentity(m_id=240000001),
        )
        .mark_unfinished_recovery_required()
    )
    runtime.scheduler_ownership = SimpleNamespace(get=lambda m_id: object())

    preview = preview_operation(
        cast(Any, runtime),
        operation=ControlOperation.RECOVERY_APPLY,
        service=None,
        target={},
        intent={"transaction_id": "recover-owned"},
    )

    assert "target_origin_unknown" not in preview["blocked_reasons"]


def test_production_preview_rejects_non_target_soc_schedule() -> None:
    runtime = PanelRuntime()
    schedule = runtime.scheduler_manager.normalized_schedules[0]
    runtime.scheduler_manager.normalized_schedules = (replace(schedule, service_type=110),)

    with pytest.raises(vol.Invalid, match="Only Target SoC"):
        preview_operation(
            cast(Any, runtime),
            operation=ControlOperation.UPDATE,
            service=None,
            target={"sch_index": 12, "service_no": 8, "source_id": 4},
            intent={"socref": 70},
        )



def test_production_preview_cannot_accept_caller_supplied_grant() -> None:
    runtime = PanelRuntime()
    local = runtime.scheduler_manager.normalized_schedules[0]
    runtime.scheduler_manager.normalized_schedules = (
        replace(
            local,
            ownership=replace(
                local.ownership,
                ownership=Ownership.EXTERNAL,
                editable=False,
                deletable=False,
                protected=True,
            ),
        ),
    )
    target = {"sch_index": 12, "service_no": 8, "source_id": 4}

    preview = preview_operation(
        cast(Any, runtime),
        operation=ControlOperation.REPLACE,
        service=None,
        target=target,
        intent={"socref": 70, "request_grant": True},
    )

    assert "request_grant" not in preview["intent"]
    assert "manual_grant_required" in preview["blocked_reasons"]
    with pytest.raises(vol.Invalid):
        panel_module._INTENT_SCHEMA({"grant_authorized": True})
    with pytest.raises(vol.Invalid, match="unsupported values"):
        preview_operation(
            cast(Any, PanelRuntime()),
            operation=ControlOperation.UPDATE,
            service=None,
            target=target,
            intent={"start_epoch": 1_893_456_000},
        )


def test_production_planner_preview_preserves_protected_schedules() -> None:
    runtime = PanelRuntime()
    schedule = runtime.scheduler_manager.normalized_schedules[0]
    runtime.scheduler_manager.normalized_schedules = (
        replace(
            schedule,
            ownership=replace(
                schedule.ownership,
                ownership=Ownership.EXTERNAL,
                source_label="External",
                editable=False,
                deletable=False,
                protected=True,
            ),
        ),
    )
    proposed = {
        "m_id": 240000002,
        "start_epoch": 1_893_459_600,
        "duration_seconds": 900,
        "socref": 70,
    }

    preview = preview_operation(
        cast(Any, runtime),
        operation=ControlOperation.PLANNER_APPLY,
        service=None,
        target={},
        intent={"schedules": [proposed]},
    )

    assert preview["after"][0]["sch_index"] == 12
    assert preview["after"][0]["protected"] is True
    assert preview["after"][1] == proposed
    overlapping = preview_operation(
        cast(Any, runtime),
        operation=ControlOperation.PLANNER_APPLY,
        service=None,
        target={},
        intent={
            "schedules": [
                {
                    **proposed,
                    "start_epoch": schedule.start_epoch + 100,
                }
            ]
        },
    )
    assert "scheduler_external_conflict" in overlapping["blocked_reasons"]


@pytest.mark.asyncio
async def test_control_api_registration_is_optional_card_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    static_paths: list[object] = []
    commands: list[object] = []

    async def register_static(paths: list[object]) -> None:
        static_paths.extend(paths)

    monkeypatch.setattr(
        panel_module.websocket_api,
        "async_register_command",
        lambda _hass, command: commands.append(command),
    )
    hass = SimpleNamespace(
        data={},
        http=SimpleNamespace(async_register_static_paths=register_static),
    )

    await panel_module.async_register_control_api(cast(Any, hass))
    await panel_module.async_register_control_api(cast(Any, hass))

    assert len(static_paths) == 1
    assert getattr(static_paths[0], "url_path") == panel_module.PANEL_MODULE_URL
    assert len(commands) == 10
    assert panel_module.websocket_gateway_scheduler_preview in commands
    assert panel_module.websocket_gateway_scheduler_apply in commands


class _MemoryStore:
    values: dict[str, dict[str, Any]] = {}

    def __init__(self, hass: Any, version: int, key: str) -> None:
        del hass, version
        self.key = key

    async def async_load(self) -> dict[str, Any] | None:
        value = self.values.get(self.key)
        return deepcopy(value) if value is not None else None

    async def async_save(self, value: dict[str, Any]) -> None:
        self.values[self.key] = deepcopy(value)


async def _simulator_panel_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> SimulatorEntryRuntime:
    _MemoryStore.values = {}
    monkeypatch.setattr(simulator_entry_module, "Store", _MemoryStore)
    runtime = SimulatorEntryRuntime(
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(entry_id="sim-panel")),
    )
    await runtime.async_initialize()
    return runtime


@pytest.mark.asyncio
async def test_simulator_preview_apply_is_one_use_and_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = await _simulator_panel_runtime(monkeypatch)
    before = runtime.simulator.snapshot()

    preview = preview_operation(
        runtime,
        operation=ControlOperation.DIRECT_TARGET_SOC,
        service="targetsoc",
        target={},
        intent={"socref": 76},
    )
    applied = await apply_simulator_preview(runtime, preview["preview_id"])
    replay = await apply_simulator_preview(runtime, preview["preview_id"])

    assert before["battery"]["target_soc_percent"] is None
    assert preview["can_apply"] is True
    assert preview["before"]["battery"]["target_soc_percent"] is None
    assert preview["after"]["battery"]["target_soc_percent"] == 76
    assert preview["after"]["battery"]["power_w"] == 0
    assert preview["after"]["active_service"] == 107
    assert preview["after"]["demand_response_power_w"] == 0
    assert applied["status"] == "success"
    assert runtime.simulator.adapter.battery.target_soc_percent == 76
    assert replay["blocked_reasons"] == ["preview_missing_or_consumed"]

    restored = SimulatorEntryRuntime(
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace(entry_id="sim-panel")),
    )
    await restored.async_initialize()
    assert restored.simulator.adapter.battery.target_soc_percent == 76


@pytest.mark.asyncio
async def test_simulator_preview_rejects_state_race_and_is_consumed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = await _simulator_panel_runtime(monkeypatch)
    preview = preview_operation(
        runtime,
        operation=ControlOperation.DIRECT_TARGET_SOC,
        service="targetsoc",
        target={},
        intent={"socref": 75},
    )
    runtime.simulator.adapter.battery.soc_percent = 51

    stale = await apply_simulator_preview(runtime, preview["preview_id"])
    replay = await apply_simulator_preview(runtime, preview["preview_id"])

    assert stale["blocked_reasons"] == ["simulator_state_changed"]
    assert runtime.simulator.adapter.battery.target_soc_percent is None
    assert replay["blocked_reasons"] == ["preview_missing_or_consumed"]






@pytest.mark.asyncio
async def test_simulator_preview_canonicalizes_create_identity_and_rejects_nonfinite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = await _simulator_panel_runtime(monkeypatch)
    intent = {
        "start_epoch": 1_893_456_000,
        "duration_seconds": 900,
        "socref": 75,
    }

    preview = preview_operation(
        runtime,
        operation=ControlOperation.CREATE,
        service=None,
        target={"m_id": 240000001, "asset_id": "battery-1"},
        intent=intent,
    )
    assert preview["service"] == "targetsoc"
    assert preview["target"]["asset_id"] == "battery-1"
    assert preview["target"]["service_no"] == 110

    with pytest.raises(vol.Invalid, match="asset identity"):
        preview_operation(
            runtime,
            operation=ControlOperation.CREATE,
            service="targetsoc",
            target={"m_id": 240000002, "asset_id": "other"},
            intent=intent,
        )
    with pytest.raises(vol.Invalid, match="Target SoC service"):
        preview_operation(
            runtime,
            operation=ControlOperation.CREATE,
            service="other",
            target={"m_id": 240000002},
            intent=intent,
        )
    with pytest.raises(vol.Invalid, match="Target SoC service"):
        preview_operation(
            runtime,
            operation=ControlOperation.STOP,
            service="other",
            target={},
            intent={},
        )
    with pytest.raises(vol.Invalid, match="finite"):
        preview_operation(
            runtime,
            operation=ControlOperation.DIRECT_TARGET_SOC,
            service=None,
            target={},
            intent={"socref": float("nan")},
        )
    with pytest.raises(vol.Invalid, match="outside simulated battery bounds"):
        preview_operation(
            runtime,
            operation=ControlOperation.DIRECT_TARGET_SOC,
            service=None,
            target={},
            intent={"socref": 5},
        )
    with pytest.raises(vol.Invalid, match="finite"):
        panel_module._INTENT_SCHEMA({"socref": "nan"})
    with pytest.raises(vol.Invalid, match="finite"):
        panel_module._INTENT_SCHEMA({"power_w": "inf"})
    with pytest.raises(vol.Invalid):
        panel_module._INTENT_SCHEMA({"socref": -1})
    with pytest.raises(vol.Invalid):
        panel_module._INTENT_SCHEMA({"socref": 101})
    with pytest.raises(vol.Invalid):
        panel_module._SCHEDULE_INTENT_SCHEMA(
            {
                "m_id": 1,
                "start_epoch": 1,
                "duration_seconds": 1,
                "socref": 101,
            }
        )


@pytest.mark.asyncio
async def test_simulator_planner_preview_preserves_external_schedules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = await _simulator_panel_runtime(monkeypatch)
    await runtime.async_load_scenario("external_conflict")
    external = runtime.simulator.adapter.schedules[240000001]

    preview = preview_operation(
        runtime,
        operation=ControlOperation.PLANNER_APPLY,
        service=None,
        target={"asset_id": "battery-1"},
        intent={
            "schedules": [
                {
                    "m_id": 240000002,
                    "start_epoch": int(external.end.timestamp()) + 900,
                    "duration_seconds": 900,
                    "socref": 70,
                }
            ]
        },
    )

    assert [schedule["m_id"] for schedule in preview["after"]] == [
        240000001,
        240000002,
    ]
    assert preview["after"][1]["origin"] == "local_created"
    assert preview["after"][1]["service_no"] == 110
    assert preview["after"][1]["end"] == (
        external.end + timedelta(seconds=1800)
    ).isoformat()

    blocked = preview_operation(
        runtime,
        operation=ControlOperation.PLANNER_APPLY,
        service=None,
        target={"asset_id": "battery-1"},
        intent={
            "schedules": [
                {
                    "m_id": 240000003,
                    "start_epoch": int(external.start.timestamp()) + 900,
                    "duration_seconds": 900,
                    "socref": 70,
                }
            ]
        },
    )
    assert blocked["can_apply"] is False
    assert "scheduler_external_conflict" in blocked["blocked_reasons"]


@pytest.mark.asyncio
async def test_simulator_mutations_share_one_persistence_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = await _simulator_panel_runtime(monkeypatch)
    preview = preview_operation(
        runtime,
        operation=ControlOperation.DIRECT_TARGET_SOC,
        service="targetsoc",
        target={},
        intent={"socref": 75},
    )
    save_entered = asyncio.Event()
    release_save = asyncio.Event()
    original_save = _MemoryStore.async_save

    async def delayed_save(store: _MemoryStore, value: dict[str, Any]) -> None:
        save_entered.set()
        await release_save.wait()
        await original_save(store, value)

    monkeypatch.setattr(_MemoryStore, "async_save", delayed_save)
    apply_task = asyncio.create_task(
        apply_simulator_preview(runtime, preview["preview_id"])
    )
    await save_entered.wait()
    advance_task = asyncio.create_task(runtime.async_advance(60))
    await asyncio.sleep(0)

    assert not advance_task.done()
    release_save.set()
    applied, advanced = await asyncio.gather(apply_task, advance_task)
    assert applied["status"] == "success"
    assert advanced["clock"] == "2030-01-01T00:01:00+00:00"


@pytest.mark.asyncio
async def test_simulator_recovery_preview_requires_matching_observed_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = await _simulator_panel_runtime(monkeypatch)
    await runtime.async_load_scenario("recovery_required")
    schedule = runtime.simulator.adapter.schedules[240000001]
    runtime.simulator.adapter.schedules[240000001] = replace(schedule, socref=99)
    revision = runtime.simulator.journal.journal.revision

    preview = preview_operation(
        runtime,
        operation=ControlOperation.RECOVERY_APPLY,
        service="targetsoc",
        target={},
        intent={"transaction_id": "scenario-recovery"},
    )

    assert preview["can_apply"] is False
    assert "adapter_capability_unsupported" in preview["blocked_reasons"]
    assert preview["after"]["recovery_evidence"]["reason"] == (
        "recovery_state_mismatch"
    )
    assert runtime.simulator.journal.journal.revision == revision
    assert runtime.simulator.journal.journal.unresolved_transactions




def _panel_schedule_target(schedule: dict[str, Any]) -> dict[str, Any]:
    return {
        key: schedule[key]
        for key in ("m_id", "sch_index", "service_no", "source_id", "asset_id")
        if schedule.get(key) is not None
    }


@pytest.mark.asyncio
async def test_all_simulator_demonstration_paths_preview_and_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = await _simulator_panel_runtime(monkeypatch)

    await runtime.async_load_scenario("direct_target_soc")
    direct_preview = preview_operation(
        runtime,
        operation=ControlOperation.DIRECT_TARGET_SOC,
        service=None,
        target={},
        intent={"socref": 77},
    )
    direct = await apply_simulator_preview(runtime, direct_preview["preview_id"])
    assert direct["status"] == "success"
    assert runtime.simulator.adapter.battery.target_soc_percent == 77
    direct_state = runtime.simulator.snapshot()
    assert direct_preview["after"] == {
        "battery": direct_state["battery"],
        "active_service": direct_state["active_service"],
        "demand_response_power_w": direct_state["demand_response_power_w"],
    }

    await runtime.async_load_scenario("direct_demand_response")
    demand_preview = preview_operation(
        runtime,
        operation=ControlOperation.DIRECT_DEMAND_RESPONSE,
        service=None,
        target={},
        intent={"power_w": -1200},
    )
    demand = await apply_simulator_preview(runtime, demand_preview["preview_id"])
    assert demand["status"] == "success"
    assert runtime.simulator.adapter.battery.power_w == -1200
    demand_state = runtime.simulator.snapshot()
    assert demand_preview["after"] == {
        "battery": demand_state["battery"],
        "active_service": demand_state["active_service"],
        "demand_response_power_w": demand_state["demand_response_power_w"],
    }

    await runtime.async_load_scenario("scheduler_crud")
    schedule = panel_state(
        cast(Any, SimpleNamespace(data={DOMAIN: {"sim-panel": runtime}}))
    )["entries"][0]["schedules"][0]
    update_preview = preview_operation(
        runtime,
        operation=ControlOperation.UPDATE,
        service="targetsoc",
        target=_panel_schedule_target(schedule),
        intent={"duration_seconds": 1800, "socref": 71},
    )
    updated = await apply_simulator_preview(runtime, update_preview["preview_id"])
    assert updated["status"] == "success"
    assert runtime.simulator.adapter.schedules[240000001].socref == 71
    assert update_preview["after"] == runtime.simulator.adapter.schedules[
        240000001
    ].as_dict()
    delete_preview = preview_operation(
        runtime,
        operation=ControlOperation.DELETE,
        service="targetsoc",
        target=_panel_schedule_target(schedule),
        intent={},
    )
    deleted = await apply_simulator_preview(runtime, delete_preview["preview_id"])
    assert deleted["status"] == "success"
    assert runtime.simulator.adapter.schedules == {}

    await runtime.async_load_scenario("external_conflict")
    external = panel_state(
        cast(Any, SimpleNamespace(data={DOMAIN: {"sim-panel": runtime}}))
    )["entries"][0]["schedules"][0]
    replacement_intent = {
        "start_epoch": external["start_epoch"],
        "duration_seconds": 2700,
        "socref": 66,
    }
    blocked = preview_operation(
        runtime,
        operation=ControlOperation.REPLACE,
        service="targetsoc",
        target=_panel_schedule_target(external),
        intent=replacement_intent,
    )
    authorized = preview_operation(
        runtime,
        operation=ControlOperation.REPLACE,
        service="targetsoc",
        target=_panel_schedule_target(external),
        intent={**replacement_intent, "request_grant": True},
    )
    external_result = await apply_simulator_preview(
        runtime,
        authorized["preview_id"],
    )
    assert blocked["can_apply"] is False
    assert authorized["can_apply"] is True
    assert external_result["status"] == "success"
    assert "request_grant" not in authorized["intent"]
    assert runtime.simulator.adapter.schedules[240000001].origin.value == (
        "external_observed"
    )

    await runtime.async_load_scenario("deterministic_planner")
    planner_start = int(runtime.simulator.adapter.clock.now.timestamp()) + 900
    planner_preview = preview_operation(
        runtime,
        operation=ControlOperation.PLANNER_APPLY,
        service=None,
        target={"asset_id": "battery-1"},
        intent={
            "schedules": [
                {
                    "m_id": 240000010,
                    "start_epoch": planner_start,
                    "duration_seconds": 900,
                    "socref": 64,
                }
            ]
        },
    )
    planned = await apply_simulator_preview(runtime, planner_preview["preview_id"])
    assert planned["status"] == "success"
    assert sorted(runtime.simulator.adapter.schedules) == [240000010]
    assert planner_preview["after"] == runtime.simulator.snapshot()["schedules"]

    await runtime.async_load_scenario("recovery_required")
    recovery_preview = preview_operation(
        runtime,
        operation=ControlOperation.RECOVERY_APPLY,
        service="targetsoc",
        target={},
        intent={"transaction_id": "scenario-recovery"},
    )
    recovered = await apply_simulator_preview(runtime, recovery_preview["preview_id"])
    assert recovered["status"] == "success"
    assert runtime.simulator.journal.journal.unresolved_transactions == ()

    await runtime.async_load_scenario("partial_replacement")
    partial_schedule = panel_state(
        cast(Any, SimpleNamespace(data={DOMAIN: {"sim-panel": runtime}}))
    )["entries"][0]["schedules"][0]
    partial_preview = preview_operation(
        runtime,
        operation=ControlOperation.REPLACE,
        service="targetsoc",
        target=_panel_schedule_target(partial_schedule),
        intent={
            "start_epoch": partial_schedule["start_epoch"],
            "duration_seconds": 1800,
            "socref": 62,
        },
    )
    partial = await apply_simulator_preview(runtime, partial_preview["preview_id"])
    assert partial["status"] == "partial"
    assert runtime.simulator.journal.journal.replacement_state["phase"] == "partial"

    await runtime.async_load_scenario("timeout_delivery_unknown")
    timeout_preview = preview_operation(
        runtime,
        operation=ControlOperation.DIRECT_TARGET_SOC,
        service=None,
        target={},
        intent={"socref": 73},
    )
    timed_out = await apply_simulator_preview(runtime, timeout_preview["preview_id"])
    assert timed_out["status"] == "recovery_required"
    assert timed_out["adapter_status"] == "timed_out"
    with pytest.raises(vol.Invalid, match="no evidence-based recovery path"):
        preview_operation(
            runtime,
            operation=ControlOperation.RECOVERY_APPLY,
            service=None,
            target={},
            intent={"transaction_id": timed_out["transaction_id"]},
        )
    runtime.simulator.adapter.inject_fault("timeout", active=False)
    runtime.simulator.adapter.inject_fault("delivery_unknown")
    uncertain_preview = preview_operation(
        runtime,
        operation=ControlOperation.DIRECT_TARGET_SOC,
        service=None,
        target={},
        intent={"socref": 72},
    )
    uncertain = await apply_simulator_preview(runtime, uncertain_preview["preview_id"])
    assert uncertain["status"] == "recovery_required"
    assert uncertain["adapter_status"] == "delivery_unknown"
    assert runtime.simulator.adapter.battery.target_soc_percent == 72
