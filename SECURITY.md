# Security policy

## Reporting a vulnerability

Please report security problems **privately**, not in a public issue.

- Use GitHub's private vulnerability reporting: **Security › Report a vulnerability** on this repository.
- Or email the maintainer at the address on the maintainer's GitHub profile.

Include the app and integration versions, what you saw, and (if you can) how to reproduce it. Please do not attach recordings, SSIDs, passwords or tokens.

## What to expect

- An acknowledgement within 7 days.
- A fix or a mitigation plan within 90 days. We follow a **90-day coordinated disclosure window**: details are published after a fix ships, or after 90 days, whichever comes first.
- Credit in the changelog if you want it.

## Scope

In scope: the Dashcam Sync app (the container, its API on port 8100, its ingress panel, its AppArmor profile) and the Dashcam Sync integration.

Hardening already in place, and worth testing against:

- The integration API needs a bearer token that is only handed to Core through Supervisor discovery; the ingress panel only accepts the Supervisor's ingress proxy.
- Camera responses are untrusted: bodies capped at 4 MB, XML parsed with defusedxml, every request timed out, camera paths never used as local file names.
- ffmpeg runs with only the `file` protocol, a 120 s timeout and CPU and memory limits.
- The app has no telemetry and never sends anything off the host.
