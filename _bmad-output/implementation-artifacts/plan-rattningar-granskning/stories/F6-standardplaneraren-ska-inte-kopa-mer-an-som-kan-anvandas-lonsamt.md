---
title: Standardplaneraren ska inte köpa mer än som kan användas lönsamt
type: bugfix
created: 2026-10-29
status: done
baseline_revision: d423cdc48f50fe20ecc16f17e8443ab6ed408ebf
review_loop_iteration: 1
followup_review_recommended: false
context: []
warnings: []
deferred:
  - summary: >-
      F5:s batteriets laddeffekt saknar ett controller-test.
    evidence: |-
      Granskaren såg att F5:s SolarCharging-gräns inte testas med lägre batterigräns i controller-vägen. Hanteras i F7b enligt BMA-32.
    location: >-
      custom_components/pixiihome_controller/price_controller.py:1050
    severity: medium
  - summary: >-
      F5:s solaktivitetsrad kan visas även när dygnsplaneraren är av.
    evidence: |-
      Solmodellen loggar när en prognos finns även om standardplanen väljs. Hanteras i F7b enligt BMA-32.
    location: >-
      custom_components/pixiihome_controller/price_controller.py:1083
    severity: low
---

<intent-contract>

## Intent

**Problem:** Standardplaneraren kan köpa flera kWh fast prognosen visar att nästan ingen energi kan användas lönsamt före planhorisontens slut. Vid noll nettolast och avstängd export finns ingen avsättning alls.

**Approach:** Begränsa laddad energi till lönsam framtida avsättning i samma planeringssegment. Räkna av energi som redan finns lagrad och förväntad sol genom prognosen för nettolast. Behåll tidigare beteende när lastprognos saknas.

## Boundaries & Constraints

**Always:** Använd endast planens prisintervall, befintlig `net_load_kw` och exportpolicy. Respektera SOC-, effekt-, vinst- och externa fönstergränser. Ändrad funktion ska logga beslut och orsak via anroparens `log_activity` utan live-anrop i test.

**Never:** Ändra externa scheman, skicka om osäkra anrop, berör live-systemet, eller ändra dygnsplaneraren i denna story.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|---------------|----------------------------|----------------|
| Ingen avsättning | Priser 0,20/0,20/3,00; 10 kWh; SOC 10 %; 5 kW; RTE 90 %; påslag 0,50; minsta vinst 0,50; export av; nettolast 0 | Ingen laddning | Ingen felstatus |
| Liten avsättning | Samma men nettolast 0,1 kW sista timmen | Laddning högst cirka 0,1/0,9 kWh och motsvarande urladdning | Ingen felstatus |
| Befintlig energi | Last i dyr timme täcks redan av batteriets energi över min-SOC | Ingen extra laddning för samma last | Ingen felstatus |
| Sol | Negativ nettolast i dyr timme, export av | Ingen laddning för den timmen | Ingen felstatus |
| Ingen prognos | `net_load_kw=None` eller okänt värde | Tidigare laddbeteende bibehålls | Ingen felstatus |
| Export | Export tillåten och exportvinst kvalificerar | Laddning får fortfarande planeras inom gränserna | Ingen felstatus |

</intent-contract>

## Code Map

- `custom_components/pixiihome_controller/control/price_optimization.py:297-570` — `build_price_plan` bygger segment kring kända externa fönster; laddning vid rad 436-473 använder bara headroom, urladdning vid rad 475-538 begränsas redan av `net_load_kw`. `_qualifying_discharge` vid rad 643-665 avgör lönsam export eller egenförbrukning. Ren planeringsfunktion; ingen direkt runtime-loggning.
- `custom_components/pixiihome_controller/price_controller.py:890-990` — `_build_plan` anropar planeraren och är rätt plats för aktivitetslogg med orsak om planbeslutet ändras; behåll skriv- och schemavägar orörda.
- `tests/test_price_optimization.py:18-54` — `greenely` och `plan` är testhjälpare; lägg F6-matrisens riktade tester här.

## Tasks & Acceptance

