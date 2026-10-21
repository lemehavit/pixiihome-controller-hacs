# Worked examples

[Guide contents](README.md) · [Previous: actions and simulator](actions-and-simulator.md)

Values illustrate the UI and interactions. They are not recommended battery or
electrical limits for every site. Use actual entity IDs and your reviewed site
configuration. These examples do not run tests or modify a Gateway by themselves.

## 1. Monitoring-only installation

1. Configure MQTT and add the Gateway by serial.
2. Leave all seven switches off. Scheduler HTTPS is optional for basic telemetry.
3. Find the actual entity IDs in **Settings → Devices & services → Entities**.
4. Add a Manual dashboard card, replacing the IDs below:

```yaml
type: entities
title: Pixii monitoring
entities:
  - sensor.your_pixii_usable_battery_soc
  - sensor.your_pixii_battery_power
  - sensor.your_pixii_grid_meter_power
  - sensor.your_pixii_current_service
  - binary_sensor.your_pixii_mqtt_telemetry_connected
```

Expect positive battery power for charge, negative for discharge. Connectivity
can be on while one required telemetry family is stale; check individual sensor
availability. For a ready-made configuration with real registry IDs, use
`generate_dashboard_config` instead of guessing IDs.

## 2. Preview prices while preserving all existing schedules

Select a compatible price entity, enter an explicit usable capacity and review
efficiency/cost/power/SOC settings. Leave `managed_source_id` empty, external
authority false, price export off and price schedule writes off.

Configure a suitable policy and production authority, then enable only the price
controller. Preparation reads the Gateway; once ready it should show `preview`
with `price_schedule_writes_disabled`. Add the price calendar:

```yaml
type: calendar
entities:
  - calendar.your_price_optimization_schedules
```

Replace the calendar ID. A Planförslag event is an estimate, not a created native
schedule. The current controller plans with the remaining known prices and replans when
next-day prices arrive. It can still wait for an idle window,
complete inventory or usable capacity even with live writes off.

## 3. Interpret a solar-charging setup

Suppose you choose start `1.0 kW`, stop `0.5 kW`, buffer `0.2 kW`, both debounce
times `60 s`, and site-appropriate charge/SOC limits. After policy/authority and
the solar controller are enabled, sustained export can start charging. Short
cloud/load changes do not immediately toggle it. The native request aims to
leave a small export buffer and requests no discharge.

Watch Solar charging status plus actual battery/grid power. If the SOC ceiling
is reached, charging stops/its exact owned lease is cleaned up according to the
controller state. Do not enable automatic APS at the same time.

## 4. Interpret an APS band

With lower charge threshold `−0.2 kW` and upper discharge threshold `2.0 kW`,
the intended band is modest export through 2 kW import. Below the lower signed
grid limit the battery can charge; above the upper limit it can discharge,
within its SOC, power, policy and phase constraints.

Use the automatic switch for continued renewable protection. The one-shot Start
button uses **Manual/APS duration** and does not enable that automatic loop.

## 5. Check a fuse-margin calculation without enabling control

For an illustrative three-phase site with 16 A fuses and 2 A margin, the observer
works with a 14 A target envelope per phase, further constrained by voltage,
active/reactive current, battery evidence and supported control contracts. Enter
the site's actual values, enable the observer and keep the control switch off.

Compare currents with a known load. If signed phase power is absent,
`phase_direction_missing` is expected: current magnitude alone cannot determine
whether charging or discharging would help. Selecting Home Assistant meter
entities enables observation, not the current native service-120 control path.

## 6. Notify on a reconciliation problem

This example only creates a Home Assistant notification. Replace the recovery
sensor ID with yours before using it:

```yaml
alias: Pixii needs reconciliation
triggers:
  - trigger: state
    entity_id: sensor.your_pixii_control_recovery_status
    to: "required"
actions:
  - action: persistent_notification.create
    data:
      title: Pixii control needs review
      message: Inspect the controller status and journal outcome before further commands.
mode: single
```

Do not replace the notification action with automatic replay, storage cleanup or
a physical validation action. Other blocked/reserved states can exist even when
this particular sensor says `none`; also inspect feature status attributes.

[← Actions and simulator](actions-and-simulator.md) · [Next: troubleshooting →](troubleshooting.md)
