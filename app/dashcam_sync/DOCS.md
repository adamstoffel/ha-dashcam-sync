# Dashcam Sync

Dashcam Sync pulls video clips off a Wi-Fi dashcam into Home Assistant whenever the car is in range, with no phone app. This app does all the camera work. The companion **Dashcam Sync integration** turns the result into devices, entities, a live camera and a clip library in the media browser.

## Honest expectations

- Hotspot transfers are slow, often under 1 MB/s. Event clips are the realistic goal; a full drive of footage rarely finishes.
- Most cameras powered from a mirror or cigarette lighter turn off with the car. They are only in range while the car is on, or in range of the Wi-Fi adapter after parking.
- Recordings can show other people and licence plates. Everything stays on your hardware: no cloud, no telemetry.

## Requirements

- Home Assistant OS or Supervised with the Supervisor (Container and Core installs cannot run apps).
- Home Assistant 2026.11 or newer.
- For a camera that creates its own Wi-Fi (hotspot): a Wi-Fi adapter that Home Assistant OS has a driver for (a supported USB adapter is the usual fix when the built-in card is not enough) with good range to where the car parks.
- For a camera that joins your home Wi-Fi (station mode): nothing extra; give the camera a DHCP reservation.

## Install

1. Add this app repository under **Settings › Apps › App store › Repositories**.
2. Install **Dashcam Sync**, start it, and open it from the sidebar.
3. Install the **Dashcam Sync integration** from the Marketplace (or its My link) and confirm the discovered "Dashcam Sync" device.
4. In the panel, open **Cameras › Add camera** and follow the setup wizard.

## Supported cameras

| Driver | Typical cameras | Status |
| --- | --- | --- |
| `novatek` | Novatek NT96xxx clones (FitcamX-style), camera at 192.168.1.254 | stable target for 1.0 |
| `viidure_app` | Cameras using the Viidure `/app` API, camera at 192.168.169.1 | stable target for 1.0 |
| `http_index` | Any camera that serves an HTML directory listing of video files | stable target for 1.0 |

BlackVue, AIT and Ambarella families are planned for 1.1 and 1.2. Unsupported camera? Press **Capture device profile** in the Cameras page and attach the result to a GitHub issue.

## How a sync works

1. The app watches for the camera (every 15 s, or every 3 s for 5 minutes after an arrival hint such as a car tracker coming home).
2. It detects the camera family, puts the camera in playback mode (this stops recording) and lists the files.
3. New clips are queued: event clips first, then parking clips, then normal clips from the last `recent_minutes`, then the rest, then photos. Front and rear files with the same start time are queued together.
4. Each file is downloaded to a `.partial` file and resumed with an HTTP Range request if the link drops. The size is verified against the listing and `Content-Length`, a SHA-256 is stored, and the file is moved into place atomically.
5. **The camera is always put back into recording**, even after errors, a crash or a power loss: a flag is written to disk before playback mode is entered, and the next contact restores first.
6. In the background, `.ts` files are remuxed to `.mp4` without re-encoding, a 320 px thumbnail is made, and a `.json` sidecar is written.

## Configuration

Everything is stored as the app's options and can be edited in three places: this **Configuration** tab, the setup wizard in the panel, and (for things that live in Core) the integration's options flow. The daemon hot-reloads the options; only changing `log_level` restarts the app.

### Cameras

