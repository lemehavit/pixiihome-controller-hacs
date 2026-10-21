# Actions, advanced controls and simulator

[Guide contents](README.md) · [Previous: entity reference](entities.md)

## Select an entry and read responses

Integration actions use domain `pixiihome_controller`. They accept optional
`config_entry_id`; supply it whenever several entries are loaded, including a
production entry alongside a simulator. It is a config-entry ID, not a serial,
device ID or entity ID.

All nine registered actions are **response-only**: inspect the returned status
and inner result instead of treating a returned call as a successful operation.
Actions with explicit administrator checks require an authenticated admin caller;
unattended calls without that context can be rejected.

## Read and presentation actions

| Action | Fields beyond optional `config_entry_id` | Function and intended use |
| --- | --- | --- |
| `query_running_service` | None | Administrator-only production MQTT GET of current service. Does not change power; a service reply does not prove command ownership. Unavailable on simulator entries. |
| `refresh_scheduler_list` | Required `days_back`, `days_forward`: integer 0–31 each | Read native scheduler inventory over a bounded UTC range. Inspect `status`, `entry_count`, authentication and errors. The action selector can show a larger range, but server validation limits each field to 31. |
| `refresh_scheduler_detail` | Required `sch_index`: nonnegative integer | Read one exact native scheduler record. Use its real inventory index; do not pass an MQTT ID. |
| `generate_dashboard_config` | None | Administrator-only response containing native-entity dashboard configuration. Does not create a dashboard or control the battery. |

Example, in **Developer tools → Actions**:

```yaml
action: pixiihome_controller.refresh_scheduler_list
data:
  config_entry_id: YOUR_CONFIG_ENTRY_ID
  days_back: 0
  days_forward: 2
```

This performs a read. It is not a battery availability permit, a deletion request
or a comprehensive ownership check.

## Specialist maintenance actions

These are documented for completeness, **not installation steps or routine
troubleshooting instructions**. They require a separately reviewed maintenance
window, appropriate site authority and their own prerequisites. Do not place
physical validation actions in unattended automations or retry uncertain calls.

| Action | Fields | What it does / boundary |
| --- | --- | --- |
| `run_scheduler_validation_roundtrip` | Optional `config_entry_id` only | Administrator-only production CREATE/readback/DELETE validation of one finite future Target SOC interval. Checks release/authority, safe state, fresh inventory and cleanup. It performs real writes, not a read-only connectivity test. |
| `set_service_reference_delay` | Required `expected_delay`: integer 0–60 s | Administrator-only installer-level change to the documented 10 s delay, after comparing the exact current value and preserving other settings. Not an automatic fix for a blocked controller. |
| `run_meter_limitation_soft_limit_trial` | Required `direction`: `exp` or `gen`; required `max_correction_w`: 50–300 W | Administrator-only production service-120 test, bounded to 20 s with hard limits disabled and restoration of the original record. Native `exp` and `gen` map to export/import respectively. Existing native settings must meet the trial's strict prerequisites. |
| `run_price_guard_validation` | Optional `duration_seconds`: exactly 40 (default) or 120 | Administrator-only physical 300 W zero-export test of an exact owned Peak Shaving interval and temporary identity-bound SOC stop; separate 300 W/25 s interval and 0 W/15 s Demand Response priority portion. The latter can be deferred. Not FCR/FFR validation; does not change saved SOC limits. |
| `recover_expired_price_validation` | Required `evidence` object; optional `dry_run`: true by default | Administrator-only archival recovery for an exact expired own validation with saved correlated GET/DELETE evidence and fresh checks. May save a conservative reservation to the journal when `dry_run=false`; sends no CREATE/SET/DELETE. Preserves old failed outcomes and allocator ownership. |

For the price trial, inspect the inner status, physical own-output samples,
`stop_confirmed`, cleanup/reservation and `priority_handoff`. No measured own
discharge means no successful discharge test. Zero output does not prove native
identity release. A complete historical report from another installation is
neither a safe recovery input nor authorization for your system.

