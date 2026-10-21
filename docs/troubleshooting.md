# Troubleshooting

[Guide contents](README.md) · [Previous: worked examples](examples.md)

## HACS cannot find the integration

Add the repository as **Integration**, not Dashboard. It is currently a custom
repository rather than part of HACS's default catalog. Restart Home Assistant
after downloading. For a manual install, verify that
`custom_components/pixiihome_controller/manifest.json` exists without an extra nested folder.

## MQTT is unavailable or telemetry is missing

Check Home Assistant's MQTT integration and broker connection. Confirm locally
that status topics contain the same Gateway serial you entered. Inspect message
freshness and whether the relevant family exists on your firmware. Sensor
unavailability is not solved by enabling production control.

## Scheduler is unavailable

Check the local URL, trusted bootstrap ID, credentials, CA filename and certificate
hostname. Confirm Home Assistant can reach the Gateway. Missing scheduler access
does not prevent MQTT monitoring. Do not disable TLS verification to work around it.

## A controller or operation is blocked

Inspect the controller's status and integration diagnostics. Common reasons are
off authority, missing/stale telemetry, an active external service, incomplete
schedule inventory, incompatible controllers or unresolved command ownership.
Wait for an external operation to finish; do not delete it to clear a blocker.
An uncertain command must be reconciled rather than retried blindly.

### Check the right layer

1. Check MQTT data, scheduler login and actual system/battery/grid state.
2. Check observer/source configuration and operating numbers.
3. Check authority, the full automation policy and the intended controller switch.
4. Check the feature's status **and attributes**. A normal observer state does not
   imply its control overlay is active; an enabled controller can be waiting.
5. Inspect existing schedules, ownership and recovery/reservation state before
   attempting another operation. Current Service does not identify its sender.

### Common status and reason codes

These codes appear in controller states, `reason`, `control_blocked_reason` or
returned action errors. They explain blockers rather than authorizing bypasses.

| Code / status | What it means | Intended next step |
| --- | --- | --- |
| `production_control_disabled`, `release_gate_closed` | Required production authority is not enabled. | Leave off for monitoring, or review [authority/policy](control-policy.md) before enabling the intended operation. |
| `automation_policy_not_authorized`, `price_automation_policy_not_authorized` | Policy disabled, incomplete or missing required services/classes/limits. | Review every field and increase revision when changing it. A switch does not create a policy. |
| `waiting_telemetry`, `plant_telemetry_not_safe`, `telemetry_stale` | Required fresh safe measurements are missing. | Verify each family; connectivity on only proves at least one is fresh. |
| `waiting_for_idle`, `waiting_for_priority`, `non_idle_service_active` | Another native operation or unverified state prevents this operation. | Inspect current service and schedules; wait for the conflicting actor. |
| `solar_conflicts_with_adaptive_peak_shaving`, `aps_conflicts_with_automatic_solar_charging` | Both controllers would own native APS. | Choose one and verify cleanup before switching modes. |
| `waiting_surplus` | No sustained solar surplus, or start debounce has not elapsed. | Compare thresholds/debounce with actual meter power. |
| `soc_limit` | Solar usable-SOC ceiling reached. | Expected waiting behavior; do not change a ceiling just to force charging. |
| `price_entity_required`, `price_entity_unavailable`, `price_entity_stale` | Price source absent, unavailable or outdated. | Check the selected entity and provider updates. |
| `price_series_empty`, `price_unit_unsupported`, `price_timestamp_timezone_required` | Attributes, units or timestamps are unsupported. | Compare with [supported price data](price-optimization.md#supported-price-data), not just the scalar state. |
| `future_price_horizon_incomplete` | Required future/day-ahead data is incomplete. | Wait for provider data; refresh frequency does not create prices. |
| `price_usable_capacity_required` | Explicit planning capacity is zero/missing. | Enter a reviewed positive usable kWh capacity. |
| `preview` / `price_schedule_writes_disabled` | Proposals available, live writing off. | Intended no-write price preview state. |
| `price_action_exceeds_policy_duration` | Proposed interval exceeds policy authorization. | Review plan and policy together; horizon hours are not an interval permit. |
| `price_scheduler_inventory_incomplete`, `price_recurring_schedule_scope_unknown` | Inventory/recurrence cannot prove a safe plan. | Refresh/read records and preserve unknown/external schedules. |
| `price_mutation_context_changed` | Source, mode, policy or settings changed during reconciliation. | Inspect the new state before a new evaluation. |
| `phase_direction_missing`, `meter_stale`, `meter_timestamps_inconsistent` | Phase sign/freshness evidence incomplete. | Review bindings, W/A/V units and timestamps. |
| `native_meter_required_for_service_120` | Native control requires the Pixii meter source. | Keep entity-source observation only, or configure supported native evidence. |
| `meter_limitation_in_use`, `phase_service_in_use` | Native configuration is already in use. | Preserve it rather than overwriting another actor. |
| `preview_state_changed`, `preview_missing_or_consumed` | Preview stale or already consumed. | Refresh, inspect state and create a new preview. |
| `control_recovery_required`, `recovery_pending`, `delivery_unknown` | Uncertain command outcome. | Reconcile exact identity/readback; do not replay an activation. |
| `price_stop_quarantine_active` | Output-stop/expiry does not prove identity reservation can end. | Inspect reconciliation evidence and keep the allocator/journal intact. |

### Settings do not appear where expected

Operational numbers and switches belong to logical child devices; technical
bindings are under Configure. Production excludes simulator-only generic
stop/calibration buttons and the legacy controller-status sensor. Retired
planner/load-balancing numbers are not a second effective production setup.
See the [complete entity reference](entities.md).

### Dashboard or calendar confusion

For an unknown custom card, inspect its registered JavaScript resource and
refresh the browser after a restart. Use the native-entity dashboard generator
to avoid guessing IDs. Price events can be proposals: inspect `schedule_status`
before assuming a command exists. Both calendars are read-only through standard
calendar actions.

## Report a reproducible issue

Use the GitHub bug template with integration and Home Assistant versions, Gateway
firmware, production/simulator mode, expected behavior and exact steps. Prefer
reproducing in the simulator. Review diagnostics before attaching them: remove
hostnames, addresses, serials, identifiers, credentials and private power/history
data. Never attach raw HAR files, keys, cookies, complete journals or MQTT captures.

The maintainer may ask for a minimal anonymized protocol example. Keep raw
installation reports locally. See [SECURITY.md](../SECURITY.md) for private reporting.

[← Worked examples](examples.md) · [Back to guide contents](README.md)
