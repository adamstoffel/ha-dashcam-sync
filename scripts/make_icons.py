"""Generate the icon and logo PNGs for the app and the integration.

Run with any Python that has Pillow: ``python scripts/make_icons.py``.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
BLUE = (3, 169, 244, 255)
DARK = (17, 56, 87, 255)
WHITE = (255, 255, 255, 255)
SCALE = 4  # draw large, then downsample for smooth edges


def glyph(size: int, bg: bool = True) -> Image.Image:
    """A car with Wi-Fi arcs above it, on a rounded blue square."""
    s = size * SCALE
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if bg:
        d.rounded_rectangle((0, 0, s - 1, s - 1), radius=s // 5, fill=BLUE)
    fg = WHITE if bg else BLUE
    # Wi-Fi arcs
    cx, cy = s * 0.5, s * 0.46
    for i, r in enumerate((0.12, 0.2, 0.28)):
        rr = s * r
        d.arc((cx - rr, cy - rr, cx + rr, cy + rr), 215, 325, fill=fg, width=int(s * 0.04))
    d.ellipse((cx - s * 0.025, cy - s * 0.025, cx + s * 0.025, cy + s * 0.025), fill=fg)
    # Car body
    body_top, body_bottom = s * 0.60, s * 0.78
    d.rounded_rectangle((s * 0.16, body_top, s * 0.84, body_bottom), radius=s * 0.05, fill=fg)
    d.polygon(
        [(s * 0.27, body_top), (s * 0.35, s * 0.50), (s * 0.65, s * 0.50), (s * 0.73, body_top)],
        fill=fg,
    )
    # Windows and wheels punched out in the background colour
    hole = BLUE if bg else (0, 0, 0, 0)
    d.polygon(
        [
            (s * 0.33, body_top - s * 0.01),
            (s * 0.385, s * 0.54),
            (s * 0.615, s * 0.54),
            (s * 0.67, body_top - s * 0.01),
        ],
        fill=hole,
    )
    for x in (0.31, 0.69):
        d.ellipse((s * (x - 0.07), s * 0.72, s * (x + 0.07), s * 0.86), fill=DARK if bg else fg)
        d.ellipse(
            (s * (x - 0.03), s * 0.76, s * (x + 0.03), s * 0.82), fill=WHITE if bg else (0, 0, 0, 0)
        )
    return img.resize((size, size), Image.LANCZOS)


def logo(width: int, height: int) -> Image.Image:
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    mark = glyph(height - 10)
    img.paste(mark, (5, 5), mark)
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", max(12, height // 6))
    except OSError:
        font = ImageFont.load_default()
    d.text((height + 4, height * 0.28), "Dashcam", fill=BLUE, font=font)
    d.text((height + 4, height * 0.28 + height // 5), "Sync", fill=DARK, font=font)
    return img


def main() -> None:
    app = ROOT / "app" / "dashcam_sync"
    brand = ROOT / "integration" / "custom_components" / "dashcam_sync" / "brand"
    brand.mkdir(parents=True, exist_ok=True)
    glyph(128).save(app / "icon.png")
    logo(250, 100).save(app / "logo.png")
    glyph(256).save(brand / "icon.png")
    logo(512, 256).save(brand / "logo.png")


if __name__ == "__main__":
    main()
