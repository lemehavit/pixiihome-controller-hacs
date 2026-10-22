from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.pixiihome_controller.control.journal import (
    JournalPhase,
    JournalTargetIdentity,
    JournalTransaction,
)
from custom_components.pixiihome_controller.control.models import (
    ControlPolicyInput,
    OperationImpact,
    OperatorMode,
    PlantHealth,
    ReleaseState,
)
from custom_components.pixiihome_controller.exceptions import SchedulerProtocolError
from custom_components.pixiihome_controller.protocol.scheduler import (
    scheduler_epoch_from_gateway_wire,
    scheduler_epoch_to_gateway_wire,
)
from custom_components.pixiihome_controller.scheduler.gateway import (
    AiohttpSchedulerGatewayClient,
    GatewaySchedulerController,
    validate_changes,
)

RECORD = {
    "asset_id": "mqtt_control",
    "duration": 3600,
    "service_enabled": 1,
    "service_no": 2337,
    "service_prio": 0,
    "service_type": 107,
    "source_id": 7,
    "start_time": 1_893_456_000,
    "rec_type": 0,
    "recurrence1": 0,
    "recurrence2": 0,
    "recurrence3": 0,
    "rec_end_time": 0,
    "duration__min": 1,
    "rec_end_time__min": 0,
    "rec_type__max": 5,
    "rec_type__min": 0,
    "recurrence1__min": 0,
    "recurrence2__min": 0,
    "recurrence3__min": 0,
    "service_no__min": 0,
    "start_time__min": 0,
}


