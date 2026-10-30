---
title: 'Kvarvarande tjänst 120 / fasstöd efter omstart när styrningen är av'
type: 'bugfix'
created: '2026-10-29'
status: 'done'
baseline_revision: 'e12f825729324ac0424dd7c2220c71c45d8ef8db'
review_loop_iteration: 0
followup_review_recommended: true
context: []
warnings: []
deferred: []
---

<intent-contract>

## Intent

**Problem:** Vid omstart med avstängd styrning läses leaser för tjänst 120 och fasstöd inte in. En kvarvarande Gateway-effekt kan därför sakna synlig återställningsstatus och aldrig städas.

**Approach:** Läs båda leaser utan Gateway-skrivning före observationsloopen. Låt avstängd `tick()` använda den befintliga identitetskontrollerade återställningen, visa konflikt som `recovery_required` och logga varför en sparad effekt städas.

## Boundaries & Constraints

**Always:** Läs sparad lease även när switchen är av. Bevara lease vid konflikt eller osäkert utfall; läs Gateway-tillstånd innan varje återställningsskrivning. Logga åtgärd och orsak med `log_activity` i ändrade funktioner. Testa utan live-system.

**Never:** Aktivera ny tjänst eller fasstyrning vid avstängd behörighet; skriv över annan aktörs Gateway-post; ändra externa scheman eller andra stories.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Ägd native-post | Sparad lease, switch av, Gateway visar ägd post | Original återställs via avstängt varv; lease rensas; orsak loggas | Läser identitet före skrivning |
| Ändrad native-post | Sparad lease, switch av, Gateway visar annan post | Ingen skrivning; lease kvar; sensor visar `recovery_required` | Konfliktorsak exponeras |
| Sparat fasstöd | Sparad faslease, switch av | Fas-Gateway skapas och ägd effekt städas via avstängt varv | Konflikt lämnar lease och visas |
| Tomma leaser | Switch av, ingen sparad lease | Inga Gateway-skrivningar; status avstängd | Ingen återställning behövs |

</intent-contract>

## Code Map

- `custom_components/pixiihome_controller/__init__.py` — `async_setup_entry` omkring 522–572 skapar regulatorn och anropar `prepare()` bara när switchen är på. Observatörens `start()` anropas redan oberoende av switchen; dess egen `config.enabled` styr om loopen körs.
- `custom_components/pixiihome_controller/meter_limit_controller.py` — `_recover_saved_lease` omkring 125–148 blandar inläsning och Gateway-skrivning; `prepare` omkring 150 använder den. `tick` omkring 269–297 kör `deactivate` när behörigheten är av. `deactivate` omkring 344–367 läser posten och skriver bara om den fortfarande är ägd.
- `custom_components/pixiihome_controller/idle_phase_controller.py` — `_recover` omkring 59–70 läser och släpper leasen direkt; `prepare` sätter `PhaseGateway`; `stop`/`release` omkring 233–260 gör identitetskontrollerad städning. Dela inläsning från skrivning för avstängd start.
- `custom_components/pixiihome_controller/load_balancing_observer.py` — `start` omkring 67 kräver `config.enabled`; `_run` omkring 134 anropar regulatorns `tick`. Läs för återställningsvägen; ändra endast om sparad lease annars saknar körande städloop.
- `tests/test_controller_coexistence.py` — befintliga avstängda varv, konflikter, sensor och observer; lägg till omstart med bestående Store-bytes för båda lease-typerna.
- `tests/test_runtime_scheduler.py` — `MemoryStore` och uppstartsfixture visar kontrolljournalens omstart; återanvänd för uppstartsregression om möjligt.

## Tasks & Acceptance

**Execution:**
- `custom_components/pixiihome_controller/meter_limit_controller.py` — läs sparad native-lease och faslease utan Gateway-skrivning i en separat uppstartsväg; logga varför återställning behövs.
- `custom_components/pixiihome_controller/idle_phase_controller.py` — låt fasleasen läsas och `PhaseGateway` skapas utan att återställning sker innan avstängt varv.
- `custom_components/pixiihome_controller/__init__.py` — anropa läsvägen när switchen är av, håll observatören igång för sparad lease och exponera återställningsfel på sensorn.
- `tests/test_controller_coexistence.py` — testa omstart med ägd respektive ändrad native-post, sparat fasstöd och tomma leaser; verifiera skrivningar, lease, sensor och logg.
- `tests/test_runtime_scheduler.py` — testa att verklig uppstart med avstängd styrning kör läsvägen och inte aktiverar ny styrning.

