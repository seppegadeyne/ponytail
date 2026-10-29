# Hermes fork (seppegadeyne/ponytail)

Upstream: https://github.com/DietrichGebert/ponytail. This fork only changes the Hermes
adapter (`__init__.py`, `plugin.yaml`) and adds `hermes-addendum.md`; skills and rules
files stay upstream so `git merge upstream/main` stays clean.

## What differs from upstream

- **Auto activation per session.** The ruleset is injected once (~750 tokens) only when a
  session is coding work:
  1. the first user message or subagent goal names code (a source file, a PR, a diff,
     commit, refactor, debug, tests, codebase...), or
  2. Hermes runs inside a git repo (`TERMINAL_CWD`), or
  3. mid-session, the first real code edit (`patch`/`write_file` on a source file inside a
     git repo) or a test/build/lint run in `terminal` (pytest, npm test, tsc, ruff...)
     carries the ruleset in its result. Reads, `git diff`/`gh pr` and scratch scripts
     outside a repo do not count: in a replay they mostly fired on CV, ops and research chats.
  Once active, a session stays active. Chats about anything else get nothing.
- **Skipped sources** (default `cron,tool`): never auto-activated.
- **Per-session modes.** `/ponytail lite|full|ultra|off` applies to the session it was typed
  in, not to the whole (multiplexed) gateway process.
- **Settings in config.yaml**, env vars only as fallback.
- **House-rules addendum** (`hermes-addendum.md`): project AGENTS.md/CONTRIBUTING.md win,
  requested robustness is scope, reply in the user's language, verify with real runs.

## Settings

```yaml
plugins:
  entries:
    ponytail:
      settings:
        default_mode: full          # lite | full | ultra | off  (env PONYTAIL_DEFAULT_MODE)
        activation: auto            # auto | always | manual     (env PONYTAIL_ACTIVATION)
        skip_sources: [cron, tool]  #                            (env PONYTAIL_SKIP_SOURCES)
        addendum: true              # false drops hermes-addendum.md
```

## Install

`hermes plugins install` currently fails on Hermes' install gate for this repo (root
`plugin.json` without Agent Plugins schema, NousResearch/hermes-agent#125937). Until then:

```bash
git clone https://github.com/seppegadeyne/ponytail ~/.hermes/plugins/ponytail
hermes plugins enable ponytail
```

## Tests

```bash
python3 tests/test_hermes_fork.py
node --test tests/hermes-plugin.test.js
```

## Sync upstream

```bash
git fetch upstream && git merge upstream/main && python3 tests/test_hermes_fork.py
```
