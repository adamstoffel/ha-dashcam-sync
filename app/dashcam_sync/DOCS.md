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
4. In the panel, open **Cameras › Add camera**. The wizard asks four things:
    1. **Connection:** *Camera hotspot* (the camera runs its own Wi-Fi) or *Local network* (the camera is a client on your LAN).
    2. **Details:** for a hotspot, the Wi-Fi interface (listed from Home Assistant, e.g. `wlp4s0`), the camera's SSID and password; for the local network, its IP (with **Test** to detect it).
    3. **Name**, with an *Advanced* section for the driver, the camera IP, the IPv4 mode and Home Assistant's static address.
    4. **Connect:** for a hotspot, Home Assistant is asked to join the camera's Wi-Fi (after the address check). If it cannot, the wizard shows the exact profile to enter under **Settings › System › Network**, with copy buttons, and carries on once the camera answers.

    **Edit** on a camera opens the same steps; leave the password empty to keep the stored one.

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
4. Each file is downloaded to a hidden `.partial` file and only gets its real name, and appears anywhere, once it is complete:
    - **Interrupted links resume.** A dropped connection, a Wi-Fi fade (no data for 20 s) or a body that ends early keeps the partial file; the next contact continues with an HTTP Range request from the last byte saved to disk. Nothing is fetched twice.
    - **Resumes are checked.** A partial answer must start exactly where asked (`Content-Range`); a camera that restarts from byte 0 restarts the file, and one that answers from anywhere else is refused rather than spliced.
    - **Completeness is checked.** The size must match every size the camera states: the listing, `Content-Length` and the `Content-Range` total. If a camera states no size at all, the app asks for the bytes after the end and only accepts the file when the camera confirms there are none; otherwise the clip is marked failed, never kept as complete.
    - A SHA-256 is stored, and the file is moved into place atomically. A crash between moving it and recording it is picked up on the next visit without downloading again.
5. **The camera is always put back into recording**, even after errors, a crash or a power loss: a flag is written to disk before playback mode is entered, and the next contact restores first.
6. In the background, `.ts` files are remuxed to `.mp4` without re-encoding, a 320 px thumbnail is made, and a `.json` sidecar is written.

## Configuration

Everything is stored as the app's options. Edit them in the **Dashcam Sync panel**: **Cameras** (the setup wizard and Edit) and **Settings** (sync, storage, retention). This **Configuration** tab shows the same options as YAML, for anyone who prefers it; the tables below list every key. Two things live in Home Assistant instead, under the integration's **Configure**: the arrival-hint entity and the live camera entity. Changes apply without a restart: from the panel at once, from this tab within about 15 seconds. Only changing `log_level` restarts the app.

### Integration token

