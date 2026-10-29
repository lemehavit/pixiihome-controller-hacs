---
title: 'Dygnsplanen får bara räkna med solladdning som någon utför'
type: 'bugfix'
created: '2026-10-29'
status: done
baseline_revision: cbe6fd8df646e964d3de70fcc436847075a51aad
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: [oversized]
deferred:
  - summary: >-
      En redan publicerad dygnsplan ligger kvar i attributen när priscontrollern stannar (batterikalibrering, avstängd styrbehörighet och andra blockeringar), med solsegment märkta automatic_solar_charging.
    evidence: |-
      Sond 2026-10-29: en tick med sol och sedan en tick med battery_calibration_active=True eller production_control_enabled=False. Efteråt är whole_day kvar och segments visar fortfarande kind=solar, performed_by=automatic_solar_charging. _tick_locked returnerar före _build_plan (price_controller.py:641-660, prepare rad 381) och nollställer aldrig self.whole_day eller self.plan. Detta fanns före F5 och gäller alla blockeringsorsaker, men inga scheman skrivs medan controllern står still.
    location: >-
      custom_components/pixiihome_controller/price_controller.py:632-660
    severity: medium
---

<intent-contract>

## Intent

**Problem:** Dygnsplaneraren räknar laddning inom solöverskottet som gratis och utan åtgärd, även när automatisk solladdning är av eller redan har nått sitt SOC-tak. Planen visar då en SOC och ekonomi som ingen funktion genomför.

**Approach:** Skicka in en solladdningsbegränsning (på/av, maxeffekt, SOC-tak, pågående smart uppskjutning) från `_build_plan`. Solladdning utan schema får bara finnas inom den. Laddning utöver begränsningen är en skriven nätladdning med vinstkrav. Varje flöde i `whole_day_plan` anger vem som utför det.

## Boundaries & Constraints

**Always:** Utan begränsning (`solar_charging=None`) gäller tidigare beteende. SOC-taket = min(`solar_max_soc_percent`, policyns `soc_max_percent`). Maxeffekt = min(`solar_max_charge_power_kw`, batteriets laddgräns, policyns `max_power_kw`). Solladdning räknas som av när switchen är av eller policyn inte tillåter `automatic`. Logga med `log_activity` och orsak när begränsningen ändras. Verifiera bara med tester.

**Never:** Ändra solregulatorn, standardplaneraren, externa scheman eller skrivvägar. Inga live-anrop.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|---------------|----------------------------|----------------|
| Solladdning av | Sol 3 kW, last 0, platta priser, SOC 50 % | SOC ligger kvar på 50 %, inga åtgärder, inget flöde | Ingen |
| På med tak 80 % | Samma, SOC 50 %, tak 80 % | SOC stiger till 80 % och stannar, flödet utförs av `automatic_solar_charging` | Ingen |
| Maxeffekt | Sol 3 kW, maxeffekt 1 kW | Högst 1 kW solladdning per intervall | Ingen |
| Smart uppskjutning | Uppskjutning pågår, tröskel = laddpris + minsta fördel | Ingen solladdning i dagens intervall med pris ≥ tröskeln före sista laddkvarten | Ingen |
| Lönsam nätladdning med sol av | Billig sol-timme före dyr timme | Laddningen blir en skriven `demandresponse`-åtgärd, utförd av `price_schedule` | Ingen |
| Ingen begränsning | `solar_charging=None` | Samma plan som före F5 | Ingen |

</intent-contract>

## Code Map

