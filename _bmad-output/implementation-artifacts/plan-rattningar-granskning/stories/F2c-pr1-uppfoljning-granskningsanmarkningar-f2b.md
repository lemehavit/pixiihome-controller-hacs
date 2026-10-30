---
title: 'PR1-uppföljning – granskningsanmärkningar F2b'
type: 'bugfix'
created: '2026-10-29'
status: 'done'
baseline_revision: '2e63c19a3645630a47805bd1ea2841b16813881c'
review_loop_iteration: 0
followup_review_recommended: true
context: []
warnings: []
deferred: []
---

<intent-contract>

## Intent

**Problem:** Återställningsfel kan läcka fri feltext till sensorns maskinläsbara orsak, ett tomt `ValueError` kan ge tom orsak, och en saknad eller osparbar journal kan ge en missvisande eller utebliven varning om tjänst 120.

**Approach:** Håll sensorns orsak stabil, visa feltext i logg och avisering, rapportera kvarstående återställningsfel även när journalen saknas och formulera sparfelsaviseringen utifrån behörighetens faktiska lagringsstatus.

## Boundaries & Constraints

**Always:** Ett test per punkt 1–4 i CAP-F2c. Behåll fail-closed och återställningsförsökets observerbara orsak. Versionen är 0.5.21; uppdatera CHANGELOG. Kör begärda verifieringar. Pusha PR1, slå ihop PR1 i PR2 med merge-commit, testa och pusha PR2.

**Never:** Rör inte live-systemet, andra stories, main eller release. Ingen rebase eller force-push.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Oväntat fel | Observatörens `deactivate()` ger `OSError("read failed")` | `control_blocked_reason` är `meter_limitation_restore_failed`; logg/avisering bär `read failed` | Observatören fortsätter |
| Tomt ValueError | Observatörens `deactivate()` ger `ValueError("")` | Reservorsaken `meter_limitation_restore_failed` används | Ingen tom orsak |
| Saknad journal | Återställning misslyckas och `control_journal is None` | Avisering om tjänst 120 visas före `HomeAssistantError` | Felet kastas fortfarande |
| Sparfel | Återställning och journalsparning misslyckas | Avisering säger att behörigheten endast stängts av i minnet, inte sparats | Ursprungligt sparfel kastas; gate fail-closed |

</intent-contract>

## Code Map

- `custom_components/pixiihome_controller/meter_limit_controller.py` — `tick()` kring rad 296–318 sätter `reason` från undantaget; separera stabil statuskod från fri feltext och förenkla ValueError-grenen. `attributes()` exponerar orsaken.
- `custom_components/pixiihome_controller/runtime.py` — `async_set_production_control_enabled()` kring rad 688–745 hanterar misslyckad återställning, saknad journal och sparfel; `_update_authority_cleanup_notification()` kring rad 752 formulerar nu aviseringen som om avstängning sparats. Undvik att ändra annan journalhantering.
- `tests/test_controller_coexistence.py` — observatörstestet kring rad 598–663 granskar sensorstatus efter `OSError`; lägg till separat tomt-ValueError-test och kontrollera loggad feltext.
- `tests/test_authority_release.py` — fake-runtime och notifieringstester kring rad 46–300 kan utökas för saknad journal och sparfel; det synkrona testet kring rad 189 har felaktig asyncio-markering.
- `CHANGELOG.md` — befintlig 0.5.21-post på rad 3; lägg till F2c utan versionshöjning.

## Tasks & Acceptance

**Execution:**
- `custom_components/pixiihome_controller/meter_limit_controller.py` — använd stabil felkod för oväntade fel och reservorsak för tomt ValueError; behåll fritext i aktivitetsorsak.
- `custom_components/pixiihome_controller/runtime.py` — avisera om kvarstående tjänst 120 före fel vid saknad journal och beskriv osparad avstängning rätt vid sparfel.
- `tests/test_controller_coexistence.py` — testa oväntat fel respektive tomt ValueError var för sig.
- `tests/test_authority_release.py` — testa saknad journal och sparfel var för sig; ta bort felaktig markering på synkront test.
- `CHANGELOG.md` — dokumentera F2c under 0.5.21.

**Acceptance Criteria:**
- Given avstängd styrning med kvarvarande lease, when oväntat fel eller tomt ValueError inträffar, then sensorns blockeringsorsak är den stabila reservkoden och observatören lever vidare.
- Given misslyckad återställning av tjänst 120, when journalen saknas, then användaren får återställningsavisering innan felet kastas.
- Given misslyckad återställning och journalsparning, when behörigheten stängs i minnet, then aviseringen anger att avstängningen inte sparats och ursprungligt sparfel kastas.

## Spec Change Log

## Review Triage Log

### 2026-10-29 — Review pass
- verdicts: 3 findings — high 0, medium 3, low 0, false 0, maybe-false 0
- findings:
  - `[medium]` `[patch]` Ändrad fritext för oväntat återställningsfel försvann när sensorkoden var oförändrad — senaste detaljen sparas nu separat och testet kontrollerar två olika fel och en identisk upprepning.
  - `[medium]` `[patch]` Observatörens senare notifieringsuppdatering kunde felaktigt säga att avstängningen var sparad — sparstatus behålls i runtime och testas när ett annat städproblem kvarstår.
  - `[medium]` `[patch]` Saknad journal kunde ge avisering om minnesavstängning medan gate ännu var öppen — fail-closed körs före avisering och fel; testet kontrollerar ordningen och gate-statusen.

## Verification

**Commands:**
- `pytest -q tests/test_authority_release.py tests/test_controller_coexistence.py` — berörda tester passerar.
- `ruff check .` — inga lintfel.
- `pyright` — inga typfel.
- `pytest` — hela begärda sviten passerar.
- `python scripts/build_release.py --check` — releasekontrollen passerar.

## Auto Run Result

Status: done

**Sammanfattning:** Sensorn använder stabila återställningskoder och aktivitetsloggen behåller varje ny felorsak. Saknad eller osparbar journal stänger styrgate och ger korrekt varning om tjänst 120 och avstängningens lagringsstatus. Synkront test saknar nu asyncio-markering.

**Ändrade filer:** `meter_limit_controller.py` (orsak och fellogg), `runtime.py` (fail-closed och avisering), `test_controller_coexistence.py` och `test_authority_release.py` (regressionstester), `CHANGELOG.md` (F2c-post), denna storyspec (resultat).

**Granskning:** Tre medelstora fynd lappades: ändrad feltext som försvann ur loggen, tappad sparstatus vid notifieringsuppdatering och öppen gate vid saknad journal. Inga fynd sköts upp eller avvisades. Lappade grupper: medium 3. En uppföljande granskning rekommenderas eftersom notifieringens livscykel endast verifierats med Home Assistant-fakes, inte i körande HA.

**Verifiering:** Berörda tester passerade (46); hela pytest-sviten passerade (1 477). `ruff check .`, `pyright` (0 fel), `python scripts/build_release.py --check` (0.5.21) och `git diff --check` passerade via projektets Python-miljö där det behövdes.

**Kvarvarande risk:** Ingen live-verifiering enligt storyns begränsning.
