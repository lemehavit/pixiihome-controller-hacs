from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from homeassistant.core import Context, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError

from custom_components.pixiihome_controller import (
    _METER_LIMITATION_TRIAL_SERVICE_SCHEMA,
    _REFERENCE_DELAY_SERVICE_SCHEMA,
    _SCHEDULER_DETAIL_SERVICE_SCHEMA,
    _SCHEDULER_SERVICE_SCHEMA,
    _SERVICE_SCHEMA,
    _async_query_running_service,
    _async_refresh_scheduler_detail,
    _async_refresh_scheduler_list,
    _async_run_meter_limitation_soft_limit_trial,
    _async_run_scheduler_validation_roundtrip,
    _async_set_service_reference_delay,
    async_setup,
)
from custom_components.pixiihome_controller.command import CommandResult, CommandStatus
from custom_components.pixiihome_controller.const import (
    CONF_ENTRY_ID,
    CONF_EXPECTED_SERVICE_REFERENCE_DELAY,
    CONF_METER_LIMITATION_TRIAL_DIRECTION,
    CONF_METER_LIMITATION_TRIAL_MAX_CORRECTION,
    CONF_SCHEDULER_DAYS_BACK,
    CONF_SCHEDULER_DAYS_FORWARD,
    CONF_SCHEDULER_INDEX,
    DOMAIN,
    LIVE_WRITES_ENABLED,
    SERVICE_QUERY_RUNNING_SERVICE,
    SERVICE_REFRESH_SCHEDULER_DETAIL,
    SERVICE_REFRESH_SCHEDULER_LIST,
    SERVICE_RUN_METER_LIMITATION_SOFT_LIMIT_TRIAL,
    SERVICE_RUN_SCHEDULER_VALIDATION,
    SERVICE_SET_SERVICE_REFERENCE_DELAY,
)
from custom_components.pixiihome_controller.exceptions import (
    SchedulerAuthenticationError,
    SchedulerAuthenticationReason,
    SchedulerAuthenticationStage,
)
from custom_components.pixiihome_controller.protocol.models import SchedulerDetail, SchedulerEntry


class FakeServices:
    def __init__(self) -> None:
        self.registration: tuple[Any, ...] | None = None
        self.registrations: list[tuple[Any, ...]] = []
        self.hass: Any | None = None

    def has_service(self, domain: str, service: str) -> bool:
        return any(
            registration[0:2] == (domain, service)
            for registration in self.registrations
        )

    def async_register(self, *args: Any, **kwargs: Any) -> None:
        registration = (*args, kwargs)
        self.registration = registration
        self.registrations.append(registration)

    async def async_call(
        self,
        domain: str,
        service: str,
        data: dict[str, Any],
        *,
        return_response: bool,
    ) -> dict[str, Any]:
        registration = next(
            registration
            for registration in self.registrations
            if registration[0:2] == (domain, service)
        )
        schema = registration[-1]["schema"]
        handler = registration[2]
        call = ServiceCall(
            cast(Any, self.hass),
            domain,
            service,
            schema(data),
            return_response=return_response,
        )
        return await handler(call)


class FakeManager:
    def __init__(self) -> None:
        self.commands: list[Any] = []

    async def execute(self, command: Any) -> CommandResult:
        self.commands.append(command)
        return CommandResult(
            command_id="command-1",
            method=command.method.value,
            service=command.service,
            status=CommandStatus.SUCCESS,
        )


@pytest.mark.asyncio
async def test_services_register_without_legacy_automation_entities() -> None:
    services = FakeServices()
    hass = SimpleNamespace(data={}, services=services)
    assert not hasattr(hass, "states")

    assert await async_setup(cast(Any, hass), {})

    registrations = {registration[1]: registration for registration in services.registrations}
    assert registrations[SERVICE_REFRESH_SCHEDULER_LIST][0:3] == (
        DOMAIN,
        SERVICE_REFRESH_SCHEDULER_LIST,
        _async_refresh_scheduler_list,
    )
    assert registrations[SERVICE_REFRESH_SCHEDULER_DETAIL][0:3] == (
        DOMAIN,
        SERVICE_REFRESH_SCHEDULER_DETAIL,
        _async_refresh_scheduler_detail,
    )
    assert registrations[SERVICE_QUERY_RUNNING_SERVICE][0:3] == (
        DOMAIN,
        SERVICE_QUERY_RUNNING_SERVICE,
        _async_query_running_service,
    )
    assert registrations[SERVICE_RUN_SCHEDULER_VALIDATION][0:3] == (
        DOMAIN,
        SERVICE_RUN_SCHEDULER_VALIDATION,
        _async_run_scheduler_validation_roundtrip,
    )
    assert registrations[SERVICE_RUN_METER_LIMITATION_SOFT_LIMIT_TRIAL][0:3] == (
        DOMAIN,
        SERVICE_RUN_METER_LIMITATION_SOFT_LIMIT_TRIAL,
        _async_run_meter_limitation_soft_limit_trial,
    )
    assert registrations[SERVICE_SET_SERVICE_REFERENCE_DELAY][0:3] == (
        DOMAIN,
        SERVICE_SET_SERVICE_REFERENCE_DELAY,
        _async_set_service_reference_delay,
    )
    assert all(
        registration[-1]["supports_response"] is SupportsResponse.ONLY
        for registration in services.registrations
    )


