---
title: PR2-uppföljning – granskningsanmärkningar F7/F6
type: bugfix
created: '2026-10-29'
status: done
baseline_revision: 92b80ecc909819534ec46709e40e0f5c0ec60022
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: []
deferred: []
---

<intent-contract>

## Intent

**Problem:** F7:s omspelning kan räkna oskriven laddning som sol och använda DP:s urladdningsnivå efter att tidigare åtgärder filtrerats. F6:s laddbehov gör en dyr suffixsökning, gränsloggningen kan ange en begränsning utan ändrat beslut och segmenttestet bevisar inte sin gräns.

**Approach:** Låt dygnsplanens faktiska energibana styra laddning och urladdning, förtydliga kostnadens betydelse, beräkna framtida pristopp effektivt och gör testerna känsliga för varje regressionspunkt.

## Boundaries & Constraints

**Always:** Behåll 0.5.22, externa scheman och nuvarande pris-/SOC-policy. Varje ändrat beslut ska loggas med orsak genom befintlig controller-yta. Kör endast simulerade tester, Ruff, Pyright, Pytest och releasekontroll.

**Never:** Anropa live-HA, Gateway eller MQTT; slå ihop till main eller gör release. Ändra inte andra stories.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|---------------|----------------------------|----------------|
| Ingen sol | Laddning ≤0,05 kW, sol 0 | Ingen oskriven solladdning eller ökad SOC | Ingen |
| Verkligt överskott | Sol större än last, laddning inom solgräns | Endast överskottet kan laddas utan schema | Ingen |
| Filtrerad urladdning | Tidigare urladdning skrevs inte | Senare urladdning håller sig över min-SOC i faktisk bana | Ingen |
| Känd återställning | Lönsam topp före gränsen och hög last efter | Laddning före gränsen finansieras bara av segmentets topp | Ingen |
| Oförändrad laddning | Prognosbegränsning större än annat aktivt tak | `charge_demand_limited` är falsk | Ingen |

</intent-contract>

## Code Map

- `custom_components/pixiihome_controller/control/whole_day_planner.py:353-473` — `grid_charge`, `planned_refill`, `kept` och `candidate_delta` spelar upp DP-val. Kontrollera faktiskt solöverskott och klipp även urladdning mot `kept/step - idx_min`; beräkna senare flöden från `kept` där genomförbart.
- `custom_components/pixiihome_controller/control/price_optimization.py:438-520` — F6:s framtidssökning räknar `later_peak` för varje kandidat; förberäkna suffix-max inom segment och utan blockerade intervall. Sätt begränsningsflaggan endast när faktiskt laddbeslut ändras.
- `custom_components/pixiihome_controller/price_controller.py:277-284,993-1007` — publik `cost_without_battery_sek` och loggning av laddgräns; dokumentera att basen inkluderar fast aggregatpåfyllning, eller byt namn konsekvent i dokumentation/tester.
- `tests/test_whole_day_planner.py:286-408` — befintliga F7-fall; lägg regressioner för sol utan överskott och filtrerad urladdning följd av urladdning.
- `tests/test_price_optimization.py:66-165` — F6-hjälpare, gränstest och flaggtest. Gränstestet måste vara rött mot `150cacb`.
- `tests/test_price_controller.py:730-750,1500-1530` — publik ekonomi och loggrad. `docs/price-optimization.md`, `CHANGELOG.md` — betydelse och 0.5.22-post.

## Tasks & Acceptance

**Execution:**
- `custom_components/pixiihome_controller/control/whole_day_planner.py` — rätta solklassning, urladdningsgräns och omspelning efter filtrering; dokumentera kvarvarande DP-begränsning i kod om omspelning inte kan följa `kept` helt.
- `custom_components/pixiihome_controller/control/price_optimization.py` — ersätt upprepad suffixsökning och koppla `charge_demand_limited` till ändrat beslut.
- `custom_components/pixiihome_controller/price_controller.py`, `docs/price-optimization.md` — gör publikt basvärde och eventuell omspelningsbegränsning begriplig; låt logg spegla faktiskt begränsning.
- `tests/test_whole_day_planner.py`, `tests/test_price_optimization.py`, `tests/test_price_controller.py` — lägg ett riktat test per kodanmärkning och ett gränstest som misslyckas på `150cacb`.
- `CHANGELOG.md` — lägg F7b under 0.5.22 utan versionsändring.

