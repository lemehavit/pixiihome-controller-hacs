"""Production load balancing through Pixii Service 120 soft limits.

The controller owns only a complete, read-back-verified Service 120 record.  It
never enables a hard limit and never starts, stops, or rewrites the service
underneath the limiter.  Turning the controller off restores the exact record
that was present when it was enabled.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from math import floor, sqrt
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.storage import Store

from .activity import log_activity
from .control.load_balancing import PhaseSample, finite
from .idle_phase_controller import IdlePhaseLoadBalancer

if TYPE_CHECKING:
    from .runtime import RuntimeData


_REQUIRED_REFERENCE_DELAY_SECONDS = 10
_MIN_SOFT_LIMIT_W = 100
_LIMIT_UPDATE_THRESHOLD_W = 250


class MeterLimitLoadBalancer:
    """Own a fail-closed Service 120 soft-limit overlay."""

    def __init__(self, runtime: RuntimeData) -> None:
        self.runtime = runtime
        self.store = Store(
            runtime.hass,
            1,
            f"pixiihome_controller.{runtime.serial}.meter_limit_lease",
        )
        self.lease: dict[str, Any] | None = None
        self.status = "disabled"
        self.reason: str | None = None
        self.startup_read_error: str | None = None
        self.applied_limit_w: int | None = None
        self.writes = 0
        self._lock = asyncio.Lock()
        self.idle_support = IdlePhaseLoadBalancer(runtime)

    def attributes(self) -> dict[str, Any]:
        return {
            "control_status": self.status,
            "control_blocked_reason": self.reason,
            "control_writes": self.writes,
            "meter_limitation_control_allowed": True,
            "meter_limitation_hard_limits_allowed": False,
            "meter_limitation_per_phase_contract_verified": True,
            "meter_limitation_scope": "total_budget_split_per_phase",
            "applied_total_soft_limit_w": self.applied_limit_w,
            "applied_per_phase_soft_limit_w": (
                round(self.applied_limit_w / 3, 1) if self.applied_limit_w is not None else None
            ),
            "correction_active": self.lease is not None,
            "underlying_service_commands": 0,
            "underlying_service_timer_changes": 0,
            "idle_overload_support": False,
            **self.idle_support.attributes(),
        }

    async def _save_lease(self, lease: dict[str, Any] | None) -> None:
        payload = {"lease": lease}
        await self.store.async_save(payload)
        if await self.store.async_load() != payload:
            raise ValueError("meter_limitation_journal_invalid")
        self.lease = lease

    @staticmethod
    def _record(values: Mapping[str, Any]) -> dict[str, int | float]:
        keys = (
            "enabled",
            "exp_soft_enabled",
            "exp_hard_enabled",
            "exp_soft_limit",
            "exp_hard_limit",
            "gen_soft_enabled",
            "gen_hard_enabled",
            "gen_soft_limit",
            "gen_hard_limit",
            "limit_per_phase",
            "meter_id",
        )
        if not all(key in values for key in keys):
            raise ValueError("meter_limitation_settings_incomplete")
        return {key: values[key] for key in keys}

    @staticmethod
    def _pristine(record: Mapping[str, int | float]) -> bool:
        return (
            record.get("enabled") == 0
            and record.get("exp_soft_enabled") == 0
            and record.get("gen_soft_enabled") == 0
            and record.get("exp_hard_enabled") == 0
            and record.get("gen_hard_enabled") == 0
            and record.get("exp_hard_limit") == 0
            and record.get("gen_hard_limit") == 0
        )

    async def _read(self) -> tuple[dict[str, int | float], dict[str, Any]]:
        scheduler = self.runtime.gateway_scheduler
        if scheduler is None:
            raise ValueError("native_meter_unavailable")
        values = await scheduler.client.read_load_balancing_status(meter_limitation=True)
        record = self._record(values)
        self.startup_read_error = None
        return record, values

    async def _write(self, record: Mapping[str, int | float]) -> None:
        scheduler = self.runtime.gateway_scheduler
        if scheduler is None:
            raise ValueError("native_meter_unavailable")
        await scheduler.client.write_meter_limitation_settings(record)
        self.writes += 1

    async def load_saved_lease(self, *, control_off: bool = False) -> None:
        """Read durable ownership without contacting or changing the Gateway."""
        saved = await self.store.async_load()
        if saved is None:
            self.lease = None
            return
        if not isinstance(saved, dict) or set(saved) != {"lease"}:
            raise ValueError("meter_limitation_journal_invalid")
        lease = saved["lease"]
        if lease is None:
            self.lease = None
            return
        if (
            not isinstance(lease, dict)
            or not isinstance(lease.get("original"), dict)
            or not isinstance(lease.get("owned"), list)
        ):
            raise ValueError("meter_limitation_journal_invalid")
        self.lease = lease
        self.status, self.reason = "recovery_required", "meter_limitation_restore_pending"
        if control_off:
            log_activity(
                self.runtime, "load_balancing_observation",
                "Saved meter limitation requires restoration",
                cause="A Service 120 lease remained after restart while control is off",
            )

    async def _recover_saved_lease(self) -> None:
        await self.load_saved_lease()
        if self.lease is None:
            return
        lease = self.lease
        current, _ = await self._read()
        original = self._record(lease["original"])
        owned = [self._record(item) for item in lease["owned"]]
        if current == original:
            await self._save_lease(None)
            return
        if current not in owned:
            raise ValueError("meter_limitation_recovery_conflict")
        await self._write(original)
        await self._save_lease(None)

    async def prepare(self) -> None:
        """Validate prerequisites and recover a previous owned record."""
        async with self._lock:
            observer = self.runtime.load_balancing_observer
            if observer is None or not observer.config.enabled:
                raise ValueError("load_balancing_configuration_required")
            if observer.config.source != "pixii":
                raise ValueError("native_meter_required_for_service_120")
            scheduler = self.runtime.gateway_scheduler
            if scheduler is None:
                raise ValueError("native_meter_unavailable")
            general = await scheduler.client.read_load_balancing_status(
                general_service_settings=True
            )
            if general.get("ref_delay") != _REQUIRED_REFERENCE_DELAY_SECONDS:
                raise ValueError("meter_limitation_reference_delay_invalid")
            await self._recover_saved_lease()
            current, _ = await self._read()
            if not self._pristine(current):
                raise ValueError("meter_limitation_in_use")
            if current.get("meter_id") != 1:
                raise ValueError("meter_limitation_meter_id_unverified")
            if not observer.samples:
                await observer.async_refresh()
            self._target_record(current)
            await self.idle_support.prepare()
            self.startup_read_error = None
            self.status, self.reason = "ready", None

    def _soft_limit_w(self, ranges: Mapping[str, Any]) -> int:
        observer = self.runtime.load_balancing_observer
        if observer is None:
            raise ValueError("load_balancing_configuration_required")
        config = observer.config
        samples: tuple[PhaseSample, ...] = observer.samples
        if len(samples) != config.phases:
            raise ValueError("phase_data_missing")
        capacities: list[float] = []
        for sample, fuse in zip(samples, config.fuses, strict=True):
            if not all(finite(value) for value in vars(sample).values()):
                raise ValueError("phase_data_invalid")
            apparent_w = sample.current_a * sample.voltage_v
            if abs(sample.grid_power_w) > apparent_w + max(50, apparent_w * 0.1):
                raise ValueError("phase_measurements_inconsistent")
            reactive_sq = max(0.0, apparent_w**2 - sample.grid_power_w**2)
            allowed_apparent_w = (fuse - config.margin_a) * sample.voltage_v
            if reactive_sq >= allowed_apparent_w**2:
                raise ValueError("reactive_current_exceeds_limit")
            capacities.append(sqrt(allowed_apparent_w**2 - reactive_sq))
        total = floor(min(capacities) * config.phases)
        minimums = [
            ranges.get("exp_soft_limit__min"),
            ranges.get("gen_soft_limit__min"),
            _MIN_SOFT_LIMIT_W,
        ]
        maximums = [
            ranges.get("exp_soft_limit__max"),
            ranges.get("gen_soft_limit__max"),
        ]
        valid_min = [float(value) for value in minimums if finite(value)]
        valid_max = [float(value) for value in maximums if finite(value)]
        if total < max(valid_min):
            # A Gateway minimum is not permission to exceed the fuse envelope.
            raise ValueError("meter_limitation_limit_outside_gateway_range")
        if valid_max:
            total = min(total, int(min(valid_max)))
        if total < _MIN_SOFT_LIMIT_W:
            raise ValueError("meter_limitation_limit_outside_gateway_range")
        return total

    def _target_record(
        self, original: Mapping[str, int | float], ranges: Mapping[str, Any] | None = None
    ) -> dict[str, int | float]:
        observer = self.runtime.load_balancing_observer
        if observer is None:
            raise ValueError("load_balancing_configuration_required")
        limit = self._soft_limit_w(ranges or {})
        snapshot = observer.snapshot
        charge_allowed = (
            snapshot.get("battery_healthy") is True
            and finite(snapshot.get("battery_charge_capacity_dc_w"))
            and snapshot["battery_charge_capacity_dc_w"] > 0
        )
        discharge_allowed = snapshot.get("discharge_available") is True
        return {
            **self._record(original),
            "enabled": 1,
            "exp_soft_enabled": 1 if charge_allowed else 0,
            "exp_soft_limit": limit,
            "gen_soft_enabled": 1 if discharge_allowed else 0,
            "gen_soft_limit": limit,
            "exp_hard_enabled": 0,
            "exp_hard_limit": 0,
            "gen_hard_enabled": 0,
            "gen_hard_limit": 0,
            "limit_per_phase": 1,
        }

    async def activate(self) -> None:
        """Enable the verified per-phase soft envelope with durable ownership."""
        async with self._lock:
            if self.lease is not None:
                return
            original, ranges = await self._read()
            if not self._pristine(original):
                raise ValueError("meter_limitation_in_use")
            wanted = self._target_record(original, ranges)
            await self._save_lease({"original": original, "owned": [wanted]})
            try:
                await self._write(wanted)
            except Exception:
                current, _ = await self._read()
                if current == original:
                    await self._save_lease(None)
                raise
            self.applied_limit_w = int(wanted["exp_soft_limit"])
            self.status, self.reason = "active", None
            log_activity(self.runtime, "load_balancing_observation",
                         f"Load balancing on: soft limit {int(wanted['exp_soft_limit'])} W")

    async def tick(self) -> None:
        """Maintain only owned fields and update SoC-dependent discharge permission."""
        enabled = (
            self.runtime.controller_state.load_balancing_enabled
            and self.runtime.production_control_enabled()
        )
        if not enabled:
            if self.lease is not None or getattr(self.idle_support, "lease", None) is not None:
                previous = (self.status, self.reason)
                try:
                    await self.deactivate()
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 - keep the observer alive for recovery
                    reason = (
                        str(error)
                        if isinstance(error, ValueError)
                        else str(error) or "meter_limitation_restore_failed"
                    )
                    conflict_logged_by_deactivate = reason == "meter_limitation_recovery_conflict"
                    if (
                        previous != ("recovery_required", reason)
                        and not conflict_logged_by_deactivate
                    ):
                        log_activity(
                            self.runtime,
                            "load_balancing_observation",
                            "Load balancing restore needs recovery",
                            cause=reason,
                        )
                    self.status, self.reason = "recovery_required", reason
                    return
                restored = getattr(self.runtime, "_meter_limitation_restored", None)
                if callable(restored) and self.lease is None:
                    restored()
            startup_read_error = getattr(self, "startup_read_error", None)
            if startup_read_error is not None:
                self.status, self.reason = "recovery_required", startup_read_error
            else:
                self.status, self.reason = "disabled", None
            return
        async with self._lock:
            try:
                if self.lease is None:
                    # prepare() is mandatory on the explicit switch transition.
                    self.status, self.reason = "blocked", "load_balancing_rearm_required"
                    return
                current, ranges = await self._read()
                owned = [self._record(item) for item in self.lease["owned"]]
                if current not in owned:
                    raise ValueError("meter_limitation_settings_changed")
                wanted = self._target_record(self.lease["original"], ranges)
                changed = (
                    any(
                        current[key] != wanted[key]
                        for key in (
                            "enabled",
                            "exp_soft_enabled",
                            "gen_soft_enabled",
                            "limit_per_phase",
                        )
                    )
                    or abs(float(current["exp_soft_limit"]) - float(wanted["exp_soft_limit"]))
                    >= _LIMIT_UPDATE_THRESHOLD_W
                )
                if changed:
                    updated = {**self.lease, "owned": [*owned[-3:], wanted]}
                    await self._save_lease(updated)
                    await self._write(wanted)
                    log_activity(
                        self.runtime, "load_balancing_observation",
                        f"Load balancing soft limit {int(current['exp_soft_limit'])} W → "
                        f"{int(wanted['exp_soft_limit'])} W",
                    )
                self.applied_limit_w = int(wanted["exp_soft_limit"])
                self.status, self.reason = "active", None
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.status = "blocked"
                self.reason = (
                    str(error)
                    if isinstance(error, ValueError)
                    else "meter_limitation_control_failed"
                )
        if self.status == "active":
            await self.idle_support.tick()

    async def deactivate(self) -> None:
        """Restore the exact pre-enable record when the current record is ours."""
        await self.idle_support.stop()
        async with self._lock:
            if self.lease is None:
                self.applied_limit_w = None
                self.status, self.reason = "disabled", None
                return
            current, _ = await self._read()
            original = self._record(self.lease["original"])
            owned = [self._record(item) for item in self.lease["owned"]]
            if current != original:
                if current not in owned:
                    changed = (self.status, self.reason) != (
                        "recovery_required", "meter_limitation_recovery_conflict"
                    )
                    self.status, self.reason = (
                        "recovery_required",
                        "meter_limitation_recovery_conflict",
                    )
                    if changed:
                        log_activity(
                            self.runtime, "load_balancing_observation",
                            "Load balancing restore needs recovery",
                            cause="meter_limitation_recovery_conflict: Gateway Service 120 "
                            "record no longer matches the saved lease",
                        )
                    raise ValueError("meter_limitation_recovery_conflict")
                await self._write(original)
            await self._save_lease(None)
            self.applied_limit_w = None
            self.status, self.reason = "disabled", None
            log_activity(
                self.runtime, "load_balancing_observation",
                "Load balancing off: original meter limitation restored",
                cause="Control is off and the Gateway record matched the saved lease",
            )

    async def stop(self) -> None:
        """Release the owned overlay during unload without touching another actor."""
        await self.deactivate()
