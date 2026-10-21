"""Reproduce the live false positive and require new physical stop samples."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.pixiihome_controller import price_stop
from custom_components.pixiihome_controller.exceptions import CommandValidationError
from custom_components.pixiihome_controller.protocol.scheduler_commands import ScheduledPriceStop


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "samples,success",
    [
        ([(0, 0, 1), (0, 0, 2)], True),
        ([(104, -89, 1), (104, -242, 2), (104, -297, 3)], False),
        ([(104, 0, 1), (104, 0, 2)], False),
        ([(0, 0, -1), (0, 0, -1)], False),
        ([(0, 0, 1), (0, 0, 1), (0, 0, 1)], False),
        ([(0, 0, 1), (104, -89, 2), (0, 0, 3)], False),
        ([(0, 89, 1), (0, 89, 2)], False),
        ([(121, 0, 1), (121, 0, 2)], False),
        ([(0, float("nan"), 1), (0, 0, 2)], False),
    ],
)
@pytest.mark.parametrize("expired", [False, True])
async def test_physical_stop_needs_two_distinct_new_idle_samples_before_deadline(
    monkeypatch, samples, success, expired
):
    since = datetime.now(UTC)
    index = 0

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return since + timedelta(seconds=index + 1)

    telemetry = SimpleNamespace(
        value=lambda family, key: samples[index][0 if key == "srv_type" else 1],
        received_at={}, is_fresh=lambda *args, **kwargs: True,
    )
    telemetry.received_at["core"] = since + timedelta(seconds=samples[0][2])

    async def sleep(seconds):
        nonlocal index
        index += 1
        if index >= len(samples):
            raise TimeoutError
        telemetry.received_at["core"] = since + timedelta(seconds=samples[index][2])

    monkeypatch.setattr(price_stop, "datetime", Clock)
    monkeypatch.setattr(price_stop.asyncio, "sleep", sleep)
    runtime = SimpleNamespace(
        telemetry=telemetry, plant_health=lambda: SimpleNamespace(healthy=True)
    )
    result = await price_stop.verify_price_stopped(
        runtime, since, since + timedelta(seconds=0 if expired else 20)
    )
    assert result is (success and not expired)


@pytest.mark.asyncio
async def test_physical_recovery_needs_health_and_fresh_telemetry():
    runtime = SimpleNamespace(plant_health=lambda: SimpleNamespace(healthy=False))
    assert not await price_stop.verify_price_stopped(runtime, datetime.now(UTC), None)


@pytest.mark.parametrize("service", ["peakshaving", "demandresponse"])
def test_stop_deletes_exact_price_identity_without_immediate_or_replacement_settings(service):
    command = ScheduledPriceStop(1000000000004, service).build()
    assert command.to_payload() == {
        "method": "delete", "m_id": 1000000000004,
    }
    assert command.expected_services == (service,)


@pytest.mark.parametrize("service", ["targetsoc", "adaptivepeakshaving", "runningservice", "idle"])
def test_price_stop_rejects_other_services(service):
    with pytest.raises(CommandValidationError):
        ScheduledPriceStop(1000000000004, service).build()