class Client(AiohttpSchedulerGatewayClient):
    def __init__(self) -> None:
        self._alevel = "9"
        self._sessid = "S" * 32
        self.record = deepcopy(RECORD)
        self.calls: list[dict[str, Any]] = []
        self.bad_ack = False
        self.wrong_id = False
        self.mismatch = False
        self.timeout = False
        self.present = True
        self.soc = {"reference": 80, "source_id": 7, "description": "Shared target"}

    async def _ensure_authenticated(self) -> str:
        assert self._sessid is not None
        return self._sessid

    async def _request_json(self, prefix: str, payload: Any, **kwargs: Any) -> Any:
        self.calls.append(deepcopy(payload))
        msg = payload["Omnibus"]["msg"]
        if msg[6] == 107:
            ack = list(msg)
            ack[2] = 7
            if msg[2] == 6:
                if self.timeout:
                    raise TimeoutError
                if not self.mismatch:
                    self.soc.update(payload["Omnibus"]["data"])
                if self.bad_ack:
                    ack[2] = 9
                if self.wrong_id:
                    ack[8] += 1
            return {"status": "ok", "Omnibus": {"msg": ack, "data": deepcopy(self.soc)}}
        if msg[2] == 5:
            wire_record = deepcopy(self.record)
            wire_record["start_time"] = scheduler_epoch_to_gateway_wire(wire_record["start_time"])
            return {
                "status": "ok",
                "Omnibus": {
                    "msg": [1, 1, 7, 3, 255, 2, 101, 200, msg[8], 33, 0, 0],
                    "data": wire_record,
                },
            }
        if self.timeout:
            raise TimeoutError
        if msg[7] == 51:
            self.present = False
        elif not self.mismatch:
            self.record = deepcopy(payload["Omnibus"]["data"])
            self.record["start_time"] = scheduler_epoch_from_gateway_wire(self.record["start_time"])
        ack = list(msg)
        ack[2] = 9 if self.bad_ack else 7
        if self.wrong_id:
            ack[8] += 1
        return {"status": "ok", "Omnibus": {"msg": ack}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase_service", "meter_limitation", "general_service_settings"),
    [(False, False, False), (True, False, False), (False, True, False), (False, False, True)],
)
@pytest.mark.parametrize("bad_reply", [False, True])
async def test_phase_observation_uses_exact_get_allowlist_and_checks_reply(
    phase_service, meter_limitation, general_service_settings, bad_reply
):
    class ReadClient(Client):
        async def _request_json(self, prefix, payload, **kwargs):
            self.calls.append(deepcopy(payload))
            assert payload["Omnibus"]["data"] == {}
            assert kwargs["cookies"] == {"Sessid": "S" * 32, "ALevel": "9"}
            expected = (
                [1, 0, 5, 3, 255, 2, 129, 200, 0, 33, 0, 0]
                if phase_service
                else [1, 0, 5, 3, 255, 2, 120, 200, 0, 33, 0, 0]
                if meter_limitation
                else [1, 0, 5, 3, 255, 2, 0, 200, 0, 33, 0, 0]
                if general_service_settings
                else [1, 0, 5, 3, 255, 0, 24, 4, 0, 33, 0, 0]
            )
            assert payload["Omnibus"]["msg"] == expected
            ack = list(expected)
            ack[2] = 9 if bad_reply else 7
            return {
                "status": "ok",
                "Omnibus": {
                    "msg": ack,
                    "data": {
                        "meter_a1": 12,
                        "batt_soc": float("nan"),
                        "act_ref1": 42,
                        "enabled": 1,
                        "meter_id": 1,
                        "limit_per_phase": 0,
                        "exp_soft_enabled": 0,
                        "exp_soft_limit": float("inf"),
                        "exp_hard_limit__min": -50000,
                        "exp_hard_limit__max": 50000,
                        "ref_delay": 10,
                        "ref_delay__min": 0,
                        "ref_delay__max": 60,
                        "meter_other_srv": 1,
                        "meter_other_srv__min": 0,
                        "meter_other_srv__max": 6,
                        "password": "do-not-expose",
                        "meter_v1": True,
                    },
                },
            }

    client = ReadClient()
    if bad_reply:
        with pytest.raises(SchedulerProtocolError):
            await client.read_load_balancing_status(
                phase_service=phase_service,
                meter_limitation=meter_limitation,
                general_service_settings=general_service_settings,
            )
    else:
        result = await client.read_load_balancing_status(
            phase_service=phase_service,
            meter_limitation=meter_limitation,
            general_service_settings=general_service_settings,
        )
        assert result == (
            {"act_ref1": 42, "enabled": 1}
            if phase_service
            else {
                "enabled": 1,
                "meter_id": 1,
                "limit_per_phase": 0,
                "exp_soft_enabled": 0,
                "exp_hard_limit__min": -50000,
                "exp_hard_limit__max": 50000,
            }
            if meter_limitation
            else {
                "ref_delay": 10,
                "ref_delay__min": 0,
                "ref_delay__max": 60,
                "meter_other_srv": 1,
                "meter_other_srv__min": 0,
                "meter_other_srv__max": 6,
            }
            if general_service_settings
            else {"meter_a1": 12}
        )
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_load_balancing_capability_read_rejects_ambiguous_target():
    client = Client()
    with pytest.raises(ValueError, match="only one"):
        await client.read_load_balancing_status(phase_service=True, meter_limitation=True)
    with pytest.raises(ValueError, match="only one"):
        await client.read_load_balancing_status(
            meter_limitation=True, general_service_settings=True
        )
    assert not client.calls


class GeneralServiceSettingsClient(Client):
    def __init__(self, *, corrupt_first_write: bool = False, bad_ack: bool = False) -> None:
        super().__init__()
        self.settings: dict[str, int | float] = {
            "soc_lim_en": 1,
            "ref_delay": 1,
            "ref_delay__min": 0,
            "ref_delay__max": 60,
            "power_share": 1.0,
            "meter_other_srv": 1,
        }
        self.corrupt_first_write = corrupt_first_write
        self.general_writes = 0
        self.bad_general_ack = bad_ack

    async def _request_json(self, prefix: str, payload: Any, **kwargs: Any) -> Any:
        del prefix, kwargs
        self.calls.append(deepcopy(payload))
        message = payload["Omnibus"]["msg"]
        assert message[3:9] == [3, 255, 2, 0, 200, 0]
        acknowledgement = list(message)
        acknowledgement[2] = 7
        if message[2] == 5:
            return {
                "status": "ok",
                "Omnibus": {"msg": acknowledgement, "data": deepcopy(self.settings)},
            }
        assert message == [1, 0, 6, 3, 255, 2, 0, 200, 0, 33, 0, 0]
        self.general_writes += 1
        self.settings.update(payload["Omnibus"]["data"])
        if self.corrupt_first_write and self.general_writes == 1:
            self.settings["power_share"] = 0.5
        if self.bad_general_ack:
            acknowledgement[2] = 9
        return {"status": "ok", "Omnibus": {"msg": acknowledgement}}


