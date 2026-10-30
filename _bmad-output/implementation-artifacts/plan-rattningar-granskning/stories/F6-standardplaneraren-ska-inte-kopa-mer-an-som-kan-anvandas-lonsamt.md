---
title: Standardplaneraren ska inte köpa mer än som kan användas lönsamt
type: bugfix
created: 2026-10-29
status: done
baseline_revision: 80e2ad876921a4ce4f5e595c02acdcdeed87c6b2
review_loop_iteration: 0
followup_review_recommended: true
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
- `custom_components/pixiihome_controller/control/price_optimization.py` — spåra energi laddad i aktuellt segment separat även utan stängande fönster. Använd den billiga energins egen prisbas både vid framtida täckningsavräkning och faktisk urladdning; blanda inte in äldre dyr energi i dess prisbas. Om både ny och äldre energi kvalificerar ska båda kunna täcka samma framtida slot upp till prognostiserat behov.
- `custom_components/pixiihome_controller/control/price_optimization.py` — fördela kvalificerade energidelar konsekvent mellan flera framtida lastslotar: en del som bara fungerar i den första sloten ska inte ersättas av en billigare del som behövs i den andra. Samma ordning ska användas i behovsberäkning och urladdning.
- `custom_components/pixiihome_controller/control/price_optimization.py` — bevara schemats fulla urladdningseffekt vid egenförbrukning när prognosen är lägre än möjlig effekt; begränsa bara simulerad leverans, SOC och marginal till prognosen. Utan `net_load_kw` ska prisbas och åtgärder följa beteendet före denna fixrunda.
- `custom_components/pixiihome_controller/price_controller.py` — logga med orsak när F6-begränsningen ändrar planbeslutet, utan att ändra externa scheman.
- `tests/test_price_optimization.py` — verifiera matrisen numeriskt samt gränsen mellan externa fönster.
- `tests/test_price_optimization.py` — lägg två numeriska regressioner: hög senaste kända prisbas 3,00 med SOC 50 %, last 2 kW, priser 0,20/0,20/3,00; och känt återställningsfönster i timme 3 med prisbas 3,00 och priser 0,20/0,20/3,00/3,00. Båda ska behålla lönsam laddning och urladdning.
- `tests/test_price_optimization.py` — lägg BMA-40:s två öppna segment: bas 3,00, SOC 50 %, priser 0,20/0,20/2,40 och bas 3,00, SOC 30 %, priser 0,20/0,20/2,00; last 2 kW i sista timmen, export av. Kräv cirka 2/0,9 kWh laddning totalt en gång, planerad urladdning som levererar 2 kWh och positiv marginal. Lägg även ett stängt segment där billig och äldre lagrad energi tillsammans kvalificerar för samma slot; det får inte överladda.
- `tests/test_price_optimization.py` — testa två skilda billiga laddpriser och kontrollera viktad urladdningsbas och marginal. Testa även initial prisbas 2,00, priser 0,20/3,00/2,50, SOC 20 % och 0,90 kW last i båda dyra timmarna: båda urladdningarna ska planeras och täcka prognosen.
- `tests/test_price_optimization.py` — testa att låg prognos ger låg simulerad leverans men fortsatt full tillåten effekt i schemaåtgärden, och att samma fall utan prognos behåller tidigare prisbas och åtgärdsval.

