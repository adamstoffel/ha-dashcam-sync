# Changelog

All notable changes to the integration and the app. Both are released together with the same CalVer version (`YYYY.M.patch`).

## Unreleased

- App: change where clips are stored from the panel (Storage › Location: local folder, a network share or attached disk, or both), and **Move clips** to bring existing clips to the new place. Each copy is size-checked before the old one is deleted; the move is resumable, cancellable, refuses to start when the destination is missing, and takes turns with shipping, retention and deletes. API: `GET/PATCH /storage/location`, `GET/POST/DELETE /storage/migration`, and `migration` push events.
- App: validation errors from the API now name the field and the reason instead of "1 validation error".
- App: a local disk added in Home Assistant (**Settings › System › Storage › Add storage › Local disk**: a USB drive, SD card or internal drive) can now be the second storage target, not only a network share. The same safety rule applies: it must be mounted at exactly that folder, so a missing disk never fills the system disk.
- Integration: fixed the integration failing to load on Home Assistant 2026.11 (`cannot import name 'async_extract_referenced_entity_ids' from 'homeassistant.helpers.service'`). The helper now comes from `homeassistant.helpers.target`, which exists in both older and newer releases.
- CI: a new check that every Home Assistant name the integration imports exists in the minimum supported version (`scripts/check_ha_api.py`), and a nightly run against the newest release. The tests only ever ran on an older Home Assistant than the one the integration claims to support, which is how the above got through.
- App: an optional **Integration token** option, so the integration can be added by hand when discovery does not appear (the previous instructions pointed at a token that was never logged). The token is never logged; the log now says when discovery was offered.
- Tests: two timing assumptions removed (the WebSocket snapshot reply could be preceded by another push; the Novatek heartbeat test assumed a fixed delay).

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
  - a status read answered just before a push could land after it and undo it, so a finished sync could show as never having run until the next change (pushes and status reads now carry a `rev` order number, and a pushed state replaces a status answer only if it is newer);
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
- Integration fixes from review and end-to-end testing, each with a regression test:
  - switch, select and button changes could be delayed up to 10 s by the coordinator's refresh debounce (writes now refresh immediately);
  - events pushed by the app were answered from a stale cache instead of refreshing;
  - one malformed push message (or a malformed snapshot) could end live updates or poison the state: messages and snapshots are shape-checked;
  - the app's instance id is no longer derived from the API token, so rotating the token no longer breaks *Reconfigure* or creates a duplicate entry;
  - non-JSON or wrongly shaped API responses now fail cleanly (setup retries instead of failing);
  - the arrival-hint entity can be cleared again; the Wi-Fi kill switch carries on after one camera fails; the hub device cannot be deleted from the UI; network-storage entities appear when a share is added after setup; image paths are resolved off the event loop.
- Hardening: the wizard refuses public, loopback and Home Assistant-internal hosts; option edits are serialised.
- Push updates over a WebSocket with a 30 s poll as the fallback.
