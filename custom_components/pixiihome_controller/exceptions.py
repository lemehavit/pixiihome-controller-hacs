"""Integration-specific exceptions."""

from enum import StrEnum


class SchedulerAuthenticationReason(StrEnum):
    """Sanitized reasons for an explicit scheduler authentication failure."""

    SESSION_NOT_AUTHORIZED = "session_not_authorized"
    LOGIN_REJECTED = "login_rejected"
    INVALID_SESSION_RESPONSE = "invalid_session_response"


class SchedulerAuthenticationStage(StrEnum):
    """Sanitized operation stages for scheduler authentication failures."""

    LOGIN = "login"
    LIST = "list"
    DETAIL = "detail"


class PixiihomeControllerError(Exception):
    """Base integration error."""


class MqttUnavailable(PixiihomeControllerError):
    """Home Assistant's MQTT integration is unavailable."""


class MqttParseError(PixiihomeControllerError, ValueError):
    """An MQTT topic or payload cannot be parsed safely."""


class SchedulerProtocolError(PixiihomeControllerError, ValueError):
    """A sanitized scheduler request/response has an unexpected shape."""


class SchedulerClientError(PixiihomeControllerError):
    """A sanitized scheduler HTTP client failure."""


class SchedulerConfigurationError(SchedulerClientError):
    """A scheduler setting is invalid or cannot be activated safely."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class SchedulerAuthenticationError(SchedulerClientError):
    """The explicit scheduler login failed for a sanitized reason."""

    def __init__(
        self,
        reason: SchedulerAuthenticationReason,
        stage: SchedulerAuthenticationStage,
    ) -> None:
        if not isinstance(reason, SchedulerAuthenticationReason):
            raise TypeError("scheduler authentication reason must be typed")
        if not isinstance(stage, SchedulerAuthenticationStage):
            raise TypeError("scheduler authentication stage must be typed")
        self.reason = reason
        self.stage = stage
        super().__init__("scheduler authentication failed")



class CommandValidationError(PixiihomeControllerError, ValueError):
    """A command payload or integration-owned identifier is unsafe."""
