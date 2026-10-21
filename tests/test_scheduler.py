from __future__ import annotations

import json
import ssl
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import pytest

from custom_components.pixiihome_controller.exceptions import (
    SchedulerAuthenticationError,
    SchedulerAuthenticationReason,
    SchedulerAuthenticationStage,
    SchedulerClientError,
    SchedulerConfigurationError,
    SchedulerProtocolError,
)
from custom_components.pixiihome_controller.protocol.scheduler import (
    build_detail_request,
    build_list_request,
    parse_detail_request,
    parse_detail_response,
    parse_list_request,
    parse_list_response,
)
from custom_components.pixiihome_controller.scheduler import client as scheduler_client
from custom_components.pixiihome_controller.scheduler.client import (
    MAX_SCHEDULER_RESPONSE_BYTES,
    AiohttpSchedulerReadClient,
    SanitizedFixtureSchedulerClient,
    SchedulerClientConfig,
)
from custom_components.pixiihome_controller.scheduler.client import (
    _extract_bootstrap_id_legacy_compatibility_probe as extract_bootstrap_id,
)
from custom_components.pixiihome_controller.scheduler.manager import (
    SCHEDULER_LIST_ENTRY_LIMIT,
    SchedulerRefreshManager,
)
from custom_components.pixiihome_controller.scheduler.models import (
    Ownership,
    default_ownership,
)

from .conftest import load_json

BOOTSTRAP_ID = "A" * 32
SESSION_ID = "S" * 32
BOOTSTRAP_COMPAT_ID = "F" * 32
ALEVEL = 9

class _TextContent:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def read(self, limit: int) -> bytes:
        return self._body[:limit]


class _FakeResponse:
    def __init__(
        self,
        status: int,
        body: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self._body = body
        self.headers = headers or {}
        self.content = _TextContent(body.encode())

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *args: Any) -> None:
        del args

    async def text(self) -> str:
        return self._body


class _StreamingContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def iter_chunked(self, size: int):
        del size
        for chunk in self._chunks:
            yield chunk


class _StreamingResponse(_FakeResponse):
    def __init__(
        self,
        status: int,
        chunks: list[bytes],
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(status, "", headers=headers)
        self.content = _StreamingContent(chunks)
        self.charset = "utf-8"

class _FakeSession:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def _request_payload(call: tuple[str, dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    query = parse_qs(urlsplit(call[0]).query)
    data = query["data"][0]
    prefix, json_text = data.split("json:", 1)
    return prefix, json.loads(json_text)


def test_list_and_detail_fixtures_parse():
    request = parse_list_request(load_json("scheduler/list_request.json"))
    entries = parse_list_response(load_json("scheduler/list_response.json"))
    detail_request = parse_detail_request(load_json("scheduler/detail_request.json"))
    detail = parse_detail_response(load_json("scheduler/detail_response.json"))

    assert request.from_epoch < request.to_epoch
    assert [entry.sch_index for entry in entries] == [4100, 4101]
    assert detail_request.sch_index == 4127
    assert detail.sch_index == 4127
    assert detail.service_no != detail.sch_index
    assert datetime.fromtimestamp(detail.start_epoch, UTC).tzinfo is UTC


@pytest.mark.asyncio
async def test_scheduler_refresh_manager_normalizes_read_only_views():
    list_request = parse_list_request(load_json("scheduler/list_request.json"))
    client = SanitizedFixtureSchedulerClient(
        load_json("scheduler/list_response.json"),
        {4127: load_json("scheduler/detail_response.json")},
    )
    manager = SchedulerRefreshManager(client, clock=lambda: 123.0)

    entries = await manager.refresh_list(list_request.from_epoch, list_request.to_epoch)
    detail = await manager.refresh_detail(4127)

    assert [entry.sch_index for entry in entries] == [4100, 4101]
    assert detail.sch_index == 4127
    assert [view.sch_index for view in manager.views] == [4100, 4101, 4127]
    assert all(view.ownership.ownership is Ownership.UNKNOWN for view in manager.views)
    assert all(view.ownership.protected for view in manager.views)
    diagnostics = manager.diagnostics()
    assert diagnostics["status"] == "read_only_refresh_manager"
    assert diagnostics["list_entry_count"] == 2
    assert diagnostics["detail_count"] == 1
    assert diagnostics["normalized_schedule_count"] == 3
    assert diagnostics["complete_schedule_count"] == 0
    assert diagnostics["partial_schedule_count"] == 3
    assert diagnostics["mismatch_count"] == 0
    assert diagnostics["view_count"] == 3
    assert diagnostics["ownership"] == {
        "integration_owned": 0,
        "external": 0,
        "unknown_protected": 3,
    }
    assert diagnostics["list_refresh_attempts"] == 1
    assert diagnostics["list_refresh_successes"] == 1
    assert diagnostics["detail_refresh_attempts"] == 1
    assert diagnostics["detail_refresh_successes"] == 1
    assert diagnostics["refresh_failures"] == 0
    assert diagnostics["last_error"] is None
    assert diagnostics["last_refresh_at"] == 123.0
    visibility = cast(dict[str, Any], diagnostics["list_visibility"])
    assert visibility["fresh"] is True
    assert visibility["invalidated"] is False
    assert visibility["from_epoch"] == list_request.from_epoch
    assert visibility["to_epoch"] == list_request.to_epoch


@pytest.mark.asyncio
async def test_scheduler_refresh_manager_reports_sanitized_failure():
    list_response = load_json("scheduler/list_response.json")
    list_response["Omnibus"]["msg"][7] = 200
    client = SanitizedFixtureSchedulerClient(list_response, {})
    manager = SchedulerRefreshManager(client)

    with pytest.raises(SchedulerProtocolError):
        await manager.refresh_list(1789250400, 1789855199)

    diagnostics = manager.diagnostics()
    assert diagnostics["refresh_failures"] == 1
    assert diagnostics["last_error"] == "SchedulerProtocolError"


def test_source_id_is_not_ownership():
    detail = parse_detail_response(load_json("scheduler/detail_external_like_response.json"))
    ownership = default_ownership()
    assert detail.source_id == 7
    assert ownership.ownership is Ownership.UNKNOWN
    assert ownership.source_label is None
    assert ownership.editable is False
    assert ownership.protected is True


def test_fixture_contains_no_raw_har_secrets_or_site_data():
    serialized = json.dumps(load_json("scheduler/detail_external_like_response.json"))
    for forbidden in ("Sessid", "Cookie", "installer", "192.168.", "password", "certificate"):
        assert forbidden.lower() not in serialized.lower()


def test_unexpected_scheduler_message_is_rejected():
    payload = load_json("scheduler/list_request.json")
    payload["Omnibus"]["msg"][7] = 200
    with pytest.raises(SchedulerProtocolError):
        parse_list_request(payload)


def test_scheduler_request_builders_match_sanitized_wire_fixtures():
    generated_list = build_list_request(1789250400, 1789855199)
    generated_omnibus = cast(dict[str, object], generated_list["Omnibus"])
    generated_data = cast(dict[str, object], generated_omnibus["data"])
    browser_list = load_json("scheduler/list_request.json")

    assert set(generated_list) == {"cmd", "Omnibus"}
    assert generated_list["cmd"] == "message"
    assert generated_omnibus["msg"] == [
        1,
        0,
        5,
        3,
        255,
        2,
        101,
        52,
        0,
        33,
        0,
        0,
    ]
    assert generated_data == {
        "from": 1789250400,
        "to": 1789855199,
    }
    assert all(isinstance(value, int) for value in generated_data.values())
    assert {
        "cmd": generated_list["cmd"],
        "Omnibus": generated_omnibus,
    } == {
        "cmd": browser_list["cmd"],
        "Omnibus": browser_list["Omnibus"],
    }

    generated_detail = build_detail_request(4127)
    browser_detail = load_json("scheduler/detail_request.json")
    assert set(generated_detail) == {"cmd", "Omnibus"}
    assert generated_detail["cmd"] == "message"
    assert generated_detail["Omnibus"] == browser_detail["Omnibus"]
    assert generated_detail == browser_detail


def test_scheduler_request_builders_reject_unbounded_numerics() -> None:
    with pytest.raises(SchedulerProtocolError):
        build_list_request(cast(Any, 1.5), 2)
    with pytest.raises(SchedulerProtocolError):
        build_list_request(0, 10**40)
    with pytest.raises(SchedulerProtocolError):
        build_detail_request(cast(Any, float("inf")))


def test_bootstrap_extraction_requires_one_anchored_property():
    source = (
        'var vars={loginid:"'
        + BOOTSTRAP_ID
        + '", nested:{loginid:"'
        + BOOTSTRAP_ID
        + '"}};'
    )
    assert extract_bootstrap_id(source) == BOOTSTRAP_ID

    duplicate = f'var vars={{loginid:"{BOOTSTRAP_ID}",loginid:"{BOOTSTRAP_ID}"}};'
    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(duplicate)


def test_bootstrap_extraction_accepts_sanitized_multi_vars_shape_rejected_by_114_guard() -> None:
    # Minimum sanitized structural reproduction; it does not claim the live response's exact order.
    source = (
        Path(__file__).parent
        / "fixtures"
        / "scheduler"
        / "bootstrap_multiple_vars.js"
    ).read_text(encoding="utf-8")

    assert extract_bootstrap_id(source) == BOOTSTRAP_COMPAT_ID


@pytest.mark.parametrize(
    "source",
    [
        f'var vars={{theme:"dark"}}; var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};',
        (
            f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
            'var unrelated={["theme"]:"dark"};'
        ),
    ],
    ids=["same-binding-unrelated-before", "distinct-binding-unrelated-after"],
)
def test_bootstrap_extraction_ignores_unrelated_vars_objects(source: str) -> None:
    assert extract_bootstrap_id(source) == BOOTSTRAP_COMPAT_ID


@pytest.mark.parametrize(
    "source",
    [
        (
            f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
            f'var vars={{["loginid"]:"{BOOTSTRAP_ID}"}};'
        ),
        (
            f'var vars={{["loginid"]:"{BOOTSTRAP_ID}"}};'
            f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
        ),
    ],
    ids=["computed-after-candidate", "computed-before-candidate"],
)
def test_bootstrap_extraction_rejects_relevant_computed_loginid(source: str) -> None:
    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)


def test_bootstrap_extraction_rejects_unanchored_vars_replacement() -> None:
    source = (
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
        'var vars={theme:"dark"};'
    )

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)


@pytest.mark.parametrize(
    "source",
    [
        (
            'var unrelated={["loginid"]:"FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF"};'
            f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
        ),
        (
            f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
            'var unrelated={["loginid"]:"FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF"};'
        ),
    ],
    ids=["computed-decoy-before", "computed-decoy-after"],
)
def test_bootstrap_extraction_ignores_proven_unrelated_computed_key(
    source: str,
) -> None:
    assert extract_bootstrap_id(source) == BOOTSTRAP_COMPAT_ID

def test_bootstrap_extraction_accepts_unrelated_vars_property_name() -> None:
    source = (
        'var decoy={vars:"fixture"};'
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
    )

    assert extract_bootstrap_id(source) == BOOTSTRAP_COMPAT_ID


@pytest.mark.parametrize(
    "property_source",
    ["vars", "value:vars", '[vars]:"fixture"'],
    ids=["shorthand-reference", "value-reference", "computed-key-reference"],
)
def test_bootstrap_extraction_rejects_unrelated_object_vars_references(
    property_source: str,
) -> None:
    source = (
        f'var decoy={{{property_source}}};'
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
    )

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)