@pytest.mark.asyncio
async def test_service_reference_delay_preserves_record_and_requires_readback():
    client = GeneralServiceSettingsClient()

    result = await client.set_service_reference_delay(1)

    assert result == {
        "status": "configured",
        "changed": True,
        "before": 1,
        "after": 10,
        "preserved": True,
        "acknowledgement_verified": True,
    }
    assert client.general_writes == 1
    assert client.calls[1]["Omnibus"] == {
        "msg": [1, 0, 6, 3, 255, 2, 0, 200, 0, 33, 0, 0],
        "data": {"soc_lim_en": 1, "ref_delay": 10, "power_share": 1.0},
    }
    assert client.settings["meter_other_srv"] == 1


@pytest.mark.asyncio
async def test_service_reference_delay_rejects_changed_precondition_without_write():
    client = GeneralServiceSettingsClient()

    with pytest.raises(HomeAssistantError, match="service_reference_delay_changed"):
        await client.set_service_reference_delay(2)

    assert client.general_writes == 0


@pytest.mark.asyncio
async def test_service_reference_delay_rolls_back_unrelated_change():
    client = GeneralServiceSettingsClient(corrupt_first_write=True)

    with pytest.raises(SchedulerProtocolError, match="mismatch rolled back"):
        await client.set_service_reference_delay(1)

    assert client.general_writes == 2
    assert client.settings["ref_delay"] == 1
    assert client.settings["power_share"] == 1.0


@pytest.mark.asyncio
async def test_service_reference_delay_accepts_exact_readback_after_bad_ack():
    client = GeneralServiceSettingsClient(bad_ack=True)

    result = await client.set_service_reference_delay(1)

    assert result["status"] == "configured"
    assert result["acknowledgement_verified"] is False


class MeterLimitationTrialClient(Client):
    def __init__(self, *, fail_sample: bool = False) -> None:
        super().__init__()
        self.settings: dict[str, int | float] = {
            "enabled": 0,
            "exp_soft_enabled": 0,
            "exp_hard_enabled": 0,
            "exp_soft_limit": 0,
            "exp_hard_limit": 0,
            "gen_soft_enabled": 0,
            "gen_hard_enabled": 0,
            "gen_soft_limit": 0,
            "gen_hard_limit": 0,
            "meter_id": 1,
            "limit_per_phase": 0,
        }
        self.fail_sample = fail_sample
        self.telemetry_reads = 0

    async def _request_json(self, prefix: str, payload: Any, **kwargs: Any) -> Any:
        del prefix, kwargs
        self.calls.append(deepcopy(payload))
        msg = payload["Omnibus"]["msg"]
        ack = list(msg)
        ack[2] = 7
        if msg[2] == 5 and msg[6] == 0:
            return {
                "status": "ok",
                "Omnibus": {
                    "msg": ack,
                    "data": {
                        "soc_lim_en": 1,
                        "ref_delay": 10,
                        "power_share": 1,
                        "meter_other_srv": 1,
                    },
                },
            }
        if msg[2] == 5 and msg[6] == 120:
            return {
                "status": "ok",
                "Omnibus": {"msg": ack, "data": deepcopy(self.settings)},
            }
        if msg[2] == 6 and msg[6] == 120:
            limit_per_phase = self.settings["limit_per_phase"]
            self.settings = {
                **deepcopy(payload["Omnibus"]["data"]),
                "limit_per_phase": limit_per_phase,
            }
            return {"status": "ok", "Omnibus": {"msg": ack}}
        if msg[2] == 5 and msg[6] == 24:
            self.telemetry_reads += 1
            if self.fail_sample and self.telemetry_reads > 1:
                raise TimeoutError
            return {
                "status": "ok",
                "Omnibus": {
                    "msg": ack,
                    "data": {
                        "srv_type": 107,
                        "batt_soc": 50,
                        **{f"meter_w{i}": 1000 for i in range(1, 4)},
                        **{f"meter_a{i}": 4.35 for i in range(1, 4)},
                        **{f"ac_w{i}": 0 for i in range(1, 4)},
                    },
                },
            }
        raise AssertionError(f"unexpected message: {msg}")