**Acceptance Criteria:**
- Given rapportens tre priser och ingen framtida nettolast, when standardplanen byggs, then den har ingen laddningsåtgärd.
- Given rapportens fall med 0,1 kW sista timmen, when standardplanen byggs, then sammanlagd laddning är högst cirka 0,1/0,9 kWh och inte 8,4 kWh.
- Given saknad lastprognos, when samma plan byggs, then dess laddningsåtgärder följer tidigare headroom-beteende.
- Given lagrad energi med prisbas 3,00 som inte kan säljas lönsamt i den dyra timmen, when standardplanen byggs med billig laddning 0,20 och 2 kW prognostiserad last, then planen laddar för den lönsamma cykeln och har positiv förväntad marginal.
- Given ett känt återställningsfönster som ger prisbas 3,00 efter fönstret, when standardplanen byggs med priser 0,20/0,20/3,00/3,00 och 2 kW prognostiserad last, then planen laddar för den lönsamma cykeln före återställningen och har positiv förväntad marginal.
- Given BMA-40:s två fall med dyr initial energi och billig laddning i ett öppet segment, when standardplanen byggs, then laddas ungefär 2/0,9 kWh totalt en gång, 2 kWh urladdas till lasten och marginalen är positiv.
- Given ett segment före känt återställningsfönster där både nyladdad och initial energi kvalificerar, when två billiga intervall föregår en dyr lastslot, then sammanlagd laddning begränsas av kvarvarande behov efter båda energidelarna och ingen överladdning sker.
- Given initial prisbas 2,00 och priser 0,20/3,00/2,50 med 0,90 kW last i båda dyra timmarna, when standardplanen byggs, then både första och andra urladdningen finns och levererar ungefär 0,90 kWh vardera.
- Given laddning till två olika priser i samma segment, when batteriet urladdas, then visad referensprisbas och marginal motsvarar den energiviktade prisbasen för de använda energidelarna.
- Given en nettolastprognos under möjlig urladdningseffekt, when en egenförbrukningsåtgärd skapas, then åtgärdens effekt förblir tillgänglig för högre verklig last medan planerad energi, SOC och marginal följer prognosen.
- Given att lastprognos saknas, when standardplanen byggs för ett fall med dyr initial energi och billig laddning, then prisbas och åtgärdsval överensstämmer med beteendet före den tredje fixrundan.

## Spec Change Log

- 2026-10-29: BMA-32 visade att `needed_stored_energy - available` räknar av även dyrt lagrad energi som inte kvalificerar för framtida urladdning. Specen kräver nu samma prisbasbedömning som urladdningsgrenen och två regressioner. Detta undviker att billig lönsam laddning kapas till noll. KEEP: F6:s prognos-, sol-, export- och segmentgränser samt avsaknad av live-anrop.
- 2026-10-30: BMA-40 visade att en blandad `acquisition_price` i öppet segment döljer lönsam nyladdad energi och dubblerar billig laddning utan urladdning. Specen kräver separata prisbaser för ny och äldre energi i både täckning och urladdning, plus två exakta regressioner. Granskningen visade också att stängt segment måste kunna nyttja båda kvalificerade energidelarna. KEEP: selektiv avräkning per urladdningsläge, exportfallet, segmentgränser, prognosfallback och loggning med orsak.
- 2026-10-30: Granskning av tredje rättningen visade att ny billig energi kan förbrukas i första lastslot trots att äldre energi bara är lönsam där; då saknas andra urladdningen. Specen kräver konsekvent prioritet för energidelarna mellan prognos och plan, plus test för två framtida lastslotar och viktad prisbas. KEEP: BMA-40:s exakta tvåfallstester, stängda fönstret och övriga F6-gränser.
- 2026-10-30: Egen kontroll hittade två avvikelser mot F6:s befintliga kontrakt: ny kod minskade schemaeffekten till prognosen trots `build_price_plan`-dokumentation om full effekt, och separata energidelar kan ändra beteendet utan prognos. Specen kräver separata kontroller. KEEP: korrekt simulerad leverans, SOC och marginal samt prisbasprioritet när prognos finns.

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

### 2026-10-30 — Tredje fixrundans första granskning
- verdicts: 3 findings — high 1, medium 2, low 0, false 0, maybe-false 0
- findings:
  - `[medium]` `[bad_spec]` Testerna för dyr initial energi krävde bara positiv laddning — nu krävs cirka 2/0,9 kWh laddning och 2 kWh leverans i båda BMA-40-fallen.
  - `[medium]` `[bad_spec]` I stängt segment kunde en framtidsslot inte täckas av både ny och initial kvalificerad energi — specen kräver båda lager i täckning och urladdning samt en regression mot överladdning.
  - `[high]` `[bad_spec]` BMA-40:s öppna segment blandar ny billig och äldre dyr energi i `acquisition_price` — separata energidelar och prisbaser krävs för lönsam urladdning.

### 2026-10-30 — Tredje fixrundans andra granskning
- verdicts: 2 findings — high 1, medium 1, low 0, false 0, maybe-false 0
- findings:
  - `[medium]` `[bad_spec]` Två skilda billiga laddpriser saknade kontroll av viktad urladdningsbas — testkravet har lagts till.
  - `[high]` `[bad_spec]` Prognosen reserverar äldre energi för första lastslot men urladdningen förbrukar billig ny energi där — reproducerat; specen kräver att båda slotarna faktiskt levereras.