@pytest.mark.parametrize(
    "mutation",
    [
        'vars["loginid"]="FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF";',
        'vars.loginid="FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF";',
    ],
    ids=["computed-property", "direct-property"],
)
def test_bootstrap_extraction_rejects_loginid_mutation(mutation: str) -> None:
    source = f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};{mutation}'

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)


def test_bootstrap_extraction_rejects_computed_global_vars_replacement() -> None:
    source = (
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
        'window["vars"]={theme:"dark"};'
    )

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)

def test_bootstrap_extraction_rejects_computed_global_vars_from_call_result() -> None:
    source = (
        'const decoy={};'
        'function getGlobal(x){return window;}'
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
        'getGlobal(decoy)["vars"]={theme:"dark"};'
    )

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)

@pytest.mark.parametrize(
    "selector",
    ['"vars"', "key"],
    ids=["static-vars", "dynamic-key"],
)
def test_bootstrap_extraction_rejects_grouped_compound_global_replacement(
    selector: str,
) -> None:
    source = (
        'const decoy={};'
        'var key="vars";'
        'var vars={theme:"dark"};'
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
        f'(window || decoy)[{selector}]={{theme:"dark"}};'
    )

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)

@pytest.mark.parametrize(
    ("receiver", "selector"),
    [
        pytest.param("holder.target", '"vars"', id="dotted-static-vars"),
        pytest.param('holder["target"]', "key", id="computed-dynamic-key"),
    ],
)
def test_bootstrap_extraction_rejects_nested_local_object_global_replacement(
    receiver: str,
    selector: str,
) -> None:
    source = (
        'const holder={target:window};'
        'var key="vars";'
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
        f'{receiver}[{selector}]={{theme:"dark"}};'
    )

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)

@pytest.mark.parametrize(
    "selector",
    ['"v"+"ars"', '("vars")', "key"],
    ids=["concatenated-alias", "parenthesized-alias", "dynamic-key"],
)
def test_bootstrap_extraction_rejects_computed_global_vars_aliases(
    selector: str,
) -> None:
    source = (
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
        'var key="vars";'
        f'window[{selector}]={{theme:"dark"}};'
    )

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)


def test_bootstrap_extraction_rejects_computed_global_alias_chain() -> None:
    source = (
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
        'var globalAlias=window;'
        'globalAlias["v"+"ars"]={theme:"dark"};'
    )

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)


def test_bootstrap_extraction_accepts_inert_global_member_literal() -> None:
    source = (
        'window["theme"]={theme:"dark"};'
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
    )

    assert extract_bootstrap_id(source) == BOOTSTRAP_COMPAT_ID


@pytest.mark.parametrize(
    "receiver",
    ["decoy", "(decoy)", "((decoy))"],
    ids=["direct-local", "grouped-local", "nested-grouped-local"],
)
def test_bootstrap_extraction_accepts_computed_access_on_const_object(
    receiver: str,
) -> None:
    source = (
        'const decoy={};'
        f'{receiver}[key]="fixture";'
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
    )

    assert extract_bootstrap_id(source) == BOOTSTRAP_COMPAT_ID

@pytest.mark.parametrize(
    "target",
    ["window", "globalAlias", "unknownTarget"],
    ids=["global", "known-global-alias", "unknown-target"],
)
def test_bootstrap_extraction_rejects_reflect_set_vars_replacement(
    target: str,
) -> None:
    source = (
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
        "const globalAlias=window;"
        f'Reflect.set({target}, "vars", {{theme:"replacement"}});'
    )

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)


@pytest.mark.parametrize(
    "target",
    ["window", "unknownTarget"],
    ids=["global", "unknown"],
)
@pytest.mark.parametrize(
    "key_expression",
    ['key', '"v"+"ars"', '("vars")'],
    ids=["dynamic-identifier", "composed-string", "parenthesized-string"],
)
def test_bootstrap_extraction_rejects_dynamic_reflect_set_key(
    target: str,
    key_expression: str,
) -> None:
    source = (
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
        f'Reflect.set({target}, {key_expression}, {{theme:"replacement"}});'
    )

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)


def test_bootstrap_extraction_rejects_reflect_set_loginid_mutation() -> None:
    source = (
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
        f'Reflect.set(vars, "loginid", "{BOOTSTRAP_ID}");'
    )

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)


def test_bootstrap_extraction_ignores_reflect_set_on_unrelated_objects() -> None:
    source = (
        'const decoy={theme:"fixture"};'
        'Reflect.set(decoy, key, "changed");'
        'Reflect.set(window, `theme`, "dark");'
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
    )

    assert extract_bootstrap_id(source) == BOOTSTRAP_COMPAT_ID


@pytest.mark.parametrize(
    "call",
    [
        "mutate(window);",
        "new Mutator(window);",
        "mutate(...window);",
    ],
    ids=["plain-call", "constructor-call", "spread-argument"],
)
def test_bootstrap_extraction_rejects_uninterpretable_call_tied_to_global(
    call: str,
) -> None:
    source = (
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
        "function mutate(target) { return target; }"
        f"{call}"
    )

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)


@pytest.mark.parametrize(
    "receiver",
    ["window.vars.settings", 'window["vars"]["settings"]'],
    ids=["member-chain", "computed-member-chain"],
)
def test_bootstrap_extraction_rejects_call_on_nested_vars_member(
    receiver: str,
) -> None:
    source = f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};{receiver}.refresh();'

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)


def test_bootstrap_extraction_rejects_object_assign_to_global() -> None:
    source = (
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
        'Object.assign(window, {vars:{theme:"replacement"}});'
    )

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)


@pytest.mark.parametrize(
    "target",
    ["window || {}", "condition ? window : {}"],
    ids=["logical-expression", "conditional-expression"],
)
def test_bootstrap_extraction_rejects_compound_global_mutator_argument(
    target: str,
) -> None:
    source = (
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
        + f"Object.assign({target}, "
        + '{"vars":{loginid:"'
        + BOOTSTRAP_ID
        + '"}});'
    )

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)


class _IndexedReadTokens(list[Any]):
    def __init__(self, tokens: list[Any]) -> None:
        super().__init__(tokens)
        self.indexed_reads = 0
        self.line_terminators_before = set(
            getattr(tokens, "line_terminators_before", ())
        )

    def __getitem__(self, index: int | slice) -> Any:
        self.indexed_reads += 1
        return super().__getitem__(index)