- `custom_components/pixiihome_controller/control/whole_day_planner.py:95-398` -- `build_whole_day_plan`. Solandel antas i dag vara `max(0,-net)` på fyra ställen: beslutskostnaden i `slot_cost` (rad 216-222), `grid_charge`-sonden (rad 289), oskriven laddning i omspelningen (rad 352-356) och `charged_energy` (rad 359). Ersätt med en per-intervall- och nivåberoende fri solandel. DP-loopen (rad 259-278) behöver vinstkravet per nivå för den del som går över solandelen.
- `custom_components/pixiihome_controller/control/whole_day_planner.py:46-84,401-433` -- `WholeDaySlot.kind` (sol eller nätladdning efter överskott) och `_action` (laddåtgärd bara över överskottet). Båda ska använda den fria solandelen. Lägg till ett fält för vem som utför flödet.
- `custom_components/pixiihome_controller/solar_controller.py:278-300,346-350,430-520` -- solregulatorns villkor, tak `min(solar_max_soc_percent, policy.soc_max_percent)`, `smart_decision`, `_smart_deferring`. Läses bara.
- `custom_components/pixiihome_controller/control/smart_solar.py:23-44` -- `SmartSolarDecision` (`charge_price`, `deadline`). Läses bara.
- `custom_components/pixiihome_controller/price_controller.py:896-990,1953-1985` -- `_build_plan` anropar `build_whole_day_plan`, och `_segments` bygger attributets segment. Här byggs begränsningen, loggas och läggs till i segmenten.
- `tests/test_whole_day_planner.py:37-57` -- `_plan`, `_series`, `NOW`. `tests/test_price_controller.py:97,708-745` -- `runtime()` och F7:s attributtest som mall.

## Tasks & Acceptance

**Execution:**
- `custom_components/pixiihome_controller/control/whole_day_planner.py` -- lägg till `SolarCharging` (enabled, maximum_kw, maximum_soc_percent, defer_from_price, defer_until) och parametern `solar_charging`. Låt fri solandel = min(överskott, maxeffekt, utrymme till taket) och 0 när solladdningen är av eller uppskjuten. Vinstkravet och kravet på skriven åtgärd ska gälla allt utöver den. Ge `WholeDaySlot` fältet `performed_by` (`automatic_solar_charging`, `price_schedule`, `external_schedule`, `none`) och ta med det i `as_dict`. -- Planen räknar bara med solladdning som utförs.
- `custom_components/pixiihome_controller/price_controller.py` -- bygg `SolarCharging` i `_build_plan` från switch, policy, inställningar, batteriets laddgräns och solregulatorns pågående smart beslut. Logga när den ändras. Lägg `performed_by` i `_segments` (en ny körning när det ändras). -- Attributet visar vem som utför varje flöde.
- `tests/test_whole_day_planner.py` -- testa matrisens rader. -- CAP-F5.
- `tests/test_price_controller.py` -- testa att switchen av ger oförändrad SOC i attributet och att switchen på ger ett segment med `performed_by` samt en loggrad. -- Yttre yta.

**Acceptance Criteria:**
- Given sol 3 kW, last 0, solladdning av, when dygnsplanen publiceras, then `whole_day_plan` har inga segment och projicerad SOC är oförändrad.
- Given solladdning på med tak 80 %, when dygnsplanen publiceras, then högsta projicerade SOC är 80 % och solsegmentet har `performed_by: automatic_solar_charging`.

## Spec Change Log

## Review Triage Log

### 2026-10-29 — Review pass
- verdicts: 8 findings — high 0, medium 3, low 2, false 3, maybe-false 0
- findings:
  - `[medium]` `[patch]` Kopplingen från smart uppskjutning till planen (`_solar_charging`: `_smart_deferring`, `charge_price` + minsta fördel, `deadline`) saknade test på controller-ytan. Att byta namn på attributet eller ta bort fördelstermen gick inte att upptäcka. — Lade till `test_whole_day_plan_follows_smart_solar_deferral`, som fångar båda mutationerna.
  - `[medium]` `[patch]` Taket för skriven laddning efter en filtrerad urladdning (`elif delta > 0` i omspelningen) saknade test. — Lade till `test_written_charge_after_a_filtered_discharge_refills_only_what_was_used`. Utan taket blir laddningen 3,0 kW och SOC 53 % i stället för 1,4 kW och 38 %.
  - `[low]` `[reject]` `authorized` och policyns `soc_max` i `_solar_charging` kan inte observeras, eftersom prisplaneraren redan tillämpar dem. — Det är harmlöst skydd som specen kräver ska spegla solregulatorn, och ingen fel utdata uppstår.
  - `[false]` `[reject]` R2-läsningen: SOC skulle aldrig få stiga med solladdning av, oavsett priser. — Avsikten gäller *solladdning*. En lönsam laddning som körs av ett skrivet prisschema utförs av någon och är ingen solladdning. Ingen annan läsning går att försvara mot planens "räkna bara med solladdning som någon utför".
  - `[low]` `[reject]` Effekttaket från batteriets laddgräns och policyn testas bara på planerarnivå, inte via controllern. — Det är en direkt `min()` av tre värden. En bugg där är osannolik i vardagen, och planerartestet täcker själva begränsningen.
  - `[medium]` `[patch]` Överskott under `solar_export_start_threshold_kw` räknades som solladdning, men solregulatorn startar inte under tröskeln (`solar_controller.py`, `waiting_surplus`). — Lade till fältet `SolarCharging.minimum_surplus_kw`, som fylls från inställningen, och testet `test_surplus_below_the_start_threshold_is_not_solar_charged`.
  - `[false]` `[reject]` SOC-taket i `controllers.py` jämförs bara mot `solar_max_soc_percent`. — Den levande regulatorn är `solar_controller.py:348`, som använder `min(setting, policy)`. Diffen följer den.
  - `[false]` `[reject]` "Varje flöde" skulle kunna betyda även last-, sol- och exportflöden. — Attributets segment innehåller bara batteriflöden, och vart och ett har nu `performed_by`. Tomgångsintervall är inget flöde.

