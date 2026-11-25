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
- Found by an independent code review, each fixed with a regression test that fails without the fix:
  - fresh clips from a camera with a reset clock were purged straight after download (age is now counted from download time at the earliest);
  - a deleted or purged clip was downloaded again at the next visit (deletions now leave a tombstone);
  - an unexpected error was recorded as a successful sync and could end a camera's worker for good (the worker now survives any tick, and unexpected errors are failed sessions);
  - *Restore recording* and *Sync now* were ignored once the retry backoff was exhausted;
  - clips still being remuxed could be copied to the NAS as raw `.ts` without thumbnail or sidecar;
  - a failing callback or a renamed `local_folder` could kill shipping and post-processing until restart; interrupted post-processing now resumes on start;
  - the host check could be bypassed with userinfo (`ip:x@internal-host`) or IPv4-mapped IPv6 addresses;
  - `PATCH /cameras/{id}` accepted invalid values that made the camera vanish on reload;
  - mount probes ran on the event loop, so a hung share could freeze the API and every download;
  - the panel's camera selector sent you to the Overview.
- Hardening: the wizard refuses public, loopback and Home Assistant-internal hosts; option edits are serialised.
- Push updates over a WebSocket with a 30 s poll as the fallback.