@pytest.mark.parametrize(
    ("expression", "prefix", "accepts"),
    [
        pytest.param(
            'decoy["vars"]' + '?.["vars"]' * 4096,
            "const decoy={};",
            False,
            id="nested-computed-member",
        ),
        pytest.param(
            "decoy[key]" + ";decoy[key]" * 4095,
            'const decoy={};var key="theme";',
            True,
            id="repeated-direct-local-computed-members",
        ),
        pytest.param(
            "window" + '["window"]' * 4096,
            "",
            True,
            id="nested-computed-global-identity-members",
        ),
        pytest.param(
            "decoy.f()" + ".f()" * 2048,
            "const decoy={f(){return this;}};",
            True,
            id="chained-calls",
        ),
        pytest.param(
            "`prefix-${" + "other+" * 4096 + "other}-suffix`",
            "const other=1;",
            True,
            id="repeated-template-expression",
        ),
    ],
)
def test_bootstrap_extraction_bounds_repeated_expression_work(
    monkeypatch: pytest.MonkeyPatch,
    expression: str,
    prefix: str,
    accepts: bool,
) -> None:
    original_tokenize = scheduler_client._tokenize_javascript
    tokenized: _IndexedReadTokens | None = None

    def _count_indexed_reads(source: str) -> _IndexedReadTokens:
        nonlocal tokenized
        tokenized = _IndexedReadTokens(original_tokenize(source))
        return tokenized

    monkeypatch.setattr(
        scheduler_client,
        "_tokenize_javascript",
        _count_indexed_reads,
    )
    source = (
        prefix
        + f"{expression};"
        + f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
    )

    if accepts:
        assert extract_bootstrap_id(source) == BOOTSTRAP_COMPAT_ID
    else:
        with pytest.raises(SchedulerClientError):
            extract_bootstrap_id(source)
    assert tokenized is not None
    assert tokenized.indexed_reads <= len(tokenized) * 100


def test_bootstrap_extraction_accepts_unrelated_normal_calls() -> None:
    source = (
        'function notify(message) { return message; }'
        'notify("bootstrap ready");'
        'Reflect.set(window, "theme", "dark");'
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
    )

    assert extract_bootstrap_id(source) == BOOTSTRAP_COMPAT_ID


@pytest.mark.parametrize(
    ("before", "after"),
    [
        pytest.param(
            "const localPayload={note:'fixture'};",
            "window.fakeMethod(localPayload);",
            id="global-call-with-local-argument",
        ),
        pytest.param(
            "const localObject={};",
            "localObject[method]();",
            id="dynamic-local-member-call",
        ),
        pytest.param(
            "",
            "window?.fakeMethod?.('fixture');",
            id="optional-global-member-call",
        ),
        pytest.param(
            "",
            "window['fakeMethod']('fixture');",
            id="literal-computed-global-member-call",
        ),
        pytest.param(
            "",
            "window[`fakeMethod`]('fixture');",
            id="static-template-global-member-call",
        ),
        pytest.param(
            "",
            "(window.fakeMethod)({label:'vars',note:'loginid'});",
            id="grouped-global-call-with-fake-literal",
        ),
        pytest.param(
            "const globalAlias=window;",
            "globalAlias.initialize();",
            id="global-alias-call-without-arguments",
        ),
        pytest.param(
            "var globalAlias;globalAlias=window;",
            "globalAlias.initialize();",
            id="simple-global-alias-reassignment",
        ),
        pytest.param(
            "var globalAlias;globalAlias=window;var chainedAlias;"
            "chainedAlias=globalAlias;",
            "chainedAlias.initialize();",
            id="transitive-global-alias-reassignment",
        ),
        pytest.param(
            "function initialize(localPayload){"
            "window.fakeMethod(localPayload);}",
            "",
            id="nested-function-global-call",
        ),
        pytest.param(
            "function install(window){return 1;}",
            "window.initialize();",
            id="unrelated-shadow-does-not-taint-global-call",
        ),
        pytest.param(
            "var globalAlias;globalAlias=window;"
            "function install(window){return 1;}",
            "globalAlias.initialize();",
            id="unrelated-shadow-does-not-taint-global-alias-call",
        ),
        pytest.param(
            "",
            'window?.["initialize"]();',
            id="optional-computed-global-member-call",
        ),
        pytest.param(
            "function install(eval){eval('fixture');}",
            "",
            id="shadowed-direct-eval-is-not-global",
        ),
        pytest.param(
            "function install(setTimeout){setTimeout('fixture',0);}",
            "",
            id="shadowed-direct-timer-is-not-global",
        ),
        pytest.param(
            "",
            "setTimeout(()=>1, 1);",
            id="timer-call-with-function-expression",
        ),
        pytest.param(
            "",
            "const output=`ready-${window.fakeMethod('vars')}`;",
            id="template-interpolation-global-call",
        ),
        pytest.param(
            "const globalAlias=window;",
            "window.first();globalAlias.second('vars');"
            "self.third({note:'loginid'});",
            id="multiple-global-calls",
        ),
        pytest.param(
            "window.beforeBootstrap();",
            "",
            id="global-call-before-bootstrap",
        ),
        pytest.param(
            "",
            "window.afterBootstrap();",
            id="global-call-after-bootstrap",
        ),
        pytest.param(
            "",
            "if(condition){window.inBranch({label:'vars'});}",
            id="unrelated-branch-global-call",
        ),
        pytest.param(
            "function first(){(window.prepare)('fixture');}"
            "function second(){globalThis.observe({label:'vars'});}"
            "function third(){self.notify();}",
            "",
            id="sanitized-amendment-7b-multi-call-shape",
        ),
    ],
)
def test_bootstrap_extraction_accepts_global_receiver_calls_without_relevant_flow(
    before: str,
    after: str,
) -> None:
    source = (
        before
        + f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
        + after
    )

    assert extract_bootstrap_id(source) == BOOTSTRAP_COMPAT_ID


@pytest.mark.parametrize(
    "operation",
    [
        pytest.param("vars.refresh();", id="direct-vars-receiver"),
        pytest.param(
            "window.vars.settings.refresh();",
            id="vars-derived-receiver",
        ),
        pytest.param("window.consume(vars);", id="global-call-receives-vars"),
        pytest.param(
            "window.consume(vars.loginid);",
            id="global-call-receives-loginid",
        ),
        pytest.param(
            "window.consume({value:vars.loginid});",
            id="global-call-receives-relevant-object",
        ),
        pytest.param(
            'window.register(function(){vars.loginid="'
            + BOOTSTRAP_ID
            + '";});',
            id="callback-mutates-loginid",
        ),
        pytest.param(
            "window.register(function(){loginid='fixture';});",
            id="callback-mutates-bare-loginid",
        ),
        pytest.param(
            "window.register(()=>{++loginid;});",
            id="callback-preincrements-bare-loginid",
        ),
        pytest.param(
            "window.register(()=>{storage.loginid++;});",
            id="callback-postincrements-unproven-loginid",
        ),
        pytest.param(
            "window.register(()=>{({value:loginid}=payload);});",
            id="callback-mutates-destructured-loginid",
        ),
        pytest.param(
            "vars=window.getVars();",
            id="call-result-assigned-to-vars",
        ),
        pytest.param(
            "window.vars=window.getVars();",
            id="call-result-replaces-global-vars",
        ),
        pytest.param(
            "window.vars.loginid=window.getLoginId();",
            id="call-result-assigned-to-loginid",
        ),
        pytest.param(
            'window.vars["loginid"]=window.getLoginId();',
            id="computed-loginid-call-result",
        ),
        pytest.param(
            "loginid=window.getLoginId();",
            id="bare-loginid-call-result",
        ),
        pytest.param("window[method]();", id="dynamic-global-member-call"),
        pytest.param(
            "function install(window){window.fakeMethod();}",
            id="shadowed-window-parameter",
        ),
        pytest.param(
            'function install(window){window?.["initialize"]();}',
            id="shadowed-optional-computed-global-member",
        ),
        pytest.param(
            "const globalAlias=window;"
            "function install(globalAlias){globalAlias.fakeMethod();}",
            id="shadowed-global-alias-parameter",
        ),
        pytest.param(
            "function install(window){const alias=window;alias.fakeMethod();}",
            id="alias-from-shadowed-global-source",
        ),
        pytest.param(
            "function install(window){const alias=window;const chainedAlias=alias;"
            "chainedAlias.fakeMethod();}",
            id="transitive-alias-from-shadowed-global-source",
        ),
        pytest.param(
            "function install(window){var alias;alias=window;alias.fakeMethod();}",
            id="alias-from-shadowed-global-reassignment",
        ),
        pytest.param(
            "function install(window){var alias;alias=window;var chainedAlias;"
            "chainedAlias=alias;chainedAlias.fakeMethod();}",
            id="transitive-reassigned-alias-from-shadowed-global-source",
        ),
        pytest.param(
            "var alias;alias=window;alias=localObject;alias.fakeMethod();",
            id="reassigned-global-alias-from-unknown-source",
        ),
        pytest.param(
            "var globalAlias;globalAlias=window;"
            "({value:globalAlias}={value:localObject});"
            "globalAlias.fakeMethod();",
            id="destructuring-reassigned-global-alias",
        ),
        pytest.param(
            "var globalAlias;globalAlias=window;var chainedAlias;"
            "chainedAlias=globalAlias;"
            "({value:globalAlias}={value:localObject});"
            "chainedAlias.fakeMethod();",
            id="destructuring-reassigned-alias-invalidates-dependent",
        ),
        pytest.param(
            "var globalAlias;globalAlias=window;"
            "for(globalAlias of localValues){}"
            "globalAlias.fakeMethod();",
            id="for-of-reassigned-global-alias",
        ),
        pytest.param(
            "function install(){this.fakeMethod();}",
            id="function-scoped-dynamic-this-member-call",
        ),
        pytest.param(
            "function install(){const thisAlias=this;thisAlias.fakeMethod();}",
            id="alias-derived-from-dynamic-this",
        ),
        pytest.param(
            'const output=`before-${window.consume(vars.loginid)}`;',
            id="relevant-call-in-template-interpolation",
        ),
    ],
)
def test_bootstrap_extraction_rejects_global_calls_with_relevant_flow(
    operation: str,
) -> None:
    source = (
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
        + operation
    )

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)


