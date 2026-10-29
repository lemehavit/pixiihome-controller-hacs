---
title: 'Avstängd styrbehörighet måste alltid gå igenom'
type: 'bugfix'
created: '2026-10-29'
status: 'done'
baseline_revision: '585a516a78d2a857df5913087c5dbdfcc6307897'
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: []
deferred: []
---

<intent-contract>

## Intent

**Problem:** Ett fel vid återställning av lastbalanseringens tjänst 120 avbryter den globala avstängningen innan behörigheten sparas. Samma fel i ett avstängt observatörsvarv kan avsluta observatörsloopen.

**Approach:** Samla återställningsfelet som ett städproblem, spara avstängningen och avisera om kvarvarande effekt. Låt ett avstängt varv markera återställningsbehov och överleva felet, med den sparade leasen kvar för identitetsverifierat nytt försök nästa varv.

## Boundaries & Constraints

**Always:** Behörighet av ska sparas och `release_gate.live_writes_enabled` bli `False` även vid återställningskonflikt. Logga åtgärd och orsak med `log_activity` i ändrade funktioner. Bevara lease och återställ bara när Gateway-posten identifierats som vår.

**Never:** Rör live-systemet, externa scheman eller andra stories. Skicka inte om en skrivning med osäkert utfall utan tillståndsläsning. Dölj inte ett misslyckat journalsparande som lyckad avstängning.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Normal avstängning | Ägd tjänst 120 kan återställas | Behörighet av, lease rensad, ingen städavisering | Inget fel |
| Konflikt vid avstängning | `deactivate()` signalerar annan ägare | Behörighet av och journal sparad, lease kvar, avisering nämner lastbalanseringen | Felet läggs till `cleanup_problems` |
| Fel i avstängt varv | Lease finns, `deactivate()` kastar | `recovery_required`, begriplig orsak, nästa observationsvarv kan köras | Ingen exception lämnar `tick()` |
| Senare återställning | Gateway-posten åter är identifierbart vår | Nästa varv återställer originalet och rensar lease | Läs före eventuell skrivning |

</intent-contract>

## Code Map

- `custom_components/pixiihome_controller/runtime.py` — `RuntimeData.async_set_production_control_enabled` omkring rad 663–739: `_async_release_owned_control()` samlar städproblem; lastbalanseringens `deactivate()` omkring rad 684 kastar före `_save_control_journal_verified`; slutet loggar och skapar `cleanup incomplete`-avisering. Behåll journalens fail-closed-väg.
- `custom_components/pixiihome_controller/meter_limit_controller.py` — `MeterLimitLoadBalancer.tick` omkring rad 269–281: avstängd gren anropar `deactivate()` utan felhantering. `deactivate` omkring rad 327–351 gör identitetskontroll, lämnar lease vid konflikt och sätter redan `recovery_required`.
- `custom_components/pixiihome_controller/load_balancing_observer.py` — `_run` anropar `controller.tick()` utan fångst; därmed måste avstängd `tick` hålla observatörsloopen vid liv. Läsbevis, ändra inte observatören för denna story.
- `tests/test_authority_release.py` — befintlig fake-runtime och test för städproblem, journalsparande och avisering; utöka för lastbalanseringskonflikt.
- `tests/test_controller_coexistence.py` — befintliga isolerade controller-tester; lägg till avstängd `tick` och senare identitetsverifierad återställning.

## Tasks & Acceptance

**Execution:**
- `custom_components/pixiihome_controller/runtime.py` — fånga återställningsfel från lastbalanseringen som städproblem och fortsätt journalförd avstängning; avisera vad som kan vara kvar.
- `custom_components/pixiihome_controller/meter_limit_controller.py` — fånga fel från `deactivate()` i avstängd `tick`, sätt återställningsstatus och orsak, logga orsaken, låt nästa varv försöka igen.
- `tests/test_authority_release.py` — testa konflikt, sparad avstängning, stängd gate och avisering.
- `tests/test_controller_coexistence.py` — testa att två avstängda varv överlever konflikt och att senare säkert återställningsvarv rensar lease.

