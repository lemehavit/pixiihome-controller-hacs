# Entity and setting reference

[Guide contents](README.md) · [Previous: schedules and dashboards](schedules-and-dashboard.md)

This lists every entity definition exposed by the current platforms. Actual
entities depend on runtime mode and configured subsystems. Features without a
controller/observer may have no corresponding status entity. Names are English
UI labels; translated names can differ.

**Keys are not complete entity IDs.** Home Assistant derives IDs from names and
registry history. Find the actual ID on your device/entity page. Unique IDs
normally combine the Gateway serial and the key. Two important exceptions are
the APS switch's legacy `aps_keep_active` identity and the scheduler calendar's
`scheduler_calendar` identity (translation key `schedules`).

## Measurement and status sensors

| Key / displayed name | Unit | Function and intended use |
| --- | --- | --- |
| `raw_soc` — Raw Battery SoC | % | Native battery percentage. Compare with usable SOC and its reserve attributes rather than assuming both percentages have the same window. |
| `usable_soc` — Usable Battery SoC | % | Native usable SOC when valid, otherwise calculated from configured limits. Main SOC concept for automatic feature reserves. |
| `stored_energy` — Battery Stored Energy | kWh | Current stored-energy measurement; not a monotonically increasing energy-consumption counter. |
| `battery_power` — Battery Power | W | Positive charging, negative discharging. Observe actual response after an operation. |
| `battery_soh` — Battery State of Health | % | Gateway-reported battery health, distinct from current SOC. |
| `building_power` — Building Power | W | Gateway meter `building_ac_w`; view house/load demand in the site's meter arrangement. |
| `meter_power` — Grid/Meter Power | W | Signed grid flow, positive import and negative export. Verify meter placement/signs before relying on control. |
| `system_status` — System Status | Label | Normalized native system status. A normal label alone is not a full control permit. |
| `grid_status` — Grid Status | Label | Native grid state, used with battery/system/events checks. |
| `battery_status` — Battery Status | Label | Native battery readiness/status. |
| `current_service` — Current Service | Label | Current native battery service, such as idle, Demand Response or Scheduler. Does not identify who issued it. |
| `production_control` — Production Control Authority | enabled / disabled | Read-only view of persisted authority; the switch with this key is the control. |
| `authority_revision` — Authority Revision | Integer | Durable authority generation for diagnosing changes and invalidated previews. Not a firmware version. |
| `recovery_status` — Control Recovery Status | none / required | Whether journal transactions remain unresolved. `none` is not proof every native identity is absent or that no controller is blocked. |
| `controller_status` — Automatic Controller Status | Label | Simulator-only legacy arbitration result. Not created for production; use each production feature status instead. |

SOC attributes include `effective_min_soc`, `effective_max_soc`,
`reported_min_soc`, `reported_max_soc` and `usable_soc_source`. Missing/invalid
limits are not silently an unrestricted 0–100% operating window. Measurement
sensors become unavailable when their required telemetry family is stale;
some unavailable fields simply mean the firmware does not publish that value.

### Feature and scheduler sensors

