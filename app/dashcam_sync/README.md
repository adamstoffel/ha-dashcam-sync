# Dashcam Sync

Pull the clips off a Wi-Fi dashcam into Home Assistant whenever the car is in range. No phone app, no cloud.

- Finds the camera, detects its API family and downloads clips, **event clips first**.
- Interrupted downloads **resume**; every file is verified; the camera is **always put back into recording**.
- Stores clips under `/media`, on a NAS through Home Assistant's network storage, or both, with rolling expiration.
- A panel in the sidebar for setup, clips, storage and logs.

Install the companion **Dashcam Sync integration** to get devices, entities, a live camera and a clip library in Home Assistant.

Read the *Documentation* tab for requirements, every option and the guide to overlapping camera addresses.
