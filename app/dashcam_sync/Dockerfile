# Home Assistant's base image; the tag tracks patch releases of Python and Alpine, so every
# rebuild picks up their fixes. Move to a new Python or Alpine minor by editing this tag.
ARG BUILD_FROM=ghcr.io/home-assistant/base-python:3.13-alpine3.22
FROM ${BUILD_FROM}

ENV LANG=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/opt/dashcam/src \
    DASHCAM_DATA=/data \
    DASHCAM_MEDIA=/media \
    DASHCAM_SHARE=/share

# ffmpeg remuxes clips and makes thumbnails; iputils-ping backs the overlap probe.
RUN apk add --no-cache ffmpeg iputils-ping

COPY requirements.txt /tmp/requirements.txt
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt
ENV PATH="/opt/venv/bin:${PATH}"

COPY src /opt/dashcam/src
COPY rootfs /

LABEL io.hass.type="addon" \
      io.hass.name="Dashcam Sync" \
      io.hass.description="Pull clips from Wi-Fi dashcams into Home Assistant" \
      org.opencontainers.image.source="https://github.com/adamstoffel/ha-dashcam-sync" \
      org.opencontainers.image.licenses="Apache-2.0"
