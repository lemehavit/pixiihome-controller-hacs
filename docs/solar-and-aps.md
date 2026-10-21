# Solar charging and adaptive peak shaving

[Guide contents](README.md) · [Previous: manual control](manual-control.md)

Both functions use finite native Adaptive Peak Shaving (APS, service 105) leases.
They are mutually exclusive: turn one off and verify cleanup before enabling the
other. They require scheduler/Gateway access, production authority, an authorized
automation policy and fresh safe battery/grid measurements.

## Automatic solar charging

### Purpose

Use surplus generation to charge the battery while keeping a small export buffer.
This controller sets maximum discharge to zero. It is not a solar forecast and
does not buy cheap grid electricity based on price.

### Configure

On the **Automatic solar charging** child device, set:

| Setting | How to choose it |
| --- | --- |
| `solar_export_start_threshold_kw` | Export magnitude that must be sustained before charging starts. Enter a positive magnitude; do not enter a negative meter value. |
| `solar_export_stop_threshold_kw` | Lower export threshold used while running. Keep it below the start threshold for useful hysteresis. |
| `solar_export_buffer_kw` | Small residual export target. For example, `0.2` means a target near `−200 W` grid flow, subject to regulation and measurement delay. |
| `solar_max_charge_power_kw` | Upper charge-power bound. The battery's current capability and policy ceiling can lower it. |
| `solar_max_soc_percent` | Usable-SOC ceiling for solar charging. It is directional: this controller never discharges to reach it. |
| `solar_start_debounce_seconds`, `solar_stop_debounce_seconds` | How long the respective surplus condition must persist. Increase to avoid reacting to brief clouds/load changes. |
| `solar_max_limit_change_kw` | Maximum native adjustment per regulation change. |
| `solar_update_interval_seconds`, `solar_lease_seconds` | Recheck interval and finite lease duration. Use the [number reference](entities.md#solar-charging-numbers) for bounds. |

Enable **Automatic solar charging** only after the policy and authority are
configured. Preparation checks native priority and can add APS below Scheduler
when it is absent from the active priority list. It does not acquire an unrelated
APS operation merely because service 105 is already running.

### Smart solar charging

Switch **Smart solar charging** lets the controller sell surplus during
expensive hours and charge later the same day, only when the solar forecast
safely reaches **Solar maximum SoC**. Status `deferring` means it is waiting.
See [Energy forecast](energy-forecast.md#smart-solar-charging).

### Observe and stop

Check **Solar charging status**. `waiting_surplus` is normal without sustained
export; `active` indicates the controller has a verified owned lease. Inspect
`reason`, measured/export values, `owned_message_id` and lease/write counters in
its attributes. `soc_limit` means the usable ceiling has been reached.

Renewals stop when prerequisites, ownership or priority no longer hold. Turning
the controller off attempts its own finite-lease cleanup; if a reliable stop
cannot be proved, lease expiry/reconciliation remains important. Verify output
and status rather than interpreting the switch alone as a physical stop.

**Example:** start threshold `1.0 kW`, stop threshold `0.5 kW`, export buffer
`0.2 kW` and debounce `60 s` mean brief export spikes are ignored. These values
illustrate how the controls interact; charge power and SOC must suit your site.

## Adaptive peak shaving

### Purpose

Keep grid power within a signed band by charging below a lower grid-power limit
and discharging above an upper limit. This is a power-threshold controller,
independent of electricity prices.

### Configure

On the **Adaptive peak shaving** child device:

| Setting | Meaning |
| --- | --- |
| `aps_export_limit_kw` | **APS charge below grid power**: lower signed grid-power limit. A negative value represents permitted export before charging responds. |
| `aps_import_limit_kw` | **APS discharge above grid power**: upper signed grid-power limit. A positive value represents permitted import before discharge responds. |
| `aps_max_charge_kw`, `aps_max_discharge_kw` | Independent power ceilings, additionally capped by policy and battery capability. |
| `aps_min_soc_percent`, `aps_max_soc_percent` | Usable-SOC bounds. At/below minimum, discharge is zero; at/above maximum, charge is zero. The opposite direction can still be permitted. |
| `aps_max_limit_change_kw` | Maximum native adjustment step. |
| `aps_lease_seconds`, `aps_update_interval_seconds` | Finite renewable lease and controller recheck interval. |

The lower limit must not exceed the upper limit; minimum SOC must be below
maximum SOC. All defaults/ranges are in the [APS number reference](entities.md#adaptive-peak-shaving-numbers).

**Example:** lower `−0.2 kW` and upper `2.0 kW` request charging when export
exceeds roughly 200 W and discharging when import exceeds roughly 2 kW. Set
power/SOC limits for the installation; thresholds are not instant-response guarantees.

Enable the **Adaptive peak shaving** switch after authorizing
`adaptivepeakshaving` in the policy. Preparation verifies priority below native
Scheduler and Demand Response, adding APS only in the supported missing-entry
case. An incompatible existing priority list blocks activation.

### Expected operation

The automatic controller deliberately uses `keep_active=false`. It protects its
limits while allowing lower-priority functions between them. It renews only its
own verified lease, yields to protected services and never treats a foreign APS
activation as its own.

Check **Adaptive peak shaving status**, `reason`, the signed limit attributes,
`priority_verified`, `owned_message_id` and `lease_seconds_remaining`. `active`
does not imply continuous charging/discharging: no response may be needed inside
the band. Use the controller switch to disable it; the separate one-shot Start
button uses a different duration contract described under [manual control](manual-control.md).

[← Manual control](manual-control.md) · [Next: load balancing →](load-balancing.md)