### 2026-10-29 — Review pass (uppföljning, BLOCK från BMA-33)
- verdicts: 11 findings — high 0, medium 2, low 4, false 4, maybe-false 1
- findings:
  - `[low]` `[patch]` (verification-gap) Starttröskeln `solar_export_start_threshold_kw` saknade test på controllernivå. Om `minimum_surplus_kw` togs bort i `_solar_charging` gick alla tester ändå igenom. — Lade till `test_whole_day_plan_counts_no_solar_charging_below_the_start_threshold` (överskott 0,5 kW, tröskel 1,0 kW). Den faller när argumentet byts mot `0.0`.
  - `[medium]` `[patch]` (verification-gap, other) `_solar_charging` bortsåg från adaptiv toppkapning (APS). Runtime tillåter APS på medan solladdning är på (`runtime.py:1127-1139`), och då räknade planen med solladdning som solregulatorn blockerar (`solar_controller.py:287-290`). — Samma rotorsak som BMA-33:s fynd nedan. `_solar_charging` har nu samma körtidsspärrar som solregulatorn: kalibrering, APS och styrbehörighet. Dessutom räknas `policy.max_duration_seconds <= 0` som ej behörig. Testet `test_whole_day_plan_counts_no_solar_charging_while_adaptive_peak_shaving_is_on` visar inga segment, SOC 50 % och en loggrad.
  - `[medium]` `[patch]` (extern granskning BMA-33) `price_controller.py:1038`: `SolarCharging.enabled` bortsåg från batterikalibrering, APS och avstängd styrbehörighet. — Grupperat med raden ovan, samma patch. Kalibrering och avstängd styrbehörighet stoppar redan priscontrollern innan någon plan byggs (`price_controller.py:641-645`, `381-382`). Spärren testas därför direkt på controllerns `_solar_charging` i `test_solar_limit_is_off_while_the_solar_controller_is_held[calibration|production_control]`. Alla tre testerna faller utan patchen. En kvarliggande gammal plan i attributen under sådana stopp fanns före F5 och är uppskjuten (se `deferred`).
  - `[low]` `[reject]` (intent-alignment) Ekonomin (`expected_margin_sek`, `planned_cost_sek`) har ingen egen assertion. — Den räknas per intervall från samma `battery_kw` som SOC-testerna kontrollerar. Ett eget test ger ingen ny väg, och ingen fel utdata är påvisad.
  - `[low]` `[reject]` (intent-alignment) Smart uppskjutning approximeras via privata `_smart_deferring` och testas med en stub. — Approximationen är avsiktlig och dokumenterad i Design Notes. Ett namnbyte fångas redan av `test_whole_day_plan_follows_smart_solar_deferral`.
  - `[false]` `[reject]` (intent-alignment) `price_schedule`-märkningen kontrolleras inte mot en verklig skrivning. — Planerartestet visar att en motsvarande åtgärd finns i `plan.actions`. Skrivvägen är oförändrad av F5 och testas på andra ställen. Ingen fel utdata.
  - `[false]` `[reject]` (intent-alignment) Ett blandat intervall får en enda etikett. — Det är ett designval. Det skrivna schemat utför hela flödet i intervallet, och etiketten säger just vem som utför det.
  - `[low]` `[reject]` (intent-alignment) `kind == "grid_charge"` betyder nu "utöver den fria solandelen". — Det är avsiktligt enligt avsikten ("skriven nätladdning"), och inget annat än `_segments` läser fältet.
  - `[maybe-false]` `[reject]` (intent-alignment) Invarianten utan begränsning jämförs bara mot koden efter F5. — Det som skulle avgöra saken är en jämförelse mot planen från före F5. Klämman `elif delta > 0` ändrar bara skrivna laddningar efter en filtrerad urladdning, där F5 medvetet rättade ett olönsamt schema. Om fyndet stämmer är det `low`.
  - `[low]` `[reject]` (intent-alignment) Begränsningen loggas även i skuggläge när dygnsplaneraren är av. — Skuggplanen publiceras också då i `whole_day_plan`, så loggraden beskriver rätt plan. Det blir bara en rad per ändring.
  - `[false]` `[reject]` (intent-alignment) Teständringar i `_blocked_notice_runtime` ligger utanför avsikten. — De kommer från senare commits som slogs ihop in i grenen (F3b/F2c), inte från F5. Diffens intervall `cf14af7^..HEAD` tog med dem.

