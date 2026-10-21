# Configuration

[Guide contents](README.md) · [Next: control authority and policy](control-policy.md)

## First setup

1. Install through HACS and restart Home Assistant as described in the
   [installation instructions](../README.md#install-with-hacs).
2. Add **Pixiihome Controller** in **Settings → Devices & services**.
3. Choose the runtime below. For production, verify your serial and MQTT data
   before enabling scheduler access. Leave control switches off initially.
4. Open **Configure** on the integration entry for its three options pages.
   Operational numbers and switches are on the device pages instead.

| Initial field | Default / accepted input | Meaning and intended use |
| --- | --- | --- |
| `runtime_mode` | Production Gateway; alternatively Local Simulator | Production uses your real Gateway; simulator is isolated. This is an entry-creation choice. |
| `serial` | No default; 1–64 letters, digits, `_` or `-` | Required in production. Match the serial in status topics exactly. Unused in simulator mode. |

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

### Scheduler option reference

These fields are in **Configure → Scheduler**. Saving valid options reloads the
entry. A blank password/bootstrap field preserves an existing saved value.

| Field | Default / range | What to enter and why |
| --- | --- | --- |
| `scheduler_enabled` | Selected when a scheduler URL is configured | Enable HTTPS inventory and supported controller access. Disable for MQTT-only monitoring. |
| `scheduler_url` | Empty | Local Gateway HTTPS URL, for example `https://pixii-gateway.example`. The example hostname must be replaced with your certificate-matching address. |
| `scheduler_username` | Empty | Gateway account used for scheduler access. Write features can require installer privileges. |
| `scheduler_password` | Empty on the form | Password for that account. Keep it private; a blank edit preserves the saved password. |
| `scheduler_bootstrap_id` | Empty on the form; exactly 32 characters | Owner-supplied trusted ID. Do not use another site's ID or obtain it from a public example. |
| `scheduler_timeout` | 10 s; 1–60 s | HTTPS timeout. A longer timeout does not resolve invalid credentials or uncertain command delivery. |
| `scheduler_ca_compatibility` | Off | Enable only for a Gateway requiring the private-CA compatibility described above. |
| `scheduler_ca_filename` | Empty; safe filename only | Public CA file under `/config/ssl/`. Required when compatibility is enabled. |
| `automation_policy` | Disabled policy object | Shared automatic-control authorization. See the complete [policy reference](control-policy.md#automation-policy-fields). |

### Other options pages

| Page | Complete field reference | Use it when |
| --- | --- | --- |
| Load balancing | [Meter, phase and fuse settings](load-balancing.md#options-reference) | Enabling the observer or configuring physical phase protection support. |
| Price optimization | [Price source and scheduling settings](price-optimization.md#source-and-timing-options) | Selecting a price sensor, horizon or optional aggregator source. |
| Energy forecast | [Forecast entities](energy-forecast.md#configure-the-forecast) | Adding a solar forecast, inverter production or outdoor temperature. |

There is no integration YAML configuration to copy into `configuration.yaml`.
Examples in this guide describe UI settings, dashboard cards or read actions.

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

[← Guide contents](README.md) · [Next: control authority and policy →](control-policy.md)