| Key / displayed name | Function / what to inspect |
| --- | --- |
| `scheduler_inventory` — Scheduler Inventory | Number of schedules in the current inventory. Read `data_status`, authentication/freshness, schedule completeness and `schedules` attributes before using it as evidence. Requires a coordinator. |
| `load_balancing_observation` — Load balancing status | Observer result and combined diagnostic/control attributes. Inspect `control_status` and `control_blocked_reason` separately from observation state. |
| `charge_headroom` — Estimated charge headroom (symmetric) | W of calculated symmetric charging room. A proposal/estimate, not measured battery power or an executed request. |
| `required_discharge` — Estimated discharge required (symmetric) | W of calculated symmetric discharge assistance. Battery limits or phase asymmetry can prevent delivering it. |
| `phase_current_l1` — Building phase current L1 | A for the configured phase source; check correspondence with real phase L1. |
| `phase_current_l2` — Building phase current L2 | A for L2. Unavailable when only one phase is configured. |
| `phase_current_l3` — Building phase current L3 | A for L3. Unavailable when only one phase is configured. |
| `load_balancing_usable_soc` — Load balancing usable SoC | % using the observer's fresh battery evidence; useful when checking its discharge reserve. |
| `load_balancing_min_usable_soc` — Load balancing minimum usable SoC | % configured reserve from load-balancing options, not a second battery sensor. |
| `solar_charging_status` — Solar charging status | Controller state plus `reason`, surplus/limits, priority, ownership, lease and write evidence. See [solar guide](solar-and-aps.md#automatic-solar-charging). |
| `adaptive_peak_shaving_status` — Adaptive peak shaving status | Controller state; attributes include signed grid limits, SOC limits, `priority_verified`, `owned_message_id`, remaining lease and renewals. |
| `price_optimization_status` — Price optimization status | Plan/controller state; inspect `reason`, price-source format/interval count, plan/blockers, settings and created/deleted counters. |
| `price_next_charge` — Next price-optimized charge | Timestamp for current/next unexpired charge interval. Read `schedule_status`, power, start/end and local-time attributes. It may be a proposal. |
| `price_next_discharge` — Next price-optimized discharge | Same for discharge, including export/self-consumption mode. A currently running interval can have a past start timestamp. |
| `price_scheduled_count` — Created price schedules | Count of unexpired reconciled `scheduled` price intervals; includes `schedules` list. Does not count all proposals or lifetime operations. |

Load-balancing numeric readings require appropriate current observer status;
current measurements can remain visible while direction evidence is missing.
The reserve setting remains readable even when no complete envelope is available.
These differences are intentional and do not establish a safe write context.

## Connectivity binary sensor

`mqtt_connected` — **MQTT telemetry connected** is on when **any** of core,
battery or meter telemetry is fresh. It is unavailable before any message has
been seen. It does not prove all required families are fresh, scheduler login
works, or control is safe. In the simulator it represents simulated telemetry.

## Calendar entities

| Key / label | Function and intended use |
| --- | --- |
| `scheduler_calendar` — Schedules | Read-only normalized Gateway inventory for the requested time window. Requires scheduler access; includes external schedules without making them integration-owned. |
| `price_optimization_calendar` — Price optimization schedules | Read-only view of price proposals and owned records. Event labels distinguish Planförslag (planned), Skapat i Pixii (last reconciled) and Efterkontroll krävs (unverified). |

Neither calendar supports create/update/delete through standard calendar actions.
Use the explicit scheduler-card preview workflow where supported. Times are
displayed by Home Assistant; the card's local fields use Europe/Stockholm.

## Switches

All seven switches default to **off** and persist across restarts. Operations
are administrator-controlled; an on switch is not proof of active battery power.

| Key / label | Function and intended use |
| --- | --- |
| `production_control` — Production-control authority | Master production write permission. Enabling it alone starts no automatic controller. Turning it off is not a universal physical stop. |
| `automatic_solar_charging` — Automatic solar charging | Enable the surplus-only controller after configuring its settings/policy. Mutually exclusive with automatic APS. |
| `adaptive_peak_shaving` — Adaptive peak shaving | Enable the renewable APS controller. Its unique-ID suffix remains `aps_keep_active` for compatibility; this switch is not the native keep-active parameter. |
| `load_balancing` — Load balancing | Enable native control after configuring/enabling the observer separately. Entity meter source is not supported for current service-120 production writes. |
| `automatic_price_optimization` — Automatic price optimization | Run the price controller; authority/policy are required even for its no-schedule-write preview mode. |
| `price_schedule_writes_enabled` — Allow price optimizer to change schedules | Permit live price reconciliation. Off permits proposals after prerequisites, but does not prove old schedules were removed. |
| `price_export_allowed` — Allow price-optimized grid export | Allow export when the separate net-profit floor passes. Does not enable the controller or bypass any other gate. |

## Buttons

Set the relevant numbers **before** pressing a Start button. Production controls
require authority, authenticated administrator context and all operation checks.

| Key / label | Production? | Function |
| --- | --- | --- |
| `start_charging` — Start charging | Yes, guarded | Finite positive Demand Response using manual power and duration. |
| `start_discharging` — Start discharging | Yes, guarded | Finite negative Demand Response using manual power and duration. Does not itself impose zero export. |
| `start_target_soc` — Start target SoC | Yes, guarded | Finite native Target SOC using its own target and duration numbers. |
| `start_adaptive_peak_shaving` — Start adaptive peak shaving | Yes, guarded | One-shot APS using APS parameters and manual/APS duration; distinct from the automatic controller switch. |
| `stop_manual_power` — Stop manual power | Simulator only | Exercises simulated stop; no corresponding generic production stop entity. |
| `stop_target_soc` — Stop target SoC | Simulator only | Simulated target stop. |
| `stop_adaptive_peak_shaving` — Stop adaptive peak shaving | Simulator only | Simulated APS stop; automatic-controller owned cleanup is a separate path. |
| `start_battery_calibration` — Start battery calibration | Simulator only | Simulated calibration; not an endorsed production battery-maintenance operation. |

## Number settings

All **33 active number entities** are listed below. Values persist and are
validated. Accepted UI bounds are not recommendations or evidence of hardware
capacity. No number directly starts a command; active controllers can consume
new values on their next evaluation. Power limits also face policy, battery,
phase, service and ownership checks.

### Manual control numbers

[How to configure and use these controls](manual-control.md).

| Key / name | Default | Range; step | Unit | Function and use |
| --- | --- | --- | --- | --- |
| `manual_power_kw`<br>Manual power | 1 | 0–10; 0.1 | kW | Positive magnitude for the next manual charge/discharge. The Start button selects direction; changing the number alone does not start it. |
| `manual_power_duration_seconds`<br>Manual/APS duration | 3600 | 60–604800; 60 | s | Duration for manual fixed-power and one-shot APS starts. Automatic APS uses its own lease field. Choose a finite interval before starting. |
| `target_soc_percent`<br>Target SoC | 80 | 10–100; 1 | % | Native Target SOC reference used by Start target SoC. Distinct from usable-SOC limits used by automatic features. |
| `target_soc_duration_seconds`<br>Target SoC duration | 3600 | 60–604800; 60 | s | Maximum native target interval requested by Start target SoC; not a guarantee of reaching the target. |

### Adaptive peak shaving numbers

[How to configure and use these controls](solar-and-aps.md#adaptive-peak-shaving).

| Key / name | Default | Range; step | Unit | Function and use |
| --- | --- | --- | --- | --- |
| `aps_import_limit_kw`<br>APS discharge above grid power | 0 | -100–100; 0.1 | kW | Upper signed grid-power limit: discharge above this import threshold. Must be at least the lower charge threshold. |
| `aps_export_limit_kw`<br>APS charge below grid power | 0 | -100–100; 0.1 | kW | Lower signed grid-power limit: charge below this threshold. Negative values represent export. |
| `aps_max_charge_kw`<br>APS maximum charge | 10 | 0–100; 0.1 | kW | Maximum APS charging power, further limited by battery capability and policy. |
| `aps_max_discharge_kw`<br>APS maximum discharge | 10 | 0–100; 0.1 | kW | Maximum APS discharging power, further limited by battery capability and policy. |
| `aps_max_limit_change_kw`<br>APS maximum limit change | 1 | 0.1–100; 0.1 | kW | Maximum native APS limit adjustment step. Use to moderate regulation changes. |
| `aps_min_soc_percent`<br>APS minimum usable SoC | 10 | 0–100; 1 | % | Usable-SOC discharge reserve for automatic APS; discharge is zero at/below it. |
| `aps_max_soc_percent`<br>APS maximum usable SoC | 90 | 0–100; 1 | % | Usable-SOC charging ceiling for automatic APS; charge is zero at/above it. Minimum must be below maximum. |
| `aps_lease_seconds`<br>APS control lease | 120 | 30–900; 1 | s | Finite automatic APS interval, renewed only with valid ownership/evidence. Policy can shorten it. |
| `aps_update_interval_seconds`<br>APS control update interval | 10 | 5–60; 1 | s | Automatic APS recheck interval. Not the Gateway's internal regulation cycle. |

### Solar charging numbers

[How to configure and use these controls](solar-and-aps.md#automatic-solar-charging).

| Key / name | Default | Range; step | Unit | Function and use |
| --- | --- | --- | --- | --- |
| `solar_export_start_threshold_kw`<br>Solar start threshold | 1 | 0–50; 0.1 | kW | Positive export magnitude required to begin solar charging after start debounce. |
| `solar_export_stop_threshold_kw`<br>Solar stop threshold | 0.5 | 0–50; 0.1 | kW | Lower export threshold for stopping/ceasing renewal after stop debounce. Use below the start threshold. |
| `solar_max_charge_power_kw`<br>Solar maximum charge | 10 | 0–10; 0.1 | kW | Maximum solar charging power, additionally capped by battery and policy. Solar discharge remains zero. |
| `solar_export_buffer_kw`<br>Solar export buffer | 0.2 | 0–2; 0.1 | kW | Positive magnitude of small residual export target; 0.2 kW corresponds to about −200 W signed grid power. |
| `solar_max_limit_change_kw`<br>Solar maximum limit change | 1 | 0.1–10; 0.1 | kW | Maximum native solar APS adjustment step. |
| `solar_lease_seconds`<br>Solar control lease | 120 | 30–900; 1 | s | Finite solar-control lease renewed only while surplus, safety and ownership remain valid. |
| `solar_update_interval_seconds`<br>Solar control update interval | 10 | 5–60; 1 | s | How often solar conditions/renewals are evaluated. |
| `solar_max_soc_percent`<br>Solar maximum SoC | 90 | 0–100; 1 | % | Usable-SOC charging ceiling. Does not request discharge if SOC starts above the ceiling. |
| `solar_start_debounce_seconds`<br>Solar start debounce | 60 | 0–3600; 1 | s | Time sustained export must satisfy the start condition; filters brief surplus spikes. |
| `solar_stop_debounce_seconds`<br>Solar stop debounce | 60 | 0–3600; 1 | s | Time the stop condition must persist; filters brief changes before stopping/ending renewal. |

### Price optimization numbers

[How to configure and use these controls](price-optimization.md).

| Key / name | Default | Range; step | Unit | Function and use |
| --- | --- | --- | --- | --- |
| `price_min_difference_sek_per_kwh`<br>Minimum net profit for self-consumption | 0.5 | 0–20; 0.01 | SEK/kWh | Minimum net profit per delivered AC kWh for self-consumption, after import cost, losses and cycle cost. This is not a raw price spread. |
| `price_export_min_difference_sek_per_kwh`<br>Minimum net profit for export | 1 | 0–20; 0.01 | SEK/kWh | Separate minimum net profit for export. Only relevant when price export is allowed; failed export profitability can still leave self-use eligible. |
| `price_grid_import_cost_sek_per_kwh`<br>Grid cost per imported kWh | 0 | 0–20; 0.01 | SEK/kWh | Variable cost per imported kWh in addition to the price source. Avoid double-counting fees already included in that source. |
| `price_max_charge_kw`<br>Price optimization maximum charge | 10 | 0–100; 0.1 | kW | Price-plan charging ceiling. Policy, battery capability, available energy and conflicts can reduce or block it. |
| `price_max_discharge_kw`<br>Price optimization maximum discharge | 10 | 0–100; 0.1 | kW | Price-plan discharge ceiling, used for self-consumption or allowed export. |
| `price_min_soc_percent`<br>Price optimization minimum usable SoC | 10 | 0–100; 1 | % | Usable-SOC reserve for price discharge. A lower initial SOC can still allow charging. |
| `price_max_soc_percent`<br>Price optimization maximum usable SoC | 90 | 0–100; 1 | % | Usable-SOC ceiling for price charging. A higher initial SOC can still allow discharge; keep minimum below maximum. |
| `price_usable_capacity_kwh`<br>Usable battery capacity for price planning | 0 | 0–1000; 0.1 | kWh | Explicit usable energy capacity for price planning. Default zero blocks planning; it is not inferred from nameplate capacity. |
| `price_round_trip_efficiency_percent`<br>Battery round-trip efficiency | 90 | 50–100; 1 | % | Estimated AC round-trip efficiency for energy/profit calculations. Enter percent, for example 90 rather than 0.9. |
| `price_cycle_cost_sek_per_kwh`<br>Battery cycle cost per delivered kWh | 0 | 0–20; 0.01 | SEK/kWh | Estimated battery-wear cost per delivered AC kWh. Default zero omits this expense; it does not mean wear is free. |

### Retired settings and compatibility

The following keys are retained only for older simulator snapshots and are not
active production number entities. Use current feature settings/options instead:

- `load_balancing_adjustment_step_kw`
- `load_balancing_debounce_seconds`
- `load_balancing_max_charge_kw`
- `load_balancing_max_discharge_kw`
- `load_balancing_max_phase_current_a`
- `load_balancing_safety_margin_a`
- `load_balancing_update_interval_seconds`
- `planner_max_soc_percent`
- `planner_min_soc_percent`
- `planner_reserve_soc_percent`
- `solar_min_soc_percent`

The stored boolean `aps_keep_active` also has no standalone setting switch in
the current UI. The switch retaining that unique identity enables automatic APS,
whose native request explicitly uses `keep_active=false`.

[← Schedules and dashboards](schedules-and-dashboard.md) · [Next: actions and simulator →](actions-and-simulator.md)
