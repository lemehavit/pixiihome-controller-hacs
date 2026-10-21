# Schedules, calendars and dashboards

[Guide contents](README.md) · [Previous: price optimization](price-optimization.md)

## Inspect the Gateway inventory

Configure scheduler HTTPS first. **Scheduler Inventory** and the **Schedules**
calendar expose normalized records, including externally created ones. Their
presence does not make those records integration-owned.

Check inventory `data_status`, freshness, authentication, completeness and
consistency. An incomplete or stale list is not proof of an empty conflict-free
schedule window. Different identifiers have different purposes:

| Identifier | Meaning |
| --- | --- |
| `sch_index` | Index for a native scheduler-detail record. |
| `service_no` | Native service instance/reference. |
| `m_id` | MQTT command/schedule identity where supported by that transport. |
| `source_id` | Origin/source metadata; not automatic ownership permission. |
| `asset_id` | Asset reference in the native record. |

Do not substitute one for another merely because numeric values happen to match.

## Add the optional scheduler card

First open **Dashboard resources** (enable Advanced mode in your profile if that
menu is hidden). Add URL `/pixiihome_controller/lovelace.js` with resource type
**JavaScript module**. The integration serves that module; it does not create
the Lovelace resource entry for you. Then add a **Manual** card with:

```yaml
type: custom:pixiihome-control-card
```

For several entries you can optionally select one initially:

```yaml
type: custom:pixiihome-control-card
entry_id: YOUR_CONFIG_ENTRY_ID
```

Use the actual integration config-entry ID, not an entity ID or serial. If left
out, the card can select from its available entries. Its WebSocket controls
require an authenticated Home Assistant administrator. The current custom card
uses Swedish UI text even if native entity names are translated differently.

If the card is unknown after installation, restart Home Assistant, refresh the
browser, and inspect **Dashboard resources** for the module path above. Native
entities and calendars work independently of the custom card.

## Preview and apply a schedule

1. Click **Uppdatera scheman** (refresh schedules).
2. Select a record, or **Nytt schema** if the entry advertises supported creation.
3. Enter the service-specific values and finite start/end or duration.
4. Choose **Förhandsgranska** (preview), then read the before/after values,
   blockers, affected records and required confirmation.
5. Apply only the exact reviewed preview. Refresh or re-preview if state changes.
6. Inspect reconciliation and actual inventory/output afterward. For an uncertain
   outcome use **Kontrollera resultat** (check result) where offered; do not send
   the same mutation again to find out whether the first one worked.

Available create service forms include Target SOC, Demand Response, Peak Shaving
and Adaptive Peak Shaving, subject to the entry's supported adapter and policy.
Power fields use kW in the card and are converted to native W. A typed preview
does not make every service/edit operation supported on every Gateway.

Manual edits/deletes of an external schedule are deliberate operations, separate
from the optimizer's default ownership boundaries. Do not select an aggregator
schedule unless changing that exact record is intended and permitted. A shared
Target SOC reference can affect other records; inspect the preview's scope rather
than assuming a changed SOC value is isolated to one displayed row.

Previews bind the observed state, authority revision and proposed intent. They
are single-use and rejected after relevant changes. A failed, expired or consumed
preview is not a reason to bypass the checks. Neither calendar supports direct
calendar create/update/delete actions.

## Time zones and recurring records

Card time inputs use **Europe/Stockholm**, not the browser's arbitrary local zone.
During the autumn DST transition, choose the first or second occurrence when a
wall time repeats. A nonexistent spring-transition time is rejected. Native
records are normalized to UTC; Home Assistant calendar rendering can use its
configured time zone.

Recurrence is shown for native recurring records. Automatic price reconciliation
is conservative about unknown/incomplete recurrence; it does not guess a safe
one-shot window from the next occurrence alone.

## Price calendar versus Gateway calendar

The **Price optimization schedules** calendar combines current proposals and
owned records. Its labels distinguish:

| Label | Meaning |
| --- | --- |
| Planförslag | Computed proposal; no proof it was created. |
| Skapat i Pixii | Owned schedule reconciled at its last keyed readback. Not continuous proof that an external actor has not changed the Gateway since. |
| Efterkontroll krävs | Provisional/conflicted record requiring reconciliation. |

Ended intervals disappear. Existing owned intervals can remain visible when new
planning is disabled. Read the record's status and controller reason before
using a calendar timestamp in another automation.

## Generate a dashboard with native entities

The response-only action below returns a dashboard configuration using **actual
registered entity IDs** for the selected entry:

```yaml
action: pixiihome_controller.generate_dashboard_config
data:
  config_entry_id: YOUR_CONFIG_ENTRY_ID
```

Run it interactively as an administrator in **Developer tools → Actions**. Review
and copy its returned configuration into a dashboard you choose. The action does
not save a dashboard, modify YAML or start control. Groups with no matching
registered entities are omitted. See [worked dashboard examples](examples.md).

[← Price optimization](price-optimization.md) · [Next: entity reference →](entities.md)