@pytest.mark.asyncio
async def test_meter_limitation_trial_is_bounded_and_restores(monkeypatch):
    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(
        "custom_components.pixiihome_controller.scheduler.gateway.asyncio.sleep",
        no_sleep,
    )
    client = MeterLimitationTrialClient()

    result = await client.run_meter_limitation_soft_limit_trial("exp", 300)

    writes = [
        call["Omnibus"]["data"]
        for call in client.calls
        if call["Omnibus"]["msg"][2:7] == [6, 3, 255, 2, 120]
    ]
    assert len(writes) == 2
    assert writes[0]["enabled"] == writes[0]["exp_soft_enabled"] == 1
    assert writes[0]["exp_soft_limit"] == 2700
    assert writes[0]["exp_hard_enabled"] == writes[0]["gen_hard_enabled"] == 0
    assert writes[1]["enabled"] == writes[1]["exp_soft_enabled"] == 0
    assert result["status"] == "completed_and_restored"
    assert result["restored"] is True
    assert len(result["samples"]) == 5


@pytest.mark.asyncio
async def test_meter_limitation_trial_restores_after_sample_failure():
    client = MeterLimitationTrialClient(fail_sample=True)

    with pytest.raises(TimeoutError):
        await client.run_meter_limitation_soft_limit_trial("gen", 300)

    assert client.settings["enabled"] == 0
    assert client.settings["gen_soft_enabled"] == 0
    assert client.settings["gen_soft_limit"] == 0
    assert client.settings["exp_hard_enabled"] == 0
    assert client.settings["gen_hard_enabled"] == 0


class Runtime:
    def __init__(self, client: Client) -> None:
        self.client = client
        self.production_authority = SimpleNamespace(revision=1)
        self.control_journal = SimpleNamespace(revision=0)
        self.scheduler_manager = self
        self.phases: list[JournalPhase] = []
        self.started: dict[str, Any] | None = None
        self.enabled = True

    async def _require_admin(self, user_id: str) -> None:
        if user_id not in {"admin", "other-admin"}:
            raise HomeAssistantError("admin_required")

    def production_control_enabled(self) -> bool:
        return self.enabled

    def scheduler_control_context(self, operation: Any) -> ControlPolicyInput:
        return ControlPolicyInput(
            operation=operation,
            operator_mode=OperatorMode.LOCAL_CONTROL,
            release_state=ReleaseState.LIVE_RELEASE_OPEN
            if self.enabled
            else ReleaseState.LIVE_RELEASE_CLOSED,
            plant_health=PlantHealth(True, True, True, True, True, ()),
            operation_impact=OperationImpact(active_service=0, immediate_effect_known=True),
            scheduler_visibility_fresh=True,
            ownership_store_valid=True,
            ownership_ambiguous=False,
            mqtt_connected=True,
        )

    async def refresh_list(self, start: int, end: int) -> tuple[Any, ...]:
        if not self.client.present:
            return ()
        data = self.client.record
        return (
            SimpleNamespace(
                sch_index=4435,
                start_epoch=data["start_time"],
                duration_seconds=data["duration"],
                end_epoch=data["start_time"] + data["duration"],
                service_no=data["service_no"],
                service_type=data["service_type"],
                enabled=bool(data["service_enabled"]),
                recurrence=None,
            ),
        )

    async def begin_control_transaction(self, **kwargs: Any) -> None:
        self.started = kwargs
        self.control_journal.revision += 1

    async def transition_control_transaction(
        self, transaction: str, phase: Any, **kwargs: Any
    ) -> None:
        self.phases.append(phase)


