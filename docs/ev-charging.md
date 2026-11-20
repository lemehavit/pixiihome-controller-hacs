# EV charging: smart charging and charger load balancing

Version 0.5.33 can control an EV charger and a car through their own Home
Assistant integrations. It was built for Easee (charger) and MySkoda (car), but
any charger whose integration offers the same Easee actions works. Nothing here
talks to the Pixii Gateway.

| Part | What it does | How |
| --- | --- | --- |
| Smart EV charging | Starts and pauses charging so the car reaches its target by the ready-by hour in the cheapest price intervals | `easee.action_command` `resume` / `pause` |
| Car charge limit | Sets the car's own limit to the target, rounded up to what the car accepts | `number.set_value` on the car's charge limit |
| EV load balancing | Keeps every phase under main fuse minus margin by changing the charger current | `easee.set_circuit_dynamic_limit` |

Both switches are off after installation.

## Configure

Everything has its own device, **EV charging**, under the Pixii Gateway, with
its own switches and numbers. They do not change the battery's settings.

Open **Configure → EV charging**. The simplest setup is to choose only the
**charger device** and the **car device** (0.5.34). Their entities are found
automatically (Easee: `…_status`, `…_power`, `…_dynamic_circuit_limit`;
MySkoda: `…_battery_percentage`, `…_charge_limit`, `…_charging`,
`…_charger_connected`), and start, pause and current go through the charger
device's Easee actions, so no helper entities are needed. An entity chosen in
a field below wins over the one found; the sensor attribute `used_entities`
shows what is used.

The form has five sections:

| Section | Field | Example (Easee + MySkoda) |
| --- | --- | --- |
| Charger | Status sensor (required) | `sensor.<charger>_status` |
| Charger | Power sensor | `sensor.<charger>_power` |
| Charger | Start / pause charging | Empty = Easee `resume`/`pause` on the status sensor's device. Or choose a button, script or switch (a switch is turned on to start and off to pause) |
| Charger | Current control (number) | Empty = Easee dynamic circuit limit. Or a `number`/`input_number` in amperes |
| Charger | Current limit sensor | `sensor.<charger>_dynamic_circuit_limit` |
| Charger | Phases, minimum / maximum current | 3, 6 A, 16 A |
| Car | Battery level, charge limit (number) | `sensor.<car>_battery_percentage`, `number.<car>_charge_limit` |
| Car | Charging switch | `switch.<car>_charging`, only used to restart a session the charger calls completed |
| Car | Plugged in (binary sensor) | Optional; otherwise the charger status decides |
| Car | Capacity, efficiency | 77 kWh, 90 % |
| Price | Price sensor | Empty = the price sensor of price optimization |
| Load balancing | Grid current L1–L3 | Empty = Pixii load balancing measurements |
| Load balancing | Main fuse per phase, margin | Taken from Pixii load balancing when it is configured |
| Solar charging | Grid power (+ import, − export), or export plus import sensors | Empty = Pixii's grid meter |
| Solar charging | Solar production now (inverter power) | Empty = the energy forecast's solar power sensor |
| Solar charging | Solar forecast today / tomorrow | Empty = the energy forecast's (for example Solcast) |

On the **EV charging** device:

| Entity | Default |
| --- | --- |
| Smart EV charging | Off |
| EV load balancing | Off |
| EV may charge from home battery | Off |
| EV target battery level | 80 % |
| EV charge-now level | 20 % |
| EV ready by (hour) | 7 |
| EV solar charging | Off |
| EV before home battery on solar surplus | Off |
| EV sell solar instead from | 1.00 kr/kWh |
| EV solar charging target | 100 % |
| EV solar charging allowed grid import | 0 kW |
| EV solar start delay / stop delay | 3 min / 5 min |

## Smart charging

Every 10 seconds, while the car is plugged in:

1. The energy needed is `(target − battery level) × capacity ÷ efficiency`,
   plus 15 % margin. It is recalculated as the battery level rises.
2. Each price interval gets its own available power:
   - the charger's maximum current × phases × 230 V;
   - with EV load balancing on: limited by main fuse − margin − the forecast
     house load for that interval (spread over three phases, the busiest phase
     counted as 1.5 × an even share); for the current interval the measured
     phase room is used instead;
   - limited by what the car itself accepts, learnt when it draws clearly less
     than allowed for two minutes (one-phase charging, cold battery, taper).
     Forgotten when the car is unplugged.
   An interval where the room is below the minimum current gets no power.
