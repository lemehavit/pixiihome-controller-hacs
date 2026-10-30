# Changelog

## 0.5.22 — 2026-10-29

- F3c: Regressionstester täcker en laddgräns under 0,05 kW och en filtrerad
  laddning före urladdning; engelska prisplanstexter har rättats.
- F7b: Dygnsplanens omspelning begränsar automatisk solladdning till verkligt
  överskott och urladdning till faktisk energi över minsta SOC. Standardplanens
  framtida pristoppar beräknas per segment och laddgränsen loggas bara när
  laddbeslutet ändras. Omspelningen skapar inte laddning från ett nollval under
  minsta SOC. Jämförelsekostnadens externa påfyllning förtydligas.
- F4: Ett eget schema som snart ska börja raderas inte medan det fortfarande
  kan behövas för planens styrning.
- F7: Dygnsplanens SOC och ekonomi räknas från de åtgärder som faktiskt skrivs.
- F6: Standardplaneraren köper bara energi som kan användas lönsamt.
- F5: Dygnsplanen räknar bara med solladdning som någon faktiskt utför.

## 0.5.21 — 2026-10-29

- F2c: Stabil sensororsak vid återställningsfel och tydlig varning när tjänst 120
  kvarstår utan sparad avstängning av styrbehörigheten.
- F3b: Granskningsuppföljning för säker leaseåterställning, korrekt policyloggning
  och kontroll av orsaken i prisoptimeringens loggbok.
- F3c: Separata läsfel för tjänst 120 och fasjournalen, tydliga återställningsorsaker
  samt säker avlastning och avisering vid journalfel.
- F2/F2b: Avstängd styrbehörighet släpper mätarbegränsningen även när
  styruppdateringar blockeras, med säker hantering av misslyckade försök.
- F10: Nedstängning stoppar kommunikationen även om ett tidigare steg misslyckas.
- F3: Kvarvarande tjänst 120 och fasstöd städas upp efter omstart när styrning är av.
- F8: Varningen för stillastående prisoptimering följer den faktiska stopptiden.
- F9: Ändrade aggregatormål läses om innan prisplaneringen fortsätter.
- F1: Fasstödet följer automationspolicyns gränser.
- D1: Dokumentationen för prisoptimering och energiprognos har uppdaterats.

## 0.5.20 — 2026-10-28

- Fix: the whole-day plan showed exports (and their money) that the export
  minimum then kept from being written. On 29 Oct it showed a 17:00-18:15
  export and 89 kr, while only two 15-minute house-load discharges were
  written: the aggregator's 20-21 Target SOC buys the energy back at 4.36
  kr, so selling at 7.1-7.35 nets only about 1.3-1.5 kr/kWh, below the
  2 kr export minimum. A refill by a fixed aggregator Target SOC now
  carries the same minimum-profit hurdle as the plan's own grid charges, so
  the plan, its segments and its expected money match what is written.
- Change: when the plan has no charge of its own after a discharge, the
  aggregator Target SOC windows are now held (lowered) up to the cheapest
  later aggregator refill in the horizon, not only up to the last
  discharge. On 29 Oct only 18-19 was held, so the 20-21 window refilled
  at 4.36 kr and the export did not pay; holding 20-21 lets the 22-23
  window refill at 3.58 instead. Only when "Allow changing aggregator
  schedules" is on.
- Test: the planner settles an ended, unconfirmed own DELETE end to end.

## 0.5.19 — 2026-10-28

- Fix: when the Gateway did not confirm the removal of one of our own price
  schedules and the read-only post-check could not settle it before that
  schedule started, every price write stayed blocked indefinitely
  (`scheduler_ownership_store_invalid`, Control Recovery "required"). On 28
  Oct this hit a 15:30-15:45 charge and blocked tomorrow evening's export.
  Once a one-shot schedule's whole interval has passed (plus 5 minutes) it
  cannot run again, so its outcome no longer matters: the identity is kept
  reserved (`expired_reserved`, never reused or re-sent) and the block lifts.
  Nothing is sent to the Gateway; the activity log says why.

