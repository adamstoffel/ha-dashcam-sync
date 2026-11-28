# Contributing

Thanks for helping. The most valuable contribution is **a driver for a camera family that is not supported yet**.

## Layout

One repository serves both install paths: the root `repository.yaml` makes it an app repository (the Supervisor finds `app/dashcam_sync`), and the root `hacs.json` with `custom_components/` makes it a HACS integration repository.

| Directory | Contents |
| --- | --- |
| `app/` | The Home Assistant app: daemon, drivers, ingress panel, and its tests |
| `custom_components/dashcam_sync/` | The integration (HACS installs this folder) |
| `integration/` | The integration's tests and tool settings; run its checks from here |
| `e2e/`, `blueprints/`, `docs/`, `scripts/` | End-to-end suite, the notification blueprint, documentation, tooling |

## Set up

```bash
# App (Python 3.13)
python3.13 -m venv .venv-app && . .venv-app/bin/activate
pip install -e "app[dev]"
cd app && ruff check . && ruff format --check . && mypy && pytest --cov

# Integration and end-to-end suite (Python 3.14, like Home Assistant itself)
python3.14 -m venv .venv-ha && . .venv-ha/bin/activate
pip install -r integration/requirements_test.txt PyTurboJPEG av numpy
cd integration && ruff check --config pyproject.toml . ../custom_components && mypy && pytest --cov
```

ffmpeg is needed for the post-processing tests (they skip without it).

Try the app without a car: `python scripts/fake_camera.py` starts a fake camera and `scripts/dev.sh` runs the daemon against it.

Or just use the Makefile: `make setup`, then `make check` (lint, types, tests, e2e, docs), `make ui` for the browser tests and `make help` for the rest. `pip install pre-commit && pre-commit install` runs ruff, actionlint and shellcheck on every commit.

## Quality bars

- App: `ruff`, `mypy --strict`, 90 % line coverage. Integration: `ruff`, 95 % coverage.
- Every reported bug gets a fixture or a test **before** its fix merges.
- Documentation is tested: relative links, screenshots, every API route (`docs/api.md`) and every app option (`DOCS.md`) must be documented, or CI fails.
- Strings in the integration follow hassfest rules (no URLs or HTML in translations; use placeholders).
- Update `CHANGELOG.md` in the same PR. User-facing text says "app", not "add-on".

## Writing a driver

See [docs/writing-a-driver.md](docs/writing-a-driver.md). In short: capture a device profile (panel › Cameras › Capture device profile), turn it into a fixture in `app/tests/fixtures/`, write the driver against it, and CI replays every fixture against every driver to catch false positives. New drivers ship as experimental until two users confirm them on real hardware.

## Trying a branch on a real Home Assistant

In the Terminal & SSH app on Home Assistant OS:

```sh
wget -qO- https://raw.githubusercontent.com/adamstoffel/ha-dashcam-sync/<branch>/scripts/install-on-ha.sh | sh -s -- <branch>
```

It copies the app into the local apps folder (built on that machine) and the integration into `custom_components`, gives the app a new version number so the Supervisor reloads its options and translations (a `-dev` suffix would not work: pre-releases sort before the installed version), and updates it. Then restart Home Assistant and hard-refresh the panel.

## Branches and releases

- `main` is the default branch and is protected by repository rulesets (Settings › Rules): work happens on a branch, lands by pull request (squash or rebase) once every CI job is green, and history stays linear. The required checks are listed by CI job name, so if you rename a job in `.github/workflows/ci.yml`, update the ruleset too or merges will wait for a check that never runs.
- Dependabot (`.github/dependabot.yml`) opens weekly PRs for workflow actions, the app's runtime dependencies and the integration's test harness. To change the app's runtime dependencies by hand, edit `app/dashcam_sync/requirements.in` and, from that folder, run `pip-compile --allow-unsafe --generate-hashes --strip-extras --output-file=requirements.txt requirements.in`. The image installs only hash-checked packages.
- Releases are tags `vYYYY.M.patch` on `main` (betas `vYYYY.M.patchbN`). Release tags are immutable. The release workflow refuses a stable tag until the hardware gate for that version passes.
- **Releasing is merging a version bump.** In the PR, bump the *release version* (the manifest, the app's `__init__.py` and `pyproject.toml`) and add the CHANGELOG entry; leave `version` in `app/dashcam_sync/config.yaml` alone. When the PR lands on `main`, `auto-release.yml` sees that `v<version>` has no tag yet and runs the release workflow: CI again, both images built, signed and pushed, the GitHub release (which creates the tag on the merged commit), and last, a commit to `main` that sets `config.yaml` to the new version. Only then does the app store offer the update, so it can never offer a version whose images do not exist; if any step fails, users keep seeing the previous version. A merge that does not change the release version releases nothing. A stable version without a passing hardware gate is skipped with a warning; record `docs/hardware-gate/<version>.md`, then re-run *Auto release* from the Actions tab (or push the tag yourself). `python scripts/check_versions.py` checks that the release versions agree and that `config.yaml` never runs ahead of them.
- **One-time setup for the last step.** `main` is protected, so the release pushes that commit with a deploy key the ruleset lets through:
  1. Make a key pair: `ssh-keygen -t ed25519 -N "" -C "dashcam-sync release" -f release_key`.
  2. Settings › Deploy keys › *Add deploy key*: paste `release_key.pub`, title it *Release*, tick **Allow write access**.
  3. Settings › Secrets and variables › Actions › *New repository secret*: name `RELEASE_DEPLOY_KEY`, value the contents of `release_key` (the private key). Then delete both files.
  4. Settings › Rules › Rulesets › the ruleset that protects `main` › *Bypass list* › *Add bypass* › **Deploy keys**, mode *Always*. Save.
  Without it the release still publishes the images and the GitHub release, and its last job fails with a message saying what is missing; add the key and re-run that job.
