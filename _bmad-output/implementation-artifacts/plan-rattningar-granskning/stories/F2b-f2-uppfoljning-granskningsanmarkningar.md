---
title: 'F2-uppföljning – granskningsanmärkningar'
type: 'bugfix'
created: '2026-10-29'
status: 'done'
baseline_revision: 'c4e1cad8140cadc6127da9a14c61f68eb54c57e6'
review_loop_iteration: 1
followup_review_recommended: false
context: []
warnings: []
deferred: []
---

<intent-contract>

## Intent

**Problem:** F2:s återställningsfel kan ge upprepade aktivitetsrader, en kvarstående städavisering efter återhämtning och en tyst förlorad felorsak. Misslyckad journalsparning kan dölja att tjänst 120 fortfarande är aktiv.

**Approach:** Gör aktivitetsloggen ändringsstyrd, avfärda städaviseringen efter lyckad städning, bevara undantagets orsak och avisera även när journalsparning misslyckas. Rätta det missvisande testnamnet och feltexten.

## Boundaries & Constraints

**Always:** Ett test per punkt 1–4 i CAP-F2b; logga handling och orsak. Behåll lease vid återställningsfel och journalens fail-closed-beteende.

**Never:** Rör live-systemet, andra stories eller externa scheman. Skicka inte om osäkra Gateway-skrivningar utan tillståndsläsning.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Upprepad konflikt | Två avstängda tick med samma återställningsfel | En aktivitetsrad för `recovery_required` | Status och orsak kvarstår |
| Ändrad orsak | Nytt återställningsfel efter första | En ny aktivitetsrad | Ny orsak synlig |
| Senare städning | Tidigare `authority_cleanup`-avisering, därefter lyckad återställning | Aviseringen avfärdas | Ingen kvarstående varning |
| Oväntat fel | `OSError` vid `deactivate()` | Felorsaken loggas och syns i städproblem | Behörigheten stängs ändå |
| Sparfel efter återställningsfel | `deactivate()` och journalsparning misslyckas | Avisering om möjlig kvarvarande tjänst 120 | Fail-closed och ursprungligt sparfel bevaras |

</intent-contract>

## Code Map

- `custom_components/pixiihome_controller/meter_limit_controller.py` — `tick()` rad 283–313 loggar varje fel vid avstängd styrning; jämför föregående status/orsak före uppdatering. `deactivate()` rad 363–398 är den normala senare återställningsvägen i avstängd observatör.
- `custom_components/pixiihome_controller/runtime.py` — `async_set_production_control_enabled()` rad 667–750 fångar `deactivate`, sparar journal och skapar `authority_cleanup`; felvägen rad 715–719 behöver avisering före återkast. Bevara andra städproblem om lastbalanseringen återställs senare.
- `tests/test_authority_release.py` — fake-runtime rad 46–173 täcker avstängning och städproblem; rätta missvisande testnamn och feltext, utöka med tre runtimefall.
- `tests/test_controller_coexistence.py` — befintliga avstängda tick-tester kan utökas för ändringsstyrd aktivitetslogg.

## Tasks & Acceptance

**Execution:**
- `custom_components/pixiihome_controller/meter_limit_controller.py` — logga återställningsbehov bara när status eller orsak ändras; meddela runtime när ett senare avstängt observationsvarv återställt leasen.
- `custom_components/pixiihome_controller/runtime.py` — logga oväntat undantags orsak, avisera om möjlig kvarvarande tjänst 120 även vid journalsparfel och uppdatera eller avfärda avisering efter lyckad senare återställning. Bevara avisering för andra kvarvarande städproblem.
- `tests/test_authority_release.py` — lägg till separata tester för avisering, felorsak och journalsparfel; rätta namn och feltext.
- `tests/test_controller_coexistence.py` — testa ändringsstyrd logg för återkommande och ändrad orsak.

**Acceptance Criteria:**
- Given en avstängd kontroll med kvarvarande lease, when samma återställningsfel inträffar i flera observationsvarv, then loggas återställningsbehov en gång och loopen fortsätter.
- Given en tidigare avisering om ofullständig städning, when observatörens senare avstängda `tick()` återställer den ägda tjänst 120-posten, then avfärdas aviseringen om inga andra städproblem kvarstår; annars uppdateras den till de kvarstående problemen.
- Given ett oväntat undantag vid återställning, when behörigheten stängs av, then aktivitetsloggen och aviseringen visar orsak utan att förhindra avstängningen.
- Given återställningsfel följt av journalsparfel, when användaren försöker stänga av, then fail-closed gäller, sparfelet kastas och aviseringen nämner möjlig kvarvarande tjänst 120.

