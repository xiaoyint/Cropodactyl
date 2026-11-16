"""Video metadata via ffprobe, with a small thread-safe prefetching cache."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from fractions import Fraction

from . import geometry


class ProbeError(Exception):
    """The file cannot be used (corrupt, unsupported, no video, ...)."""


@dataclass(frozen=True)
class AudioStream:
    index: int  # absolute stream index in the input file
    codec: str
    channels: int
    bit_rate: int | None
    sample_rate: int | None


@dataclass(frozen=True)
class VideoInfo:
    path: str
    video_index: int
    codec: str
    pix_fmt: str
    raw_w: int  # coded size as stored in the file
    raw_h: int
    rotation: int  # 0, 90, 180 or 270
    frame_w: int  # size after rotation (what ffmpeg filters see)
    frame_h: int
    sar: float  # sample aspect ratio after rotation
    sar_text: str
    display_w: int
    display_h: int
    duration: float
    fps: float
    fps_text: str
    variable_fps: bool
    color_transfer: str
    hdr: str | None  # "hlg", "pq" or None
    bit_depth: int
    audio: tuple[AudioStream, ...] = field(default_factory=tuple)
    size_bytes: int = 0
    tags: tuple[tuple[str, str], ...] = ()  # container metadata (date, GPS, device, ...)

    def tag(self, name: str) -> str | None:
        name = name.lower()
        for k, v in self.tags:
            if k.lower() == name:
                return v
        return None

    @property
    def location(self) -> str | None:
        """ISO 6709 geotag (Android ``location`` or Apple ISO6709 key)."""
        return self.tag("location") or self.tag("com.apple.quicktime.location.ISO6709")

    @property
    def creation_time(self) -> str | None:
        return self.tag("creation_time")

    @property
    def has_audio(self) -> bool:
        return bool(self.audio)

    @property
    def is_portrait(self) -> bool:
        return self.display_h > self.display_w


def _fraction(text: str | None) -> Fraction | None:
    if not text or text in ("N/A", "0/0"):
        return None
    try:
        if ":" in text:
            a, b = text.split(":", 1)
            f = Fraction(int(a), int(b))
        else:
            f = Fraction(text)
    except (ValueError, ZeroDivisionError):
        return None
    return f if f > 0 else None


def _float(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and f > 0 else None  # rejects NaN and <= 0


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _rotation(stream: dict) -> int:
    raw = None
    for sd in stream.get("side_data_list") or []:
        if "rotation" in sd:
            raw = sd["rotation"]
            break
    if raw is None:
        raw = (stream.get("tags") or {}).get("rotate", 0)
    try:
        rot = float(raw or 0)
    except (TypeError, ValueError):
        rot = 0.0
    # ffmpeg only auto-rotates by multiples of 90 degrees.
    return int(round(rot / 90.0) * 90) % 360


def _bit_depth(pix_fmt: str, stream: dict) -> int:
    raw = _int(stream.get("bits_per_raw_sample"))
    if raw:
        return raw
    for depth in (16, 14, 12, 10, 9):
        if f"p{depth}" in pix_fmt:
            return depth
    return 8


def parse_probe(path: str, data: dict) -> VideoInfo:
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    videos = [s for s in streams if s.get("codec_type") == "video"
              and not (s.get("disposition") or {}).get("attached_pic")]
    if not videos:
        raise ProbeError("no video stream found")
    v = videos[0]
    raw_w, raw_h = _int(v.get("width")), _int(v.get("height"))
    if not raw_w or not raw_h:
        raise ProbeError("video stream has no valid resolution")

    rotation = _rotation(v)
    sar_f = _fraction(v.get("sample_aspect_ratio")) or Fraction(1)
    if rotation in (90, 270):
        frame_w, frame_h = raw_h, raw_w
        sar_f = 1 / sar_f
    else:
        frame_w, frame_h = raw_w, raw_h
    sar = float(sar_f)
    dw, dh = geometry.display_size(frame_w, frame_h, sar)
    if geometry.max_k(dw, dh) < 1:
        raise ProbeError(f"video too small for a 16:10 crop ({dw}x{dh})")

    duration = _float(fmt.get("duration")) or _float(v.get("duration"))
    if not duration:
        raise ProbeError("could not determine the duration (file may be corrupt)")

    r = _fraction(v.get("r_frame_rate"))
    avg = _fraction(v.get("avg_frame_rate"))
    rate = avg or r
    fps = float(rate) if rate else 30.0
    if not 0.5 <= fps <= 1000:
        fps = 30.0
    variable = bool(r and avg and abs(float(r) - float(avg)) / float(r) > 0.002)
    fps_text = f"{fps:.3f}".rstrip("0").rstrip(".")

    transfer = str(v.get("color_transfer") or "")
    hdr = {"arib-std-b67": "hlg", "smpte2084": "pq"}.get(transfer)

    audio = tuple(
        AudioStream(
            index=int(s.get("index", 0)),
            codec=str(s.get("codec_name") or "unknown"),
            channels=_int(s.get("channels")) or 2,
            bit_rate=_int(s.get("bit_rate")),
            sample_rate=_int(s.get("sample_rate")),
        )
        for s in streams if s.get("codec_type") == "audio"
    )
    pix_fmt = str(v.get("pix_fmt") or "")
    try:
        size = os.path.getsize(path)
    except OSError:
        size = _int(fmt.get("size")) or 0

    return VideoInfo(
        path=path,
        video_index=int(v.get("index", 0)),
        codec=str(v.get("codec_name") or "unknown"),
        pix_fmt=pix_fmt,
        raw_w=raw_w, raw_h=raw_h,
        rotation=rotation,
        frame_w=frame_w, frame_h=frame_h,
        sar=sar, sar_text=f"{sar_f.numerator}:{sar_f.denominator}",
        display_w=dw, display_h=dh,
        duration=duration,
        fps=fps, fps_text=fps_text, variable_fps=variable,
        color_transfer=transfer, hdr=hdr,
        bit_depth=_bit_depth(pix_fmt, v),
        audio=audio,
        size_bytes=size,
        tags=tuple((str(k), str(v)) for k, v in (fmt.get("tags") or {}).items()),
    )


def ffprobe_json_args(ffprobe: str, path: str) -> list[str]:
    return [ffprobe, "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", path]


def run_ffprobe(path: str, ffprobe: str | None = None, timeout: float = 60) -> dict:
    ffprobe = ffprobe or shutil.which("ffprobe") or "ffprobe"
    try:
        proc = subprocess.run(ffprobe_json_args(ffprobe, path), capture_output=True,
                              text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except FileNotFoundError as e:
        raise ProbeError("ffprobe not found – please install FFmpeg") from e
    except subprocess.TimeoutExpired as e:
        raise ProbeError("ffprobe timed out") from e
    if proc.returncode != 0:
        msg = (proc.stderr or "").strip().splitlines()
        raise ProbeError(msg[-1] if msg else f"ffprobe failed ({proc.returncode})")
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as e:
        raise ProbeError("ffprobe returned invalid output") from e


def probe(path: str, ffprobe: str | None = None) -> VideoInfo:
    return parse_probe(path, run_ffprobe(path, ffprobe))


class ProbeCache:
    """Caches probe results; can prefetch a list of files in the background."""

    def __init__(self, ffprobe: str | None = None):
        self._ffprobe = ffprobe
        self._lock = threading.Lock()
        self._cache: dict[str, VideoInfo | ProbeError] = {}
        self._thread: threading.Thread | None = None
        self._queue: list[str] = []

    def get(self, path: str) -> VideoInfo:
        with self._lock:
            hit = self._cache.get(path)
        if hit is None:
            hit = self._probe(path)
        if isinstance(hit, ProbeError):
            raise hit
        return hit

    def _probe(self, path: str):
        from . import photo
        try:
            res = photo.probe_photo(path) if photo.is_photo(path) else probe(path, self._ffprobe)
        except ProbeError as e:
            res = e
        except Exception as e:  # never let a weird file kill the app
            res = ProbeError(str(e))
        with self._lock:
            self._cache[path] = res
        return res

    def prefetch(self, paths: list[str]) -> None:
        with self._lock:
            self._queue = [p for p in paths if p not in self._cache]
            running = self._thread is not None and self._thread.is_alive()
        if not running:
            self._thread = threading.Thread(target=self._worker, daemon=True,
                                            name="probe-prefetch")
            self._thread.start()

    def _worker(self) -> None:
        while True:
            with self._lock:
                if not self._queue:
                    return
                path = self._queue.pop(0)
                cached = path in self._cache
            if not cached:
                self._probe(path)
