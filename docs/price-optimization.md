# Price optimization

[Guide contents](README.md) · [Previous: load balancing](load-balancing.md)

## Purpose

Propose future battery charge/discharge intervals from a Home Assistant price
series, usable SOC/capacity, power limits and minimum net profit. With explicit
write permission it reconciles integration-owned schedules. It does not fetch
prices itself, configure a provider account, or guarantee financial savings.

Self-consumption uses zero-export Peak Shaving for discharge. Export, when
separately allowed and profitable, uses signed Demand Response. Charging uses
Demand Response. A generic manual discharge is a different operation.

## Source and timing options

Open **Configure → Price optimization**:

| Field | Default / range | Function and intended use |
| --- | --- | --- |
| `price_entity_id` | Empty; a `sensor.*` entity | Select a sensor with a supported timestamped price series in its attributes. A scalar current price alone is insufficient. |
| `managed_source_id` | Empty; integer 0–2147483647 if used | Advanced explicit source binding for supported relocation of a future external Target SOC charging schedule. Leave empty to avoid requesting that feature. It is not your Gateway serial or a schedule ID. |
| `planning_horizon_hours` | 36 h; integer 12–72 h | Future planning window, limited by available valid prices, policy windows and external schedules. Does not authorize a native interval of that length. |
| `minimum_lead_minutes` | 5 min; integer 1–60 min | Minimum lead time before a proposed future interval may begin. |
| `refresh_minutes` | 60 min; integer 5–360 min | Periodic replanning interval. Relevant price, service and inventory changes can also wake the controller. |

## Supported price data

The selected sensor's `unit_of_measurement` must be **SEK/kWh** or **öre/kWh**.
Öre values are divided by 100 internally. Values must be finite numbers, not
numeric strings. A generic EUR/kWh sensor cannot be selected without adapting
its data and currency yourself.

Supported attribute layouts, in detection order:

| Attributes | Item fields | Notes |
| --- | --- | --- |
| `previous_day`, `current_day`, `next_day` | `date`, `time`, `price` | Greenely-style date such as `Nov 01 2026` and `HH:MM`; wall times are interpreted in Europe/Stockholm. |
| `raw_today`, `raw_tomorrow` | `start`/`end` and `price` or `value` | Timestamped day arrays, including compatible Nord Pool-style sensors. |
| `prices` | `start` or `start_time`, optional `end` or `end_time`, and `price` or `value` | Generic timestamped series. Without end time, the item is treated as one hour. |

Timestamped items accept timezone-aware ISO timestamps or Home Assistant datetime
values. Include an explicit offset/`Z`; naive times are rejected. Missing,
duplicate or invalid intervals and unsupported formats do not become an empty
"successful" plan. Daily adapters plan with the remaining current-day intervals until next-day data
is published; the `next_day_prices_available` attribute shows which case applies.
Publication triggers a replan that replaces obsolete future owned schedules.

### Generic attribute example

This illustrates a price sensor's attributes, not integration configuration.
Dates are illustrative and must be current when testing a real source. In actual
use provide a complete future series, not just these two intervals.

```yaml
unit_of_measurement: SEK/kWh
prices:
  - start: "2026-11-01T01:00:00+01:00"
    end: "2026-11-01T02:00:00+01:00"
    price: 0.50
  - start: "2026-11-01T02:00:00+01:00"
    end: "2026-11-01T03:00:00+01:00"
    price: 1.20
```

## Configure a preview first

1. Set up scheduler HTTPS, MQTT telemetry and a supported price entity.
2. Set **Usable battery capacity for price planning** (`price_usable_capacity_kwh`).
   Its default is **0**, deliberately blocking planning until an explicit positive
   usable capacity is entered. Do not substitute total nameplate capacity without
   accounting for the usable window.
3. Configure charge/discharge power, usable-SOC bounds, efficiency, cycle cost and
   variable import costs on the **Price optimization** device.
4. Keep **Allow creating schedules** and **Allow grid export**
   off. Set an appropriate policy authorizing `automatic`,
   `demandresponse` and `peakshaving`.
5. Enable production authority and **Automatic price optimization**. Current code
   requires authority/policy even for this controller's preview mode; it is not a
   completely authority-free offline planner. Preparation reads prerequisites.
6. Inspect **Price optimization status**, plan attributes and the price calendar.
   `preview` with reason `price_schedule_writes_disabled` is the expected no-write
   state once all other prerequisites pass.

