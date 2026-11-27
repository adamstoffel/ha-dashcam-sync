# Screenshot credits

The panel screenshots in this folder show clips cut from **"Dashcam Recording (urban)"** by Fernost, from Wikimedia Commons, released under **CC0 1.0** (public domain dedication):
<https://commons.wikimedia.org/wiki/File:Dashcam_Recording_(urban).ogv>

Nothing here comes from a real user's camera. Names, addresses and folders in the screenshots are examples.

To regenerate them (needs ffmpeg and the test environment from CONTRIBUTING.md), put any dashcam videos you may use in a folder and run:

```bash
DASHCAM_DEMO_FOOTAGE=/path/to/videos node e2e/ui/panel.cjs --screens docs/images
```

Without `DASHCAM_DEMO_FOOTAGE` the demo uses generated test-pattern video instead.
