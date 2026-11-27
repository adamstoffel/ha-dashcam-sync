---
name: download-integrity-review
description: Review or change anything that downloads, verifies, places, converts, copies, moves or deletes clip files in the app (engine.py, drivers/http.py, storage.py, postprocess.py, migrate.py, retention.py), so an interrupted Wi-Fi or LAN link, a crash or a full disk can never leave a clip that looks complete but is not, lose a clip, or download it twice. Use for any change on that path and when a clip is reported truncated, missing or duplicated.
---

# Download and file integrity review

Interrupted links **will** happen (the car drives away, Wi-Fi fades, power is cut). These invariants must hold; each has fault-injection tests in `app/tests/test_interruptions.py`, `test_engine.py` and `test_migrate.py`.

## Invariants

1. **Nothing incomplete looks complete.** A download lives in `<target>/.partial/<id>.part` until verified; only then is it moved to its final name and recorded (`complete_clip`). The media browser and the integration only show recorded clips.
2. **Size is checked against every size the camera states**: the listing, `Content-Length` (`start + length`), and the `Content-Range` total. They must agree; too large is discarded; too short is kept and resumed.
3. **No size anywhere → confirm the end.** A body without a length ends at a closed connection, which a dropped link looks exactly like. Ask for `Range: bytes=<size>-`: `416` or an empty `206` confirms; data means incomplete; no confirmation means the clip fails (never marked done).
4. **Resumes are exact.** Bytes are `fsync`ed before the offset is recorded; on resume the part is truncated to the recorded offset. A `206` must start exactly at the requested offset (`Content-Range`); from byte 0 restarts the file; anywhere else is refused, never spliced.
5. **Progress or stop.** A resume that writes no bytes and is still short discards the part (the file on the card is not what was listed).
6. **Crash windows are closed.** Every multi-step file operation is ordered so a crash leaves either the old state or a state the next run adopts:
   - place then record: a placed but unrecorded file of the right size is adopted (`_adopt_placed`);
   - remux: record the new `.mp4` before deleting the `.ts`;
   - copy/ship/move: copy to a hidden temp, check size, rename; a finished same-size copy is reused (`reuse_or_unique`), never duplicated as `_2`;
   - move: the clip file itself goes last, so a failure leaves it where the catalogue points.
7. **One writer at a time.** Shipping, retention, deletes and the clip mover take the daemon's `files_lock` (per clip for the mover). Never hold it across an `await`.
8. **The camera is always put back into recording**, even after errors, cancellation or a crash (restore flag on disk).
9. **Clean leftovers safely**: only unreferenced `.part` files and old hidden temp files, only on targets that are really mounted.
10. **Never delete the last copy** of a clip, and never delete from the camera before the local copy is verified.

## Fault injection

The fake camera (`app/tests/fakecam.py`, `Faults`) can: drop after N bytes, end cleanly early (`eof_after`), stall (`stall_after`), hide all sizes (`hide_length`), answer a Range from byte 0 or from the wrong offset, ignore Range, send a short body, and answer `416` past the end. For any new failure mode: add the fault, write the test, show it failing on the old code.

## Questions for every change on this path

- What if the link drops, or the process is killed, between each pair of lines that touch a file or the catalogue?
- What if the camera answers with a different status, no length, or a wrong range?
- What if the destination is full, unmounted, or on another file system?
- Can this run concurrently with shipping, retention, a delete or a move?
