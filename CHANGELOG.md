# Changelog

All notable changes to the integration and the app. Both are released together with the same CalVer version (`YYYY.M.patch`).

## 2026.11.0

First version. See `app/dashcam_sync/CHANGELOG.md` for the app side.

- Integration: Supervisor discovery, config/options/reauth/reconfigure flows, devices per camera, translated entities (sensors, event, image, live camera, buttons, switch, select), media source, actions (`sync`, `keep_clip`, `unkeep_clip`, `delete_clip`, `purge`), diagnostics, repair issues with fix flows, and a "Notify on new dashcam event clip" blueprint.
- Found and fixed by the end-to-end, browser and property-based suites before release:
  - the app took ~30 s to stop while an integration was connected (WebSockets are now closed on shutdown);
  - switch and select changes did not reach Home Assistant while the socket was live, and app-level issues (unsupported camera, storage low) were never pushed: hub state is now pushed and writes force a fresh read;
  - `last_event_clip` was not pushed, and neither it nor `last_sync` survived an app restart;
  - the address-block check returned a misleading result for LAN cameras;
  - a non-list `cameras` option crashed option parsing;
  - a malformed WebSocket frame could drop the integration's socket;
  - four hassfest errors (services target filter, HTML-like and URL text in translations, `fix_flow` beside `description`).
- Hardening: the wizard refuses public, loopback and Home Assistant-internal hosts; option edits are serialised.
- Push updates over a WebSocket with a 30 s poll as the fallback.