@pytest.mark.parametrize(
    "operation",
    [
        pytest.param('eval("vars.loginid=\'x\'");', id="direct-eval"),
        pytest.param(
            'Function("vars.loginid=\'x\'")();',
            id="direct-function-constructor",
        ),
        pytest.param(
            'new Function("vars.loginid=\'x\'")();',
            id="new-function-constructor",
        ),
        pytest.param(
            'window.eval("vars.loginid=\'x\'");',
            id="global-eval",
        ),
        pytest.param(
            'window["eval"]("vars.loginid=\'x\'");',
            id="computed-global-eval",
        ),
        pytest.param(
            'window?.["eval"]("vars.loginid=\'x\'");',
            id="optional-computed-global-eval",
        ),
        pytest.param(
            'const globalAlias=window;globalAlias["eval"]("vars.loginid=\'x\'");',
            id="global-alias-computed-eval",
        ),
        pytest.param(
            'window.eval.call(window,"vars.loginid=\'x\'");',
            id="eval-call-bypass",
        ),
        pytest.param(
            'window.eval.apply(window,["vars.loginid=\'x\'"]);',
            id="eval-apply-bypass",
        ),
        pytest.param(
            'window.eval.bind(window)("vars.loginid=\'x\'");',
            id="eval-bind-bypass",
        ),
        pytest.param(
            'const run=window["Function"];run("vars.loginid=\'x\'")();',
            id="global-function-alias-transfer",
        ),
        pytest.param(
            'window.execScript("vars.loginid=\'x\'");',
            id="global-exec-script",
        ),
        pytest.param(
            'const globalAlias=window;globalAlias.importScripts("payload.js");',
            id="global-alias-import-scripts",
        ),
        pytest.param(
            'window["constructor"]["constructor"]("vars.loginid=\'x\'")();',
            id="computed-global-function-constructor-chain",
        ),
        pytest.param(
            '({})["constructor"]["constructor"]("vars.loginid=\'x\'")();',
            id="computed-object-function-constructor-chain",
        ),
        pytest.param(
            'window.constructor.constructor("vars.loginid=\'x\'")();',
            id="global-function-constructor-chain",
        ),
        pytest.param(
            'setTimeout("vars.loginid=\'x\'",0);',
            id="string-timer-callback",
        ),
        pytest.param("setTimeout(callback,0);", id="unknown-timer-callback"),
        pytest.param(
            "window.setInterval(callback,100);",
            id="unknown-global-timer-callback",
        ),
        pytest.param(
            "const later=window.setTimeout;later(()=>{},0);",
            id="global-timer-alias-transfer",
        ),
        pytest.param(
            'window?.["setInterval"](callback,100);',
            id="optional-computed-timer-unknown-callback",
        ),
    ],
)
def test_bootstrap_extraction_rejects_code_execution_and_dynamic_timers(
    operation: str,
) -> None:
    source = (
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
        + operation
    )

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)

@pytest.mark.parametrize(
    ("operation", "rejects"),
    [
        pytest.param(
            "const install=(window)=>flag && window.fakeMethod();",
            True,
            id="binary-expression-body-shadow",
        ),
        pytest.param(
            'const install=window=>flag ? 0 : window?.["initialize"]();',
            True,
            id="conditional-expression-body-shadow",
        ),
        pytest.param(
            "const install=(window)=>flag && 1,"
            "run=window.initialize();",
            False,
            id="comma-ends-shadow-range",
        ),
        pytest.param(
            "const install=(window)=>flag && 1\n"
            "window.initialize();",
            False,
            id="line-break-ends-expression-before-next-statement",
        ),
        pytest.param(
            "const install=(window)=>flag &&\nwindow.fakeMethod();",
            True,
            id="line-break-after-binary-operator",
        ),
        pytest.param(
            "const install=(window)=>flag && 1/*line\nbreak*/"
            "window.initialize();",
            False,
            id="block-comment-line-break-ends-expression",
        ),
        pytest.param(
            "const install=(window)=>flag && `value-${window.fakeMethod()}`;",
            True,
            id="template-interpolation-in-expression-body",
        ),
    ],
)
def test_bootstrap_extraction_tracks_complete_expression_arrow_scopes(
    operation: str,
    rejects: bool,
) -> None:
    source = f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};' + operation
    if rejects:
        with pytest.raises(SchedulerClientError):
            extract_bootstrap_id(source)
    else:
        assert extract_bootstrap_id(source) == BOOTSTRAP_COMPAT_ID