### 2026-10-30 — Egen kontroll före slutgranskning
- verdicts: 2 findings — high 0, medium 2, low 0, false 0, maybe-false 0
- findings:
  - `[medium]` `[bad_spec]` Egenförbrukningsschemat fick prognosens effekt i stället för full tillåten effekt — kräver skild planerad leverans och schemalagd effekt.
  - `[medium]` `[bad_spec]` Separata prisbaser kan ändra plan utan prognos — kräver ett numeriskt test mot tidigare beteende och bevarande av den vägen.

### 2026-10-30 — Slutgranskning av tredje fixrundan
- verdicts: inga nya high- eller mediumfynd i två oberoende granskningar.
- findings:
  - `[medium]` `[patch]` Okänt värde från en befintlig prognosfunktion kunde lämna den nya prisbaslogiken aktiv — hela planen återgår nu till tidigare beteende när prognosen är ofullständig; numeriskt test tillagt.

### 2026-10-30 — Fjärde fixrundans granskning
- verdicts: 1 finding — high 1, medium 0, low 0, false 0, maybe-false 0
- findings:
  - `[high]` `[patch]` Lagrad exportkvalificerad energi uteslöts när ny energi bara kvalificerade för egenförbrukning — numeriskt regressionstest visade onödig laddning 2,22 kWh; lägesval, framtida täckning och faktisk urladdning använder nu samma prioritet och kompatibilitet.

## Design Notes

Beräkna behovet inom segmentet mellan externa återställningar. `net_load_kw` är redan last minus sol, så en separat solavräkning skulle dubbelräkna den. Räkna prognosens levererbara AC-energi baklänges genom RTE för att få nödvändig inköpt AC-energi. Skilj energi som fanns vid segmentstart från energi laddad i segmentet när prognos finns; deras prisbaser är separata. Räkna av endast den del som kan levereras lönsamt i rätt urladdningsläge vid relevant framtida pris. När båda delar kvalificerar får båda användas till samma slot, men tillsammans aldrig mer än behovet. Använd samma prioritet i prognostäckning och faktisk urladdning. Den del som har högre prisbas har färre lönsamma framtida slotar och bör därför användas först när den kvalificerar, så att billigare energi finns kvar till senare slotar. Pris och marginal för en urladdning med två delar ska viktas efter förbrukad energi. Schemat behåller full effekt vid egenförbrukning; bara simulerad energi begränsas av prognosen. Utan prognos gäller tidigare blandade prisbas och åtgärdsval.

## Verification

**Commands:**
- `.venv/review-environment/Scripts/python.exe -m pytest -q tests/test_price_optimization.py` — F6-fall och befintliga planeringstester passerar.
- `.venv/review-environment/Scripts/python.exe -m ruff check custom_components/pixiihome_controller/control/price_optimization.py custom_components/pixiihome_controller/price_controller.py tests/test_price_optimization.py` — inga lintfel.
- `.venv/review-environment/Scripts/python.exe -m pyright custom_components/pixiihome_controller/control/price_optimization.py custom_components/pixiihome_controller/price_controller.py` — inga typfel.

## Auto Run Result

status: done

**Sammanfattning:** Standardplaneraren begränsar nu laddning till lönsam framtida användning inom aktuellt planeringssegment. Den räknar av lagrad energi och använder nettolastprognosen, som redan inkluderar sol. Utan känd prognos gäller tidigare beteende. Kvalificerad export kan fortfarande motivera laddning.

**Ändrade filer:** `control/price_optimization.py` beräknar laddgränsen; `price_controller.py` loggar begränsning med orsak och utan upprepning för samma plan; `tests/test_price_optimization.py` och `tests/test_price_controller.py` testar numeriska fall och loggning.

**Granskning:** En mediumbrist i loggningen patchades. Inga fynd sköts upp eller avvisades. Patchade fynd: high 0, medium 1, low 0. Uppföljande granskning rekommenderas inte (`false`).

**Verifiering:** `pytest -q tests/test_price_optimization.py` gav 48 godkända; `pytest -q tests/test_price_controller.py` gav 67 godkända. Riktad Ruff passerade och riktad Pyright gav 0 fel och 0 varningar. `git diff --check HEAD` passerade. Ingen live-verifiering gjordes.

