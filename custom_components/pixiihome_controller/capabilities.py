"""Documented and observed Pixii capability tracking."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .protocol.models import ParsedMqttMessage

# This catalog is deliberately narrower than the Gateway's whole protocol.  A
# command is eligible only when it is represented by one of our validated
# builders and its prerequisite telemetry families are present.
DOCUMENTED_COMMANDS: dict[str, frozenset[str]] = {
    "runningservice": frozenset({"get"}),
    "demandresponse": frozenset({"set"}),
    "targetsoc": frozenset({"set"}),
    "adaptivepeakshaving": frozenset({"set"}),
    "batterycalibration": frozenset({"set"}),
}
UNSUPPORTED_RESPONSE_CODES = frozenset({8})
NOT_PRESENT_RESPONSE_CODES = frozenset({9})
MAX_CAPABILITY_FAMILIES = 32
MAX_CAPABILITY_FIELDS_PER_FAMILY = 128
MAX_CAPABILITY_SERVICES = 32
MAX_CAPABILITY_METHODS_PER_SERVICE = 16
MAX_CAPABILITY_IDENTIFIER_LENGTH = 64


@dataclass(frozen=True, slots=True)
class CapabilityDecision:
    """Separate protocol approval from Gateway observations."""
    service: str
    method: str
    documented: bool
    observed: bool
    explicitly_unsupported: bool
    transiently_not_present: bool
    prerequisites_present: bool

    @property
    def permitted(self) -> bool:
        """Return whether the conservative pre-publish capability gate passes."""
        return (
            self.documented
            and not self.explicitly_unsupported
            and self.prerequisites_present
        )


@dataclass(slots=True)
class CapabilityRegistry:
    """Track runtime evidence without requiring it to bootstrap known commands."""

    families: set[str] = field(default_factory=set)
    fields: dict[str, set[str]] = field(default_factory=dict)
    command_methods: dict[str, set[str]] = field(default_factory=dict)
    unsupported_commands: set[tuple[str, str]] = field(default_factory=set)
    not_present_commands: set[tuple[str, str]] = field(default_factory=set)
    messages_seen: int = 0
    overflow_observations: int = 0

    def observe(self, message: ParsedMqttMessage) -> None:
        self.messages_seen += 1
        family = message.family
        if (
            not family
            or len(family) > MAX_CAPABILITY_IDENTIFIER_LENGTH
            or (
                family not in self.families
                and len(self.families) >= MAX_CAPABILITY_FAMILIES
            )
        ):
            self.overflow_observations += 1
            return
        self.families.add(family)
        if not isinstance(message.payload, dict):
            return
        observed_fields = self.fields.setdefault(family, set())
        for field_name in message.payload:
            if (
                not isinstance(field_name, str)
                or len(field_name) > MAX_CAPABILITY_IDENTIFIER_LENGTH
                or (
                    field_name not in observed_fields
                    and len(observed_fields) >= MAX_CAPABILITY_FIELDS_PER_FAMILY
                )
            ):
                self.overflow_observations += 1
                continue
            observed_fields.add(field_name)
        if family != "ack":
            return
        service = message.payload.get("service")
        method = message.payload.get("method")
        values = message.payload.get("values")
        if isinstance(values, dict):
            service = values.get("service", service)
            method = values.get("method", method)
        if not isinstance(service, str) or not isinstance(method, str):
            return
        service = service.strip().lower()
        method = method.strip().lower()
        if (
            not service
            or not method
            or len(service) > MAX_CAPABILITY_IDENTIFIER_LENGTH
            or len(method) > MAX_CAPABILITY_IDENTIFIER_LENGTH
            or (
                service not in self.command_methods
                and len(self.command_methods) >= MAX_CAPABILITY_SERVICES
            )
        ):
            self.overflow_observations += 1
            return
        observed_methods = self.command_methods.setdefault(service, set())
        if (
            method not in observed_methods
            and len(observed_methods) >= MAX_CAPABILITY_METHODS_PER_SERVICE
        ):
            self.overflow_observations += 1
            return
        observed_methods.add(method)
        response = message.payload.get("response")
        if isinstance(values, dict):
            response = values.get("response", response)
        if isinstance(response, int) and not isinstance(response, bool):
            command = (service, method)
            if response in UNSUPPORTED_RESPONSE_CODES:
                self.unsupported_commands.add(command)
            elif response in NOT_PRESENT_RESPONSE_CODES:
                self.not_present_commands.add(command)

    def supports_family(self, family: str) -> bool:
        return family in self.families

    def supports_field(self, family: str, field_name: str) -> bool:
        return field_name in self.fields.get(family, set())

    def command_capability(
        self,
        service: str,
        method: str,
        *,
        required_families: tuple[str, ...] = (),
    ) -> CapabilityDecision:
        """Evaluate catalog approval, observations, and prerequisites separately."""
        service = service.strip().lower()
        method = method.strip().lower()
        return CapabilityDecision(
            service=service,
            method=method,
            documented=method in DOCUMENTED_COMMANDS.get(service, frozenset()),
            observed=method in self.command_methods.get(service, set()),
            explicitly_unsupported=(service, method) in self.unsupported_commands,
            transiently_not_present=(service, method) in self.not_present_commands,
            prerequisites_present=all(self.supports_family(family) for family in required_families),
        )

    def supports_command(
        self,
        service: str,
        method: str,
        *,
        required_families: tuple[str, ...] = (),
    ) -> bool:
        """Return conservative eligibility, not merely positive ACK evidence."""
        return self.command_capability(
            service,
            method,
            required_families=required_families,
        ).permitted

    def snapshot(self) -> dict[str, Any]:
        return {
            "families": sorted(self.families),
            "fields": {family: sorted(fields) for family, fields in sorted(self.fields.items())},
            "documented_commands": {
                service: sorted(methods)
                for service, methods in sorted(DOCUMENTED_COMMANDS.items())
            },
            "observed_command_methods": {
                service: sorted(methods)
                for service, methods in sorted(self.command_methods.items())
            },
            "unsupported_command_methods": [
                {"service": service, "method": method}
                for service, method in sorted(self.unsupported_commands)
            ],
            "not_present_command_methods": [
                {"service": service, "method": method}
                for service, method in sorted(self.not_present_commands)
            ],
            "messages_seen": self.messages_seen,
            "overflow_observations": self.overflow_observations,
        }
