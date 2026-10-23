"""Explicit, one-use HTTPS schedule operations using the Gateway's own UI contract.

These operations address sch_index directly. They never adopt external schedules
or translate a scheduler index into an MQTT message ID.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
from math import isfinite
from time import monotonic, time
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import HomeAssistantError

from ..control.journal import JournalPhase, JournalTargetIdentity
from ..control.models import AdapterCapability, ControlOperation, ScheduleOrigin
from ..control.policy import evaluate_control_operation
from ..exceptions import SchedulerAuthenticationStage, SchedulerProtocolError
from ..protocol.scheduler import (
    build_detail_request,
    parse_detail_response,
    scheduler_epoch_from_gateway_wire,
    scheduler_epoch_to_gateway_wire,
)
from .client import AiohttpSchedulerReadClient
from .manager import SchedulerListResponseTooLargeError

if TYPE_CHECKING:
    from ..runtime import RuntimeData

_DETAIL_KEYS = {
    "asset_id",
    "duration",
    "service_enabled",
    "service_no",
    "service_prio",
    "service_type",
    "source_id",
    "start_time",
    "sch_index",
    "rec_type",
    "recurrence",
    "recurrence1",
    "recurrence2",
    "recurrence3",
    "recurrence4",
    "rec_end_time",
    # Numeric constraint metadata observed on Gateway 2.3.63 DETAIL readback.
    "duration__min",
    "rec_end_time__min",
    "rec_type__max",
    "rec_type__min",
    "recurrence1__min",
    "recurrence2__min",
    "recurrence3__min",
    "service_no__min",
    "start_time__min",
}
_CHANGE_KEYS = {"start_time", "duration"}
_MAX_INTEGER = 2_147_483_647
_SOC_SCOPE_ENTRY_LIMIT = 5000
_GENERAL_SERVICE_WRITE_KEYS = ("soc_lim_en", "ref_delay", "power_share")
_METER_LIMITATION_WRITE_KEYS = (
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
_LOAD_BALANCING_REFERENCE_DELAY_SECONDS = 10
_METER_LIMITATION_MAX_TRIAL_CORRECTION_W = 300
_METER_LIMITATION_TRIAL_SECONDS = 20


def validate_changes(
    changes: dict[str, Any],
    record: dict[str, Any] | None = None,
) -> dict[str, int]:
    """Limit edits to the source-evidenced move/resize operation."""
    if not changes or not set(changes) <= _CHANGE_KEYS:
        raise HomeAssistantError("scheduler_edit_requires_start_time_or_duration")
    if any(type(value) is not int or not 0 < value <= _MAX_INTEGER for value in changes.values()):
        raise HomeAssistantError("scheduler_edit_value_invalid")
    if record is not None:
        for key, value in changes.items():
            if value < record.get(f"{key}__min", 0):
                raise HomeAssistantError("scheduler_edit_below_gateway_minimum")
    return dict(changes)


class AiohttpSchedulerGatewayClient(AiohttpSchedulerReadClient):
    """Extend the read client with separately invoked Gateway UI operations."""

    async def read_load_balancing_status(
        self,
        *,
        phase_service: bool = False,
        meter_limitation: bool = False,
        general_service_settings: bool = False,
    ) -> dict[str, Any]:
        """Read native load-balancing evidence through an exact GET; never SET."""
        if sum((phase_service, meter_limitation, general_service_settings)) > 1:
            raise ValueError("only one load-balancing capability can be read at a time")
        await self._ensure_authenticated()
        cookies, headers = self._authenticated_read_context()
        message = (
            [1, 0, 5, 3, 255, 2, 129, 200, 0, 33, 0, 0]
            if phase_service
            else [1, 0, 5, 3, 255, 2, 120, 200, 0, 33, 0, 0]
            if meter_limitation
            else [1, 0, 5, 3, 255, 2, 0, 200, 0, 33, 0, 0]
            if general_service_settings
            else [1, 0, 5, 3, 255, 0, 24, 4, 0, 33, 0, 0]
        )
        response = await self._request_json(
            cookies["Sessid"], {"cmd": "message", "Omnibus": {"msg": message, "data": {}}},
            cookies=cookies, headers=headers, stage=SchedulerAuthenticationStage.DETAIL,
        )
        if response.get("status") == "NotLoggedOn":
            self._sessid = self._alevel = None
        omnibus = response.get("Omnibus", {})
        ack = omnibus.get("msg") if isinstance(omnibus, dict) else None
        data = omnibus.get("data") if isinstance(omnibus, dict) else None
        if (
            response.get("status") != "ok"
            or not isinstance(ack, list) or len(ack) != 12
            or ack[2] != 7 or ack[3:9] != message[3:9]
            or not isinstance(data, dict)
        ):
            raise SchedulerProtocolError("load balancing read response unverified")
        meter_limitation_keys = {
            "enabled",
            "meter_id",
            "limit_per_phase",
            "exp_soft_enabled",
            "exp_soft_limit",
            "exp_hard_enabled",
            "exp_hard_limit",
            "gen_soft_enabled",
            "gen_soft_limit",
            "gen_hard_enabled",
            "gen_hard_limit",
        }
        meter_limitation_range_keys = {
            f"{key}{suffix}"
            for key in {
                "meter_id",
                "limit_per_phase",
                "exp_soft_limit",
                "exp_hard_limit",
                "gen_soft_limit",
                "gen_hard_limit",
            }
            for suffix in ("__min", "__max")
        }
        general_service_keys = {
            "soc_lim_en",
            "ref_delay",
            "ref_delay__min",
            "ref_delay__max",
            "power_share",
            "power_share__min",
            "power_share__max",
            "meter_other_srv",
            "meter_other_srv__min",
            "meter_other_srv__max",
        }
        keys = (
            {"enabled", "act_enabled", "react_enabled", "duration", "keep_active"}
            | {f"act_{name}{i}" for name in ("ref", "type") for i in range(1, 4)}
            if phase_service
            else meter_limitation_keys | meter_limitation_range_keys
            if meter_limitation
            else general_service_keys
            if general_service_settings
            else {"batt_soc", "srv_type"}
            | {
                f"{name}{i}"
                for name in ("meter_a", "meter_v", "meter_w", "ac_w")
                for i in range(1, 4)
            }
        )
        return {
            key: data[key]
            for key in keys
            if key in data and type(data[key]) in (int, float) and isfinite(data[key])
        }

    @staticmethod
    def _general_service_write_record(values: Mapping[str, Any]) -> dict[str, int | float]:
        """Validate the exact installer-level payload used by Gateway 2.3.63."""
        if not all(key in values for key in _GENERAL_SERVICE_WRITE_KEYS):
            raise SchedulerProtocolError("general service settings incomplete")
        record = {key: values[key] for key in _GENERAL_SERVICE_WRITE_KEYS}
        if record["soc_lim_en"] not in (0, 1) or type(record["ref_delay"]) is not int:
            raise SchedulerProtocolError("general service settings invalid")
        if (
            isinstance(record["power_share"], bool)
            or not isinstance(record["power_share"], (int, float))
            or not isfinite(record["power_share"])
        ):
            raise SchedulerProtocolError("general service settings invalid")
        return record

    async def _write_general_service_settings(
        self, record: Mapping[str, int | float]
    ) -> None:
        """Write one exact, fully preserved installer-level settings record."""
        data = self._general_service_write_record(record)
        await self._ensure_authenticated()
        if self._alevel is None or int(self._alevel) < 9:
            raise HomeAssistantError("scheduler_installer_access_required")
        cookies, headers = self._authenticated_read_context()
        message = [1, 0, 6, 3, 255, 2, 0, 200, 0, 33, 0, 0]
        response = await self._request_json(
            cookies["Sessid"],
            {"cmd": "message", "Omnibus": {"msg": message, "data": data}},
            cookies=cookies,
            headers=headers,
            stage=SchedulerAuthenticationStage.DETAIL,
        )
        omnibus = response.get("Omnibus", {})
        ack = omnibus.get("msg") if isinstance(omnibus, dict) else None
        if (
            response.get("status") != "ok"
            or not isinstance(ack, list)
            or len(ack) != 12
            or any(type(value) is not int for value in ack)
            or ack[2] != 7
            or ack[3:9] != message[3:9]
        ):
            raise SchedulerProtocolError(
                "general service settings write acknowledgement unverified"
            )

    async def set_service_reference_delay(self, expected_current: int) -> dict[str, Any]:
        """Set the documented AMS delay with exact readback and bounded rollback."""
        if type(expected_current) is not int or not 0 <= expected_current <= 60:
            raise HomeAssistantError("service_reference_delay_expected_value_invalid")
        before_values = await self.read_load_balancing_status(
            general_service_settings=True
        )
        before = self._general_service_write_record(before_values)
        if before["ref_delay"] != expected_current:
            raise HomeAssistantError("service_reference_delay_changed")
        target = _LOAD_BALANCING_REFERENCE_DELAY_SECONDS
        minimum = before_values.get("ref_delay__min")
        maximum = before_values.get("ref_delay__max")
        if (minimum is not None and target < minimum) or (
            maximum is not None and target > maximum
        ):
            raise HomeAssistantError("service_reference_delay_outside_gateway_range")
        if expected_current == target:
            return {
                "status": "already_configured",
                "changed": False,
                "before": expected_current,
                "after": target,
                "preserved": True,
                "acknowledgement_verified": True,
            }

        requested = {**before, "ref_delay": target}
        write_error: Exception | None = None
        try:
            await self._write_general_service_settings(requested)
        except Exception as err:  # noqa: BLE001 - exact readback resolves uncertain ACK
            write_error = err
        try:
            after_values = await self.read_load_balancing_status(
                general_service_settings=True
            )
            after = self._general_service_write_record(after_values)
        except Exception as err:
            raise SchedulerProtocolError(
                "service reference delay write outcome unverified"
            ) from (write_error or err)
        if after == requested:
            return {
                "status": "configured",
                "changed": True,
                "before": expected_current,
                "after": target,
                "preserved": True,
                "acknowledgement_verified": write_error is None,
            }
        if after == before:
            if write_error is not None:
                raise write_error
            raise SchedulerProtocolError("service reference delay write had no effect")

        try:
            await self._write_general_service_settings(before)
            rolled_back = self._general_service_write_record(
                await self.read_load_balancing_status(general_service_settings=True)
            )
        except Exception as err:
            raise SchedulerProtocolError(
                "service reference delay rollback unverified"
            ) from err
        if rolled_back != before:
            raise SchedulerProtocolError("service reference delay rollback mismatch")
        raise SchedulerProtocolError("service reference delay mismatch rolled back")

    @staticmethod
    def _meter_limitation_write_record(
        values: Mapping[str, Any],
    ) -> dict[str, int | float]:
        """Validate the complete payload used by the Gateway web client."""
        if not all(key in values for key in _METER_LIMITATION_WRITE_KEYS):
            raise SchedulerProtocolError("meter limitation settings incomplete")
        record = {key: values[key] for key in _METER_LIMITATION_WRITE_KEYS}
        for key in (
            "enabled",
            "exp_soft_enabled",
            "exp_hard_enabled",
            "gen_soft_enabled",
            "gen_hard_enabled",
            "limit_per_phase",
        ):
            if type(record[key]) is not int or record[key] not in (0, 1):
                raise SchedulerProtocolError("meter limitation settings invalid")
        if type(record["meter_id"]) is not int or record["meter_id"] < 0:
            raise SchedulerProtocolError("meter limitation settings invalid")
        for key in (
            "exp_soft_limit",
            "exp_hard_limit",
            "gen_soft_limit",
            "gen_hard_limit",
        ):
            value = record[key]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(value)
                or value < 0
            ):
                raise SchedulerProtocolError("meter limitation settings invalid")
        return record

    async def _write_meter_limitation_settings(
        self, record: Mapping[str, int | float]
    ) -> None:
        """Write one complete web-client payload with exact acknowledgement."""
        data = self._meter_limitation_write_record(record)
        await self._ensure_authenticated()
        if self._alevel is None or int(self._alevel) < 9:
            raise HomeAssistantError("scheduler_installer_access_required")
        cookies, headers = self._authenticated_read_context()
        message = [1, 0, 6, 3, 255, 2, 120, 200, 0, 33, 0, 0]
        response = await self._request_json(
            cookies["Sessid"],
            {"cmd": "message", "Omnibus": {"msg": message, "data": data}},
            cookies=cookies,
            headers=headers,
            stage=SchedulerAuthenticationStage.DETAIL,
        )
        omnibus = response.get("Omnibus", {})
        ack = omnibus.get("msg") if isinstance(omnibus, dict) else None
        if (
            response.get("status") != "ok"
            or not isinstance(ack, list)
            or len(ack) != 12
            or any(type(value) is not int for value in ack)
            or ack[2] != 7
            or ack[3:9] != message[3:9]
        ):
            raise SchedulerProtocolError(
                "meter limitation write acknowledgement unverified"
            )

    async def write_meter_limitation_settings(
        self, record: Mapping[str, int | float]
    ) -> dict[str, int | float]:
        """Write and read back one complete Service 120 record exactly."""
        requested = self._meter_limitation_write_record(record)
        await self._write_meter_limitation_settings(requested)
        actual = self._meter_limitation_write_record(
            await self.read_load_balancing_status(meter_limitation=True)
        )
        if actual != requested:
            raise SchedulerProtocolError("meter limitation write readback mismatch")
        return actual

    async def run_meter_limitation_soft_limit_trial(
        self, direction: str, max_correction_w: int
    ) -> dict[str, Any]:
        """Run one bounded soft-limit trial and always restore the full record."""
        if direction not in {"exp", "gen"}:
            raise HomeAssistantError("meter_limitation_trial_direction_invalid")
        if (
            type(max_correction_w) is not int
            or not 50 <= max_correction_w <= _METER_LIMITATION_MAX_TRIAL_CORRECTION_W
        ):
            raise HomeAssistantError("meter_limitation_trial_correction_invalid")

        general = await self.read_load_balancing_status(general_service_settings=True)
        if general.get("ref_delay") != _LOAD_BALANCING_REFERENCE_DELAY_SECONDS:
            raise HomeAssistantError("meter_limitation_reference_delay_invalid")
        before_values = await self.read_load_balancing_status(meter_limitation=True)
        before = self._meter_limitation_write_record(before_values)
        if (
            before.get("enabled") != 0
            or before_values.get("limit_per_phase") != 0
            or any(
                before.get(key) != 0
                for key in (
                    "exp_soft_enabled",
                    "exp_hard_enabled",
                    "gen_soft_enabled",
                    "gen_hard_enabled",
                )
            )
        ):
            raise HomeAssistantError("meter_limitation_trial_requires_pristine_state")

        baseline = await self.read_load_balancing_status()
        required = {
            *(f"meter_w{i}" for i in range(1, 4)),
            *(f"meter_a{i}" for i in range(1, 4)),
            *(f"ac_w{i}" for i in range(1, 4)),
            "srv_type",
        }
        if not required <= baseline.keys():
            raise HomeAssistantError("meter_limitation_trial_telemetry_incomplete")
        total_meter_w = sum(float(baseline[f"meter_w{i}"]) for i in range(1, 4))
        magnitude_w = abs(total_meter_w)
        if magnitude_w < max_correction_w + 200 or magnitude_w > 50_000:
            raise HomeAssistantError("meter_limitation_trial_grid_flow_outside_bounds")
        soft_limit_w = int(round(magnitude_w - max_correction_w))
        requested = {
            **before,
            "enabled": 1,
            f"{direction}_soft_enabled": 1,
            f"{direction}_soft_limit": soft_limit_w,
        }

        samples: list[dict[str, Any]] = []
        started = monotonic()
        write_verified = False
        restored: dict[str, int | float] | None = None
        try:
            await self._write_meter_limitation_settings(requested)
            active_values = await self.read_load_balancing_status(meter_limitation=True)
            active = self._meter_limitation_write_record(active_values)
            if active != requested or active_values.get("limit_per_phase") != 0:
                raise SchedulerProtocolError("meter limitation trial readback mismatch")
            write_verified = True
            for _ in range(5):
                status = await self.read_load_balancing_status()
                samples.append(
                    {
                        "elapsed_seconds": round(monotonic() - started, 3),
                        "srv_type": status.get("srv_type"),
                        "batt_soc": status.get("batt_soc"),
                        "meter_w": [status.get(f"meter_w{i}") for i in range(1, 4)],
                        "meter_a": [status.get(f"meter_a{i}") for i in range(1, 4)],
                        "ac_w": [status.get(f"ac_w{i}") for i in range(1, 4)],
                    }
                )
                if len(samples) < 5:
                    await asyncio.sleep(_METER_LIMITATION_TRIAL_SECONDS / 4)
        finally:
            await asyncio.shield(self._write_meter_limitation_settings(before))
            restored_values = await asyncio.shield(
                self.read_load_balancing_status(meter_limitation=True)
            )
            restored = self._meter_limitation_write_record(restored_values)
            if restored != before or restored_values.get("limit_per_phase") != 0:
                raise SchedulerProtocolError("meter limitation trial rollback mismatch")

        return {
            "status": "completed_and_restored",
            "direction": direction,
            "max_correction_w": max_correction_w,
            "soft_limit_w": soft_limit_w,
            "baseline": baseline,
            "samples": samples,
            "write_verified": write_verified,
            "hard_limits_enabled": False,
            "restored": restored == before,
            "duration_seconds": round(monotonic() - started, 3),
        }

    async def read_record(self, sch_index: int) -> dict[str, Any]:
        await self._ensure_authenticated()
        cookies, headers = self._authenticated_read_context()
        response = await self._request_json(
            cookies["Sessid"],
            build_detail_request(sch_index),
            cookies=cookies,
            headers=headers,
            stage=SchedulerAuthenticationStage.DETAIL,
        )
        detail = parse_detail_response(response)
        if response.get("status") != "ok" or detail.sch_index != sch_index:
            raise SchedulerProtocolError("scheduler DETAIL identity invalid")
        data = dict(response["Omnibus"]["data"])
        if not set(data) <= _DETAIL_KEYS or detail.asset_id != data.get("asset_id"):
            raise SchedulerProtocolError("scheduler DETAIL write schema unsupported")
        for key, value in data.items():
            if key not in {"asset_id", "recurrence"} and (
                type(value) is not int or not 0 <= value <= _MAX_INTEGER
            ):
                raise SchedulerProtocolError("scheduler DETAIL write value invalid")
        if data.get("service_enabled") not in (0, 1):
            raise SchedulerProtocolError("scheduler DETAIL enabled value invalid")
        if "sch_index" in data and data["sch_index"] != sch_index:
            raise SchedulerProtocolError("scheduler DETAIL identity mismatch")
        # The existing parser bounds and sanitizes nested recurrence metadata.
        # Keep the full write record, but expose canonical UTC to HA callers.
        canonical = deepcopy(data)
        canonical["start_time"] = scheduler_epoch_from_gateway_wire(
            canonical["start_time"]
        )
        return canonical

    async def read_target_soc(self, service_no: int) -> dict[str, Any]:
        """Read the Target SoC parameter record addressed by the schedule service_no."""
        if type(service_no) is not int or not 0 <= service_no <= _MAX_INTEGER:
            raise SchedulerProtocolError("scheduler Target SoC identity invalid")
        await self._ensure_authenticated()
        cookies, headers = self._authenticated_read_context()
        message = [1, 0, 5, 3, 255, 2, 107, 200, service_no, 33, 1, 0]
        response = await self._request_json(
            cookies["Sessid"],
            {"cmd": "message", "Omnibus": {"msg": message, "data": 0}},
            cookies=cookies,
            headers=headers,
            stage=SchedulerAuthenticationStage.DETAIL,
        )
        data = self._target_soc_response(response, message)
        return deepcopy(data)

    @staticmethod
    def _target_soc_response(response: Mapping[str, Any], message: list[int]) -> dict[str, Any]:
        omnibus = response.get("Omnibus", {})
        ack = omnibus.get("msg") if isinstance(omnibus, dict) else None
        data = omnibus.get("data") if isinstance(omnibus, dict) else None
        if (
            response.get("status") != "ok"
            or not isinstance(ack, list)
            or len(ack) != 12
            or ack[2] != 7
            or ack[3:9] != message[3:9]
            or not isinstance(data, dict)
        ):
            raise SchedulerProtocolError("scheduler Target SoC response unverified")
        value = data.get("reference")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not isfinite(value)
            or not 0 <= value <= 100
        ):
            raise SchedulerProtocolError("scheduler Target SoC reference invalid")
        return data

    async def write_target_soc(self, service_no: int, reference: int | float) -> None:
        """Use the installed Gateway UI's partial reference update, once."""
        if type(service_no) is not int or not 0 <= service_no <= _MAX_INTEGER:
            raise SchedulerProtocolError("scheduler Target SoC identity invalid")
        if (
            isinstance(reference, bool)
            or not isinstance(reference, (int, float))
            or not isfinite(reference)
            or not 0 <= reference <= 100
        ):
            raise HomeAssistantError("scheduler_soc_value_invalid")
        await self._ensure_authenticated()
        if self._alevel is None or int(self._alevel) < 9:
            raise HomeAssistantError("scheduler_installer_access_required")
        cookies, headers = self._authenticated_read_context()
        message = [1, 0, 6, 3, 255, 2, 107, 200, service_no, 33, 0, 0]
        response = await self._request_json(
            cookies["Sessid"],
            {"cmd": "message", "Omnibus": {"msg": message, "data": {"reference": reference}}},
            cookies=cookies,
            headers=headers,
            stage=SchedulerAuthenticationStage.DETAIL,
        )
        omnibus = response.get("Omnibus", {})
        ack = omnibus.get("msg") if isinstance(omnibus, dict) else None
        if (
            response.get("status") != "ok"
            or not isinstance(ack, list)
            or len(ack) != 12
            or ack[2] != 7
            or ack[3:9] != message[3:9]
        ):
            raise SchedulerProtocolError("scheduler Target SoC write acknowledgement unverified")

    async def write_record(
        self,
        sch_index: int,
        before: dict[str, Any],
        changes: dict[str, Any] | None,
    ) -> None:
        """Send once; retrying an uncertain write is deliberately unsupported."""
        if type(sch_index) is not int or not 0 <= sch_index <= _MAX_INTEGER:
            raise SchedulerProtocolError("scheduler target invalid")
        if changes is None:
            message = [1, 0, 6, 3, 255, 2, 101, 51, sch_index, 7, 1, 0]
            data: Any = sch_index
        else:
            message = [1, 1, 6, 3, 255, 2, 101, 200, sch_index, 33, 0, 0]
            data = {**deepcopy(before), **validate_changes(changes, before)}
            data["start_time"] = scheduler_epoch_to_gateway_wire(data["start_time"])
        await self._ensure_authenticated()
        if self._alevel is None or int(self._alevel) < 9:
            raise HomeAssistantError("scheduler_installer_access_required")
        cookies, headers = self._authenticated_read_context()
        response = await self._request_json(
            cookies["Sessid"],
            {"cmd": "message", "Omnibus": {"msg": message, "data": data}},
            cookies=cookies,
            headers=headers,
            stage=SchedulerAuthenticationStage.DETAIL,
        )
        omnibus = response.get("Omnibus")
        ack = omnibus.get("msg") if isinstance(omnibus, dict) else None
        if (
            response.get("status") != "ok"
            or not isinstance(ack, list)
            or len(ack) != 12
            or any(type(value) is not int for value in ack)
            or ack[2] != 7
            or ack[3:9] != message[3:9]
        ):
            raise SchedulerProtocolError("scheduler write acknowledgement unverified")