@pytest.mark.parametrize(
    "operation",
    [
        pytest.param(
            'const {window:alias}=window;alias.eval("vars.loginid=\'x\'");',
            id="global-window-identity-transfer",
        ),
        pytest.param(
            'const {window:alias}=window\nalias.eval("vars.loginid=\'x\'");',
            id="global-window-identity-transfer-asi-boundary",
        ),
        pytest.param(
            'const {window:{self:alias}}=window;'
            'alias.Function("vars.loginid=\'x\'")();',
            id="nested-global-identity-transfer",
        ),
        pytest.param(
            'const globalAlias=globalThis;'
            'const {["window"]:alias}=globalAlias;'
            'alias.eval("vars.loginid=\'x\'");',
            id="global-identity-through-tracked-alias",
        ),
        pytest.param(
            'let alias;({window:alias}=window);'
            'alias.eval("vars.loginid=\'x\'");',
            id="global-identity-destructuring-assignment",
        ),
        pytest.param(
            'const {eval:run}=window;run("vars.loginid=\'x\'");',
            id="global-eval-renamed-in-declaration",
        ),
        pytest.param(
            'const {execScript:run}=window;run("vars.loginid=\'x\'");',
            id="global-exec-script-destructuring-transfer",
        ),
        pytest.param(
            'const globalAlias=window;const {Function:run}=((globalAlias));'
            'run("vars.loginid=\'x\'")();',
            id="global-function-renamed-from-parenthesized-alias",
        ),
        pytest.param(
            'const {"eval":run}=window;run("vars.loginid=\'x\'");',
            id="quoted-global-eval-key",
        ),
        pytest.param(
            'const {["eval"]:run}=window;run("vars.loginid=\'x\'");',
            id="computed-static-global-eval-key",
        ),
        pytest.param(
            'const {[`eval`]:run}=window;run("vars.loginid=\'x\'");',
            id="computed-template-global-eval-key",
        ),
        pytest.param(
            'const {[selector]:run}=window;run("vars.loginid=\'x\'");',
            id="computed-dynamic-global-key",
        ),
        pytest.param(
            'const {...rest}=window;rest.eval("vars.loginid=\'x\'");',
            id="global-object-rest-transfer",
        ),
        pytest.param(
            'const {nested:{eval:run}}=window;run("vars.loginid=\'x\'");',
            id="nested-global-eval-key",
        ),
        pytest.param(
            '({eval:run}=window);run("vars.loginid=\'x\'");',
            id="global-eval-destructuring-assignment",
        ),
        pytest.param(
            'const {setTimeout:later}=window;later("vars.loginid=\'x\'",0);',
            id="global-timer-destructuring-transfer",
        ),
        pytest.param(
            'const {constructor:{constructor:run}}=window;'
            'run("vars.loginid=\'x\'")();',
            id="global-constructor-chain-destructuring",
        ),
        pytest.param(
            'const {importScripts:load}=window;load("payload.js");',
            id="global-import-scripts-destructuring",
        ),
        pytest.param(
            'const {"\\x65val":run}=window;run("vars.loginid=\'x\'");',
            id="escaped-quoted-global-eval-key",
        ),
        pytest.param(
            'const {["\\x65val"]:run}=window;run("vars.loginid=\'x\'");',
            id="escaped-computed-global-eval-key",
        ),
        pytest.param(
            'const {[`\\x65val`]:run}=window;run("vars.loginid=\'x\'");',
            id="escaped-template-global-eval-key",
        ),
        pytest.param(
            'const {eval:run}=window||{};run("vars.loginid=\'x\'");',
            id="compound-global-source-eval-transfer",
        ),
        pytest.param(
            'const {window:alias}=window||{};'
            'alias.eval("vars.loginid=\'x\'");',
            id="compound-global-source-identity-transfer",
        ),
        pytest.param(
            'const {window:{self:alias}}=window||{};'
            'alias.Function("vars.loginid=\'x\'")();',
            id="compound-global-source-nested-identity-transfer",
        ),
        pytest.param(
            'const {[selector]:run}=window||{};'
            'run("vars.loginid=\'x\'");',
            id="compound-global-source-dynamic-selector",
        ),
        pytest.param(
            'const identityAlias=window.window;'
            'identityAlias.eval("vars.loginid=\'x\'");',
            id="global-window-member-identity-transfer",
        ),
        pytest.param(
            'const identityAlias=window["window"];'
            'identityAlias.eval("vars.loginid=\'x\'");',
            id="computed-global-window-member-identity-transfer",
        ),
        pytest.param(
            'const identityAlias=window[`window`];'
            'identityAlias.Function("vars.loginid=\'x\'")();',
            id="template-global-window-member-identity-transfer",
        ),
        pytest.param(
            'const identityAlias=window?.["window"];'
            'identityAlias.eval("vars.loginid=\'x\'");',
            id="optional-computed-global-window-identity-transfer",
        ),
        pytest.param(
            'let identityAlias;identityAlias=window.window;'
            'identityAlias.eval("vars.loginid=\'x\'");',
            id="global-window-member-identity-reassignment",
        ),
        pytest.param(
            'window.window.eval("vars.loginid=\'x\'");',
            id="direct-global-window-identity-chain-eval",
        ),
        pytest.param(
            'window["window"]["eval"]("vars.loginid=\'x\'");',
            id="computed-global-window-identity-chain-eval",
        ),
        pytest.param(
            'window.window.self.eval("vars.loginid=\'x\'");',
            id="nested-direct-global-identity-chain-eval",
        ),
        pytest.param(
            'const identityAlias=window["\\x77indow"];'
            'identityAlias.eval("vars.loginid=\'x\'");',
            id="escaped-global-identity-selector",
        ),
        pytest.param(
            'const identityAlias=window[selector];'
            'identityAlias.eval("vars.loginid=\'x\'");',
            id="dynamic-global-identity-selector",
        ),
        pytest.param(
            'const identityAlias=window.window||{};'
            'identityAlias.eval("vars.loginid=\'x\'");',
            id="logical-global-identity-alias",
        ),
        pytest.param(
            'const identityAlias=window||{};'
            'identityAlias.eval("vars.loginid=\'x\'");',
            id="logical-bare-global-alias",
        ),
        pytest.param(
            'const identityAlias=flag?window.window:local;'
            'identityAlias.eval("vars.loginid=\'x\'");',
            id="conditional-global-identity-alias",
        ),
        pytest.param(
            'const identityAlias=flag?local:(other?window:local);'
            'identityAlias.eval("vars.loginid=\'x\'");',
            id="nested-conditional-global-identity-alias",
        ),
        pytest.param(
            'const identityAlias=(window||{}).window;'
            'identityAlias.eval("vars.loginid=\'x\'");',
            id="logical-selection-identity-member-transfer",
        ),
        pytest.param(
            'const identityAlias=(window||{})["window"];'
            'identityAlias.Function("vars.loginid=\'x\'")();',
            id="computed-logical-selection-identity-member-transfer",
        ),
        pytest.param(
            'const identityAlias=flag?(window||{}).self:local;'
            'identityAlias.eval("vars.loginid=\'x\'");',
            id="conditional-logical-selection-identity-member-transfer",
        ),
        pytest.param(
            'const {vars:alias}=window;'
            'alias.loginid="BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB";',
            id="global-vars-destructuring-alias-mutation",
        ),
        pytest.param(
            'const {"vars":alias}=window;'
            'alias.loginid="BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB";',
            id="quoted-global-vars-destructuring-alias-mutation",
        ),
        pytest.param(
            'const {["vars"]:alias}=window;'
            'alias.loginid="BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB";',
            id="computed-global-vars-destructuring-alias-mutation",
        ),
        pytest.param(
            'const {window:{vars:alias}}=window;'
            'alias.loginid="BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB";',
            id="nested-global-vars-destructuring-alias-mutation",
        ),
        pytest.param(
            'const {window:{vars:alias}}=window||{};'
            'alias.loginid="BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB";',
            id="compound-nested-global-vars-destructuring-alias-mutation",
        ),
        pytest.param(
            'const {vars:alias}=window||{};'
            'alias.loginid="BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB";',
            id="compound-global-vars-destructuring-alias-mutation",
        ),
    ],
)
def test_bootstrap_extraction_rejects_global_dynamic_selector_destructuring(
    operation: str,
) -> None:
    source = f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};' + operation
    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)


def test_bootstrap_extraction_keeps_safe_global_and_local_destructuring() -> None:
    source = (
        "const {theme}=window;"
        "const {theme:compoundTheme}=window||{};"
        "const {window:alias}=window;alias.initialize();"
        "const custom={window:{eval:()=>1}};"
        "const {eval:customRun}=custom.window||{};customRun('fixture');"
        "function install(window){"
        "const {eval:localRun}=window||{};localRun('fixture');}"
        "let reassigned;({window:reassigned}=window);reassigned.initialize();"
        "const directIdentity=window.window;directIdentity.initialize();"
        "const computedIdentity=window['window'];"
        "computedIdentity.fakeMethod();"
        "const localVarsOwner={other:{vars:{loginid:'fixture'}}};"
        "const {other:{vars:localVars}}=localVarsOwner;"
        "localVars.loginid='changed';"
        "const conditionalResult=window?{value:1}:{value:2};"
        "const methodResult=window.fakeMethod();"
        "const {eval:run}=custom;run('fixture');"
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
    )

    assert extract_bootstrap_id(source) == BOOTSTRAP_COMPAT_ID


def test_bootstrap_extraction_bounds_arrow_expression_depth() -> None:
    expression = "operand" + "**operand" * (
        scheduler_client.MAX_ARROW_EXPRESSION_NESTING + 1
    )
    source = (
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
        f"const install=(window)=>{expression};"
    )

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)


def test_bootstrap_extraction_rejects_global_vars_member_call() -> None:
    source = (
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
        "window.vars();"
    )

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)
@pytest.mark.parametrize(
    "operation",
    [
        pytest.param(
            'window.vars={loginid:"' + BOOTSTRAP_ID + '"};',
            id="global-vars-replacement",
        ),
        pytest.param(
            'window.vars.loginid="' + BOOTSTRAP_ID + '";',
            id="global-loginid-write",
        ),
        pytest.param(
            'const globalAlias=window;globalAlias.vars.loginid="'
            + BOOTSTRAP_ID
            + '";',
            id="aliased-global-loginid-write",
        ),
        pytest.param(
            'const globalAlias=window;globalAlias["vars"]["loginid"]="'
            + BOOTSTRAP_ID
            + '";',
            id="computed-aliased-global-loginid-write",
        ),
        pytest.param(
            'const globalAlias=window;globalAlias.vars["loginid"]="'
            + BOOTSTRAP_ID
            + '";',
            id="mixed-aliased-global-loginid-write",
        ),
        pytest.param(
            "window.vars.loginid++;",
            id="global-loginid-postincrement",
        ),
        pytest.param(
            "const globalAlias=window;++globalAlias.vars.loginid;",
            id="aliased-global-loginid-preincrement",
        ),
        pytest.param(
            '({value:window.vars.loginid}={value:"'
            + BOOTSTRAP_ID
            + '"});',
            id="destructuring-global-loginid-write",
        ),
        pytest.param(
            'for(window.vars.loginid of ["' + BOOTSTRAP_ID + '"]){}',
            id="for-of-global-loginid-write",
        ),
        pytest.param(
            '({value:vars.loginid}={value:"'
            + BOOTSTRAP_ID
            + '"});',
            id="destructuring-vars-loginid-write",
        ),
    ],
)
def test_bootstrap_extraction_rejects_global_vars_member_writes(
    operation: str,
) -> None:
    source = f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};' + operation

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)

def test_bootstrap_extraction_ignores_reflect_set_to_unrelated_vars_property() -> None:
    source = (
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
        'Reflect.set((vars), "theme", "dark");'
    )

    assert extract_bootstrap_id(source) == BOOTSTRAP_COMPAT_ID


def test_bootstrap_extraction_rejects_shadowed_reflect_set() -> None:
    source = (
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
        'const Reflect={set(target,key,value){return target.vars=value;}};'
        'Reflect.set(window, "theme", "dark");'
    )

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)

