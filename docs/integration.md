# Dashcam Sync integration

Companion integration for the **Dashcam Sync** app. The app pulls clips off a Wi-Fi dashcam into Home Assistant storage; this integration turns the result into devices, entities, a live camera and a clip library.

Requires Home Assistant 2026.11 or newer with the Supervisor, and the Dashcam Sync app (add its repository under **Settings › Apps › App store › Repositories**).

## Supported devices

The integration does not talk to cameras itself; it talks to the Dashcam Sync app, which supports these camera families (see the [supported camera list](https://github.com/adamstoffel/ha-dashcam-sync/blob/main/docs/supported-cameras.md) for quirks):

| Driver | Typical cameras |
| --- | --- |
| `novatek` | Novatek NT96xxx clones (FitcamX-style), default address 192.168.1.254 |
| `viidure_app` | Cameras using the Viidure `/app` API, default address 192.168.169.1 or 192.168.0.1 |
| `http_index` | Any camera that serves an HTML directory listing of video files |

BlackVue, AIT and Ambarella families are planned. If your camera is unsupported, the app can capture a redacted device profile that a contributor can turn into a driver.

## Set up

The app announces itself through Supervisor discovery: confirm **Dashcam Sync discovered** under **Settings › Devices & services**. If discovery is unavailable, add the integration manually with the app's internal host name (for example `local-dashcam-sync`), port `8100` and the API token.

## What it adds

One **hub** device for the app and one **device per camera**.

| Entity | Platform | Notes |
| --- | --- | --- |
| Connected | binary sensor (connectivity) | Camera reachable now |
| Sync status | sensor (enum) | idle, detecting, preparing, listing, downloading, restoring, error |
| Clips remaining / Data remaining | sensor | Queue length and bytes left |
| Download speed, SD card free | sensor (diagnostic) | Rolling average; from the camera |
| Last sync, Last event clip | sensor (timestamp) | |
| Clip | event | `event_clip`, `clip`, `sync_completed`, `sync_failed`, with `path`, `kind`, `channel`, `duration`, `media_content_id` |
| Latest event | image | Thumbnail of the newest event clip |
| Live | camera | RTSP stream opened by Core; unavailable out of range; optional |
| Sync now, Restore recording | button | Restore forces the camera back into recording |
| Auto sync | switch (config) | Pauses one camera without deleting it |
| Sync mode | select (config) | events_only, events_first, everything |

Hub entities: local and network storage used and free, **Network storage** problem sensor, **Purge now** button.

## Actions

| Action | What it does |
| --- | --- |
| `dashcam_sync.sync` | Start a session for the targeted camera. |
| `dashcam_sync.keep_clip` / `unkeep_clip` | Protect a clip from every expiration rule, by media content ID. |
| `dashcam_sync.delete_clip` | Delete a clip from all targets. |
| `dashcam_sync.purge` | Run retention now; `dry_run: true` returns what would be deleted. |

```yaml
# Keep every event clip
automation:
  - triggers:
      - trigger: state
        entity_id: event.polestar_2_clip
        attribute: event_type
        to: event_clip
    actions:
      - action: dashcam_sync.keep_clip
        data:
          media_content_id: "{{ trigger.to_state.attributes.media_content_id }}"
```

## Data updates

The integration is `local_push`. The app pushes every change over a WebSocket (`/api/v1/ws`): camera statuses as they change, new clips and sync results as they happen, and hub-level state (storage, issues) whenever it differs. If the socket drops, the integration reconnects with backoff and falls back to polling `/api/v1/status` every 30 seconds, and entities go unavailable if the app cannot be reached at all. After the integration changes something itself (the switch, the select, a button) it reads fresh state immediately.

## Media browser

**Media › Dashcam Sync › camera › day › Event or All**, newest first, with thumbnails. It reads the app's catalogue, so a NAS target browses as fast as local disk.

## Blueprint

[Notify on new dashcam event clip](../blueprints/automation/dashcam_sync/notify_event_clip.yaml) sends a mobile notification with the thumbnail and a link to the clip.

[![Import blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fadamstoffel%2Fha-dashcam-sync%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fdashcam_sync%2Fnotify_event_clip.yaml)

## Options

**Configure** offers:

- **Arrival hint and live view**: pick any entity (a car's device tracker, a garage door) as an arrival hint; when it changes to home or open, the app probes every 3 s for 5 minutes instead of every 15 s. Toggle the live camera entity.
- **Connect a camera's Wi-Fi**: Home Assistant joins the camera's hotspot through the Supervisor, with a small static address block, no gateway and no DNS. The flow runs the overlap check first and never connects when it fails. If Home Assistant cannot change the profile, it shows the exact values to type under **Settings › System › Network**.

## Repairs

No Wi-Fi adapter; address-block overlap (with a re-check fix); camera possibly left in playback mode (fix: restore); default route moved (fix: disconnect the Wi-Fi); network storage offline for over an hour; storage below the minimum free space (fix: purge); unsupported camera; app and integration API mismatch; Home Assistant announcing itself on the Wi-Fi adapter.

## Known limitations

- Hotspot transfers are slow and most mirror-powered cameras turn off with the car: event clips are the realistic goal.
- One hotspot camera per Wi-Fi adapter.
- The integration can only change Wi-Fi through the Supervisor's client, which Core does not document for custom integrations; the manual steps are the fallback.

## Removal

Remove the integration under **Settings › Devices & services**. The app keeps running and keeps its clips; remove the app separately if you no longer need it.

## Troubleshooting

Download diagnostics from the device page (SSIDs, passphrases, tokens and IP addresses are redacted) and read the app's **Logs** page. File bugs at <https://github.com/adamstoffel/ha-dashcam-sync/issues>.