## Design Notes

Ekonomin (nätflöde = net + batteri) är densamma oavsett vem som laddar. Det som skiljer är vinstkravet och om ett schema måste skrivas. Fri sol i AC-kW för nivå L: `min(room_kw[k], max(0, idx_sol − L)·step/(eta·h))`. I DP blir kostnaden `econ[delta] + hurdle·max(0, laddad_AC − fri_sol·h)`. Det är billigt per nivå och kräver ingen ny tabell. Smart uppskjutning modelleras som att intervall i dag med pris ≥ `charge_price + solar_smart_min_advantage` före `deadline` inte får solladdning, eftersom regulatorn då exporterar. Antagande: bara när regulatorn just nu skjuter upp (`_smart_deferring`).

## Verification

**Commands:**
- `.venv/review-environment/Scripts/python.exe -m pytest -q tests/test_whole_day_planner.py tests/test_price_controller.py` -- alla godkända
- `.venv/review-environment/Scripts/python.exe -m ruff check custom_components/pixiihome_controller/control/whole_day_planner.py custom_components/pixiihome_controller/price_controller.py tests/test_whole_day_planner.py tests/test_price_controller.py` -- inga fel
- `.venv/review-environment/Scripts/python.exe -m pyright custom_components/pixiihome_controller/control/whole_day_planner.py custom_components/pixiihome_controller/price_controller.py` -- 0 fel

## Auto Run Result

Status: done

**Sammanfattning:** Dygnsplanen räknar nu bara med solladdning som solregulatorn verkligen utför:
- **Solladdning av** (switchen av eller policyn utan `automatic`): ingen gratis solladdning.
- **Solladdning på:** högst min(solinställning, batteriets laddgräns, policy) i kW och upp till min(`solar_max_soc_percent`, policyns tak). Ingen solladdning när överskottet är under starttröskeln, och inte heller i dyra intervall före sista laddkvarten medan smart uppskjutning pågår.
- **Laddning utöver detta** är en skriven prisplansladdning med vinstkrav.
- **Attributet:** varje segment i `whole_day_plan` har `performed_by`.
- **Loggning:** en ändrad begränsning loggas med orsak.
- **Omspelningen:** en laddning som DP räknade som sol stannar vid soltaket, och en skriven laddning fyller bara upp till DP:s planerade nivå.

