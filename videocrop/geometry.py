"""Pure crop geometry for an aspect-locked crop rectangle (16:10 or 1:1).

Coordinate systems
------------------
* **frame**   – pixels of the decoded frame *after* rotation metadata has been
  applied (this is what FFmpeg's filter graph sees, because ffmpeg
  auto-rotates before user filters).
* **display** – frame pixels corrected for non-square pixels (SAR), i.e. what
  the viewer actually sees.  For square-pixel sources display == frame.
* **widget**  – screen pixels of the preview widget (handled by
  :class:`ViewTransform`).

The crop rectangle is stored in display coordinates as ``(x, y, k)`` with
``width = aw * k`` and ``height = ah * k`` where ``(aw, ah)`` is the aspect
unit: (16, 10) for 16:10 (the default) or (10, 10) for square.  Using an
integer multiplier makes the aspect ratio exact by construction, and for
square-pixel sources a crop of at most 1728x1080 (16:10) or 1080x1080 (1:1)
can be exported without any rescaling.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

AR_W = 16
AR_H = 10
ASPECT = AR_W / AR_H  # 1.6

WIDE = (AR_W, AR_H)  # 16:10
SQUARE = (10, 10)    # 1:1 (unit 10 px so sizes step like 16:10 heights)
ASPECTS = {WIDE: "16:10", SQUARE: "1:1"}

# Output bounding boxes.  The largest crop of the chosen aspect that fits the
# box is the output limit; smaller crops are never upscaled.
VIDEO_BOX = (1920, 1080)  # "1080p": 1728x1080 at 16:10, 1080x1080 square
PHOTO_BOX = (7680, 7680)  # 8K: 7680x4800 at 16:10, 7680x7680 square

MAX_OUT_W = 1728
MAX_OUT_H = 1080
MAX_OUT_K = MAX_OUT_W // AR_W  # 108  -> 1728 x 1080

# Photos: no downscaling unless the crop is larger than 8K (7680 x 4800 at 16:10)
MAX_PHOTO_K = 7680 // AR_W  # 480 -> 7680 x 4800

MIN_K = 8  # smallest crop the UI allows: 128 x 80 display pixels
SMALL_OUTPUT_HEIGHT = 480  # warn below this output height

assert MAX_OUT_K * AR_H == MAX_OUT_H
assert min(VIDEO_BOX[0] // AR_W, VIDEO_BOX[1] // AR_H) == MAX_OUT_K
assert min(PHOTO_BOX[0] // AR_W, PHOTO_BOX[1] // AR_H) == MAX_PHOTO_K


@dataclass(frozen=True)
class Crop:
    """Crop rectangle in display coordinates; size is always aw*k x ah*k."""

    x: int
    y: int
    k: int
    aw: int = AR_W
    ah: int = AR_H

    @property
    def aspect(self) -> tuple[int, int]:
        return self.aw, self.ah

    @property
    def label(self) -> str:
        return ASPECTS.get(self.aspect, f"{self.aw}:{self.ah}")

    @property
    def w(self) -> int:
        return self.aw * self.k

    @property
    def h(self) -> int:
        return self.ah * self.k

    @property
    def right(self) -> int:
        return self.x + self.w

    @property
    def bottom(self) -> int:
        return self.y + self.h

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


@dataclass(frozen=True)
class SourceCrop:
    """Crop in frame pixels, as passed to FFmpeg's ``crop`` filter."""

    x: int
    y: int
    w: int
    h: int


# --------------------------------------------------------------------------
# Basic helpers
# --------------------------------------------------------------------------

def display_size(frame_w: int, frame_h: int, sar: float | None) -> tuple[int, int]:
    """Size of the frame as it should be displayed (square pixels).

    The larger dimension is stretched so that no resolution is thrown away.
    """
    if not sar or sar <= 0 or abs(sar - 1.0) < 1e-6:
        return frame_w, frame_h
    if sar > 1.0:
        return max(1, round(frame_w * sar)), frame_h
    return frame_w, max(1, round(frame_h / sar))


