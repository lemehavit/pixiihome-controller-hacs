---
title: Sista uppföljningsrundan – icke-blockerande granskningsanmärkningar
type: bugfix
created: '2026-10-30'
status: done
baseline_revision: c820332f5fcd7e90c9c1d2b1a90c45f0cdb93dcf
review_loop_iteration: 0
followup_review_recommended: true
context: []
warnings: [multiple-goals, oversized]
deferred: []
---

<intent-contract>

## Intent

**Problem:** Kvarvarande icke-blockerande anmärkningar på PR 1 och PR 2 ger missvisande återställningsorsaker, sårbar avlastning, otillräckliga regressionstester och språkbrott i dokumentationen.

**Approach:** Åtgärda CAP-F3c och styrelsens kommentarer ”F3c-omfattning” del 1 och 2 med riktade tester, först på PR 1, därefter via merge-commit till PR 2 och PR 2:s egna punkter.

## Boundaries & Constraints

**Always:** Behåll 0.5.21 på PR 1 och 0.5.22 på PR 2. Ett test per kodanmärkning. Committa och pusha PR 1, slå ihop den i PR 2 med merge-commit utan rebase eller force-push, verifiera och pusha PR 2. Kör Ruff, Pyright, Pytest och releasekontroll.

**Never:** Rör live-HA, Gateway eller MQTT. Slå inte ihop till `main` och gör ingen release. Ändra inte F6-logiken i `price_optimization.py` eller prognosanmärkningen vid rad 381–388. PR 2:s punkt om `unrestricted_power` är enbart ett test; om testet kräver logikändring ska den punkten rapporteras som ej utförd.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|---------------|---------------------------|----------------|
| Faslease vid aktiv prepare | Service 129-lease finns | Ingen loggrad som säger att styrningen är av | Bevara återställningsbehov |
| Fasjournalfel följt av lyckad Service 120-läsning | Fasjournalen fortfarande felaktig | `startup_read_error` kvarstår | Återhämtning först efter rätt källas läsning |
| Avlastning utan scheduler | Faslease finns, `gateway_scheduler` saknas | Övrig unload fullföljs | `native_meter_unavailable` rapporteras |
| Strukturerad återställningsorsak | `ValueError` med känd kod | Sensorn visar samma kod | Oväntat fel ger `meter_limitation_restore_failed` |
| Saknad journal med tidigare fel | `control_state_error` finns | Ursprunglig orsak bevaras | Aviseringsfel döljer inte journalfelet; städproblem loggas |
| Filtrerad laddning före urladdning | Planerad laddning skrivs inte | Min-SOC hålls | Ingen negativ energibana |

</intent-contract>

## Code Map

- `custom_components/pixiihome_controller/idle_phase_controller.py:70-100,338-395` — logg från `load_saved_lease`, `prepare` och `stop`/`release`; för `control_off` vidare och fånga frånvarande scheduler vid avlastning.
- `custom_components/pixiihome_controller/meter_limit_controller.py:110-115,190-195,295-325` — Service 120-läsning, felkälla och strukturerade skäl i inaktiv `tick`.
- `custom_components/pixiihome_controller/__init__.py:546-568,610-622` — startläsning från två journaler och unload-ytan.
- `custom_components/pixiihome_controller/runtime.py:680-795` — saknad journal, felorsak, notifiering och `_authority_cleanup_persisted`.
- `tests/test_controller_coexistence.py:77-185,509-735` — återstart, återställning och sensor; utöka för PR 1.
- `tests/test_authority_release.py:189-310` — journalfel och avisering; utöka för PR 1.
- `tests/test_runtime_scheduler.py:720-760` — befintlig `async_unload_entry`-kontroll; utöka för schedulerlöst faslease.
- `custom_components/pixiihome_controller/control/price_optimization.py:526` — befintlig `unrestricted_power`-vakt; endast nytt PR 2-test i `tests/test_price_optimization.py:160-180`.
- `custom_components/pixiihome_controller/control/whole_day_planner.py:396-398` — befintligt urladdningsklipp; nytt PR 2-test i `tests/test_whole_day_planner.py:286-340`.
- `custom_components/pixiihome_controller/price_controller.py:282-283`, `docs/price-optimization.md:189-194` — svensk kommentar och dokumentation i engelsk kontext.
- `CHANGELOG.md` och `_bmad-output/implementation-artifacts/plan-rattningar-granskning/stories/F3b-pr1-uppfoljning-granskningsanmarkningar-f3-f8-f1.md:71-97` — rätta tom rad och elva skadade svenska textrader på PR 1; lägg F3c-notiser i befintliga versionsposter.

## Tasks & Acceptance

