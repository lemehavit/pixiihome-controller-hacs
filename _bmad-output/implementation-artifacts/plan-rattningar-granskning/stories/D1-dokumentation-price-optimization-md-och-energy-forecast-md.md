---
title: 'D1: Dokumentation price-optimization.md och energy-forecast.md'
type: 'chore'
created: '2026-10-29'
status: 'done'
baseline_revision: 'c31f534c7107c73e462edd76d93b1494f8e2cbe4'
review_loop_iteration: 0
followup_review_recommended: false
context: []
warnings: []
deferred: []
---

<intent-contract>

## Intent

**Problem:** Två användardokument säger emot koden. `docs/price-optimization.md` (~rad 160–162) påstår att sänkta aggregatormåls original bara hålls i minnet och glöms vid omstart, men sedan 0.5.17 sparas de i journalen (`lowered_external_targets`) och läses in igen. `docs/energy-forecast.md` (~rad 70–73) säger att statusen är `deferring` när regulatorn laddar direkt, och nämner attributet `reachable_soc_percent`, som inte finns – det heter `reachable_usable_soc_percent`.

**Approach:** Skriv om just de meningarna så att de beskriver det koden gör: originalen sparas i kontrolljournalen och används efter omstart; `deferring` betyder att regulatorn *väntar* (säljer nu, laddar senare), och beräkningen visas som attribut på solregulatorn, bland annat `reachable_usable_soc_percent`.

## Boundaries & Constraints

**Always:** Dokumenten är på engelska och ska förbli det; behåll stil, radbredd (~80 tecken) och närliggande text. Varje påstående ska kunna beläggas i koden som Code Map pekar på.

**Never:** Ingen kodändring. Inga andra avsnitt eller dokument. Inga löften om beteende som koden inte har (t.ex. att sparningen alltid lyckas – den är "best effort").

</intent-contract>

## Code Map

- `docs/price-optimization.md:160-162` -- punkten "A lowered target is restored…" med den felaktiga meningen "Originals are kept in memory, so a Home Assistant restart forgets them; check the scheduler view after a restart."
- `docs/energy-forecast.md:70-74` -- stycket "If the target can no longer be reached…" med fel `deferring`-beskrivning och `reachable_soc_percent`.
- `custom_components/pixiihome_controller/runtime.py:1384-1390` -- `async_save_lowered_targets`: "Remember lowered aggregator targets across restarts (best effort)", skriver till kontrolljournalen.
- `custom_components/pixiihome_controller/runtime.py:432-434` -- vid start läses `LOWERED_EXTERNAL_TARGETS` (`"lowered_external_targets"`, `const.py:15`) från journalens sparade tillstånd.
- `custom_components/pixiihome_controller/price_controller.py:1352-1368` -- första varvet efter start tar över de sparade referenserna, så ett mål kan höjas tillbaka när planen behåller mer; ändringar sparas efter varje varv.
- `custom_components/pixiihome_controller/control/smart_solar.py:34-45` -- `SmartSolarDecision.as_dict`: `deferring` (bool = väntar), `decision`, `reachable_usable_soc_percent` m.fl.
- `custom_components/pixiihome_controller/solar_controller.py:146-151` -- attributen på solregulatorn: `smart_solar_charging` är bara switchens läge; beslutets fält läggs ut som egna attribut, plus `smart_hold_until`.
- `custom_components/pixiihome_controller/solar_controller.py:419-422, 463-465` -- status `deferring` publiceras bara när den väntar; när målet inte längre nås laddar den och håller laddningen (`charging_held_after_deferral`, `smart_hold_until`).

## Tasks & Acceptance

**Execution:**
- `docs/price-optimization.md` -- ersätt minnesmeningen med: originalen sparas i kontrolljournalen (`lowered_external_targets`) och används efter en omstart så att målet kan återställas; om journalen inte kan sparas, kontrollera schemavyn -- rättar fel påstående.
- `docs/energy-forecast.md` -- skriv om stycket: när det väntar är statusen `deferring` (attributet `deferring` = `true`); när målet inte längre nås laddar den direkt i minst 30 minuter och `deferring` är `false`, med `decision`/`reason_text` som förklaring; beräkningen visas i solregulatorns attribut, bl.a. `reachable_usable_soc_percent` -- rättar status- och attributnamn.

