"""Authenticated, read-only Pixii scheduler HTTP client."""

from __future__ import annotations

import json
import re
import ssl
from collections.abc import Iterable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from typing import Any, Protocol, cast
from urllib.parse import quote, urlsplit, urlunsplit

import aiohttp

from ..const import (
    CONF_SCHEDULER_BOOTSTRAP_ID,
    CONF_SCHEDULER_CA_COMPATIBILITY,
    CONF_SCHEDULER_CA_FILENAME,
    CONF_SCHEDULER_ENABLED,
)
from ..exceptions import (
    SchedulerAuthenticationError,
    SchedulerAuthenticationReason,
    SchedulerAuthenticationStage,
    SchedulerClientError,
    SchedulerConfigurationError,
    SchedulerProtocolError,
)
from ..protocol.models import SchedulerDetail, SchedulerEntry
from ..protocol.scheduler import (
    build_detail_request,
    build_list_request,
    parse_detail_response,
    parse_list_response,
    scheduler_epoch_from_gateway_wire,
    scheduler_epoch_to_gateway_wire,
)
from .tls import normalize_scheduler_ca_filename, scheduler_tls_diagnostics

DEFAULT_SCHEDULER_TIMEOUT = 10.0
BOOTSTRAP_ID_RE = re.compile(r"^[A-Za-z0-9]{32}$")
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9]{1,128}$")
MIN_AUTH_LEVEL = -2_147_483_648
MAX_AUTH_LEVEL = 2_147_483_647
_JSON_SAFE = "-_.!~*'()"
AUTH_LOSS_MARKERS = ("notAuthorised", "NotLoggedOn")
MAX_SCHEDULER_RESPONSE_BYTES = 1024 * 1024
MAX_TEMPLATE_NESTING = 64
MAX_DESTRUCTURING_PATTERN_NESTING = 64
MAX_BINDING_SCOPE_NESTING = 64
MAX_ARROW_EXPRESSION_NESTING = 64
_BOOTSTRAP_CODE_EXECUTION_SELECTORS = frozenset(
    {"eval", "Function", "execScript", "importScripts"}
)
_BOOTSTRAP_TIMER_SELECTORS = frozenset({"setTimeout", "setInterval"})
_BOOTSTRAP_GLOBAL_IDENTITY_SELECTORS = frozenset(
    {"window", "globalThis", "self", "frames", "top", "parent"}
)
_BOOTSTRAP_UNSAFE_GLOBAL_SELECTORS = (
    _BOOTSTRAP_CODE_EXECUTION_SELECTORS
    | _BOOTSTRAP_TIMER_SELECTORS
    | {"constructor"}
)
_BOOTSTRAP_ARROW_BINARY_OPERATORS = (
    ((">", ">", ">", "="), 2, True),
    (("<", "<", "="), 2, True),
    ((">", ">", "="), 2, True),
    (("&", "&", "="), 2, True),
    (("|", "|", "="), 2, True),
    (("?", "?", "="), 2, True),
    (("+", "="), 2, True),
    (("-", "="), 2, True),
    (("*", "*", "="), 2, True),
    (("*", "="), 2, True),
    (("/", "="), 2, True),
    (("%", "="), 2, True),
    (("&", "="), 2, True),
    (("|", "="), 2, True),
    (("^", "="), 2, True),
    (("=",), 2, True),
    (("|", "|"), 4, False),
    (("?", "?"), 4, False),
    (("&", "&"), 5, False),
    (("|",), 6, False),
    (("^",), 7, False),
    (("&",), 8, False),
    (("=", "=", "="), 9, False),
    (("!", "=", "="), 9, False),
    (("=", "="), 9, False),
    (("!", "="), 9, False),
    (("<", "<", "<"), 11, False),
    ((">", ">", ">"), 11, False),
    (("<", "<"), 11, False),
    ((">", ">"), 11, False),
    (("<", "="), 10, False),
    ((">", "="), 10, False),
    (("<",), 10, False),
    ((">",), 10, False),
    (("in",), 10, False),
    (("instanceof",), 10, False),
    (("+",), 12, False),
    (("-",), 12, False),
    (("*", "*"), 14, True),
    (("*",), 13, False),
    (("/",), 13, False),
    (("%",), 13, False),
)
_INTERPOLATED_TEMPLATE_TOKEN = ("template_interpolation", "", True)
_TEMPLATE_BOUNDARY_TOKEN = ("template_boundary", "", True)
_INTERPOLATED_TEMPLATE_END_TOKEN = ("template_interpolation_end", "", True)


class _JavaScriptTokenStream(list[Any]):
    """Tokens with source line-terminator boundaries preserved."""

    def __init__(self) -> None:
        super().__init__()
        self.line_terminators_before: set[int] = set()


class SchedulerHttpResponse(Protocol):
    """Small response surface used by the client and offline fakes."""

    status: int
    headers: Mapping[str, str]

    async def text(self) -> str: ...


class SchedulerHttpSession(Protocol):
    """Small HTTP session surface that keeps the client independently testable."""

    def get(
        self,
        url: str,
        *,
        allow_redirects: bool,
        cookies: Mapping[str, str] | None,
        headers: Mapping[str, str] | None,
        ssl: ssl.SSLContext,
        timeout: aiohttp.ClientTimeout,
    ) -> AbstractAsyncContextManager[SchedulerHttpResponse]: ...


def _declared_response_length(response: SchedulerHttpResponse) -> int | None:
    """Return a validated Content-Length, if the server declared one."""
    headers: Any = getattr(response, "headers", None)
    if headers is None:
        return None
    raw_length = headers.get("Content-Length")
    if raw_length is None:
        raw_length = headers.get("content-length")
    if raw_length is None:
        return None
    if not isinstance(raw_length, str) or not raw_length.isdecimal():
        raise SchedulerProtocolError("scheduler HTTP Content-Length was invalid")
    length = int(raw_length)
    if length > MAX_SCHEDULER_RESPONSE_BYTES:
        raise SchedulerProtocolError("scheduler HTTP response was too large")
    return length


async def _bounded_response_text(response: SchedulerHttpResponse) -> str:
    """Read a scheduler response without allowing an unbounded body allocation."""
    _declared_response_length(response)
    content: Any = getattr(response, "content", None)
    iter_chunked = getattr(content, "iter_chunked", None)
    if callable(iter_chunked):
        chunks: list[bytes] = []
        size = 0
        iterator: Any = iter_chunked(64 * 1024)
        async for chunk in iterator:
            if not isinstance(chunk, bytes):
                raise SchedulerProtocolError("scheduler HTTP response body was invalid")
            size += len(chunk)
            if size > MAX_SCHEDULER_RESPONSE_BYTES:
                raise SchedulerProtocolError("scheduler HTTP response was too large")
            chunks.append(chunk)
        encoding = getattr(response, "charset", None) or "utf-8"
        try:
            return b"".join(chunks).decode(encoding)
        except (LookupError, UnicodeDecodeError) as err:
            raise SchedulerProtocolError(
                "scheduler HTTP response encoding was invalid"
            ) from err
    read = getattr(content, "read", None)
    if callable(read):
        body = await cast(Any, read)(MAX_SCHEDULER_RESPONSE_BYTES + 1)
        if not isinstance(body, bytes):
            raise SchedulerProtocolError("scheduler HTTP response body was invalid")
        if len(body) > MAX_SCHEDULER_RESPONSE_BYTES:
            raise SchedulerProtocolError("scheduler HTTP response was too large")
        encoding = getattr(response, "charset", None) or "utf-8"
        try:
            return body.decode(encoding)
        except (LookupError, UnicodeDecodeError) as err:
            raise SchedulerProtocolError(
                "scheduler HTTP response encoding was invalid"
            ) from err
    raise SchedulerProtocolError("scheduler HTTP response streaming unavailable")


@dataclass(frozen=True, slots=True)
class SchedulerClientConfig:
    """Optional per-entry scheduler HTTP configuration."""

    base_url: str
    username: str | None = None
    password: str | None = None
    bootstrap_id: str | None = None
    timeout: float = DEFAULT_SCHEDULER_TIMEOUT
    pixii_ca_compatibility: bool = False
    ca_filename: str | None = None

    def __post_init__(self) -> None:
        if normalize_scheduler_url(self.base_url) != self.base_url:
            raise SchedulerClientError("scheduler URL must be an HTTPS origin")
        if not 1.0 <= self.timeout <= 60.0:
            raise SchedulerClientError("scheduler timeout is outside the allowed range")
        if (
            self.bootstrap_id is not None
            and normalize_scheduler_bootstrap_id(self.bootstrap_id) != self.bootstrap_id
        ):
            raise SchedulerConfigurationError("bootstrap_id_invalid")
        if (
            self.ca_filename is not None
            and normalize_scheduler_ca_filename(self.ca_filename) != self.ca_filename
        ):
            raise SchedulerConfigurationError("ca_filename_invalid")
        if self.pixii_ca_compatibility and self.ca_filename is None:
            raise SchedulerConfigurationError("ca_filename_required")

    @classmethod
    def from_mapping(cls, values: Mapping[str, object]) -> SchedulerClientConfig | None:
        """Build configuration only when a valid HTTPS origin is configured."""
        if values.get(CONF_SCHEDULER_ENABLED) is False:
            return None
        compatibility = values.get(CONF_SCHEDULER_CA_COMPATIBILITY, False)
        if not isinstance(compatibility, bool):
            raise SchedulerConfigurationError("ca_compatibility_invalid")
        raw_filename = values.get(CONF_SCHEDULER_CA_FILENAME)
        if raw_filename in (None, ""):
            ca_filename = None
        else:
            ca_filename = normalize_scheduler_ca_filename(raw_filename)
            if ca_filename is None:
                raise SchedulerConfigurationError("ca_filename_invalid")
        if compatibility and ca_filename is None:
            raise SchedulerConfigurationError("ca_filename_required")

        raw_url = values.get("scheduler_url")
        if raw_url is None or raw_url == "":
            return None
        base_url = normalize_scheduler_url(raw_url)
        if base_url is None:
            raise SchedulerClientError("scheduler URL must be an HTTPS origin")
        bootstrap_id = normalize_scheduler_bootstrap_id(
            values.get(CONF_SCHEDULER_BOOTSTRAP_ID)
        )
        if bootstrap_id is None:
            raise SchedulerConfigurationError("bootstrap_id_required")
        timeout_value = values.get("scheduler_timeout", DEFAULT_SCHEDULER_TIMEOUT)
        if isinstance(timeout_value, bool) or not isinstance(timeout_value, int | float | str):
            raise SchedulerClientError("scheduler timeout is invalid")
        try:
            timeout = float(timeout_value)
        except ValueError as err:
            raise SchedulerClientError("scheduler timeout is invalid") from err
        if not 1.0 <= timeout <= 60.0:
            raise SchedulerClientError("scheduler timeout is outside the allowed range")
        username = values.get("scheduler_username")
        password = values.get("scheduler_password")
        return cls(
            base_url=base_url,
            username=username.strip() if isinstance(username, str) and username.strip() else None,
            password=password if isinstance(password, str) and password else None,
            bootstrap_id=bootstrap_id,
            timeout=timeout,
            pixii_ca_compatibility=compatibility,
            ca_filename=ca_filename,
        )