The current [hardware limitations](../README.md#control-status-and-limitations)
remain in force even when an action reports an acknowledged command.

## Local simulator

Create a separate **Local Simulator** entry through Add integration. It uses
serial `SIMULATOR` internally and no MQTT/Gateway transport. Native entity
settings, simulated buttons, previews and the durable simulation state let you
explore behavior without moving physical power. It is never selected as an
automatic fallback when a production connection fails.

The simulator is a model, not a guarantee of real firmware timing, battery
limits, support-service behavior or native stop semantics. In particular,
simulator-only Stop/calibration buttons do not establish production support.

### Advanced scenario API

An authenticated administrator WebSocket client can use the following commands.
Always use the **simulator entry ID**. The scenario/clock/reset endpoints reject
production entries. These are WebSocket messages, not YAML action names.

| WebSocket type | Fields | Use |
| --- | --- | --- |
| `pixiihome_controller/panel/state` | None | Inspect available entries and their current state. |
| `pixiihome_controller/panel/scenario` | `entry_id`, `scenario` | Load one named local scenario below. Replaces the local scenario state. |
| `pixiihome_controller/panel/advance` | `entry_id`, `seconds`: integer 1–604800 | Advance the simulated clock; does not wait for real elapsed time. |
| `pixiihome_controller/panel/reset` | `entry_id` | Reset the selected local simulation. Does not reset a real Gateway. |
| `pixiihome_controller/panel/preview` | `entry_id`, typed `operation`; optional `service`, `target`, `intent` | Advanced typed operation preview; supported fields differ by operation. The card builds the relevant create requests. |
| `pixiihome_controller/panel/apply` | `entry_id`, server-issued `preview_id` | Consumes a reviewed preview once; does not accept arbitrary MQTT payloads. Re-preview after state changes. |
| `pixiihome_controller/panel/recover` | `entry_id`, `transaction_id` | Production-only readback of an eligible unresolved MQTT CREATE; does not resend the write. |
| `pixiihome_controller/gateway_scheduler/preview` | `entry_id`, `sch_index`, `operation`: `update` or `delete`; optional `changes` | Production native-scheduler preview. Changes can include `socref`, `start_time` and `duration`; scope and current state are validated. |
| `pixiihome_controller/gateway_scheduler/apply` | `entry_id`, server-issued `preview_id`, `confirm_external_target: true` | Applies the exact user-bound native-scheduler approval, including explicitly selected external records. This is a real write path. |
| `pixiihome_controller/gateway_scheduler/recover` | `entry_id`, `transaction_id` | Production native-scheduler outcome readback without resending the mutation. |

Available scenario names:

| Scenario | Demonstrates |
| --- | --- |
| `direct_target_soc` | Simulated native SOC target. |
| `direct_demand_response` | Signed simulated charge/discharge. |
| `scheduler_crud` | Create/read/update/delete workflow and exact identity. |
| `external_conflict` | Refusing a conflicting external schedule. |
| `deterministic_planner` | Deterministic plan and its typed apply path. |
| `recovery_required` | Unresolved transaction requiring reconciliation. |
| `partial_replacement` | Partial delete/create replacement and compensation state. |
| `timeout_delivery_unknown` | Uncertain delivery and why a blind retry is unsafe. |

For example, a WebSocket client can load a simulator scenario with this message
(substitute the ID of the **Local Simulator** entry):

```json
{
  "type": "pixiihome_controller/panel/scenario",
  "entry_id": "YOUR_SIMULATOR_CONFIG_ENTRY_ID",
  "scenario": "external_conflict"
}
```

No separate public switch enables the lower-level deterministic planner. It is
an advanced simulator/typed-preview surface, distinct from the automatic price
controller. Old planner number settings are retired, not an alternative way to
configure production price reserves.

[← Entity reference](entities.md) · [Next: worked examples →](examples.md)
