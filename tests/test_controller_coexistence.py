"""Regression coverage for controller coexistence and authorization windows."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from custom_components.pixiihome_controller.control.load_balancing import (
    LoadBalancingConfig,
    PhaseSample,
)
from custom_components.pixiihome_controller.control.phase_gateway import PHASE_FIELDS
from custom_components.pixiihome_controller.control.policy import AutomationPolicy
from custom_components.pixiihome_controller.idle_phase_controller import IdlePhaseLoadBalancer
from custom_components.pixiihome_controller.meter_limit_controller import MeterLimitLoadBalancer

from .test_price_controller import policy


@pytest.mark.parametrize("start,duration,expected", [
    ("2026-10-16T07:59:30+02:00", 120, 0),
    ("2026-10-16T08:00:00+02:00", 120, 120),
    ("2026-10-16T08:59:30+02:00", 120, 30),
    ("2026-10-17T08:00:00+02:00", 120, 0),
])
def test_authorization_window_clips_real_lease_duration(start, duration, expected):
    configured = replace(policy(), time_window={"start": "08:00", "end": "08:59", "days": [4]})
    assert configured.bounded_duration(datetime.fromisoformat(start), duration) == expected


def test_overnight_policy_uses_the_start_day_and_stockholm_dst():
    configured = replace(policy(), time_window={"start": "22:00", "end": "02:59", "days": [5]})
    # The repeated hour on Sunday belongs to Saturday's overnight authorization.
    for hour in (0, 1):
        assert configured.bounded_duration(datetime(2026, 10, 25, hour, 30, tzinfo=UTC), 120) == 120
    assert configured.bounded_duration(datetime(2026, 10, 25, 2, 30, tzinfo=UTC), 120) == 0


@pytest.mark.parametrize("field,value", [
    ("max_power_kw", float("nan")), ("max_power_kw", float("inf")),
    ("time_window", {"start": "99:00", "end": "23:59", "days": [4]}),
])
def test_invalid_automation_policy_fails_closed(field, value):
    data = policy().as_dict()
    data[field] = value
    assert not AutomationPolicy.from_mapping(data).enabled


class PhaseGateway:
    def __init__(self):
        self.priority_list = [102, 101, 105, 107]
        self.record = dict.fromkeys(PHASE_FIELDS, 0)
        self.service = 0
        self.switches = []

    async def read(self, service):
        return ({"enabled": 1, "profile": 0, "running_profile": 0,
                 "used_list": self.priority_list} if service == 100 else self.record)

    def settings(self, data):
        return dict(data)

    async def priority(self, values):
        self.priority_list = list(values)

    async def save(self, values):
        self.record = dict(values)

    async def switch(self, on):
        self.switches.append(on)
        self.service = 129 if on else 0

    async def core(self):
        return {"srv_type": self.service}


@pytest.mark.asyncio
async def test_idle_phase_support_yields_to_all_existing_services_and_does_not_restart_them():
    controller = object.__new__(IdlePhaseLoadBalancer)
    controller.gateway = PhaseGateway()
    controller.lease = None
    controller._lock = asyncio.Lock()
    controller.targets = controller.previous = controller.last_tick = None
    controller.runtime = SimpleNamespace()
    async def persist(lease):
        controller.lease = lease
    controller._persist = persist
    await controller._apply((300, 0, -300))
    assert controller.gateway.priority_list == [102, 101, 105, 107, 129]
    assert controller.lease["owned_priority"][-1] == 129
    # Hardware arbitration hands over to an arriving support activation.
    controller.gateway.service = 102
    await controller.tick()
    assert controller.lease is None
    assert controller.gateway.priority_list == [102, 101, 105, 107]
    assert controller.gateway.switches == [True]
    assert controller.gateway.service == 102


def test_gateway_minimum_cannot_raise_the_site_fuse_limit():
    controller = object.__new__(MeterLimitLoadBalancer)
    controller.runtime = SimpleNamespace(load_balancing_observer=SimpleNamespace(
        config=LoadBalancingConfig(fuse_l1=20, fuse_l2=20, fuse_l3=20),
        samples=(PhaseSample(5, 230, 1150, 0),) * 3,
    ))
    with pytest.raises(ValueError, match="meter_limitation_limit_outside_gateway_range"):
        controller._soft_limit_w({"exp_soft_limit__min": 50_000})
