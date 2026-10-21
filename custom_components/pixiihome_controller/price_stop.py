"""Physical stop evidence independent of a cleared scheduler object."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from math import isfinite
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .runtime import RuntimeData


async def verify_price_stopped(
    runtime: RuntimeData, since: datetime, deadline: datetime | None,
) -> bool:
    """Require two new idle/near-zero core samples, before natural expiry.

    Never send a command here. A foreign service, stale sample, natural
    expiry or loss of health cannot establish physical early-stop success.
    ``deadline=None`` is used only for read-only expired-identity recovery.
    """
    previous: datetime | None = None
    try:
        async with asyncio.timeout(15):
            while deadline is None or datetime.now(UTC) < deadline:
                if not runtime.plant_health().healthy:
                    return False
                service = runtime.telemetry.value("core", "srv_type")
                power = runtime.telemetry.value("core", "ac_w")
                received = runtime.telemetry.received_at.get("core")
                if service not in {0, 101, 102, 104}:
                    return False
                idle = (
                    service == 0
                    and type(power) in {int, float} and isfinite(power) and abs(power) <= 50
                    and runtime.telemetry.is_fresh("core", max_age_seconds=10)
                    and received is not None and received > since
                    and (deadline is None or received < deadline)
                )
                if idle and received is not None:
                    if previous is not None and received > previous:
                        return True
                    previous = received
                else:
                    previous = None
                await asyncio.sleep(1)
    except TimeoutError:
        pass
    return False