def test_bootstrap_extraction_rejects_destructured_reflect_shadow() -> None:
    source = (
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
        "const {Reflect}=custom;"
        'Reflect.set(window, "theme", "dark");'
    )

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)




def test_bootstrap_extraction_rejects_custom_reflect_set_member() -> None:
    source = (
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
        'const custom={Reflect:{set(target,key,value){return target[key]=value;}}};'
        'custom.Reflect.set(window, "theme", "dark");'
    )

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)


def test_bootstrap_extraction_rejects_interpolated_template_mutation() -> None:
    source = (
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
        f'`${{vars.loginid="{BOOTSTRAP_ID}"}}`;'
    )

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)


def test_bootstrap_extraction_ignores_escaped_template_interpolation() -> None:
    source = (
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
        r'var inert=`\${vars={loginid:"FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF"}}`;'
    )

    assert extract_bootstrap_id(source) == BOOTSTRAP_COMPAT_ID


def test_bootstrap_extraction_accepts_sanitized_template_interpolation_fixture() -> None:
    source = (
        Path(__file__).parent
        / "fixtures"
        / "scheduler"
        / "bootstrap_template_interpolation.js"
    ).read_text(encoding="utf-8")

    assert extract_bootstrap_id(source) == BOOTSTRAP_COMPAT_ID


def test_bootstrap_extraction_rejects_interpolated_computed_loginid() -> None:
    source = (
        'var vars = {[`login${suffix}`]:"'
        + BOOTSTRAP_COMPAT_ID
        + '"};'
    )

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)


@pytest.mark.parametrize(
    "mutation",
    [
        pytest.param(
            'Reflect.set(window, "vars", {theme:"dark"})',
            id="global-vars-replacement",
        ),
        pytest.param(
            f'Reflect.set(vars, "loginid", "{BOOTSTRAP_ID}")',
            id="loginid-mutation",
        ),
    ],
)
def test_bootstrap_extraction_rejects_reflect_mutation_in_interpolation(
    mutation: str,
) -> None:
    source = (
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
        + "`"
        + "${"
        + mutation
        + "}`;"
    )

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)


def test_bootstrap_extraction_rejects_computed_loginid_assignment_in_interpolation() -> None:
    mutation = f'vars["loginid"]="{BOOTSTRAP_ID}"'
    source = (
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
        + "`"
        + "${"
        + mutation
        + "}`;"
    )

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)


def test_bootstrap_extraction_accepts_unrelated_nested_interpolation_delimiters() -> None:
    source = (
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
        "function notify(value){return value;}"
        "`prefix-${ /* } / */ notify([{value:(1)}])}-suffix`;"
    )

    assert extract_bootstrap_id(source) == BOOTSTRAP_COMPAT_ID


def test_bootstrap_extraction_accepts_nested_template_interpolation() -> None:
    source = (
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
        "`outer-${`inner-${suffix}`}-tail`;"
    )

    assert extract_bootstrap_id(source) == BOOTSTRAP_COMPAT_ID


def test_bootstrap_extraction_accepts_escaped_backtick_in_template_text() -> None:
    source = (
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
        + r"const inert = `before\`after`;"
    )

    assert extract_bootstrap_id(source) == BOOTSTRAP_COMPAT_ID


def test_bootstrap_extraction_rejects_unproven_interpolated_global_key() -> None:
    source = (
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
        'Reflect.set(window, `theme-${suffix}`, "dark");'
    )

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)


@pytest.mark.parametrize(
    "template",
    [
        pytest.param("`${}`;", id="empty-expression"),
        pytest.param("`${(value]}`;", id="mismatched-delimiters"),
        pytest.param("`${value`;", id="unterminated-expression"),
        pytest.param(r"`${\}`;", id="bare-backslash-escape"),
    ],
)
def test_bootstrap_extraction_rejects_malformed_template_interpolation(
    template: str,
) -> None:
    source = f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};' + template

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)


def test_bootstrap_extraction_fails_closed_on_regex_like_interpolation() -> None:
    source = (
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
        + '`prefix-${/}/; vars.loginid="'
        + BOOTSTRAP_ID
        + '"}`;'
    )

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)


def test_bootstrap_extraction_bounds_nested_template_depth() -> None:
    prefix = f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
    nested = "suffix"
    for _ in range(64):
        nested = "`outer-${" + nested + "}`"

    assert extract_bootstrap_id(prefix + nested + ";") == BOOTSTRAP_COMPAT_ID

    nested = "suffix"
    for _ in range(65):
        nested = "`outer-${" + nested + "}`"
    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(prefix + nested + ";")


def test_bootstrap_extraction_rejects_concatenated_loginid_literal() -> None:
    source = f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"+"B"}};'

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)


def test_bootstrap_extraction_rejects_escaped_loginid_value() -> None:
    source = f'var vars={{loginid:"\\u0041{BOOTSTRAP_COMPAT_ID[1:]}"}};'

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)


def test_bootstrap_extraction_rejects_escaped_vars_object_property_key() -> None:
    source = (
        f'var vars={{"log\\u0069nid":"{BOOTSTRAP_ID}",'
        f'loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
    )

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)

@pytest.mark.parametrize(
    "method",
    [
        "loginid(){return 'FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF';}",
        "async loginid(){return 'FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF';}",
        "*loginid(){return 'FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF';}",
        "async *loginid(){return 'FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF';}",
        "get loginid(){return 'FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF';}",
        "set loginid(value){}",
        "get \"loginid\"(){return 'FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF';}",
        "async [\"loginid\"](){return 'FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF';}",
        "get [\"loginid\"](){return 'FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF';}",
        "async [key](){return 'FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF';}",
        "async [\"log\"+\"inid\"](){return 'FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF';}",
    ],
    ids=[
        "plain-method",
        "async-method",
        "generator-method",
        "async-generator-method",
        "getter",
        "setter",
        "quoted-getter",
        "async-computed-method",
        "computed-getter",
        "async-dynamic-computed-method",
        "async-expression-computed-method",
    ],
)
def test_bootstrap_extraction_rejects_loginid_method_override(
    method: str,
) -> None:
    source = (
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}",'
        f'{method}}};'
    )

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)


def test_bootstrap_extraction_accepts_provably_unrelated_method_forms() -> None:
    source = (
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}",'
        'get "theme"(){return "fixture";},'
        'async ["theme"](){return "fixture";}};'
    )

    assert extract_bootstrap_id(source) == BOOTSTRAP_COMPAT_ID



def test_bootstrap_extraction_rejects_escaped_computed_global_vars_name() -> None:
    source = (
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
        'window["va\\u0072s"]={theme:"dark"};'
    )

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)


@pytest.mark.parametrize(
    "identifier",
    [
        r"v\u0061rs",
        r"\u0076ars",
        r"\u{76}ars",
    ],
    ids=["escaped-vars-continuation", "escaped-vars-start", "braced-unicode-start"],
)
def test_bootstrap_extraction_rejects_unicode_escaped_vars_reference(
    identifier: str,
) -> None:
    source = (
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
        f'{identifier}.loginid="{BOOTSTRAP_ID}";'
    )

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)


def test_bootstrap_extraction_rejects_unicode_escaped_loginid_property() -> None:
    source = (
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}",'
        f'log\\u0069nid:"{BOOTSTRAP_ID}"}};'
    )

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)


@pytest.mark.parametrize(
    "member",
    ["`vars`", "`va\\u0072s`"],
    ids=["plain-static-vars", "escaped-static-vars"],
)
def test_bootstrap_extraction_rejects_static_template_global_vars_member(
    member: str,
) -> None:
    source = (
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
        f'window[{member}]={{theme:"dark"}};'
    )

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)


def test_bootstrap_extraction_accepts_unrelated_static_template_member() -> None:
    source = (
        f'var unrelated={{[`vars`]:"fixture"}};'
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
    )

    assert extract_bootstrap_id(source) == BOOTSTRAP_COMPAT_ID


def test_bootstrap_extraction_ignores_unicode_escapes_in_comments_and_strings() -> None:
    source = (
        r'// \u0076ars.loginid="FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF";'
        "\n"
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}",note:"\\u0069"}};'
    )

    assert extract_bootstrap_id(source) == BOOTSTRAP_COMPAT_ID

@pytest.mark.parametrize("line_ending", ["\r", "\n", "\r\n", "\u2028", "\u2029"])
def test_bootstrap_extraction_does_not_hide_mutation_after_line_comment(
    line_ending: str,
) -> None:
    source = (
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
        f'// ignored{line_ending}vars.loginid="{BOOTSTRAP_ID}";'
    )

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)


def test_bootstrap_extraction_rejects_two_valid_vars_objects() -> None:
    source = (
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
        f'var vars={{loginid:"{BOOTSTRAP_ID}"}};'
    )

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)


def test_bootstrap_extraction_rejects_zero_valid_vars_objects() -> None:
    source = (
        'var vars={theme:"dark"};'
        'var vars={nested:{loginid:"FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF"}};'
    )

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)


