"""Device profile capture for unsupported cameras.

A read-only session that records a port scan, responses to a fixed list of
probe URLs, the first 64 KB of the root page and response headers. Anything
that identifies the owner's network (SSIDs, passwords, MAC addresses, IPs
other than the camera's own) is redacted before the bundle is shown.
"""

from __future__ import annotations

import base64
import re
from typing import Any

import aiohttp

from . import __version__
from .detect import fingerprint, split_host
from .drivers.http import MAX_BODY

PROBE_URLS = (
    "/",
    "/index.html",
    "/?custom=1&cmd=3016",
    "/?custom=1&cmd=3012",
    "/app/getproductinfo",
    "/Config/version.bin",
    "/cgi-bin/Config.cgi",
    "/cgi-bin/Config.cgi?action=get&property=Camera.Menu.FWversion",
    "/DCIM/",
    "/CARDV/",
    "/CARDV/MOVIE/",
    "/blackvue_vod.cgi",
    "/command/command.cgi?op=100",
    "/api/info",
)
MAX_ROOT = 64 * 1024
PROFILE_VERSION = 1
REDACTED = "REDACTED"

_MAC = re.compile(r"\b[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}\b")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_JSON_SECRET = re.compile(
    r'(?i)("(?:ssid|pass(?:word|phrase)?|pwd|psk|key|wifi_?pass\w*|ap_?ssid|sta_?ssid)"\s*:\s*")[^"]*(")'
)
_XML_SECRET = re.compile(
    r"(?is)(<(?:ssid|pass(?:word|phrase)?|pwd|psk|key|wifipassword|string)>)[^<]*(</)"
)
_QUERY_SECRET = re.compile(r"(?i)((?:ssid|pass(?:word|phrase)?|pwd|psk)=)[^&\s\"'<]+")


def redact(text: str, camera_ip: str) -> str:
    """Strip identifying values from a captured response."""
    text = _MAC.sub("XX:XX:XX:XX:XX:XX", text)

    def ip(match: re.Match[str]) -> str:
        return match.group(0) if match.group(0) == camera_ip else "x.x.x.x"

    text = _IPV4.sub(ip, text)
    text = _JSON_SECRET.sub(rf"\g<1>{REDACTED}\g<2>", text)
    text = _XML_SECRET.sub(rf"\g<1>{REDACTED}\g<2>", text)
    return _QUERY_SECRET.sub(rf"\g<1>{REDACTED}", text)


async def capture_profile(
    session: aiohttp.ClientSession, host: str, *, timeout: float = 4.0
) -> dict[str, Any]:
    """Return a redacted bundle suitable for a GitHub issue and for fixtures."""
    name, _ = split_host(host)
    ports = await fingerprint(host)
    responses: dict[str, Any] = {}
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    for path in PROBE_URLS:
        key = f"GET {path}"
        try:
            async with session.get(
                f"http://{host}{path}", timeout=client_timeout, allow_redirects=False
            ) as resp:
                raw = await resp.content.read(MAX_ROOT if path == "/" else MAX_ROOT // 8)
                headers = {
                    k: redact(v, name) for k, v in resp.headers.items() if k.lower() != "set-cookie"
                }
                text = raw.decode("utf-8", errors="replace")
                body: dict[str, Any]
                if _mostly_text(raw):
                    body = {"text": redact(text, name)}
                else:
                    body = {"base64_head": base64.b64encode(raw[:256]).decode()}
                responses[key] = {"status": resp.status, "headers": headers, **body}
        except (aiohttp.ClientError, TimeoutError, OSError) as err:
            responses[key] = {"error": type(err).__name__}
    return {
        "profile_version": PROFILE_VERSION,
        "app_version": __version__,
        "camera_host": name,
        "ports": {str(p): ok for p, ok in ports.items()},
        "responses": responses,
        "limits": {"max_body": MAX_BODY, "root_bytes": MAX_ROOT},
    }


def _mostly_text(raw: bytes) -> bool:
    if not raw:
        return True
    sample = raw[:512]
    printable = sum(1 for b in sample if b in (9, 10, 13) or 32 <= b < 127)
    return printable / len(sample) > 0.85
