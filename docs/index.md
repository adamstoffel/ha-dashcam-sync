# Dashcam Sync documentation

- [Install walkthrough](#install-walkthrough)
- [Supported cameras](supported-cameras.md)
- [Capturing a device profile](device-profile.md)
- [Writing a driver](writing-a-driver.md)

## Install walkthrough

You need two My Home Assistant links: one to add the app repository, one to install the integration.

1. **Add the app repository.** Open <https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fadamstoffel%2Fha-dashcam-sync> and confirm, or add the URL under **Settings › Apps › App store › Repositories**.
2. **Install and start the app.** Open **Dashcam Sync** in the store, install it and start it. It appears in the sidebar.
3. **Install the integration.** Open <https://my.home-assistant.io/redirect/hacs_repository/?owner=adamstoffel&repository=dashcam-sync-integration&category=integration> (or search the Marketplace for *Dashcam Sync* once the catalog lists it) and restart Home Assistant.
4. **Confirm discovery.** Under **Settings › Devices & services**, accept **Dashcam Sync discovered**. If nothing is discovered, add the integration manually with host `local-dashcam-sync`, port `8100` and a token you choose: set **Integration token** (32+ characters) on the app's Configuration tab and restart the app. See the app's documentation.
5. **Add your camera.** Open **Dashcam Sync** from the sidebar › **Cameras › Add camera**. Choose *camera's own Wi-Fi* or *camera on my home network*, scan or type the camera's Wi-Fi name and password, press **Probe camera**, then **Save**.
6. **Join the hotspot (hotspot cameras).** Under **Settings › Devices & services › Dashcam Sync › Configure › Connect a camera's Wi-Fi**, pick the camera. The flow checks the reserved address block first, then writes the Wi-Fi profile. If Home Assistant cannot change it, the flow shows the exact values to type under **Settings › System › Network**.
7. **Drive.** When the car comes in range the app connects, syncs event clips first, and puts the camera back into recording. The clips appear under **Media › Dashcam Sync**.

### Wi-Fi adapter guidance

A hotspot camera needs a Wi-Fi adapter that Home Assistant OS has a driver for. If the repair issue *No Wi-Fi adapter* appears, the built-in card has no driver: use a supported USB adapter, or use a camera that can join your home network (station mode). Range to the parking spot matters more than anything else for transfer speed. Each hotspot camera needs its own adapter. If the adapter is also enabled under **Settings › System › Network › Network adapter**, turn that off so Home Assistant does not announce itself on the camera's network.

### Which clips do I get?

| Sync mode | Fetches |
| --- | --- |
| `events_only` | event and parking clips |
| `events_first` (default) | those, plus normal clips from the last `recent_minutes` (10) |
| `everything` | the whole card, in priority order |

### Storage

Clips go under `/media/dashcam_sync` by default. To use a NAS, a USB drive or an SD card, add it under **Settings › System › Storage › Add storage** (a network share, or *Local disk* with usage *Media*), then set the app's `storage.target` to `network` or `both` and `network_folder` to its name. Retention defaults: 14 days of normal clips locally (90 on the share), 30 days of parking clips, events kept until space is needed, 200 GB cap locally, 10 % minimum free space.
