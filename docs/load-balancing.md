# Load balancing and phase measurements

[Guide contents](README.md) · [Previous: solar and APS](solar-and-aps.md)

## Purpose and two enable controls

Load balancing estimates phase headroom and can apply a native meter-limitation
soft-limit overlay. When no battery service is running, supported short service
129 leases can provide idle-only phase support. It does not restart an earlier
mode or extend an external command's timer.

The **Enabled** field in **Configure → Load balancing** enables the **observer**.
The separate **Load balancing** switch on the device page enables **control**,
after production authority, policy and native prerequisites pass. Observation can
therefore be enabled while control stays off.

These calculations and soft limits are not a substitute for electrical fuses,
protection or correct wiring. Verify meter placement, phase order and sign
conventions for the installation.

## Options reference

| Field | Default | Accepted input | Function / intended use |
| --- | --- | --- | --- |
| `enabled` | Off | Boolean | Run the observer. Does not itself enable the control switch. |
| `source` | `pixii` | `pixii` / `entities` | Use native Gateway meter fields or selected Home Assistant meter entities. Current production service-120 control requires `pixii`. |
| `phases` | 3 | 1 or 3 | Number of measured phases. Match the electrical installation. Availability of a native control contract is checked separately. |
| `fuse_l1`, `fuse_l2`, `fuse_l3` | 16 A each | 1–1000 A | Actual phase fuse ratings. Only configured phases are used. Accepted UI ranges are not hardware ratings. |
| `margin_a` | 2 A | 0–100 A, below every used fuse rating | Reserve current below the fuse rating for regulation/measurement uncertainty. |
| `min_soc` | 10% | 0–100% usable SOC | Reserve below which discharge assistance is unavailable. |
| `max_charge_kw`, `max_discharge_kw` | 10 kW each | 0–1000 kW | Bounds used for available assistance. Battery/hardware capabilities remain additional constraints. |
| `poll_seconds` | 5 s | Integer 2–60 s | Observer refresh period. |
| `stale_seconds` | 15 s | Integer 5–120 s, at least twice `poll_seconds` | Maximum permitted measurement age/response delay in the relevant checks. |
| `recovery_step_w` | 500 W | 10–10000 W | Recovery/ramp step used in phase-support planning. |

### Home Assistant meter bindings

When source is **Home Assistant** and the observer is enabled, the next step asks
for bindings below for each configured phase. Select distinct sensor entities.

| Fields | Required? | Units and use |
| --- | --- | --- |
| `current_l1`, `current_l2`, `current_l3` | Yes for each used phase | Current in A; used to assess fuse loading. |
| `voltage_l1`, `voltage_l2`, `voltage_l3` | Yes for each used phase | Voltage in V; used with current/power to calculate an envelope. |
| `power_l1`, `power_l2`, `power_l3` | Optional in the form | Signed active power in W, positive import/negative export. Without direction evidence, the observer can show currents but reports `phase_direction_missing` instead of authorizing an envelope. |

Units, freshness and inter-sensor timestamp consistency are validated. External
entity readings do not remove the need for Gateway access: the current observer
also reads native phase/battery/service evidence. Production control rejects the
entity source with `native_meter_required_for_service_120`.

## Configure observation first

1. Configure scheduler HTTPS access.
2. Enable the observer, select the meter source, set the actual phases/fuses and
   reserve margin, then save.
3. Keep the Load balancing control switch off. Check phase currents against the
   physical meter and known loads, including both import and export if available.
4. Inspect **Load balancing status**, **Estimated charge headroom (symmetric)**,
   **Estimated discharge required (symmetric)** and usable/reserve SOC sensors.
   These are estimates, not executed charge/discharge requests.

## Enable production control

The current native path requires Pixii meter data, appropriate Gateway account
access, an enabled observer, fresh safe battery evidence and a supported pristine
service-120 record with hard limits disabled and the expected meter binding.
The native service-reference delay must be exactly **10 seconds**. If another
actor already owns/configures that limiter or phase service, preparation blocks.

Do not automatically invoke the installer maintenance action to force those
prerequisites. Review [maintenance actions](actions-and-simulator.md#specialist-maintenance-actions)
if a site's native settings need separate work.

After enabling the shared authority/policy and the Load balancing switch, inspect
status attributes such as `control_status`, `control_blocked_reason`,
`applied_total_soft_limit_w`, `idle_support_status` and `idle_support_active`.
The native service-120 implementation uses a total budget split per phase; a
diagnostic aggregate is not a promise that every independent phase can be
controlled under every active service.

The automatic idle-phase support (Service 129) also follows the automation
policy's limits. It does not discharge at or below `soc_min_percent`. It does not
charge at or above `soc_max_percent`. It limits each phase to
`max_power_kw × 1000 / phases`, and each run lasts at most
`min(15 s, max_duration_seconds)`. If the policy is disabled or invalid, no idle
support starts and an active one is released (`automation_policy_not_authorized`).
That includes any policy that fails the general validity check: disabled, no
`automatic` operation class, an empty service list, zero power or duration, or
an inverted SOC range.
If the policy limits cannot cover the overload, the support is blocked rather
than exceeding them. The logbook records each block or limit once, with its cause.
This is intentional: whether the idle-phase support is in the service allowlist,
and whether the time window is open, are **not** checked, because fuse
protection must not stop outside a service list or time window.

Disabling control restores its exact owned limiter record when identity and
readback permit it, and releases its own idle-phase support. Uncertain ownership
blocks blind restoration. It does not stop the external service under the overlay.

## Typical waiting states

| State/reason | Meaning / next check |
| --- | --- |
| `disabled` / `waiting` | Observer off or waiting for its first complete sample. |
| `observing` | Complete calculation; no overload is currently indicated. |
| `overload` | Measured phase demand needs assistance within the calculated envelope. |
| `insufficient_capacity` | Available battery/phase capacity cannot meet the requested correction. |
| `phase_direction_missing` | Signed phase power is missing. Current magnitude alone is insufficient. |
| `meter_stale`, `meter_timestamps_inconsistent` | Inspect update rates, timestamps and units; do not loosen limits just to clear the state. |
| `meter_limitation_reference_delay_invalid` | Native delay differs from the required 10 s; separate installer review is needed. |
| `meter_limitation_in_use`, `phase_service_in_use` | Existing native settings/ownership prevent safe acquisition. Preserve the other actor's configuration. |

Old `load_balancing_*` number entities were retired. Configure the options above;
there is no second effective set of production fuse limits in those legacy numbers.

[← Solar and APS](solar-and-aps.md) · [Next: price optimization →](price-optimization.md)
