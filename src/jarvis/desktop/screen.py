"""Screen capture with the scaling rules the computer-use model expects.

The model sees a downscaled screenshot and answers in that screenshot's pixel space;
:class:`ScreenMap` converts both ways, and is recomputed on every capture so a
resolution change mid-task can't skew clicks.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass

from PIL import Image

# Toolset limits for Claude 5.5-class models are 2576 px on the long edge and
# ~3.75 MP; staying well under 2000 px per side lets long sessions keep every
# screenshot without hitting the stricter many-image limits.
HARD_MAX_EDGE = 2000


@dataclass(frozen=True)
class ScreenMap:
    left: int  # monitor origin in virtual-desktop pixels
    top: int
    width: int  # physical monitor size
    height: int
    scale: float  # screenshot px = physical px * scale

    @property
    def shot_size(self) -> tuple[int, int]:
        return max(1, round(self.width * self.scale)), max(1, round(self.height * self.scale))

    def to_screen(self, x: float, y: float) -> tuple[int, int]:
        sw, sh = self.shot_size
        if not (0 <= x <= sw and 0 <= y <= sh):
            raise ValueError(f"coordinate ({x}, {y}) is outside the {sw}x{sh} screenshot")
        px = self.left + min(self.width - 1, int(x / self.scale))
        py = self.top + min(self.height - 1, int(y / self.scale))
        return px, py

    def contains(self, px: int, py: int) -> bool:
        return self.left <= px < self.left + self.width and self.top <= py < self.top + self.height

    def to_shot(self, px: int, py: int) -> tuple[int, int]:
        return round((px - self.left) * self.scale), round((py - self.top) * self.scale)


def scale_for(width: int, height: int, max_edge: int) -> float:
    edge = min(max_edge, HARD_MAX_EDGE)
    return min(1.0, edge / max(width, height))


def encode_png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


class ScreenCapture:
    def __init__(self, monitor: int = 1, max_edge: int = 1366) -> None:
        self._monitor = monitor
        self._max_edge = max_edge
        self.map: ScreenMap | None = None
        self._last_full: Image.Image | None = None

    def _grab(self) -> tuple[Image.Image, ScreenMap]:
        import mss

        with mss.MSS() as sct:
            if self._monitor >= len(sct.monitors):
                raise OSError(
                    f"monitor {self._monitor} not found ({len(sct.monitors) - 1} present)"
                )
            mon = sct.monitors[self._monitor]
            raw = sct.grab(mon)
        img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        smap = ScreenMap(
            mon["left"],
            mon["top"],
            raw.size[0],
            raw.size[1],
            scale_for(raw.size[0], raw.size[1], self._max_edge),
        )
        return img, smap

    def screenshot(self) -> bytes:
        img, smap = self._grab()
        self.map, self._last_full = smap, img
        return encode_png(img.resize(smap.shot_size, Image.Resampling.LANCZOS))

    def zoom(self, region: tuple[float, float, float, float]) -> bytes:
        """Full-resolution crop of a region given in screenshot coordinates."""
        if self.map is None:
            self.screenshot()
        assert self.map is not None
        img, _ = self._grab()
        x0, y0, x1, y1 = region
        if x1 <= x0 or y1 <= y0:
            raise ValueError("zoom region must be [x0, y0, x1, y1] with x1 > x0 and y1 > y0")
        s = self.map.scale
        box = (
            max(0, math.floor(x0 / s)),
            max(0, math.floor(y0 / s)),
            min(img.width, math.ceil(x1 / s)),
            min(img.height, math.ceil(y1 / s)),
        )
        crop = img.crop(box)
        fit = min(1.0, min(self._max_edge, HARD_MAX_EDGE) / max(crop.width, crop.height))
        if fit < 1.0:
            crop = crop.resize(
                (max(1, round(crop.width * fit)), max(1, round(crop.height * fit))),
                Image.Resampling.LANCZOS,
            )
        return encode_png(crop)

    def monitor_area(self) -> tuple[int, int, int, int]:
        """(left, top, width, height) of the controlled monitor, freshly measured."""
        import mss

        with mss.MSS() as sct:
            if self._monitor >= len(sct.monitors):
                raise OSError(f"monitor {self._monitor} not found")
            mon = sct.monitors[self._monitor]
        return mon["left"], mon["top"], mon["width"], mon["height"]

    def current_map(self) -> ScreenMap:
        if self.map is None:
            _, self.map = self._grab()
        return self.map
