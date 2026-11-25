# Changelog

All notable changes to the integration and the app. Both are released together with the same CalVer version (`YYYY.M.patch`).

## 2026.11.0

First version. See `app/dashcam_sync/CHANGELOG.md` for the app side.

- Integration: Supervisor discovery, config/options/reauth/reconfigure flows, devices per camera, translated entities (sensors, event, image, live camera, buttons, switch, select), media source, actions (`sync`, `keep_clip`, `unkeep_clip`, `delete_clip`, `purge`), diagnostics, repair issues with fix flows, and a "Notify on new dashcam event clip" blueprint.
- Push updates over a WebSocket with a 30 s poll as the fallback.
