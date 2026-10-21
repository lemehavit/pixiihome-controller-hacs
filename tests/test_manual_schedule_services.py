"""Manual service creation and exact reconciliation, without a live gateway."""

from __future__ import annotations

from dataclasses import replace

import pytest

from custom_components.pixiihome_controller.control.models import ControlOperation
from custom_components.pixiihome_controller.exceptions import CommandValidationError
from custom_components.pixiihome_controller.panel import preview_operation
from custom_components.pixiihome_controller.protocol.mqtt import parse_ack
from custom_components.pixiihome_controller.protocol.scheduler_commands import ScheduledCreate
from custom_components.pixiihome_controller.protocol.scheduler_readback import (
    reconcile_scheduler_get,
    scheduler_expected_state_from_command,
)
from custom_components.pixiihome_controller.protocol.scheduler_values import (
    validate_scheduler_values,
)
from custom_components.pixiihome_controller.runtime import RuntimeData
from custom_components.pixiihome_controller.scheduler.mutations import (
    SchedulerCreateRequest,
    SchedulerExecutionResult,
    SchedulerExecutionStatus,
    SchedulerTransactionStatus,
)
from custom_components.pixiihome_controller.scheduler.ownership import (
    OwnershipLifecycle,
    scheduler_expected_state_from_record,
)

from .test_panel import _creation_runtime
from .test_scheduler_manager import M_ID, START, FakeExecutor, create_ack, manager

SERVICES = [
    ("demandresponse", {"pacref": -2500}),
    (
        "peakshaving",
        {
            "exportlim": -1000,
            "importlim": 5000,
            "charge_max": 3000,
            "discharge_max": 4000,
            "keep_active": False,
        },
    ),
    (
        "adaptivepeakshaving",
        {
            "exportlim": 0,
            "importlim": 5000,
            "charge_max": 3000,
            "discharge_max": 4000,
            "keep_active": True,
            "max_lim_change": 500,
        },
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("service", "values"), SERVICES)
async def test_create_complete_values_require_matching_keyed_readback(service, values):
    command = ScheduledCreate(
        service=service, m_id=M_ID, starttime=START, duration_seconds=3600, values=values
    ).build()
    expected = scheduler_expected_state_from_command(command)
    observed = parse_ack(
        {
            "method": "get",
            "service": service,
            "m_id": M_ID,
            "response": 65535,
            "values": {**values, "duration": 3600, "starttime": expected.as_mapping()["starttime"]},
        }
    )
    assert reconcile_scheduler_get(observed, expected).matched
    for key, value in values.items():
        wrong = not value if isinstance(value, bool) else value + 1
        assert not reconcile_scheduler_get(
            replace(observed, values={**observed.values, key: wrong}),
            expected,
        ).matched
        assert not reconcile_scheduler_get(
            replace(observed, values={k: v for k, v in observed.values.items() if k != key}),
            expected,
        ).matched
    executor = FakeExecutor(
        [
            SchedulerExecutionResult(
                SchedulerExecutionStatus.ACK_SUCCESS,
                replace(create_ack(), service=service),
            )
        ],
        [observed],
    )
    instance = manager(executor)
    result = await instance.create(SchedulerCreateRequest(START, 3600, values, service))
    assert result.status is SchedulerTransactionStatus.SUCCESS
    record = instance.ownership.records[M_ID]
    assert record.lifecycle is OwnershipLifecycle.OWNED
    assert scheduler_expected_state_from_record(record).parameters == values
    assert len(executor.mutation_commands) == len(executor.get_commands) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(("service", "values"), SERVICES)
async def test_preview_routes_selected_service_to_typed_manager(service, values):
    runtime = _creation_runtime()
    preview = preview_operation(
        runtime,
        operation=ControlOperation.CREATE,
        service=service,
        target={},
        intent={"start_epoch": 1_893_460_000, "duration_seconds": 900, "values": values},
    )
    assert preview["can_apply"]
    runtime._production_previews[preview["preview_id"]].update(user_id="admin", expires_at=4e9)
    await RuntimeData.async_apply_production_preview(runtime, preview["preview_id"], "admin")
    request = runtime.scheduler_mutation_manager.create.await_args.args[0]
    assert request.service == service and dict(request.values) == values


@pytest.mark.parametrize(("service", "values"), SERVICES)
@pytest.mark.parametrize("bad", [True, float("nan"), float("inf"), "2"])
def test_invalid_parameter_never_becomes_a_wire_command(service, values, bad):
    key = next(k for k in values if k != "keep_active")
    with pytest.raises(CommandValidationError):
        validate_scheduler_values(service, {**values, key: bad})
    with pytest.raises(CommandValidationError):
        validate_scheduler_values(service, {**values, "unknown": 1})


@pytest.mark.parametrize(
    "change",
    [
        {"keep_active": 1},
        {"exportlim": 6000},
        {"charge_max": -1},
        {"discharge_max": -1},
        {"max_lim_change": -1},
    ],
)
def test_peak_shaving_limits_and_boolean_are_validated(change):
    service, values = SERVICES[2]
    with pytest.raises(CommandValidationError):
        validate_scheduler_values(service, {**values, **change})