3. Below the charge-now level, or when the power available until the ready-by
   hour cannot deliver the energy needed, it charges at once.
4. Otherwise it picks the cheapest intervals before the ready-by hour until
   their energy covers the need, and charges only in them. An interval in which
   charging started is finished.
5. If tomorrow's prices are not published yet and the unpriced hours before the
   ready-by time can deliver the whole charge, it waits for them.
6. While price optimization has a planned battery discharge, the car does not
   charge unless **EV may charge from home battery** is on. The charge-now
   level and the ready-by deadline still charge.
7. At the target it pauses. The car's own limit is set to the target rounded up
   to its step and range (MySkoda: 50–100 % in steps of 10). A target below 50 %
   is reached by pausing the charger.

Turning smart charging off resumes a pause it made.

## Load balancing

The **EV charger current** sensor (0.5.35, amperes, with phase room, fuse and
margin) shows what EV load balancing set. It is separate from the battery's
load balancing and never changes the battery.

The tightest phase decides, because a one-phase car may use any phase:

`new current = what the car draws now + min over phases (fuse − margin − measured current)`

Decreases are written at once (at most every 15 s). Increases wait 60 s and
rise at most 4 A per step. If the result is below the minimum current, charging
is paused and resumes after two minutes with at least 2 A extra room. Without
phase measurements the charger is held at the minimum current. When the car is
unplugged the current is set to the minimum, so a new session starts low.

When Pixii load balancing is on and active, current the home battery uses for
charging counts as room for the car: the battery's own limiter gives it up when
the car needs it. The car therefore has priority over grid charging of the
battery.

Turning EV load balancing off gives the charger its maximum current back.

## Solar charging (0.5.36)

A function of its own, independent of smart charging. When **EV solar
charging** is on and the car is plugged in:

- The surplus is what leaves the house now (grid export minus import) plus
  what the car already draws, never more than the solar production now, and
  minus what the home battery discharges, so export from a discharging battery
  (for example a planned price export) is not taken for sun. The charger current is the surplus divided by
  230 V × phases, between the minimum and maximum current. It rises at most
  4 A a minute and falls at once.
- It starts when the surplus has paid for at least the minimum current for the
  start delay, and pauses when it has been below that for the stop delay (the
  minimum current is kept meanwhile). *Allowed grid import* lets it start and
  keep going with that much bought from the grid, useful when the minimum
  current needs more power than the sun gives (6 A on three phases is 4.1 kW).
- When the price now is at or above *sell solar instead from*, the surplus is
  sold and the car does not charge from it.
- It stops at the *solar charging target*; the car's own charge limit is set to
  the higher of the smart and solar targets.
- The home battery takes the surplus first. With *EV before home battery*, the
  power the battery is solar-charging with also counts for the car, and the
  battery's own solar charging backs off as the car takes it.
- With EV load balancing on, the fuse still decides: the lower of the two
  currents is used.
- When smart charging wants to charge (cheap price, below the charge-now level
  or the deadline), smart charging decides and the current follows only load
  balancing.

When solar charging is on, smart charging also uses the solar forecast: the
forecast surplus (solar minus forecast house load) in an interval is counted
as costing only the spot price, while grid energy also costs the grid import
cost from price optimization's settings. Intervals with sun therefore rank as
cheaper than the same price at night, unless the price is at or above the
sell-instead price. `planned_slots` show `solar_kw` for such intervals.

## Logbook

Every action has a logbook line on **EV charging status** with *Who* and *Why*,
and the same reason appears on the charger's own logbook line. The sensor's
attributes show the plan (`planned_slots` with price and power, `need_kwh`,
`ready_by`), `charge_power_now_kw`, `car_max_power_kw`, the phase room and the
last actions.

## Before turning it on

Turn off your own automations that do the same thing, or they will fight:
dynamic current up/down, "start low", "pause if high current" and "resume when
the battery is done", and for solar charging the solar increase/decrease,
"start charging on solar" and "pause when no solar" automations.