**Ändrade filer:**
- `custom_components/pixiihome_controller/control/whole_day_planner.py` — `SolarCharging`, nivåberoende fri solandel i DP och omspelning, `performed_by`.
- `custom_components/pixiihome_controller/price_controller.py` — `_solar_charging` bygger begränsningen från switch, policy, inställningar, batteri och smart uppskjutning, och loggar ändringar. `_segments` visar `performed_by`.
- `tests/test_whole_day_planner.py` — matrisens rader, starttröskeln, soltaket efter filtrerad urladdning och taket för skriven påfyllning.
- `tests/test_price_controller.py` — controller-ytan med solladdning av, solladdning på med tak 80 %, och smart uppskjutning.

**Granskning:**
- **Patchat:** 3 mediumfynd (high 0, medium 3, low 0).
- **Uppskjutet:** inget.
- **Avvisat:** 5 fynd. Två är låga (harmlöst policyskydd; effekttaket testas på planerarnivå) och tre är falska (R2-läsningen, `controllers.py`-taket, "varje flöde"). Orsakerna står i triageloggen.
- **Före granskningen** hittade och rättade jag själv en regression: en filtrerad urladdning gjorde att DP:s solpåfyllning skrevs som ett olönsamt laddschema. Testet för det fångar felet utan rättelsen.
- **Uppföljande granskning rekommenderas** (`true`), eftersom tre mediumfynd patchades. Den overifierade risken: omspelningen efter filtrerade åtgärder har nu två klämmor (soltak och DP-nivå) utan ny optimering. Ovanliga prisbanor med både filtrerad urladdning och blandad sol- och nätladdning i samma intervall är inte genomtestade.

**Verifiering:**
- `pytest tests/test_whole_day_planner.py tests/test_price_controller.py` gav 99 godkända.
- `ruff check` på de fyra filerna passerade.
- `pyright` på de två kodfilerna gav 0 fel.
- `git diff --check` passerade.
- Inga live-anrop.

**Kvarvarande risk:**
- Energistegen kan ge en laddning strax över solens effektgräns. Den skrivs då som ett litet schema, samma mönster som vid överskottsgränsen före F5.
- Uppskjutningen läses via det privata attributet `_smart_deferring`. Ett namnbyte fångas nu av ett test.
- Planen bortser från starttröskelns hysteres (stopptröskeln), så den är något försiktig när solladdningen redan är igång.

### Uppföljningspass 2026-10-29 (BLOCK från BMA-33)

**Sammanfattning:** Planerarens solladdningsgräns `_solar_charging` har nu samma körtidsspärrar som solregulatorn (`solar_controller.py:283-293`). Under batterikalibrering, adaptiv toppkapning eller avstängd styrbehörighet räknar planen inte med någon solladdning. Detsamma gäller när policyns `max_duration_seconds` är 0. Orsaken loggas med `log_activity`.

**Ändrade filer:**
- `custom_components/pixiihome_controller/price_controller.py` — `_solar_charging`: spärrarna, villkoret för `max_duration_seconds` och loggorsaken.
- `tests/test_price_controller.py` — APS via hela ticken, kalibrering och styrbehörighet via `_solar_charging`, och starttröskeln på controllernivå.

**Granskning:**
- 11 fynd.
- **Patchat:** 2 poster, en medium (APS, grupperad med BMA-33:s fynd) och en low (tröskeltest). Ingen high.
- **Uppskjutet:** 1 fynd. En gammal plan ligger kvar i attributen när controllern stannar. Det fanns före F5 (medium).
- **Avvisat:** 7 fynd, med orsaker i triageloggen.

**Uppföljande granskning:** rekommenderas inte (`false`). Det här är ett uppföljningspass, och ingen high patchades.

**Verifiering:**
- `pytest tests/test_whole_day_planner.py tests/test_price_controller.py` gav 103 godkända.
- Utan patchen i `price_controller.py` faller de tre spärrtesterna.
- Med `minimum_surplus_kw=0.0` faller tröskeltestet.
- `ruff check` på de fyra filerna passerade.
- `pyright` på de två kodfilerna gav 0 fel.
- `git diff --check` passerade.
- Inga live-anrop.

**Kvarvarande risk:**
- Den kvarliggande gamla planen under stopp (se `deferred`).
- Spärrarna för kalibrering och styrbehörighet nås i dag inte via ticken, eftersom priscontrollern stannar före dem. De speglar solregulatorn för framtida anropare.
