# Capturing a device profile

When no driver recognises your camera, the app shows it as **Unsupported**. A device profile lets a contributor write the driver without owning your camera.

1. Join the camera's Wi-Fi (or put it on your network) so the app can reach it.
2. In the Dashcam Sync panel open **Cameras**, find the camera (or add it with its address) and press **Capture device profile**.
3. Copy the JSON, or save it, and attach it to an [Unsupported camera issue](https://github.com/adamstoffel/ha-dashcam-sync/issues/new?template=unsupported_camera.yml).

## What is captured

A read-only session: a TCP port scan, **GET** requests to a fixed list of probe URLs (see `PROBE_URLS` in `app/dashcam_sync/src/dashcam_sync/profile.py`), the first 64 KB of the root page, and response headers. Nothing is sent to the camera except those GETs.

## What is removed

Before the profile is shown, the app redacts SSIDs, passwords and passphrases (in JSON, XML and query strings), MAC addresses, and every IPv4 address except the camera's own. `Set-Cookie` headers are dropped. Read the JSON before you post it, and remove anything else you do not want public.

## What happens next

A contributor saves the bundle as `app/tests/fixtures/<family>.json`. CI replays every fixture against every driver, so a new driver must claim its own fixture and must not claim anyone else's. A driver ships as **experimental** until two users confirm it on real hardware.
