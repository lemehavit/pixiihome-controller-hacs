# Configuration

## Connect telemetry first

Configure Home Assistant's MQTT integration using your existing broker. The
Gateway must publish Pixii telemetry to that broker. This integration uses Home
Assistant's MQTT connection; it does not connect directly to an aggregator broker
or configure the Gateway's MQTT settings for you.

Select **Production Gateway** and supply its serial. Supported status topic layouts:

```text
pixii/status/<serial>/<family>
pixii/<serial>/status/<family>
```

Families include `core`, `battery`, `meter` and `energy`. Availability depends on
Gateway firmware and installed equipment. AC/battery power is positive when
charging and negative when discharging. Missing fields remain unavailable.
The simulator is a separate config entry; it never replaces a failed production connection.

## Optional scheduler HTTPS

Open the integration's **Configure** options and enable scheduler access. Enter
the Gateway's local HTTPS URL, username, password and the owner's exact trusted
32-character bootstrap ID. The integration pins this supplied ID; it does not
scrape `/js/user.js`. Leave scheduler access disabled if you do not have these values.

Some Gateways use a private CA certificate:

1. Download the **CA Certificate** from **Communication → Certificates** in your Gateway.
   Only the public CA certificate is needed, never an MQTT client private key.
2. Store it under `/config/ssl/`, for example `/config/ssl/pixii-ca.crt`.
3. Enable **Use Pixii Gateway private CA compatibility** and enter only
   `pixii-ca.crt` in the certificate filename field.

The filename must be a safe basename inside `/config/ssl/`. Certificate and
hostname verification remain enabled. Compatibility relaxes only
`VERIFY_X509_STRICT` in the dedicated scheduler TLS context for certificates
without `keyUsage`; it does not change global Home Assistant or MQTT TLS settings.
Use a hostname covered by the Gateway's certificate.

## Enable control deliberately

Production control authority, automatic controllers, live price schedule writes
and price export are independent gates. Defaults are off. Changes persist across
restarts. Inspect each device's switches and current status after an upgrade.

- **Production control authority** permits guarded writes. It does not itself
  enable an automatic controller.
- **Adaptive peak shaving**, **solar charging**, **load balancing** and **automatic
  price optimization** have their own controls and safety inputs.
- **Price schedule writes** allows a price plan to affect the Gateway. Keep it off
  while inspecting proposals.
- **Price export** is a separate opt-in. Select the correct price unit and account
  for import fees, efficiency and cycle cost before relying on profit estimates.

Adaptive peak shaving and automatic solar charging cannot run together: they
share a native service. Fuse ratings and phase inputs must match the real site.
Never increase a limit simply to clear a blocked status.

External service priority, complete schedule inventory, fresh telemetry, battery
limits, exact ownership and journal state are checked before writes. Do not run
multiple uncoordinated controllers for the same battery. The integration does not
disable, migrate or remove existing Home Assistant YAML or automation publishers.

Aggregator coexistence is optional and requires explicit source-ID authority
before relocation of a proven future charging schedule is considered. If you want
all aggregator schedules preserved, keep that authority disabled. No ownership is
inferred merely from a schedule's `m_id`, service number or source ID.

## Uncertain stops and recovery

An acknowledged DELETE and idle telemetry can prove that output stopped while
the Gateway still reserves an identity. The integration retains that reservation
and the original journal outcome; it does not reuse the ID based on idle alone.
Do not edit the journal, clear ownership or replay a command to bypass this state.

Administrative validation/recovery actions are for a separately reviewed,
bounded maintenance procedure. They are not needed to install or monitor the system.
Do not run them from unattended automations or copy a test identity from another site.
