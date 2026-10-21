# Changelog

## 0.5.1 — 2026-10-21

- Battery activity no longer reports this integration's own price, solar or
  APS schedules as external schedules of unknown source.
- The scheduler card lists upcoming schedules first, marks ended ones, and
  names schedules created by this integration (for example *Prisoptimering*).
- **Battery savings today** gains a `this_week_sek` attribute (Monday to today).
- Logbook entries now fill Home Assistant's **What happened** with who caused the
  activity and why (price, expected profit and minimum for price schedules,
  the external source for foreign schedules, the reason for stops).
- Fix: price optimization no longer blocks with `price_schedule_identity_ambiguous`,
  and no longer treats its own schedule as an unknown external window, while
  one of its own schedules is in the Gateway list. The Gateway list carries no
  owner, so an OWNED record (proven by its keyed readback) now matches the one
  complete, consistent one-shot entry with exactly its start, duration and
  service. Two identical entries still block.

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