@pytest.mark.asyncio
async def test_scheduler_services_setup_is_idempotent() -> None:
    services = FakeServices()
    hass = SimpleNamespace(data={}, services=services)

    assert await async_setup(cast(Any, hass), {})
    first_count = len(services.registrations)
    assert await async_setup(cast(Any, hass), {})

    assert len(services.registrations) == first_count


@pytest.mark.asyncio
async def test_running_service_action_uses_selected_entry_and_only_builds_get():
    manager = FakeManager()

    async def get_user(user_id: str) -> Any:
        assert user_id == "admin"
        return SimpleNamespace(is_admin=True)

    runtime = SimpleNamespace(command_manager=manager)
    hass = SimpleNamespace(
        auth=SimpleNamespace(async_get_user=get_user),
        data={DOMAIN: {"entry-a": runtime}},
    )
    call = ServiceCall(
        cast(Any, hass),
        DOMAIN,
        SERVICE_QUERY_RUNNING_SERVICE,
        {CONF_ENTRY_ID: "entry-a"},
        context=Context(user_id="admin"),
        return_response=True,
    )
    response = await _async_query_running_service(call)

    assert response["status"] == CommandStatus.SUCCESS.value
    assert response["service"] == "runningservice"
    assert response["method"] == "get"
    assert response["message_id"] is None
    assert manager.commands[0].service == "runningservice"
    assert manager.commands[0].method.value == "get"


@pytest.mark.asyncio
async def test_running_service_action_requires_entry_when_multiple_are_loaded():
    runtime = SimpleNamespace(command_manager=FakeManager())
    hass = SimpleNamespace(data={DOMAIN: {"entry-a": runtime, "entry-b": runtime}})
    call = ServiceCall(cast(Any, hass), DOMAIN, SERVICE_QUERY_RUNNING_SERVICE, {})

    with pytest.raises(HomeAssistantError):
        await _async_query_running_service(call)


@pytest.mark.asyncio
async def test_validation_service_checks_release_gate_before_runtime_io() -> None:
    calls = 0

    async def run_roundtrip() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"status": "success"}

    async def get_user(user_id: str) -> Any:
        assert user_id == "admin"
        return SimpleNamespace(is_admin=True)

    runtime = SimpleNamespace(
        release_gate=SimpleNamespace(live_writes_enabled=False),
        async_run_scheduler_validation_roundtrip=run_roundtrip,
    )
    hass = SimpleNamespace(
        auth=SimpleNamespace(async_get_user=get_user),
        data={DOMAIN: {"entry-a": runtime}},
    )
    call = ServiceCall(
        cast(Any, hass),
        DOMAIN,
        SERVICE_RUN_SCHEDULER_VALIDATION,
        {CONF_ENTRY_ID: "entry-a"},
        context=Context(user_id="admin"),
        return_response=True,
    )

    response = await _async_run_scheduler_validation_roundtrip(call)

    assert response["reason"] == "release_gate_closed"
    assert calls == 0