**Acceptance Criteria:**
- Given sparad native-lease och avstängd styrning vid uppstart, when observatörens första varv körs, then ägd Gateway-post återställs till originalet och leasen rensas efter identitetsläsning.
- Given sparad lease vars Gateway-post ändrats av annan aktör, when ett avstängt varv körs, then ingen skrivning görs och sensorn visar `recovery_required` med konfliktorsak.
- Given sparad faslease och avstängd styrning vid uppstart, when avstängt varv körs, then fasstödet återställs endast efter identitetskontroll och leasen rensas.

## Spec Change Log

## Review Triage Log

### 2026-10-29 — Review pass
- verdicts: 2 findings — high 0, medium 2, low 0, false 0, maybe-false 0
- findings:
  - `[medium]` `[patch]` Samtidig återställning av native- och faslease saknade körd verifiering — `tests/test_controller_coexistence.py` testar nu båda sparade leaserna i samma avstängda varv och att båda journalerna rensas.
  - `[medium]` `[patch]` Testerna prövade uppstart, observatör och återställning var för sig i stället för som ett flöde — `tests/test_runtime_scheduler.py` kör nu `async_setup_entry`, observatörens första varv, Gateway-skrivning, journalsparning och sensorstatus tillsammans.

## Verification

**Commands:**
- `.venv/review-environment/Scripts/python.exe -m pytest -q tests/test_controller_coexistence.py tests/test_runtime_scheduler.py` — relevanta regressionstester passerar.
- `.venv/review-environment/Scripts/python.exe -m ruff check custom_components/pixiihome_controller/__init__.py custom_components/pixiihome_controller/meter_limit_controller.py custom_components/pixiihome_controller/idle_phase_controller.py tests/test_controller_coexistence.py tests/test_runtime_scheduler.py` — inga lintfel.
- `.venv/review-environment/Scripts/python.exe -m pyright` — inga typfel.

## Auto Run Result

Status: done

**Sammanfattning:** Uppstart med avstängd styrning läser sparade leaser för tjänst 120 och fasstöd utan Gateway-anrop. Observatörens avstängda varv gör identitetsverifierad återställning och visar konflikter som `recovery_required`.

**Ändrade filer:**
- `custom_components/pixiihome_controller/__init__.py` — läser sparade leaser vid avstängd uppstart och visar inläsningsfel.
- `custom_components/pixiihome_controller/meter_limit_controller.py` — separerar lease-inläsning från återställning och hanterar faslease utan native-lease.
- `custom_components/pixiihome_controller/idle_phase_controller.py` — läser faslease utan skrivning och kontrollerar identitet före återställning.
- `custom_components/pixiihome_controller/load_balancing_observer.py` — startar återställningsvarv vid sparad lease även om observation är av.
- `tests/test_controller_coexistence.py` — testar ägd post, konflikt, fasstöd, båda leaserna och tomma leaser.
- `tests/test_runtime_scheduler.py` — testar uppstartsläsning och en verklig första observationsiteration.

**Granskning:** Två medelstora verifieringsluckor lappades; inga fynd sköts upp eller avvisades. Lappade fynd: high 0, medium 2, low 0. Ytterligare uppföljande granskning rekommenderas. Kvarvarande oprövad risk är en kombinerad lease där en Gateway-post ändrats av en annan aktör under uppstart.

**Verifiering:** `.venv/review-environment/Scripts/python.exe -m pytest -q tests/test_controller_coexistence.py tests/test_runtime_scheduler.py` gav 52 godkända tester. Ruff på de fem berörda kod- och testfilerna passerade. Pyright gav 0 fel och 0 varningar.

**Begränsning:** Ingen live-verifiering gjordes enligt kontraktet.
