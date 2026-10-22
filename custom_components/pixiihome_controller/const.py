"""Constants for the Pixiihome Controller integration."""

from __future__ import annotations

DOMAIN = "pixiihome_controller"
NAME = "Pixiihome Controller"
VERSION = "0.5.9"
CONF_SERIAL = "serial"
CONF_RUNTIME_MODE = "runtime_mode"
CONF_COMMAND_STATE = "command_state"
CONF_CONTROL_JOURNAL_INITIALIZED = "control_journal_initialized"
# Switches remembered while a restart holds control closed for recovery.
RESUME_AFTER_RECOVERY = "resume_after_recovery"
CONF_ENTRY_ID = "config_entry_id"
CONF_SCHEDULER_OWNERSHIP_STATE = "scheduler_ownership_state"
CONF_SCHEDULER_ENABLED = "scheduler_enabled"
CONF_SCHEDULER_URL = "scheduler_url"
CONF_SCHEDULER_USERNAME = "scheduler_username"
CONF_SCHEDULER_PASSWORD = "scheduler_password"
CONF_SCHEDULER_BOOTSTRAP_ID = "scheduler_bootstrap_id"
CONF_SCHEDULER_TIMEOUT = "scheduler_timeout"
CONF_SCHEDULER_CA_COMPATIBILITY = "scheduler_ca_compatibility"
CONF_SCHEDULER_DAYS_BACK = "days_back"
CONF_SCHEDULER_DAYS_FORWARD = "days_forward"
CONF_SCHEDULER_INDEX = "sch_index"
CONF_EXPECTED_SERVICE_REFERENCE_DELAY = "expected_delay"
CONF_METER_LIMITATION_TRIAL_DIRECTION = "direction"
CONF_METER_LIMITATION_TRIAL_MAX_CORRECTION = "max_correction_w"
SCHEDULER_MAX_DAYS = 31
CONF_SCHEDULER_CA_FILENAME = "scheduler_ca_filename"
CONF_PRODUCTION_CONTROL_ENABLED = "production_control_enabled"
CONF_AUTOMATION_POLICY = "automation_policy"
CONF_CONTROLLER_STORE_VERSION = 1
SERVICE_GENERATE_DASHBOARD_CONFIG = "generate_dashboard_config"
SERVICE_QUERY_RUNNING_SERVICE = "query_running_service"
SERVICE_RUN_SCHEDULER_VALIDATION = "run_scheduler_validation_roundtrip"
SERVICE_REFRESH_SCHEDULER_LIST = "refresh_scheduler_list"
SERVICE_REFRESH_SCHEDULER_DETAIL = "refresh_scheduler_detail"
SERVICE_SET_SERVICE_REFERENCE_DELAY = "set_service_reference_delay"
SERVICE_RUN_METER_LIMITATION_SOFT_LIMIT_TRIAL = (
    "run_meter_limitation_soft_limit_trial"
)
PLATFORMS: tuple[str, ...] = (
    "sensor",
    "binary_sensor",
    "calendar",
    "number",
    "button",
    "switch",
)

STATUS_FAMILIES: tuple[str, ...] = (
    "core",
    "battery",
    "meter",
    "energy",
    "cabinet_env",
    "pfr",
    "batt_extended",
    "dc_pv",
)

# Both topic layouts are documented by Pixii. The current layout is preferred;
# the legacy layout is subscribed for compatibility with older Gateways.
TOPIC_TEMPLATES: tuple[str, ...] = (
    "pixii/status/{serial}/{family}",
    "pixii/{serial}/status/{family}",
)
ACK_TOPIC_TEMPLATE = "pixii/ack/{serial}"
CONTROL_TOPIC_TEMPLATE = "pixii/{serial}/control"
# Default-closed production safety gate. Persisted administrator authority opens the
# runtime gate only after the policy, evidence, ownership, journal, and readback checks.
LIVE_WRITES_ENABLED = False

STALE_AFTER_SECONDS = 180.0