@pytest.mark.asyncio
async def test_validation_service_rejects_non_admin_before_runtime_io() -> None:
    calls = 0

    async def run_roundtrip() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"status": "success"}

    async def get_user(user_id: str) -> Any:
        assert user_id == "viewer"
        return SimpleNamespace(is_admin=False)

    runtime = SimpleNamespace(
        release_gate=SimpleNamespace(live_writes_enabled=True),
        async_run_scheduler_validation_roundtrip=run_roundtrip,
    )
    hass = SimpleNamespace(
        auth=SimpleNamespace(async_get_user=get_user),
        data={DOMAIN: {"entry-a": runtime}},
    )
    call = ServiceCall(
        cast(Any, hass),
        DOMAIN,
        SERVICE_RUN_SCHEDULER_VALIDATION,
        {CONF_ENTRY_ID: "entry-a"},
        context=Context(user_id="viewer"),
        return_response=True,
    )

    with pytest.raises(HomeAssistantError, match="Administrator"):
        await _async_run_scheduler_validation_roundtrip(call)
    assert calls == 0


@pytest.mark.asyncio
async def test_reference_delay_service_requires_admin_and_delegates_exact_current():
    calls: list[int] = []
    observed: list[int] = []

    async def set_delay(expected: int) -> dict[str, Any]:
        calls.append(expected)
        return {"status": "configured", "before": expected, "after": 10}

    async def get_user(user_id: str) -> Any:
        assert user_id == "admin"
        return SimpleNamespace(is_admin=True)

    runtime = SimpleNamespace(
        gateway_scheduler=SimpleNamespace(set_service_reference_delay=set_delay),
        load_balancing_observer=SimpleNamespace(
            apply_verified_reference_delay=observed.append
        ),
    )
    hass = SimpleNamespace(
        auth=SimpleNamespace(async_get_user=get_user),
        data={DOMAIN: {"entry-a": runtime}},
    )
    call = ServiceCall(
        cast(Any, hass),
        DOMAIN,
        SERVICE_SET_SERVICE_REFERENCE_DELAY,
        _REFERENCE_DELAY_SERVICE_SCHEMA(
            {
                CONF_ENTRY_ID: "entry-a",
                CONF_EXPECTED_SERVICE_REFERENCE_DELAY: 1,
            }
        ),
        context=Context(user_id="admin"),
        return_response=True,
    )

    response = await _async_set_service_reference_delay(call)

    assert response == {
        "status": "configured",
        "before": 1,
        "after": 10,
        "observer_cache_updated": True,
    }
    assert calls == [1]
    assert observed == [10]


@pytest.mark.asyncio
async def test_meter_limitation_trial_requires_admin_and_delegates_bounds():
    calls: list[tuple[str, int]] = []

    async def run_trial(direction: str, correction: int) -> dict[str, Any]:
        calls.append((direction, correction))
        return {"status": "completed_and_restored", "restored": True}

    async def get_user(user_id: str) -> Any:
        assert user_id == "admin"
        return SimpleNamespace(is_admin=True)

    runtime = SimpleNamespace(
        gateway_scheduler=SimpleNamespace(
            run_meter_limitation_soft_limit_trial=run_trial
        )
    )
    hass = SimpleNamespace(
        auth=SimpleNamespace(async_get_user=get_user),
        data={DOMAIN: {"entry-a": runtime}},
    )
    call = ServiceCall(
        cast(Any, hass),
        DOMAIN,
        SERVICE_RUN_METER_LIMITATION_SOFT_LIMIT_TRIAL,
        _METER_LIMITATION_TRIAL_SERVICE_SCHEMA(
            {
                CONF_ENTRY_ID: "entry-a",
                CONF_METER_LIMITATION_TRIAL_DIRECTION: "exp",
                CONF_METER_LIMITATION_TRIAL_MAX_CORRECTION: 300,
            }
        ),
        context=Context(user_id="admin"),
        return_response=True,
    )

    response = await _async_run_meter_limitation_soft_limit_trial(call)

    assert response == {"status": "completed_and_restored", "restored": True}
    assert calls == [("exp", 300)]



def test_live_writes_remain_disabled():
    assert LIVE_WRITES_ENABLED is False


def test_running_service_action_schema_rejects_generic_mqtt_arguments():
    with pytest.raises(Exception):
        _SERVICE_SCHEMA({"topic": "pixii/control", "payload": "{}"})


