"""Durable ownership of the APS entry added to the native priority list."""

from __future__ import annotations

from math import isfinite
from typing import TYPE_CHECKING, Any

from .activity import log_activity
from .control.phase_gateway import PhaseGateway

if TYPE_CHECKING:
    from .runtime import RuntimeData

APS_PRIORITY = "aps_priority"
APS_RECHARGE = "aps_recharge"


class ApsPriority:
    """Restore only an exact, journaled list; never infer a former configuration."""

    def __init__(self, runtime: RuntimeData) -> None:
        self.runtime = runtime
        self.status = "unchanged"
        self.reason: str | None = None

    def _saved(self) -> dict[str, Any] | None:
        journal = self.runtime.control_journal
        if journal is None:
            raise ValueError("aps_priority_journal_unavailable")
        saved = journal.controller_state.get(APS_PRIORITY)
        if saved is None:
            return None
        if not isinstance(saved, dict) or saved.get("owner") not in {"solar", "aps"}:
            raise ValueError("aps_priority_journal_invalid")
        for key in ("original", "owned"):
            values = saved.get(key)
            if (not isinstance(values, list) or not values or len(values) > 32
                    or any(type(n) is not int or not 101 <= n <= 129 for n in values)
                    or len(set(values)) != len(values)):
                raise ValueError("aps_priority_journal_invalid")
        if (saved["owned"].count(105) != 1 or 105 in saved["original"]
                or [n for n in saved["owned"] if n != 105] != saved["original"]):
            raise ValueError("aps_priority_journal_invalid")
        return saved

    def pending(self, owner: str | None = None) -> bool:
        journal = self.runtime.control_journal
        saved = None if journal is None else journal.controller_state.get(APS_PRIORITY)
        return bool(saved is not None and (
            owner is None or isinstance(saved, dict) and saved.get("owner") == owner
        ))

    def _activity(self, saved: dict[str, Any], outcome: str) -> None:
        owner = saved.get("owner")
        log_activity(
            self.runtime,
            "solar_charging_status" if owner == "solar" else "adaptive_peak_shaving_status",
            f"APS priority {outcome}; owner={owner}; original={saved.get('original')}; "
            f"written={saved.get('owned')}",
        )

    def attributes(self) -> dict[str, Any]:
        return {"aps_priority_status": self.status, "aps_priority_reason": self.reason}

    def _gateway(self) -> PhaseGateway:
        scheduler = self.runtime.gateway_scheduler
        if scheduler is None:
            raise ValueError("aps_gateway_connection_required")
        return PhaseGateway(scheduler.client)

    async def _release_idle(self) -> None:
        balancer = self.runtime.meter_limit_load_balancer
        if balancer is not None:
            idle = balancer.idle_support
            async with idle._lock:
                # Recover the independent journal before editing its base list.
                await idle.load_saved_lease()
                await idle.release()

    async def _error(self, saved: dict[str, Any] | None, error: Exception) -> None:
        self.status, self.reason = "recovery_required", str(error) or "aps_priority_read_failed"
        if saved is not None:
            self._activity(saved, f"recovery required: {self.reason}")
            try:
                await self.runtime.async_save_aps_state(
                    APS_PRIORITY, {**saved, "status": self.reason}
                )
            except Exception:
                pass  # The original write-ahead record must remain recoverable.

    async def acquire(self, owner: str, build: Any) -> tuple[tuple[int, ...], bool]:
        async with self.runtime.native_priority_lock:
            saved = None
            try:
                saved = self._saved()
                await self._release_idle()
                gateway = self._gateway()
                priority = await gateway.read(100)
                if priority.get("enabled") != 1:
                    raise ValueError("aps_service_priority_disabled")
                current = priority.get("used_list")
                if saved is not None:
                    if saved["owner"] != owner or current != saved["owned"]:
                        raise ValueError("aps_priority_recovery_conflict")
                    self.status, self.reason = "owned", None
                    return tuple(saved["owned"]), True
                wanted, changed = build(current)
                if changed:
                    saved = {"owner": owner, "original": current, "owned": list(wanted),
                             "status": "write_pending"}
                    await self.runtime.async_save_aps_state(APS_PRIORITY, saved)
                    self._activity(saved, "ownership recorded before write")
                    # Compare once more after the durable write-ahead commit.
                    latest = await gateway.read(100)
                    if latest.get("enabled") != 1 or latest.get("used_list") != current:
                        raise ValueError("aps_priority_recovery_conflict")
                    await gateway.priority(list(wanted))
                    self._activity(saved, "written and verified")
                    await self.runtime.async_save_aps_state(
                        APS_PRIORITY, {**saved, "status": "owned"}
                    )
                self.status, self.reason = ("owned" if changed else "preexisting"), None
                return wanted, changed
            except Exception as error:
                await self._error(saved, error)
                raise

    async def release(self, owner: str | None = None) -> None:
        async with self.runtime.native_priority_lock:
            saved = None
            try:
                saved = self._saved()
                if saved is None or owner is not None and saved["owner"] != owner:
                    return
                await self._release_idle()
                gateway = self._gateway()
                current = await gateway.read(100)
                if current.get("enabled") != 1 or current.get("used_list") not in (
                    saved["original"], saved["owned"]
                ):
                    raise ValueError("aps_priority_recovery_conflict")
                if current["used_list"] == saved["owned"]:
                    await self.runtime.async_save_aps_state(
                        APS_PRIORITY, {**saved, "status": "restore_pending"}
                    )
                    current = await gateway.read(100)
                    if current.get("enabled") != 1 or current.get("used_list") != saved["owned"]:
                        raise ValueError("aps_priority_recovery_conflict")
                    await gateway.priority(saved["original"])
                await self.runtime.async_save_aps_state(APS_PRIORITY, None)
                self.status, self.reason = "restored", None
                self._activity(saved, "original restored and ownership released")
            except Exception as error:
                await self._error(saved, error)
                raise


async def retire_recharged_context(runtime: Any) -> bool:
    """A fresh SOC rise consumes the old discharge evidence, durably."""
    context = getattr(runtime, "aps_recharge_context", None)
    fresh = getattr(getattr(runtime, "telemetry", None), "is_fresh", None)
    if not isinstance(context, dict) or not callable(fresh):
        return False
    measured = context.get("usable_soc_percent")
    if not isinstance(measured, int | float) or type(measured) not in (int, float):
        return False
    measured = float(measured)
    soc = runtime.battery.usable_soc
    if (soc is None
            or not isfinite(measured) or not isfinite(soc)
            or not fresh("battery") or not fresh("core") or soc <= measured + 0.5):
        return False
    await runtime.async_save_aps_state(APS_RECHARGE, None)
    runtime.aps_recharge_context = None
    price = getattr(runtime, "price_optimization_controller", None)
    if price is not None:
        price.request_refresh()
    return True


def priority_attributes(runtime: Any) -> dict[str, Any]:
    manager = getattr(runtime, "aps_priority", None)
    return manager.attributes() if manager is not None else {}