def normalize_scheduler_url(value: object) -> str | None:
    """Return a bare HTTPS origin, rejecting credentials and URL suffixes."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    parsed = urlsplit(candidate)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        return None
    return urlunsplit(("https", parsed.netloc, "", "", ""))


def normalize_scheduler_bootstrap_id(value: object) -> str | None:
    """Return one explicitly configured Pixii bootstrap identifier."""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate if BOOTSTRAP_ID_RE.fullmatch(candidate) else None


def _simple_alias_assignment_source(
    tokens: list[Any],
    target_index: int,
) -> str | None:
    if (
        target_index < 0
        or target_index >= len(tokens)
        or not isinstance(tokens[target_index], str)
        or not tokens[target_index].isidentifier()
        or (
            target_index > 0
            and tokens[target_index - 1] in {"var", "let", "const", ".", "?"}
        )
        or target_index + 2 >= len(tokens)
        or tokens[target_index + 1] != "="
        or tokens[target_index + 2] in {"=", ">"}
    ):
        return None

    value_index = target_index + 2
    value_end = value_index + 1
    if (
        tokens[value_index] == "("
        and value_index + 2 < len(tokens)
        and tokens[value_index + 2] == ")"
    ):
        value_index += 1
        value_end = value_index + 2
    value = tokens[value_index] if value_index < len(tokens) else None
    if (
        not isinstance(value, str)
        or not value.isidentifier()
        or (
            value_end < len(tokens)
            and tokens[value_end] not in {",", ";", "}"}
        )
    ):
        return None
    return value


def _reject_unsupported_bootstrap_calls(
    tokens: list[Any],
    delimiter_matches: dict[int, int],
    closing_matches: dict[int, int],
    global_aliases: set[str],
    local_object_names: set[str],
) -> tuple[set[int], set[int]]:
    """Reject call forms that can obscure bootstrap binding mutations."""

    def _unwrap_parentheses(start: int, end: int) -> tuple[int, int]:
        while (
            start < end
            and tokens[start] == "("
            and delimiter_matches.get(start) == end - 1
        ):
            start += 1
            end -= 1
        return start, end

    def _simple_target(start: int, end: int) -> str:
        start, end = _unwrap_parentheses(start, end)
        if end - start != 1:
            return "unknown"
        token = tokens[start]
        if not isinstance(token, str) or not token.isidentifier():
            return "unknown"
        if token in global_aliases:
            return "global"
        if token == "vars":
            return "vars"
        if token in local_object_names:
            return "local"
        return "unknown"

    def _static_key(start: int, end: int) -> str | None:
        if end - start != 1:
            return None
        token = tokens[start]
        if (
            isinstance(token, tuple)
            and token[0] in {"string", "template"}
            and not token[2]
        ):
            return token[1]
        return None

    def _global_identity_pattern_targets(
        opening: int,
        closing: int,
    ) -> list[int]:
        targets: list[int] = []
        pending = [(opening, closing, 0)]
        while pending:
            pattern_opening, pattern_closing, depth = pending.pop()
            if depth >= MAX_DESTRUCTURING_PATTERN_NESTING:
                raise SchedulerClientError(
                    "bootstrap identity pattern is nested too deeply"
                )
            if tokens[pattern_opening] != "{":
                continue
            cursor = pattern_opening + 1
            while cursor < pattern_closing:
                if tokens[cursor] == ",":
                    cursor += 1
                    continue
                entry_start = cursor
                if tokens[cursor] in {".", "..."}:
                    cursor = _pattern_comma(entry_start, pattern_closing) + 1
                    continue
                key_index = cursor
                selector = None
                if tokens[cursor] == "[":
                    key_closing = delimiter_matches.get(cursor)
                    if key_closing is None or key_closing >= pattern_closing:
                        cursor = _pattern_comma(entry_start, pattern_closing) + 1
                        continue
                    selector = _static_key(cursor + 1, key_closing)
                    cursor = key_closing + 1
                else:
                    key = tokens[cursor]
                    if isinstance(key, str) and key.isidentifier():
                        selector = key
                    elif isinstance(key, tuple) and not key[2]:
                        selector = key[1]
                    cursor += 1

                if cursor < pattern_closing and tokens[cursor] == ":":
                    target_start = cursor + 1
                    if (
                        selector in _BOOTSTRAP_GLOBAL_IDENTITY_SELECTORS
                        and target_start < pattern_closing
                    ):
                        if tokens[target_start] == "{":
                            target_closing = delimiter_matches.get(target_start)
                            if (
                                target_closing is not None
                                and target_closing < pattern_closing
                            ):
                                pending.append(
                                    (
                                        target_start,
                                        target_closing,
                                        depth + 1,
                                    )
                                )
                        elif (
                            isinstance(tokens[target_start], str)
                            and tokens[target_start].isidentifier()
                            and (
                                target_start + 1 == pattern_closing
                                or tokens[target_start + 1] in {"=", ","}
                            )
                        ):
                            targets.append(target_start)
                elif (
                    selector in _BOOTSTRAP_GLOBAL_IDENTITY_SELECTORS
                    and isinstance(tokens[key_index], str)
                    and tokens[key_index].isidentifier()
                ):
                    targets.append(key_index)
                cursor = _pattern_comma(cursor, pattern_closing) + 1
        return targets

    def _pattern_comma(index: int, limit: int) -> int:
        while index < limit:
            if tokens[index] in {"(", "[", "{"}:
                nested_closing = delimiter_matches.get(index)
                if nested_closing is None or nested_closing >= limit:
                    return limit
                index = nested_closing + 1
            elif tokens[index] == ",":
                return index
            else:
                index += 1
        return limit

    def _classify_target(
        start: int,
        end: int,
        depth: int = 0,
    ) -> tuple[str, int | None]:
        start, end = _unwrap_parentheses(start, end)
        if depth > 3 or start >= end:
            return "unknown", None
        if (
            end - start >= 3
            and tokens[start] == tokens[start + 1] == tokens[start + 2] == "."
        ):
            return "unknown", None
        if end - start == 1:
            target = _simple_target(start, end)
            reference = start if target == "vars" else None
            return target, reference

        final_token = tokens[end - 1]
        if isinstance(final_token, str) and final_token.isidentifier():
            dot_index = end - 2
            if dot_index >= start and tokens[dot_index] == ".":
                base_end = dot_index
                if dot_index > start and tokens[dot_index - 1] == "?":
                    base_end -= 1
                base, _ = _classify_target(start, base_end, depth + 1)
                if final_token == "vars" and base == "global":
                    return "vars", end - 1
                if (
                    final_token in _BOOTSTRAP_GLOBAL_IDENTITY_SELECTORS
                    and base == "global"
                ):
                    return "global", None
                if base in {"vars", "vars_member"}:
                    return "vars_member", None

        if final_token == "]":
            opening = closing_matches.get(end - 1)
            if opening is not None and opening > start:
                base, _ = _classify_target(start, opening, depth + 1)
                key = _static_key(opening + 1, end - 1)
                if key == "vars" and base == "global":
                    return "vars", None
                if (
                    key in _BOOTSTRAP_GLOBAL_IDENTITY_SELECTORS
                    and base == "global"
                ):
                    return "global", None
                if base in {"vars", "vars_member"}:
                    return "vars_member", None
        return "unknown", None

    def _arguments_by_call(
        invocation_openings: dict[int, int],
    ) -> dict[int, list[tuple[int, int]]]:
        arguments_by_call = {opening: [] for opening in invocation_openings}
        argument_starts = {
            opening: opening + 1 for opening in invocation_openings
        }
        opening_stack: list[int] = []
        for index, token in enumerate(tokens):
            if token in {"(", "{", "["}:
                opening_stack.append(index)
                continue
            if token in {")", "}", "]"}:
                opening = closing_matches.get(index)
                if opening in invocation_openings:
                    start = argument_starts[opening]
                    if start < index:
                        arguments_by_call[opening].append((start, index))
                if opening_stack and opening_stack[-1] == opening:
                    opening_stack.pop()
                continue
            if token == "," and opening_stack:
                opening = opening_stack[-1]
                if opening in invocation_openings:
                    arguments_by_call[opening].append(
                        (argument_starts[opening], index)
                    )
                    argument_starts[opening] = index + 1
        return arguments_by_call

    def _is_invocation(opening: int, closing: int) -> bool:
        if closing + 1 < len(tokens) and tokens[closing + 1] in {"=>", "{"}:
            return False
        previous_index = opening - 1
        if previous_index < 0:
            return False
        previous = tokens[previous_index]
        if previous in {
            "if",
            "for",
            "while",
            "switch",
            "catch",
            "with",
            "function",
            "return",
            "throw",
            "case",
            "delete",
            "typeof",
            "void",
            "yield",
            "await",
        }:
            return False
        return (
            isinstance(previous, str) and previous.isidentifier()
        ) or previous in {")", "]", ".", "?"}

    expression_start_cache: dict[int, int] = {}

    def _expression_start(end_index: int) -> int:
        visited: list[int] = []
        start = end_index
        while start >= 0:
            cached = expression_start_cache.get(start)
            if cached is not None:
                start = cached
                break
            visited.append(start)
            token = tokens[start]
            if token == "]":
                opening = closing_matches.get(start)
                if opening is not None:
                    if (
                        opening >= 2
                        and tokens[opening - 2] == "?"
                        and tokens[opening - 1] == "."
                    ):
                        start = opening - 3
                        continue
                    if opening > 0 and tokens[opening - 1] == "?":
                        start = opening - 2
                        continue
                    start = opening - 1
                    continue
            if token == ")":
                opening = closing_matches.get(start)
                if opening is not None:
                    if (
                        opening > 0
                        and isinstance(tokens[opening - 1], str)
                        and tokens[opening - 1].isidentifier()
                    ):
                        start = opening - 1
                        continue
                    start = opening
                    break
            if isinstance(token, str) and token.isidentifier():
                if (
                    start >= 3
                    and tokens[start - 1] == "."
                    and tokens[start - 2] == "?"
                ):
                    start -= 3
                    continue
                if start >= 2 and tokens[start - 1] == ".":
                    start -= 2
                    continue
            break
        if start < 0:
            start = 0
        for index in visited:
            expression_start_cache[index] = start
        return start

    def _expression_end(start_index: int) -> int:
        if start_index < 0 or start_index >= len(tokens):
            return start_index
        if tokens[start_index] == "(":
            closing = delimiter_matches.get(start_index)
            if closing is None:
                return start_index + 1
            end_index = closing + 1
        elif (
            isinstance(tokens[start_index], str)
            and tokens[start_index].isidentifier()
        ):
            end_index = start_index + 1
        else:
            return start_index + 1
        while end_index < len(tokens):
            token = tokens[end_index]
            if (
                token == "?"
                and end_index + 2 < len(tokens)
                and tokens[end_index + 1] == "."
                and isinstance(tokens[end_index + 2], str)
                and tokens[end_index + 2].isidentifier()
            ):
                end_index += 3
            elif (
                token == "."
                and end_index + 1 < len(tokens)
                and isinstance(tokens[end_index + 1], str)
                and tokens[end_index + 1].isidentifier()
            ):
                end_index += 2
            elif token in {"[", "("}:
                closing = delimiter_matches.get(end_index)
                if closing is None:
                    break
                end_index = closing + 1
            else:
                break
        return end_index

    def _concise_arrow_body_end(start_index: int) -> int:
        line_terminators_before = getattr(
            tokens,
            "line_terminators_before",
            (),
        )

        def _arrow_body_start(index: int) -> int | None:
            if index >= len(tokens):
                return None
            if (
                isinstance(tokens[index], str)
                and tokens[index].isidentifier()
                and index + 2 < len(tokens)
                and tokens[index + 1 : index + 3] == ["=", ">"]
            ):
                return index + 3
            if tokens[index] != "(":
                return None
            closing = delimiter_matches.get(index)
            if (
                closing is not None
                and closing + 2 < len(tokens)
                and tokens[closing + 1 : closing + 3] == ["=", ">"]
            ):
                return closing + 3
            return None

        def _function_body_end(start: int) -> int | None:
            cursor = start + 1
            while cursor < len(tokens):
                token = tokens[cursor]
                if token == "{":
                    closing = delimiter_matches.get(cursor)
                    return None if closing is None else closing + 1
                if token in {"(", "["}:
                    closing = delimiter_matches.get(cursor)
                    if closing is None:
                        return None
                    cursor = closing + 1
                    continue
                if token in {",", ";", ")", "]", "}"}:
                    return None
                cursor += 1
            return None

        def _template_expression_end(start: int) -> int | None:
            cursor = start + 1
            while cursor < len(tokens):
                token = tokens[cursor]
                if token == _INTERPOLATED_TEMPLATE_END_TOKEN:
                    return cursor + 1
                if token != _TEMPLATE_BOUNDARY_TOKEN:
                    return None
                cursor += 1
                if cursor >= len(tokens) or tokens[cursor] != "(":
                    return None
                closing = delimiter_matches.get(cursor)
                if closing is None:
                    return None
                cursor = closing + 1
                if (
                    cursor >= len(tokens)
                    or tokens[cursor] != _TEMPLATE_BOUNDARY_TOKEN
                ):
                    return None
                cursor += 1
            return None

        def _operator_at(
            index: int,
        ) -> tuple[int, int, bool] | None:
            if (
                index + 1 < len(tokens)
                and tokens[index] == "="
                and tokens[index + 1] == ">"
            ):
                return None
            for parts, precedence, right_associative in (
                _BOOTSTRAP_ARROW_BINARY_OPERATORS
            ):
                if index + len(parts) > len(tokens):
                    continue
                if all(
                    tokens[index + offset] == part
                    for offset, part in enumerate(parts)
                ):
                    return len(parts), precedence, right_associative
            return None

        def _primary_end(start: int, depth: int) -> int | None:
            if depth > MAX_ARROW_EXPRESSION_NESTING:
                raise SchedulerClientError(
                    "bootstrap arrow expression is nested too deeply"
                )
            if start >= len(tokens):
                return None
            token = tokens[start]

            if token == "async":
                arrow_body = _arrow_body_start(start + 1)
                if arrow_body is not None:
                    return _arrow_expression_end(arrow_body, depth + 1)
                if (
                    start + 1 < len(tokens)
                    and tokens[start + 1] == "function"
                ):
                    end = _function_body_end(start + 1)
                    return end

            arrow_body = _arrow_body_start(start)
            if arrow_body is not None:
                return _arrow_expression_end(arrow_body, depth + 1)
            if token == "function":
                end = _function_body_end(start)
                return end
            if token == _INTERPOLATED_TEMPLATE_TOKEN:
                end = _template_expression_end(start)
                if end is None:
                    return None
            elif token == "(":
                closing = delimiter_matches.get(start)
                if closing is None:
                    return None
                end = closing + 1
            elif token in {"[", "{"}:
                closing = delimiter_matches.get(start)
                if closing is None:
                    return None
                end = closing + 1
            elif token in {
                "!",
                "~",
                "+",
                "-",
                "typeof",
                "void",
                "delete",
                "await",
                "yield",
                "new",
            }:
                operand_start = start + 1
                if (
                    token in {"+", "-"}
                    and operand_start < len(tokens)
                    and tokens[operand_start] == token
                ):
                    operand_start += 1
                elif (
                    token == "yield"
                    and operand_start < len(tokens)
                    and tokens[operand_start] == "*"
                ):
                    operand_start += 1
                return _primary_end(operand_start, depth + 1)
            elif token in {",", ";", ")", "]", "}", ":", "=>"}:
                return None
            else:
                end = start + 1

            while end < len(tokens):
                suffix = tokens[end]
                if (
                    suffix == "."
                    and end + 1 < len(tokens)
                    and isinstance(tokens[end + 1], str)
                    and tokens[end + 1].isidentifier()
                ):
                    end += 2
                elif (
                    suffix == "?"
                    and end + 1 < len(tokens)
                    and tokens[end + 1] == "."
                ):
                    if (
                        end + 2 < len(tokens)
                        and tokens[end + 2] in {"[", "("}
                    ):
                        closing = delimiter_matches.get(end + 2)
                        if closing is None:
                            return None
                        end = closing + 1
                    elif (
                        end + 2 < len(tokens)
                        and isinstance(tokens[end + 2], str)
                        and tokens[end + 2].isidentifier()
                    ):
                        end += 3
                    else:
                        break
                elif suffix in {"[", "("}:
                    closing = delimiter_matches.get(end)
                    if closing is None:
                        return None
                    end = closing + 1
                elif (
                    suffix in {"+", "-"}
                    and end + 1 < len(tokens)
                    and tokens[end + 1] == suffix
                    and end not in line_terminators_before
                ):
                    end += 2
                else:
                    break
            return end

        def _operator_expression_end(
            start: int,
            minimum_precedence: int,
            depth: int,
        ) -> int | None:
            if depth > MAX_ARROW_EXPRESSION_NESTING:
                raise SchedulerClientError(
                    "bootstrap arrow expression is nested too deeply"
                )
            end = _primary_end(start, depth + 1)
            if end is None:
                return None
            while end < len(tokens):
                if (
                    end in line_terminators_before
                    and tokens[end] in {"+", "-"}
                    and end + 1 < len(tokens)
                    and tokens[end + 1] == tokens[end]
                ):
                    break
                operator = _operator_at(end)
                if operator is not None:
                    operator_length, precedence, right_associative = operator
                    if precedence < minimum_precedence:
                        break
                    right_end = _operator_expression_end(
                        end + operator_length,
                        precedence if right_associative else precedence + 1,
                        depth + 1,
                    )
                    if right_end is None:
                        return None
                    end = right_end
                    continue
                if tokens[end] == "?":
                    if 3 < minimum_precedence:
                        break
                    consequent_end = _operator_expression_end(
                        end + 1,
                        0,
                        depth + 1,
                    )
                    if (
                        consequent_end is None
                        or consequent_end >= len(tokens)
                        or tokens[consequent_end] != ":"
                    ):
                        return None
                    alternate_end = _operator_expression_end(
                        consequent_end + 1,
                        3,
                        depth + 1,
                    )
                    if alternate_end is None:
                        return None
                    end = alternate_end
                    continue
                break
            return end

        def _arrow_expression_end(
            body_start: int,
            depth: int,
        ) -> int | None:
            if depth > MAX_ARROW_EXPRESSION_NESTING:
                raise SchedulerClientError(
                    "bootstrap arrow expression is nested too deeply"
                )
            if body_start >= len(tokens):
                return None
            if tokens[body_start] == "{":
                closing = delimiter_matches.get(body_start)
                return None if closing is None else closing + 1
            return _operator_expression_end(body_start, 0, depth + 1)

        end = _operator_expression_end(start_index, 0, 0)
        if end is None:
            raise SchedulerClientError(
                "bootstrap arrow expression scope is unsupported"
            )
        if end < len(tokens):
            token = tokens[end]
            if token not in {",", ";", ")", "]", "}", _TEMPLATE_BOUNDARY_TOKEN}:
                if end in line_terminators_before and (
                    isinstance(token, str)
                    and token.isidentifier()
                    or isinstance(token, tuple)
                    or (
                        token in {"+", "-"}
                        and end + 1 < len(tokens)
                        and tokens[end + 1] == token
                    )
                ):
                    return end
                raise SchedulerClientError(
                    "bootstrap arrow expression boundary is unsupported"
                )
        return end


    def _callee_receiver(opening: int) -> tuple[str, int, int]:
        callee_index = opening - 1
        if callee_index < 0:
            return "unknown", -1, -1
        if callee_index >= 2 and tokens[callee_index - 1] == ".":
            receiver_end = callee_index - 2
            if receiver_end >= 0 and tokens[receiver_end] == "?":
                receiver_end -= 1
            receiver_start = _expression_start(receiver_end)
            receiver_exclusive = receiver_end + 1
            return (
                _classify_target(receiver_start, receiver_exclusive)[0],
                receiver_start,
                receiver_exclusive,
            )
        if tokens[callee_index] == "]":
            bracket = closing_matches.get(callee_index)
            if bracket is not None:
                receiver_end = bracket - 1
                if (
                    receiver_end >= 2
                    and tokens[receiver_end] == "."
                    and tokens[receiver_end - 1] == "?"
                ):
                    receiver_end -= 2
                receiver_start = _expression_start(receiver_end)
                receiver_exclusive = receiver_end + 1
                return (
                    _classify_target(receiver_start, receiver_exclusive)[0],
                    receiver_start,
                    receiver_exclusive,
                )
        if tokens[callee_index] == ")":
            receiver_start = _expression_start(callee_index)
            receiver_exclusive = callee_index + 1
            return (
                _classify_target(receiver_start, receiver_exclusive)[0],
                receiver_start,
                receiver_exclusive,
            )
        return (
            _classify_target(callee_index, callee_index + 1)[0],
            callee_index,
            callee_index + 1,
        )

    def _member_access(
        start: int,
        end: int,
    ) -> tuple[int, int, str | None] | None:
        start, end = _unwrap_parentheses(start, end)
        if end - start >= 2 and tokens[end - 2] == "?" and tokens[end - 1] == ".":
            end -= 2
            start, end = _unwrap_parentheses(start, end)
        if start >= end:
            return None

        final_token = tokens[end - 1]
        if isinstance(final_token, str) and final_token.isidentifier():
            dot_index = end - 2
            if dot_index < start or tokens[dot_index] != ".":
                return None
            receiver_end = dot_index
            if dot_index > start and tokens[dot_index - 1] == "?":
                receiver_end -= 1
            return start, receiver_end, final_token

        if final_token == "]":
            opening = closing_matches.get(end - 1)
            if opening is None or opening <= start:
                return None
            receiver_end = opening
            if (
                opening >= start + 2
                and tokens[opening - 2] == "?"
                and tokens[opening - 1] == "."
            ):
                receiver_end = opening - 2
            elif opening > start and tokens[opening - 1] == "?":
                receiver_end = opening - 1
            return start, receiver_end, _static_key(opening + 1, end - 1)
        return None

    global_alias_reference_cache: dict[
        tuple[int, int], tuple[str, int, bool] | None
    ] | None = None

    def _global_alias_reference(
        start: int,
        end: int,
        *,
        require_identity_member: bool = False,
        allow_unregistered_base: bool = False,
    ) -> tuple[str, int] | None:
        start, end = _unwrap_parentheses(start, end)
        reference_cache = global_alias_reference_cache
        path: list[tuple[tuple[int, int], bool]] = []
        structural_reference: tuple[str, int, bool] | None = None
        while start < end:
            start, end = _unwrap_parentheses(start, end)
            interval = (start, end)
            if reference_cache is not None and interval in reference_cache:
                structural_reference = reference_cache[interval]
                break
            if end - start == 1:
                name = tokens[start]
                if isinstance(name, str) and name.isidentifier():
                    structural_reference = (name, start, False)
                    if reference_cache is not None:
                        reference_cache[interval] = structural_reference
                break
            member = _member_access(start, end)
            if (
                member is None
                or member[2] not in _BOOTSTRAP_GLOBAL_IDENTITY_SELECTORS
            ):
                path.append((interval, False))
                break
            path.append((interval, True))
            start, end = member[0], member[1]
        for interval, contains_identity_member in reversed(path):
            if structural_reference is not None:
                name, base_index, inner_contains_identity_member = (
                    structural_reference
                )
                structural_reference = (
                    name,
                    base_index,
                    contains_identity_member or inner_contains_identity_member,
                )
            if reference_cache is not None:
                reference_cache[interval] = structural_reference
        if structural_reference is None:
            return None
        name, base_index, contains_identity_member = structural_reference
        if (
            (allow_unregistered_base or name in global_aliases)
            and (not require_identity_member or contains_identity_member)
        ):
            return name, base_index
        return None

    def _global_alias_name(start: int, end: int) -> str | None:
        start, end = _unwrap_parentheses(start, end)
        if end - start != 1:
            return None
        token = tokens[start]
        return token if isinstance(token, str) and token in global_aliases else None

    def _callee_details(
        opening: int,
    ) -> tuple[str, int, int, str | None, bool]:
        callee_end = opening
        if (
            opening >= 2
            and tokens[opening - 2] == "?"
            and tokens[opening - 1] == "."
        ):
            callee_end -= 2
        callee_start = (
            _expression_start(callee_end - 1) if callee_end > 0 else 0
        )
        member = _member_access(callee_start, callee_end)
        if member is not None:
            receiver_start, receiver_end, selector = member
            receiver_kind, _ = _classify_target(receiver_start, receiver_end)
            return (
                receiver_kind,
                receiver_start,
                receiver_end,
                selector,
                True,
            )

        callee_kind, _ = _classify_target(callee_start, callee_end)
        if callee_kind in {"global", "vars", "vars_member"}:
            return callee_kind, callee_start, callee_end, None, False
        receiver_kind, receiver_start, receiver_end = _callee_receiver(opening)
        return receiver_kind, receiver_start, receiver_end, None, False

    def _assignment_operator_start(index: int) -> int | None:
        if index >= len(tokens) or tokens[index] != "=":
            return None
        if index + 1 < len(tokens) and tokens[index + 1] in {"=", ">"}:
            return None
        if index > 0 and tokens[index - 1] == "=":
            return None
        if index > 0 and tokens[index - 1] == "!":
            return None
        if index > 0 and tokens[index - 1] in {"<", ">"}:
            operator = tokens[index - 1]
            count = 1
            while index - count - 1 >= 0 and tokens[index - count - 1] == operator:
                count += 1
            return index - count if count > 1 else None
        if index > 0 and tokens[index - 1] in {
            "+",
            "-",
            "*",
            "/",
            "%",
            "&",
            "|",
            "^",
            "?",
        }:
            operator = tokens[index - 1]
            start = index - 1
            if (
                operator in {"*", "&", "|", "?"}
                and start > 0
                and tokens[start - 1] == operator
            ):
                start -= 1
            return start
        return index

    def _proven_alias_source(name_index: int) -> str | None:
        if (
            name_index == 0
            or tokens[name_index - 1] not in {"var", "let", "const"}
            or name_index + 2 >= len(tokens)
            or tokens[name_index + 1] != "="
        ):
            return None
        value_index = name_index + 2
        value_end = value_index + 1
        if (
            tokens[value_index] == "("
            and delimiter_matches.get(value_index) == value_index + 2
        ):
            value_index += 1
            value_end = value_index + 2
        value = tokens[value_index] if value_index < len(tokens) else None
        if (
            not isinstance(value, str)
            or value not in global_aliases
            or (
                value_end < len(tokens)
                and tokens[value_end] not in {",", ";", "}"}
            )
        ):
            return None
        return value

    def _proven_reassignment_alias_source(name_index: int) -> str | None:
        value = _simple_alias_assignment_source(tokens, name_index)
        return value if value in global_aliases else None

    def _is_shadow_binding_range(start: int, end: int) -> bool:
        opening = start - 1
        if opening < 0:
            return False
        if tokens[opening] in {"{", "["}:
            return (
                end + 1 < len(tokens)
                and tokens[end + 1] in {"=", "of", "in"}
            )
        if tokens[opening] != "(":
            return False
        if (
            end + 2 < len(tokens)
            and tokens[end + 1] == "="
            and tokens[end + 2] == ">"
        ):
            return True
        if end + 1 >= len(tokens) or tokens[end + 1] != "{":
            return False
        previous = tokens[opening - 1] if opening > 0 else None
        if previous in {"if", "for", "while", "switch", "with"}:
            return False
        if previous in {"function", "catch"}:
            return True
        if opening >= 2 and tokens[opening - 2] == "function":
            return True
        return (
            isinstance(previous, str) and previous.isidentifier()
        ) or previous == "]"


    reflect_shadowed = False
    parameter_ranges: list[tuple[int, int]] = []
    for opening, closing in delimiter_matches.items():
        if (
            tokens[opening] == "("
            and closing + 1 < len(tokens)
            and (
                tokens[closing + 1] == "{"
                or (
                    closing + 2 < len(tokens)
                    and tokens[closing + 1] == "="
                    and tokens[closing + 2] == ">"
                )
            )
        ):
            parameter_ranges.append((opening + 1, closing))
        elif (
            tokens[opening] in {"{", "["}
            and closing + 1 < len(tokens)
            and tokens[closing + 1] in {"=", "of", "in"}
        ):
            parameter_ranges.append((opening + 1, closing))
    identity_alias_sources: dict[int, tuple[str, int]] = {}
    identity_alias_declaration_indexes: dict[int, int] = {}
    identity_alias_dependents: dict[str, set[str]] = {}
    line_terminators_before = getattr(tokens, "line_terminators_before", ())

    def _record_identity_alias(
        target_index: int,
        source_reference: tuple[str, int],
        declaration_index: int | None = None,
    ) -> None:
        target_name = tokens[target_index]
        if not isinstance(target_name, str) or not target_name.isidentifier():
            return
        source_name, source_index = source_reference
        identity_alias_sources[target_index] = (source_name, source_index)
        identity_alias_dependents.setdefault(source_name, set()).add(target_name)
        if declaration_index is not None:
            identity_alias_declaration_indexes[target_index] = declaration_index

    for opening, closing in delimiter_matches.items():
        if (
            tokens[opening] != "{"
            or closing + 2 >= len(tokens)
            or tokens[closing + 1] != "="
        ):
            continue
        source_start = closing + 2
        try:
            source_end = _concise_arrow_body_end(source_start)
        except SchedulerClientError:
            continue
        source_start, source_end = _unwrap_parentheses(
            source_start,
            source_end,
        )
        if (
            source_end < len(tokens)
            and tokens[source_end] not in {",", ";", ")", "}", "]"}
            and source_end not in line_terminators_before
        ):
            continue
        source_reference = _global_alias_reference(
            source_start,
            source_end,
            allow_unregistered_base=True,
        )
        if source_reference is None:
            continue
        declaration_index = opening - 1
        if (
            declaration_index < 0
            or tokens[declaration_index] not in {"var", "let", "const"}
        ):
            declaration_index = None
        for target_index in _global_identity_pattern_targets(
            opening,
            closing,
        ):
            _record_identity_alias(
                target_index,
                source_reference,
                declaration_index,
            )

    for declaration_index, declaration in enumerate(tokens):
        if (
            declaration not in {"var", "let", "const"}
            or declaration_index + 3 >= len(tokens)
            or not isinstance(tokens[declaration_index + 1], str)
            or not tokens[declaration_index + 1].isidentifier()
            or tokens[declaration_index + 2] != "="
        ):
            continue
        target_index = declaration_index + 1
        source_start = target_index + 2
        try:
            source_end = _concise_arrow_body_end(source_start)
        except SchedulerClientError:
            continue
        source_reference = _global_alias_reference(
            source_start,
            source_end,
            require_identity_member=True,
            allow_unregistered_base=True,
        )
        if source_reference is not None:
            _record_identity_alias(
                target_index,
                source_reference,
                declaration_index,
            )

    binding_pattern_ranges = list(parameter_ranges)
    for opening, closing in delimiter_matches.items():
        if (
            tokens[opening] in {"{", "["}
            and closing + 1 < len(tokens)
            and tokens[closing + 1] in {"=", "of", "in"}
        ):
            binding_pattern_ranges.append((opening + 1, closing))
    for target_index, target_name in enumerate(tokens):
        if (
            not isinstance(target_name, str)
            or not target_name.isidentifier()
            or target_index + 1 >= len(tokens)
            or _assignment_operator_start(target_index + 1) != target_index + 1
            or (
                target_index > 0
                and tokens[target_index - 1]
                in {"var", "let", "const", ".", "?"}
            )
            or any(
                start <= target_index < end
                for start, end in binding_pattern_ranges
            )
        ):
            continue
        source_start = target_index + 2
        try:
            source_end = _concise_arrow_body_end(source_start)
        except SchedulerClientError:
            continue
        source_reference = _global_alias_reference(
            source_start,
            source_end,
            require_identity_member=True,
            allow_unregistered_base=True,
        )
        if source_reference is not None:
            _record_identity_alias(target_index, source_reference)

    pending_identity_aliases = list(global_aliases)
    while pending_identity_aliases:
        source_name = pending_identity_aliases.pop()
        for target_name in identity_alias_dependents.get(source_name, ()):
            if target_name not in global_aliases:
                global_aliases.add(target_name)
                pending_identity_aliases.append(target_name)
    global_alias_reference_cache = {}
    range_changes = [0] * (len(tokens) + 1)
    for start, end in parameter_ranges:
        range_changes[start] += 1
        range_changes[end] -= 1

    shadowed_global_aliases: set[str] = {"this"}
    alias_dependents: dict[
        tuple[str, int | None],
        set[tuple[str, int | None]],
    ] = {}
    builtin_global_aliases = {
        "window",
        "globalThis",
        "self",
        "frames",
        "top",
        "parent",
        "this",
    }
    shadow_binding_changes = [0] * (len(tokens) + 1)
    for start, end in parameter_ranges:
        if _is_shadow_binding_range(start, end):
            shadow_binding_changes[start] += 1
            shadow_binding_changes[end] -= 1

    brace_scope_at_index: list[int | None] = []
    brace_scope_parent: dict[int, int | None] = {}
    brace_stack: list[int] = []
    for scope_index, scope_token in enumerate(tokens):
        if scope_token == "{":
            brace_scope_parent[scope_index] = (
                brace_stack[-1] if brace_stack else None
            )
            brace_stack.append(scope_index)
        brace_scope_at_index.append(brace_stack[-1] if brace_stack else None)
        if scope_token == "}" and brace_stack:
            brace_stack.pop()

    scoped_shadowed_aliases: dict[str, set[int]] = {}
    scoped_shadowed_alias_ranges: dict[str, list[tuple[int, int]]] = {}
    function_body_scopes: set[int] = set()
    parameter_scope_at_index: list[int | None] = [None] * len(tokens)
    parameter_binding_indexes: set[int] = set()
    parameter_name_indexes: set[int] = set()
    alias_binding_scopes: dict[str, set[int | None]] = {}
    identifier_binding_scopes: dict[str, set[int | None]] = {}
    identifier_binding_ranges: dict[str, list[tuple[int, int]]] = {}

    def _scope_chain(scope: int | None) -> list[int | None]:
        chain: list[int | None] = []
        while scope is not None:
            if len(chain) >= MAX_BINDING_SCOPE_NESTING:
                raise SchedulerClientError(
                    "bootstrap JavaScript scope is nested too deeply"
                )
            chain.append(scope)
            scope = brace_scope_parent.get(scope)
        chain.append(None)
        return chain

    def _nearest_function_scope(scope: int | None) -> int | None:
        for ancestor in _scope_chain(scope):
            if ancestor in function_body_scopes:
                return ancestor
        return None

    def _is_parameter_property_key(index: int) -> bool:
        return (
            index > 0
            and index + 1 < len(tokens)
            and tokens[index - 1] in {"{", ","}
            and tokens[index + 1] == ":"
        )

    def _record_parameter_binding(
        start: int,
        end: int,
        *,
        body_scope: int | None = None,
        expression_range: tuple[int, int] | None = None,
    ) -> None:
        if body_scope is not None or expression_range is not None:
            cursor = start
            segment_start = start
            while cursor <= end:
                if cursor == end or tokens[cursor] == ",":
                    name_index = segment_start
                    if tokens[name_index : name_index + 3] == [".", ".", "."]:
                        name_index += 3
                    if (
                        name_index < cursor
                        and isinstance(tokens[name_index], str)
                        and tokens[name_index].isidentifier()
                        and (
                            name_index + 1 == cursor
                            or tokens[name_index + 1] == "="
                        )
                    ):
                        name = tokens[name_index]
                        parameter_name_indexes.add(name_index)
                        if body_scope is not None:
                            identifier_binding_scopes.setdefault(
                                name,
                                set(),
                            ).add(body_scope)
                        elif expression_range is not None:
                            identifier_binding_ranges.setdefault(
                                name,
                                [],
                            ).append(expression_range)
                    segment_start = cursor + 1
                elif tokens[cursor] in {"(", "[", "{"}:
                    closing = delimiter_matches.get(cursor)
                    if closing is not None and closing < end:
                        cursor = closing
                cursor += 1

        for index in range(start, end):
            parameter_binding_indexes.add(index)
            token = tokens[index]
            if (
                not isinstance(token, str)
                or token not in global_aliases
                or _is_parameter_property_key(index)
            ):
                continue
            if body_scope is not None:
                parameter_scope_at_index[index] = body_scope
                scoped_shadowed_aliases.setdefault(token, set()).add(body_scope)
                alias_binding_scopes.setdefault(token, set()).add(body_scope)
            elif expression_range is not None:
                scoped_shadowed_alias_ranges.setdefault(token, []).append(
                    expression_range
                )

    def _parameter_needs_lexical_scope(start: int, end: int) -> bool:
        for index in range(start, end):
            name = tokens[index]
            if not isinstance(name, str):
                continue
            if (
                name in global_aliases
                or name in _BOOTSTRAP_CODE_EXECUTION_SELECTORS
                or name in _BOOTSTRAP_TIMER_SELECTORS
            ):
                return True
        return False

    for start, end in parameter_ranges:
        if (
            start == 0
            or tokens[start - 1] != "("
            or not _is_shadow_binding_range(start, end)
        ):
            continue
        body_scope = None
        body_start = None
        if end + 1 < len(tokens) and tokens[end + 1] == "{":
            body_scope = end + 1
        elif (
            end + 3 < len(tokens)
            and tokens[end + 1] == "="
            and tokens[end + 2] == ">"
        ):
            body_start = end + 3
            if tokens[body_start] == "{":
                body_scope = body_start
        if body_scope is not None:
            function_body_scopes.add(body_scope)
            _record_parameter_binding(start, end, body_scope=body_scope)
        elif body_start is not None and _parameter_needs_lexical_scope(
            start,
            end,
        ):
            _record_parameter_binding(
                start,
                end,
                expression_range=(
                    body_start,
                    _concise_arrow_body_end(body_start),
                ),
            )

    for index, token in enumerate(tokens[:-2]):
        if (
            not isinstance(token, str)
            or not token.isidentifier()
            or tokens[index + 1 : index + 3] != ["=", ">"]
        ):
            continue
        body_start = index + 3
        if body_start < len(tokens) and tokens[body_start] == "{":
            function_body_scopes.add(body_start)
            _record_parameter_binding(
                index,
                index + 1,
                body_scope=body_start,
            )
        elif _parameter_needs_lexical_scope(index, index + 1):
            _record_parameter_binding(
                index,
                index + 1,
                expression_range=(
                    body_start,
                    _concise_arrow_body_end(body_start),
                ),
            )

    def _declaration_scope(index: int, kind: str) -> int | None:
        scope = brace_scope_at_index[index]
        if kind == "var":
            return _nearest_function_scope(scope)
        if kind == "catch":
            opening = index - 1
            closing = delimiter_matches.get(opening)
            body = closing + 1 if closing is not None else len(tokens)
            if body < len(tokens) and tokens[body] == "{":
                return body
        return scope

    for index, kind in enumerate(tokens):
        if kind not in {"var", "let", "const", "function", "class", "catch"}:
            continue
        name_index = index + 1
        if (
            name_index >= len(tokens)
            or not isinstance(tokens[name_index], str)
            or not tokens[name_index].isidentifier()
        ):
            continue
        name = tokens[name_index]
        declaration_scope = _declaration_scope(index, kind)
        if kind in {"var", "let", "const"}:
            identifier_binding_scopes.setdefault(name, set()).add(
                declaration_scope
            )
        if name in global_aliases:
            alias_binding_scopes.setdefault(name, set()).add(declaration_scope)

    identity_alias_declaration_scopes: dict[int, int | None] = {}
    for target_index, declaration_index in (
        identity_alias_declaration_indexes.items()
    ):
        name = tokens[target_index]
        declaration_scope = _declaration_scope(
            declaration_index,
            tokens[declaration_index],
        )
        identifier_binding_scopes.setdefault(name, set()).add(
            declaration_scope
        )
        if name in global_aliases:
            alias_binding_scopes.setdefault(name, set()).add(
                declaration_scope
            )
            identity_alias_declaration_scopes[target_index] = (
                declaration_scope
            )

    def _is_lexically_bound(name: str, index: int) -> bool:
        bindings = identifier_binding_scopes.get(name, ())
        if any(
            scope in bindings
            for scope in _scope_chain(brace_scope_at_index[index])
        ):
            return True
        return any(
            start <= index < end
            for start, end in identifier_binding_ranges.get(name, ())
        )

    def _binding_scope_for_alias(name: str, index: int) -> int | None:
        bindings = alias_binding_scopes.get(name, set())
        for scope in _scope_chain(brace_scope_at_index[index]):
            if scope in bindings:
                return scope
        return _nearest_function_scope(brace_scope_at_index[index])

    for index, token in enumerate(tokens):
        if (
            isinstance(token, str)
            and token in global_aliases
            and index + 1 < len(tokens)
            and _assignment_operator_start(index + 1) == index + 1
        ):
            alias_binding_scopes.setdefault(token, set()).add(
                _binding_scope_for_alias(token, index)
            )

    def _is_shadowed_global_alias(name: str, index: int) -> bool:
        if name in shadowed_global_aliases:
            return True
        for scope in _scope_chain(brace_scope_at_index[index]):
            if (
                scope is not None
                and scope in scoped_shadowed_aliases.get(name, ())
            ):
                return True
        return any(
            start <= index < end
            for start, end in scoped_shadowed_alias_ranges.get(name, ())
        )

    def _is_proven_global_alias_occurrence(name: str, index: int) -> bool:
        if _is_shadowed_global_alias(name, index):
            return False
        if name in builtin_global_aliases:
            return True
        bindings = alias_binding_scopes.get(name, set())
        return any(
            scope in bindings for scope in _scope_chain(brace_scope_at_index[index])
        )

    def _add_shadowed_alias(name: str, scope: int | None) -> bool:
        if scope is None:
            if name in shadowed_global_aliases:
                return False
            shadowed_global_aliases.add(name)
            return True
        scopes = scoped_shadowed_aliases.setdefault(name, set())
        if scope in scopes:
            return False
        scopes.add(scope)
        return True

    def _mark_shadowed_alias(
        name: str,
        index: int,
        scope: int | None = None,
    ) -> bool:
        if scope is None:
            scope = parameter_scope_at_index[index]
        if scope is None:
            scope = _binding_scope_for_alias(name, index)
        return _add_shadowed_alias(name, scope)

    def _record_alias_dependency(
        source_name: str,
        target_name: str,
        index: int,
    ) -> None:
        source_scope = _binding_scope_for_alias(source_name, index)
        target_scope = _binding_scope_for_alias(target_name, index)
        alias_dependents.setdefault(
            (source_name, source_scope),
            set(),
        ).add((target_name, target_scope))

    def _propagate_shadowed_aliases(
        seeds: Iterable[tuple[str, int | None]] | None = None,
    ) -> None:
        pending: list[tuple[str, int | None]] = []
        if seeds is None:
            pending.extend(
                (name, None) for name in shadowed_global_aliases
            )
            pending.extend(
                (name, scope)
                for name, scopes in scoped_shadowed_aliases.items()
                for scope in scopes
            )
        else:
            pending.extend(seeds)
        while pending:
            source_name, source_scope = pending.pop()
            for alias_name, alias_scope in alias_dependents.get(
                (source_name, source_scope),
                (),
            ):
                if _add_shadowed_alias(alias_name, alias_scope):
                    pending.append((alias_name, alias_scope))

    alias_assignment_indexes: dict[tuple[str, int | None], list[int]] = {}
    for assignment_index, assignment_token in enumerate(tokens):
        if not isinstance(assignment_token, str) or assignment_token not in global_aliases:
            continue
        alias_source = _proven_reassignment_alias_source(assignment_index)
        if alias_source is None:
            continue
        scope_key = (assignment_token, brace_scope_at_index[assignment_index])
        alias_assignment_indexes.setdefault(scope_key, []).append(assignment_index)
    for target_index in identity_alias_sources:
        if (
            target_index in identity_alias_declaration_scopes
            or target_index in parameter_binding_indexes
        ):
            continue
        target_name = tokens[target_index]
        if isinstance(target_name, str) and target_name in global_aliases:
            alias_assignment_indexes.setdefault(
                (target_name, _binding_scope_for_alias(target_name, target_index)),
                [],
            ).append(target_index)

    safe_uninitialized_alias_declarations: set[int] = set()
    for declaration_index, declaration_kind in enumerate(tokens):
        if (
            declaration_kind not in {"var", "let"}
            or declaration_index + 1 >= len(tokens)
        ):
            continue
        name_index = declaration_index + 1
        name = tokens[name_index]
        if (
            not isinstance(name, str)
            or name not in global_aliases
            or (
                name_index + 1 < len(tokens)
                and tokens[name_index + 1] not in {",", ";", "}"}
            )
        ):
            continue
        assignments = alias_assignment_indexes.get(
            (name, brace_scope_at_index[name_index]),
            (),
        )
        if assignments and assignments[-1] > name_index:
            safe_uninitialized_alias_declarations.add(name_index)

    shadow_binding_depth = 0
    for index, token in enumerate(tokens):
        shadow_binding_depth += shadow_binding_changes[index]
        if not isinstance(token, str) or token not in global_aliases:
            continue
        previous = tokens[index - 1] if index > 0 else None
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        identity_source = identity_alias_sources.get(index)
        if identity_source is not None:
            source_name, source_index = identity_source
            if (
                index in parameter_binding_indexes
                or not _is_proven_global_alias_occurrence(
                    source_name,
                    source_index,
                )
            ):
                _mark_shadowed_alias(
                    token,
                    index,
                    identity_alias_declaration_scopes.get(index),
                )
            else:
                _record_alias_dependency(
                    source_name,
                    token,
                    source_index,
                )
            continue
        if (
            shadow_binding_depth
            and previous != "="
            and not (previous in {"{", ","} and following == ":")
            and index not in parameter_binding_indexes
        ):
            _mark_shadowed_alias(token, index)
        if previous in {"var", "let", "const", "function", "class", "catch"}:
            alias_source = _proven_alias_source(index)
            binding_scope = _declaration_scope(index, previous)
            if token in builtin_global_aliases:
                _mark_shadowed_alias(token, index, binding_scope)
            elif alias_source is None:
                if index not in safe_uninitialized_alias_declarations:
                    _mark_shadowed_alias(token, index, binding_scope)
            elif _is_shadowed_global_alias(alias_source, index):
                _mark_shadowed_alias(token, index, binding_scope)
            else:
                _record_alias_dependency(alias_source, token, index)
            continue
        if previous == "." or (
            previous in {"{", ","} and following == ":"
        ):
            continue
        if (
            index + 2 < len(tokens)
            and following == "="
            and tokens[index + 2] == ">"
        ):
            if index not in parameter_binding_indexes:
                _mark_shadowed_alias(token, index)
            continue

        assignment_start = None
        for offset in range(1, 5):
            if index + offset >= len(tokens):
                break
            assignment_start = _assignment_operator_start(index + offset)
            if assignment_start is not None:
                break
        has_update = (
            index + 2 < len(tokens)
            and tokens[index + 1] == tokens[index + 2]
            and tokens[index + 1] in {"+", "-"}
        ) or (
            index >= 2
            and tokens[index - 1] == tokens[index - 2]
            and tokens[index - 1] in {"+", "-"}
        )
        if assignment_start == index + 1 or has_update:
            alias_source = _proven_reassignment_alias_source(index)
            if alias_source is None or _is_shadowed_global_alias(
                alias_source,
                index,
            ):
                _mark_shadowed_alias(token, index)
            else:
                _record_alias_dependency(alias_source, token, index)
    _propagate_shadowed_aliases()

    def _selection_global_alias_flow(
        start: int,
        end: int,
        depth: int = 0,
    ) -> tuple[bool, bool]:
        if depth > MAX_ARROW_EXPRESSION_NESTING:
            raise SchedulerClientError(
                "bootstrap global identity selection is nested too deeply"
            )
        start, end = _unwrap_parentheses(start, end)
        if start >= end:
            return False, False

        source_reference = _global_alias_reference(start, end)
        if (
            source_reference is not None
            and _is_proven_global_alias_occurrence(*source_reference)
        ):
            return False, True

        identity_member = _member_access(start, end)
        if (
            identity_member is not None
            and identity_member[2] in _BOOTSTRAP_GLOBAL_IDENTITY_SELECTORS
        ):
            has_selection, may_be_global = _selection_global_alias_flow(
                identity_member[0],
                identity_member[1],
                depth + 1,
            )
            if has_selection and may_be_global:
                return True, True

        if (
            tokens[start] == "function"
            or tokens[start : start + 2] == ["async", "function"]
            or (
                isinstance(tokens[start], str)
                and tokens[start].isidentifier()
                and tokens[start + 1 : start + 3] == ["=", ">"]
            )
            or (
                tokens[start] == "("
                and delimiter_matches.get(start) is not None
                and delimiter_matches[start] + 2 < end
                and tokens[delimiter_matches[start] + 1 : delimiter_matches[start] + 3]
                == ["=", ">"]
            )
            or (
                tokens[start] == "async"
                and start + 3 < end
                and isinstance(tokens[start + 1], str)
                and tokens[start + 1].isidentifier()
                and tokens[start + 2 : start + 4] == ["=", ">"]
            )
        ):
            return False, False

        question_index: int | None = None
        cursor = start
        while cursor < end:
            token = tokens[cursor]
            if token in {"(", "[", "{"}:
                closing = delimiter_matches.get(cursor)
                if closing is not None and closing < end:
                    cursor = closing + 1
                    continue
            if (
                token == "?"
                and cursor + 1 < end
                and tokens[cursor + 1] not in {".", "?"}
                and (cursor == start or tokens[cursor - 1] != "?")
            ):
                question_index = cursor
                break
            cursor += 1

        if question_index is not None:
            nested_questions = 1
            cursor = question_index + 1
            alternate_index: int | None = None
            while cursor < end:
                token = tokens[cursor]
                if token in {"(", "[", "{"}:
                    closing = delimiter_matches.get(cursor)
                    if closing is not None and closing < end:
                        cursor = closing + 1
                        continue
                if (
                    token == "?"
                    and cursor + 1 < end
                    and tokens[cursor + 1] not in {".", "?"}
                    and (cursor == start or tokens[cursor - 1] != "?")
                ):
                    nested_questions += 1
                elif token == ":":
                    nested_questions -= 1
                    if nested_questions == 0:
                        alternate_index = cursor
                        break
                cursor += 1
            if alternate_index is None:
                return True, False
            _, consequent_global = _selection_global_alias_flow(
                question_index + 1,
                alternate_index,
                depth + 1,
            )
            _, alternate_global = _selection_global_alias_flow(
                alternate_index + 1,
                end,
                depth + 1,
            )
            return True, consequent_global or alternate_global

        has_logical_selection = False
        selection_may_be_global = False
        segment_start = start
        cursor = start
        while cursor < end:
            token = tokens[cursor]
            if token in {"(", "[", "{"}:
                closing = delimiter_matches.get(cursor)
                if closing is not None and closing < end:
                    cursor = closing + 1
                    continue
            is_logical_selection = (
                cursor + 1 < end
                and (
                    token == "|"
                    and tokens[cursor + 1] == "|"
                    or token == "&"
                    and tokens[cursor + 1] == "&"
                    or token == "?"
                    and tokens[cursor + 1] == "?"
                )
                and (cursor + 2 >= end or tokens[cursor + 2] != "=")
            )
            if is_logical_selection:
                has_logical_selection = True
                _, segment_is_global = _selection_global_alias_flow(
                    segment_start,
                    cursor,
                    depth + 1,
                )
                selection_may_be_global = (
                    selection_may_be_global or segment_is_global
                )
                segment_start = cursor + 2
                cursor += 2
            else:
                cursor += 1

        if not has_logical_selection:
            return False, False
        _, final_segment_is_global = _selection_global_alias_flow(
            segment_start,
            end,
            depth + 1,
        )
        return (
            True,
            selection_may_be_global or final_segment_is_global,
        )

    for target_index, token in enumerate(tokens):
        if not isinstance(token, str) or not token.isidentifier():
            continue
        is_declaration = (
            target_index > 0
            and tokens[target_index - 1] in {"var", "let", "const"}
            and target_index + 1 < len(tokens)
            and tokens[target_index + 1] == "="
        )
        is_assignment = (
            not is_declaration
            and target_index + 1 < len(tokens)
            and _assignment_operator_start(target_index + 1)
            == target_index + 1
            and not (
                target_index > 0
                and tokens[target_index - 1] in {".", "?"}
            )
            and not any(
                range_start <= target_index < range_end
                for range_start, range_end in binding_pattern_ranges
            )
        )
        if not is_declaration and not is_assignment:
            continue
        source_start = target_index + 2
        try:
            source_end = _concise_arrow_body_end(source_start)
        except SchedulerClientError:
            continue
        has_selection, may_be_global = _selection_global_alias_flow(
            source_start,
            source_end,
        )
        if has_selection and may_be_global:
            raise SchedulerClientError(
                "bootstrap derives a global identity from a compound source"
            )

    parameters_depth = 0
    for index, token in enumerate(tokens):
        parameters_depth += range_changes[index]
        if token != "Reflect":
            continue
        if parameters_depth:
            reflect_shadowed = True
        if index > 0 and tokens[index - 1] in {
            "var",
            "let",
            "const",
            "function",
            "class",
        }:
            reflect_shadowed = True
        if index + 1 < len(tokens) and tokens[index + 1] in {
            "=",
            "+=",
            "-=",
            "++",
            "--",
            "??=",
            "||=",
            "&&=",
        }:
            reflect_shadowed = True
        if (
            index + 3 < len(tokens)
            and tokens[index + 1] == "."
            and tokens[index + 3]
            in {"=", "+=", "-=", "++", "--", "??=", "||=", "&&="}
        ):
            reflect_shadowed = True
        if tokens[index + 1 : index + 2] == ["["]:
            close = delimiter_matches.get(index + 1)
            if close is not None and close + 1 < len(tokens):
                if tokens[close + 1] in {"=", "+=", "-=", "++", "--"}:
                    reflect_shadowed = True

    for opening, closing in delimiter_matches.items():
        if (
            tokens[opening] != "["
            or _static_key(opening + 1, closing) != "Reflect"
        ):
            continue
        receiver_end = opening - 1
        if (
            receiver_end >= 2
            and tokens[receiver_end] == "."
            and tokens[receiver_end - 1] == "?"
        ):
            receiver_end -= 2
        if receiver_end >= 0:
            receiver = _classify_target(
                _expression_start(receiver_end),
                receiver_end + 1,
            )[0]
            if receiver == "global":
                reflect_shadowed = True

    invocation_openings = {
        opening: closing
        for opening, closing in delimiter_matches.items()
        if tokens[opening] == "(" and _is_invocation(opening, closing)
    }
    invocation_closing_indexes = set(invocation_openings.values())
    call_arguments = _arguments_by_call(invocation_openings)
    relevant_reference_prefix = [0] * (len(tokens) + 1)
    loginid_token_prefix = [0] * (len(tokens) + 1)
    for index, token in enumerate(tokens):
        is_relevant_reference = isinstance(token, str) and (
            token in global_aliases or token == "vars"
        )
        if is_relevant_reference:
            previous = tokens[index - 1] if index > 0 else None
            following = tokens[index + 1] if index + 1 < len(tokens) else None
            if previous in {"var", "let", "const", "function", "class"} or (
                previous == "."
                and not (
                    index >= 3
                    and tokens[index - 1]
                    == tokens[index - 2]
                    == tokens[index - 3]
                    == "."
                )
            ):
                is_relevant_reference = False
            elif following == ":" and previous in {"{", ","}:
                is_relevant_reference = False
            elif following == "(":
                closing = delimiter_matches.get(index + 1)
                if (
                    closing is not None
                    and closing + 1 < len(tokens)
                    and tokens[closing + 1] == "{"
                ):
                    is_relevant_reference = False
        relevant_reference_prefix[index + 1] = (
            relevant_reference_prefix[index] + int(is_relevant_reference)
        )
        loginid_token_prefix[index + 1] = (
            loginid_token_prefix[index] + int(token == "loginid")
        )

    def _contains_relevant_reference(start: int, end: int) -> bool:
        return relevant_reference_prefix[end] > relevant_reference_prefix[start]

    def _contains_loginid_token(start: int, end: int) -> bool:
        return loginid_token_prefix[end] > loginid_token_prefix[start]

    closing_to_opening = {")": "(", "]": "[", "}": "{"}
    token_contexts = [-1] * len(tokens)
    container_stack: list[int] = []
    for index, token in enumerate(tokens):
        if token in closing_to_opening:
            token_contexts[index] = (
                container_stack[-1] if container_stack else -1
            )
            opening = closing_matches.get(index)
            if container_stack and container_stack[-1] == opening:
                container_stack.pop()
            continue
        token_contexts[index] = container_stack[-1] if container_stack else -1
        if token in {"(", "{", "["}:
            container_stack.append(index)

    next_boundaries = [len(tokens)] * len(tokens)
    latest_boundary_by_context: dict[int, int] = {}
    for index in range(len(tokens) - 1, -1, -1):
        context = token_contexts[index]
        next_boundaries[index] = latest_boundary_by_context.get(
            context,
            len(tokens),
        )
        token = tokens[index]
        if token in {",", ";"} or (
            token in closing_to_opening and closing_matches.get(index) == context
        ):
            latest_boundary_by_context[context] = index

    def _is_relevant_assignment_target(start: int, end: int) -> bool:
        target_kind, _ = _classify_target(start, end)
        if target_kind in {"vars", "vars_member"}:
            return True
        start, end = _unwrap_parentheses(start, end)
        if start < end and tokens[end - 1] in {"]", "}"}:
            pattern_opening = closing_matches.get(end - 1)
            if (
                pattern_opening is not None
                and tokens[pattern_opening] in {"[", "{"}
            ):
                previous = (
                    tokens[pattern_opening - 1]
                    if pattern_opening > 0
                    else None
                )
                if previous in {"(", "{", ",", ";", ":", "=", ">"}:
                    start = pattern_opening
        if start >= end:
            return False
        if end - start == 1 and tokens[start] == "loginid":
            return True
        if tokens[start] in {"{", "["} and _contains_loginid_token(
            start,
            end,
        ):
            return True
        member = _member_access(start, end)
        if member is None:
            return _contains_relevant_reference(start, end)
        receiver_start, receiver_end, selector = member
        receiver_kind, _ = _classify_target(receiver_start, receiver_end)
        if receiver_kind in {"vars", "vars_member"}:
            return True
        if selector is None or selector in {"vars", "loginid"}:
            return receiver_kind != "local"
        return False

    def _is_global_vars_assignment_target(start: int, end: int) -> bool:
        target_kind, _ = _classify_target(start, end)
        if target_kind not in {"vars", "vars_member"}:
            return False
        current_start, current_end = _unwrap_parentheses(start, end)
        while current_start < current_end:
            member = _member_access(current_start, current_end)
            if member is None:
                return False
            receiver_start, receiver_end, selector = member
            if (
                selector == "vars"
                and _global_alias_reference(receiver_start, receiver_end)
            ):
                return True
            current_start, current_end = _unwrap_parentheses(
                receiver_start,
                receiver_end,
            )
        return False

    def _is_member_expression_opening(opening: int) -> bool:
        if opening <= 0:
            return False
        previous = tokens[opening - 1]
        if previous in {")", "]", ".", "?"}:
            return True
        return (
            isinstance(previous, str)
            and previous.isidentifier()
            and previous not in {"var", "let", "const", "of", "in"}
        )

    def _destructuring_pattern_bounds(
        start: int,
        end: int,
    ) -> tuple[int, int] | None:
        start, end = _unwrap_parentheses(start, end)
        if (
            start >= end
            or tokens[start] not in {"{", "["}
            or delimiter_matches.get(start) != end - 1
            or (tokens[start] == "[" and _is_member_expression_opening(start))
        ):
            return None
        return start, end

    def _assignment_target_range(operator_start: int) -> tuple[int, int]:
        target_end = operator_start
        last_target_index = operator_start - 1
        if last_target_index >= 0 and tokens[last_target_index] in {"]", "}"}:
            opening = closing_matches.get(last_target_index)
            if opening is not None and tokens[opening] == "{":
                return opening, target_end
            if (
                opening is not None
                and tokens[opening] == "["
                and not _is_member_expression_opening(opening)
            ):
                return opening, target_end
        return _expression_start(last_target_index), target_end

    def _top_level_pattern_token(
        start: int,
        end: int,
        sought: str,
    ) -> int | None:
        cursor = start
        while cursor < end:
            if tokens[cursor] in {"(", "{", "["}:
                closing = delimiter_matches.get(cursor)
                if closing is not None and closing < end:
                    cursor = closing + 1
                    continue
            if tokens[cursor] == sought:
                return cursor
            cursor += 1
        return None

    def _destructuring_target_ranges(
        pattern_start: int,
        pattern_end: int,
    ) -> list[tuple[int, int]]:
        targets: list[tuple[int, int]] = []
        pending = [(pattern_start, pattern_end, 0)]
        while pending:
            start, end, depth = pending.pop()
            bounds = _destructuring_pattern_bounds(start, end)
            if bounds is None:
                start, end = _unwrap_parentheses(start, end)
                if start < end:
                    targets.append((start, end))
                continue
            start, end = bounds
            if depth >= MAX_DESTRUCTURING_PATTERN_NESTING:
                raise SchedulerClientError(
                    "bootstrap assignment pattern is nested too deeply"
                )
            is_object_pattern = tokens[start] == "{"
            segment_start = start + 1
            inner_end = end - 1
            cursor = segment_start
            while cursor < inner_end:
                if tokens[cursor] in {"(", "{", "["}:
                    closing = delimiter_matches.get(cursor)
                    if closing is not None and closing < inner_end:
                        cursor = closing + 1
                        continue
                if tokens[cursor] == ",":
                    _append_destructuring_target(
                        segment_start,
                        cursor,
                        is_object_pattern,
                        depth,
                        pending,
                    )
                    segment_start = cursor + 1
                cursor += 1
            _append_destructuring_target(
                segment_start,
                inner_end,
                is_object_pattern,
                depth,
                pending,
            )
        return targets

    def _append_destructuring_target(
        entry_start: int,
        entry_end: int,
        is_object_pattern: bool,
        depth: int,
        pending: list[tuple[int, int, int]],
    ) -> None:
        if entry_start >= entry_end:
            return
        target_start = entry_start
        if is_object_pattern:
            colon = _top_level_pattern_token(entry_start, entry_end, ":")
            if colon is not None:
                target_start = colon + 1
        if (
            target_start + 2 < entry_end
            and tokens[target_start] == tokens[target_start + 1]
            == tokens[target_start + 2] == "."
        ):
            target_start += 3
        elif tokens[target_start] == "...":
            target_start += 1
        default_assignment = _top_level_pattern_token(
            target_start,
            entry_end,
            "=",
        )
        target_end = (
            default_assignment if default_assignment is not None else entry_end
        )
        if target_start < target_end:
            pending.append((target_start, target_end, depth + 1))

    pattern_shadowed_aliases: set[tuple[str, int | None]] = set()

    def _check_non_simple_write_target(start: int, end: int) -> None:
        target_kind, _ = _classify_target(start, end)
        if (
            _is_global_vars_assignment_target(start, end)
            or target_kind in {"vars", "vars_member"}
        ):
            raise SchedulerClientError(
                "bootstrap assignment pattern mutates relevant state"
            )
        alias = _global_alias_name(start, end)
        if alias is not None:
            identity_source = (
                identity_alias_sources.get(start)
                if end == start + 1
                else None
            )
            if identity_source is not None and _is_proven_global_alias_occurrence(
                identity_source[0],
                identity_source[1],
            ):
                return
            alias_scope = _binding_scope_for_alias(alias, start)
            if _mark_shadowed_alias(alias, start, alias_scope):
                pattern_shadowed_aliases.add((alias, alias_scope))
        elif _is_relevant_assignment_target(start, end):
            raise SchedulerClientError(
                "bootstrap assignment pattern mutates relevant state"
            )

    relevant_mutation_changes = [0] * (len(tokens) + 1)
    relevant_result_changes = [0] * (len(tokens) + 1)
    for index, token in enumerate(tokens):
        if token != "=":
            continue
        operator_start = _assignment_operator_start(index)
        if operator_start is None or operator_start == 0:
            continue
        target_start, target_end = _assignment_target_range(operator_start)
        pattern_bounds = _destructuring_pattern_bounds(
            target_start,
            target_end,
        )
        if pattern_bounds is not None:
            for pattern_target in _destructuring_target_ranges(*pattern_bounds):
                _check_non_simple_write_target(*pattern_target)
        elif _is_global_vars_assignment_target(target_start, target_end):
            raise SchedulerClientError(
                "bootstrap vars binding is mutated through a global receiver"
            )
        if not _is_relevant_assignment_target(target_start, target_end):
            continue
        relevant_mutation_changes[operator_start] += 1
        result_start = index + 1
        result_end = next_boundaries[index]
        if result_start < result_end:
            relevant_result_changes[result_start] += 1
            relevant_result_changes[result_end] -= 1
    for opening, closing in delimiter_matches.items():
        if (
            tokens[opening] != "("
            or opening == 0
            or (
                tokens[opening - 1] != "for"
                and not (
                    opening > 1
                    and tokens[opening - 1] == "await"
                    and tokens[opening - 2] == "for"
                )
            )
        ):
            continue
        separator = None
        for index in range(opening + 1, closing):
            if (
                tokens[index] in {"in", "of"}
                and token_contexts[index] == opening
                and (
                    index == opening + 1
                    or tokens[index - 1] not in {".", "?"}
                )
            ):
                separator = index
                break
        if separator is None:
            continue
        target_start = opening + 1
        if tokens[target_start] in {"var", "let", "const"}:
            target_start += 1
        if target_start >= separator:
            continue
        pattern_bounds = _destructuring_pattern_bounds(
            target_start,
            separator,
        )
        if pattern_bounds is None:
            _check_non_simple_write_target(target_start, separator)
        else:
            for pattern_target in _destructuring_target_ranges(*pattern_bounds):
                _check_non_simple_write_target(*pattern_target)

    _propagate_shadowed_aliases(pattern_shadowed_aliases)
    for index in range(len(tokens) - 1):
        token = tokens[index]
        if token not in {"+", "-"} or tokens[index + 1] != token:
            continue
        if index > 0:
            target_start = _expression_start(index - 1)
            if _is_global_vars_assignment_target(target_start, index):
                raise SchedulerClientError(
                    "bootstrap vars binding is mutated through a global receiver"
                )
            if _is_relevant_assignment_target(target_start, index):
                relevant_mutation_changes[index] += 1
        target_start = index + 2
        if target_start < len(tokens):
            target_end = _expression_end(target_start)
            if _is_global_vars_assignment_target(target_start, target_end):
                raise SchedulerClientError(
                    "bootstrap vars binding is mutated through a global receiver"
                )
            if _is_relevant_assignment_target(target_start, target_end):
                relevant_mutation_changes[index] += 1

    relevant_mutation_prefix = [0] * (len(tokens) + 1)
    for index, change in enumerate(relevant_mutation_changes[:-1]):
        relevant_mutation_prefix[index + 1] = (
            relevant_mutation_prefix[index] + change
        )

    def _contains_relevant_mutation(start: int, end: int) -> bool:
        return relevant_mutation_prefix[end] > relevant_mutation_prefix[start]


    relevant_result_active = [False] * len(tokens)
    active_relevant_result_ranges = 0
    for index in range(len(tokens)):
        active_relevant_result_ranges += relevant_result_changes[index]
        relevant_result_active[index] = active_relevant_result_ranges > 0


    def _is_function_expression(start: int, end: int) -> bool:
        start, end = _unwrap_parentheses(start, end)
        if start >= end:
            return False
        if tokens[start] == "async":
            start += 1
        if start >= end:
            return False
        if tokens[start] == "function":
            return True
        arrow = _top_level_pattern_token(start, end, "=")
        return (
            arrow is not None
            and arrow + 1 < end
            and tokens[arrow + 1] == ">"
        )

    def _call_follows(index: int) -> bool:
        return tokens[index + 1 : index + 2] == ["("] or tokens[
            index + 1 : index + 4
        ] == ["?", ".", "("]

    def _is_member_key(index: int) -> bool:
        previous = tokens[index - 1] if index > 0 else None
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        if previous in {"{", ","} and following == ":":
            return True
        if following != "(":
            return False
        closing = delimiter_matches.get(index + 1)
        return (
            previous in {"{", ","}
            and closing is not None
            and closing + 1 < len(tokens)
            and tokens[closing + 1] == "{"
        )

    def _is_global_alias_value_reference(index: int) -> bool:
        token = tokens[index]
        if (
            not isinstance(token, str)
            or not token.isidentifier()
            or token not in global_aliases
            or not _is_proven_global_alias_occurrence(token, index)
        ):
            return False
        previous = tokens[index - 1] if index > 0 else None
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        if (
            previous == "."
            or (following == ":" and previous in {"{", ","})
            or _is_member_key(index)
            or previous in {"const", "let", "var", "function", "class", "catch"}
            or _assignment_operator_start(index + 1) == index + 1
        ):
            return False
        return True

    def _skip_to_pattern_comma(index: int, limit: int) -> int:
        while index < limit:
            if tokens[index] in {"(", "[", "{"}:
                closing = delimiter_matches.get(index)
                if closing is None:
                    return limit
                index = closing + 1
            elif tokens[index] == ",":
                break
            else:
                index += 1
        return index

    def _reject_dynamic_selector_pattern(
        opening: int,
        closing: int,
    ) -> None:
        pending_patterns = [(opening, closing, 0, True)]
        while pending_patterns:
            pattern_opening, pattern_closing, depth, identity_path = (
                pending_patterns.pop()
            )
            if depth > MAX_DESTRUCTURING_PATTERN_NESTING:
                raise SchedulerClientError(
                    "bootstrap destructuring pattern is nested too deeply"
                )
            cursor = pattern_opening + 1
            if tokens[pattern_opening] == "{":
                while cursor < pattern_closing:
                    if tokens[cursor] == ",":
                        cursor += 1
                        continue
                    if tokens[cursor : cursor + 3] == [".", ".", "."]:
                        raise SchedulerClientError(
                            "bootstrap destructures an unclassified global object"
                        )
                    if tokens[cursor] == "[":
                        key_closing = delimiter_matches.get(cursor)
                        if key_closing is None or key_closing >= pattern_closing:
                            raise SchedulerClientError(
                                "bootstrap has an invalid global destructuring key"
                            )
                        selector = _static_key(cursor + 1, key_closing)
                        if (
                            selector is None
                            or selector in _BOOTSTRAP_UNSAFE_GLOBAL_SELECTORS
                            or (identity_path and selector == "vars")
                        ):
                            raise SchedulerClientError(
                                "bootstrap destructures a dynamic global selector"
                            )
                        cursor = key_closing + 1
                    else:
                        property_token = tokens[cursor]
                        selector = None
                        if (
                            isinstance(property_token, str)
                            and property_token.isidentifier()
                        ):
                            selector = property_token
                        elif (
                            isinstance(property_token, tuple)
                            and property_token[0] in {"string", "template"}
                        ):
                            if property_token[2]:
                                raise SchedulerClientError(
                                    "bootstrap uses an escaped global selector"
                                )
                            selector = property_token[1]
                        if (
                            selector in _BOOTSTRAP_UNSAFE_GLOBAL_SELECTORS
                            or (identity_path and selector == "vars")
                        ):
                            raise SchedulerClientError(
                                "bootstrap destructures a global code-execution selector"
                            )
                        cursor += 1
                    if (
                        cursor < pattern_closing
                        and tokens[cursor] == ":"
                    ):
                        cursor += 1
                        if cursor >= pattern_closing:
                            raise SchedulerClientError(
                                "bootstrap has an incomplete global destructuring target"
                            )
                        if tokens[cursor] in {"{", "["}:
                            target_closing = delimiter_matches.get(cursor)
                            if (
                                target_closing is None
                                or target_closing >= pattern_closing
                            ):
                                raise SchedulerClientError(
                                    "bootstrap has an invalid global destructuring target"
                                )
                            pending_patterns.append(
                                (
                                    cursor,
                                    target_closing,
                                    depth + 1,
                                    identity_path
                                    and selector
                                    in _BOOTSTRAP_GLOBAL_IDENTITY_SELECTORS,
                                )
                            )
                            cursor = target_closing + 1
                        else:
                            cursor += 1
                        if (
                            cursor < pattern_closing
                            and tokens[cursor] == "="
                        ):
                            cursor = _skip_to_pattern_comma(
                                cursor + 1,
                                pattern_closing,
                            )
                    elif (
                        cursor < pattern_closing
                        and tokens[cursor] == "="
                    ):
                        cursor = _skip_to_pattern_comma(
                            cursor + 1,
                            pattern_closing,
                        )
                    elif (
                        cursor < pattern_closing
                        and tokens[cursor] != ","
                    ):
                        raise SchedulerClientError(
                            "bootstrap has an unsupported global destructuring property"
                        )
                    if (
                        cursor < pattern_closing
                        and tokens[cursor] == ","
                    ):
                        cursor += 1
            else:
                while cursor < pattern_closing:
                    if tokens[cursor] == ",":
                        cursor += 1
                        continue
                    if tokens[cursor : cursor + 3] == [".", ".", "."]:
                        cursor += 3
                    target = tokens[cursor] if cursor < pattern_closing else None
                    if target in {"{", "["}:
                        target_closing = delimiter_matches.get(cursor)
                        if (
                            target_closing is None
                            or target_closing >= pattern_closing
                        ):
                            raise SchedulerClientError(
                                "bootstrap has an invalid nested global pattern"
                            )
                        pending_patterns.append(
                            (cursor, target_closing, depth + 1, False)
                            )
                        cursor = target_closing + 1
                    else:
                        cursor += 1
                    if (
                        cursor < pattern_closing
                        and tokens[cursor] == "="
                    ):
                        cursor = _skip_to_pattern_comma(
                            cursor + 1,
                            pattern_closing,
                        )
                    if (
                        cursor < pattern_closing
                        and tokens[cursor] == ","
                    ):
                        cursor += 1
                    elif cursor < pattern_closing:
                        raise SchedulerClientError(
                            "bootstrap has an unsupported global array pattern"
                        )

    global_alias_value_prefix = [0] * (len(tokens) + 1)
    global_source_parameters_depth = 0
    for index in range(len(tokens)):
        global_source_parameters_depth += range_changes[index]
        is_global_alias_value = (
            global_source_parameters_depth == 0
            and _is_global_alias_value_reference(index)
        )
        global_alias_value_prefix[index + 1] = (
            global_alias_value_prefix[index] + is_global_alias_value
        )

    for pattern_start, pattern_closing in parameter_ranges:
        pattern_opening = pattern_start - 1
        if (
            pattern_opening < 0
            or tokens[pattern_opening] != "{"
            or pattern_closing + 2 >= len(tokens)
            or tokens[pattern_closing + 1] != "="
        ):
            continue
        source_start = pattern_closing + 2
        try:
            source_end = _concise_arrow_body_end(source_start)
        except SchedulerClientError:
            continue
        source_start, source_end = _unwrap_parentheses(source_start, source_end)
        source_name = (
            tokens[source_start]
            if source_end - source_start == 1
            else None
        )
        if (
            isinstance(source_name, str)
            and source_name in global_aliases
            and _is_proven_global_alias_occurrence(source_name, source_start)
        ):
            _reject_dynamic_selector_pattern(
                pattern_opening,
                pattern_closing,
            )
        elif (
            global_alias_value_prefix[source_end]
            > global_alias_value_prefix[source_start]
        ):
            _reject_dynamic_selector_pattern(
                pattern_opening,
                pattern_closing,
            )
            if _global_identity_pattern_targets(
                pattern_opening,
                pattern_closing,
            ):
                raise SchedulerClientError(
                    "bootstrap derives a global identity from a compound source"
                )

    for index, token in enumerate(tokens):
        if token == "]":
            member = _member_access(_expression_start(index), index + 1)
            if member is not None:
                receiver_start, receiver_end, selector = member
                if selector == "constructor":
                    raise SchedulerClientError(
                        "bootstrap accesses a dynamic Function constructor"
                    )
                source_reference = _global_alias_reference(
                    receiver_start,
                    receiver_end,
                )
                if (
                    source_reference is not None
                    and _is_proven_global_alias_occurrence(*source_reference)
                ):
                    if selector is None:
                        raise SchedulerClientError(
                            "bootstrap uses a dynamic global member selector"
                        )
                    if selector in _BOOTSTRAP_CODE_EXECUTION_SELECTORS:
                        raise SchedulerClientError(
                            "bootstrap accesses a global code-execution entry point"
                        )
                    if (
                        selector in _BOOTSTRAP_TIMER_SELECTORS
                        and not _call_follows(index)
                    ):
                        raise SchedulerClientError(
                            "bootstrap transfers a global timer callable"
                        )
            continue
        if not isinstance(token, str):
            continue
        is_member = False
        if (
            token in _BOOTSTRAP_CODE_EXECUTION_SELECTORS
            or token in _BOOTSTRAP_TIMER_SELECTORS
            or token == "constructor"
        ) and index > 0 and tokens[index - 1] == ".":
            member = _member_access(_expression_start(index), index + 1)
            if member is not None and member[2] == token:
                is_member = True
                if token == "constructor":
                    raise SchedulerClientError(
                        "bootstrap accesses a dynamic Function constructor"
                    )
                receiver_start, receiver_end, _ = member
                source_reference = _global_alias_reference(
                    receiver_start,
                    receiver_end,
                )
                if (
                    source_reference is not None
                    and _is_proven_global_alias_occurrence(*source_reference)
                ):
                    if token in _BOOTSTRAP_CODE_EXECUTION_SELECTORS:
                        raise SchedulerClientError(
                            "bootstrap accesses a global code-execution entry point"
                        )
                    if (
                        token in _BOOTSTRAP_TIMER_SELECTORS
                        and not _call_follows(index)
                    ):
                        raise SchedulerClientError(
                            "bootstrap transfers a global timer callable"
                        )
        if token == "constructor" and not is_member and not _is_member_key(index):
            if index + 1 < len(tokens) and tokens[index + 1] in {
                "(",
                ".",
                "[",
                "?",
            }:
                raise SchedulerClientError(
                    "bootstrap accesses a dynamic Function constructor"
                )
        if token in _BOOTSTRAP_CODE_EXECUTION_SELECTORS:
            previous = tokens[index - 1] if index > 0 else None
            is_declaration_name = previous in {
                "function",
                "class",
                "var",
                "let",
                "const",
            }
            if (
                index not in parameter_name_indexes
                and not is_member
                and not is_declaration_name
                and not _is_member_key(index)
                and not _is_lexically_bound(token, index)
            ):
                raise SchedulerClientError(
                    "bootstrap uses a direct code-execution entry point"
                )
        if token in _BOOTSTRAP_TIMER_SELECTORS and not is_member:
            previous = tokens[index - 1] if index > 0 else None
            if (
                index not in parameter_name_indexes
                and previous not in {"function", "class", "var", "let", "const"}
                and not _is_member_key(index)
                and not _is_lexically_bound(token, index)
                and not _call_follows(index)
            ):
                raise SchedulerClientError(
                    "bootstrap transfers a direct timer callable"
                )


    harmless_vars_references: set[int] = set()
    for opening, arguments in call_arguments.items():
        if relevant_result_active[opening]:
            raise SchedulerClientError(
                "bootstrap call result flows into relevant state"
            )
        is_reflect_set = (
            opening >= 3
            and tokens[opening - 3] == "Reflect"
            and tokens[opening - 2] == "."
            and tokens[opening - 1] == "set"
            and (opening < 4 or tokens[opening - 4] not in {".", "?"})
            and not reflect_shadowed
        )
        if is_reflect_set:
            if any(
                _contains_relevant_mutation(start, end)
                for start, end in arguments
            ):
                raise SchedulerClientError(
                    "bootstrap call argument mutates relevant state"
                )
            if len(arguments) < 2:
                if any(
                    end - start >= 3
                    and tokens[start] == tokens[start + 1] == tokens[start + 2] == "."
                    for start, end in arguments
                ):
                    raise SchedulerClientError(
                        "bootstrap Reflect.set uses unsupported spread arguments"
                    )
                continue
            target_kind, target_reference = _classify_target(*arguments[0])
            key = _static_key(*arguments[1])
            target_start, target_end = arguments[0]
            key_start, key_end = arguments[1]
            target_has_spread = (
                target_end - target_start >= 3
                and tokens[target_start]
                == tokens[target_start + 1]
                == tokens[target_start + 2]
                == "."
            )
            key_has_spread = (
                key_end - key_start >= 3
                and tokens[key_start]
                == tokens[key_start + 1]
                == tokens[key_start + 2]
                == "."
            )
            if target_kind != "local" and (
                target_has_spread
                or key_has_spread
                or target_kind == "vars_member"
                or key is None
                or (target_kind == "global" and key == "vars")
                or (target_kind == "vars" and key == "loginid")
                or (
                    target_kind == "unknown"
                    and key in {"vars", "loginid"}
                )
            ):
                raise SchedulerClientError(
                    "bootstrap Reflect.set may mutate relevant state"
                )
            if (
                target_kind == "vars"
                and target_reference is not None
                and key != "loginid"
            ):
                harmless_vars_references.add(target_reference)
            continue

        receiver_kind, receiver_start, receiver_end, selector, is_member = (
            _callee_details(opening)
        )
        callee_end = opening
        if opening >= 2 and tokens[opening - 2 : opening] == ["?", "."]:
            callee_end -= 2
        callee_start = (
            _expression_start(callee_end - 1) if callee_end > 0 else 0
        )
        callee_start, callee_end = _unwrap_parentheses(callee_start, callee_end)
        direct_timer = (
            callee_end - callee_start == 1
            and tokens[callee_start] in _BOOTSTRAP_TIMER_SELECTORS
            and not _is_lexically_bound(tokens[callee_start], callee_start)
        )
        global_timer_member = False
        if (
            receiver_kind == "global"
            and is_member
            and selector in _BOOTSTRAP_TIMER_SELECTORS
        ):
            source_reference = _global_alias_reference(
                receiver_start,
                receiver_end,
            )
            global_timer_member = (
                source_reference is not None
                and _is_proven_global_alias_occurrence(*source_reference)
            )
        if direct_timer or global_timer_member:
            if not arguments or not _is_function_expression(*arguments[0]):
                raise SchedulerClientError(
                    "bootstrap timer call has a dynamic callback"
                )
        if receiver_kind in {"vars", "vars_member"}:
            raise SchedulerClientError(
                "bootstrap call uses a relevant receiver"
            )
        if receiver_kind == "global":
            source_reference = _global_alias_reference(
                receiver_start,
                receiver_end,
            )
            if (
                not is_member
                or selector is None
                or selector in {"vars", "loginid"}
                or source_reference is None
                or not _is_proven_global_alias_occurrence(*source_reference)
            ):
                raise SchedulerClientError(
                    "bootstrap call uses an unclassified global member"
                )
        elif receiver_start >= 0 and _contains_relevant_reference(
            receiver_start,
            receiver_end,
        ):
            raise SchedulerClientError(
                "bootstrap call uses a relevant receiver"
            )
        for start, end in arguments:
            if _contains_relevant_mutation(start, end):
                raise SchedulerClientError(
                    "bootstrap call argument mutates relevant state"
                )
            if _contains_relevant_reference(start, end):
                raise SchedulerClientError(
                    "bootstrap call receives a relevant object"
                )
    return harmless_vars_references, invocation_closing_indexes



def extract_bootstrap_id(source: str) -> str:
    """Reject automatic bootstrap discovery from untrusted JavaScript."""
    del source
    raise SchedulerConfigurationError("automatic_bootstrap_discovery_disabled")


def _extract_bootstrap_id_legacy_compatibility_probe(source: str) -> str:
    """Extract the sole effective direct vars.loginid string from JavaScript."""
    tokens = _tokenize_javascript(source)
    brace_matches = _matching_braces(tokens)
    vars_objects: list[tuple[int, int]] = []
    declaration_names: set[int] = set()
    local_object_names: set[str] = set()
    global_aliases = {"window", "globalThis", "self", "frames", "top", "parent", "this"}
    global_alias_dependents: dict[str, set[str]] = {}
    delimiter_matches: dict[int, int] = {}
    delimiters: list[tuple[str, int]] = []
    closing_to_opening = {")": "(", "]": "[", "}": "{"}
    for index, token in enumerate(tokens):
        if (
            token in {"var", "let", "const"}
            and index + 3 < len(tokens)
            and isinstance(tokens[index + 1], str)
            and tokens[index + 1].isidentifier()
            and tokens[index + 2] == "="
        ):
            name = tokens[index + 1]
            initializer_index = index + 3
            initializer = tokens[initializer_index]
            if initializer == "{":
                if token == "const" and name != "vars":
                    local_object_names.add(name)
            else:
                value_index = initializer_index
                if (
                    initializer == "("
                    and value_index + 2 < len(tokens)
                    and tokens[value_index + 2] == ")"
                ):
                    value_index += 1
                    value_end = value_index + 2
                else:
                    value_end = value_index + 1
                value = tokens[value_index] if value_index < len(tokens) else None
                if (
                    isinstance(value, str)
                    and value.isidentifier()
                    and (
                        value_end >= len(tokens)
                        or tokens[value_end] in {",", ";", "}"}
                    )
                ):
                    global_alias_dependents.setdefault(value, set()).add(name)
        alias_source = _simple_alias_assignment_source(tokens, index)
        if alias_source is not None:
            global_alias_dependents.setdefault(alias_source, set()).add(token)

        if (
            not delimiters
            and token in {"var", "let", "const"}
            and index + 1 < len(tokens)
            and tokens[index + 1] == "vars"
        ):
            if (
                token != "var"
                or index + 3 >= len(tokens)
                or tokens[index + 2] != "="
                or tokens[index + 3] != "{"
            ):
                raise SchedulerClientError(
                    "bootstrap vars declaration is unsupported"
                )
            object_start = index + 3
            object_end = brace_matches.get(object_start)
            if object_end is None:
                raise SchedulerClientError("bootstrap vars object is not closed")
            vars_objects.append((object_start, object_end))
            declaration_names.add(index + 1)

        if token in {"{", "(", "["}:
            delimiters.append((token, index))
        elif token in closing_to_opening:
            if not delimiters or delimiters[-1][0] != closing_to_opening[token]:
                raise SchedulerClientError("bootstrap JavaScript has unbalanced delimiters")
            _, opening_index = delimiters.pop()
            delimiter_matches[opening_index] = index
    if delimiters:
        raise SchedulerClientError("bootstrap JavaScript has unbalanced delimiters")
    pending_aliases = list(global_aliases)
    while pending_aliases:
        source_name = pending_aliases.pop()
        for alias_name in global_alias_dependents.get(source_name, ()):
            if alias_name not in global_aliases:
                global_aliases.add(alias_name)
                pending_aliases.append(alias_name)
    closing_matches = {
        close_index: open_index
        for open_index, close_index in delimiter_matches.items()
    }
    harmless_vars_references, invocation_closing_indexes = (
        _reject_unsupported_bootstrap_calls(
            tokens,
            delimiter_matches,
            closing_matches,
            global_aliases,
            local_object_names,
        )
    )


    # Explicit property keys are inert; bare values and shorthand bindings
    # still refer to the `vars` binding.
    for index, token in enumerate(tokens):
        if (
            token != "vars"
            or index in declaration_names
            or index in harmless_vars_references
        ):
            continue
        is_property_name = (
            index > 0
            and index + 1 < len(tokens)
            and tokens[index - 1] in {"{", ","}
            and tokens[index + 1] == ":"
        )
        if not is_property_name:
            raise SchedulerClientError(
                "bootstrap vars binding is referenced or mutated"
            )

    # A computed member can hide a global `vars` replacement. Accept only a
    # single inert literal key known not to name that binding.

    computed_receiver_scope_cache: dict[int, str] = {}

    def _computed_member_receiver_scope(receiver_index: int) -> str:
        visited: list[int] = []
        while receiver_index >= 0:
            receiver = tokens[receiver_index]
            # A property result is not proven to share its base's scope.
            if (
                isinstance(receiver, str)
                and receiver.isidentifier()
                and receiver_index > 0
                and tokens[receiver_index - 1] == "."
            ):
                scope = "unknown"
                break
            cached_scope = computed_receiver_scope_cache.get(receiver_index)
            if cached_scope is not None:
                scope = cached_scope
                break
            visited.append(receiver_index)
            if isinstance(receiver, str) and receiver.isidentifier():
                if receiver in global_aliases:
                    scope = "global"
                    break
                if receiver in local_object_names:
                    scope = "local"
                    break
                scope = "unknown"
                break
            if receiver == "]":
                scope = "unknown"
                break
            if receiver == ")":
                opening_index = closing_matches.get(receiver_index)
                if opening_index is None:
                    scope = "unknown"
                    break
                # A call result is not scoped like its last argument.
                if receiver_index in invocation_closing_indexes:
                    scope = "unknown"
                    break
                inner_start = opening_index + 1
                inner_end = receiver_index - 1
                if inner_start == inner_end:
                    receiver_index = inner_start
                    continue
                # Grouping preserves local scope only for a full identifier.
                if (
                    inner_start < inner_end
                    and tokens[inner_start] == "("
                    and tokens[inner_end] == ")"
                    and delimiter_matches.get(inner_start) == inner_end
                ):
                    receiver_index = inner_end
                    continue
                scope = "unknown"
                break
            scope = "unknown"
            break
        else:
            scope = "unknown"
        for visited_index in visited:
            computed_receiver_scope_cache[visited_index] = scope
        return scope

    for open_index, close_index in delimiter_matches.items():
        if tokens[open_index] != "[":
            continue
        if (
            open_index > 0
            and tokens[open_index - 1] in {"{", ","}
            and close_index + 1 < len(tokens)
            and tokens[close_index + 1] == ":"
        ):
            continue
        receiver_index = open_index - 1
        if (
            receiver_index >= 2
            and tokens[receiver_index] == "."
            and tokens[receiver_index - 1] == "?"
        ):
            receiver_index -= 2
        elif receiver_index >= 0 and tokens[receiver_index] == "?":
            receiver_index -= 1
        if receiver_index < 0 or not (
            isinstance(tokens[receiver_index], str)
            and tokens[receiver_index].isidentifier()
            or tokens[receiver_index] in {")", "]"}
        ):
            continue
        selector = (
            tokens[open_index + 1]
            if close_index == open_index + 2
            else None
        )
        static_key = (
            selector[1]
            if isinstance(selector, tuple)
            and selector[0] in {"string", "template"}
            and not selector[2]
            else None
        )
        if static_key is not None and static_key != "vars":
            continue
        if _computed_member_receiver_scope(receiver_index) == "local":
            continue
        raise SchedulerClientError(
            "bootstrap vars binding uses unsupported computed access"
        )
    valid_candidates: list[tuple[str, tuple[int, int]]] = []

    def _has_unsupported_loginid_method(
        member_index: int,
        object_end: int,
    ) -> bool:
        key_index = member_index
        prefix = tokens[key_index]
        if prefix in {"get", "set", "async", "*"}:
            key_index += 1
            if (
                prefix == "async"
                and key_index < object_end
                and tokens[key_index] == "*"
            ):
                key_index += 1
        if key_index >= object_end:
            return False

        key_token = tokens[key_index]
        if key_token == "[":
            key_close = delimiter_matches.get(key_index)
            if key_close is None or key_close >= object_end:
                return False
            key_value: str | None = None
            if key_close == key_index + 2:
                literal = tokens[key_index + 1]
                if (
                    isinstance(literal, tuple)
                    and literal[0] in {"string", "template"}
                    and not literal[2]
                ):
                    key_value = literal[1]
            after_key = key_close + 1
        else:
            if isinstance(key_token, str) and key_token.isidentifier():
                key_value = key_token
            elif (
                isinstance(key_token, tuple)
                and key_token[0] == "string"
                and not key_token[2]
            ):
                key_value = key_token[1]
            else:
                key_value = None
            after_key = key_index + 1

        if after_key >= object_end or tokens[after_key] != "(":
            return False
        parameters_close = delimiter_matches.get(after_key)
        if (
            parameters_close is None
            or parameters_close + 1 >= object_end
            or tokens[parameters_close + 1] != "{"
        ):
            return False
        return key_value is None or key_value == "loginid"

    for object_start, object_end in vars_objects:
        login_id: str | None = None
        nested_braces = 1
        paren_depth = bracket_depth = 0
        for index in range(object_start + 1, object_end):
            token = tokens[index]
            member_start = tokens[index - 1] in {"{", ","}
            if (
                nested_braces == 1
                and paren_depth == 0
                and bracket_depth == 0
                and member_start
                and _has_unsupported_loginid_method(index, object_end)
            ):
                raise SchedulerClientError(
                    "bootstrap loginid property uses an unsupported method"
                )
            if (
                nested_braces > 0
                and paren_depth == 0
                and bracket_depth == 0
                and token == "["
                and member_start
            ):
                raise SchedulerClientError(
                    "bootstrap vars object uses an unsupported computed property"
                )
            if (
                nested_braces == 1
                and paren_depth == 0
                and bracket_depth == 0
                and token == "."
                and tokens[index - 1] in {"{", ","}
                and index + 2 < len(tokens)
                and tokens[index + 1] == tokens[index + 2] == "."
            ):
                raise SchedulerClientError(
                    "bootstrap vars object uses an unsupported spread property"
                )
            has_colon = index + 1 < object_end and tokens[index + 1] == ":"
            is_escaped_property_key = (
                isinstance(token, tuple)
                and token[0] == "string"
                and token[2]
                and member_start
                and has_colon
            )
            if (
                nested_braces == 1
                and paren_depth == 0
                and bracket_depth == 0
                and is_escaped_property_key
            ):
                raise SchedulerClientError(
                    "bootstrap vars object uses an escaped property name"
                )

            is_quoted_loginid = (
                isinstance(token, tuple)
                and token[0] == "string"
                and token[1] == "loginid"
            )
            if (
                nested_braces == 1
                and paren_depth == 0
                and bracket_depth == 0
                and (token == "loginid" or is_quoted_loginid)
            ):
                if is_quoted_loginid and (member_start or has_colon):
                    raise SchedulerClientError(
                        "bootstrap loginid property must use identifier syntax"
                    )
                if member_start and not has_colon:
                    raise SchedulerClientError("bootstrap loginid property is malformed")
                if has_colon:
                    if not member_start:
                        raise SchedulerClientError("bootstrap loginid property is malformed")
                    if login_id is not None:
                        raise SchedulerClientError(
                            "bootstrap JavaScript has ambiguous loginid properties"
                        )
                    value_index = index + 2
                    value = tokens[value_index] if value_index < object_end else None
                    if not (isinstance(value, tuple) and value[0] == "string"):
                        raise SchedulerClientError("bootstrap loginid is not a string literal")
                    if value[2]:
                        raise SchedulerClientError(
                            "bootstrap loginid string uses unsupported escapes"
                        )
                    if (
                        value_index + 1 > object_end
                        or tokens[value_index + 1] not in {",", "}"}
                    ):
                        raise SchedulerClientError(
                            "bootstrap loginid is not a direct string literal"
                        )
                    candidate = value[1]
                    if not BOOTSTRAP_ID_RE.fullmatch(candidate):
                        raise SchedulerClientError("bootstrap loginid failed validation")
                    login_id = candidate

            if token == "{":
                nested_braces += 1
            elif token == "}":
                nested_braces -= 1
            elif token == "(":
                paren_depth += 1
            elif token == ")":
                paren_depth -= 1
            elif token == "[":
                bracket_depth += 1
            elif token == "]":
                bracket_depth -= 1
        if login_id is not None:
            valid_candidates.append((login_id, (object_start, object_end)))

    if (
        len(valid_candidates) != 1
        or not vars_objects
        or valid_candidates[0][1] != vars_objects[-1]
    ):
        raise SchedulerClientError(
            "bootstrap JavaScript has an unexpected bootstrap candidate count"
        )
    return valid_candidates[0][0]


def _tokenize_javascript(source: str) -> _JavaScriptTokenStream:
    """Tokenize JavaScript and expose template expressions to bootstrap analysis."""
    tokens = _JavaScriptTokenStream()
    frames: list[dict[str, Any]] = []
    template_depth = 0
    index = 0
    closing_to_opening = {")": "(", "]": "[", "}": "{"}
    line_terminator_pending = False

    def _append_token(token: Any) -> None:
        nonlocal line_terminator_pending
        if line_terminator_pending:
            tokens.line_terminators_before.add(len(tokens))
            line_terminator_pending = False
        tokens.append(token)

    while index < len(source):
        if frames and frames[-1]["kind"] == "template":
            frame = frames[-1]
            char = source[index]
            if char == "\\":
                frame["had_escape"] = True
                if index + 1 >= len(source):
                    raise SchedulerClientError(
                        "bootstrap JavaScript has an unterminated template"
                    )
                if not frame["interpolated"]:
                    frame["raw"].append(source[index : index + 2])
                index += 2
                continue
            if source.startswith("${", index):
                if not frame["interpolated"]:
                    _append_token(_INTERPOLATED_TEMPLATE_TOKEN)
                    frame["interpolated"] = True
                    frame["raw"].clear()
                _append_token(_TEMPLATE_BOUNDARY_TOKEN)
                _append_token("(")
                frames.append(
                    {
                        "kind": "interpolation",
                        "delimiters": [],
                        "expression_start": len(tokens),
                    }
                )
                index += 2
                continue
            if char == "`":
                if frame["interpolated"]:
                    _append_token(_INTERPOLATED_TEMPLATE_END_TOKEN)
                else:
                    _append_token(
                        ("template", "".join(frame["raw"]), frame["had_escape"])
                    )
                frames.pop()
                template_depth -= 1
                index += 1
                continue
            if not frame["interpolated"]:
                frame["raw"].append(char)
            index += 1
            continue

        interpolation = (
            frames[-1]
            if frames and frames[-1]["kind"] == "interpolation"
            else None
        )
        char = source[index]
        if source.startswith("//", index):
            index += 2
            while index < len(source) and source[index] not in "\r\n\u2028\u2029":
                index += 1
            if index < len(source):
                line_terminator_pending = True
                if (
                    source[index] == "\r"
                    and index + 1 < len(source)
                    and source[index + 1] == "\n"
                ):
                    index += 1
                index += 1
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            if end == -1:
                raise SchedulerClientError("bootstrap JavaScript has an unterminated comment")
            for comment_index in range(index + 2, end):
                if source[comment_index] in "\r\n\u2028\u2029":
                    line_terminator_pending = True
                    break
            index = end + 2
            continue
        if char.isspace():
            if char in "\r\n\u2028\u2029":
                line_terminator_pending = True
            index += 1
            continue
        if char in {"'", '"'}:
            quote_char = char
            index += 1
            value: list[str] = []
            had_escape = False
            while index < len(source):
                char = source[index]
                if char == "\\":
                    had_escape = True
                    if index + 1 >= len(source):
                        raise SchedulerClientError(
                            "bootstrap JavaScript has an unterminated string"
                        )
                    value.append(source[index + 1])
                    index += 2
                    continue
                if char == quote_char:
                    break
                if char in "\r\n":
                    raise SchedulerClientError("bootstrap JavaScript has an invalid string")
                value.append(char)
                index += 1
            if index >= len(source) or source[index] != quote_char:
                raise SchedulerClientError("bootstrap JavaScript has an unterminated string")
            _append_token(("string", "".join(value), had_escape))
            index += 1
            continue
        if char == "`":
            if template_depth >= MAX_TEMPLATE_NESTING:
                raise SchedulerClientError(
                    "bootstrap JavaScript template nesting is too deep"
                )
            template_depth += 1
            frames.append(
                {
                    "kind": "template",
                    "raw": [],
                    "had_escape": False,
                    "interpolated": False,
                }
            )
            index += 1
            continue
        if interpolation is not None and char == "\\":
            raise SchedulerClientError(
                "bootstrap JavaScript uses an unsupported escape in template interpolation"
            )
        if interpolation is not None and char == "/":
            raise SchedulerClientError(
                "bootstrap JavaScript uses an unsupported slash in template interpolation"
            )
        if interpolation is not None and char in {"{", "}", "(", ")", "[", "]"}:
            delimiters = interpolation["delimiters"]
            if char == "}" and not delimiters:
                if len(tokens) == interpolation["expression_start"]:
                    raise SchedulerClientError(
                        "bootstrap JavaScript has an empty template interpolation"
                    )
                frames.pop()
                _append_token(")")
                _append_token(_TEMPLATE_BOUNDARY_TOKEN)
                index += 1
                continue
            if char in {"{", "(", "["}:
                delimiters.append(char)
            else:
                if not delimiters or delimiters[-1] != closing_to_opening[char]:
                    raise SchedulerClientError(
                        "bootstrap JavaScript has unbalanced interpolation delimiters"
                    )
                delimiters.pop()
            _append_token(char)
            index += 1
            continue
        # Unicode escapes can alias IdentifierNames; inert literal regions ran first.
        if source.startswith("\\u", index):
            raise SchedulerClientError(
                "bootstrap JavaScript uses unsupported Unicode identifier escapes"
            )
        if char.isalpha() or char in "_$":
            end = index + 1
            while end < len(source) and (source[end].isalnum() or source[end] in "_$"):
                end += 1
            _append_token(source[index:end])
            index = end
            continue
        if char.isdigit():
            end = index + 1
            while end < len(source) and (source[end].isalnum() or source[end] in "._"):
                end += 1
            _append_token(source[index:end])
            index = end
            continue
        _append_token(char)
        index += 1

    if frames:
        frame = frames[-1]
        if frame["kind"] == "interpolation":
            if frame["delimiters"]:
                raise SchedulerClientError(
                    "bootstrap JavaScript has unbalanced interpolation delimiters"
                )
            raise SchedulerClientError(
                "bootstrap JavaScript has an unterminated template interpolation"
            )
        raise SchedulerClientError("bootstrap JavaScript has an unterminated template")
    return tokens


def _matching_braces(tokens: list[Any]) -> dict[int, int]:
    """Index every balanced brace pair in one linear pass."""
    openings: list[int] = []
    matches: dict[int, int] = {}
    for index, token in enumerate(tokens):
        if token == "{":
            openings.append(index)
        elif token == "}":
            if not openings:
                raise SchedulerClientError("bootstrap JavaScript has unbalanced delimiters")
            matches[openings.pop()] = index
    if openings:
        raise SchedulerClientError("bootstrap vars object is not closed")
    return matches


class SchedulerReadClient(Protocol):
    """Interface for an explicit authenticated, read-only scheduler adapter."""

    async def list_entries(self, from_epoch: int, to_epoch: int) -> list[SchedulerEntry]: ...

    async def get_detail(self, sch_index: int) -> SchedulerDetail: ...


class AiohttpSchedulerReadClient:
    """Perform one explicit authenticated scheduler read sequence at a time."""

    def __init__(
        self,
        session: Any,
        config: SchedulerClientConfig,
        *,
        ssl_context: ssl.SSLContext | None = None,
        bootstrap_id: str | None = None,
    ) -> None:
        self._session = session
        self._config = config
        self._ssl_context = ssl_context or ssl.create_default_context()
        self._timeout = aiohttp.ClientTimeout(total=config.timeout)
        resolved_bootstrap_id = normalize_scheduler_bootstrap_id(
            bootstrap_id if bootstrap_id is not None else config.bootstrap_id
        )
        if resolved_bootstrap_id is None:
            raise SchedulerConfigurationError("bootstrap_id_required")
        self._bootstrap_id = resolved_bootstrap_id
        self._sessid: str | None = None
        self._alevel: str | None = None

    async def list_entries(self, from_epoch: int, to_epoch: int) -> list[SchedulerEntry]:
        """Explicitly login if needed, then perform one read-only LIST."""
        await self._ensure_authenticated()
        cookies, headers = self._authenticated_read_context()
        payload = await self._request_json(
            cookies["Sessid"],
            build_list_request(
                scheduler_epoch_to_gateway_wire(from_epoch),
                scheduler_epoch_to_gateway_wire(to_epoch),
            ),
            cookies=cookies,
            headers=headers,
            stage=SchedulerAuthenticationStage.LIST,
        )
        return [
            replace(
                entry,
                start_epoch=scheduler_epoch_from_gateway_wire(entry.start_epoch),
            )
            for entry in parse_list_response(payload)
        ]

    async def get_detail(self, sch_index: int) -> SchedulerDetail:
        """Explicitly login if needed, then perform one read-only DETAIL."""
        await self._ensure_authenticated()
        cookies, headers = self._authenticated_read_context()
        payload = await self._request_json(
            cookies["Sessid"],
            build_detail_request(sch_index),
            cookies=cookies,
            headers=headers,
            stage=SchedulerAuthenticationStage.DETAIL,
        )
        detail = parse_detail_response(payload)
        recurrence = dict(detail.recurrence)
        if type(recurrence.get("rec_end_time")) is int and recurrence["rec_end_time"] > 0:
            recurrence["rec_end_time"] = scheduler_epoch_from_gateway_wire(
                recurrence["rec_end_time"]
            )
        return replace(
            detail,
            start_epoch=scheduler_epoch_from_gateway_wire(detail.start_epoch),
            recurrence=recurrence,
        )

    def _authenticated_read_context(
        self,
    ) -> tuple[dict[str, str], dict[str, str]]:
        sessid = self._sessid
        alevel = self._alevel
        if sessid is None or alevel is None:
            raise SchedulerProtocolError("scheduler authentication state is incomplete")
        return (
            {"Sessid": sessid, "ALevel": alevel},
            {"X-Requested-With": "XMLHttpRequest"},
        )

    def diagnostics(self) -> dict[str, object]:
        """Return authentication and TLS state without sensitive values."""
        return {
            "transport": "https",
            "redirects_allowed": False,
            "retry_count": 0,
            "timeout_seconds": self._config.timeout,
            "bootstrap_id_in_memory": self._bootstrap_id is not None,
            "sessid_in_memory": self._sessid is not None,
            **scheduler_tls_diagnostics(
                self._ssl_context,
                pixii_ca_compatibility=self._config.pixii_ca_compatibility,
            ),
        }


    async def _ensure_authenticated(self) -> str:
        if self._sessid is not None:
            return self._sessid
        if not self._config.username or not self._config.password:
            raise SchedulerClientError("scheduler credentials are not configured")
        response = await self._request_json(
            self._bootstrap_id,
            {
                "cmd": "adm",
                "func": "login",
                "name": self._config.username,
                "password": self._config.password,
            },
            stage=SchedulerAuthenticationStage.LOGIN,
        )
        if response.get("status") != "ok":
            raise SchedulerAuthenticationError(
                SchedulerAuthenticationReason.LOGIN_REJECTED,
                SchedulerAuthenticationStage.LOGIN,
            )
        sessid = response.get("sessid")
        if not isinstance(sessid, str) or not SESSION_ID_RE.fullmatch(sessid):
            raise SchedulerAuthenticationError(
                SchedulerAuthenticationReason.INVALID_SESSION_RESPONSE,
                SchedulerAuthenticationStage.LOGIN,
            )
        alevel = response.get("alevel")
        if (
            type(alevel) is not int
            or not MIN_AUTH_LEVEL <= alevel <= MAX_AUTH_LEVEL
        ):
            raise SchedulerProtocolError("scheduler login response alevel was invalid")
        self._sessid = sessid
        self._alevel = str(alevel)
        return sessid

    async def _request_json(
        self,
        prefix: str,
        payload: Mapping[str, object],
        *,
        cookies: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        stage: SchedulerAuthenticationStage,
    ) -> Mapping[str, Any]:
        if cookies is None and self._sessid is not None:
            cookies = {"Sessid": self._sessid}
        text = await self._request_text(
            f"/ajax?data={prefix}json:{quote(_json_text(payload), safe=_JSON_SAFE)}",
            cookies=cookies,
            headers=headers,
        )
        if any(marker in text for marker in AUTH_LOSS_MARKERS):
            self._sessid = None
            self._alevel = None
            raise SchedulerAuthenticationError(
                SchedulerAuthenticationReason.SESSION_NOT_AUTHORIZED,
                stage,
            )
        try:
            decoded = json.loads(text)
        except (TypeError, ValueError) as err:
            raise SchedulerProtocolError("scheduler response was not valid JSON") from err
        if not isinstance(decoded, Mapping):
            raise SchedulerProtocolError("scheduler response was not an object")
        return decoded

    async def _request_text(
        self,
        path: str,
        *,
        cookies: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> str:
        url = f"{self._config.base_url}{path}"
        try:
            async with self._session.get(
                url,
                allow_redirects=False,
                cookies=cookies,
                headers=headers,
                ssl=self._ssl_context,
                timeout=self._timeout,
            ) as response:
                status = response.status
                if 300 <= status < 400:
                    raise SchedulerClientError("scheduler HTTP redirect rejected")
                if not 200 <= status < 300:
                    raise SchedulerClientError(
                        "scheduler HTTP response was not successful"
                    )
                text = await _bounded_response_text(response)
        except SchedulerClientError:
            raise
        except (aiohttp.ClientError, TimeoutError):
            raise SchedulerClientError("scheduler HTTP request failed") from None
        return text

def _json_text(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class SanitizedFixtureSchedulerClient:
    """Test adapter that parses supplied response objects and performs no I/O."""

    def __init__(
        self,
        list_response: Mapping[str, object],
        detail_responses: Mapping[int, Mapping[str, object]],
    ):
        self._list_response = list_response
        self._detail_responses = detail_responses

    async def list_entries(self, from_epoch: int, to_epoch: int) -> list[SchedulerEntry]:
        del from_epoch, to_epoch
        return parse_list_response(self._list_response)

    async def get_detail(self, sch_index: int) -> SchedulerDetail:
        response = self._detail_responses.get(sch_index)
        if response is None:
            raise SchedulerProtocolError(f"No sanitized detail fixture for {sch_index}")
        return parse_detail_response(response)