Normally you do nothing: the app generates a token and hands it to Home Assistant through Supervisor discovery. If **Dashcam Sync discovered** never appears, you can add the integration by hand. Set **Integration token (optional)** on this tab to a string of at least 32 characters (a password manager's generated one is fine), restart the app, then add the integration with host `local-dashcam-sync`, port `8100` and that token. The log says `Using the API token from the app configuration` when it was picked up, and never prints the token itself. Clear the field and restart to go back to the generated token.

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
| `target` | `local` (/media), `network` (a network share or an added disk) or `both` (local first, then a copy there). |
| `local_folder` | Folder created under /media (default `dashcam_sync`). |
| `network_folder` | Name of the storage you added under **Settings › System › Storage › Add storage**: a network share, or a **Local disk** such as a USB drive or SD card. Storage with usage *Media* appears under `/media/<name>`, with usage *Share* under `/share/<name>`. |
| `spool_when_network_down` | Keep new clips locally (`.spool`) and ship them when the share returns. |
| `keep_original` | Keep the camera's `.ts` next to the remuxed `.mp4`. |

The app never mounts anything itself. Before every write it requires a live mount at exactly that path: a network share (`cifs`, `nfs`) or a local disk Home Assistant supports (ext2/3/4, FAT, exFAT, NTFS, Btrfs, F2FS). When a share or disk is missing, the Supervisor can leave an empty folder behind, and writing there would silently fill the system disk. It then writes and deletes a probe file and refuses to write if free space would drop below `min_free_percent`. A network target that stays offline for an hour raises a repair issue.

Clips are never part of the app's backup (only the small catalogue in `/data` is). Back clips up through the NAS.

Folder layout, identical on every target:

```
/media/dashcam_sync/
  polestar_2/
    2026/11/22/
      event/   20261122_101600_front.mp4  .jpg  .json
      normal/  20261122_101500_front.mp4  20261122_101500_rear.mp4 ...
  .partial/    in-progress downloads (hidden from the media browser)
  .spool/      files waiting for an offline disk or share
```

#### Using an extra disk or SD card

A second disk in the Home Assistant machine (USB drive, SD card in a built-in reader, internal drive) can hold the clips:

1. Format it on a computer first. exFAT or ext4 are good choices. Home Assistant does not format disks.
2. Add it under **Settings › System › Storage › Add storage**, with **Type** *Local disk* and **Usage** *Media*. Give it a short name, for example `dashcam_disk`.
3. In the panel's **Settings › Storage**, set **Keep clips on** to *A disk or share* (clips only there) or *Both* (a local copy as well), and pick the disk. In YAML: `storage.target` `network` or `both`, and `network_folder` set to its name.

If the disk is unplugged, new clips wait in the local spool and move over when it is back. For an SD card, use a "high endurance" card: dashcam footage is a lot of continuous writing.

#### Changing where clips are stored

Open the panel's **Storage** page. Under **Location**, choose where new clips go (this Home Assistant, a network share or disk, or both), the local folder, and which attached share or disk to use, then **Save location**. You can also edit `storage` on this Configuration tab.

A change only affects new clips. If existing clips are now in the wrong place, the Storage page says how many and offers **Move clips**:

- Each clip is copied to its new place (with its thumbnail and metadata), its size is checked, and only then is the old copy deleted. On the same disk the files are simply renamed.
- Clips end up wherever the settings put them: switching to a disk moves them off the system disk, switching to *both* adds the second copy, switching back to local brings them home.
- The move runs one clip at a time. **Cancel**, a restart or a full disk stops it after the clip in hand; press **Move clips** again to continue where it stopped. Nothing is copied twice.
- It does not start while the new location is not attached or not writable, and the location cannot be changed while a move is running.
- Clips whose files cannot be reached are left alone, as are copies on an old disk that is no longer attached. The page lists both.

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

The network rules are the same option names prefixed with `network_`. Size-based deletion removes normal, then parking, then photo clips, and event clips only if still needed, oldest first in each. A clip's age is counted from its start time but never from earlier than when it was downloaded, so a camera whose clock was reset (many cameras without GPS fall back to 1970 or 2014 after a power loss) cannot make fresh clips expire. Deleted and purged clips are remembered, so the app does not download them again while they are still on the camera's card. A clip you mark **Keep** (the Clips page, or the `dashcam_sync.keep_clip` action) is never deleted by any rule. Use **Dry run** on the Storage page to see what the next purge would remove.

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

The app never edits the host network. **Home Assistant Core**, through the integration, joins the camera's hotspot on the Wi-Fi adapter (asked from the camera wizard's **Connect** step, or again with **Configure › Re-join a camera's Wi-Fi** on the integration), and the app only reads network state. You can instead set the Wi-Fi profile yourself under **Settings › System › Network**; the wizard shows the exact values with copy buttons.

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
| Clips are not appearing | Check the **Logs** page, the disk or share status and free space on the **Storage** page. |
| "Unsupported camera" | Capture a device profile and attach it to a GitHub issue. |
| Share offline | Re-add or repair the network storage under Settings › System › Storage. New clips are spooled meanwhile. |
