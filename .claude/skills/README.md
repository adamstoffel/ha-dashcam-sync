# Skills for this repository

Claude Code loads these from `.claude/skills/`. Third-party skills are vendored **unmodified**, each with its licence; update them by copying a newer upstream folder and recording the commit below.

## Third-party

| Skill | Use it for | Source (commit) | Licence |
| --- | --- | --- | --- |
| `ha-integration-knowledge` | Building, testing and reviewing the integration in `integration/` | [home-assistant/core](https://github.com/home-assistant/core/tree/dev/.claude/skills) `.claude/skills` @ `11de95a` | Apache-2.0 |
| `ha-review` | Code reviews (diff against `main` here, not `dev`) | home-assistant/core @ `11de95a` | Apache-2.0 |
| `ha-quality-scale-verify` | Checking `integration/.../quality_scale.yaml` claims rule by rule | home-assistant/core @ `11de95a` | Apache-2.0 |
| `web-design-guidelines` | Auditing the ingress panel (`app/dashcam_sync/src/dashcam_sync/ui_static/`) | [vercel-labs/agent-skills](https://github.com/vercel-labs/agent-skills) @ `5a37a63` (rules pinned by SHA-256 inside) | MIT |

### Home Assistant skills in a custom-integration repository

They are written for Home Assistant Core. Map their paths and tools like this:

| In the skill | In this repository |
| --- | --- |
| `homeassistant/components/<domain>/` | `custom_components/dashcam_sync/` (tests in `integration/tests/`) |
| `tests/components/<domain>/` | `integration/tests/` |
| `python -m script.hassfest ...` | the `hassfest` CI job (`home-assistant/actions/hassfest`) |
| base branch `dev` | `main` |
| library on PyPI | none: the integration talks to the app's local API (`integration/.../api.py`) |
| translations generated from `strings.json` | `strings.json` and `translations/en.json` are both committed and must match |

## Recommended plugins (installed per user, not vendored)

- [obra/superpowers](https://github.com/obra/superpowers): `systematic-debugging` (root cause before fixes) and `verification-before-completion` (evidence before claims) apply to every change here.
- `agent-protocols` and `backend-design`: code review, security and audit passes on the app daemon and its API.

## This repository's own

Only where the knowledge exists nowhere else:

| Skill | Use it for |
| --- | --- |
| `verify-change` | The exact commands for every check in this repo, and how to prove a fix |
| `supervisor-app-review` | Behaviour of a real Home Assistant OS Supervisor and browser that the test fakes must match (every real-world bug so far came from here) |
| `download-integrity-review` | The invariants that keep interrupted downloads, crashes and moves from losing or corrupting clips |
