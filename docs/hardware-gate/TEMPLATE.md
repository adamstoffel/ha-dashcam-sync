# Hardware gate for <version>

Copy this file to `docs/hardware-gate/<version>.md`, run the checklist on a real camera for each row and fill in `pass`, `fail` or `n/a`. Paste the table into the release PR. A failed row blocks the release (`scripts/check_hardware_gate.py` enforces this in the release workflow).

Checklist per camera: **detect** (family recognised), **list** (files listed), **resume** (a download interrupted by pulling the camera's power resumes on the next visit without being downloaded twice), **verify** (size and SHA-256 recorded), **remux** (`.ts` to `.mp4`, if the camera produces `.ts`), **restore** (camera is recording again after the session, after an error and after a killed app), **purge** (retention deletes the expected clips).

At least one camera per stable driver, on both an amd64 and an aarch64 host.

| camera | driver | arch | detect | list | resume | verify | remux | restore | purge |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| (model) | novatek | amd64 | | | | | | | |
| (model) | novatek | aarch64 | | | | | | | |
| (model) | viidure_app | amd64 | | | | | | | |
| (model) | viidure_app | aarch64 | | | | | | | |
| (model) | http_index | amd64 | | | | | | | |
| (model) | http_index | aarch64 | | | | | | | |
