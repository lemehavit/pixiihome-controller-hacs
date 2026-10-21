# Changelog

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
