# Constraints

Last reviewed: 2026-11-07

## Floor (always enforced)
- AGENTS.md is the source of the ruleset; rule copies must stay in sync (`scripts/check-rule-copies.js`).
- All version files are pinned to the same version (`scripts/check-versions.js`).
- No new suppression comments (`eslint-disable`, `# noqa`, `# type: ignore`).
- No stubs or silent failures (empty `catch {}`, bare `except:`).
- No skipped or deleted tests without a reason in the commit message.
- No secrets in source.
- This file is not weakened to make a change pass.

## Enforced with numbers
| Dimension | Rule | Checked by | Runs at |
|---|---|---|---|
| Rule copies | in sync with AGENTS.md | `node scripts/check-rule-copies.js` | every change, CI |
| Versions | all pinned equal | `node scripts/check-versions.js` | every change, CI |
| Tests | all pass | `npm test` (CI: `.github/workflows/test.yml`) | every change, CI |
| Benchmark | selftest passes | `python3 benchmarks/agentic/run.py --selftest` | CI |

## Measured, not yet enforced (ratchets)
| Metric | Today | Direction | Tolerance |
|---|---|---|---|
| Failing tests: `node --test tests/*.test.js` | 1 of 144 fails (`csv: correct pandas one-liner passes`, needs pandas) | must not grow; target 0 | 0 |
| Suppressions: `git grep -nE "@ts-ignore\|@ts-expect-error\|eslint-disable\|biome-ignore\|# noqa\|# type: ignore\|phpcs:ignore" -- ':!node_modules' \| wc -l` | 0 | must not grow | 0 |
| Skipped tests: `git grep -nE "(\bxit\|it\.skip\|test\.skip\|describe\.skip\|\bt\.skip)\(" -- tests \| wc -l` | 2 (conditional platform skips) | must not grow | 0 |

## Exceptions
| ID | Rule | Path | Reason | Expires |
|---|---|---|---|---|