def controller() -> tuple[Runtime, Client, GatewaySchedulerController]:
    client = Client()
    runtime = Runtime(client)
    return runtime, client, GatewaySchedulerController(runtime, client)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_managed_target_soc_move_binds_source_reference_and_readback() -> None:
    runtime, client, control = controller()
    client.record["recurrence"] = {"type": "once"}
    new_start = RECORD["start_time"] + 7200

    result = await control.move_managed_target_soc(
        sch_index=4435,
        expected_source_id=7,
        expected_start_epoch=RECORD["start_time"],
        expected_duration_seconds=RECORD["duration"],
        expected_reference=80,
        new_start_epoch=new_start,
    )

    assert result["status"] == "reconciled"
    assert client.record["start_time"] == new_start
    assert client.record["source_id"] == 7
    assert client.soc["reference"] == 80
    assert runtime.started is not None
    assert runtime.started["operation"] == "automatic_price_external_schedule_move"
    assert runtime.phases[-1] is JournalPhase.RECONCILED


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"duration": 0},
        {"duration": True},
        {"duration": 1.5},
        {"start_time": -1},
        {"duration": 2**31},
        {"socref": 50},
        {"service_no": 3},
        {"source_id": 1},
    ],
)
def test_edit_rejects_unsupported_or_invalid_values(changes: Any) -> None:
    with pytest.raises(HomeAssistantError):
        validate_changes(changes)


@pytest.mark.asyncio
@pytest.mark.parametrize("service_type", [102, 104, 105, 107, 110, 111, 119, 121, 129])
async def test_external_schedule_time_edit_preserves_provenance(service_type: int) -> None:
    runtime, client, control = controller()
    client.record["service_type"] = service_type
    preview = await control.preview(4435, "update", {"duration": 7200}, "admin")
    assert preview["can_apply"]
    assert all(call["Omnibus"]["msg"][2] == 5 for call in client.calls)
    result = await control.apply(preview["preview_id"], "admin")
    assert result["status"] == "reconciled"
    assert client.record == {**RECORD, "service_type": service_type, "duration": 7200}
    assert runtime.started is not None
    assert runtime.started["target_identity"].m_id is None
    assert runtime.started["target_identity"].sch_index == 4435
    assert runtime.phases == [
        JournalPhase.COMMAND_SENT,
        JournalPhase.ACKNOWLEDGED,
        JournalPhase.RECONCILED,
    ]
    with pytest.raises(HomeAssistantError):
        await control.apply(preview["preview_id"], "admin")


