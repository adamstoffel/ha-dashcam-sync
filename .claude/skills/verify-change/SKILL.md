---
name: verify-change
description: Run every check this repo has before committing or pushing a change to the app, the integration, the panel, the scripts or the docs. Use after any code change in ha-dashcam-sync, before saying a change is done, and when CI fails. Lists the exact commands, which checks a change needs, and how to prove a bug fix (fail before, pass after).
---

# Verify a change (ha-dashcam-sync)

A change is done when every check below that it touches passes, and a bug fix has a test that fails on the old code. Do not push with a failing or skipped check; do not weaken a test to get green.

## Environments

Two virtualenvs at the repo root (create with `scripts/dev.py` docs in CONTRIBUTING if missing):

- `.venv-app`: the app (`app/`), Python 3.13, `pip install -e "app[dev]"`.
- `.venv-ha`: the integration (`integration/`) and the end-to-end suite (`e2e/`), Python 3.14 (what Home Assistant runs on), with `pytest-homeassistant-custom-component`.

## The checks

| Area touched | Command (from the repo root unless noted) |
| --- | --- |
| App code | `cd app && ../.venv-app/bin/ruff check . && ../.venv-app/bin/ruff format --check . && ../.venv-app/bin/mypy && ../.venv-app/bin/python -m pytest -q --cov --cov-fail-under=90` |
| Integration | `cd integration && ../.venv-ha/bin/ruff check . && ../.venv-ha/bin/ruff format --check . && ../.venv-ha/bin/python -m mypy && ../.venv-ha/bin/python -m pytest -q --cov=custom_components.dashcam_sync --cov-fail-under=95` |
| App ↔ integration behaviour | `cd e2e && ../.venv-ha/bin/python -m pytest -q --timeout=60` (run it 3 times: it catches races) |
| Panel (`app/.../ui_static`) | `node --check app/dashcam_sync/src/dashcam_sync/ui_static/app.js && node e2e/ui/panel.cjs` (add `--screens <dir>` for screenshots) |
| Scripts, docs, repo metadata | `.venv-ha/bin/python -m pytest -q scripts` and `python3 scripts/check_versions.py` |
| Home Assistant imports | `python3 scripts/check_ha_api.py` (and `latest`) whenever the integration imports something new |
| API routes | every route must be in `docs/api.md` (`app/tests/test_packaging.py` enforces it) |
| Options or schema | `config.yaml` schema ⊇ every model field; translations name every option (packaging tests) |

Run the app suite and the browser test twice when they touch timing (WebSocket, downloads, scans, polling). A test that passes only sometimes has a race: find it (usually an interim UI state or a fixed sleep) instead of retrying.

## Proving a bug fix

1. Write the test first, from the symptom (what the user saw).
2. Show it failing on the old code: `git stash push -- <changed source files>`, run the test, `git stash pop`.
3. Show it passing with the fix.
4. Say both results in the commit message.

When a test's fake differs from the real system (the fake Supervisor, the fake camera, the fake API), fix the fake to behave like the real thing first: the real-system bugs in this repo all hid behind a fake that was too kind. See `supervisor-app-review`.

## Before pushing

- `CHANGELOG.md` (root) has an *Unreleased* entry for user-visible changes.
- Docs follow the code: `app/dashcam_sync/DOCS.md`, `docs/api.md`, `docs/troubleshooting.md`, `CONTRIBUTING.md`.
- Commit messages say what was wrong and how it was proven fixed.
- Push to the session's working branch only.