| Option | Meaning |
| --- | --- |
| `id` | Stable key for folders, devices and entity IDs (`^[a-z0-9_]{1,32}$`). |
| `name` | Display name. |
| `connection` | `hotspot` (the camera's own Wi-Fi) or `lan` (camera joined to your network). |
| `interface` | Host Wi-Fi adapter for hotspot cameras, for example `wlan0`. |
| `ssid`, `wifi_password` | The camera hotspot. The password is masked and only shared with the integration. |
| `ipv4` | `static` (default) claims a small block around the camera; `auto` only when the camera's range does not overlap your network. |
| `static_address` | Home Assistant's address with prefix on the camera network, for example `192.168.1.253/30`. Computed by the wizard. |
| `host` | Camera IP or hostname; empty means auto-detect. |
| `driver` | Force a driver, or `auto`. |
| `enabled` | Pause one camera without deleting it. |
| `sync_mode` | Override the default sync mode for this camera. |

A rule failure (for example a hotspot camera without an `ssid`, or two hotspot cameras on one adapter) disables that camera, logs why and raises a repair issue in Home Assistant. It never stops the whole app.

### Storage

| Option | Meaning |
| --- | --- |
| `target` | `local` (/media), `network` (a share) or `both` (local first, then a copy to the share). |
| `local_folder` | Folder created under /media (default `dashcam_sync`). |
| `network_folder` | Name of a network storage you added under **Settings › System › Storage › Add network storage**. A share with usage *Media* appears under `/media/<name>`, one with usage *Share* under `/share/<name>`. |
| `spool_when_network_down` | Keep new clips locally (`.spool`) and ship them when the share returns. |
| `keep_original` | Keep the camera's `.ts` next to the remuxed `.mp4`. |

The app never mounts shares itself. Before every write it requires a live `cifs` or `nfs` mount at exactly that path, because the Supervisor can leave an empty folder behind when a share is offline and writing there silently fills the local disk. It then writes and deletes a probe file and refuses to write if free space would drop below `min_free_percent`. A network target that stays offline for an hour raises a repair issue.

Clips are never part of the app's backup (only the small catalogue in `/data` is). Back clips up through the NAS.

Folder layout, identical on every target:

```
/media/dashcam_sync/
  polestar_2/
    2026/11/22/
      event/   20261122_101600_front.mp4  .jpg  .json
      normal/  20261122_101500_front.mp4  20261122_101500_rear.mp4 ...
  .partial/    in-progress downloads (hidden from the media browser)
  .spool/      files waiting for an offline network target
```

### Retention

Retention runs daily at `purge_time` and after each sync, for each target on its own rules.

| Option | Local default | Network default | Effect |
| --- | --- | --- | --- |
| `normal_days` | 14 | 90 | Delete normal clips older than this. |
| `parking_days` | 30 | 30 | Delete parking clips older than this. |
| `event_days` | 0 (keep) | 0 (keep) | `0` means age never expires events. |
| `photo_days` | 90 | 90 | Delete photos older than this. |
| `max_total_gb` | 200 | none (0) | Oldest-first delete until under the cap. |
| `min_free_percent` | 10 | 5 | Oldest-first delete until free space recovers. |

The network rules are the same option names prefixed with `network_`. Size-based deletion removes normal, then parking, then photo clips, and event clips only if still needed, oldest first in each. A clip you mark **Keep** (the Clips page, or the `dashcam_sync.keep_clip` action) is never deleted by any rule. Use **Dry run** on the Storage page to see what the next purge would remove.

### Sync

| Option | Meaning |
| --- | --- |
| `mode` | `events_only` fetches event and parking clips; `events_first` adds normal clips from the last `recent_minutes`; `everything` drains the whole queue. |
| `recent_minutes` | The window of normal clips always fetched (measured against the camera's newest clip, so a wrong camera clock does not matter). |
| `delete_from_camera` | Off by default. When on, only verified, unlocked files are deleted. |
| `delete_events_from_camera` | Event clips are only deleted from the camera when this is also on. |
| `remux_to_mp4` | Lossless `.ts` to `.mp4` remux. |
| `thumbnails` | Create thumbnails. |

## Wi-Fi: how the connection works

The app never edits the host network. **Home Assistant Core**, through the integration, joins the camera's hotspot on the Wi-Fi adapter (the integration's options flow, one step per camera), and the app only reads network state. You can instead set the Wi-Fi profile yourself under **Settings › System › Network**; the options flow shows the exact values.

The profile uses mode infrastructure with the camera's SSID and a WPA passphrase, a static IPv4 address in a small block around the camera with **no gateway and no DNS servers**, and IPv6 disabled. Your default route and name resolution stay on Ethernet, and only those four addresses are routed to the car. NetworkManager rejoins the hotspot every time the car comes in range.

If the Wi-Fi adapter is also enabled under **Settings › System › Network › Network adapter**, a repair suggests turning it off so Home Assistant does not announce itself on the camera's network.

After joining, the app confirms the default route is still on the wired interface and that your router still answers. If either check fails, the integration disconnects the Wi-Fi profile within seconds and raises a repair issue.

### Overlapping addresses

**What the issue is.** Your dashcam runs its own small Wi-Fi network and gives itself an address such as `192.168.1.254`. Many home networks use the same range. While Home Assistant is connected to the dashcam, it would send traffic for those addresses to the car instead of your home network. Dashcam Sync avoids this by claiming only a block of four addresses around the dashcam. Only those four addresses are affected, and only for Home Assistant.

**How to check it.**

1. Open the Dashcam Sync panel › Cameras › your camera › **Network check**. It shows the dashcam's address, the reserved block (for example `192.168.1.252` to `192.168.1.255`) and your home network's range.
2. Read the result:
    - *No overlap* or *Overlap, block free*: nothing to fix. Go to step 3 anyway.
    - *Address in use*: a device on your home network uses one of the four addresses. The panel names it if Home Assistant knows it.
    - *Camera address collides*: something on your home network has exactly the dashcam's address.
3. Open your router's client list (in UniFi: Client Devices, filter by IP) and confirm nothing uses the four addresses, including devices that are switched off right now.

**How to fix it, if needed.**

1. **Keep the block free for the future** (recommended for everyone with an overlap). In your router, set the DHCP range so it does not include the four addresses. In UniFi: Settings › Networks › your network › DHCP range. Example: if the block is `192.168.1.252` to `192.168.1.255`, end the range at `192.168.1.251` or move it elsewhere.
2. **Address in use.** Give that device a different address: change its fixed IP or its DHCP reservation in the router, then restart it. Re-run the check from the panel.
3. **Camera address collides.** Home Assistant cannot fix this by itself, because two devices claim the same address. Options, easiest first:
    1. Change the dashcam's own address, if its phone app or settings allow it.
    2. Change the home device that uses that address, unless it is your router.
    3. Put a small travel router between the dashcam and your network in repeater mode, and add the camera in Dashcam Sync as a LAN camera at the travel router's address.
4. After any fix, press **Network check** again. The camera stays paused until the check passes.

Only Home Assistant itself loses the block's addresses while connected; every other device on your network is unaffected, because the route change exists only on the Home Assistant host. The router's DHCP pool cannot be detected from Home Assistant, so step 1 is a reminder, not a check.

### LAN (station-mode) cameras

Give the camera's IP in the wizard and add a DHCP reservation in the router. For Novatek firmware that supports it, the wizard offers **Move camera onto my Wi-Fi**: it sends your SSID and passphrase, switches the camera to station mode and waits up to 2 minutes for it at its reserved address. Some firmware falls back to its hotspot after a power cycle; the app detects that and raises a repair issue.

### More than one camera

Each hotspot camera is bound to its own wireless interface: a second USB adapter supports a second hotspot camera. LAN cameras are unlimited and sync one at a time.

## Security

- No host network, no extra capabilities, the default Supervisor API role, ingress for the UI and a custom AppArmor profile: security rating 8.
- The panel is served only to the Supervisor's ingress proxy; the integration API needs a token that is only handed to Core through discovery.
- Every camera response is treated as hostile: bodies are capped at 4 MB, XML is parsed with defusedxml, every request has a timeout, and camera-supplied paths are never used as local file names.
- The setup wizard only probes private or link-local addresses: public addresses, loopback and Home Assistant's own network are refused.
- ffmpeg runs with only the `file` protocol, a 120 s timeout and CPU and memory limits.

## Troubleshooting

| Symptom | What to try |
| --- | --- |
| "No Wi-Fi adapter" repair | Home Assistant OS has no driver for the card. Use a supported USB adapter, or use a LAN camera. |
| Camera never connects | Check the Wi-Fi profile under Settings › System › Network, the signal at the parking spot, and the Network check. |
| "Subnet overlap" repair | Follow *Overlapping addresses* above. |
| Camera stuck in playback mode | Press **Restore recording** (button on the device or in the panel). The app retries on every contact. |
| Clips are not appearing | Check the **Logs** page, the network target status and free space on the **Storage** page. |
| "Unsupported camera" | Capture a device profile and attach it to a GitHub issue. |
| Share offline | Re-add or repair the network storage under Settings › System › Storage. New clips are spooled meanwhile. |
