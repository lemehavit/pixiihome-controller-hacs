---
title: 'Ändrade aggregatormål ska läsas om'
type: 'bugfix'
created: '2026-10-29'
status: 'done'
baseline_revision: 'dc86734086e0a6cb459522e4a36176aa0a8e122c'
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: []
deferred: []
---

<intent-contract>

## Intent

**Problem:** Prisplaneringen cachar aggregatorns Target SOC efter schemats identitet. När aggregatören ändrar målet för samma schema fortsätter planen använda det gamla värdet.

**Approach:** Läs Target SOC på nytt i varje planeringsvarv. Använd bara det aktuella läsresultatet; om det inte kan läsas är fönstrets resulterande energi okänd.

## Boundaries & Constraints

**Always:** Ett ändrat mål syns vid nästa varv med samma schemaidentitet. Ett misslyckat eller ogiltigt läsresultat ger okänd energi även om ett äldre giltigt värde finns. Logga en upptäckt måländring eller övergång till oläsbart mål med åtgärd och orsak via `log_activity`. Verifiera bara med tester.

**Never:** Ändra externa scheman eller Target SOC i denna läsväg. Återanvänd inte föregående värde när aktuell läsning misslyckas. Kör inget mot live-systemet.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|--------------|---------------------------|----------------|
| Ändrat mål | Samma schema, först 90 %, sedan 20 % | Nästa planeringsvarv visar och använder 20 % | Orsak loggas |
| Oläsbart mål | Samma schema, först 90 %, sedan läsfel eller icke ändligt värde | Nästa varv har okänd energi och använder inte 90 % | Säker planeringsgräns, orsak loggas |
| Oförändrat mål | Samma schema, 90 % i två varv | Målet läses två gånger och är fortsatt 90 % | Ingen ändringslogg |

</intent-contract>

## Code Map

- `custom_components/pixiihome_controller/price_controller.py` — `_load_external_windows` cirka 1188–1265 läser Target SOC via `gateway.client.read_target_soc`; `_target_cache` kring 88 och 1230 återanvänder värdet tills schemaidentiteten ändras. `_external_windows` och `_external_meta` för vidare det lästa målet till planering och eventuell separat ändringsväg. `_adjust_external_targets` tömmer cachen efter egen skrivning kring 1437; ändra inte skrivvägens tillståndsmaskin.
- `tests/test_price_controller.py` — `runtime()` och `external_schedule()` bygger fejkad runtime och schemainventering; `test_known_running_target_soc_allows_writes_only_after_its_end` visar direkt läsning. Placera regression för två efterföljande läsvarv här.
- `_bmad-output/implementation-artifacts/plan-rattningar-granskning/plan-rattningar-granskning.md` — F9 kräver att samma identitet med 90→20 % syns i nästa varv; oläsbart mål är okänd energi. Endast läsning.

## Tasks & Acceptance

**Execution:**
- `custom_components/pixiihome_controller/price_controller.py` — läs målet vid varje `_load_external_windows`-anrop och använd aktuell läsning; behåll bara tidigare värde för ändringslogg, aldrig för planering.
- `tests/test_price_controller.py` — testa 90→20 %, oförändrat mål och läsfel/ogiltigt mål på samma schemaidentitet i två varv.

**Acceptance Criteria:**
- Given ett oförändrat externt schemas identitet vars Target SOC ändras från 90 till 20 %, when nästa planeringsvarv läser schemat, then planens externa fönster använder 20 % och ändringen loggas med orsak.
- Given ett tidigare läsbart mål som inte längre går att läsa, when nästa planeringsvarv läser schemat, then fönstrets energi är okänd och det tidigare värdet påverkar inte planen.

## Spec Change Log

## Review Triage Log

### 2026-10-29 — Review pass
- verdicts: 1 finding — high 0, medium 1, low 0, false 0, maybe-false 0
- findings:
  - `[medium]` `[patch]` Regressionen anropade bara `_load_external_windows`, medan F9 kräver att nästa planeringsvarv använder det nya målet — ett nytt test kör två hela `tick()`-varv med samma schema, verifierar 90→20 % och att planens fingeravtryck ändras. Verifieringsgranskaren hittade inga andra luckor.

## Verification

**Commands:**
- `.venv/review-environment/Scripts/python.exe -m pytest -q tests/test_price_controller.py` — förväntat: alla tester passerar.
- `.venv/review-environment/Scripts/python.exe -m ruff check custom_components/pixiihome_controller/price_controller.py tests/test_price_controller.py` — förväntat: inga fel.
- `.venv/review-environment/Scripts/python.exe -m pyright custom_components/pixiihome_controller/price_controller.py` — förväntat: inga typfel.

## Auto Run Result

Status: done

**Sammanfattning:** Target SOC läses på nytt varje planeringsvarv. Ändrade mål används direkt och loggas med orsak. Om aktuell läsning misslyckas eller ger ett icke ändligt tal blir fönstrets resulterande energi okänd; ett äldre värde används inte.

**Ändrade filer:**
- `custom_components/pixiihome_controller/price_controller.py` — läser alltid aktuellt mål och loggar förändring eller övergång till oläsbart mål.
- `tests/test_price_controller.py` — täcker ändrat, oförändrat och oläsbart mål samt två fullständiga planeringsvarv.
- Denna storyspec — dokumenterar genomförande och granskning.

**Granskning:** En medium-lucka i testytan lappades. Inga fynd sköts upp eller avvisades. Uppföljande granskning rekommenderas inte; patchen ändrade bara testet och den verifierade risken var att 20 % inte nådde ett fullständigt planeringsvarv.

**Verifiering:** `pytest -q tests/test_price_controller.py` gav 56 godkända tester. Ruff på de två ändrade kod- och testfilerna passerade. Pyright på `price_controller.py` gav 0 fel och 0 varningar.

**Begränsning:** Ingen live-verifiering gjordes enligt kontraktet.