Use [all price number defaults/ranges](entities.md#price-optimization-numbers).
Price minimum/maximum SOC limits constrain direction. An initial SOC above this
feature's maximum may still allow discharge; it does not force a discharge down
to the ceiling. Below minimum, charging may still be permitted.

## Profit settings mean net profit

The historical key names contain `difference`, but the values are minimum **net
profit per delivered AC kWh**, after losses, import cost and cycle cost.

For the simplified comparison used by the planner:

```text
delivered_cost = (buy_price + variable_import_cost) / round_trip_efficiency + cycle_cost
self_consumption_profit = later_price + avoided_variable_import_cost - delivered_cost
export_profit = later_price - delivered_cost
```

Efficiency is a fraction in that calculation (`90%` → `0.90`). Self-consumption
avoids the configured import cost; export does not earn it. Avoid double-counting
fees already included in the source price. Fixed monthly costs are not a variable
cost per imported kWh. Existing battery energy uses the model's acquisition-cost
estimate, not a verified accounting ledger of your historical bill.

Example: buy `0.50`, later price `2.00`, import cost `0.40`, efficiency `90%` and
cycle cost `0.10` give delivered cost `1.10 SEK/kWh`, self-consumption profit
`1.30`, and export profit `0.90`. A self-consumption floor of `0.50` passes; an
export floor of `1.00` fails. Enabling export does not suppress eligible self-use.

## Allow live schedules

After reviewing proposals, turning on **Allow creating schedules** permits its guarded reconciliation. It is not an immediate manual
"run this proposal" button: source, settings, policy, inventory, ownership and
active service are checked again before each mutation.

Only the first charge/discharge cycle is applied; later cycles remain proposals
until fresh energy/SOC evidence is available. Recurring or incomplete inventory and
ambiguous identity can block the plan.

External schedules are never planned over. For a one-shot external Target SOC
schedule the controller reads its target from the Gateway and assumes the battery
is at that level when the window ends, so planning continues past it. Energy that
was already stored and is sold before such a window is valued at the window's
price, because the window buys it back; energy charged inside the same gap keeps
its own price. Any other external service, or a target that cannot be read, has
unknown resulting energy and ends the plan at that schedule's start.

While a known external schedule is running (its service matches exactly one
enabled one-shot schedule in the inventory), owned schedules that start after its
end may still be written. An external service without such a schedule blocks writes.

## Adjust aggregator Target SOC (optional)

**Allow changing aggregator schedules** is off by default and
only acts when **Allow creating schedules** is also on. It lets
the controller plan a whole day across an aggregator's one-shot Target SOC
schedules instead of being refilled by each of them:

- Only future, one-shot Target SOC schedules whose parameter record is not
  shared with another schedule are touched.
- Only windows between the first planned discharge and the next planned charge
  (or the last planned discharge) are changed. Their target is lowered to the
  planned state of charge at that time, never below the price optimizer's
  minimum usable SOC (or the automation policy's minimum).
- The plan may also use the time of such a window itself (for example to sell
  in the day's most expensive hour). That window is then **deleted** (replaced
  by the price plan's own schedule), verified by a fresh schedule list. A
  deleted schedule cannot be restored, so only windows the plan uses within
  the next planning passes are deleted; later ones are only lowered until then.
  The activity log names price optimization as the cause.
- Each change is journaled, sent once and verified by readback. If the Gateway
  acknowledges a write but keeps the old target, that schedule is deleted
  instead (also verified). Uncertain results require recovery and are never
  retried.
- A lowered target is restored to its original value when the plan no longer
  needs it. Originals are saved in the control journal
  (`lowered_external_targets`) and read back after a Home Assistant restart,
  so a target can still be restored. Saving is best effort; if the journal
  cannot be written, check the scheduler view after a restart.
- At most one aggregator schedule is changed per planning pass. If a window
  cannot be held, nothing after it is written.

Raise the price optimizer's minimum SOC if you want more energy left for
support services while targets are lowered. Whether changing an aggregator's
schedules is allowed under your agreement is your decision. The controller
does not infer ownership from a matching time, service number or `m_id` value.

Enable **Allow grid export** only if intended and compatible with
the installation/agreement, then set its separate minimum net-profit threshold.
Leave `managed_source_id` empty and external authority disabled to preserve
aggregator scheduling. Relocation requires additional exact source, direction,
future one-shot and readback proof; a source ID alone does not grant blanket control.

## Use the consumption and solar forecast

With **Configure → Energy forecast** set up, the plan limits self-consumption
discharge to the forecast household load, and **Plan for high consumption**
keeps more energy for hours with usually high load. **Use whole-day planner**
plans the whole price horizon together with solar. See
[Energy forecast](energy-forecast.md).

## Read the result and disable

Dygnsplanens `cost_without_battery_sek` är jämförelsekostnaden utan
prisoptimeringens egna åtgärder. Fasta externa Target SOC-mål fyller fortfarande
batteriet i både jämförelsebanan och den planerade banan, och kostnaden för
den påfyllningen ingår i båda. Namnet behålls för kompatibilitet. Om en
föreslagen åtgärd filtreras bort spelas senare flöden upp mot faktisk beräknad
SOC; dygnsplanens DP-val optimeras däremot inte om efter filtreringen.

The next-charge/discharge sensors can describe a current or future interval.
Read their `schedule_status`: `planned` is a proposal, `scheduled` is an owned
interval reconciled at the last readback, and `unverified` needs after-checking.
**Created price schedules** counts unexpired scheduled entries, not all proposals
or lifetime commands. A timestamp can be in the past while its interval is active.

Turning the controller off runs supported cleanup of its future owned schedules.
Turning only the schedule-write gate off does not guarantee all previously
created intervals were removed. Check calendars, ownership and actual output.
The SOC guard supervises owned intervals separately from new planning permission.
If output stopped but native identity absence is unproven, the reservation is
preserved and new plans can wait for reconciliation. Do not clear the journal.

Hardware validation remains bounded: natural SOC boundary crossing and physical
priority handoff have not been verified. See the [release limitations](../README.md#control-status-and-limitations).

[← Load balancing](load-balancing.md) · [Next: energy forecast →](energy-forecast.md)