class FakeSchedulerManager:
    def __init__(self, *, error: Exception | None = None, authenticated: bool = True) -> None:
        self.error = error
        self.authenticated = authenticated
        self.list_calls: list[tuple[int, int]] = []
        self.detail_calls: list[int] = []
        self.mutation_calls = 0

    async def refresh_list(self, from_epoch: int, to_epoch: int) -> tuple[SchedulerEntry, ...]:
        self.list_calls.append((from_epoch, to_epoch))
        if self.error is not None:
            raise self.error
        return (
            SchedulerEntry(
                entry=1,
                duration_seconds=60,
                enabled=True,
                sch_index=4127,
                recurrence=False,
                start_epoch=120,
                service_no=2,
                service_type=3,
            ),
        )

    async def refresh_detail(self, sch_index: int) -> SchedulerDetail:
        self.detail_calls.append(sch_index)
        if self.error is not None:
            raise self.error
        return SchedulerDetail(
            sch_index=sch_index,
            asset_id="fixture-asset",
            duration_seconds=60,
            recurrence={"raw": "fixture"},
            service_enabled=True,
            service_no=2,
            service_priority=3,
            service_type=4,
            source_id=5,
            start_epoch=120,
        )

    def diagnostics(self) -> dict[str, object]:
        return {"client": {"sessid_in_memory": self.authenticated}}


def _scheduler_hass(
    manager: Any,
    *,
    multiple: bool = False,
) -> tuple[Any, FakeServices]:
    runtimes = {"entry-a": SimpleNamespace(scheduler_manager=manager)}
    if multiple:
        runtimes["entry-b"] = SimpleNamespace(scheduler_manager=FakeSchedulerManager())
    services = FakeServices()
    hass = SimpleNamespace(data={DOMAIN: runtimes}, services=services)
    services.hass = hass
    return hass, services


@pytest.mark.asyncio
async def test_registered_scheduler_services_use_read_paths_without_mutation() -> None:
    manager = FakeSchedulerManager()
    hass, services = _scheduler_hass(manager)
    assert await async_setup(cast(Any, hass), {})

    list_response = await services.async_call(
        DOMAIN,
        SERVICE_REFRESH_SCHEDULER_LIST,
        {
            CONF_ENTRY_ID: "entry-a",
            CONF_SCHEDULER_DAYS_BACK: 1,
            CONF_SCHEDULER_DAYS_FORWARD: 2,
        },
        return_response=True,
    )
    detail_response = await services.async_call(
        DOMAIN,
        SERVICE_REFRESH_SCHEDULER_DETAIL,
        {CONF_ENTRY_ID: "entry-a", CONF_SCHEDULER_INDEX: 4127},
        return_response=True,
    )

    assert list_response["status"] == "success"
    assert list_response["entry_count"] == 1
    assert detail_response == {
        "status": "success",
        "authenticated": True,
        "sch_index_match": True,
        "parsed": True,
    }
    assert len(manager.list_calls) == 1
    assert manager.detail_calls == [4127]
    assert manager.mutation_calls == 0


@pytest.mark.asyncio
async def test_scheduler_reads_remain_available_with_production_and_automation_disabled() -> None:
    manager = FakeSchedulerManager()
    hass, services = _scheduler_hass(manager)
    hass.data[DOMAIN]["entry-a"].release_gate = SimpleNamespace(live_writes_enabled=False)
    hass.data[DOMAIN]["entry-a"].production_control_enabled = lambda: False
    hass.data[DOMAIN]["entry-a"].automation_policy = "disabled"
    assert await async_setup(cast(Any, hass), {})

    response = await services.async_call(
        DOMAIN,
        SERVICE_REFRESH_SCHEDULER_LIST,
        {
            CONF_ENTRY_ID: "entry-a",
            CONF_SCHEDULER_DAYS_BACK: 0,
            CONF_SCHEDULER_DAYS_FORWARD: 0,
        },
        return_response=True,
    )

    assert response["status"] == "success"
    assert manager.mutation_calls == 0


@pytest.mark.asyncio
async def test_scheduler_service_requires_explicit_entry_when_multiple_loaded() -> None:
    manager = FakeSchedulerManager()
    hass, services = _scheduler_hass(manager, multiple=True)
    assert await async_setup(cast(Any, hass), {})

    with pytest.raises(HomeAssistantError, match="config_entry_id"):
        await services.async_call(
            DOMAIN,
            SERVICE_REFRESH_SCHEDULER_DETAIL,
            {CONF_SCHEDULER_INDEX: 4127},
            return_response=True,
        )
    assert manager.detail_calls == []


@pytest.mark.asyncio
async def test_invalid_scheduler_index_fails_before_detail_request() -> None:
    manager = FakeSchedulerManager()
    hass, services = _scheduler_hass(manager)
    assert await async_setup(cast(Any, hass), {})

    with pytest.raises(Exception):
        await services.async_call(
            DOMAIN,
            SERVICE_REFRESH_SCHEDULER_DETAIL,
            {CONF_ENTRY_ID: "entry-a", CONF_SCHEDULER_INDEX: -1},
            return_response=True,
        )