def test_bootstrap_extraction_rejects_malformed_candidate_with_valid_candidate() -> None:
    source = (
        'var vars={loginid:42};'
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
    )

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)


def test_bootstrap_extraction_rejects_non_string_loginid() -> None:
    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id("var vars={loginid:42};")


def test_bootstrap_extraction_rejects_invalid_loginid_format() -> None:
    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id('var vars={loginid:"not-a-bootstrap-id"};')


def test_bootstrap_extraction_rejects_malformed_direct_loginid_property() -> None:
    source = (
        "var vars={loginid};"
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
    )

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)


def test_bootstrap_extraction_rejects_quoted_loginid_conflict() -> None:
    source = (
        f'var vars={{"loginid":"{BOOTSTRAP_ID}"}};'
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'
    )

    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)


@pytest.mark.parametrize(
    "source",
    [
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"',
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}}; (',
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}}; [)',
        f'var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}}; ([)]',
        "var vars={loginid:'unterminated};",
        "var vars={loginid: `unterminated};",
        "var vars={loginid:'FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF'}; /* unfinished",
    ],
    ids=[
        "unclosed-object",
        "unclosed-paren",
        "mismatched-bracket",
        "misordered-delimiters",
        "unterminated-string",
        "unterminated-template",
        "unterminated-comment",
    ],
)
def test_bootstrap_extraction_rejects_malformed_javascript(source: str) -> None:
    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)


def test_bootstrap_extraction_handles_many_unrelated_vars_objects() -> None:
    unrelated = " ".join(
        f"var vars={{decoy:{index}}};" for index in range(1_000)
    )
    source = f'{unrelated} var vars={{loginid:"{BOOTSTRAP_COMPAT_ID}"}};'

    assert extract_bootstrap_id(source) == BOOTSTRAP_COMPAT_ID


def test_bootstrap_extraction_rejects_many_unclosed_objects() -> None:
    source = " ".join("var vars={" for _ in range(2_000))
    with pytest.raises(SchedulerClientError):
        extract_bootstrap_id(source)


@pytest.mark.asyncio
async def test_http_scheduler_client_uses_pinned_id_and_reads_list_and_detail():
    session = _FakeSession(
        [
            _FakeResponse(
                200,
                json.dumps({"status": "ok", "sessid": SESSION_ID, "alevel": ALEVEL}),
            ),
            _FakeResponse(200, json.dumps(load_json("scheduler/list_response.json"))),
            _FakeResponse(200, json.dumps(load_json("scheduler/detail_response.json"))),
        ]
    )
    ssl_context = ssl.create_default_context()
    client = AiohttpSchedulerReadClient(
        session,
        SchedulerClientConfig(
            base_url="https://gateway.example",
            bootstrap_id=BOOTSTRAP_ID,
            username="operator",
            password="password",
        ),
        ssl_context=ssl_context,
    )

    entries = await client.list_entries(1789250400, 1789855199)
    detail = await client.get_detail(4127)

    assert [entry.sch_index for entry in entries] == [4100, 4101]
    assert detail.sch_index == 4127
    assert len(session.calls) == 3
    login_prefix, login_payload = _request_payload(session.calls[0])
    list_prefix, list_payload = _request_payload(session.calls[1])
    detail_prefix, detail_payload = _request_payload(session.calls[2])
    assert login_prefix == BOOTSTRAP_ID
    assert list_prefix == SESSION_ID
    assert detail_prefix == SESSION_ID
    assert login_payload == {
        "cmd": "adm",
        "func": "login",
        "name": "operator",
        "password": "password",
    }
    assert list_payload["cmd"] == "message"
    assert list_payload["Omnibus"]["msg"] == [
        1,
        0,
        5,
        3,
        255,
        2,
        101,
        52,
        0,
        33,
        0,
        0,
    ]
    assert list_payload["Omnibus"]["data"] == {
        "from": 1789257600,
        "to": 1789862399,
    }
    assert detail_payload["cmd"] == "message"
    assert detail_payload["Omnibus"]["msg"] == [
        1,
        0,
        5,
        3,
        255,
        2,
        101,
        200,
        4127,
        33,
        1,
        0,
    ]
    assert detail_payload["Omnibus"]["data"] == 4127
    assert session.calls[0][1]["cookies"] is None
    assert session.calls[1][1]["cookies"] == {
        "Sessid": SESSION_ID,
        "ALevel": str(ALEVEL),
    }
    assert session.calls[2][1]["cookies"] == {
        "Sessid": SESSION_ID,
        "ALevel": str(ALEVEL),
    }
    assert session.calls[0][1]["headers"] is None
    assert session.calls[1][1]["headers"] == {"X-Requested-With": "XMLHttpRequest"}
    assert session.calls[2][1]["headers"] == {"X-Requested-With": "XMLHttpRequest"}
    assert all("/js/user.js" not in call[0] for call in session.calls)
    assert all("Referer" not in (call[1].get("headers") or {}) for call in session.calls)
    assert all(call[1]["ssl"] is ssl_context for call in session.calls)
    assert all(call[1]["allow_redirects"] is False for call in session.calls)
    assert client._alevel == str(ALEVEL)
    diagnostics = client.diagnostics()
    assert "alevel" not in diagnostics
    assert diagnostics.get("alevel") is None
    assert SESSION_ID not in str(diagnostics)
    assert client.diagnostics()["bootstrap_id_in_memory"] is True
    assert client.diagnostics()["sessid_in_memory"] is True


@pytest.mark.asyncio
async def test_http_scheduler_client_detail_fails_closed_without_alevel():
    session = _FakeSession([])
    client = AiohttpSchedulerReadClient(
        session,
        SchedulerClientConfig(
            base_url="https://gateway.example",
            bootstrap_id=BOOTSTRAP_ID,
            username="operator",
            password="password",
        ),
    )
    client._sessid = SESSION_ID

    with pytest.raises(SchedulerProtocolError, match="authentication state is incomplete"):
        await client.get_detail(4127)

    assert session.calls == []

@pytest.mark.asyncio
async def test_http_scheduler_client_rejects_redirect_without_retry():
    session = _FakeSession([_FakeResponse(302, "")])
    client = AiohttpSchedulerReadClient(
        session,
        SchedulerClientConfig(
            base_url="https://gateway.example",
            bootstrap_id=BOOTSTRAP_ID,
            username="operator",
            password="password",
        ),
    )

    with pytest.raises(SchedulerClientError):
        await client.list_entries(1, 2)
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_http_scheduler_client_clears_session_on_auth_loss_without_retry():
    list_response = json.dumps(load_json("scheduler/list_response.json"))
    session = _FakeSession(
        [
            _FakeResponse(
                200,
                json.dumps({"status": "ok", "sessid": SESSION_ID, "alevel": ALEVEL}),
            ),
            _FakeResponse(200, list_response),
            _FakeResponse(200, "NotLoggedOn"),
        ]
    )
    client = AiohttpSchedulerReadClient(
        session,
        SchedulerClientConfig(
            base_url="https://gateway.example",
            bootstrap_id=BOOTSTRAP_ID,
            username="operator",
            password="password",
        ),
    )

    await client.list_entries(1, 2)
    with pytest.raises(SchedulerAuthenticationError) as error:
        await client.list_entries(1, 2)

    assert error.value.reason is SchedulerAuthenticationReason.SESSION_NOT_AUTHORIZED
    assert error.value.stage is SchedulerAuthenticationStage.LIST
    assert str(error.value) == "scheduler authentication failed"

    assert len(session.calls) == 3
    assert all("/js/user.js" not in call[0] for call in session.calls)
    assert sum("/ajax" in call[0] for call in session.calls) == 3
    assert client.diagnostics()["sessid_in_memory"] is False
    assert client._alevel is None


@pytest.mark.asyncio
async def test_http_scheduler_client_classifies_detail_auth_loss():
    session = _FakeSession(
        [
            _FakeResponse(
                200,
                json.dumps({"status": "ok", "sessid": SESSION_ID, "alevel": ALEVEL}),
            ),
            _FakeResponse(200, "notAuthorised"),
        ]
    )
    client = AiohttpSchedulerReadClient(
        session,
        SchedulerClientConfig(
            base_url="https://gateway.example",
            bootstrap_id=BOOTSTRAP_ID,
            username="operator",
            password="password",
        ),
    )

    with pytest.raises(SchedulerAuthenticationError) as error:
        await client.get_detail(4127)

    assert error.value.reason is SchedulerAuthenticationReason.SESSION_NOT_AUTHORIZED
    assert error.value.stage is SchedulerAuthenticationStage.DETAIL
    assert client.diagnostics()["sessid_in_memory"] is False
    assert client._alevel is None
    assert len(session.calls) == 2


