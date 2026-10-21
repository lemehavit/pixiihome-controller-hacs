# Control authority and automation policy

[Guide contents](README.md) · [Previous: configuration](configuration.md)

## Understand the gates

Monitoring and read-only inventory do not need production-control authority.
Real control needs the relevant gates below, an administrator caller, healthy
fresh measurements and an unambiguous command/ownership state.

| Gate | Default | Effect |
| --- | --- | --- |
| Production-control authority | Off | Allows guarded production writes. It does not start a controller by itself. |
| Automation policy | Disabled | Authorizes automatic operation classes, services, power/SOC limits and times. |
| Individual controller switch | Off | Starts the selected controller after its preparation checks. Preparation can itself configure native service settings. |
| Price schedule-write switch | Off | Allows the price controller to create/reconcile schedules. Leave off for proposals. |
| Price export switch | Off | Allows price-optimized discharge for export if its separate profit threshold passes. |

All these settings persist. A restart is not an off switch. Configuring a number
does not itself send its corresponding Start command, but changing it while an
automatic controller is enabled can affect subsequent renewals/plans.

## Automation policy fields

Open **Configure → Scheduler → Automation policy**. It is an advanced object field,
not a list of Home Assistant automation entity IDs. Every field below is required.
Unknown or invalid policy input fails closed.

| Field | Meaning and how to use it |
| --- | --- |
| `policy_id` | Nonempty name you choose for this site's policy. |
| `schema_version` | `1` for the current format. |
| `revision` | Nonnegative integer. Increase it whenever you change the policy; changed content with an unchanged/lower revision is rejected. |
| `enabled` | `false` blocks automatic control. Enable only after reviewing the whole policy and site configuration. |
| `allowed_operation_classes` | Include `automatic` for the automatic controllers. An empty list authorizes none. |
| `allowed_service_classes` | Exact service names. Price optimization requires both `demandresponse` and `peakshaving`; automatic APS requires `adaptivepeakshaving`. Only list services needed for your use case. |
| `time_window.start`, `time_window.end` | `HH:MM` in Europe/Stockholm. An overnight window belongs to the day it starts. Leases are clipped at the allowed boundary. |
| `time_window.days` | List of weekday numbers: Monday `0` through Sunday `6`. |
| `max_duration_seconds` | Maximum automatic interval/lease. Zero prevents operation; price actions longer than it are blocked. It is separate from the price planning horizon. |
| `soc_min_percent`, `soc_max_percent` | Global automatic SOC constraints, combined with feature-specific limits. Keep minimum below maximum for controllers requiring an operating window. |
| `max_power_kw` | Global automatic power ceiling. Zero prevents meaningful automatic power. Hardware and feature limits can be lower. |
| `external_control_allowed` | Policy permission involved in control-coexistence decisions. It does not establish ownership or replace the exact managed-source checks. Keep false unless intentionally configuring coexistence. |

### Example policy object

This example is deliberately **disabled** and illustrates the required format.
The numerical limits are examples, not suitable defaults for every battery.
Review the limits, required services and time window before enabling your own
policy. Enabling authority/controller switches is still a separate step.

```json
{
  "policy_id": "my-battery-policy",
  "schema_version": 1,
  "revision": 1,
  "enabled": false,
  "allowed_operation_classes": ["automatic"],
  "allowed_service_classes": ["adaptivepeakshaving", "demandresponse", "peakshaving"],
  "time_window": {"start": "00:00", "end": "23:59", "days": [0, 1, 2, 3, 4, 5, 6]},
  "max_duration_seconds": 3600,
  "soc_min_percent": 20,
  "soc_max_percent": 90,
  "max_power_kw": 2,
  "external_control_allowed": false
}
```

The shipped disabled policy uses revision `0`, empty allowlists and zero duration
and power. Merely enabling a feature switch does not fill in a valid policy.

## Start and stop sequence

1. Verify monitoring, scheduler access and site limits with all controllers off.
2. Configure the required policy and feature settings.
3. Enable production authority, then only the controller you intend to use.
4. Inspect the feature status and its `reason`/blocked-reason attributes. An enabled
   switch means requested operation, not proof that power is moving.
5. To stop automation, turn off its controller while communications and authority
   still allow its normal cleanup. Check the resulting native service/output and
   any remaining owned schedules before withdrawing authority.

Turning authority off blocks new writes; it is not a universal stop command and
does not prove an already-running native interval ended. Cleanup can itself
fail or require reconciliation. Do not bypass that state by editing storage or
replaying the same command.

## Controllers and coexistence

Automatic solar and automatic APS are mutually exclusive because they both own
service 105. Price operations wait for external active services; a verified
integration-owned background APS lease is a special supported case. Load
balancing uses a soft-limit overlay and, when appropriate, short idle-only phase
support; it is not permission to restart another service.

Preparation can inspect/configure native priority entries for the integration's
own supporting services. The implementation checks higher-priority Scheduler and
Demand Response precedence; this is not a guarantee that all Gateway firmware or
physical support-service interactions have been validated.

[← Configuration](configuration.md) · [Next: manual control →](manual-control.md)
