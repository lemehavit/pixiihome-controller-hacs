from __future__ import annotations

from datetime import UTC, datetime, timedelta

from custom_components.pixiihome_controller.control.authorization import (
    MAX_MANUAL_GRANTS,
    AutomationPolicy,
    GrantRegistry,
    ManualActionGrant,
)
from custom_components.pixiihome_controller.control.models import (
    ControlOperation,
    ScheduleOrigin,
)

NOW = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
SCOPE = {
    "config_entry_id": "entry-1",
    "gateway_serial": "GATEWAY",
    "operation": ControlOperation.UPDATE,
    "target_identity": "scheduler:https:42",
    "fingerprint": "f" * 64,
    "preview_revision": "preview-7",
}


def test_manual_grant_is_one_use_and_exactly_scoped() -> None:
    grant = ManualActionGrant.issue(**SCOPE, now=NOW)

    assert not grant.authorize(**{**SCOPE, "fingerprint": "e" * 64}, now=NOW)
    assert grant.authorize(**SCOPE, now=NOW)
    assert not grant.authorize(**SCOPE, now=NOW)


def test_manual_grant_expires_and_restart_invalidates_registry() -> None:
    grant = ManualActionGrant.issue(**SCOPE, now=NOW, ttl=timedelta(seconds=30))

    assert not grant.authorize(**SCOPE, now=NOW + timedelta(seconds=31))

    first_process = GrantRegistry()
    issued = first_process.issue(**SCOPE, now=NOW)
    restarted_process = GrantRegistry()
    assert not restarted_process.authorize(issued.grant_id, **SCOPE, now=NOW)


def test_grant_registry_is_bounded_and_clearable() -> None:
    registry = GrantRegistry()
    issued = [
        registry.issue(
            **{
                **SCOPE,
                "fingerprint": f"{index:064x}",
            },
            now=NOW,
        )
        for index in range(MAX_MANUAL_GRANTS + 1)
    ]
    assert not registry.authorize(
        issued[0].grant_id,
        **{**SCOPE, "fingerprint": f"{0:064x}"},
        now=NOW,
    )
    registry.clear()
    assert not registry.authorize(
        issued[-1].grant_id,
        **{**SCOPE, "fingerprint": f"{MAX_MANUAL_GRANTS:064x}"},
        now=NOW,
    )


def test_automation_policy_bounds_every_control_dimension() -> None:
    policy = AutomationPolicy(
        services=frozenset({"targetsoc"}),
        operations=frozenset({ControlOperation.CREATE}),
        start_hour_utc=6,
        end_hour_utc=22,
        max_duration_seconds=3600,
        min_soc=20,
        max_soc=90,
        max_abs_power_w=5000,
    )

    assert policy.allows(
        operation=ControlOperation.CREATE,
        service="targetsoc",
        start=NOW,
        duration_seconds=1800,
        soc=80,
        power_w=-4000,
    )
    assert not policy.allows(
        operation=ControlOperation.CREATE,
        service="targetsoc",
        start=NOW,
        duration_seconds=3601,
        soc=80,
        power_w=-4000,
    )
    assert not policy.allows(
        operation=ControlOperation.CREATE,
        service="targetsoc",
        start=NOW,
        duration_seconds=1800,
        soc=80,
        power_w=-4000,
        origin=ScheduleOrigin.EXTERNAL_OBSERVED,
    )