## Spec Change Log

### 2026-10-29 — Observatörens återhämtning
- Granskningen visade att den första implementationen avfärdade aviseringen först efter ett nytt administratörsanrop. Den normala återhämtningen sker i avstängd `tick()`.
- Uppgifterna och acceptanskriteriet anger nu observatörens senare återställning och att andra städproblem behålls.
- KEEP: Ändringsstyrd logg, synlig OSError-orsak, avisering före återkast vid journalsparfel och fail-closed.

## Review Triage Log

### 2026-10-29 — Review pass
- verdicts: 1 finding — high 0, medium 1, low 0, false 0, maybe-false 0
- findings:
  - `[medium]` `[bad_spec]` Aviseringen avfärdas bara vid ett nytt administratörsanrop; normalt senare `tick()` lämnar den kvar — `runtime.py` har avfärdning endast i `async_set_production_control_enabled()`, medan `meter_limit_controller.py` återställer leasen i avstängd `tick()`; specens uppgifter och acceptanskriterium har preciserats.

### 2026-10-29 — Andra granskningspasset
- verdicts: 3 findings — high 0, medium 2, low 1, false 0, maybe-false 0
- findings:
  - `[medium]` `[patch]` Ensamt Service 120-problem saknade avfärdningstest — testet anropar nu `_meter_limitation_restored()` med enbart detta problem och kontrollerar notifierings-ID.
  - `[medium]` `[patch]` Intentionens avfärdning efter observatörens återhämtning var bara delvis verifierad — samma test bekräftar avfärdningen och controller-testet bekräftar anropet från `tick()`.
  - `[low]` `[patch]` Konfliktvägen använde annat aktivitetsmeddelande än CAP-F2b — `deactivate()` loggar nu `Load balancing restore needs recovery` endast vid ändrad status eller orsak.

## Verification

**Commands:**
- `pytest -q tests/test_authority_release.py tests/test_controller_coexistence.py` — alla tester passerar.
- `ruff check custom_components/pixiihome_controller/runtime.py custom_components/pixiihome_controller/meter_limit_controller.py tests/test_authority_release.py tests/test_controller_coexistence.py` — inga fel.
- `pyright` — inga typfel.

## Auto Run Result

Status: done

**Sammanfattning:** Återställningsloggningen är ändringsstyrd, oväntade fel visar sin orsak, städaviseringen uppdateras efter observatörens återställning och visas även vid misslyckad journalsparning. Missvisande testnamn och feltext är rättade.

**Ändrade filer:**
- `custom_components/pixiihome_controller/meter_limit_controller.py` — ändringsstyrd logg och återhämtningssignal efter avstängt varv.
- `custom_components/pixiihome_controller/runtime.py` — städaviseringens livscykel, synlig felorsak och bevarat ursprungligt journalsparfel.
- `tests/test_authority_release.py` — avisering, felorsak, journalsparfel och rättad testtext.
- `tests/test_controller_coexistence.py` — återkommande fel, orsaksskifte och senare återställning.

**Granskning:** Första passet gav ett mediumfynd som korrigerades via spec och ny implementation. Andra passet gav två mediumanmärkningar med samma testlucka och ett lågt meddelandefynd; alla lappades. Inget sköts upp eller avvisades. Lappade grupper i sista passet: medium 1, low 1. Ytterligare uppföljande granskning rekommenderas inte.

**Verifiering:** `pytest -q tests/test_authority_release.py tests/test_controller_coexistence.py` gav 36 godkända tester. Ruff på de fyra berörda filerna passerade. Pyright gav 0 fel och 0 varningar. `git diff --check` körs inför commit.

**Kvarvarande risk:** Tester använder fakes för Home Assistants aviseringar; inget live-system har kontaktats. En avisering som överlever en omstart tillsammans med andra städproblem kan inte rekonstrueras helt från minneslistan i denna story.
