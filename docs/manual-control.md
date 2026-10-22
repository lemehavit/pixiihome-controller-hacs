# Manual charging and target SOC

[Guide contents](README.md) · [Previous: authority and policy](control-policy.md)

## What this is for

Use manual controls for an explicitly requested, finite native operation. Use an
automatic controller for repeated behavior. The **Manual control** child device
contains the related numbers and buttons; [all defaults and ranges](entities.md#manual-control-numbers)
are listed separately.

## Fixed-power charge or discharge

1. Confirm the battery is available and that no external/support service conflicts.
2. Set **Manual power** (`manual_power_kw`) to the desired positive magnitude.
3. Set **Manual/APS duration** (`manual_power_duration_seconds`) to the finite
   duration. This field is in seconds, even if the UI presents a formatted value.
4. With production authority enabled, press **Start charging** or **Start discharging**.
5. Check Current Service, Battery Power and Control Recovery Status. An accepted
   request is not proof of reconciled operation; the typed path checks readback.

Charging sends positive Demand Response power; discharging sends negative power.
For example, a magnitude of `0.5 kW` requests `+500 W` when charging and `−500 W`
when discharging. **Manual discharge has no general no-export guarantee.** Zero
export is a distinct price self-consumption/peak-shaving behavior, not a property
of every discharge command.

## Target SOC

Set **Target SoC** (`target_soc_percent`) and **Target SoC duration**
(`target_soc_duration_seconds`), then press **Start target SoC**. This sends the
native Gateway `targetsoc` service and its `socref`, rather than a fixed-power
request. Do not assume a native target reference is interchangeable with the
integration's calculated usable-SOC feature limits. Gateway battery protection
and native service semantics still apply.

## One-shot APS versus automatic APS

**Start adaptive peak shaving** sends a finite native APS operation using the APS
power-band numbers and **Manual/APS duration**. It is different from the
**Adaptive peak shaving** switch, which manages renewable leases using
`aps_lease_seconds` and `aps_update_interval_seconds`.

The legacy stored `aps_keep_active` setting is not a separate public switch.
The automatic controller explicitly uses `keep_active=false`; the similarly
identified switch enables that controller. Use the
[automatic APS guide](solar-and-aps.md#adaptive-peak-shaving) for that workflow.

## Battery calibration

**Start battery calibration** sends Pixii's `batterycalibration` service
(Pixii MQTT documentation 13880 rev 1.7, section 4.2.5) with
`starttime: "now"`. The battery first discharges and then charges to
**Calibration endpoint SoC** (default 90 %). **Calibration duration** (default
2 hours) is the service's duration. Calibration is a prioritised service, so
the button refuses to start when:

- another service is running (the integration's own solar or APS lease is
  fine; it is paused),
- the Gateway schedule list is not current or does not cover the whole window,
- a recurring schedule exists, or
- any schedule lies inside the window. An aggregator Target SoC schedule is
  the only exception, and only when **Allow changing aggregator schedules** is
  on.

While the calibration runs, price optimization, solar charging and adaptive
peak shaving wait (reason `battery_calibration_running`). The end time is kept
across restarts. The logbook records the start and who pressed the button.

## Stop buttons and simulator differences

The generic **Stop manual power**, **Stop target SoC** and **Stop adaptive peak
shaving** buttons are **not created for production**. Their service-specific
production stop contracts are not verified. They exist only on the isolated
simulator surface.

Choose finite durations before starting a production operation. A controller's
exact owned-lease cleanup is a separate path and does not make a generic Stop
button available. Withdrawing authority or restarting Home Assistant is not
proof that the native operation stopped. Use your site's established Gateway
operating procedure if a real operation needs intervention.

The presence/availability of a Start button also does not guarantee execution:
authority, safe current telemetry, service conflicts, policy/adapter checks and
the durable journal can still reject it.

[← Authority and policy](control-policy.md) · [Next: solar and APS →](solar-and-aps.md)
