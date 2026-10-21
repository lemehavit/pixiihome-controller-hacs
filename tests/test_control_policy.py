from __future__ import annotations

from dataclasses import replace

import pytest

from custom_components.pixiihome_controller.control.models import (
    AdapterCapability,
    ControlOperation,
    ControlPolicyInput,
    Initiative,
    OperationImpact,
    OperatorMode,
    PlantHealth,
    ReleaseState,
    RuntimeMode,
    ScheduleOrigin,
)
from custom_components.pixiihome_controller.control.policy import evaluate_control_operation


def healthy_plant() -> PlantHealth:
    return PlantHealth(
        telemetry_fresh=True,
        system_safe=True,
        battery_safe=True,
        grid_safe=True,
        system_events_clear=True,
        reasons=(),
        system_status=1,
        battery_status=1,
        grid_status=1,
        system_events=0,
    )


def allowed_context(operation: ControlOperation = ControlOperation.UPDATE) -> ControlPolicyInput:
    return ControlPolicyInput(
        operation=operation,
        runtime_mode=RuntimeMode.SIMULATOR,
        operator_mode=OperatorMode.LOCAL_CONTROL,
        origin=ScheduleOrigin.LOCAL_CREATED,
        release_state=ReleaseState.PREVIEW_ONLY,
        plant_health=healthy_plant(),
        operation_impact=OperationImpact(
            active_service=0,
            immediate_effect_known=True,
            exact_target=True,
        ),
        scheduler_visibility_fresh=True,
        ownership_store_valid=True,
        ownership_ambiguous=False,
        mqtt_connected=True,
    )


def test_production_mutation_stops_at_release_gate() -> None:
    context = replace(
        allowed_context(),
        runtime_mode=RuntimeMode.PRODUCTION,
        release_state=ReleaseState.LIVE_RELEASE_CLOSED,
    )

    decision = evaluate_control_operation(context)

    assert not decision.permitted
    assert "release_gate_closed" in decision.blocked_reasons


def test_validation_authorization_never_opens_production_release_gate() -> None:
    context = replace(
        allowed_context(),
        runtime_mode=RuntimeMode.PRODUCTION,
        release_state=ReleaseState.LIVE_RELEASE_CLOSED,
        validation_authorized=True,
    )

    decision = evaluate_control_operation(context)

    assert not decision.permitted
    assert "release_gate_closed" in decision.blocked_reasons


@pytest.mark.parametrize("owned,future,service,allowed", [
    (True, True, 105, True), (False, True, 105, False),
    (True, False, 105, False), (True, True, 102, False),
    (True, True, 121, False),
])
def test_future_create_exception_is_limited_to_owned_background_aps(
    owned, future, service, allowed,
):
    context = replace(
        allowed_context(ControlOperation.CREATE), owned_background_aps=owned,
        future_schedule_only=future,
        operation_impact=OperationImpact(active_service=service, immediate_effect_known=True),
    )
    assert evaluate_control_operation(context).permitted is allowed

@pytest.mark.parametrize(
    "service",
    [
        "ffr",
        "fcr",
        "batterycalibration",
        "powercontrol",
        "aps",
        "voltagesupport",
        "unknown",
    ],
)
def test_generic_direct_control_is_never_permitted(service: str) -> None:
    decision = evaluate_control_operation(
        replace(
            allowed_context(ControlOperation.DIRECT_CONTROL),
            requested_service=service,
        )
    )

    assert not decision.permitted
    assert decision.blocked_reasons == ("generic_direct_control_forbidden",)


@pytest.mark.parametrize(
    ("operation", "service"),
    [
        (ControlOperation.DIRECT_TARGET_SOC, "targetsoc"),
        (ControlOperation.DIRECT_DEMAND_RESPONSE, "demandresponse"),
    ],
)
def test_only_explicit_supported_direct_services_enter_policy(
    operation: ControlOperation,
    service: str,
) -> None:
    permitted = evaluate_control_operation(
        replace(allowed_context(operation), requested_service=service)
    )
    mismatched = evaluate_control_operation(
        replace(allowed_context(operation), requested_service="powercontrol")
    )

    assert permitted.permitted
    assert mismatched.blocked_reasons == ("requested_service_mismatch",)


def test_active_target_soc_is_not_a_plant_health_failure() -> None:
    context = replace(
        allowed_context(ControlOperation.UPDATE),
        operation_impact=OperationImpact(
            active_service=110,
            immediate_effect_known=True,
            exact_target=True,
        ),
    )

    decision = evaluate_control_operation(context)

    assert context.plant_health.healthy
    assert decision.permitted


def test_create_during_active_service_is_operation_specific_block() -> None:
    context = replace(
        allowed_context(ControlOperation.CREATE),
        operation_impact=OperationImpact(active_service=110, immediate_effect_known=True),
    )

    decision = evaluate_control_operation(context)

    assert not decision.permitted
    assert decision.blocked_reasons == ("active_service_impact_unproven",)


@pytest.mark.parametrize(
    ("capability", "reason"),
    [
        (AdapterCapability.UNSUPPORTED, "adapter_capability_unsupported"),
        (AdapterCapability.EVIDENCE_REQUIRED, "evidence_required"),
    ],
)
def test_adapter_capabilities_fail_before_transport(
    capability: AdapterCapability,
    reason: str,
) -> None:
    decision = evaluate_control_operation(
        replace(allowed_context(), adapter_capability=capability)
    )

    assert not decision.permitted
    assert reason in decision.blocked_reasons


def test_external_manual_target_requires_scoped_grant() -> None:
    external = replace(
        allowed_context(),
        origin=ScheduleOrigin.EXTERNAL_OBSERVED,
        initiative=Initiative.MANUAL,
    )

    blocked = evaluate_control_operation(external)
    permitted = evaluate_control_operation(replace(external, grant_authorized=True))

    assert "manual_grant_required" in blocked.blocked_reasons
    assert permitted.permitted


def test_external_automation_requires_explicit_policy_authorization() -> None:
    external = replace(
        allowed_context(),
        origin=ScheduleOrigin.EXTERNAL_OBSERVED,
        initiative=Initiative.AUTOMATION,
    )

    blocked = evaluate_control_operation(external)
    permitted = evaluate_control_operation(replace(external, automation_authorized=True))

    assert "automation_policy_denies_external_target" in blocked.blocked_reasons
    assert permitted.permitted


def test_fail_closed_defaults_never_permit_a_mutation() -> None:
    decision = evaluate_control_operation(ControlPolicyInput(ControlOperation.CREATE))

    assert not decision.permitted
    assert "release_gate_closed" in decision.blocked_reasons
    assert "plant_health_unknown" in decision.blocked_reasons
    assert "target_origin_unknown" in decision.blocked_reasons
