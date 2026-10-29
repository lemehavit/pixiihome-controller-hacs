"""Bounded read-only observation of phase loading. Has no control adapter."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .control.load_balancing import (
    CONF_LOAD_BALANCING,
    LoadBalancingConfig,
    PhaseSample,
    finite,
    phase_envelope,
    usable_soc_evidence,
)

if TYPE_CHECKING:
    from .runtime import RuntimeData


class LoadBalancingObserver:
    """Poll independently of page refresh, with one task and no implicit restart."""

    def __init__(self, runtime: RuntimeData) -> None:
        self.runtime = runtime
        self._listeners: list[Callable[[], None]] = []
        self._task: asyncio.Task[None] | None = None
        self._capability_checked = False
        self.phase_service_available = False
        self.meter_limitation_available = False
        self.meter_limitation_capability: dict[str, Any] = {}
        self.general_service_settings_available = False
        self.general_service_settings: dict[str, Any] = {}
        self.snapshot: dict[str, Any] = {}
        self.samples: tuple[PhaseSample, ...] = ()
        try:
            self.config = LoadBalancingConfig.from_mapping(
                runtime._scheduler_settings().get(CONF_LOAD_BALANCING)
            )
            self.status = "waiting" if self.config.enabled else "disabled"
        except (ValueError, TypeError):
            self.config = LoadBalancingConfig()
            self.status = "invalid_config"

    def add_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(callback)

        def remove() -> None:
            if callback in self._listeners:
                self._listeners.remove(callback)

        return remove

    def apply_verified_reference_delay(self, delay: int) -> None:
        """Publish a delay that the Gateway writer has read back exactly."""
        if type(delay) is not int or not 0 <= delay <= 60:
            raise ValueError("service_reference_delay_invalid")
        self.general_service_settings = {
            **self.general_service_settings,
            "ref_delay": delay,
        }
        for listener in tuple(self._listeners):
            listener()

    def start(self) -> None:
        controller = getattr(self.runtime, "meter_limit_load_balancer", None)
        has_saved_lease = controller is not None and (
            controller.lease is not None or controller.idle_support.lease is not None
        )
        if (self.config.enabled or has_saved_lease) and self._task is None:
            # This loop is deliberately long lived.  Register it as a background
            # task so Home Assistant does not wait for it while wrapping up the
            # startup phase.
            self._task = self.runtime.hass.async_create_background_task(
                self._run(),
                "pixiihome_controller load balancing observer",
            )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def attributes(self) -> dict[str, Any]:
        controller = getattr(self.runtime, "meter_limit_load_balancer", None)
        return {
            **self.snapshot,
            "observation_only": False,
            "control_writes": 0,
            "resume_allowed": False,
            "mode_binding": "native_service_120_overlay",
            "phase_service_available": self.phase_service_available,
            "individual_phase_control_verified": True,
            "meter_limitation_available": self.meter_limitation_available,
            "meter_limitation_capability": self.meter_limitation_capability,
            "meter_limitation_control_allowed": True,
            "meter_limitation_hard_limits_allowed": False,
            "meter_limitation_soft_limit_response_seconds": 15,
            "meter_limitation_per_phase_contract_documented": False,
            "meter_limitation_per_phase_contract_physically_verified": True,
            "meter_limitation_soft_limit_unit": "external_meter_w",
            "meter_limitation_scope": "total_budget_split_per_phase",
            "meter_limitation_exp_mapping": "export",
            "meter_limitation_gen_mapping": "import",
            "meter_limitation_document_source": "Pixii 15087 rev. 1.0 section 3.6",
            "general_service_settings_available": self.general_service_settings_available,
            "native_service_reference_delay_seconds": self.general_service_settings.get(
                "ref_delay"
            ),
            "native_service_reference_delay_required_seconds": 10,
            "native_meter_other_services_id": self.general_service_settings.get(
                "meter_other_srv"
            ),
            "measurement_source": self.config.source,
            "fuse_limits_a": list(self.config.fuses),
            "margin_a": self.config.margin_a,
            "min_soc_percent": self.config.min_soc,
            "soc_basis": "usable",
            "control_blocked_reasons": [
                "native_measurement_timestamp_unverified",
                "hardware_limits_unverified",
            ],
            "measurement_freshness_verified": False,
            "limitations": [
                "service_120_hard_limits_must_remain_disabled",
                "hardware_limits_unverified",
                "meter_location_and_phase_order_unverified",
            ],
            **(controller.attributes() if controller is not None else {}),
        }

    async def _run(self) -> None:
        while True:
            await self.async_refresh()
            controller = getattr(self.runtime, "meter_limit_load_balancer", None)
            if controller is not None:
                await controller.tick()
                for listener in tuple(self._listeners):
                    listener()
            await asyncio.sleep(self.config.poll_seconds)

    async def async_refresh(self) -> None:
        if not self.config.enabled:
            return
        self.snapshot = {}
        self.samples = ()
        try:
            await self._read()
        except asyncio.CancelledError:
            raise
        except ValueError as error:
            self.status = (
                str(error)
                if str(error)
                in {
                    "phase_data_missing",
                    "phase_data_invalid",
                    "phase_measurements_inconsistent",
                    "reactive_current_exceeds_limit",
                    "phase_direction_missing",
                    "meter_stale",
                    "meter_timestamps_inconsistent",
                    "native_meter_unavailable",
                }
                else "read_failed"
            )
        except Exception:
            # No URLs, credentials or raw Gateway replies in diagnostics.
            self.status = "read_failed"
        for listener in tuple(self._listeners):
            listener()

    async def _read(self) -> None:
        gateway = self.runtime.gateway_scheduler
        if gateway is None:
            raise ValueError("native_meter_unavailable")
        started = datetime.now(UTC)
        core = await gateway.client.read_load_balancing_status()
        if not self._capability_checked:
            self._capability_checked = True
            try:
                capability = await gateway.client.read_load_balancing_status(phase_service=True)
                self.phase_service_available = all(f"act_ref{i}" in capability for i in (1, 2, 3))
            except Exception:
                self.phase_service_available = False
            try:
                limitation = await gateway.client.read_load_balancing_status(meter_limitation=True)
                self.meter_limitation_available = all(
                    key in limitation for key in ("enabled", "meter_id", "limit_per_phase")
                )
                if self.meter_limitation_available:
                    self.meter_limitation_capability = limitation
            except Exception:
                self.meter_limitation_available = False
                self.meter_limitation_capability = {}
        try:
            general = await gateway.client.read_load_balancing_status(
                general_service_settings=True
            )
            self.general_service_settings_available = all(
                key in general for key in ("ref_delay", "meter_other_srv")
            )
            if self.general_service_settings_available:
                self.general_service_settings = general
        except Exception:
            self.general_service_settings_available = False
            self.general_service_settings = {}
        now = datetime.now(UTC)
        if (now - started).total_seconds() > self.config.stale_seconds:
            raise ValueError("meter_stale")
        self.snapshot = {
            "observed_at": now.isoformat(),
            "service_type": core.get("srv_type"),
            "response_received_at": now.isoformat(),
            "phase_currents_a": [],
        }
        samples, timestamps = [], []
        for i in range(1, self.config.phases + 1):
            battery = core.get(f"ac_w{i}")
            if not finite(battery):
                raise ValueError("phase_data_missing")
            if self.config.source == "pixii":
                current, voltage, power = (core.get(f"meter_{key}{i}") for key in ("a", "v", "w"))
            else:
                current = self._entity_value(f"current_l{i}", "A", now, timestamps)
                voltage = self._entity_value(f"voltage_l{i}", "V", now, timestamps)
                power = self._entity_value(f"power_l{i}", "W", now, timestamps, optional=True)
            self.snapshot["phase_currents_a"].append(current if finite(current) else None)
            if power is None:
                continue
            if not all(finite(v) for v in (current, voltage, power)):
                raise ValueError("phase_data_missing")
            assert finite(current) and finite(voltage) and finite(power)
            samples.append(PhaseSample(current, voltage, power, battery))
        if timestamps:
            if (max(timestamps) - min(timestamps)).total_seconds() > self.config.poll_seconds:
                raise ValueError("meter_timestamps_inconsistent")
            self.snapshot["measurement_age_seconds"] = (now - min(timestamps)).total_seconds()
        if len(samples) != self.config.phases:
            raise ValueError("phase_direction_missing")
        self.samples = tuple(samples)
        telemetry = self.runtime.telemetry
        received = telemetry.received_at.get("battery")
        age = (now - received).total_seconds() if received is not None else None
        battery_payload = telemetry.payload("battery")
        soc = usable_soc_evidence(
            battery_payload, fresh=age is not None and 0 <= age <= self.config.stale_seconds
        )
        usable_soc = soc["usable_soc_percent"]
        low, high = soc["reserved_min_soc_percent"], soc["reserved_max_soc_percent"]
        soc["min_raw_soc_equivalent_percent"] = (
            low + self.config.min_soc * (high - low) / 100 if finite(low) and finite(high) else None
        )
        soc["battery_telemetry_age_seconds"] = age
        count, count_ok = battery_payload.get("num_batt"), battery_payload.get("num_batt_ok")
        battery_healthy = (
            soc["soc_evidence_fresh"]
            and type(battery_payload.get("batt_status")) is int
            and battery_payload["batt_status"] == 1
            and type(count) is int
            and count > 0
            and type(count_ok) is int
            and count_ok == count
        )
        charge_limit = battery_payload.get("batt_cap_w_ch")
        discharge_limit = battery_payload.get("batt_cap_w_disch")
        charge_permitted = battery_healthy and finite(charge_limit) and charge_limit > 0
        discharge_permitted = battery_healthy and finite(discharge_limit) and discharge_limit > 0
        soc["battery_charge_capacity_dc_w"] = charge_limit if finite(charge_limit) else None
        soc["battery_discharge_capacity_dc_w"] = (
            discharge_limit if finite(discharge_limit) else None
        )
        soc["battery_healthy"] = battery_healthy
        soc["discharge_blocked_reason"] = (
            "battery_telemetry_stale"
            if not soc["soc_evidence_fresh"]
            else "usable_soc_unavailable"
            if usable_soc is None
            else "usable_soc_reserve_reached"
            if usable_soc <= self.config.min_soc
            else "battery_not_ready"
            if not battery_healthy
            else "battery_discharge_capacity_unavailable"
            if not discharge_permitted
            else None
        )
        envelope = phase_envelope(
            self.config,
            tuple(samples),
            usable_soc,
            charge_permitted=charge_permitted,
            discharge_permitted=discharge_permitted,
        )
        self.snapshot.update(soc)
        self.snapshot.update(envelope)
        self.status = (
            "insufficient_capacity"
            if not envelope["feasible"]
            else "overload"
            if envelope["overloaded_phases"]
            else "observing"
        )

    def _entity_value(
        self,
        field: str,
        unit: str,
        now: datetime,
        timestamps: list[datetime],
        *,
        optional: bool = False,
    ) -> float | None:
        entity_id = getattr(self.config, field)
        if not entity_id and optional:
            return None
        state = self.runtime.hass.states.get(entity_id)
        if state is None or state.state in {"unknown", "unavailable"}:
            raise ValueError("phase_data_missing")
        reported = state.last_reported
        age = (now - reported).total_seconds()
        if not 0 <= age <= self.config.stale_seconds:
            raise ValueError("meter_stale")
        actual_unit = state.attributes.get("unit_of_measurement")
        factor = 1000 if unit == "W" and actual_unit == "kW" else 1
        if actual_unit != unit and factor == 1:
            raise ValueError("phase_data_invalid")
        try:
            value = float(state.state) * factor
        except (TypeError, ValueError):
            raise ValueError("phase_data_invalid") from None
        if not finite(value):
            raise ValueError("phase_data_invalid")
        timestamps.append(reported)
        return value
