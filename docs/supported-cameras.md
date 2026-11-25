# Supported cameras

Confidence levels: **documented** means the driver follows published community protocol notes and passes the fixture tests; **hardware-verified** means it also passed the hardware gate on a real camera. Nothing is hardware-verified yet (see the README).

| Driver | Detected by | Typical default address | Listing and download | Confidence | Release |
| --- | --- | --- | --- | --- | --- |
| `novatek` | `/?custom=1&cmd=3016` heartbeat answers XML | 192.168.1.254 | `cmd=3015` after `cmd=3001&par=2` (playback mode); files over plain HTTP | documented | 1.0 |
| `viidure_app` | JSON from `/app/getproductinfo` | 192.168.169.1 | `/app/getfilelist?folder=loop&start=0&end=99`, then HTTP download of the returned `.ts` paths | documented | 1.0 |
| `http_index` | Any HTML directory listing with video links | any | Crawl at most 40 folders, download by URL | documented | 1.0 |
| `blackvue` | `/Config/version.bin` | 10.99.77.1 | Vendor list endpoint + HTTP | planned | 1.1 |
| `ait` | `/cgi-bin/Config.cgi` | 192.72.1.1 | `Config.cgi` file list | planned | 1.1 |
| `ambarella` | Port 7878 JSON session | | Session-based JSON commands | planned | 1.2 |

## Quirks worth knowing

- **Novatek** cameras only list files in playback mode, which stops recording. The app writes a restore flag first and always puts the camera back into recording. They accept one HTTP request at a time and drop idle Wi-Fi unless a heartbeat (`cmd=3016`) arrives every 10 s during long downloads.
- A camera that answers `200` instead of `206` to a Range request is restarted from the beginning; the app remembers this per camera.
- Some firmware falls back to its hotspot after a power cycle even after "Move camera onto my Wi-Fi"; the app raises a repair issue.

## Unsupported camera?

[Capture a device profile](device-profile.md) and open an *Unsupported camera* issue.