**Acceptance Criteria:**
- Given ingen sol och högst 0,05 kW laddning, when dygnsplanen byggs, then ingen oskriven laddning ökar SOC.
- Given verkligt solöverskott, when dygnsplanen byggs, then bara utförbar solladdning påverkar SOC.
- Given en filtrerad urladdning, when senare åtgärder spelas upp, then ingen urladdning använder mer än faktisk energi över min-SOC.
- Given känd extern återställning med lönsam topp före och stor last efter, when standardplanen byggs, then laddningen i första segmentet styrs av dess topp och testet avslöjar regressionsrevisionen `150cacb`.
- Given prognosgränsen inte ändrar laddbeslutet, when standardplanen byggs, then flagga och controllerlogg för begränsning saknas.
- Given ett fast aggregatmål som fyller batteriet, when kostnad publiceras, then dokumentationen förklarar att basvärdet innehåller påfyllningen.

## Spec Change Log

## Review Triage Log

### 2026-10-29 — Review pass
- verdicts: 5 findings — high 0, medium 1, low 0, false 4, maybe-false 0
- findings:
  - `[false]` `[reject]` De ändrade omspelningsbesluten saknar separat aktivitetsassertion — befintlig controller loggar plan och dess orsak genom `_log_plan`; specifik ny loggrad per simulerad slot krävs inte av CAP-F7b.
  - `[false]` `[reject]` Publik baskostnad saknar nytt test med extern påfyllning — CAP-F7b tillåter dokumentation utan test för punkt 3; kodkommentar och `docs/price-optimization.md` beskriver inkluderad extern påfyllning.
  - `[false]` `[reject]` Segmenttestets regressionsegenskap saknade bevis i diffen — testet kördes i en fristående worktree på `150cacb` och misslyckades med 5,0 kWh mot gränsen cirka 1,11 kWh.
  - `[false]` `[reject]` Överensstämmelse med CAP-F7b kunde inte fastställas från diffen ensam — `SPEC.md` punkt CAP-F7b lästes och dess sju punkter täcks av ändringen och dokumentationsundantagen.
  - `[medium]` `[patch]` Solantagandet loggades inte vid byte från standard- till dygnsplan med oförändrad solgräns — loggtillståndet återställs i standardläge; ett controller-test växlar läge och verifierar loggraden.

## Design Notes

`cost_without_battery_sek` betyder kostnad utan *egna prisåtgärder*, inte ett batteri som aldrig fylls: fasta externa mål ingår i båda jämförelsebanorna. Behåll gärna nyckeln för kompatibilitet och dokumentera den. Kontrollera suffix-max per segment bakifrån; blockerade intervall ska inte bli toppar.

## Verification

**Commands:**
- `python -m pytest -q tests/test_whole_day_planner.py tests/test_price_optimization.py tests/test_price_controller.py` — riktade tester passerar.
- `python -m ruff check .` — inga lintfel.
- `python -m pyright` — inga typfel.
- `python scripts/build_release.py --check` — releasekontroll passerar.

## Auto Run Result

Status: done

**Sammanfattning:** Dygnsplanens sol- och SOC-omspelning följer utförbar energi. Standardplanens framtida prisbehov beräknas per segment och begränsningsloggning speglar ändrade beslut. Solantagandet loggas även när dygnsplanen aktiveras efter standardläge.

**Ändrade filer:** `control/whole_day_planner.py` korrigerar omspelningen; `control/price_optimization.py` korrigerar segmentbehov och suffix-max; `price_controller.py` korrigerar loggning och beskriver baskostnad; `tests/test_whole_day_planner.py`, `tests/test_price_optimization.py`, `tests/test_price_controller.py` lägger regressioner; `docs/price-optimization.md` och `CHANGELOG.md` dokumenterar beteendet utan versionsbyte.

**Granskning:** Ett medium-fynd patchades. Fyra granskningsiakttagelser avvisades efter kontroll enligt triageloggen. Inget sköts upp. Uppföljande granskning rekommenderas inte (`false`): detta pass patchade varken ett high-fynd eller minst två medium-fynd.

**Verifiering:** Riktade soltester: 4 godkända. Hela Pytest: alla tester godkända. Ruff: godkänd. Pyright: 0 fel och 0 varningar. `scripts/build_release.py --check`: `Metadata OK: 0.5.22`. Segmenttestet misslyckades som avsett på `150cacb` i fristående worktree.

**Kvarvarande risk:** Inga kända blockerande risker. En varning från `pytest-asyncio` och beroendens avskrivningsvarningar kvarstår.