def max_k(dw: int, dh: int, aspect: tuple[int, int] = WIDE) -> int:
    """Largest multiplier so that an aw*k x ah*k rectangle fits in dw x dh."""
    aw, ah = aspect
    return max(0, min(dw // aw, dh // ah))


def min_k(dw: int, dh: int, aspect: tuple[int, int] = WIDE) -> int:
    return max(1, min(MIN_K, max_k(dw, dh, aspect)))


def initial_crop(dw: int, dh: int, aspect: tuple[int, int] = WIDE) -> Crop:
    """Largest possible centred crop of the given aspect (default 16:10)."""
    aw, ah = aspect
    k = max_k(dw, dh, aspect)
    if k < 1:
        raise ValueError(f"video too small for a {aw}:{ah} crop ({dw}x{dh})")
    return Crop((dw - aw * k) // 2, (dh - ah * k) // 2, k, aw, ah)


def clamp_crop(c: Crop, dw: int, dh: int) -> Crop:
    """Force the crop inside the frame and within the allowed size range."""
    k = max(min_k(dw, dh, c.aspect), min(c.k, max_k(dw, dh, c.aspect)))
    w, h = c.aw * k, c.ah * k
    x = min(max(int(c.x), 0), dw - w)
    y = min(max(int(c.y), 0), dh - h)
    return Crop(x, y, k, c.aw, c.ah)


def moved(c: Crop, dx: float, dy: float, dw: int, dh: int) -> Crop:
    return clamp_crop(Crop(round(c.x + dx), round(c.y + dy), c.k, c.aw, c.ah), dw, dh)


def centered_at(cx: float, cy: float, k: int, dw: int, dh: int,
                aspect: tuple[int, int] = WIDE) -> Crop:
    aw, ah = aspect
    k = max(min_k(dw, dh, aspect), min(k, max_k(dw, dh, aspect)))
    return clamp_crop(Crop(round(cx - aw * k / 2), round(cy - ah * k / 2), k, aw, ah), dw, dh)


def with_aspect(c: Crop, aspect: tuple[int, int], dw: int, dh: int) -> Crop:
    """Switch aspect ratio around the same centre, keeping the height where
    possible (16:10 -> 1:1 gives a square as tall as the old crop)."""
    if c.aspect == tuple(aspect):
        return c
    k = max(1, round(c.h / aspect[1]))
    return centered_at(c.cx, c.cy, k, dw, dh, aspect)


def zoomed(c: Crop, steps: int, dw: int, dh: int) -> Crop:
    """Grow (steps > 0) or shrink (steps < 0) the crop around its centre."""
    if steps == 0:
        return c
    dk = max(1, round(c.k * 0.04))
    return centered_at(c.cx, c.cy, c.k + steps * dk, dw, dh, c.aspect)


def resized_from_corner(anchor_x: int, anchor_y: int, sx: int, sy: int,
                        px: float, py: float, dw: int, dh: int,
                        aspect: tuple[int, int] = WIDE) -> Crop:
    """Resize by dragging a corner while the opposite corner stays fixed.

    ``(anchor_x, anchor_y)`` is the fixed corner; ``sx``/``sy`` (+1 or -1)
    give the direction from the anchor towards the dragged corner and
    ``(px, py)`` is the pointer position in display coordinates.  The
    rectangle grows so that it reaches the pointer in whichever dimension
    is further out, keeps the aspect ratio, and never leaves the frame.
    """
    aw, ah = aspect
    gx = (px - anchor_x) * sx
    gy = (py - anchor_y) * sy
    k = round(max(gx / aw, gy / ah))
    avail_w = dw - anchor_x if sx > 0 else anchor_x
    avail_h = dh - anchor_y if sy > 0 else anchor_y
    k_hi = max(1, min(avail_w // aw, avail_h // ah))
    k = max(min(min_k(dw, dh, aspect), k_hi), min(k, k_hi))
    x = anchor_x if sx > 0 else anchor_x - aw * k
    y = anchor_y if sy > 0 else anchor_y - ah * k
    return clamp_crop(Crop(x, y, k, aw, ah), dw, dh)


# --------------------------------------------------------------------------
# Mapping to the source frame and the output resolution
# --------------------------------------------------------------------------

def _even_floor(v: float) -> int:
    i = int(math.floor(v + 1e-9))
    return i - (i % 2)


def _even_round(v: float) -> int:
    return 2 * int(round(v / 2.0))


def to_source_crop(c: Crop, frame_w: int, frame_h: int, dw: int, dh: int) -> SourceCrop:
    """Translate a display-space crop to even-aligned frame pixels."""
    sx = frame_w / dw
    sy = frame_h / dh
    max_w = frame_w - frame_w % 2
    max_h = frame_h - frame_h % 2
    w = min(max(2, _even_round(c.w * sx)), max_w)
    h = min(max(2, _even_round(c.h * sy)), max_h)
    x = _even_floor(min(c.x * sx, frame_w - w))
    y = _even_floor(min(c.y * sy, frame_h - h))
    return SourceCrop(max(0, x), max(0, y), w, h)


def max_out_k(aspect: tuple[int, int], box: tuple[int, int] = VIDEO_BOX) -> int:
    """Largest output multiplier of this aspect inside the output box."""
    return min(box[0] // aspect[0], box[1] // aspect[1])


def output_k(c: Crop, box: tuple[int, int] = VIDEO_BOX) -> int:
    return min(c.k, max_out_k(c.aspect, box))


def output_size(c: Crop, box: tuple[int, int] = VIDEO_BOX) -> tuple[int, int]:
    """Exact-aspect output size: fits the box (1728x1080 / 1080x1080 for
    video), never upscaled."""
    k = output_k(c, box)
    return c.aw * k, c.ah * k


def is_downscaled(c: Crop, box: tuple[int, int] = VIDEO_BOX) -> bool:
    return output_k(c, box) < c.k


def needs_scaling(src: SourceCrop, out_w: int, out_h: int) -> bool:
    return (src.w, src.h) != (out_w, out_h)


def is_small_output(c: Crop, box: tuple[int, int] = VIDEO_BOX) -> bool:
    return output_size(c, box)[1] < SMALL_OUTPUT_HEIGHT


def fits_box(w: int, h: int, box: tuple[int, int] = VIDEO_BOX) -> bool:
    """Does w x h fit the box in either orientation?  (1080p means 1920x1080
    landscape or 1080x1920 portrait.)"""
    return max(w, h) <= max(box) and min(w, h) <= min(box)


def fit_size(dw: int, dh: int, box: tuple[int, int] = VIDEO_BOX,
             even: bool = True) -> tuple[int, int]:
    """Uncropped output size: the whole display frame scaled down (never up)
    to fit the box in its own orientation, aspect ratio kept.  With *even*
    both sides are rounded down to even numbers (needed for yuv420p video)."""
    s = min(1.0, max(box) / max(dw, dh), min(box) / min(dw, dh))
    if even:
        return max(2, _even_floor(dw * s + 1e-6)), max(2, _even_floor(dh * s + 1e-6))
    return max(1, int(math.floor(dw * s + 1e-6))), max(1, int(math.floor(dh * s + 1e-6)))


def same_aspect(w: int, h: int, dw: int, dh: int, tol_px: float = 2.0) -> bool:
    """w x h has the aspect ratio of dw x dh up to rounding (*tol_px* pixels)."""
    return abs(w * dh / dw - h) <= tol_px


# --------------------------------------------------------------------------
# Preview mapping (display <-> widget)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ViewTransform:
    """widget = offset + display * scale"""

    scale: float
    ox: float
    oy: float

    def to_widget(self, x: float, y: float) -> tuple[float, float]:
        return self.ox + x * self.scale, self.oy + y * self.scale

    def to_display(self, wx: float, wy: float) -> tuple[float, float]:
        return (wx - self.ox) / self.scale, (wy - self.oy) / self.scale


def fit_frame(dw: int, dh: int, vw: int, vh: int, margin: int = 12) -> ViewTransform:
    """Show the whole frame centred in the view."""
    aw = max(1, vw - 2 * margin)
    ah = max(1, vh - 2 * margin)
    s = min(aw / dw, ah / dh)
    return ViewTransform(s, (vw - dw * s) / 2, (vh - dh * s) / 2)


def fit_crop(c: Crop, vw: int, vh: int, margin: int = 12) -> ViewTransform:
    """Zoom so that only the crop region fills the view (preview mode)."""
    aw = max(1, vw - 2 * margin)
    ah = max(1, vh - 2 * margin)
    s = min(aw / c.w, ah / c.h)
    left = (vw - c.w * s) / 2
    top = (vh - c.h * s) / 2
    return ViewTransform(s, left - c.x * s, top - c.y * s)
