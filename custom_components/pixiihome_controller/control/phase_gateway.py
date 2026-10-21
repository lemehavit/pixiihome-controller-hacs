"""Narrow native adapter for expiring service-129 power corrections only."""

from __future__ import annotations

import asyncio
from math import isfinite
from typing import Any

from ..exceptions import SchedulerAuthenticationStage, SchedulerProtocolError

PHASE_FIELDS = ("act_enabled", "react_enabled", "duration", "keep_active") + tuple(
    f"{name}{i}" for name in ("act_ref", "act_type", "react_ref") for i in (1, 2, 3)
)
SERVICE_TYPES = {
    "demandresponse": 102,
    "peakshaving": 104,
    "adaptivepeakshaving": 105,
    "targetsoc": 107,
}


class PhaseGateway:
    def __init__(self, client: Any) -> None:
        self.client = client
        self.writes = 0

    async def message(self, msg: list[int], data: Any, *, batch: bool = False) -> Any:
        async with asyncio.timeout(3):
            await self.client._ensure_authenticated()
            cookies, headers = self.client._authenticated_read_context()
            if msg[2] == 6:
                if self.client._alevel is None or int(self.client._alevel) < 9:
                    raise ValueError("scheduler_installer_access_required")
                self.writes += 1
            item = {"msg": msg, "data": data}
            reply = await self.client._request_json(
                cookies["Sessid"],
                {"cmd": "message", "Omnibus": [item] if batch else item},
                cookies=cookies,
                headers=headers,
                stage=SchedulerAuthenticationStage.DETAIL,
            )
        if reply.get("status") == "NotLoggedOn":
            self.client._sessid = self.client._alevel = None
        omni = reply.get("Omnibus")
        if isinstance(omni, list) and len(omni) == 1:
            omni = omni[0]
        if not isinstance(omni, dict):
            raise SchedulerProtocolError("phase control acknowledgement unverified")
        ack = omni.get("msg")
        if (
            reply.get("status") != "ok"
            or not isinstance(ack, list)
            or len(ack) != 12
            or ack[2] != 7
            or ack[3:9] != msg[3:9]
        ):
            raise SchedulerProtocolError("phase control acknowledgement unverified")
        return omni.get("data")

    async def read(self, service: int) -> dict[str, Any]:
        if service not in {100, 102, 104, 105, 107, 129}:
            raise ValueError("phase_read_unsupported")
        data = await self.message(
            [1, 0, 5, 3, 255, 2, service, 200, 0, 33, 1 if service == 100 else 0, 0],
            0 if service == 100 else {},
        )
        if not isinstance(data, dict):
            raise SchedulerProtocolError("phase read invalid")
        return data

    async def core(self) -> dict[str, Any]:
        data = await self.message([1, 0, 5, 3, 255, 0, 24, 4, 0, 33, 0, 0], {})
        if not isinstance(data, dict):
            raise SchedulerProtocolError("phase core invalid")
        return data

    @staticmethod
    def settings(data: dict[str, Any]) -> dict[str, int | float]:
        if not all(type(data.get(k)) in (int, float) and isfinite(data[k]) for k in PHASE_FIELDS):
            raise ValueError("phase_settings_invalid")
        return {k: data[k] for k in PHASE_FIELDS}

    async def save(self, settings: dict[str, int | float]) -> None:
        self.settings(settings)
        await self.message([1, 0, 6, 3, 255, 2, 129, 200, 0, 33, 0, 0], {**settings, "enabled": 0})
        if self.settings(await self.read(129)) != settings:
            raise ValueError("phase_settings_changed")

    async def priority(self, used: list[int]) -> None:
        if (
            not used
            or len(used) > 32
            or len(set(used)) != len(used)
            or any(type(n) is not int or not 101 <= n <= 129 for n in used)
        ):
            raise ValueError("phase_priority_invalid")
        await self.message(
            [1, 0, 6, 3, 255, 2, 100, 200, 0, 33, 1, 0], {"used_list": used, "enabled": 1}
        )
        if (await self.read(100))["used_list"] != used:
            raise ValueError("phase_priority_changed")

    async def switch(self, on: bool) -> None:
        await self.message([1, 0, 6, 3, 255, 2, 129, 14 if on else 15, 0, 12, 1, 0], 1, batch=True)
