# Changelog

## 2026.11.0

First public version.

- Drivers for Novatek, Viidure `/app` and generic HTTP index cameras.
- Resumable, verified downloads; the camera is always put back into recording.
- Local, network or both storage with rolling retention and a Keep flag.
- Small-block addressing with overlap checks for hotspot cameras.
- Ingress panel with setup wizard, clip browser, storage page and logs.
- Integration API on port 8100 with Supervisor discovery.
- Panel: refreshes until thumbnails exist; the address-block check is only offered for hotspot cameras.
- Fixes found by end-to-end and property-based testing: fast shutdown with a connected integration, pushed hub state, persisted last sync and last event clip, robust option parsing.
- Wizard hosts are validated (private addresses only); option edits are serialised.
