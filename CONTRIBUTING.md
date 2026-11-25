# Contributing

Thanks for helping. The most valuable contribution is **a driver for a camera family that is not supported yet**.

## Layout

This repository is a monorepo that releases as two repositories (see `scripts/split_repos.sh`):

| Directory | Becomes | Contents |
| --- | --- | --- |
| `app/` | `dashcam-sync-app` | The Home Assistant app: daemon, drivers, ingress panel |
| `integration/` | `dashcam-sync-integration` | `custom_components/dashcam_sync`, blueprint, `hacs.json` |

## Set up

```bash
# App (Python 3.13)
python3.13 -m venv .venv-app && . .venv-app/bin/activate
pip install -e "app[dev]"
cd app && ruff check . && ruff format --check . && mypy && pytest --cov

# Integration (Python 3.13)
python3.13 -m venv .venv-ha && . .venv-ha/bin/activate
pip install -r integration/requirements_test.txt PyTurboJPEG av numpy
cd integration && ruff check . && pytest --cov
```

ffmpeg is needed for the post-processing tests (they skip without it).

Try the app without a car: `python scripts/fake_camera.py` starts a fake camera and `scripts/dev.sh` runs the daemon against it.

## Quality bars

- App: `ruff`, `mypy --strict`, 90 % line coverage. Integration: `ruff`, 95 % coverage.
- Every reported bug gets a fixture or a test **before** its fix merges.
- Update `CHANGELOG.md` in the same PR. User-facing text says "app", not "add-on".

## Writing a driver

See [docs/writing-a-driver.md](docs/writing-a-driver.md). In short: capture a device profile (panel › Cameras › Capture device profile), turn it into a fixture in `app/tests/fixtures/`, write the driver against it, and CI replays every fixture against every driver to catch false positives. New drivers ship as experimental until two users confirm them on real hardware.