@pytest.mark.asyncio
async def test_missing_scheduler_configuration_fails_without_network_io() -> None:
    hass, services = _scheduler_hass(None)
    assert await async_setup(cast(Any, hass), {})

    response = await services.async_call(
        DOMAIN,
        SERVICE_REFRESH_SCHEDULER_DETAIL,
        {CONF_ENTRY_ID: "entry-a", CONF_SCHEDULER_INDEX: 4127},
        return_response=True,
    )

    assert response == {
        "status": "error",
        "authenticated": False,
        "error": "configuration",
    }


@pytest.mark.asyncio
async def test_scheduler_auth_failure_is_sanitized_without_retry() -> None:
    manager = FakeSchedulerManager(
        error=SchedulerAuthenticationError(
            SchedulerAuthenticationReason.SESSION_NOT_AUTHORIZED,
            SchedulerAuthenticationStage.DETAIL,
        ),
        authenticated=False,
    )
    hass, services = _scheduler_hass(manager)
    assert await async_setup(cast(Any, hass), {})

    response = await services.async_call(
        DOMAIN,
        SERVICE_REFRESH_SCHEDULER_DETAIL,
        {CONF_ENTRY_ID: "entry-a", CONF_SCHEDULER_INDEX: 4127},
        return_response=True,
    )

    assert response == {
        "status": "error",
        "authenticated": False,
        "error": "authentication",
        "authentication_reason": "session_not_authorized",
        "authentication_stage": "detail",
    }
    assert manager.detail_calls == [4127]
    assert manager.mutation_calls == 0



@pytest.mark.asyncio
async def test_scheduler_list_auth_failure_is_sanitized_without_retry() -> None:
    manager = FakeSchedulerManager(
        error=SchedulerAuthenticationError(
            SchedulerAuthenticationReason.SESSION_NOT_AUTHORIZED,
            SchedulerAuthenticationStage.LIST,
        ),
        authenticated=False,
    )
    hass, services = _scheduler_hass(manager)
    assert await async_setup(cast(Any, hass), {})

    response = await services.async_call(
        DOMAIN,
        SERVICE_REFRESH_SCHEDULER_LIST,
        {
            CONF_ENTRY_ID: "entry-a",
            CONF_SCHEDULER_DAYS_BACK: 1,
            CONF_SCHEDULER_DAYS_FORWARD: 1,
        },
        return_response=True,
    )

    assert response == {
        "status": "error",
        "entry_count": 0,
        "authenticated": False,
        "error": "authentication",
        "authentication_reason": "session_not_authorized",
        "authentication_stage": "list",
    }
    assert len(manager.list_calls) == 1
    assert manager.mutation_calls == 0

def test_scheduler_service_schemas_enforce_bounded_inputs() -> None:
    assert _SCHEDULER_SERVICE_SCHEMA(
        {
            CONF_SCHEDULER_DAYS_BACK: 31,
            CONF_SCHEDULER_DAYS_FORWARD: 31,
        }
    )
    assert _SCHEDULER_DETAIL_SERVICE_SCHEMA({CONF_SCHEDULER_INDEX: 0})
    with pytest.raises(Exception):
        _SCHEDULER_SERVICE_SCHEMA(
            {
                CONF_SCHEDULER_DAYS_BACK: 32,
                CONF_SCHEDULER_DAYS_FORWARD: 0,
            }
        )
    for invalid_days_back, invalid_days_forward in (
        (-0.5, 1),
        (31.9, 1),
        (True, 1),
    ):
        with pytest.raises(Exception):
            _SCHEDULER_SERVICE_SCHEMA(
                {
                    CONF_SCHEDULER_DAYS_BACK: invalid_days_back,
                    CONF_SCHEDULER_DAYS_FORWARD: invalid_days_forward,
                }
            )
    with pytest.raises(Exception):
        _SCHEDULER_DETAIL_SERVICE_SCHEMA({CONF_SCHEDULER_INDEX: -1})
    with pytest.raises(Exception):
        _SCHEDULER_DETAIL_SERVICE_SCHEMA(
            {CONF_SCHEDULER_INDEX: 4127, "payload": {}}
        )