**Kvarvarande risk:** Beräkningen uppskattar framtida kvalificerad leverans från priser, prognos och effektgränser; den simulerar inte om hela den uppskattade leveransen verkligen blir en urladdningsåtgärd i varje ovanlig prisbana.

### Uppföljningsresultat 2026-10-29

status: done

**Sammanfattning:** Lagrad energi räknas nu av från framtida laddbehov bara när dess prisbas kvalificerar för samma urladdningsläge i den framtida sloten. Detta bevarar lönsamma billiga cykler när tidigare energi är dyr och när endast ny energi är lönsam att exportera.

**Ändrade filer:** `control/price_optimization.py` jämför prisbas och urladdningsläge före avräkning; `price_controller.py` förtydligar aktivitetsorsaken; `tests/test_price_optimization.py` testar dyr initial energi, känt återställningsfönster och exportfallet; `tests/test_price_controller.py` verifierar orsaken.

**Granskning:** Två fynd patchades i sista passet (high 0, medium 1, low 1); inga nya fynd sköts upp eller avvisades. De två tidigare F5-observationerna är registrerade som uppskjutna till F7b. Uppföljande granskning rekommenderas inte (`false`) enligt arbetsflödets tröskel för ett uppföljningspass.

**Verifiering:** `pytest -q tests/test_price_optimization.py`: 51 godkända. `pytest -q tests/test_price_controller.py`: 74 godkända. Riktad Ruff passerade, riktad Pyright gav 0 fel och 0 varningar, `git diff --check HEAD` passerade. Inga live-anrop gjordes.

**Kvarvarande risk:** Framtida leverans uppskattas fortfarande snarare än att fullständigt simulera varje ovanlig urladdningsbana. F5-observationerna hanteras i F7b.

### Tredje fixrundans resultat 2026-10-30

status: done

**Sammanfattning:** I öppna och stängda segment har äldre lagrad energi och ny billig laddning nu skilda prisbaser när nettolastprognos finns. Prognosens behov och faktisk urladdning använder samma prioritet för energidelarna. Utan fullständig prognos används tidigare blandad prisbas och åtgärdsval.

**Ändrade filer:** `control/price_optimization.py` håller isär och förbrukar energidelarna i konsekvent ordning; `tests/test_price_optimization.py` täcker BMA-40:s två fall, stängt återställningsfönster, två framtida lastslotar, viktat pris, schemaeffekt samt saknad eller okänd prognos.

**Granskning:** Två slutgranskningar fann inga nya high- eller mediumfynd. Tidigare fynd i denna runda åtgärdades inom F6. Uppföljande granskning rekommenderas inte (`false`).

**Verifiering:** `pytest -q tests/test_price_optimization.py`: 61 godkända. `pytest -q tests/test_price_controller.py`: 78 godkända. Riktad Ruff passerade; riktad Pyright gav 0 fel och 0 varningar; `git diff --check` passerade. Inga live-anrop gjordes.

**Kvarvarande risk:** Planen använder fortfarande en pris- och lastprognos; verklig last eller sol kan avvika från prognosen. Full testsvit och CI kördes inte här.

### Fjärde fixrundans resultat 2026-10-30

Status: done

**Sammanfattning:** Lagrad energi som kvalificerar för export väljs före nyladdad energi som bara kvalificerar för egenförbrukning. Exportkvalificerad energi kan också täcka prognostiserad egenförbrukning, så samma behov finansierar inte onödig laddning. Den valda urladdningens marginal beräknas för dess faktiska läge.

**Ändrade filer:** `control/price_optimization.py` rättar lägesval, täckning och urladdning samt tar bort den oåtkomliga `forecast_known`-grenen; `tests/test_price_optimization.py` lägger ett numeriskt export/egenförbrukningsfall.

**Granskning:** Ett high-fynd patchades; inga fynd sköts upp eller avvisades i denna runda. Uppföljande granskning rekommenderas eftersom lägesvalet mellan två energidelar är en konkret kvarvarande regressionsrisk (`true`).

**Verifiering:** Hela `pytest -q` passerade; `ruff check .` passerade efter rättning av teststil; `pyright` gav 0 fel och 0 varningar; `scripts/build_release.py --check` gav Metadata OK: 0.5.22. Det numeriska regressionstestet föll före rättningen och passerade efteråt.

**Kvarvarande risk:** Prognosen kan avvika från faktisk last och sol; live-systemet har inte anropats.