@pytest.mark.asyncio
async def test_external_delete_uses_exact_gateway_index() -> None:
    runtime, client, control = controller()
    preview = await control.preview(4435, "delete", {}, "admin")
    result = await control.apply(preview["preview_id"], "admin")
    assert result["status"] == "reconciled"
    write = [call for call in client.calls if call["Omnibus"]["msg"][2] == 6]
    assert write == [
        {
            "cmd": "message",
            "Omnibus": {
                "msg": [1, 0, 6, 3, 255, 2, 101, 51, 4435, 7, 1, 0],
                "data": 4435,
            },
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["source_id", "duration", "service_no", "recurrence1"])
async def test_concurrent_aggregator_change_blocks_without_write(changed: str) -> None:
    runtime, client, control = controller()
    preview = await control.preview(4435, "delete", {}, "admin")
    client.record[changed] += 1
    with pytest.raises(HomeAssistantError):
        await control.apply(preview["preview_id"], "admin")
    assert runtime.started is None
    assert not any(call["Omnibus"]["msg"][2] == 6 for call in client.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["bad_ack", "wrong_id", "mismatch", "timeout"])
async def test_uncertain_outcome_requires_recovery_without_retry(failure: str) -> None:
    runtime, client, control = controller()
    preview = await control.preview(4435, "update", {"duration": 7200}, "admin")
    setattr(client, failure, True)
    result = await control.apply(preview["preview_id"], "admin")
    assert result["status"] == "recovery_required"
    assert runtime.phases[-1] is JournalPhase.RECOVERY_REQUIRED
    assert sum(call["Omnibus"]["msg"][2] == 6 for call in client.calls) == 1


@pytest.mark.asyncio
async def test_user_authority_and_revision_binding() -> None:
    runtime, client, control = controller()
    preview = await control.preview(4435, "delete", {}, "admin")
    with pytest.raises(HomeAssistantError):
        await control.apply(preview["preview_id"], "other-admin")
    runtime.production_authority.revision += 1
    with pytest.raises(HomeAssistantError):
        await control.apply(preview["preview_id"], "admin")
    assert runtime.started is None


@pytest.mark.asyncio
async def test_disabled_authority_produces_blocked_preview() -> None:
    runtime, client, control = controller()
    runtime.enabled = False
    preview = await control.preview(4435, "delete", {}, "admin")
    assert not preview["can_apply"]
    assert "release_gate_closed" in preview["blocked_reasons"]
    assert not control._previews


@pytest.mark.asyncio
async def test_unknown_detail_fields_block_mutation() -> None:
    runtime, client, control = controller()
    client.record["unexpected"] = "do not expose"
    with pytest.raises(SchedulerProtocolError):
        await control.preview(4435, "delete", {}, "admin")
    assert runtime.started is None


@pytest.mark.asyncio
async def test_gateway_minimum_blocks_preview_before_any_write() -> None:
    runtime, client, control = controller()
    client.record["duration__min"] = 3600
    with pytest.raises(HomeAssistantError, match="below_gateway_minimum"):
        await control.preview(4435, "update", {"duration": 1800}, "admin")
    assert runtime.started is None
    assert all(call["Omnibus"]["msg"][2] == 5 for call in client.calls)


@pytest.mark.asyncio
async def test_gateway_constraint_change_invalidates_approval() -> None:
    runtime, client, control = controller()
    preview = await control.preview(4435, "update", {"duration": 7200}, "admin")
    client.record["duration__min"] = 9000
    with pytest.raises(HomeAssistantError, match="target_changed"):
        await control.apply(preview["preview_id"], "admin")
    assert runtime.started is None
    assert all(call["Omnibus"]["msg"][2] == 5 for call in client.calls)


@pytest.mark.asyncio
async def test_expired_preview_and_disabled_gateway_role_cannot_send() -> None:
    runtime, client, control = controller()
    preview = await control.preview(4435, "delete", {}, "admin")
    control._previews[preview["preview_id"]]["expires"] = 0
    with pytest.raises(HomeAssistantError):
        await control.apply(preview["preview_id"], "admin")
    client._alevel = "1"
    blocked = await control.preview(4435, "delete", {}, "admin")
    assert not blocked["can_apply"]
    assert "scheduler_installer_access_required" in blocked["blocked_reasons"]
    assert runtime.started is None


@pytest.mark.asyncio
async def test_non_admin_preview_cannot_even_read_gateway() -> None:
    runtime, client, control = controller()
    with pytest.raises(HomeAssistantError):
        await control.preview(4435, "delete", {}, "guest")
    assert not client.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["update", "delete"])
