# Troubleshooting

## HACS cannot find the integration

Add the repository as **Integration**, not Dashboard. It is currently a custom
repository rather than part of HACS's default catalog. Restart Home Assistant
after downloading. For a manual install, verify that
`custom_components/pixiihome_controller/manifest.json` exists without an extra nested folder.

## MQTT is unavailable or telemetry is missing

Check Home Assistant's MQTT integration and broker connection. Confirm locally
that status topics contain the same Gateway serial you entered. Inspect message
freshness and whether the relevant family exists on your firmware. Sensor
unavailability is not solved by enabling production control.

## Scheduler is unavailable

Check the local URL, trusted bootstrap ID, credentials, CA filename and certificate
hostname. Confirm Home Assistant can reach the Gateway. Missing scheduler access
does not prevent MQTT monitoring. Do not disable TLS verification to work around it.

## A controller or operation is blocked

Inspect the controller's status and integration diagnostics. Common reasons are
off authority, missing/stale telemetry, an active external service, incomplete
schedule inventory, incompatible controllers or unresolved command ownership.
Wait for an external operation to finish; do not delete it to clear a blocker.
An uncertain command must be reconciled rather than retried blindly.

## Report a reproducible issue

Use the GitHub bug template with integration and Home Assistant versions, Gateway
firmware, production/simulator mode, expected behavior and exact steps. Prefer
reproducing in the simulator. Review diagnostics before attaching them: remove
hostnames, addresses, serials, identifiers, credentials and private power/history
data. Never attach raw HAR files, keys, cookies, complete journals or MQTT captures.

The maintainer may ask for a minimal anonymized protocol example. Keep raw
installation reports locally. See [SECURITY.md](../SECURITY.md) for private reporting.