**Acceptance Criteria:**
- Given den uppdaterade `docs/price-optimization.md`, when man läser punkten om återställning, then står det att originalen sparas i journalen (`lowered_external_targets`) och överlever en omstart, och texten "kept in memory" finns inte kvar.
- Given den uppdaterade `docs/energy-forecast.md`, when man söker efter `reachable_soc_percent`, then finns bara `reachable_usable_soc_percent`, och `deferring` beskrivs som att regulatorn väntar, inte laddar.
- Given hela grenen, when CI-kontrollerna körs, then går `ruff check .` och `pytest tests/test_release_package.py` igenom (docs ingår inte i paketet).

## Spec Change Log

## Review Triage Log

### 2026-10-29 — Review pass
- verdicts: 4 findings — high 0, medium 0, low 1, false 3, maybe-false 0
- findings:
  - `[low]` `[patch]` "While it waits, the status … is `deferring`" stämmer inte när väntan börjar under en aktiv laddlease: då publiceras `lease_expiring` med orsak `smart_solar_deferring` (`solar_controller.py:382-384`) — åtgärd: meningen nämner nu `lease_expiring` som alternativ status; `deferring`-attributet är `true` i båda fallen.
  - `[false]` `[reject]` Vilken `reason_text` som visas under 30-minutersspärren anges inte — texten säger bara att `decision`/`reason_text` förklarar varför, vilket stämmer (`decision` = `charging_held_after_deferral`, `solar_controller.py:464`); inget felaktigt påstående.
  - `[false]` `[reject]` Planens "sedan 0.5.17" saknas i dokumentet — användardokumenten beskriver nuvarande beteende och avsiktet kräver ingen versionsuppgift; inget fel uppstår.
  - `[false]` `[reject]` Docs har ingen automatisk kontroll — gäller hela docs-katalogen, inte ett fel i diffen; verifieringen gjordes genom läsning mot koden och grep.

## Verification

**Commands:**
- `grep -n "kept in memory\|reachable_soc_percent" docs/price-optimization.md docs/energy-forecast.md` -- expected: ingen träff
- `grep -n "lowered_external_targets" docs/price-optimization.md` -- expected: minst en träff
- `uv run pytest tests/test_release_package.py -q` (eller projektets pytest) -- expected: grönt

## Auto Run Result

Status: done

**Sammanfattning:** Två stycken i användardokumentationen rättade så att de stämmer med koden.

**Ändrade filer:**
- `docs/price-optimization.md` — originalen för sänkta aggregatormål sparas i kontrolljournalen (`lowered_external_targets`) och läses in efter omstart; sparningen är "best effort".
- `docs/energy-forecast.md` — `deferring` betyder att regulatorn väntar (status `deferring`, eller `lease_expiring` medan en aktiv lease löper ut); laddar den direkt är `deferring` = `false`; attributet heter `reachable_usable_soc_percent` och visas bland solregulatorns attribut.

**Granskning:** 1 patch (low), 0 uppskjutna, 3 avvisade (false, se triageloggen). Patchade per verdikt: high 0, medium 0, low 1.

**Uppföljande granskning:** false — ingen high och färre än två medium patchade.

**Verifiering:** `grep "kept in memory\|reachable_soc_percent"` i båda filerna: ingen träff. `grep lowered_external_targets docs/price-optimization.md`: 1 träff. `pytest tests/test_release_package.py`: 1 passed (via `uv run --no-project --with pytest`, projektets `.venv` saknar Python). `ruff check .`: All checks passed.

**Kvarvarande risker:** Ingen automatisk kontroll av dokumenttext; noggrannheten bygger på läsning mot koden i Code Map.
