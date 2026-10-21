# Pixiihome Controller

![Pixiihome Controller](custom_components/pixiihome_controller/brand/icon.png)

[![CI](https://github.com/lemehavit/pixiihome-controller-hacs/actions/workflows/ci.yml/badge.svg)](https://github.com/lemehavit/pixiihome-controller-hacs/actions/workflows/ci.yml)
[![Validate](https://github.com/lemehavit/pixiihome-controller-hacs/actions/workflows/validate.yml/badge.svg)](https://github.com/lemehavit/pixiihome-controller-hacs/actions/workflows/validate.yml)
[![HACS custom repository](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://www.hacs.xyz/docs/faq/custom_repositories/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Monitor a Pixii PowerShaper system locally in Home Assistant, inspect its schedules,
and configure guarded battery controllers. An isolated simulator lets you explore
the integration without a Gateway or a physical battery.

This is an **independent community integration, currently in beta**. It is not
affiliated with or endorsed by Pixii, Home Assistant or an electricity aggregator.

## Requirements

- Home Assistant **2026.9.3 or newer**. This is the tested baseline, not a promise
  that every subsequent Home Assistant or Gateway version has been tested.
- For a physical Gateway: Home Assistant's MQTT integration, connected to the
  broker carrying your Gateway's Pixii status messages, and the Gateway serial.
- For scheduler access: local HTTPS access, the Gateway owner's credentials and
  exact trusted 32-character bootstrap ID. Some Gateways also need their public CA
  certificate. See [configuration](docs/configuration.md).
- For price optimization: a compatible price entity in Home Assistant. SEK/kWh
  and öre/kWh are supported; no price-provider account is connected by this integration.

The simulator needs no MQTT connection, Gateway credentials or external services.
Control features require suitable meter, battery and phase telemetry; unavailable
or stale safety inputs block the corresponding operation.

## Install with HACS

This integration is available through a **HACS custom repository**. It has not
been included in the default HACS catalog.

[![Open repository in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=lemehavit&repository=pixiihome-controller-hacs&category=integration)

1. Open **HACS → ⋮ → Custom repositories**.
2. Add `https://github.com/lemehavit/pixiihome-controller-hacs` with type **Integration**.
3. Find **Pixiihome Controller** in HACS and download it.
4. Restart Home Assistant.
5. Open **Settings → Devices & services → Add integration**, search for
   **Pixiihome Controller**, then choose **Production Gateway** or **Local Simulator**.
6. For a production entry, enter the serial that appears in your MQTT status topics.
   Confirm that telemetry is current before configuring scheduler access or control.

For manual installation, download `pixiihome_controller.zip` from a
[release](https://github.com/lemehavit/pixiihome-controller-hacs/releases), extract
its contents into `/config/custom_components/pixiihome_controller/`, then follow
steps 4–6. The manifest must be directly inside that folder.

## Features

- Battery state of charge, usable energy, battery health, power, grid and system status.
- Read-only scheduler calendar and schedule details when HTTPS access is configured.
- Native Home Assistant sensors, switches, buttons and settings; optional Lovelace card.
- Guarded adaptive peak shaving, solar charging, load balancing and price planning.
- Separate switches for production control authority, live price schedule writes
  and price export. New installations start with control switches off.
- An MQTT-independent simulator, previews, diagnostics and a durable command journal.

Configure the optional card as `type: custom:pixiihome-control-card`. The device page
and native entities work without it. The integration registers the card resource.

## Control status and limitations

Start with monitoring. Production authority is persisted: after you enable it,
do not assume a restart disables it. Controllers have additional independent gates.
See [configuration and control](docs/configuration.md) before enabling them.

Controls use typed commands, ownership checks, fresh telemetry and reconciliation.
Unknown ownership or an uncertain response blocks further writes. An external
schedule or support service is not proof that this integration owns a command.
Optional aggregator schedule relocation is disabled unless a source ID and the
required authority are explicitly configured; leave it disabled to preserve those schedules.

Hardware verification is limited. A bounded 300 W zero-export test observed own
discharge and confirmed output stopped after deleting the exact owned interval.
That test used a temporary identity-bound SOC threshold; a natural battery SOC
boundary crossing and the separate higher-priority handoff were **not verified**.
The latter was deferred because the Gateway retained an identity reservation.
An idle output does not prove the schedule identity has been released.

Gateway firmware behavior can differ. No FCR/FFR override or physical FCR/FFR test
is claimed. Administrative validation actions can move real power and are not
installation or troubleshooting steps. There are no automatic physical tests.

## Help, updates and removal

- [Configure the Gateway and controllers](docs/configuration.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Contribute and run offline tests](CONTRIBUTING.md)
- [Changes](CHANGELOG.md) · [Report a bug](https://github.com/lemehavit/pixiihome-controller-hacs/issues)
- [Report a security issue privately](SECURITY.md)

Back up Home Assistant before updating. Download updates through HACS and restart
Home Assistant. Review the changelog before enabling new control behavior.

Before removing the integration, disable its automatic controllers and verify
that its owned operations have ended. Turning off authority blocks new commands;
it does not establish that an existing native Gateway interval has stopped.
Remove the config entry under **Devices & services**, remove its files through
HACS, then restart. External schedules are managed at their source.

## License

[MIT](LICENSE). The project icon is original artwork for this community integration.
Pixii and other product names remain the property of their respective owners.
