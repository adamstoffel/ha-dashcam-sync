# Troubleshooting

Start with the diagnostics download (**Settings › Devices & services › Dashcam Sync › ⋮ › Download diagnostics**; SSIDs, passphrases, tokens and IP addresses are redacted) and the app's **Logs** page.

| Symptom | Likely cause and fix |
| --- | --- |
| "Dashcam Sync discovered" never appears | The integration is not installed yet, or the app is stopped. Install the integration and restart Home Assistant. Otherwise add it manually: set **Integration token** (32+ characters) on the app's Configuration tab, restart the app, then add the integration with host `local-dashcam-sync`, port `8100` and that token. The app's log says `Offered to Home Assistant through Supervisor discovery` when it announced itself. |
| Repair: **No Wi-Fi adapter** | Home Assistant OS has no driver for the card. Use a supported USB adapter, or a camera that can join your home network (LAN mode). |
| The wizard says the camera refused the Wi-Fi password, but it is right | The host log shows `4-Way Handshake failed … WRONG_KEY`, yet a phone connects with the same password. A camera drops a client whose handshake is slow, and a weak signal at Home Assistant's adapter makes it slow (lost frames are resent), so it looks exactly like a wrong password. The integration tries three times; if all fail, bring the car closer or use a USB Wi-Fi adapter with an antenna. Many dashcams also take one client at a time: disconnect your phone and the camera's app. Joining by hand under **Settings › System › Network** retries too, and once joined, the app works the same. |
| The camera never connects | Check the Wi-Fi profile (**Settings › System › Network**), the signal at the parking spot, and the address-block check. A hotspot camera needs its own adapter. |
| Repair: **address overlap** | Something on your network uses an address in the camera's four-address block. Follow *Overlapping addresses* in the app's documentation, then press **Re-check**. The camera stays paused until the check passes. |
| Repair: **Wi-Fi disconnected: default route moved** | After joining, the default route left the wired interface. The integration already dropped the profile. Make sure the Wi-Fi profile has no gateway and no DNS (join from the camera wizard or **Configure › Re-join a camera's Wi-Fi**, not a hand-made profile). |
| Repair: **camera may be stuck in playback mode** | A session was interrupted before the camera was restored. Press the fix, the **Restore recording** button, or just wait: the app retries on every contact. |
| Sync is very slow | Hotspot Wi-Fi is slow (often under 1 MB/s). Improve range, and rely on event clips. The *Download speed* sensor is disabled by default; enable it to watch the rate. |
| Clips are not appearing | Check the **Storage** page: a network share or added disk that is not mounted is refused (the Supervisor leaves an empty folder when one is offline). New clips are spooled locally and shipped when it returns. |
| Repair: **unsupported camera** | No driver recognised it. [Capture a device profile](device-profile.md) and open an issue. |
| The panel says the camera is "away" but it is parked next to the house | The app checks reachability on the camera's ports; confirm the Wi-Fi profile is connected and that nothing blocks the camera's address. |
| Changing `log_level` restarts the app | Intended: it is the only option that does. Everything else is hot-reloaded. |

## Getting help

- Questions and ideas: [Discussions](https://github.com/adamstoffel/ha-dashcam-sync/discussions).
- Bugs: [open an issue](https://github.com/adamstoffel/ha-dashcam-sync/issues/new/choose) with the diagnostics file attached.
- Security problems: see [SECURITY.md](../SECURITY.md); please do not use public issues.