**Execution:**
- `custom_components/pixiihome_controller/idle_phase_controller.py`, `custom_components/pixiihome_controller/meter_limit_controller.py`, `custom_components/pixiihome_controller/__init__.py` — rätta de tre återställnings- och unloadpunkterna på PR 1.
- `custom_components/pixiihome_controller/runtime.py`, `custom_components/pixiihome_controller/meter_limit_controller.py` — bevara ursprungsfel, isolera aviseringsfel, sätt persistens explicit och släpp igenom kända strukturerade koder på PR 1.
- `tests/test_controller_coexistence.py`, `tests/test_authority_release.py`, `tests/test_runtime_scheduler.py` — ett regressionsfall per kodanmärkning, inklusive unload.
- `_bmad-output/implementation-artifacts/plan-rattningar-granskning/stories/F3b-pr1-uppfoljning-granskningsanmarkningar-f3-f8-f1.md` och `CHANGELOG.md` — reparera text och 0.5.21-posten på PR 1; committa, pusha och merge-committa in i PR 2.
- `tests/test_price_optimization.py`, `tests/test_whole_day_planner.py` — lägg regressionsfall för de två PR 2-punkterna utan F6-kodändring.
- `custom_components/pixiihome_controller/price_controller.py`, `docs/price-optimization.md`, `CHANGELOG.md` — skriv de två engelska texterna och 0.5.22-posten; committa och pusha PR 2.

**Acceptance Criteria:**
- Given en aktiv `prepare()` med sparad faslease, when återställning körs, then loggen säger inte att styrningen är av.
- Given två journalfelkällor, when bara Service 120-läsningen lyckas, then fasfelet kvarstår i sensorn tills fasjournalen lyckas.
- Given faslease utan scheduler, when integrationen unloadas, then övriga stoppsteg körs och felet rapporteras.
- Given en känd strukturerad `ValueError`, when Service 120-återställning misslyckas, then sensorn behåller koden.
- Given en saknad journal och ett tidigare fel, when behörighet stängs av och aviseringen misslyckas, then ursprungsfelet och journalundantaget bevaras och städproblemen loggas.
- Given en filtrerad laddning följd av urladdning, when dygnsplanen spelas upp, then min-SOC hålls i controller-synlig plan.
- Given en liten laddgräns under 0,05 kW, when behovsgränsen är mindre, then nytt test misslyckas på `92b80ec` och passerar på PR 2 utan F6-kodändring.

## Spec Change Log

## Review Triage Log

### 2026-10-30 — Review pass
- verdicts: 2 findings — high 0, medium 2, low 0, false 0, maybe-false 0
- findings:
  - `[medium]` `[patch]` Fasjournalens felägarskap saknade uppstartstest — `tests/test_runtime_scheduler.py` startar nu med ogiltig fasjournal, gör en lyckad Service 120-läsning och kontrollerar att sensorn fortfarande visar `idle_phase_journal_invalid`.
  - `[medium]` `[patch]` Testet för filtrerad laddning observerade bara planerarens interna slots — `tests/test_price_controller.py` kontrollerar nu publicerad min-SOC och segment efter filtrering; testet faller på `92b80ec`.

## Design Notes

Den kända koden för `ValueError` ska vara en namngiven tillåten mängd, inte varje godtycklig snake_case-sträng. Skilj felkällor för Service 120 och fasjournal så en lyckad läsning inte raderar ett annat fel. Avisering får aldrig hindra det ursprungliga journalfelet från att nå anroparen.

## Verification

**Commands:**
- `python -m pytest -q` — alla simulerade tester passerar på varje färdig gren.
- `python -m ruff check .` — inga lintfel.
- `python -m pyright` — inga typfel.
- `python scripts/build_release.py --check` — respektive versionsmetadata passerar.

## Auto Run Result

Status: done

**Sammanfattning:** PR 1 bevarar separata återställningsorsaker, klarar avlastning utan scheduler och skyddar journalfelet mot misslyckad avisering. PR 2 har två regressionstester som faller på äldre kod och engelska controller- och dokumentationstexter. F6-logiken är orörd.

**Ändrade filer:** `idle_phase_controller.py`, `meter_limit_controller.py`, `__init__.py` och `runtime.py` rättar PR 1:s återställning; `tests/test_controller_coexistence.py`, `tests/test_authority_release.py` och `tests/test_runtime_scheduler.py` täcker PR 1; `tests/test_price_optimization.py`, `tests/test_whole_day_planner.py` och `tests/test_price_controller.py` täcker PR 2; `price_controller.py`, `docs/price-optimization.md`, `CHANGELOG.md` och F3b-storyfilen rättar text och versionsnotiser.

**Granskning:** Två medium-fynd patchades med ytterligare tester. Inga fynd sköts upp eller avvisades. Uppföljande granskning rekommenderas (`true`) enligt arbetsflödets två-medium-tröskel; styrelsens beslut begränsar framtida icke-blockerande anmärkningar till PR-kommentarer.

**Verifiering:** Hela Pytest-sviten, Ruff, Pyright och `scripts/build_release.py --check` passerade på båda grenarna efter rättningarna; Pyright gav 0 fel och 0 varningar, releasekontrollen visade 0.5.21 respektive 0.5.22. De två PR 2-regressionstesterna och det controller-synliga SOC-testet misslyckades som avsett på `92b80ec`. `git diff --check` passerade.

**Kvarvarande risk:** Tester använder simulerad Home Assistant, Gateway och MQTT. Verklig uppstarts- och loggbokssekvens har inte körts live enligt uppdraget.
