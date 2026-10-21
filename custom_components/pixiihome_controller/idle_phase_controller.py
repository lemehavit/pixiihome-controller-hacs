"""Short-lived Service 129 support when no native battery mode is running."""

from __future__ import annotations

import asyncio
from time import monotonic
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.storage import Store

from .control.load_balancing import finite
from .control.phase_balancing import plan_phases
from .control.phase_gateway import PhaseGateway

if TYPE_CHECKING:
    from .runtime import RuntimeData


_LEASE_SECONDS = 15


class IdlePhaseLoadBalancer:
    """Start only an expiring load-balancing lease for a live idle overload."""

    def __init__(self, runtime: RuntimeData) -> None:
        self.runtime = runtime
        self.store = Store(
            runtime.hass, 1, f"pixiihome_controller.{runtime.serial}.idle_phase_lease"
        )
        self.gateway: PhaseGateway | None = None
        self.lease: dict[str, Any] | None = None
        self.status = "disabled"
        self.reason: str | None = None
        self.targets: tuple[int, ...] | None = None
        self.previous: tuple[float, ...] | None = None
        self.last_tick: float | None = None
        self._lock = asyncio.Lock()

    def attributes(self) -> dict[str, Any]:
        return {
            "idle_overload_support": True,
            "idle_support_status": self.status,
            "idle_support_blocked_reason": self.reason,
            "idle_support_target_phase_power_w": self.targets,
            "idle_support_lease_seconds": _LEASE_SECONDS,
            "idle_support_active": self.lease is not None,
            "idle_support_writes": self.gateway.writes if self.gateway else 0,
            "old_mode_restarts": 0,
            "old_mode_timer_extensions": 0,
        }

    async def _persist(self, lease: dict[str, Any] | None) -> None:
        payload = {"lease": lease}
        await self.store.async_save(payload)
        if await self.store.async_load() != payload:
            raise ValueError("idle_phase_journal_invalid")
        self.lease = lease

    async def _recover(self) -> None:
        assert self.gateway is not None
        saved = await self.store.async_load()
        if saved is None:
            self.lease = None
            return
        if not isinstance(saved, dict) or set(saved) != {"lease"}:
            raise ValueError("idle_phase_journal_invalid")
        self.lease = saved["lease"]
        if self.lease is None:
            return
        await self.release()

    async def prepare(self) -> None:
        async with self._lock:
            scheduler = self.runtime.gateway_scheduler
            if scheduler is None:
                raise ValueError("native_meter_unavailable")
            self.gateway = PhaseGateway(scheduler.client)
            await self._recover()
            services = await self.gateway.read(100)
            settings = self.gateway.settings(await self.gateway.read(129))
            if (
                services.get("enabled") != 1
                or services.get("profile") != 0
                or services.get("running_profile") != 0
                or 129 in services.get("used_list", [])
                or settings["duration"] != 0
                or settings["keep_active"] != 0
                or settings["react_enabled"] != 0
            ):
                raise ValueError("phase_service_in_use")
            self.status, self.reason = "standby", None

    def _plan(self) -> tuple[int, ...] | None:
        observer = self.runtime.load_balancing_observer
        if observer is None:
            raise ValueError("load_balancing_configuration_required")
        snapshot = observer.snapshot
        usable_soc = snapshot.get("usable_soc_percent")
        charge = snapshot.get("battery_charge_capacity_dc_w")
        discharge = snapshot.get("battery_discharge_capacity_dc_w")
        if not all(finite(value) for value in (usable_soc, charge, discharge)):
            raise ValueError("battery_limits_unavailable")
        assert usable_soc is not None and charge is not None and discharge is not None
        if snapshot.get("battery_healthy") is not True:
            raise ValueError("battery_not_ready")
        config = observer.config
        if len(observer.samples) != config.phases:
            raise ValueError("phase_data_missing")
        configured_phase_cap = (
            max(config.max_charge_kw, config.max_discharge_kw) * 1000 / config.phases
        )
        core = self.runtime.telemetry.payload("core")
        reported = [core.get(f"grid_cap_w_{i}") for i in range(1, config.phases + 1)]
        phase_cap = min(
            [float(value) for value in reported if finite(value) and value > 0]
            or [configured_phase_cap]
        )
        now = monotonic()
        elapsed = 0 if self.last_tick is None else now - self.last_tick
        plan = plan_phases(
            config,
            observer.samples,
            (0.0,) * config.phases,
            usable_soc=float(usable_soc),
            charge_limit_w=float(charge) * 0.85,
            discharge_limit_w=float(discharge) * 0.85,
            phase_limit_w=min(phase_cap, configured_phase_cap),
            duration_seconds=_LEASE_SECONDS,
            previous_w=self.previous,
            elapsed_seconds=elapsed,
        )
        self.last_tick = now
        self.previous = plan.powers_w
        return plan.powers_w if plan.correction_required else None

    async def tick(self) -> None:
        async with self._lock:
            if self.gateway is None:
                self.status, self.reason = "blocked", "load_balancing_rearm_required"
                return
            try:
                core = await self.gateway.core()
                service = core.get("srv_type")
                if service not in (0, 129):
                    if self.lease is not None:
                        await self.release()
                    self.status, self.reason = "standby_native_mode_active", None
                    self.targets = None
                    return
                targets = self._plan()
                if targets is None:
                    if self.lease is not None:
                        await self.release()
                    self.status, self.reason = "standby", None
                    self.targets = None
                    return
                await self._apply(targets)
                self.targets = targets
                self.status, self.reason = "balancing_idle_overload", None
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.status = "blocked"
                self.reason = (
                    str(error) if isinstance(error, ValueError) else "idle_phase_control_failed"
                )
                if self.lease is not None:
                    try:
                        await self.release()
                    except Exception:
                        self.status = "recovery_required"
                        self.reason = "idle_phase_recovery_required"

    async def _apply(self, targets: tuple[int, ...]) -> None:
        assert self.gateway is not None
        services = await self.gateway.read(100)
        current = self.gateway.settings(await self.gateway.read(129))
        if self.lease is None:
            if (
                129 in services.get("used_list", [])
                or services.get("enabled") != 1
                or services.get("profile") != 0
                or services.get("running_profile") != 0
                or current["duration"] != 0
                or current["keep_active"] != 0
            ):
                raise ValueError("phase_service_in_use")
            priority = list(services["used_list"])
            lease = {
                "original": current,
                "priority": priority,
                # This service is idle-only assistance. Every existing service
                # must be able to preempt it, including an arriving schedule or
                # support activation while the short lease is still alive.
                "owned_priority": [*priority, 129],
                "owned": [current],
            }
            await self._persist(lease)
            await self.gateway.priority(lease["owned_priority"])
        else:
            if (
                current not in self.lease["owned"]
                or services.get("used_list") != self.lease["owned_priority"]
            ):
                raise ValueError("phase_settings_changed")
        lease = self.lease
        if lease is None:
            raise ValueError("idle_phase_journal_invalid")
        wanted = {
            **lease["original"],
            "act_enabled": 1,
            "react_enabled": 0,
            "duration": _LEASE_SECONDS,
            "keep_active": 0,
        }
        for index in (1, 2, 3):
            wanted.update(
                {
                    f"act_ref{index}": targets[index - 1] if index <= len(targets) else 0,
                    f"act_type{index}": 0,
                    f"react_ref{index}": 0,
                }
            )
        await self._persist({**lease, "owned": [*lease["owned"][-3:], wanted]})
        await self.gateway.save(wanted)
        await self.gateway.switch(True)
        if (await self.gateway.core()).get("srv_type") != 129:
            raise ValueError("phase_priority_not_effective")

    async def release(self) -> None:
        if self.lease is None or self.gateway is None:
            return
        saved = self.lease
        services = await self.gateway.read(100)
        current = self.gateway.settings(await self.gateway.read(129))
        if (
            current not in saved["owned"]
            and current != saved["original"]
            or services.get("used_list") not in (saved["priority"], saved["owned_priority"])
        ):
            raise ValueError("idle_phase_recovery_conflict")
        if (await self.gateway.core()).get("srv_type") == 129:
            await self.gateway.switch(False)
            for _ in range(3):
                if (await self.gateway.core()).get("srv_type") != 129:
                    break
                await asyncio.sleep(0.5)
            else:
                raise ValueError("idle_phase_recovery_pending")
        if current != saved["original"]:
            await self.gateway.save(saved["original"])
        if services.get("used_list") == saved["owned_priority"]:
            await self.gateway.priority(saved["priority"])
        await self._persist(None)
        self.targets = self.previous = None
        self.last_tick = None

    async def stop(self) -> None:
        async with self._lock:
            await self.release()
            self.status, self.reason = "disabled", None