**Acceptance Criteria:**
- Given en aktiv ägd lastbegränsning med återställningskonflikt, when användaren stänger av produktionsbehörighet, then API-anropet avslutas med sparad avstängning, stängd skrivgate och avisering som nämner lastbalanseringen.
- Given en sparad lease och avstängd behörighet, when observatörsloopen möter ett `deactivate()`-fel, then loopen fortsätter och sensorns kontrollstatus visar `recovery_required` med orsak.
- Given en senare Gateway-post som kan identifieras som vår, when nästa avstängda observationsvarv körs, then originalet återställs och leasen rensas.

## Spec Change Log

## Review Triage Log

### 2026-10-29 — Follow-up review pass
- verdicts: 1 finding — high 0, medium 1, low 0, false 0, maybe-false 0
- findings:
  - `[medium]` `[patch]` Ett annat återställningsfel än ägarkonflikt saknade verifiering av avstängd behörighet — `tests/test_authority_release.py` testar nu även `OSError`, sparad avstängning, stängd skrivgate, behållen lease och avisering.

### 2026-10-29 — Review pass
- verdicts: 3 findings — high 1, medium 2, low 0, false 0, maybe-false 0
- findings:
  - `[high]` `[patch]` Testet för avstängning kontrollerade inte sparat behörighetsbeslut — `tests/test_authority_release.py` behåller nu kandidaten som skickas till sparning och hävdar att `production_control_authority.enabled` är `False`.
  - `[medium]` `[patch]` Ett annat återställningsfel än ägarkonflikt kunde avsluta observatören utan att testet reagerade — `tests/test_controller_coexistence.py` provar nu `OSError`, kvarhållen lease och säkert senare återförsök.
  - `[medium]` `[patch]` Testerna stannade vid controller-attribut i stället för observatörsloop och sensor — ett nytt test kör `_run` över felet och läser sensorns status och orsak före och efter nästa varv.

## Verification

**Commands:**
- `pytest -q tests/test_authority_release.py tests/test_controller_coexistence.py` — samtliga tester passerar.
- `ruff check custom_components/pixiihome_controller/runtime.py custom_components/pixiihome_controller/meter_limit_controller.py tests/test_authority_release.py tests/test_controller_coexistence.py` — inga lintfel.
- `pyright` — inga typfel.

## Auto Run Result

Status: done

**Uppföljande granskning:** En mediumbrist i testtäckningen lappades med `OSError` för återställningsfelet. Inga fynd sköts upp eller avvisades. Patchade fynd i detta pass: high 0, medium 1, low 0. Ytterligare uppföljande granskning rekommenderas inte.

**Uppföljande verifiering:** `pytest -q tests/test_authority_release.py tests/test_controller_coexistence.py` gav 17 godkända tester; `ruff check` på de fyra berörda kod- och testfilerna passerade; `pyright` gav 0 fel och 0 varningar. Alla kördes via `.venv/review-environment/Scripts/python.exe -m`.

**Kvarvarande risk:** Testet granskar kandidaten till journalen men kör inte ett verkligt journalsparande följt av omstart; det är samma risk som antecknades i första granskningspasset.

**Sammanfattning:** Fel vid återställning av lastbalanseringen redovisas som städproblem medan avstängd styrbehörighet sparas och skrivgaten stängs. Ett avstängt observatörsvarv visar `recovery_required`, behåller leasen och kan försöka igen efter fel.

**Ändrade filer:**
- `custom_components/pixiihome_controller/runtime.py` — samlar återställningsfel och loggar orsak vid avstängning.
- `custom_components/pixiihome_controller/meter_limit_controller.py` — bevarar observatörsloopen och återställningsstatus vid fel.
- `tests/test_authority_release.py` — testar sparad avstängning, konflikt, normal återställning och avisering.
- `tests/test_controller_coexistence.py` — testar konflikt, tillfälligt läsfel, återförsök, observatörsloop och sensor.

**Granskning:** Tre fynd lappades (high 1, medium 2); inga uppskjutna eller avvisade fynd. Uppföljande granskning rekommenderas eftersom ett högt prioriterat fynd åtgärdades. Kvarvarande risk att följa upp: testet granskar kandidaten till journalen, men kör inte ett verkligt journalsparande följt av omstart.

**Verifiering:** `pytest -q tests/test_authority_release.py tests/test_controller_coexistence.py` gav 16 godkända tester; `ruff check` på de fyra ändrade kod- och testfilerna passerade; `pyright` gav 0 fel och 0 varningar. Alla kördes via `.venv/review-environment/Scripts/python.exe -m`.

**Begränsning:** Ingen live-verifiering har gjorts enligt kontraktet.
