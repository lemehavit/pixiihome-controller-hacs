# User guide

This guide explains what each part of Pixiihome Controller does, where to
configure it, and how to tell whether it is working. It describes the public
0.5.0 code. Read the [disclaimer and hardware limits](../README.md) before control.

## Contents

1. [Installation and connection settings](configuration.md)
2. [Control authority and automation policy](control-policy.md)
3. [Manual charging and target SOC](manual-control.md)
4. [Solar charging and adaptive peak shaving](solar-and-aps.md)
5. [Load balancing and phase measurements](load-balancing.md)
6. [Price optimization and price sources](price-optimization.md)
7. [Energy forecast, smart solar charging and whole-day planning](energy-forecast.md)
8. [Schedules, calendars and dashboards](schedules-and-dashboard.md)
9. [Complete entity and number-setting reference](entities.md)
10. [Actions, advanced controls and simulator](actions-and-simulator.md)
11. [Worked examples](examples.md)
12. [Status messages and troubleshooting](troubleshooting.md)

Each feature guide follows the same sequence: purpose, prerequisites,
configuration, expected behavior and what to check when it waits or blocks.
Tables distinguish accepted input ranges from settings suitable for your site.

## Start with your goal

| I want to… | Start here | What changes the battery? |
| --- | --- | --- |
| See battery and grid measurements | [Connections](configuration.md), then [sensors](entities.md#measurement-and-status-sensors) | Monitoring does not command the battery. |
| Inspect existing schedules | [Scheduler and calendars](schedules-and-dashboard.md) | Viewing/refreshing does not change schedules. |
| Charge or discharge once | [Manual control](manual-control.md) | A Start button sends a finite native operation after checks. |
| Store surplus solar energy | [Solar charging](solar-and-aps.md#automatic-solar-charging) | Its controller creates and renews owned APS leases. |
| Keep grid power inside a band | [Adaptive peak shaving](solar-and-aps.md#adaptive-peak-shaving) | Its controller creates and renews owned APS leases. |
| Reduce phase overloads | [Load balancing](load-balancing.md) | The separate control switch enables native soft-limit/idle-phase support. |
| Inspect cheaper charge/discharge times | [Price preview](price-optimization.md#configure-a-preview-first) | Keep price schedule writes off; preparation can still read the Gateway. |
| Apply price schedules | [Price live operation](price-optimization.md#allow-live-schedules) | Authority, policy, controller and schedule-write gates must all allow it. |
| Learn without a battery | [Simulator](actions-and-simulator.md#local-simulator) | Simulator operations are local and never use production transport. |

## Where settings live

| Location | Used for |
| --- | --- |
| **Settings → Devices & services → Add integration** | Runtime mode and Gateway serial. |
| Integration **Configure → Scheduler** | Gateway HTTPS access, certificate settings and the automation-policy object. |
| **Configure → Load balancing** | Observer enable, meter source, fuses, phase bindings and sampling. |
| **Configure → Price optimization** | Price entity, horizon, refresh/lead times and optional managed source ID. |
| **Configure → Energy forecast** | Solar forecast, solar production and outdoor temperature entities. |
| Device page **Configuration** entities | Numeric operating limits, production authority and controller switches. |
| Optional Lovelace card | Scheduler browsing, preview and explicit apply/reconciliation. |
| **Developer tools → Actions** | Read actions and specialist maintenance actions; see their separate reference. |

The Gateway device owns telemetry. Related settings are grouped into logical
child devices: Manual control, Schedule planner, Load balancing, Automatic solar
charging, Adaptive peak shaving and Price optimization. These are functions of
one physical Gateway, not additional batteries.

Entity IDs vary with device names, language and registry history. This guide uses
stable **keys**, such as `price_max_charge_kw`, rather than promising a particular
`number.*` ID. Use the entity's settings dialog to copy its actual entity ID.

## Common concepts

- **Power direction:** positive battery power means charging; negative means
  discharge. Grid power is positive for import and negative for export.
- **Raw versus usable SOC:** raw SOC is the battery's native percentage. Usable SOC
  describes the usable window after configured reserves, or the Gateway's native
  usable measurement. Feature reserves are normally expressed in usable SOC.
- **Lease:** a finite native interval renewed only while checks and ownership pass.
  It is not permission to restart an unrelated mode.
- **Preview:** a proposal. It is not evidence that a schedule exists in the Gateway.
- **Output stopped versus identity absent:** zero measured power can coexist with
  a reserved schedule identity. The integration preserves that distinction.
- **Time:** native timestamps are normalized to UTC. Policy windows and the card's
  local time fields use Europe/Stockholm, including daylight-saving transitions.

[Next: installation and connection settings →](configuration.md)