async def test_recovery_only_reads_and_closes_verified_intent(operation: str) -> None:
    runtime, client, control = controller()
    after = {**RECORD, "duration": 7200} if operation == "update" else None
    transaction = JournalTransaction(
        transaction_id="pending",
        operation=f"native_scheduler_{operation}",
        target_identity=JournalTargetIdentity(sch_index=4435),
        intent={"before": RECORD, "after": after},
        phase=JournalPhase.RECOVERY_REQUIRED,
    )
    runtime.control_journal.transaction = lambda value: transaction
    client.record = deepcopy(after or RECORD)
    client.present = after is not None
    result = await control.recover("pending", "admin")
    assert result["status"] == "reconciled"
    assert runtime.phases == [JournalPhase.RECONCILED]
    assert not any(call["Omnibus"]["msg"][2] == 6 for call in client.calls)


@pytest.mark.asyncio
async def test_soc_only_edit_uses_partial_service_write_preserves_schedule_and_source() -> None:
    runtime, client, control = controller()
    original_list = runtime.refresh_list

    async def shared_list(start, end):
        entries = await original_list(start, end)
        return (*entries, SimpleNamespace(**{**vars(entries[0]), "sch_index": 4436}))

    runtime.refresh_list = shared_list
    preview = await control.preview(4435, "update", {"socref": 75}, "admin")
    assert preview["before"] == preview["after"] == RECORD
    assert [t["sch_index"] for t in preview["affected_soc_schedules"]] == [4435, 4436]
    assert preview["service_before"]["reference"] == 80
    result = await control.apply(preview["preview_id"], "admin")
    assert result["status"] == "reconciled"
    writes = [c for c in client.calls if c["Omnibus"]["msg"][2] == 6]
    assert writes == [
        {
            "cmd": "message",
            "Omnibus": {
                "msg": [1, 0, 6, 3, 255, 2, 107, 200, 2337, 33, 0, 0],
                "data": {"reference": 75},
            },
        }
    ]
    assert client.record == RECORD
    assert client.soc == {"reference": 75, "source_id": 7, "description": "Shared target"}


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["soc", "scope"])
async def test_soc_or_shared_scope_changed_after_preview_prevents_any_write(changed):
    runtime, client, control = controller()
    preview = await control.preview(4435, "update", {"socref": 75}, "admin")
    if changed == "soc":
        client.soc["reference"] = 90
    else:
        original = runtime.refresh_list

        async def shared_list(start, end):
            entries = await original(start, end)
            return (*entries, SimpleNamespace(**{**vars(entries[0]), "sch_index": 4436}))

        runtime.refresh_list = shared_list
    with pytest.raises(HomeAssistantError, match="target_changed"):
        await control.apply(preview["preview_id"], "admin")
    assert runtime.started is None
    assert all(c["Omnibus"]["msg"][2] == 5 for c in client.calls)


@pytest.mark.asyncio
async def test_soc_mismatch_remains_pending_and_recovery_only_reads():
    runtime, client, control = controller()
    preview = await control.preview(4435, "update", {"socref": 75}, "admin")
    client.mismatch = True
    result = await control.apply(preview["preview_id"], "admin")
    assert result["status"] == "recovery_required"
    assert runtime.started is not None
    transaction = JournalTransaction(
        transaction_id=result["transaction_id"],
        operation="native_scheduler_update",
        target_identity=runtime.started["target_identity"],
        intent=runtime.started["intent"],
        phase=JournalPhase.RECOVERY_REQUIRED,
    )
    runtime.control_journal.transaction = lambda _: transaction
    writes = sum(c["Omnibus"]["msg"][2] == 6 for c in client.calls)
    assert (await control.recover(transaction.transaction_id, "admin"))[
        "status"
    ] == "recovery_required"
    client.soc["reference"] = 75
    assert (await control.recover(transaction.transaction_id, "admin"))["status"] == "reconciled"
    assert sum(c["Omnibus"]["msg"][2] == 6 for c in client.calls) == writes == 1


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"status": "ok", "Omnibus": []},
        {
            "status": "ok",
            "Omnibus": {
                "msg": [1, 0, 7, 3, 255, 2, 107, 200, 2340, 33, 0, 0],
                "data": {"reference": 80},
            },
        },
    ],
)
def test_soc_read_rejects_malformed_or_wrong_identity(response):
    with pytest.raises(SchedulerProtocolError):
        Client._target_soc_response(response, [1, 0, 5, 3, 255, 2, 107, 200, 2337, 33, 1, 0])


