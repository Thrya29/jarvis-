"""The JARVIS icon, drawn in code (tray icon at runtime, .ico at build time)."""

from __future__ import annotations

from PIL import Image, ImageDraw


def icon_image(size: int = 64, active: bool = False) -> Image.Image:
    scale = 4  # draw large and downsample for smooth edges
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((s * 0.03, s * 0.03, s * 0.97, s * 0.97), fill=(11, 20, 34, 255))
    ring = (63, 169, 245, 255) if not active else (63, 185, 80, 255)
    w = max(1, int(s * 0.07))
    d.ellipse((s * 0.16, s * 0.16, s * 0.84, s * 0.84), outline=ring, width=w)
    d.ellipse((s * 0.36, s * 0.36, s * 0.64, s * 0.64), fill=ring)
    return img.resize((size, size), Image.Resampling.LANCZOS)


def write_ico(path: str) -> None:
    sizes = [16, 20, 24, 32, 40, 48, 64, 128, 256]
    icon_image(256).save(path, format="ICO", sizes=[(n, n) for n in sizes])
