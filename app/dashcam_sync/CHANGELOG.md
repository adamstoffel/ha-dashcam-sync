# Changelog

## 2026.11.0b2

- A wrong Wi-Fi password is reported as such ("Home Assistant sees the camera's Wi-Fi but could not connect to it"), and an out-of-range camera too, instead of waiting for the camera forever.
- A warning on every page when the integration is missing or a different release, and the wizard waits for it before joining a camera's Wi-Fi.

## 2026.11.0b1

First public beta.

- Tested on a real camera: SGK GK720X (Viidure `/app` API) on Home Assistant OS, including the Wi-Fi join, downloads, remux and playback.
- Point-to-point addressing when the camera's range sits inside your home network and an address next to it is taken.
- The Wi-Fi join works with password-protected hotspots (the Supervisor's `auth` key).
- Downloads are verified against the camera's own headers when its file list gives no reliable sizes; crash recovery covers every step.
- A step-by-step camera wizard with Wi-Fi scan, a Settings page, and a panel that follows Home Assistant's look.

Initial build:

- Drivers for Novatek, Viidure `/app` and generic HTTP index cameras.
- Resumable, verified downloads; the camera is always put back into recording.
- Local, network or both storage with rolling retention and a Keep flag.
- Small-block addressing with overlap checks for hotspot cameras.
- Ingress panel with setup wizard, clip browser, storage page and logs.
- Integration API on port 8100 with Supervisor discovery.
- Panel: refreshes until thumbnails exist; the address-block check is only offered for hotspot cameras.
- Fixes found by end-to-end and property-based testing: fast shutdown with a connected integration, pushed hub state, persisted last sync and last event clip, robust option parsing.
- Wizard hosts are validated (private addresses only); option edits are serialised.
- Fixes from an independent code review: clock-reset cameras no longer lose fresh clips, deleted clips stay deleted, the camera worker survives any error, explicit restore and sync work after the backoff, raw clips are never shipped before post-processing, tighter host validation, validated camera edits, and blocking storage probes moved off the event loop.