@pytest.mark.asyncio
@pytest.mark.parametrize("soc", [True, -1, 101, float("nan"), float("inf")])
async def test_invalid_soc_cannot_even_read(soc):
    runtime, client, control = controller()
    with pytest.raises(HomeAssistantError):
        await control.preview(4435, "update", {"socref": soc}, "admin")
    assert not client.calls


async def _set_target(control, *, reference=80, new=40, allow_delete=True, delete=False):
    return await control.set_external_target_soc(
        sch_index=4435,
        expected_service_no=RECORD["service_no"],
        expected_start_epoch=RECORD["start_time"],
        expected_duration_seconds=RECORD["duration"],
        expected_reference=reference,
        new_reference=new,
        allow_delete=allow_delete,
        delete=delete,
    )


@pytest.mark.asyncio
async def test_replaced_target_is_deleted_directly_without_a_reference_write() -> None:
    runtime, client, control = controller()

    result = await _set_target(control, new=80, delete=True)

    assert result["status"] == "deleted"
    assert not client.present
    assert client.soc["reference"] == 80  # the reference was never written
    assert runtime.started is not None
    assert runtime.started["operation"] == "automatic_price_external_target_soc_delete"
    assert runtime.phases[-1] is JournalPhase.RECONCILED


@pytest.mark.asyncio
async def test_direct_delete_still_refuses_a_changed_target() -> None:
    _, client, control = controller()

    with pytest.raises(HomeAssistantError, match="scheduler_external_target_changed"):
        await _set_target(control, reference=90, delete=True)

    assert client.present


@pytest.mark.asyncio
async def test_external_target_soc_is_lowered_with_journal_and_readback() -> None:
    runtime, client, control = controller()

    result = await _set_target(control)

    assert result["status"] == "adjusted"
    assert client.soc["reference"] == 40
    assert client.present
    assert runtime.started is not None
    assert runtime.started["operation"] == "automatic_price_external_target_soc_adjust"
    assert runtime.phases[-1] is JournalPhase.RECONCILED


@pytest.mark.asyncio
async def test_ineffective_target_write_falls_back_to_verified_delete() -> None:
    runtime, client, control = controller()
    client.mismatch = True  # the Gateway acknowledges but keeps the reference

    result = await _set_target(control)

    assert result["status"] == "deleted"
    assert not client.present
    assert runtime.started is not None
    assert runtime.started["operation"] == "automatic_price_external_target_soc_delete"
    assert JournalPhase.FAILED in runtime.phases
    assert runtime.phases[-1] is JournalPhase.RECONCILED


@pytest.mark.asyncio
async def test_ineffective_target_write_is_not_deleted_without_permission() -> None:
    _, client, control = controller()
    client.mismatch = True

    result = await _set_target(control, allow_delete=False)

    assert result["status"] == "unchanged"
    assert client.present


@pytest.mark.asyncio
async def test_external_target_change_since_planning_is_refused_before_writing() -> None:
    _, client, control = controller()

    with pytest.raises(HomeAssistantError, match="scheduler_external_target_changed"):
        await _set_target(control, reference=90)

    assert all(call["Omnibus"]["msg"][2] == 5 for call in client.calls)


@pytest.mark.asyncio
async def test_shared_target_parameter_record_is_never_written() -> None:
    runtime, client, control = controller()
    original = runtime.refresh_list

    async def shared(start: int, end: int):
        entries = await original(start, end)
        return (*entries, SimpleNamespace(**{**vars(entries[0]), "sch_index": 4436}))

    runtime.refresh_list = shared  # type: ignore[method-assign]

    with pytest.raises(HomeAssistantError, match="scheduler_external_target_shared"):
        await _set_target(control)

    assert client.soc["reference"] == 80
