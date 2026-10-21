"""Pixii documented numeric mappings with unknown-value preservation."""

from __future__ import annotations

from typing import Any

STATUS_LABELS = {
    0: "Undefined",
    1: "Normal",
    2: "Info",
    3: "Warning",
    4: "Alarm",
    5: "Disabled",
    6: "Disconnected",
    7: "Active Injection",
    8: "Derated",
}

SERVICE_TYPE_LABELS = {
    0: "Idle",
    101: "Scheduler",
    102: "Demand Response",
    104: "Fixed Peak Shaving",
    105: "Adaptive Peak Shaving",
    107: "Target SoC",
    110: "Voltage Support",
    111: "Frequency Regulation FFR",
    115: "Off-Grid Mode",
    116: "Battery Calibration",
    118: "DRM Mode",
    119: "Power Control",
    121: "Frequency Regulation FCR-D Up",
    122: "Frequency Regulation FCR-N",
    123: "Frequency Regulation FCR-D Down",
}

RESPONSE_LABELS = {
    0: "response undefined",
    1: "access restrictions",
    2: "value too low",
    3: "value too high",
    4: "timeout",
    5: "faulty message format",
    6: "faulty message volume",
    8: "not implemented",
    9: "not present",
    10: "missing",
    11: "not configured",
    12: "no response",
    13: "below parameter range",
    14: "above parameter range",
    16: "outside parameter range",
    65535: "success",
}

SERVICE_STATUS_LABELS = {
    0: "undefined",
    1: "enabled",
    2: "idle",
    3: "idle, kept active",
    4: "idle, delayed",
    5: "running",
    6: "running, high state of charge",
    7: "running, low state of charge",
    8: "running, high voltage",
    9: "running, low voltage",
    10: "running, delayed",
    11: "running, delayed",
    12: "disabled",
}


def label(value: Any, labels: dict[int, str]) -> str:
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return f"Unknown ({value})"
    return labels.get(numeric, f"Unknown ({numeric})")


def status_label(value: Any) -> str:
    return label(value, STATUS_LABELS)


def service_type_label(value: Any) -> str:
    return label(value, SERVICE_TYPE_LABELS)