**Execution:**
- `custom_components/pixiihome_controller/control/price_optimization.py` — beräkna framtida lönsam levererbar energi inom aktuellt segment från prognostiserad nettolast eller export och begränsa laddning med befintlig energi avräknad; befintlig urladdningsberäkning ska fortsätta använda verklig planerad energi.
- `custom_components/pixiihome_controller/control/price_optimization.py` — räkna bara av lagrad energi vars urladdningsbas kvalificerar i den framtida sloten; vid ett känt återställningsfönster följer avräkningen `charged_energy` och `stored_price` på samma sätt som urladdningsgrenen. Detta bevarar lönsamma billiga laddcykler när tidigare energi köptes dyrt.
- `custom_components/pixiihome_controller/price_controller.py` — logga med orsak när F6-begränsningen ändrar planbeslutet, utan att ändra externa scheman.
- `tests/test_price_optimization.py` — verifiera matrisen numeriskt samt gränsen mellan externa fönster.
- `tests/test_price_optimization.py` — lägg två numeriska regressioner: hög senaste kända prisbas 3,00 med SOC 50 %, last 2 kW, priser 0,20/0,20/3,00; och känt återställningsfönster i timme 3 med prisbas 3,00 och priser 0,20/0,20/3,00/3,00. Båda ska behålla lönsam laddning och urladdning.

**Acceptance Criteria:**
- Given rapportens tre priser och ingen framtida nettolast, when standardplanen byggs, then den har ingen laddningsåtgärd.
- Given rapportens fall med 0,1 kW sista timmen, when standardplanen byggs, then sammanlagd laddning är högst cirka 0,1/0,9 kWh och inte 8,4 kWh.
- Given saknad lastprognos, when samma plan byggs, then dess laddningsåtgärder följer tidigare headroom-beteende.
- Given lagrad energi med prisbas 3,00 som inte kan säljas lönsamt i den dyra timmen, when standardplanen byggs med billig laddning 0,20 och 2 kW prognostiserad last, then planen laddar för den lönsamma cykeln och har positiv förväntad marginal.
- Given ett känt återställningsfönster som ger prisbas 3,00 efter fönstret, when standardplanen byggs med priser 0,20/0,20/3,00/3,00 och 2 kW prognostiserad last, then planen laddar för den lönsamma cykeln före återställningen och har positiv förväntad marginal.

## Spec Change Log

- 2026-10-29: BMA-32 visade att `needed_stored_energy - available` räknar av även dyrt lagrad energi som inte kvalificerar för framtida urladdning. Specen kräver nu samma prisbasbedömning som urladdningsgrenen och två regressioner. Detta undviker att billig lönsam laddning kapas till noll. KEEP: F6:s prognos-, sol-, export- och segmentgränser samt avsaknad av live-anrop.

## Review Triage Log

### 2026-10-29 — Review pass
- verdicts: 1 finding — high 0, medium 1, low 0, false 0, maybe-false 0
- findings:
  - `[medium]` `[patch]` Upprepade `_build_plan`-anrop loggade samma laddgräns flera gånger — `price_controller.py:987` loggade vid varje bygge medan samma varv bygger minst två gånger; åtgärdat genom att minnas senast loggade planfingeravtryck och testa två byggen med exakt en loggrad.

### 2026-10-29 — Uppföljningspass
- verdicts: 3 findings — high 1, medium 1, low 1, false 0, maybe-false 0
- findings:
  - `[medium]` `[defer]` F5:s batteriets laddeffekt saknar ett controller-test — berör dygnsplanens solmodell i F5/F7b, inte standardplanerarens F6-väg; BMA-32:s icke blockerande anmärkningar hanteras i F7b.
  - `[low]` `[defer]` F5:s solaktivitetsrad kan visas när dygnsplaneraren är av — gäller F5/F7b och ändrades inte av F6.
  - `[high]` `[bad_spec]` Dyrt lagrad energi räknas av trots att dess prisbas inte kvalificerar — BMA-32 reproducerade förlusten av lönsamma cykler; Tasks, AC och Design Notes kräver nu selektiv avräkning och två regressioner.

### 2026-10-29 — Granskning efter rättning
- verdicts: 2 findings — high 0, medium 1, low 1, false 0, maybe-false 0
- findings:
  - `[low]` `[patch]` Loggens förklaring om kvalificerad prisbas saknade precis assertion — controller-testet kontrollerar nu den specifika klausulen.
  - `[medium]` `[patch]` Energi som bara är lönsam för egenförbrukning räknades av mot exportbehov — avräkningen kräver nu samma kvalificerade urladdningsläge som framtidssloten och en exportregression verifierar lönsam laddning.