def test_scheduler_authentication_error_requires_typed_fields():
    with pytest.raises(TypeError):
        SchedulerAuthenticationError(
            cast(Any, "session_not_authorized"),
            SchedulerAuthenticationStage.LOGIN,
        )
    with pytest.raises(TypeError):
        SchedulerAuthenticationError(
            SchedulerAuthenticationReason.SESSION_NOT_AUTHORIZED,
            cast(Any, "login"),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("login_body", "expected_reason"),
    [
        (
            "NotLoggedOn",
            SchedulerAuthenticationReason.SESSION_NOT_AUTHORIZED,
        ),
        (
            json.dumps({"status": "denied", "response": "credential=secret"}),
            SchedulerAuthenticationReason.LOGIN_REJECTED,
        ),
        (
            json.dumps({"status": "ok"}),
            SchedulerAuthenticationReason.INVALID_SESSION_RESPONSE,
        ),
        (
            json.dumps({"status": "ok", "sessid": "S" * 129}),
            SchedulerAuthenticationReason.INVALID_SESSION_RESPONSE,
        ),
        (
            json.dumps({"status": "ok", "sessid": "invalid session"}),
            SchedulerAuthenticationReason.INVALID_SESSION_RESPONSE,
        ),
    ],
)
async def test_http_scheduler_client_classifies_authentication_branches(
    login_body: str,
    expected_reason: SchedulerAuthenticationReason,
):
    session = _FakeSession([_FakeResponse(200, login_body)])
    client = AiohttpSchedulerReadClient(
        session,
        SchedulerClientConfig(
            base_url="https://gateway.example",
            bootstrap_id=BOOTSTRAP_ID,
            username="operator",
            password="password",
        ),
    )

    with pytest.raises(SchedulerAuthenticationError) as error:
        await client.list_entries(1, 2)

    assert error.value.reason is expected_reason
    assert error.value.stage is SchedulerAuthenticationStage.LOGIN
    assert str(error.value) == "scheduler authentication failed"
    assert "credential=secret" not in str(error.value)
    assert len(session.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "login_body",
    [
        json.dumps({"status": "ok", "sessid": SESSION_ID}),
        json.dumps({"status": "ok", "sessid": SESSION_ID, "alevel": "secret-alevel"}),
        json.dumps({"status": "ok", "sessid": SESSION_ID, "alevel": True}),
        json.dumps({"status": "ok", "sessid": SESSION_ID, "alevel": 10**40}),
    ],
)
async def test_http_scheduler_client_rejects_missing_or_invalid_alevel(login_body: str):
    session = _FakeSession([_FakeResponse(200, login_body)])
    client = AiohttpSchedulerReadClient(
        session,
        SchedulerClientConfig(
            base_url="https://gateway.example",
            bootstrap_id=BOOTSTRAP_ID,
            username="operator",
            password="password",
        ),
    )

    with pytest.raises(SchedulerProtocolError) as error:
        await client.list_entries(1, 2)

    assert str(error.value) == "scheduler login response alevel was invalid"
    assert "secret-alevel" not in str(error.value)
    assert client._sessid is None
    assert client._alevel is None
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_scheduler_http_response_body_is_bounded() -> None:
    session = _FakeSession(
        [_FakeResponse(200, "x" * (MAX_SCHEDULER_RESPONSE_BYTES + 1))]
    )
    client = AiohttpSchedulerReadClient(
        session,
        SchedulerClientConfig(
            base_url="https://gateway.example",
            bootstrap_id=BOOTSTRAP_ID,
            username="operator",
            password="password",
        ),
    )

    with pytest.raises(SchedulerProtocolError, match="too large"):
        await client._request_text("/")



@pytest.mark.asyncio
async def test_scheduler_http_rejects_oversized_declared_content_length() -> None:
    response = _FakeResponse(
        200,
        "not-read",
        headers={"Content-Length": str(MAX_SCHEDULER_RESPONSE_BYTES + 1)},
    )
    client = AiohttpSchedulerReadClient(
        _FakeSession([response]),
        SchedulerClientConfig(
            base_url="https://gateway.example", bootstrap_id=BOOTSTRAP_ID
        ),
    )

    with pytest.raises(SchedulerProtocolError, match="too large"):
        await client._request_text("/")


@pytest.mark.asyncio
async def test_scheduler_http_rejects_malformed_content_length() -> None:
    response = _FakeResponse(200, "not-read", headers={"Content-Length": "1.5"})
    client = AiohttpSchedulerReadClient(
        _FakeSession([response]),
        SchedulerClientConfig(
            base_url="https://gateway.example", bootstrap_id=BOOTSTRAP_ID
        ),
    )

    with pytest.raises(SchedulerProtocolError, match="Content-Length"):
        await client._request_text("/")


@pytest.mark.asyncio
async def test_scheduler_http_accepts_exact_streaming_boundary() -> None:
    body = b"x" * MAX_SCHEDULER_RESPONSE_BYTES
    response = _StreamingResponse(200, [body[:123], body[123:]])
    client = AiohttpSchedulerReadClient(
        _FakeSession([response]),
        SchedulerClientConfig(
            base_url="https://gateway.example", bootstrap_id=BOOTSTRAP_ID
        ),
    )

    assert await client._request_text("/") == body.decode()


@pytest.mark.asyncio
async def test_scheduler_http_rejects_oversized_chunked_body_before_decode() -> None:
    response = _StreamingResponse(
        200,
        [b"{\"entries\":[", b"x" * MAX_SCHEDULER_RESPONSE_BYTES, b"]}"],
    )
    client = AiohttpSchedulerReadClient(
        _FakeSession([response]),
        SchedulerClientConfig(
            base_url="https://gateway.example", bootstrap_id=BOOTSTRAP_ID
        ),
    )

    with pytest.raises(SchedulerProtocolError, match="too large"):
        await client._request_text("/")


@pytest.mark.asyncio
async def test_scheduler_http_rejects_oversized_valid_list_payload() -> None:
    response = _StreamingResponse(
        200,
        [b'{"status":"ok","entries":["', b"x" * MAX_SCHEDULER_RESPONSE_BYTES, b'"]}'],
    )
    client = AiohttpSchedulerReadClient(
        _FakeSession([response]),
        SchedulerClientConfig(
            base_url="https://gateway.example", bootstrap_id=BOOTSTRAP_ID
        ),
    )

    with pytest.raises(SchedulerProtocolError, match="too large"):
        await client._request_text("/")

@pytest.mark.asyncio
async def test_scheduler_list_cardinality_invalidates_without_truncation() -> None:
    entry = parse_list_response(load_json("scheduler/list_response.json"))[0]

    class CardinalityClient:
        calls = 0

        async def list_entries(self, from_epoch: int, to_epoch: int):
            del from_epoch, to_epoch
            self.calls += 1
            count = 1 if self.calls == 1 else SCHEDULER_LIST_ENTRY_LIMIT + 1
            return [entry for _ in range(count)]

        async def get_detail(self, sch_index: int):
            raise AssertionError(sch_index)

    manager = SchedulerRefreshManager(CardinalityClient())
    await manager.refresh_list(1, 2)

    with pytest.raises(ValueError, match="entry limit"):
        await manager.refresh_list(1, 2)

    assert len(manager.entries) == 1
    assert manager.list_visibility_diagnostics()["invalidated"] is True
    assert manager.diagnostics()["last_error"] == "response_too_large"
def test_scheduler_config_requires_explicit_valid_bootstrap_id() -> None:
    base = {
        "scheduler_url": "https://gateway.example",
        "scheduler_username": "operator",
        "scheduler_password": "password",
    }

    with pytest.raises(SchedulerConfigurationError, match="bootstrap_id_required"):
        SchedulerClientConfig.from_mapping(base)
    with pytest.raises(SchedulerConfigurationError, match="bootstrap_id_required"):
        SchedulerClientConfig.from_mapping(
            {**base, "scheduler_bootstrap_id": "javascript:alert(1)"}
        )

    config = SchedulerClientConfig.from_mapping(
        {**base, "scheduler_bootstrap_id": f"  {BOOTSTRAP_ID}  "}
    )
    assert config is not None
    assert config.bootstrap_id == BOOTSTRAP_ID


@pytest.mark.parametrize(
    "source",
    (
        f'var vars={{loginid:"{BOOTSTRAP_ID}"}};window.open("javascript:alert(1)");',
        f'var vars={{loginid:"{BOOTSTRAP_ID}"}};setImmediate("alert(1)");',
        f'var vars={{loginid:"{BOOTSTRAP_ID}"}};document.write("<script>x</script>");',
    ),
)
def test_public_bootstrap_extraction_is_disabled_for_untrusted_javascript(
    source: str,
) -> None:
    with pytest.raises(
        SchedulerConfigurationError,
        match="automatic_bootstrap_discovery_disabled",
    ):
        scheduler_client.extract_bootstrap_id(source)


def test_scheduler_http_client_fails_closed_without_bootstrap_id() -> None:
    session = _FakeSession([])

    with pytest.raises(SchedulerConfigurationError, match="bootstrap_id_required"):
        AiohttpSchedulerReadClient(
            session,
            SchedulerClientConfig(base_url="https://gateway.example"),
        )

    assert session.calls == []
