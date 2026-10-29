---
title: 'PR1-uppföljning – granskningsanmärkningar F3/F8/F1'
type: bugfix
created: '2026-10-29'
status: done
baseline_revision: 'f586578060bce4a125e43ce5ee889818d57a72a9'
review_loop_iteration: 0
followup_review_recommended: true
context: []
warnings: [oversized]
deferred: []
---

<intent-contract>

## Intent

**Problem:** Sju granskningsanmärkningar i F3, F8 och F1 lämnar kvar felaktigt återställningsläge, missad faslease, missvisande aktivitet eller för svaga tester. De måste åtgärdas på PR1 utan att ändra releaseversionen.

**Approach:** Rätta de sju avgränsade fallen i berörda kontrollrar, och lägg ett regressionstest per punkt. Uppdatera CHANGELOG för 0.5.21.

## Boundaries & Constraints

**Always:** Bevara säkert återställningsbeteende: läs före skrivning, skriv aldrig om ett osäkert anrop, lämna externa scheman orörda. Logga åtgärd och orsak. Versionen 0.5.21 är synkron i manifest.json, const.py och pyproject.toml. Kör ruff, pyright, pytest och build_release --check. Efter commit: pusha PR1, merge-commit till PR2, testa och pusha PR2.

**Never:** Rör live-systemet, merge till main, gör release eller refaktorera andra stories.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|---------------|----------------------------|----------------|
| Gammalt läsfel | Senare lyckad läsning eller prepare | startup_read_error rensas; ingen kvarstående recovery_required | Misslyckad läsning behåller fel |
| Faslease utan gateway | Lease laddad då scheduler saknas; scheduler tillkommer | release återställer lease | Konflikt blir recovery_required |
| Ursprungsinställning | Andra läsningen före save visar original | Ingen falsk konflikt | Annat värde ger konflikt |
| Policyfel | plan_phases ger ValueError med annan orsak än policy | Ingen policyspärr i loggen | Orsaken förs vidare |
| Native tjänst | Tidig retur med annat srv_type | policy_note rensas | Lease återställs först |

</intent-contract>

## Code Map

- `custom_components/pixiihome_controller/meter_limit_controller.py` — startup_read_error (44, 318–322), load_saved_lease-aktivitet (122–146), prepare (164–190).
- `custom_components/pixiihome_controller/idle_phase_controller.py` — lease/gateway (65–91, 317–357), ValueError (184–206), tidig native-retur (231–236).
- `custom_components/pixiihome_controller/__init__.py` — startens läsfelslagring (546–565).
- `custom_components/pixiihome_controller/price_controller.py` — loggboksorsak (177–183); `tests/test_price_controller.py` — aviseringstest (1077–1110).
- `tests/test_controller_coexistence.py` — lease-, återställnings- och policytester; lägg sex fall här.
- `CHANGELOG.md`, `custom_components/pixiihome_controller/manifest.json`, `custom_components/pixiihome_controller/const.py`, `pyproject.toml` — releasepost och versionskontroll.

## Tasks & Acceptance

**Execution:**
- `custom_components/pixiihome_controller/meter_limit_controller.py`, `custom_components/pixiihome_controller/__init__.py` — rensa gammalt läsfel efter lyckad läsning/prepare; flytta omstartslogg till avstängt läge.
- `custom_components/pixiihome_controller/idle_phase_controller.py` — återskapa gateway för sparad lease när scheduler återkommer; acceptera original i andra läsningen; skilj policyorsak från övrigt ValueError; rensa policy_note vid native-retur.
- `tests/test_controller_coexistence.py` — separata regressioner för punkterna 1–4, 6–7.
- `tests/test_price_controller.py` — kontrollera orsak i loggbokshändelsens data, punkt 5.
- `CHANGELOG.md` — skriv uppföljningen under 0.5.21.

**Acceptance Criteria:**
- Given ett tidigare startup-läsfel, when nästa läsning eller prepare lyckas, then avstängd styrning visar inte längre recovery_required för det felet.
- Given en sparad faslease utan gateway, when scheduler återkommer, then lease återställs och journalen rensas.
- Given prepare under aktiv styrning, when sparad lease laddas, then loggen säger inte att styrningen är av.
- Given att Service 129 redan har originalvärden vid andra läsningen, when release fortsätter, then ingen falsk konflikt uppstår.
- Given prisoptimering blockerad över gränsen, when händelsen loggas, then testet verifierar orsaksfältet.
- Given ett icke-policyrelaterat ValueError från plan_phases, when planen avbryts, then ingen policyspärr loggas.
- Given tidigare policy_note och en annan native-tjänst, when tick returnerar tidigt, then policy_note är nollställd.

## Spec Change Log

## Review Triage Log

### 2026-10-29 ? Review pass
- verdicts: 3 findings ? high 0, medium 2, low 0, false 1, maybe-false 0
- findings:
  - `[medium]` `[patch]` Startv?gens logg saknade ett test ? lade kontroll av control-off-orsak i `test_runtime_scheduler.py`.
  - `[medium]` `[patch]` Native kapacitetsbrist saknade regressionsfall ? lade test med noll urladdningskapacitet och aktiv policygr?ns.
  - `[false]` `[reject]` F8-orsaken finns inte som f?lt i `logbook_entry` ? befintlig `log_activity` skickar ett separat orsaksh?ndelse med samma context; testet kontrollerar just den kopplingen.

## Verification

**Commands:**
- `ruff check .` — utan fel.
- `pyright` — utan fel.
- `pytest` — utan fel.
- `python scripts/build_release.py --check` — utan fel.



## Auto Run Result

Status: done

- ?ndring: sju F3b-anm?rkningar ?tg?rdade med regressionstester och uppdaterad 0.5.21-post i CHANGELOG.
- Filer: meterbegr?nsning och uppstart rensar gamla l?sfel och loggar korrekt; fasst?d ?terst?ller lease och skiljer policyorsaker; testerna t?cker varje punkt.
- Granskning: tv? medelstora verifieringsluckor lagades; ingen uppskjuten; en felaktig l?sning av F8:s h?ndelsemodell avvisades enligt loggbokens befintliga context-koppling.
- Uppf?ljande granskning: true. Tv? medium-patchar g?llde testt?ckning; kvarst?ende risk ?r att HA:s verkliga loggbokspresentation och Gateway-?terkomst inte ?r live-verifierade.
- Verifiering: ruff, pyright, pytest och build_release --check passerade p? PR1 efter kod?ndringar; tv? riktade tester passerade efter granskningspatcharna.
- Kvarvarande risk: uppstart och gateway ?r simulerade i tester; inget live-system har anv?nts.
