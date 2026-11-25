# Writing a driver

Every camera family is a driver behind one interface (`app/dashcam_sync/src/dashcam_sync/drivers/base.py`). Detection runs each driver's cheap, read-only probe in order of specificity until one claims the camera.

```python
class Driver(Protocol):
    name: ClassVar[str]                 # "novatek"
    default_hosts: ClassVar[list[str]]  # ["192.168.1.254"]
    capabilities: ClassVar[Caps]        # list, range, delete, live, station_mode, gps

    async def probe(self, host: str) -> Probe | None: ...   # read-only, <= 3 s, returns confidence + model info
    async def info(self) -> DeviceInfo: ...                 # firmware, model, SD state, free space
    async def prepare(self) -> None: ...                    # e.g. stop recording, enter playback
    async def list_recordings(self) -> list[Recording]: ...
    async def open(self, rec: Recording, offset: int) -> ByteStream: ...
    async def delete(self, rec: Recording) -> None: ...
    async def restore(self) -> None: ...                    # back to movie mode + recording; idempotent
    def live_url(self) -> str | None: ...
```

## Rules a driver must follow

- **Be read-only until `prepare()`.** `probe()` and detection never change the camera.
- **`restore()` must be idempotent.** The engine calls it from a `finally`, after errors, and first on every reconnect when the restore flag is set.
- **Map vendor names onto kinds.** `Recording.kind` is `normal`, `event`, `parking` or `photo`; `channel` is `front`, `rear` or `interior`. Use `classify()` in `base.py` or your own folder map, so retention and the UI never see vendor words.
- **Never trust the camera.** Use `CameraHttp` (capped bodies, timeouts, one request at a time, defusedxml). Never use a camera-supplied path as a local file name; paths are only stored as metadata.
- **`open()` returns a `ByteStream`** with `start` (the offset actually served: `0` when the camera ignored Range) and `content_length`. The engine restarts the file when `start != offset`.

## Steps

1. Get a [device profile](device-profile.md) and save it as `app/tests/fixtures/<family>.json`.
2. Add `drivers/<family>.py`, register it in `drivers/__init__.py` (specific drivers before `http_index`).
3. Run `pytest app/tests/test_fixtures.py`: your driver must claim your fixture, and no other specific driver may claim it.
4. Add a fake-camera dialect to `app/tests/fakecam.py` and driver tests like `test_drivers.py` (probe, list, download, resume, restore).
5. Add the row to [supported-cameras.md](supported-cameras.md) and the driver name to the `driver` schema in `config.yaml`, the translations and `CameraOptions`.

New drivers are `experimental` until two users confirm them on real hardware.