class GatewaySchedulerController:
    """Bind each admin approval to fresh exact state and a durable transaction."""

    def __init__(self, runtime: RuntimeData, client: AiohttpSchedulerGatewayClient) -> None:
        self.runtime = runtime
        self.client = client
        self._previews: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    def _reasons(self, operation: ControlOperation) -> list[str]:
        context = self.runtime.scheduler_control_context(operation)
        context = replace(
            context,
            adapter_capability=AdapterCapability.SUPPORTED,
            # This is explicit authority over an observed target, never ownership.
            origin=ScheduleOrigin.EXTERNAL_OBSERVED,
            grant_authorized=True,
            operation_impact=replace(context.operation_impact, exact_target=True),
        )
        reasons = list(evaluate_control_operation(context).blocked_reasons)
        if self.client._alevel is None or int(self.client._alevel) < 9:
            reasons.append("scheduler_installer_access_required")
        return reasons

    async def set_service_reference_delay(self, expected_current: int) -> dict[str, Any]:
        """Serialize the one-shot prerequisite update with scheduler mutations."""
        async with self._lock:
            return await self.client.set_service_reference_delay(expected_current)

    async def move_managed_target_soc(
        self,
        *,
        sch_index: int,
        expected_source_id: int,
        expected_start_epoch: int,
        expected_duration_seconds: int,
        expected_reference: float,
        new_start_epoch: int,
    ) -> dict[str, Any]:
        """Move one explicitly managed future one-shot Target SoC schedule."""
        values = (
            sch_index,
            expected_source_id,
            expected_start_epoch,
            expected_duration_seconds,
            new_start_epoch,
        )
        if any(type(value) is not int or value < 0 for value in values):
            raise HomeAssistantError("scheduler_managed_move_identity_invalid")
        if (
            isinstance(expected_reference, bool)
            or not isinstance(expected_reference, int | float)
            or not isfinite(expected_reference)
            or not 0 <= expected_reference <= 100
        ):
            raise HomeAssistantError("scheduler_managed_move_reference_invalid")
        if expected_duration_seconds <= 0 or new_start_epoch <= int(time()):
            raise HomeAssistantError("scheduler_managed_move_time_invalid")
        async with self._lock:
            before = await self._observe(
                sch_index,
                max(0, expected_start_epoch - 1),
                expected_start_epoch + expected_duration_seconds + 1,
            )
            recurrence = before.get("recurrence")
            one_shot = (
                isinstance(recurrence, Mapping) and recurrence.get("type") == "once"
            ) or before.get("rec_type") == 0
            if (
                before.get("source_id") != expected_source_id
                or before.get("start_time") != expected_start_epoch
                or before.get("duration") != expected_duration_seconds
                or before.get("service_type") != 107
                or before.get("service_enabled") != 1
                or not one_shot
            ):
                raise HomeAssistantError("scheduler_managed_move_target_changed")
            reference = await self.client.read_target_soc(before["service_no"])
            if float(reference["reference"]) != float(expected_reference):
                raise HomeAssistantError("scheduler_managed_move_target_changed")
            manager = self.runtime.scheduler_manager
            if manager is None:
                raise HomeAssistantError("scheduler_unavailable")
            destination = await manager.refresh_list(
                max(0, new_start_epoch - 1),
                new_start_epoch + expected_duration_seconds + 1,
            )
            if any(
                entry.sch_index != sch_index
                and entry.enabled
                and entry.start_epoch < new_start_epoch + expected_duration_seconds
                and new_start_epoch < entry.start_epoch + entry.duration_seconds
                for entry in destination
            ):
                raise HomeAssistantError("scheduler_managed_move_destination_occupied")
            reasons = self._reasons(ControlOperation.UPDATE)
            if reasons:
                raise HomeAssistantError(", ".join(reasons))
            after = {**before, "start_time": new_start_epoch}
            authority_revision = self.runtime.production_authority.revision
            transaction_id = f"price-managed-scheduler-{secrets.token_hex(16)}"
            await self.runtime.begin_control_transaction(
                transaction_id=transaction_id,
                operation="automatic_price_external_schedule_move",
                target_identity=JournalTargetIdentity(
                    sch_index=sch_index,
                    service_no=before["service_no"],
                    source_id=expected_source_id,
                    asset_id=before.get("asset_id"),
                ),
                intent={
                    "before": before,
                    "after": after,
                    "target_soc_reference": expected_reference,
                },
            )
            await self.runtime.transition_control_transaction(
                transaction_id,
                JournalPhase.COMMAND_SENT,
            )
            if (
                authority_revision != self.runtime.production_authority.revision
                or not self.runtime.production_control_enabled()
            ):
                await self.runtime.transition_control_transaction(
                    transaction_id,
                    JournalPhase.FAILED,
                    reason="authority_changed_before_send",
                )
                raise HomeAssistantError("production_control_disabled")
            try:
                await self.client.write_record(
                    sch_index,
                    before,
                    {"start_time": new_start_epoch},
                )
                await self.runtime.transition_control_transaction(
                    transaction_id,
                    JournalPhase.ACKNOWLEDGED,
                )
                start = min(expected_start_epoch, new_start_epoch) - 1
                end = max(
                    expected_start_epoch + expected_duration_seconds,
                    new_start_epoch + expected_duration_seconds,
                ) + 1
                verified = await self._observe(sch_index, max(0, start), end) == after
                if verified:
                    verified = float(
                        (await self.client.read_target_soc(before["service_no"]))["reference"]
                    ) == float(expected_reference)
                await self.runtime.transition_control_transaction(
                    transaction_id,
                    JournalPhase.RECONCILED if verified else JournalPhase.RECOVERY_REQUIRED,
                    reason="readback_verified" if verified else "readback_mismatch",
                )
            except Exception:  # noqa: BLE001 - never retry uncertain delivery
                await self.runtime.transition_control_transaction(
                    transaction_id,
                    JournalPhase.RECOVERY_REQUIRED,
                    reason="managed_scheduler_delivery_or_readback_unknown",
                )
                return {"status": "recovery_required", "transaction_id": transaction_id}
            return {
                "status": "reconciled" if verified else "recovery_required",
                "transaction_id": transaction_id,
                "sch_index": sch_index,
                "start_epoch": new_start_epoch,
            }

    async def set_external_target_soc(
        self,
        *,
        sch_index: int,
        expected_service_no: int,
        expected_start_epoch: int,
        expected_duration_seconds: int,
        expected_reference: float,
        new_reference: float,
        allow_delete: bool,
        delete: bool = False,
    ) -> dict[str, Any]:
        """Change one future foreign one-shot Target SoC reference, or delete it.

        The price optimizer uses this only when explicitly allowed. The exact
        schedule, its unshared parameter record and the current reference are
        re-read first. Each write is sent once and verified by readback; an
        uncertain result is journaled for recovery and never retried. Deletion
        is attempted when a verified reference write had no effect, or directly
        with ``delete`` when the plan uses the window's time itself.
        """
        values = (sch_index, expected_service_no, expected_start_epoch, expected_duration_seconds)
        if any(type(value) is not int or value < 0 for value in values):
            raise HomeAssistantError("scheduler_external_target_identity_invalid")
        for reference in (expected_reference, new_reference):
            if (
                isinstance(reference, bool)
                or not isinstance(reference, int | float)
                or not isfinite(reference)
                or not 0 <= reference <= 100
            ):
                raise HomeAssistantError("scheduler_external_target_reference_invalid")
        if expected_duration_seconds <= 0 or expected_start_epoch <= int(time()):
            raise HomeAssistantError("scheduler_external_target_not_future")
        async with self._lock:
            start = max(0, expected_start_epoch - 1)
            end = expected_start_epoch + expected_duration_seconds + 1
            before = await self._observe(sch_index, start, end)
            recurrence = before.get("recurrence")
            one_shot = (
                isinstance(recurrence, Mapping) and recurrence.get("type") == "once"
            ) or before.get("rec_type") == 0
            if (
                before.get("service_no") != expected_service_no
                or before.get("start_time") != expected_start_epoch
                or before.get("duration") != expected_duration_seconds
                or before.get("service_type") != 107
                or before.get("service_enabled") != 1
                or not one_shot
            ):
                raise HomeAssistantError("scheduler_external_target_changed")
            current = float((await self.client.read_target_soc(expected_service_no))["reference"])
            if current != float(expected_reference):
                raise HomeAssistantError("scheduler_external_target_changed")
            if [item["sch_index"] for item in await self._soc_targets(expected_service_no)] != [
                sch_index
            ]:
                # A shared parameter record would also change other schedules.
                raise HomeAssistantError("scheduler_external_target_shared")
            identity = JournalTargetIdentity(
                sch_index=sch_index,
                service_no=expected_service_no,
                source_id=before.get("source_id"),
                asset_id=before.get("asset_id"),
            )
            if delete:
                return await self._delete_external_target(
                    sch_index, identity, before, current, start, end
                )
            reasons = self._reasons(ControlOperation.UPDATE)
            if reasons:
                raise HomeAssistantError(", ".join(reasons))
            result = await self._journaled_external_write(
                "automatic_price_external_target_soc_adjust",
                identity,
                {"before": before, "reference_before": current, "reference_after": new_reference},
                lambda: self.client.write_target_soc(expected_service_no, new_reference),
            )
            if result["status"] != "sent":
                return result
            readback = float(
                (await self.client.read_target_soc(expected_service_no))["reference"]
            )
            if readback == float(new_reference):
                await self.runtime.transition_control_transaction(
                    result["transaction_id"], JournalPhase.RECONCILED, reason="readback_verified"
                )
                return {**result, "status": "adjusted", "reference": readback}
            if readback != current or not allow_delete:
                await self.runtime.transition_control_transaction(
                    result["transaction_id"],
                    JournalPhase.RECOVERY_REQUIRED if readback != current else JournalPhase.FAILED,
                    reason="readback_mismatch" if readback != current else "reference_unchanged",
                )
                return {
                    **result,
                    "status": "recovery_required" if readback != current else "unchanged",
                }
            await self.runtime.transition_control_transaction(
                result["transaction_id"], JournalPhase.FAILED, reason="reference_unchanged"
            )
            return await self._delete_external_target(
                sch_index, identity, before, current, start, end
            )

    async def _delete_external_target(
        self,
        sch_index: int,
        identity: JournalTargetIdentity,
        before: dict[str, Any],
        reference: float,
        start: int,
        end: int,
    ) -> dict[str, Any]:
        """Delete one verified foreign Target SoC schedule once and confirm it is gone."""
        reasons = self._reasons(ControlOperation.DELETE)
        if reasons:
            raise HomeAssistantError(", ".join(reasons))
        deleted = await self._journaled_external_write(
            "automatic_price_external_target_soc_delete",
            identity,
            {"before": before, "reference_before": reference},
            lambda: self.client.write_record(sch_index, before, None),
        )
        if deleted["status"] != "sent":
            return deleted
        manager = self.runtime.scheduler_manager
        assert manager is not None
        entries = await manager.refresh_list(start, end)
        gone = all(entry.sch_index != sch_index for entry in entries)
        await self.runtime.transition_control_transaction(
            deleted["transaction_id"],
            JournalPhase.RECONCILED if gone else JournalPhase.RECOVERY_REQUIRED,
            reason="readback_verified" if gone else "readback_mismatch",
        )
        return {**deleted, "status": "deleted" if gone else "recovery_required"}

    async def _journaled_external_write(
        self,
        operation: str,
        identity: JournalTargetIdentity,
        intent: dict[str, Any],
        send: Any,
    ) -> dict[str, Any]:
        """Journal, recheck authority, send once and record the acknowledgement."""
        authority_revision = self.runtime.production_authority.revision
        transaction_id = f"price-external-target-{secrets.token_hex(16)}"
        await self.runtime.begin_control_transaction(
            transaction_id=transaction_id,
            operation=operation,
            target_identity=identity,
            intent=intent,
        )
        await self.runtime.transition_control_transaction(transaction_id, JournalPhase.COMMAND_SENT)
        if (
            authority_revision != self.runtime.production_authority.revision
            or not self.runtime.production_control_enabled()
        ):
            await self.runtime.transition_control_transaction(
                transaction_id, JournalPhase.FAILED, reason="authority_changed_before_send"
            )
            raise HomeAssistantError("production_control_disabled")
        try:
            await send()
            await self.runtime.transition_control_transaction(
                transaction_id, JournalPhase.ACKNOWLEDGED
            )
        except Exception:  # noqa: BLE001 - never retry uncertain delivery
            await self.runtime.transition_control_transaction(
                transaction_id,
                JournalPhase.RECOVERY_REQUIRED,
                reason="external_target_delivery_unknown",
            )
            return {"status": "recovery_required", "transaction_id": transaction_id}
        return {"status": "sent", "transaction_id": transaction_id, "sch_index": identity.sch_index}

    async def run_meter_limitation_soft_limit_trial(
        self, direction: str, max_correction_w: int
    ) -> dict[str, Any]:
        """Serialize a one-shot physical trial with all Gateway mutations."""
        async with self._lock:
            return await self.client.run_meter_limitation_soft_limit_trial(
                direction, max_correction_w
            )

    async def _observe(self, sch_index: int, start: int, end: int) -> dict[str, Any]:
        manager = self.runtime.scheduler_manager
        if manager is None:
            raise HomeAssistantError("scheduler_unavailable")
        entries = await manager.refresh_list(start, end)
        matches = [entry for entry in entries if entry.sch_index == sch_index]
        if len(matches) != 1:
            raise HomeAssistantError("scheduler_target_missing_or_ambiguous")
        record = await self.client.read_record(sch_index)
        entry = matches[0]
        if (
            entry.start_epoch != record["start_time"]
            or entry.duration_seconds != record["duration"]
            or entry.service_no != record["service_no"]
            or entry.service_type != record["service_type"]
            or entry.enabled != bool(record["service_enabled"])
        ):
            raise HomeAssistantError("scheduler_list_detail_changed")
        return record

    async def _soc_targets(self, service_no: int) -> list[dict[str, Any]]:
        """Bind all schedule references to a shared parameter record, including outside the view.

        The Gateway keeps its whole history, which outgrew the inventory's
        100-entry LIST limit, so the scope is read on its own, with a larger
        limit and without replacing (or invalidating) the inventory.
        """
        manager = self.runtime.scheduler_manager
        if manager is None:
            raise HomeAssistantError("scheduler_unavailable")
        try:
            entries = await manager.read_scope(0, _MAX_INTEGER, limit=_SOC_SCOPE_ENTRY_LIMIT)
        except SchedulerListResponseTooLargeError as error:
            raise HomeAssistantError("scheduler_soc_scope_unverified") from error
        return sorted(
            [
                {
                    "sch_index": e.sch_index,
                    "start_epoch": e.start_epoch,
                    "duration_seconds": e.duration_seconds,
                    "enabled": e.enabled,
                    "recurrence": e.recurrence,
                }
                for e in entries
                if e.service_type == 107 and e.service_no == service_no
            ],
            key=lambda e: e["sch_index"],
        )

    async def preview(
        self,
        sch_index: int,
        operation: str,
        changes: dict[str, Any],
        user_id: str,
    ) -> dict[str, Any]:
        await self.runtime._require_admin(user_id)
        if operation not in {"update", "delete"}:
            raise HomeAssistantError("scheduler_operation_unsupported")
        if type(sch_index) is not int or not 0 <= sch_index <= _MAX_INTEGER:
            raise HomeAssistantError("scheduler_target_invalid")
        if operation == "delete" and changes:
            raise HomeAssistantError("scheduler_delete_has_no_changes")
        requested_soc = changes.get("socref")
        if "socref" in changes and (
            isinstance(requested_soc, bool)
            or not isinstance(requested_soc, (int, float))
            or not isfinite(requested_soc)
            or not 0 <= requested_soc <= 100
        ):
            raise HomeAssistantError("scheduler_soc_value_invalid")
        timing = {key: value for key, value in changes.items() if key != "socref"}
        edits = validate_changes(timing) if timing and operation == "update" else {}
        if operation == "update" and not edits and requested_soc is None:
            raise HomeAssistantError("scheduler_no_changes")
        observed = await self.client.read_record(sch_index)
        before = await self._observe(
            sch_index,
            max(0, observed["start_time"] - 1),
            observed["start_time"] + observed["duration"] + 1,
        )
        if edits:
            validate_changes(edits, before)
        after = {**before, **edits} if operation == "update" else None
        service_before = None
        targets = []
        if requested_soc is not None:
            if before["service_type"] != 107:
                raise HomeAssistantError("scheduler_soc_service_unsupported")
            service_before = await self.client.read_target_soc(before["service_no"])
            if requested_soc == service_before["reference"]:
                requested_soc = None
            else:
                targets = await self._soc_targets(before["service_no"])
                if not any(t["sch_index"] == sch_index for t in targets):
                    raise HomeAssistantError("scheduler_soc_scope_unverified")
        if after is not None and after == before and requested_soc is None:
            raise HomeAssistantError("scheduler_no_changes")
        reasons = self._reasons(ControlOperation(operation))
        if len(self._previews) >= 64:
            self._previews.clear()
        token = secrets.token_hex(24)
        preview = {
            "preview_id": token,
            "sch_index": sch_index,
            "operation": operation,
            "before": before,
            "after": after,
            "changes": edits,
            "blocked_reasons": reasons,
            "can_apply": not reasons,
            "service_before": service_before,
            "target_soc_percent": requested_soc,
            "affected_soc_schedules": targets,
            "authority_revision": self.runtime.production_authority.revision,
            "journal_revision": (
                self.runtime.control_journal.revision if self.runtime.control_journal else None
            ),
        }
        if not reasons:
            self._previews[token] = {
                **deepcopy(preview),
                "user_id": user_id,
                "expires": monotonic() + 120,
            }
        return preview

    async def apply(self, token: str, user_id: str) -> dict[str, Any]:
        await self.runtime._require_admin(user_id)
        async with self._lock:
            pending = self._previews.get(token)
            if pending is None or pending["user_id"] != user_id:
                raise HomeAssistantError("preview_missing_or_consumed")
            preview = self._previews.pop(token)
            journal = self.runtime.control_journal
            if (
                monotonic() > preview["expires"]
                or journal is None
                or preview["authority_revision"] != self.runtime.production_authority.revision
                or preview["journal_revision"] != journal.revision
            ):
                raise HomeAssistantError("preview_state_changed")
            before, after = preview["before"], preview["after"]
            index = preview["sch_index"]
            start = max(
                0,
                min(before["start_time"], after["start_time"] if after else before["start_time"])
                - 1,
            )
            end = (
                max(
                    before["start_time"] + before["duration"],
                    after["start_time"] + after["duration"] if after else 0,
                )
                + 1
            )
            current = await self._observe(index, start, end)
            if current != before:
                raise HomeAssistantError("scheduler_target_changed_create_new_preview")
            target_soc = preview.get("target_soc_percent")
            if target_soc is not None:
                if (
                    await self.client.read_target_soc(before["service_no"])
                    != preview["service_before"]
                    or await self._soc_targets(before["service_no"])
                    != preview["affected_soc_schedules"]
                ):
                    raise HomeAssistantError("scheduler_target_changed_create_new_preview")
            reasons = self._reasons(ControlOperation(preview["operation"]))
            if reasons:
                raise HomeAssistantError(", ".join(reasons))
            transaction_id = f"gateway-scheduler-{secrets.token_hex(16)}"
            await self.runtime.begin_control_transaction(
                transaction_id=transaction_id,
                operation=f"native_scheduler_{preview['operation']}",
                target_identity=JournalTargetIdentity(
                    sch_index=index,
                    service_no=before["service_no"],
                    source_id=before.get("source_id"),
                    asset_id=before.get("asset_id"),
                ),
                intent={
                    "before": before,
                    "after": after,
                    "target_soc_percent": target_soc,
                    "service_before": preview.get("service_before"),
                    "affected_soc_schedules": preview.get("affected_soc_schedules", []),
                },
            )
            await self.runtime.transition_control_transaction(
                transaction_id,
                JournalPhase.COMMAND_SENT,
            )
            if (
                preview["authority_revision"] != self.runtime.production_authority.revision
                or not self.runtime.production_control_enabled()
            ):
                await self.runtime.transition_control_transaction(
                    transaction_id,
                    JournalPhase.FAILED,
                    reason="authority_changed_before_send",
                )
                raise HomeAssistantError("production_control_disabled")
            try:
                if target_soc is not None:
                    await self.client.write_target_soc(before["service_no"], target_soc)
                if after is None or after != before:
                    await self.client.write_record(
                        index,
                        before,
                        preview["changes"] if after is not None else None,
                    )
                await self.runtime.transition_control_transaction(
                    transaction_id,
                    JournalPhase.ACKNOWLEDGED,
                )
                manager = self.runtime.scheduler_manager
                assert manager is not None
                if after is None:
                    entries = await manager.refresh_list(start, end)
                    verified = all(entry.sch_index != index for entry in entries)
                else:
                    verified = await self._observe(index, start, end) == after
                    if verified and target_soc is not None:
                        verified = (await self.client.read_target_soc(before["service_no"]))[
                            "reference"
                        ] == target_soc
                await self.runtime.transition_control_transaction(
                    transaction_id,
                    JournalPhase.RECONCILED if verified else JournalPhase.RECOVERY_REQUIRED,
                    reason="readback_verified" if verified else "readback_mismatch",
                )
            except Exception:  # noqa: BLE001 - never retry uncertain delivery
                await self.runtime.transition_control_transaction(
                    transaction_id,
                    JournalPhase.RECOVERY_REQUIRED,
                    reason="gateway_scheduler_delivery_or_readback_unknown",
                )
                return {"status": "recovery_required", "transaction_id": transaction_id}
            return {
                "status": "reconciled" if verified else "recovery_required",
                "transaction_id": transaction_id,
                "sch_index": index,
                "operation": preview["operation"],
            }

    async def recover(self, transaction_id: str, user_id: str) -> dict[str, Any]:
        """Verify an uncertain result using reads only; never resend its command."""
        await self.runtime._require_admin(user_id)
        async with self._lock:
            journal = self.runtime.control_journal
            transaction = journal.transaction(transaction_id) if journal else None
            if (
                transaction is None
                or transaction.phase is not JournalPhase.RECOVERY_REQUIRED
                or transaction.operation
                not in {
                    "native_scheduler_update",
                    "native_scheduler_delete",
                    "automatic_price_external_schedule_move",
                }
                or transaction.target_identity.sch_index is None
            ):
                raise HomeAssistantError("gateway_scheduler_recovery_target_invalid")
            before = transaction.intent.get("before")
            after = transaction.intent.get("after")
            if not isinstance(before, dict) or not {"start_time", "duration"} <= set(before):
                raise HomeAssistantError("gateway_scheduler_recovery_intent_invalid")
            index = transaction.target_identity.sch_index
            start = max(0, before["start_time"] - 1)
            end = before["start_time"] + before["duration"] + 1
            verified = False
            if transaction.operation == "native_scheduler_delete" and after is None:
                manager = self.runtime.scheduler_manager
                if manager is None:
                    raise HomeAssistantError("scheduler_unavailable")
                entries = await manager.refresh_list(start, end)
                verified = all(entry.sch_index != index for entry in entries)
            elif isinstance(after, dict):
                start = min(start, after["start_time"] - 1)
                end = max(end, after["start_time"] + after["duration"] + 1)
                verified = await self._observe(index, start, end) == after
                target_soc = transaction.intent.get("target_soc_percent")
                if verified and target_soc is not None:
                    verified = (await self.client.read_target_soc(before["service_no"]))[
                        "reference"
                    ] == target_soc
            if verified:
                await self.runtime.transition_control_transaction(
                    transaction_id,
                    JournalPhase.RECONCILED,
                    reason="gateway_recovery_readback_verified",
                )
            return {
                "status": "reconciled" if verified else "recovery_required",
                "transaction_id": transaction_id,
            }