## Design Notes

Beräkna behovet inom segmentet mellan externa återställningar. `net_load_kw` är redan last minus sol, så en separat solavräkning skulle dubbelräkna den. Räkna prognosens levererbara AC-energi baklänges genom RTE för att få nödvändig inköpt AC-energi. Dra bara av befintlig energi om den kan levereras lönsamt vid relevant framtida pris enligt `_qualifying_discharge` och samma bas som urladdningsgrenen: `acquisition_price` utan stängningsfönster, annars `charged_price`/`charged_energy` eller `stored_price`. Okänd prognos bevarar tidigare beteende. En hög basprisnivå får inte blockera ett nytt billigt lönsamt köp.

## Verification

**Commands:**
- `.venv/review-environment/Scripts/python.exe -m pytest -q tests/test_price_optimization.py` — F6-fall och befintliga planeringstester passerar.
- `.venv/review-environment/Scripts/python.exe -m ruff check custom_components/pixiihome_controller/control/price_optimization.py custom_components/pixiihome_controller/price_controller.py tests/test_price_optimization.py` — inga lintfel.
- `.venv/review-environment/Scripts/python.exe -m pyright custom_components/pixiihome_controller/control/price_optimization.py custom_components/pixiihome_controller/price_controller.py` — inga typfel.

## Auto Run Result

Status: done

**Sammanfattning:** Standardplaneraren begränsar nu laddning till lönsam framtida användning inom aktuellt planeringssegment. Den räknar av lagrad energi och använder nettolastprognosen, som redan inkluderar sol. Utan känd prognos gäller tidigare beteende. Kvalificerad export kan fortfarande motivera laddning.

**Ändrade filer:** `control/price_optimization.py` beräknar laddgränsen; `price_controller.py` loggar begränsning med orsak och utan upprepning för samma plan; `tests/test_price_optimization.py` och `tests/test_price_controller.py` testar numeriska fall och loggning.

**Granskning:** En mediumbrist i loggningen patchades. Inga fynd sköts upp eller avvisades. Patchade fynd: high 0, medium 1, low 0. Uppföljande granskning rekommenderas inte (`false`).

**Verifiering:** `pytest -q tests/test_price_optimization.py` gav 48 godkända; `pytest -q tests/test_price_controller.py` gav 67 godkända. Riktad Ruff passerade och riktad Pyright gav 0 fel och 0 varningar. `git diff --check HEAD` passerade. Ingen live-verifiering gjordes.

**Kvarvarande risk:** Beräkningen uppskattar framtida kvalificerad leverans från priser, prognos och effektgränser; den simulerar inte om hela den uppskattade leveransen verkligen blir en urladdningsåtgärd i varje ovanlig prisbana.

### Uppföljningsresultat 2026-10-29

Status: done

**Sammanfattning:** Lagrad energi räknas nu av från framtida laddbehov bara när dess prisbas kvalificerar för samma urladdningsläge i den framtida sloten. Detta bevarar lönsamma billiga cykler när tidigare energi är dyr och när endast ny energi är lönsam att exportera.

**Ändrade filer:** `control/price_optimization.py` jämför prisbas och urladdningsläge före avräkning; `price_controller.py` förtydligar aktivitetsorsaken; `tests/test_price_optimization.py` testar dyr initial energi, känt återställningsfönster och exportfallet; `tests/test_price_controller.py` verifierar orsaken.

**Granskning:** Två fynd patchades i sista passet (high 0, medium 1, low 1); inga nya fynd sköts upp eller avvisades. De två tidigare F5-observationerna är registrerade som uppskjutna till F7b. Uppföljande granskning rekommenderas inte (`false`) enligt arbetsflödets tröskel för ett uppföljningspass.

**Verifiering:** `pytest -q tests/test_price_optimization.py`: 51 godkända. `pytest -q tests/test_price_controller.py`: 74 godkända. Riktad Ruff passerade, riktad Pyright gav 0 fel och 0 varningar, `git diff --check HEAD` passerade. Inga live-anrop gjordes.

**Kvarvarande risk:** Framtida leverans uppskattas fortfarande snarare än att fullständigt simulera varje ovanlig urladdningsbana. F5-observationerna hanteras i F7b.
