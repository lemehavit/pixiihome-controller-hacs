---
title: 'F6-revert â€“ ta ur F6 ur PR 2'
type: 'bugfix'
created: '2026-10-30'
status: 'done'
review_loop_iteration: 0
baseline_revision: d7e355ee6c278122053d7b6120ee9b24b36e16c6
followup_review_recommended: false
context: []
warnings: []
deferred: []
---

<intent-contract>

## Intent

**Problem:** F6:s behovsgrÃ¤ns fÃ¶r laddning har blockerats i fyra granskningar och ska inte ingÃ¥ i PR 2. Standardplaneraren ska Ã¥ter dimensionera laddning efter ledigt batteriutrymme.

**Approach:** Ta bort F6:s framtida behovsberÃ¤kning och dess signalering. BehÃ¥ll F4, F5, F7, F7b och F3c, inklusive de delar av prisbas och urladdningslogik som fungerar med laddning efter utrymme.

## Boundaries & Constraints

**Always:** Version 0.5.22; enbart nya commits pÃ¥ PR 2-grenen; fulla angivna kontroller ska passera. Dokumentera att F6 skjuts till egen PR.

**Never:** Ã„ndra live-systemet, merge till main, tagga, slÃ¤pp release, skriv om historik eller refaktorera orelaterad kod.

</intent-contract>

## Code Map

- `custom_components/pixiihome_controller/control/price_optimization.py:190-200,375-750` â€” `PricePlan.charge_demand_limited`, F6:s suffix-toppar och behovsgrÃ¤ns vid laddning; behÃ¥ll segment, externa fÃ¶nster, prisbas och begrÃ¤nsning av faktisk urladdning till prognostiserad last.
- `custom_components/pixiihome_controller/price_controller.py:970-1010` â€” F6-specifik aktivitetslogg fÃ¶r begrÃ¤nsad laddning; ta bort nÃ¤r flaggan fÃ¶rsvinner.
- `tests/test_price_optimization.py:55-210` â€” F6-fall som krÃ¤ver behovsgrÃ¤ns och F7b/F3c-regressioner; ersÃ¤tt endast F6-fÃ¶rvÃ¤ntningar med tydligt utrymmesfall.
- `tests/test_price_controller.py:1618-1675` â€” F6-loggtester; anpassa till frÃ¥nvaro av sÃ¥dan logg, behÃ¥ll andra controller-tester.
- `CHANGELOG.md:1-16`, `docs/price-optimization.md:180-195` â€” fÃ¶rklara uppskjutet F6 och Ã¥terstÃ¤lld ladddimensionering.
- `custom_components/pixiihome_controller/control/whole_day_planner.py` â€” F5/F7/F7b-beteende, lÃ¤sbevis fÃ¶r ofÃ¶rÃ¤ndrad kod.

## Tasks & Acceptance

**Execution:**
- `custom_components/pixiihome_controller/control/price_optimization.py` â€” ta bort F6-behovsgrÃ¤ns och oanvÃ¤nda hjÃ¤lpdata; lÃ¥t laddning styras av `min(maximum_charge_kw, headroom / (hours * charge_efficiency))` och behÃ¥ll genomfÃ¶rbar urladdning.
- `custom_components/pixiihome_controller/price_controller.py` â€” ta bort F6-specifik begrÃ¤nsningslogg om dess flagga inte lÃ¤ngre anvÃ¤nds.
- `tests/test_price_optimization.py`, `tests/test_price_controller.py` â€” ersÃ¤tt F6-fÃ¶rvÃ¤ntningar med regression fÃ¶r utrymmesbaserad laddning; bevara andra tester.
- `CHANGELOG.md`, `docs/price-optimization.md` â€” notera att F6 skjuts upp till separat PR och beskriv aktuellt beteende.

**Acceptance Criteria:**
- Given lÃ¶nsam senare pristopp och prognostiserad nollast med export av, when standardplanen skapas, then laddning dimensioneras av ledigt utrymme och effektgrÃ¤ns.
- Given kÃ¤nda externa fÃ¶nster och senare nettolast, when standardplanen skapas, then segment och faktisk urladdning fÃ¶ljer befintliga begrÃ¤nsningar utan F6-laddtak.
- Given PR 2-koden, when de angivna kontrollerna kÃ¶rs, then ruff, pyright, pytest och release-kontroll passerar med version 0.5.22.

## Spec Change Log

## Review Triage Log

### 2026-10-30 — Granskning
- verdicts: 0 findings — high 0, medium 0, low 0, false 0, maybe-false 0
- findings: inga

## Verification

**Commands:**
- `python -m ruff check .` â€” inga fel.
- `python -m pyright` â€” inga fel.
- `python -m pytest -q` â€” alla tester godkÃ¤nda.
- `python scripts/build_release.py --check` â€” metadata 0.5.22 godkÃ¤nd.
- `git diff --check` â€” inga whitespacefel.

## Auto Run Result

Status: done

F6:s behovsgräns, flagga och controller-logg har tagits bort. Standardplanen laddar åter efter ledigt utrymme och effektgräns. Prognosbegränsad faktisk urladdning och externa segment finns kvar.

Ändrade filer: prisplaneraren, price_controller, två riktade testfiler, CHANGELOG.md och docs/price-optimization.md.

Granskning: inga fynd; inga patchar eller uppskjutna punkter. Uppföljande granskning rekommenderas inte.

Verifierat: Ruff utan fel; Pyright 0 fel och 0 varningar; hela Pytest-sviten godkänd; release-kontroll Metadata OK: 0.5.22; git diff --cached --check utan fel.

Kvarstående risk: ingen känd inom F6x.
