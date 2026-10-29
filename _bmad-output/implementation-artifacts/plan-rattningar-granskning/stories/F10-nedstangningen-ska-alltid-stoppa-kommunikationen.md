---
title: 'Nedstängningen ska alltid stoppa kommunikationen'
type: 'bugfix'
created: '2026-10-29'
status: 'done'
baseline_revision: 'e8f732752a8c054e6d51d8cc1edf0d4489aebcc1'
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: []
deferred:
  - summary: >-
      Plattformsavlastning avbryts när runtime.stop kastar, trots att lokal kommunikation har stoppats.
    evidence: |-
      async_unload_entry tar bort runtime ur hass.data och inväntar stop före async_unload_platforms. Samma avbrott fanns före F10 när ett stoppsteg kastade; F10 isolerar lokala stoppsteg men ändrar inte avlastningsordningen.
    location: >-
      custom_components/pixiihome_controller/__init__.py:591
    severity: medium
---

<intent-contract>

## Intent

**Problem:** Ett fel när en regulator stoppas avbryter `RuntimeData.stop()` innan koordinator, journal, kommandohanterare och MQTT-transport har avslutats. Efter avlastning kan kommunikation och bakgrundsarbete därför finnas kvar.

**Approach:** Försök varje stoppsteg oberoende, redovisa varje fel och fortsätt genom hela kedjan. Bevara första felet efter avslutad städning så att misslyckad avlastning inte ser lyckad ut.

## Boundaries & Constraints

**Always:** Kör varje befintligt stoppsteg i ordning, även efter ett tidigare fel. Koordinator, journal-flush, kommandohanterare och transport måste alltid försökas. Logga åtgärd och orsak med `log_activity` samt varje fel i vanlig logg. Bevara första felet för anroparen.

**Never:** Kontakta live-systemet vid verifiering, ändra externa scheman, skicka om osäkra anrop eller refaktorera andra stories.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Normalt stopp | Alla komponenter stoppar | Samtliga stopp anropas i ordning | Inget fel |
| Regulatorfel | `meter_limit_load_balancer.stop()` kastar | Koordinator, journal, kommandohanterare och transport stoppas ändå | Felet loggas och kastas sist |
| Flera fel | Regulator och journal-flush kastar | Senare stopp försökta; båda fel redovisade | Första felet kastas sist |

</intent-contract>

## Code Map

- `custom_components/pixiihome_controller/runtime.py` — `RuntimeData.stop` omkring rad 1896–1918 stoppar komponenter sekventiellt utan felisolering; `async_flush_control_journal` omkring rad 1877 kan kasta. `log_activity` används redan i samma fil vid behörighetsändring.
- `custom_components/pixiihome_controller/activity.py` — `log_activity(runtime, status_key, message, cause=...)` är felsäker; läsbevis för loggning, ingen ändring.
- `custom_components/pixiihome_controller/__init__.py` — `async_unload_entry` omkring rad 585 tar bort runtime före `stop`; läsbevis för påverkan, ingen ändring.
- `tests/test_runtime_scheduler.py` — befintlig `_configured_runtime`, `RuntimeData` och stoppanrop; lägg till isolerade tester med ersatta stoppsteg.

## Tasks & Acceptance

**Execution:**
- `custom_components/pixiihome_controller/runtime.py` — isolera varje stoppsteg, logga orsak och fel, kasta första felet efter kedjan.
- `tests/test_runtime_scheduler.py` — bevisa fortsatt stopp efter regulatorfel och efter flera fel, inklusive avlastningsvägen, ordning och första felet.

**Acceptance Criteria:**
- Given en startad runtime vars lastbalansering kastar vid stopp, when integrationen avlastas, then kommandohanterare och MQTT-transport får sina stoppanrop och felet redovisas till anroparen.
- Given två olika stoppfel, when runtime stoppas, then varje återstående steg har försökts och båda felen finns i loggen.

## Spec Change Log

## Review Triage Log

### 2026-10-29 — Review pass
- verdicts: 2 findings — high 0, medium 2, low 0, false 0, maybe-false 0
- findings:
  - `[medium]` `[defer]` Ett stoppfel avbryter `async_unload_entry` före plattformsavlastning — `__init__.py:591–597` gör detta även före F10; separat fel i avlastningsvägen, medan F10:s lokala kommando- och MQTT-stopp nu ändå körs.
  - `[medium]` `[patch]` Avlastningstestet kontrollerade bara anrop till ersatta stoppmetoder — `tests/test_runtime_scheduler.py` kör nu verklig `CommandManager` och `PixiiMqttTransport` med fejkade prenumerationer och verifierar att samtliga avslutas.

## Verification

**Commands:**
- `.venv/review-environment/Scripts/python.exe -m pytest -q tests/test_runtime_scheduler.py` — relevanta tester passerar.
- `.venv/review-environment/Scripts/python.exe -m ruff check custom_components/pixiihome_controller/runtime.py tests/test_runtime_scheduler.py` — inga lintfel.
- `.venv/review-environment/Scripts/python.exe -m pyright` — inga typfel.

## Auto Run Result

Status: done

**Sammanfattning:** `RuntimeData.stop()` försöker varje befintligt stoppsteg i ordning trots fel, loggar varje fel och nedstängningens orsak, och kastar första felet efter sista steget.

**Ändrade filer:**
- `custom_components/pixiihome_controller/runtime.py` — oberoende stoppsteg och aktivitets-/felloggning.
- `tests/test_runtime_scheduler.py` — normalt stopp, ett eller två stoppfel och verklig lokal prenumerationsavslutning genom avlastningsvägen.

**Granskning:** Ett mediumfynd lappades; ett mediumfynd om redan befintlig plattformsavlastning sköts upp och finns i `deferred`. Inga fynd avvisades. Ytterligare uppföljande granskning rekommenderas inte (`followup_review_recommended: false`); patchade fynd: high 0, medium 1, low 0.

**Verifiering:** `pytest -q tests/test_runtime_scheduler.py` gav 32 godkända tester. `ruff check` på de två ändrade Pythonfilerna passerade. `pyright` gav 0 fel och 0 varningar. Kommandona kördes via `.venv/review-environment/Scripts/python.exe -m`.

**Kvarvarande risk:** Ett undantag från `stop()` avbryter plattformsavlastning i `async_unload_entry`; det befintliga beteendet ligger utanför denna stories stoppkedja. Ingen live-verifiering gjordes enligt kontraktet.
