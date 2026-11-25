# Local API reference

The app serves a small JSON API for the integration (and for you, if you want to script it). Base URL: `http://<app host>:8100/api/v1`. The same routes are served to the ingress panel on port 8099, where Home Assistant has already authenticated you and the Supervisor's ingress proxy is the only client allowed.

- **Authentication.** Every route except `GET /healthz` needs `Authorization: Bearer <token>`. The token is generated on first start, stored in `/data`, and handed to Core only through Supervisor discovery, unless you set the app's optional **Integration token** yourself (at least 32 characters) to add the integration by hand. Credentials (`/cameras/<id>/wifi`) are never served on the ingress port.
- **Versioning.** `GET /info` returns `api_version` (currently `1`). The integration raises a repair issue if the two sides are incompatible.
- **Errors.** Failures are `{"error": "<message>"}` with a 4xx or 5xx status.
- **Hostile input.** The wizard endpoints that take a host refuse public, loopback and Home Assistant-internal addresses.

## Status and push

| Method and path | Purpose |
| --- | --- |
| `GET /healthz` | Liveness for the Supervisor watchdog. No token. |
| `GET /info` | `api_version`, `app_version`, `instance` (a stable random id that survives token changes), camera count. |
| `GET /status` | Everything at once: `cameras` (by id), `storage`, `issues`, `network`, `overlap`, and `rev` (see below). |
| `GET /ws` | WebSocket. Sends a `snapshot` (same shape as `/status`) on connect, then pushes events. Send the text `snapshot` to request another. |

Push messages (`{"type": ..., "ts": ..., "rev": ...}`):

**Ordering.** `rev` puts pushes and status reads in one order. A status answer or `snapshot` reflects every push whose `rev` is at most its own. Every sample of state gets its own `rev`, so two answers or pushes never share one. A client that reads the status while pushes are arriving keeps a pushed state only if the push's `rev` is higher than the answer's; otherwise the answer is at least as new.

| `type` | Payload | Meaning |
| --- | --- | --- |
| `camera` | `camera`, `status` | A camera's status changed (state, queue, speed, connected, mode). |
| `hub` | `storage`, `issues`, `network`, `overlap` | Hub-level state changed (pushed when it differs, at most every 30 s). |
| `clip` | `camera`, `clip`, `event_clip`, `thumbnail` | A clip finished downloading and post-processing. |
| `session` | `camera`, `result` (`sync_completed` or `sync_failed`), `clips`, `bytes` or `reason`, `error` | A sync session ended. |
| `safety` | `route_unsafe` (camera ids) | The default route moved onto a hotspot interface: disconnect the Wi-Fi. |
| `overlap` | `camera`, `report` | The address-block check result changed. |
| `migration` | the `job` fields of `GET /storage/migration` | Progress of a clip move, once per clip and at the end. |
| `options`, `purge_completed`, `issue`, `clip_keep`, `clip_deleted` | varies | Things the integration should re-read. |

## Cameras

| Method and path | Purpose |
| --- | --- |
| `GET /cameras` | Every camera's options (passwords masked on the ingress port) and status. |
| `GET /cameras/{id}` | One camera, plus its overlap report and the last 10 sessions. |
| `PATCH /cameras/{id}` | Change `enabled`, `sync_mode` or `name`; written to the app options and hot-reloaded. |
| `DELETE /cameras/{id}` | Remove a camera from the options. |
| `POST /cameras/{id}/sync` | Start a session now (`409` if the camera is not connected or is paused). |
| `POST /cameras/{id}/restore` | Force `restore()`: put the camera back into recording. |
| `GET /cameras/{id}/wifi` | Hotspot SSID, passphrase and the reserved address block. **Token API only.** |
| `GET /cameras/{id}/overlap` | The last address-block check (runs one without probing if there is none). Hotspot cameras only. |
| `POST /cameras/{id}/overlap` | Run the check now. Body: `{"known_device_ips": [...], "probe": true}`. |
| `POST /hint` | Arrival hint: poll every 3 s for 5 minutes. Body: `{"camera": "<id>"}` or `{}` for all. |

## Clips and storage

| Method and path | Purpose |
| --- | --- |
| `GET /clips` | List finished clips. Query: `camera`, `kind` (`normal`, `event`, `parking`, `photo`), `day` (`YYYY-MM-DD`, UTC), `limit` (max 1000), `offset`. Newest first. |
| `GET /clip-days?camera=<id>` | Days that have clips, newest first. |
| `GET /clips/{id}` | One clip with its copies (`local`, `network`, `spool`), `path` and `thumbnail`. |
| `GET /clips/{id}/file`, `GET /clips/{id}/thumb` | The file (Range supported) and its thumbnail. |
| `POST /clips/{id}/keep`, `POST /clips/{id}/unkeep` | Protect from, or release to, every expiration rule. |
| `DELETE /clips/{id}` | Delete from every target. |
| `GET /storage` | Health of each target (mounted, writable, free space, clip bytes) and the last purge. |
| `POST /purge` | Run retention. Body: `{"dry_run": true}` returns what would be deleted. |
| `GET /storage/location` | Current `target`, `local_folder` and `network_folder`, and the network shares and local disks Home Assistant has attached under `/media` or `/share` (`name`, `path`, `usage`, `kind`: `network` or `disk`, `fstype`). |
| `PATCH /storage/location` | Change any of `target`, `local_folder`, `network_folder` (written to the app options). Only new clips follow; the answer includes the plan for moving the existing ones. `409` while clips are being moved. |
| `GET /storage/migration` | `plan`: clips and bytes to move, copies to remove, destinations, and clips that are `unavailable` (files unreachable), `stranded` (on an unattached old disk) or still `processing`, plus `blocked` (why it cannot start). `job`: the running or last move. |
| `POST /storage/migration` | Start moving existing clips to where the current settings put them (`202`). `409` with `nothing_to_move`, `already_running` or the blocking reason. Progress is pushed as `migration` events. |
| `DELETE /storage/migration` | Stop after the clip being moved. An interrupted move can be started again; it continues where it stopped. |
| `GET /sessions?limit=50&camera=<id>` | Recent sync sessions (diagnostics). |

## Options, logs and the setup wizard

| Method and path | Purpose |
| --- | --- |
| `GET /options` | The app options (passwords masked on the ingress port). |
| `GET /logs?lines=300` | The app's recent log lines. |
| `GET /wizard/wifi-scan?interface=wlan0` | Wi-Fi scan; `available: false` when the default role may not scan (type the SSID instead). |
| `POST /wizard/probe` | Detect the driver at `{"host": ..., "driver": "auto"}`. |
| `POST /wizard/block` | Compute the reserved block for `{"camera_ip": ...}`. |
| `GET /wizard/storage-test` | Mount health of the configured targets. |
| `POST /wizard/camera` | Validate and save a camera entry (computes `static_address` for hotspot cameras). |
| `POST /wizard/capture-profile` | A redacted, read-only profile of an unsupported camera. |
| `POST /wizard/station` | Novatek: send home Wi-Fi credentials and switch the camera to station mode. |

## Example

```bash
TOKEN=...   # from the app's log on first start, or discovery
curl -s -H "Authorization: Bearer $TOKEN" http://local-dashcam-sync:8100/api/v1/status | jq '.cameras[] | {name, connected, sync_status, clips_remaining}'
curl -s -H "Authorization: Bearer $TOKEN" -X POST http://local-dashcam-sync:8100/api/v1/purge -d '{"dry_run": true}'
```
