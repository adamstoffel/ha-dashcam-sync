---
name: supervisor-app-review
description: Review or change the Home Assistant app (formerly add-on) in app/dashcam_sync for things that only break on a real Home Assistant OS with the Supervisor - options round-trips, schema, roles, AppArmor, ingress, discovery, versions, local installs, caching. Use when touching config.yaml, supervisor.py, daemon.py option handling, api.py routes, the panel's serving, or when a bug shows up only on a real install.
---

# Supervisor app review

Every bug found by real-world testing so far came from a test fake that was kinder than the real Supervisor or browser. Check each point against the real behaviour, and make the fakes match it.

## Options

- `POST /addons/self/options` only **stores** options in the Supervisor. `/data/options.json` is written only when the app **starts**. Anything that saves options must also update the file (`SupervisorOptionsStore.write`) and anything edited in the Configuration tab arrives only through `/addons/self/info` → `options` (`SupervisorOptionsStore.sync`).
- Before a read-modify-write of options, sync from the Supervisor first, or a recent Configuration-tab edit is overwritten.
- The Supervisor validates the whole options object against `config.yaml`'s `schema`. Every model field the app writes must be in the schema (`test_every_setting_the_panel_can_save_is_in_the_schema`); unknown keys are dropped with a warning.
- The Configuration tab preselects the **first** choice of an optional `list(...)`. Put the safe default first.
- The test fake Supervisor (`fake_supervisor` in `app/tests/test_daemon_api.py`) must keep options only in memory and never write the file.

## Permissions (security rating 8, `hassio_role: default`)

- The default role reaches `/network/info`, `/discovery`, `/addons/self/*` (one path segment), and `/.+/info`. It cannot scan Wi-Fi or write network settings: those go through the integration (`scan_wifi`, `join_wifi` pushes). `scripts/check_supervisor_roles.py` verifies the role rules against the Supervisor's source.
- Never raise the role or add `host_network`/`privileged` to work around a missing permission; ask Home Assistant through the integration instead.
- AppArmor (`apparmor.txt`) must allow every path the app writes: `/data/**`, `/media/**`, `/share/**`, `/tmp`. A new write location needs a rule.

## Ingress and the panel

- Only the Supervisor's ingress proxy may reach port 8099 (`ingress_allow`). The token API (8100) serves secrets; the ingress API never does (`app[SECRETS]`).
- All panel URLs are relative (it lives under `/api/hassio_ingress/<token>/`).
- Browsers keep an iframe's files cached across updates, and a hard refresh does not reliably reach them: `index.html` is `no-cache` and names assets with `?v=<content hash>`. Never add an unversioned asset reference.
- `/config/...` links to Home Assistant pages need `target="_top"`.

## Versions and local installs

- A local app (in `/local_apps` or `/addons`) is rebuilt from source; the Supervisor reloads its metadata (schema, translations) only when the **version** changes. A pre-release suffix (`-dev`) sorts *older* than the installed release and disables Update. `scripts/install-on-ha.sh` sets `YYYY.M.<timestamp>`.
- `homeassistant:` in config.yaml and the root `hacs.json` is the minimum Home Assistant version; `scripts/check_ha_api.py` checks the integration's imports against it.

## Discovery and the token

- Discovery is posted on start and deleted on stop. The token is generated once in `/data` (or set via the `api_token` option) and is never logged.
- An uninstall wipes `/data`: new token and instance id. The integration must cope (reauth or a new discovery).

## Checklist for a change

1. Which real Supervisor or browser behaviour does this depend on? Is it in the fake?
2. Does it write somewhere new (AppArmor), call a new Supervisor endpoint (role), save an option (schema, file mirror), or add a route (docs/api.md, ingress vs token port)?
3. Would it survive: an app restart, a Home Assistant restart, an update with the same version, an uninstall/reinstall, a cached panel?
