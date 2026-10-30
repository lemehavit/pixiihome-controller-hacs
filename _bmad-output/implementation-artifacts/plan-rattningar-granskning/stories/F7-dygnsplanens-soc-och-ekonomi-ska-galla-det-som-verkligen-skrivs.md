---
title: Dygnsplanens SOC och ekonomi ska gälla det som verkligen skrivs
type: bugfix
created: '2026-10-29'
status: done
baseline_revision: fcc43fa9c7dca89d756e84915d4ec6ff1c69c97a
review_loop_iteration: 0
followup_review_recommended: true
context: []
warnings: []
deferred: []
---

<intent-contract>

## Intent

**Problem:** Dygnsplanen visar SOC, kostnad och marginal från DP-banan även när dess urladdning filtreras bort av minimivinstkravet. Användaren får då en plan som beskriver ett annat förlopp än de åtgärder som faktiskt skrivs.

**Approach:** Spela upp den valda banan efter åtgärdsfiltrering och beräkna visade flöden, SOC och ekonomi från den genomförbara banan. Behåll befintlig hantering av fasta och justerbara externa fönster.

## Boundaries & Constraints

**Always:** Planens slots, min/max SOC, kostnader, marginal och externa mål motsvarar den genomförbara åtgärdsbanan; logga ändrad plan och orsak med `log_activity` på controller-ytan. Verifiera endast med tester.

**Never:** Ändra externa scheman, skriv Gateway/MQTT, skicka om osäkra anrop, eller bygg funktionalitet för F5/F6/R2.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|---------------|----------------------------|----------------|
| Bortfiltrerad urladdning | 80 % SOC, 10 kWh, 1 kW last, priser 1.10/1.10/0.80/0.80, RTE 90 %, minvinst 0.50 | Inga åtgärder, oförändrad SOC, planerad kostnad = bas, marginal 0 | Ingen |
| Bevarad åtgärd | Lönsam laddning/urladdning | Slots och ekonomi visar genomförd energi och verklig kostnad | Ingen |
| Externt fönster | Känt fast mål eller justerbart mål | Fast reset och sänkt mål beräknas från kvarvarande energi | Ingen |

</intent-contract>

## Code Map

- `custom_components/pixiihome_controller/control/whole_day_planner.py:293-380` — DP-val, `planned_refill`, huvudloop, `kept`, fönstermål och resultat. `planned`/`margin_total`/slots bygger nu på ofiltrerad `delta`; flytta bokföringen efter `written`-beslutet.
- `custom_components/pixiihome_controller/control/whole_day_planner.py:199-222,383-465` — `slot_cost` beräknar nätkostnad och `_action` avgör skriven åtgärd; återanvänd kostnadsfunktionen vid omräkning.
- `custom_components/pixiihome_controller/price_controller.py:957-989` — `_build_plan` anropar dygnsplaneraren och väljer publicerad plan; möjlig yta för aktivitetslogg med orsak. Ändra bara om loggning krävs.
- `tests/test_whole_day_planner.py:267-329` — befintliga tester för bortfiltrerad urladdning och externa mål; utöka med rapportens numeriska fall och ekonomin.
- `tests/test_price_controller.py:684-704` — befintlig yttre controller-yta för dygnsplan; verifiera publicerade attribut om dessa förändras.

## Tasks & Acceptance

**Execution:**
- `custom_components/pixiihome_controller/control/whole_day_planner.py` — räkna om genomförbar energibana efter minimifiltret och använd den för slots, planerad kostnad, marginal, min/max SOC och externa mål.
- `custom_components/pixiihome_controller/price_controller.py` — logga varför den publicerade dygnsplanen ändras om ny loggning behövs för ändrad funktion.
- `tests/test_whole_day_planner.py` — testa rapportfallet, bevarad lönsam åtgärd och fasta/justerbara fönster.

**Acceptance Criteria:**
- Given rapportens priser och batteri, when dygnsplanen byggs, then inga åtgärder skrivs, alla slots behåller 80 % SOC, plankostnad är baskostnad och marginalen är 0.
- Given en lönsam åtgärd, when planen byggs, then åtgärdens energiflöde, SOC och ekonomi stämmer med de visade slotsen.
- Given ett justerbart externt fönster, when en urladdning filtreras bort, then målet sänks inte av den oskrivna urladdningen.

## Spec Change Log

## Review Triage Log

### 2026-10-29 — Review pass
- verdicts: 3 findings — high 0, medium 3, low 0, false 0, maybe-false 0
- findings:
  - `[medium]` `[patch]` Kostnaden för påfyllning efter en filtrerad urladdning saknade riktat test — numeriska kostnads- och marginalpåståenden lades till i testet med fast mål.
  - `[medium]` `[patch]` Positiv planmarginal saknade test — lönsamhetsbanans test räknar nu separat fram terminalvärde och förväntad marginal.
  - `[medium]` `[patch]` Den publicerade controller-ytan saknade F7-fallet — controller-testet kontrollerar attribut och uteblivna scheman. Befintlig `_log_plan` använder redan `log_activity` med orsak när åtgärdsplanen ändras.

## Verification

**Commands:**
- `.venv/review-environment/Scripts/python.exe -m pytest -q tests/test_whole_day_planner.py` — alla riktade tester passerar.
- `.venv/review-environment/Scripts/python.exe -m ruff check custom_components/pixiihome_controller/control/whole_day_planner.py tests/test_whole_day_planner.py` — inga lintfel.
- `.venv/review-environment/Scripts/python.exe -m pyright custom_components/pixiihome_controller/control/whole_day_planner.py` — inga typfel.

## Auto Run Result

Status: done

**Sammanfattning:** Dygnsplanens slots, SOC, kostnad, marginal och mål för justerbara externa fönster följer nu åtgärderna som behålls efter minimivinstfiltret. Senare laddning begränsas av faktisk ledig kapacitet och påfyllning vid fast externt mål räknas från faktiskt kvarvarande energi.

**Ändrade filer:**
- `custom_components/pixiihome_controller/control/whole_day_planner.py` — omräknad genomförbar energibana och ekonomi.
- `tests/test_whole_day_planner.py` — rapportfallet, lönsam bana och externa mål med numeriska kostnads- och marginalkontroller.
- `tests/test_price_controller.py` — publicerade attribut och uteblivna scheman för rapportfallet.
- Denna storyspec — genomförande, verifiering och granskning.

**Granskning:** Tre mediumfynd patchade med riktade tester; inga uppskjutna eller avvisade fynd. Uppföljande granskning rekommenderas (`true`) eftersom flera mediumluckor fanns. Kvar att granska separat är ovanliga banor där senare DP-beslut spelas upp efter en filtrerad åtgärd utan ny optimering.

**Verifiering:** `pytest -q tests/test_whole_day_planner.py tests/test_price_controller.py` gav 86 godkända tester. `ruff check` på de tre ändrade Pythonfilerna passerade. `pyright` på planeraren gav 0 fel och 0 varningar. `git diff --check HEAD` passerade. Inga live-anrop gjordes.