## 0.5.18 — 2026-10-26

- Fix: the 25-hour day when summer time ends stopped price optimization
  for about 35 hours (`price_interval_invalid`, Sat 24 Oct 13:10 until Mon
  00:00), and "Battery savings" showed 0.00 kr for that Sunday. A price list
  where an interval ends before it starts, the repeated hour appears twice at
  the same instant, or that hour is missing is now mended instead of
  rejected.
- Fix: a refused change of an aggregator Target SOC was planned around until
  that window ended, even when the reason was only momentary (the ownership
  check right after an unconfirmed removal). On 23 Oct this replaced the
  17:00–19:45 discharge with "nothing planned" for the evening peak. Only
  lasting reasons are kept now; others are tried again on the next pass.
- Every new reason for "blocked" is written to the Home Assistant log at
  once.
- "Nothing planned" no longer says the best price pair is below your minimum
  when it is above it; it then says that SOC limits, house load or fixed
  aggregator windows leave no room.
- "Next price-optimized charge/discharge" no longer flips between a time and
  unknown at every planning pass.

## 0.5.17 — 2026-10-23

- Fix: an aggregator Target SOC that price optimization had lowered was never
  raised again when a later plan kept more energy in the battery, and the
  aggregator's own value was forgotten at every restart. Lowered targets are
  now remembered (with the aggregator's value) across restarts, and raised
  back to the planned level, never above the aggregator's own value, with a
  logbook entry "Raised aggregator Target SOC …". A target this integration
  never lowered is never raised.

## 0.5.16 — 2026-10-23

- Fix: with the whole-day planner, a lowered aggregator Target SOC followed
  the planner's full trajectory, including discharges that are never written
  because they stay below your minimum profit. On 23 October the 20–21 and
  22–23 targets were lowered to 76 % and 71 % for such a discharge; the
  aggregator's Target SOC could then have emptied the battery to the grid at
  about 1.2–1.4 kr/kWh, below your export minimum. Targets now follow only the
  actions that are written: energy that is kept back stays in the battery.

## 0.5.15 — 2026-10-23

- Fix: when the battery ran an aggregator schedule, Battery activity said
  "Who: aggregator schedule (source 7) … (this integration)". It now says
  the schedule was not created by this integration, and that only price
  optimization may lower or replace an aggregator Target SOC, with "Allow
  changing aggregator schedules" on.

## 0.5.14 — 2026-10-23

- Fix: when price optimization removed one of its own schedules and the
  Gateway did not confirm the removal at once, the battery activity log said
  "External schedule removed from the Gateway … This integration did not
  remove it". Our own records are now recognised in every state, so that line
  no longer appears for them.
- The removal is now logged with its reason when it is sent ("Removing …
  (no longer in the plan): the Gateway did not confirm it"), and a removal
  that the read-only post-check confirms counts in the deleted schedules
  counter.

## 0.5.13 — 2026-10-23

- Fix: lowering an aggregator Target SOC was refused every time with
  "scheduler LIST response exceeded the entry limit". The check that no other
  schedule shares the target record read the whole Gateway history through
  the normal schedule list, which allows 100 entries. It now reads that scope
  separately (up to 5000 entries) and no longer marks the schedule list as
  stale when it is too large.
- Fix: that failed list also made the schedule list count as stale, which
  blocked all new price schedules for about an hour without saying why
  (`price_create_safety_blocked`). A blocked write now logs which safety
  checks stopped it, once, and shows them in the attribute `blocked_details`.
- Fix: with the whole-day planner, a discharge is only written when its own
  profit reaches your minimum (self-use or export). A 19:00 slot that netted
  0.21 kr/kWh against a 1.00 kr minimum was written before.
- Fix: a discharge before a charge the plan itself makes is valued against
  that charge price instead of an average price.
- The large plan and schedule-list attributes are no longer stored in the
  recorder (they exceeded 16 KB, so Home Assistant dropped all of them). They
  are still on the entities for dashboards.

## 0.5.12 — 2026-10-22

- Fix: when an aggregator Target SOC could not be changed, price
  optimization dropped everything planned after it and tried again on every
  pass ("Could not lower aggregator Target SOC … (refused); nothing after it
  was scheduled", twice every 15 minutes). The reason was never shown. Now
  the reason is logged once (logbook and Home Assistant log), the target is
  kept as the aggregator set it and the plan is made around it right away,
  so the rest of the plan is still scheduled.
- Fix: with the whole-day planner, a discharge before a fixed aggregator
  Target SOC window is now valued against buying the energy back in that
  window (what the plan itself assumes), not against the price at planning
  time. That made every evening action show a negative profit while the plan
  total was positive.

## 0.5.11 — 2026-10-22

- Fix: with the whole-day planner, a discharge showed its gross saving as
  profit and the sell price as what the energy cost, so the net per kWh could
  exceed the price. A discharge now shows one full cycle: what it saves minus
  what its energy cost (the plan's grid charges, or the latest known price
  for energy already in the battery) after losses and cycle cost. Charges
  show no profit of their own. The plan's total is unchanged.
- The calendar no longer shows a net figure for charges, and the expected
  energy of a discharge without export is what the house is expected to
  take, not power × time.

## 0.5.10 — 2026-10-22

- Every event in the price optimization calendar now shows the plan's money.
  A discharge shows its expected profit in the title (for example
  "+5,60 kr") and in the description, with net profit per kWh, the price and
  what the energy cost. A charge shows how much energy it buys and what it
  costs; its profit is counted on the discharge that uses it.
- Created schedules keep their figures even after the plan is recalculated.
  `schedule_timeline` gains `expected_profit_sek`, `net_profit_sek_per_kwh`,
  `price_sek_per_kwh`, `reference_price_sek_per_kwh`, `energy_kwh` and
  `charge_cost_sek`.

## 0.5.9 — 2026-10-22

- Fix: the manual-control buttons (Start charging, Start discharging, Start
  target SoC, Start adaptive peak shaving) could stay "unavailable" after a
  restart. Their availability was only checked once at start-up, often while
  control authority was still held off. They now follow authority within
  about 30 seconds.
- New: **Start battery calibration** button, with **Calibration endpoint SoC**
  (default 90 %) and **Calibration duration** (default 2 h). It follows
  Pixii's documented `batterycalibration` service: discharge, then charge to
  the endpoint. It is refused if another service runs or any schedule lies in
  the window; an aggregator Target SoC may only be overridden when changing
  aggregator schedules is allowed. Price optimization, solar charging and APS
  wait while it runs, also across a restart, and the logbook records it.

## 0.5.8 — 2026-10-22

- Fix: the post-check from 0.5.7 never ran after a restart. A restart with an
  unconfirmed command switches control authority and the automatic
  controllers off, and price optimization only ran its checks while switched
  on. The read-only post-check now runs even with the switches off.
- The post-check can also prove a removal when the Gateway does not answer
  the keyed check: a complete schedule list, read after the problem was
  seen, with nothing at that schedule's time counts as proof that it is gone.
  Nothing is ever sent again.
- Switches that were on before such a restart are remembered and turned back
  on once every unconfirmed command is settled, each through its normal
  switch path. The logbook says what came back. Turning a switch by hand in
  the meantime replaces the remembered state. A journal read from backup or
  with mismatching copies still never resumes on its own.
- The panel's "Kontrollera resultat" also handles an unconfirmed removal.
- New attribute `delete_post_check_reason` on the price status shows why a
  post-check has not settled yet.

## 0.5.7 — 2026-10-22

- Fix: when the Gateway never confirmed the removal of an own price schedule
  (timeout), every later price write stayed blocked
  (`price_create_safety_blocked`, "Efterkontroll krävs"). Price optimization
  now settles this itself with one read-only check, never a second delete:
  if the schedule is gone the block is lifted, if it is still there and
  unchanged it is treated as an own schedule again. Anything else stays
  blocked. The logbook says what the check found.
- Fix: a narrow schedule-list read (calendar, panel, Gateway check) running at
  the same time as a full refresh could drop schedules from the inventory the
  planner and activity log use. The refresh now notices this, reads again,
  and never publishes a list with missing schedules as complete.
- Fix: right after a restart the activity log could say a running schedule
  was "not started by this integration" before the schedule list was read.
  It now waits for the list.
- Moving an aggregator charge schedule to a cheaper hour now also requires
  **Allow changing aggregator schedules**, like every other change to
  aggregator schedules.

## 0.5.6 — 2026-10-22

- Price calendar: discharges without export now read "max N kW". That power
  is a ceiling (the battery follows the house load up to it), unlike export
  and charging, which run at the stated power.

## 0.5.5 — 2026-10-22

- Fix: price optimization could not remove its own future schedules when the
  plan changed (`price_delete_service_mismatch`). The delete path only
  accepted Target SoC records, but price schedules are Demand Response or
  Fixed Peak Shaving. Every new plan with a different first action was
  blocked by this since the first version.
- With **Allow changing aggregator schedules** on, the price plan may now use
  the time of an aggregator Target SOC schedule itself. When it does (for
  example to discharge in the day's most expensive hour), that aggregator
  schedule is deleted and replaced by the plan's own schedule. Only windows
  needed within the next planning passes are deleted; later ones are lowered
  until then. Off: aggregator schedules are never touched.
- Activity log: an aggregator schedule removed by price optimization is
  logged with that cause instead of "removed outside this integration".

## 0.5.4 — 2026-10-22

- The whole-day planner now honours **Allow changing aggregator schedules**,
  like the standard planner: aggregator Target SOC schedules between a planned
  discharge and the next planned charge are lowered to the planned level (never
  below the minimum SOC) instead of being treated as a refill that undoes every
  sale. Before, the whole-day planner always planned around them, so with an
  aggregator refilling every other hour it only used the battery after the last
  refill of the day.
- Whole-day planner: a discharge that only exceeds the expected house load by
  less than one energy step of the planner (rounding) now follows the house
  load (Fixed Peak Shaving) instead of becoming a fixed export of that power.

## 0.5.3 — 2026-10-22

- Activity log: external schedules now show the Gateway's source ID, for example
  "external schedule (source 7)", instead of "schedule of unknown source". When
  the ID equals the configured managed aggregator source ID, the schedule is
  named "aggregator schedule".

## 0.5.2 — 2026-10-21

- Logbook entries now fill Home Assistant's **What happened** with who caused the
  activity and why (price, expected profit and minimum for price schedules,
  the external source for foreign schedules, the reason for stops).
- Fix: price optimization no longer blocks with `price_schedule_identity_ambiguous`,
  and no longer treats its own schedule as an unknown external window, while
  one of its own schedules is in the Gateway list. The Gateway list carries no
  owner, so an OWNED record (proven by its keyed readback) now matches the one
  complete, consistent one-shot entry with exactly its start, duration and
  service. Two identical entries still block.

## 0.5.1 — 2026-10-21

- Battery activity no longer reports this integration's own price, solar or
  APS schedules as external schedules of unknown source.
- The scheduler card lists upcoming schedules first, marks ended ones, and
  names schedules created by this integration (for example *Prisoptimering*).
- **Battery savings today** gains a `this_week_sek` attribute (Monday to today).

## 0.5.0 — 2026-10-21

- **Energy forecast** (Configure → Energy forecast): household consumption is
  learned per hour of the week (bootstrapped from up to 70 days of Home Assistant
  statistics; medians of the last six weeks, a daily temperature regression and
  a high 80th-percentile value). Optional Solcast or Open-Meteo solar forecast with
  intraday correction from the inverter's production.
- New switch **Smart solar charging**: solar charging waits and the surplus is
  sold while the price now is clearly higher than later today, only when the
  conservative solar forecast still fills the battery to Solar maximum SoC with
  a safety margin. Anything uncertain charges at once.
- Price optimization limits self-consumption discharge to the forecast household
  load. New switch **Plan for high consumption** uses the high forecast.
- New switch **Use whole-day planner**: a dynamic-programming plan over the full
  price horizon with consumption and solar. Always calculated and shown in
  `whole_day_plan` for comparison; used only when switched on.
- Adjacent identical price intervals are merged into one schedule.
- New sensors: Household consumption, Consumption forecast next 24 h, Solar
  forecast remaining today and Battery savings today (with daily logbook entry).
- Notification when price optimization has been stopped longer than
  **Notify when price optimization is stopped for** (default 3 h).
- Swedish `reason_text` attribute on the status sensors.
- New **Battery activity** sensor and logbook entries for everything the battery
  does and why: each service start with its cause (integration feature, external
  Gateway schedule or unknown), charge/discharge changes, external schedules
  added or removed, each new price plan or why nothing was planned, and why solar
  charging or adaptive peak shaving stopped.

## 0.4.2 — 2026-10-21

- Activity log entries for actions actually taken, shown on each feature's status
  sensor: price schedules created or removed (time, power, price, expected profit),
  aggregator Target SOC lowered, restored or deleted, SOC-guard stops, control
  authority cleanup, solar charging start, adaptive peak shaving start and limit
  changes, and load-balancing limit changes.
- Price optimization no longer shows a transient "planning" state on every pass.

## 0.4.1 — 2026-10-21

- Shorter switch names so they fit: **Allow creating schedules**, **Allow grid
  export** and **Allow changing aggregator schedules** (entity IDs unchanged).

## 0.4.0 — 2026-10-21

- New opt-in switch **Allow price optimizer to adjust aggregator Target SOC**
  (off by default). When on, future one-shot aggregator Target SOC schedules
  between the optimizer's planned discharge and its next planned charge are
  lowered to the planned state of charge (never below the price minimum SOC),
  or deleted if the Gateway keeps the old target. Changes are journaled,
  verified by readback, limited to one per planning pass and restored when no
  longer needed.

## 0.3.9 — 2026-10-21

- Price optimization plans past one-shot external Target SOC schedules: it reads
  each target, assumes that level after the window and keeps planning instead of
  stopping at the next external schedule. Stored energy sold before such a window
  is valued at the window's price; energy charged in the same gap keeps its own.
- While a known external schedule runs, owned schedules that start after its end
  may be written. Unknown external activity still blocks writes.
- New attribute `busy_until` shows when such a running external schedule ends.

## 0.3.8 — 2026-10-21

- Price optimization plans with the remaining known prices instead of waiting for
  next-day data, and replans when it is published. New attribute
  `next_day_prices_available`.
- Turning production authority off first stops integration-owned APS/solar leases,
  deletes future price schedules and stops a running price interval while Gateway
  writes are still allowed. Anything left is reported in a persistent notification.

## 0.3.7 — 2026-10-21

First standalone public distribution of the community integration (beta).

- Add HACS metadata, installation/configuration guides, original project icon,
  MIT license, issue templates, offline CI, Hassfest and HACS validation.
- Package only integration runtime files and the license. Publish from a clean
  history; private installation reports and original operational journals are excluded.
- Move regression tests to minimal anonymized protocol/journal fixtures.
- Align the reported integration version, package version and Python baseline.
- Include the setting-backed switch routing correction from private development.
- Block remaining price-plan writes if its selected price entity is removed mid-operation.
- Include exact owned price-interval DELETE handling, physical idle verification,
  conservative identity reservations and explicit archival recovery.

Hardware status: own discharge and output stop were observed in a finite 300 W
zero-export trial. Natural SOC boundary crossing and priority handoff remain
unverified. Output stopped, but the identity was not proven absent; the priority
part was deferred. No physical FCR/FFR validation is claimed.

## Earlier private development

Versions through 0.3.6 introduced monitoring, scheduler inventory, simulator,
native entities, guarded controllers and price planning. The public repository
starts with the current source; private deployment history is not distributed.
