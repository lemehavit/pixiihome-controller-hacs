"""Read-only projection of price proposals and reconciled scheduler records."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import Any

from .control.price_optimization import PriceDirection


def price_timeline(controller: Any, now: datetime) -> list[dict[str, Any]]:
    """Keep actual schedules visible even when planning is paused or disabled.

    OWNED records were reconciled by a keyed Gateway GET. A computed proposal
    alone is never presented as a created schedule. No reads or writes occur.
    """
    from .price_controller import _action_key, _record_key, _record_start

    current = now.astimezone(UTC)
    ownership = controller.runtime.scheduler_ownership
    records = [] if ownership is None else [
        record for record in ownership.records.values()
        if record.owner == "automatic_price_optimization"
    ]
    entries: list[dict[str, Any]] = []
    keys: set[tuple[object, ...]] = set()
    plan = controller.plan
    planned = {
        **getattr(controller, "created_actions", {}),
        **({} if plan is None else {_action_key(action): action for action in plan.actions}),
    }
    for record in records:
        if record.lifecycle.value not in {"owned", "provisional", "conflicted"}:
            continue
        start = _record_start(record)
        duration = record.intended_state.get("duration")
        values = record.intended_state.get("values", {})
        if start is None or type(duration) is not int or duration <= 0:
            continue
        end = start + timedelta(seconds=duration)
        if end <= current:
            continue
        signed_power = values.get("pacref") if record.service == "demandresponse" else None
        if record.service == "peakshaving" and values.get("charge_max") == 0:
            signed_power = -float(values.get("discharge_max", 0))
        if (
            isinstance(signed_power, bool)
            or not isinstance(signed_power, int | float)
            or not isfinite(signed_power)
            or signed_power == 0
        ):
            continue
        key = _record_key(record)
        keys.add(key)
        entries.append({
            "uid": f"price-schedule-{record.m_id}",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "direction": "charge" if signed_power > 0 else "discharge",
            "power_kw": round(abs(signed_power) / 1000, 3),
            "service": record.service,
            "discharge_mode": (
                "self_consumption" if record.service == "peakshaving"
                else "export" if signed_power < 0 else None
            ),
            "schedule_status": "scheduled" if record.lifecycle.value == "owned" else "unverified",
            "message_id": record.m_id,
            "in_progress": start <= current < end,
            **_economics(planned.get(key)),
        })
    if (
        plan is not None
        and controller.runtime.controller_state.automatic_price_optimization_enabled
    ):
        for action in plan.actions:
            if action.end <= current or _action_key(action) in keys:
                continue
            entries.append({
                **action.as_dict(),
                "uid": f"price-proposal-{plan.fingerprint}-{action.start.timestamp()}",
                "schedule_status": "planned",
                "message_id": None,
                "in_progress": action.start <= current < action.end,
                **_economics(action),
            })
    return sorted(entries, key=lambda item: (item["start"], item["uid"]))


def _economics(action: Any) -> dict[str, Any]:
    """The plan's money figures for one interval; None when no plan has them.

    A discharge carries the profit of the whole cycle. A charge only buys the
    energy; its profit is counted on the discharge that uses it.
    """
    if action is None:
        return {"expected_profit_sek": None}
    hours = action.duration_seconds / 3600
    energy = action.power_kw * hours
    return {
        "expected_profit_sek": (
            round(action.expected_margin_sek, 2)
            if action.direction is PriceDirection.DISCHARGE else None
        ),
        "net_profit_sek_per_kwh": round(action.net_profit_sek_per_kwh, 4),
        "price_sek_per_kwh": round(action.price_sek_per_kwh, 4),
        "reference_price_sek_per_kwh": round(action.reference_price_sek_per_kwh, 4),
        "energy_kwh": round(energy, 2),
        "charge_cost_sek": (
            round(energy * action.price_sek_per_kwh, 2)
            if action.direction is PriceDirection.CHARGE else None
        ),
    }


def next_price_action(controller: Any, direction: PriceDirection) -> dict[str, Any] | None:
    """Return the current or next unexpired interval of one direction."""
    return next(
        (item for item in price_timeline(controller, datetime.now(UTC))
         if item["direction"] == direction.value),
        None,
    )
