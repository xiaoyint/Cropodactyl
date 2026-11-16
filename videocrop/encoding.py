"""Encoding plan, FFmpeg command lines, size-constrained encode state machine
and output validation.

The :class:`EncodeSession` state machine contains all decisions; it only
*describes* the processes to run (:class:`Step`).  The GUI executes the steps
with ``QProcess`` (see ``queue.py``); :func:`run_session_blocking` executes
them with ``subprocess`` (used by tests and handy for debugging).

Strategy per video
------------------
1. Compute the bit budget for ``TARGET_BYTES`` (120 MB) after subtracting
   audio and container overhead.
2. If the budget is generous, encode once with CRF (quality mode).  While it
   runs, the projected final size is watched; if the file is clearly going to
   exceed the hard limit the encode is aborted early.
3. Otherwise (or if the CRF result is too big) run a two-pass ABR encode at
   the budget bitrate.
4. If the result is still >= the hard limit, redo pass 2 with a reduced
   bitrate (the pass-1 statistics are reused), up to ``MAX_RETRIES`` times.
5. Validate the file with ffprobe; only then move it to its final name.
"""
from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field

from . import geometry, mp4meta
from .geometry import Crop, SourceCrop
from .probe import VideoInfo

HARD_LIMIT_BYTES = 125_000_000  # output must be strictly smaller (125 MB, decimal)
TARGET_BYTES = 120_000_000      # budget used for bitrate calculations
CRF = 18
PRESET = "medium"
MAX_RETRIES = 3
MIN_CRF_BPP = 0.05      # budget below this many bits/pixel -> skip the CRF attempt
LOW_QUALITY_BPP = 0.02  # budget below this -> warn about quality
MIN_VIDEO_BPS = 80_000
DISK_RESERVE_BYTES = 64 * 1024 * 1024


class PlanError(Exception):
    pass


@dataclass(frozen=True)
class Limits:
    hard_limit: int = HARD_LIMIT_BYTES
    target: int = TARGET_BYTES

    @property
    def overhead(self) -> int:
        """Bytes reserved for MP4 container overhead (moov, sample tables)."""
        return int(self.target * 0.01) + 100_000


# --------------------------------------------------------------------------
# FFmpeg capabilities
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class FFmpegCaps:
    ffmpeg: str
    ffprobe: str
    version: str
    fps_mode_flag: str  # "-fps_mode" (ffmpeg >= 5.1) or "-vsync" (4.x)
    has_zscale: bool
    has_tonemap: bool
    nice: str | None
    jpegtran: str | None = None  # lossless JPEG crop (libjpeg-turbo-progs)

    @property
    def can_tonemap(self) -> bool:
        return self.has_zscale and self.has_tonemap


class MissingFFmpeg(Exception):
    pass


def _run_text(args: list[str]) -> str:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=30,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (p.stdout or "") + (p.stderr or "")


@functools.lru_cache(maxsize=1)
def detect_caps() -> FFmpegCaps:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise MissingFFmpeg("ffmpeg/ffprobe not found. Install with: sudo apt install ffmpeg")
    version_text = _run_text([ffmpeg, "-hide_banner", "-version"])
    first = version_text.splitlines()[0] if version_text else ""
    encoders = _run_text([ffmpeg, "-hide_banner", "-encoders"])
    if not re.search(r"\blibx264\b", encoders):
        raise MissingFFmpeg("this ffmpeg build has no libx264 encoder")
    if not re.search(r"\baac\b", encoders):
        raise MissingFFmpeg("this ffmpeg build has no AAC encoder")
    filters = _run_text([ffmpeg, "-hide_banner", "-filters"])
    options = _run_text([ffmpeg, "-hide_banner", "-h", "long"])
    return FFmpegCaps(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        version=first,
        fps_mode_flag="-fps_mode" if "-fps_mode" in options else "-vsync",
        has_zscale=bool(re.search(r"\bzscale\b", filters)),
        has_tonemap=bool(re.search(r"\btonemap\b", filters)),
        nice=shutil.which("nice"),
        jpegtran=shutil.which("jpegtran"),
    )


# --------------------------------------------------------------------------
# Plan
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class AudioPlan:
    stream_index: int  # absolute input stream index
    copy: bool
    bitrate: int  # bits/s (actual for copies, target for encodes)


@dataclass
class EncodePlan:
    source: str
    info: VideoInfo
    crop: Crop | None  # None: whole frame, no crop (skipped in the GUI)
    src_crop: SourceCrop
    out_w: int
    out_h: int
    scaled: bool
    filter: str
    color_args: list[str]
    audio: list[AudioPlan]
    budget_video_bps: int
    try_crf_first: bool
    limits: Limits
    warnings: list[str] = field(default_factory=list)

    @property
    def audio_bps(self) -> int:
        return sum(a.bitrate for a in self.audio)

    @property
    def output_ext(self) -> str:
        return ".mp4"

    def make_session(self, caps, tmp_dir: str, final_path: str, may_overwrite: bool = False):
        return EncodeSession(self, caps, tmp_dir, final_path, may_overwrite)


AUDIO_SHARE = 0.12  # max share of the size budget for audio on long videos


def _audio_default_bps(channels: int) -> int:
    return 128_000 if channels <= 1 else 192_000 if channels <= 2 else 384_000


def _audio_min_bps(channels: int) -> int:
    return 32_000 if channels <= 1 else 48_000 if channels <= 2 else 96_000


def _audio_weight(channels: int) -> float:
    return 1.0 if channels <= 1 else 1.5 if channels <= 2 else 3.0


def plan_audio(info: VideoInfo, limits: Limits) -> list[AudioPlan]:
    """Choose per-stream audio handling.

    Normally AAC up to 320 kb/s is copied untouched (no generation loss) and
    anything else becomes AAC at a good bitrate.  When audio would use more
    than ~10 % of the size budget (long videos), each stream gets a share of
    ``AUDIO_SHARE`` of the budget instead (never below a sane minimum).
    """
    plans = []
    for a in info.audio:
        if a.codec == "aac" and a.bit_rate and a.bit_rate <= 320_000:
            plans.append(AudioPlan(a.index, True, a.bit_rate))
        else:
            plans.append(AudioPlan(a.index, False, _audio_default_bps(a.channels)))
    total_bps = (limits.target - limits.overhead) * 8 / info.duration
    if not plans or sum(p.bitrate for p in plans) <= 0.10 * total_bps:
        return plans

    weights = [_audio_weight(a.channels) for a in info.audio]
    out = []
    for a, w in zip(info.audio, weights):
        share = AUDIO_SHARE * total_bps * w / sum(weights)
        target = int(max(_audio_min_bps(a.channels),
                         min(share, _audio_default_bps(a.channels))) // 1000 * 1000)
        if a.codec == "aac" and a.bit_rate and a.bit_rate <= target:
            out.append(AudioPlan(a.index, True, a.bit_rate))
        else:
            out.append(AudioPlan(a.index, False, target))
    return out


def build_filter(info: VideoInfo, src: SourceCrop, out_w: int, out_h: int,
                 caps: FFmpegCaps | None) -> tuple[str, list[str], list[str]]:
    """Return (filtergraph, extra colour args, warnings)."""
    warnings: list[str] = []
    color_args: list[str] = []
    scaled = geometry.needs_scaling(src, out_w, out_h)
    f = [f"crop={src.w}:{src.h}:{src.x}:{src.y}:exact=1"]
    if info.hdr and caps is not None and caps.can_tonemap:
        if scaled:
            f.append(f"scale={out_w}:{out_h}:flags=lanczos")
        if info.hdr == "hlg":
            tin, npl, curve = "arib-std-b67", 300, "mobius"
        else:
            tin, npl, curve = "smpte2084", 100, "hable"
        f += [
            f"zscale=tin={tin}:pin=bt2020:min=bt2020nc:t=linear:npl={npl}",
            "format=gbrpf32le",
            "zscale=p=bt709",
            f"tonemap=tonemap={curve}:desat=0",
            "zscale=t=bt709:m=bt709:r=tv",
            "format=yuv420p",
        ]
        color_args = ["-color_primaries", "bt709", "-color_trc", "bt709",
                      "-colorspace", "bt709", "-color_range", "tv"]
    else:
        if info.hdr:
            warnings.append("HDR source but this ffmpeg lacks zscale/tonemap: colours may look washed out")
            color_args = ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
        flags = "lanczos+accurate_rnd+full_chroma_int"
        f.append(f"scale={out_w}:{out_h}:flags={flags}:out_range=tv")
        f.append("format=yuv420p")
        if "-color_range" not in color_args:
            color_args += ["-color_range", "tv"]
    f.append("setsar=1")
    return ",".join(f), color_args, warnings


def make_plan(info: VideoInfo, crop: Crop, caps: FFmpegCaps | None,
              limits: Limits = Limits()) -> EncodePlan:
    crop = geometry.clamp_crop(crop, info.display_w, info.display_h)
    src = geometry.to_source_crop(crop, info.frame_w, info.frame_h,
                                  info.display_w, info.display_h)
    out_w, out_h = geometry.output_size(crop)
    assert out_w * crop.ah == out_h * crop.aw
    return _plan(info, crop, src, out_w, out_h, caps, limits)


def make_full_plan(info: VideoInfo, caps: FFmpegCaps | None,
                   limits: Limits = Limits()) -> EncodePlan:
    """Re-encode the whole frame (no crop), scaled down to fit 1080p in the
    video's own orientation."""
    src = SourceCrop(0, 0, info.frame_w - info.frame_w % 2, info.frame_h - info.frame_h % 2)
    out_w, out_h = geometry.fit_size(info.display_w, info.display_h)
    return _plan(info, None, src, out_w, out_h, caps, limits)


def video_conversion_reasons(info: VideoInfo, limits: Limits = Limits()) -> list[str]:
    """Why this video cannot go to the frame unchanged (empty: copy it as is)."""
    reasons = []
    if os.path.splitext(info.path)[1].lower() != ".mp4":
        reasons.append("not an .mp4 file")
    if info.codec != "h264":
        reasons.append(f"codec is {info.codec}")
    if info.pix_fmt not in ("yuv420p", "yuvj420p") or info.bit_depth != 8 or info.hdr:
        reasons.append(f"{info.bit_depth}-bit {info.pix_fmt}" + (" HDR" if info.hdr else ""))
    if not geometry.fits_box(info.display_w, info.display_h):
        reasons.append(f"larger than 1080p ({info.display_w}x{info.display_h})")
    if info.rotation:
        reasons.append(f"rotation metadata ({info.rotation}°)")
    if abs(info.sar - 1) > 1e-6:
        reasons.append("non-square pixels")
    if info.size_bytes >= limits.hard_limit:
        reasons.append(f"{info.size_bytes / 1e6:.0f} MB")
    if any(a.codec != "aac" for a in info.audio):
        reasons.append("audio is not AAC")
    return reasons


def _plan(info: VideoInfo, crop: Crop | None, src: SourceCrop, out_w: int, out_h: int,
          caps: FFmpegCaps | None, limits: Limits) -> EncodePlan:
    filt, color_args, warnings = build_filter(info, src, out_w, out_h, caps)

    audio = plan_audio(info, limits)
    audio_bps = sum(a.bitrate for a in audio)
    usable_bits = (limits.target - limits.overhead) * 8
    video_bps = int((usable_bits - audio_bps * info.duration) / info.duration)
    if video_bps < MIN_VIDEO_BPS:
        raise PlanError(
            f"video is too long ({format_duration(info.duration)}) to fit under "
            f"{limits.hard_limit / 1e6:.0f} MB with acceptable quality")

    fps = min(max(info.fps, 1.0), 240.0)
    bpp = video_bps / (out_w * out_h * fps)
    if bpp < LOW_QUALITY_BPP:
        warnings.append(
            f"long video ({format_duration(info.duration)}): bitrate limited to "
            f"{video_bps / 1e6:.2f} Mb/s to stay under {limits.hard_limit / 1e6:.0f} MB – "
            "quality may be noticeably reduced")
    if crop is not None and out_h < geometry.SMALL_OUTPUT_HEIGHT:
        warnings.append(f"small crop: output is only {out_w}x{out_h}")

    return EncodePlan(
        source=info.path, info=info, crop=crop, src_crop=src,
        out_w=out_w, out_h=out_h, scaled=geometry.needs_scaling(src, out_w, out_h),
        filter=filt, color_args=color_args, audio=audio,
        budget_video_bps=video_bps, try_crf_first=bpp >= MIN_CRF_BPP,
        limits=limits, warnings=warnings,
    )


# --------------------------------------------------------------------------
# Command lines
# --------------------------------------------------------------------------

def _common_input(caps: FFmpegCaps, plan: EncodePlan) -> list[str]:
    return [caps.ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-y",
            "-i", plan.source]


def _video_args(caps: FFmpegCaps, plan: EncodePlan) -> list[str]:
    return ["-map", f"0:{plan.info.video_index}",
            "-vf", plan.filter,
            caps.fps_mode_flag, "passthrough",  # keep original timestamps / frame rate
            "-c:v", "libx264", "-preset", PRESET, "-profile:v", "high",
            "-pix_fmt", "yuv420p", *plan.color_args]


def _audio_args(plan: EncodePlan) -> list[str]:
    args: list[str] = []
    for a in plan.audio:
        args += ["-map", f"0:{a.stream_index}"]
    for i, a in enumerate(plan.audio):
        if a.copy:
            args += [f"-c:a:{i}", "copy"]
        else:
            args += [f"-c:a:{i}", "aac", f"-b:a:{i}", str(a.bitrate)]
    return args


# Container tags that describe the *source* file, not the recording; they must
# not be copied as metadata keys.
_DROP_TAGS = ("major_brand", "minor_version", "compatible_brands", "location-eng")
# Recording metadata that must survive the export (checked in validation).
_KEEP_PREFIXES = ("com.apple.quicktime.", "com.android.")


def _output_args(out_path: str) -> list[str]:
    drop = [a for t in _DROP_TAGS for a in ("-metadata", f"{t}=")]
    # -map_metadata 0 + use_metadata_tags: keep *all* recording metadata
    # (creation date, GPS, device make/model, Apple/Android keys).
    # rotate=0: rotation is already applied to the pixels (ffmpeg autorotate);
    # make sure no legacy rotate tag survives on ffmpeg 4.x muxers.
    return ["-map_metadata", "0", *drop, "-metadata:s:v:0", "rotate=0",
            "-movflags", "+faststart+use_metadata_tags",
            "-max_muxing_queue_size", "4096",
            "-progress", "pipe:1", "-nostats", "-f", "mp4", out_path]


def crf_command(caps: FFmpegCaps, plan: EncodePlan, out_path: str) -> list[str]:
    return [*_common_input(caps, plan), *_video_args(caps, plan),
            "-crf", str(CRF), *_audio_args(plan), *_output_args(out_path)]


def pass1_command(caps: FFmpegCaps, plan: EncodePlan, bitrate: int, passlog: str) -> list[str]:
    return [*_common_input(caps, plan), *_video_args(caps, plan),
            "-b:v", str(bitrate), "-pass", "1", "-passlogfile", passlog,
            "-an", "-progress", "pipe:1", "-nostats", "-f", "null", os.devnull]


def pass2_command(caps: FFmpegCaps, plan: EncodePlan, bitrate: int, passlog: str,
                  out_path: str) -> list[str]:
    return [*_common_input(caps, plan), *_video_args(caps, plan),
            "-b:v", str(bitrate), "-pass", "2", "-passlogfile", passlog,
            *_audio_args(plan), *_output_args(out_path)]


# --------------------------------------------------------------------------
# Progress parsing
# --------------------------------------------------------------------------

class ProgressParser:
    """Parses ``-progress pipe:1`` key=value output (incrementally)."""

    def __init__(self):
        self._buf = ""
        self.out_time = 0.0
        self.total_size = 0
        self.speed = ""
        self.ended = False

    def feed(self, text: str) -> bool:
        """Returns True when a progress block was completed."""
        self._buf += text
        updated = False
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            key, _, val = line.strip().partition("=")
            if key in ("out_time_us", "out_time_ms"):  # both are microseconds
                try:
                    self.out_time = max(self.out_time, int(val) / 1e6)
                except ValueError:
                    pass
            elif key == "total_size":
                try:
                    self.total_size = int(val)
                except ValueError:
                    pass
            elif key == "speed":
                self.speed = val.strip()
            elif key == "progress":
                updated = True
                if val.strip() == "end":
                    self.ended = True
        return updated


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def metadata_errors(data: dict, info: VideoInfo, path: str | None) -> list[str]:
    """Check that recording date, geotag and device metadata survived."""
    errors: list[str] = []
    out_tags = {str(k).lower(): str(v) for k, v in ((data.get("format") or {}).get("tags") or {}).items()}
    src_time = info.creation_time
    if src_time:
        out_time = out_tags.get("creation_time")
        if not out_time or out_time[:19] != src_time[:19]:
            errors.append(f"recording date lost (source {src_time}, output {out_time})")
    loc = mp4meta.normalize_iso6709(info.location)
    if loc and path:
        try:
            got = mp4meta.read_location(path)
        except (OSError, mp4meta.Mp4Error) as e:
            got = f"unreadable ({e})"
        if got != loc:
            errors.append(f"geotag lost (source {loc}, output {got})")
    for k, v in info.tags:
        if k.lower().startswith(_KEEP_PREFIXES) and out_tags.get(k.lower()) != v:
            errors.append(f"metadata {k!r} lost")
    return errors


def validate_output(data: dict, plan: EncodePlan, file_size: int,
                    path: str | None = None) -> list[str]:
    """Return a list of problems with an encoded file (empty == valid)."""
    errors: list[str] = []
    lim = plan.limits
    if file_size >= lim.hard_limit:
        errors.append(f"file is {file_size / 1e6:.1f} MB (limit {lim.hard_limit / 1e6:.0f} MB)")
    fmt = data.get("format") or {}
    if "mp4" not in str(fmt.get("format_name", "")):
        errors.append(f"container is {fmt.get('format_name')!r}, expected mp4")
    streams = data.get("streams") or []
    videos = [s for s in streams if s.get("codec_type") == "video"]
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    if len(videos) != 1:
        errors.append(f"expected 1 video stream, found {len(videos)}")
    else:
        v = videos[0]
        w, h = int(v.get("width") or 0), int(v.get("height") or 0)
        if v.get("codec_name") != "h264":
            errors.append(f"video codec is {v.get('codec_name')}, expected h264")
        if (w, h) != (plan.out_w, plan.out_h):
            errors.append(f"resolution {w}x{h}, expected {plan.out_w}x{plan.out_h}")
        if plan.crop is not None:
            aw, ah = plan.crop.aspect
            if w * ah != h * aw:
                errors.append(f"aspect ratio of {w}x{h} is not exactly {plan.crop.label}")
        elif not geometry.same_aspect(w, h, plan.info.display_w, plan.info.display_h):
            errors.append(f"aspect ratio of {w}x{h} differs from the source")
        if not geometry.fits_box(w, h):
            bw, bh = geometry.VIDEO_BOX
            errors.append(f"resolution {w}x{h} exceeds 1080p ({bw}x{bh})")
        sar = v.get("sample_aspect_ratio")
        if sar not in (None, "1:1", "0:1", "N/A"):
            errors.append(f"non-square pixels in output (SAR {sar})")
        if v.get("pix_fmt") != "yuv420p":
            errors.append(f"pixel format {v.get('pix_fmt')}, expected yuv420p")
        for sd in v.get("side_data_list") or []:
            try:
                if "rotation" in sd and float(sd["rotation"]) % 360 != 0:
                    errors.append("output still carries rotation metadata")
            except (TypeError, ValueError):
                pass
    if len(audios) != len(plan.audio):
        errors.append(f"expected {len(plan.audio)} audio stream(s), found {len(audios)}")
    for a in audios:
        if a.get("codec_name") != "aac":
            errors.append(f"audio codec is {a.get('codec_name')}, expected aac")
    try:
        out_dur = float(fmt.get("duration"))
    except (TypeError, ValueError):
        out_dur = None
    if out_dur is None:
        errors.append("output duration unknown")
    else:
        src = plan.info.duration
        if abs(out_dur - src) > max(1.0, 0.03 * src):
            errors.append(f"duration {out_dur:.2f}s differs from source {src:.2f}s")
    errors += metadata_errors(data, plan.info, path)
    return errors


# --------------------------------------------------------------------------
# Session state machine
# --------------------------------------------------------------------------

@dataclass
class Step:
    kind: str  # "ffmpeg" or "probe"
    args: list[str]
    label: str
    progress_lo: float = 0.0
    progress_hi: float = 1.0
    watch_size: bool = False  # CRF attempt: may be aborted early
    nice: bool = True


@dataclass
class StepResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    aborted: bool = False  # killed by should_abort()


@dataclass
class Outcome:
    success: bool
    message: str
    final_path: str | None = None
    size: int = 0
    method: str = ""


def disk_free(path: str) -> int:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 0


def _tail(text: str, n: int = 4) -> str:
    lines = [ln.strip() for ln in (text or "").strip().splitlines() if ln.strip()]
    msg = " | ".join(lines[-n:])
    if "No space left on device" in (text or ""):
        return "Insufficient disk space (No space left on device)"
    return msg or "unknown error"


class EncodeSession:
    def __init__(self, plan: EncodePlan, caps: FFmpegCaps, tmp_dir: str,
                 final_path: str, may_overwrite: bool = False):
        self.plan = plan
        self.caps = caps
        self.tmp_dir = tmp_dir
        self.tmp_out = os.path.join(tmp_dir, "output.mp4")
        self.passlog = os.path.join(tmp_dir, "x264pass")
        self.final_path = final_path
        self.may_overwrite = may_overwrite
        self._state = "init"
        self._bitrate = plan.budget_video_bps
        self._retries = 0
        self._method = ""
        self.notes: list[str] = []

    # -- helpers ---------------------------------------------------------
    def _crf_step(self) -> Step:
        self._state = "crf"
        return Step("ffmpeg", crf_command(self.caps, self.plan, self.tmp_out),
                    "Encoding (quality mode)", 0.0, 1.0, watch_size=True)

    def _pass1_step(self) -> Step:
        self._state = "pass1"
        return Step("ffmpeg", pass1_command(self.caps, self.plan, self._bitrate, self.passlog),
                    "Pass 1/2", 0.0, 0.5)

    def _pass2_step(self) -> Step:
        self._state = "pass2"
        label = "Pass 2/2" if not self._retries else f"Pass 2/2 (retry {self._retries})"
        return Step("ffmpeg", pass2_command(self.caps, self.plan, self._bitrate, self.passlog,
                                            self.tmp_out), label, 0.5, 1.0)

    def _probe_step(self) -> Step | Outcome:
        # Write the standard QuickTime geotag atom that photo apps read
        # (ffmpeg's MP4 muxer cannot); validation then checks the result.
        loc = mp4meta.normalize_iso6709(self.plan.info.location)
        if loc:
            try:
                mp4meta.set_location(self.tmp_out, loc)
            except (OSError, mp4meta.Mp4Error) as e:
                return Outcome(False, f"could not write geotag: {e}")
        self._state = "probe"
        return Step("probe", [self.caps.ffprobe, "-v", "error", "-print_format", "json",
                              "-show_format", "-show_streams", self.tmp_out],
                    "Validating", 1.0, 1.0, nice=False)

    def _remove_tmp_out(self) -> None:
        try:
            os.remove(self.tmp_out)
        except OSError:
            pass

    # -- public API ------------------------------------------------------
    def preflight(self) -> str | None:
        if not os.path.isfile(self.plan.source):
            return "source file no longer exists"
        need = 2 * self.plan.limits.hard_limit + DISK_RESERVE_BYTES
        free = disk_free(self.tmp_dir)
        if free < need:
            return (f"Insufficient disk space in output folder "
                    f"({free / 1e6:.0f} MB free, need {need / 1e6:.0f} MB)")
        return None

    def first_step(self) -> Step | Outcome:
        err = self.preflight()
        if err:
            return Outcome(False, err)
        return self._crf_step() if self.plan.try_crf_first else self._pass1_step()

    def should_abort(self, total_size: int, out_time: float) -> bool:
        """Early abort for the CRF attempt when it will clearly be too big."""
        if self._state != "crf":
            return False
        hard = self.plan.limits.hard_limit
        if total_size >= hard:
            return True
        dur = self.plan.info.duration
        if out_time >= max(5.0, 0.10 * dur) and total_size > 0:
            return total_size * dur / out_time > hard * 1.15
        return False

    def next_step(self, r: StepResult) -> Step | Outcome:
        st = self._state
        if st == "crf":
            if r.aborted:
                self.notes.append("quality-mode encode would exceed the size limit")
                self._remove_tmp_out()
                return self._pass1_step()
            if r.exit_code != 0:
                return Outcome(False, f"ffmpeg failed: {_tail(r.stderr)}")
            size = self._size()
            if size < self.plan.limits.hard_limit:
                self._method = f"CRF {CRF}"
                return self._probe_step()
            self.notes.append(f"quality-mode result {size / 1e6:.1f} MB too large")
            self._remove_tmp_out()
            return self._pass1_step()
        if st == "pass1":
            if r.exit_code != 0:
                return Outcome(False, f"ffmpeg pass 1 failed: {_tail(r.stderr)}")
            return self._pass2_step()
        if st == "pass2":
            if r.exit_code != 0:
                return Outcome(False, f"ffmpeg pass 2 failed: {_tail(r.stderr)}")
            size = self._size()
            lim = self.plan.limits
            if size >= lim.hard_limit:
                if self._retries >= MAX_RETRIES:
                    return Outcome(False, f"could not get below {lim.hard_limit / 1e6:.0f} MB "
                                          f"(last attempt {size / 1e6:.1f} MB)")
                new_rate = int(self._bitrate * (lim.target / size) * 0.97)
                if new_rate < MIN_VIDEO_BPS:
                    return Outcome(False, f"cannot get below {lim.hard_limit / 1e6:.0f} MB: at "
                                          f"{self._bitrate / 1e6:.2f} Mb/s the file is still "
                                          f"{size / 1e6:.1f} MB (content too complex for the limit)")
                self._retries += 1
                self._bitrate = new_rate
                self._remove_tmp_out()
                return self._pass2_step()
            self._method = f"2-pass {self._bitrate / 1e6:.2f} Mb/s"
            return self._probe_step()
        if st == "probe":
            if r.exit_code != 0:
                return Outcome(False, f"validation failed: ffprobe could not read output: {_tail(r.stderr)}")
            try:
                data = json.loads(r.stdout or "{}")
            except json.JSONDecodeError:
                return Outcome(False, "validation failed: invalid ffprobe output")
            size = self._size()
            errors = validate_output(data, self.plan, size, self.tmp_out)
            if errors:
                return Outcome(False, "validation failed: " + "; ".join(errors))
            return self._finalize(size)
        raise RuntimeError(f"unexpected state {st}")

    def _size(self) -> int:
        try:
            return os.path.getsize(self.tmp_out)
        except OSError:
            return 0

    def _finalize(self, size: int) -> Outcome:
        from .files import unique_output_path
        final = self.final_path
        if os.path.lexists(final) and not self.may_overwrite:
            final = unique_output_path(os.path.dirname(final), self.plan.source, set())
        try:
            os.makedirs(os.path.dirname(final), exist_ok=True)
            os.replace(self.tmp_out, final)
        except OSError as e:
            return Outcome(False, f"could not move output into place: {e}")
        set_file_times(self.plan.source, final, self.plan.info.creation_time)
        self._state = "done"
        return Outcome(True, "ok", final_path=final, size=size, method=self._method)


def recording_timestamp(creation_time: str | None) -> float | None:
    """Unix time of an MP4 ``creation_time`` (always UTC), or None if unusable."""
    import datetime as dt
    m = re.match(r"(\d{4}-\d\d-\d\d)[T ](\d\d:\d\d:\d\d)", creation_time or "")
    if not m:
        return None
    try:
        t = dt.datetime.strptime(f"{m.group(1)}T{m.group(2)}", "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    ts = t.replace(tzinfo=dt.timezone.utc).timestamp()
    # 1904/1970 "zero" dates from cameras without a clock are not real dates
    if t.year < 1980 or ts > dt.datetime.now(dt.timezone.utc).timestamp() + 86400:
        return None
    return ts


def set_file_times(src: str, dst: str, creation_time: str | None) -> None:
    """Set the export's file date to the recording time (as phones do), or
    to the original's file date when the video has no recording time.
    File managers and many photo apps sort by this date."""
    try:
        ts = recording_timestamp(creation_time)
        if ts is not None:
            os.utime(dst, (ts, ts))
        else:
            st = os.stat(src)
            os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
    except OSError:
        pass


def cleanup_dir(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


# --------------------------------------------------------------------------
# Blocking runner (tests / debugging)
# --------------------------------------------------------------------------

def run_session_blocking(session, on_progress=None) -> Outcome:
    step = session.first_step()
    while isinstance(step, Step):
        step = session.next_step(_run_step_blocking(session, step, on_progress))
    return step


def _run_step_blocking(session: EncodeSession, step: Step, on_progress) -> StepResult:
    if step.kind != "ffmpeg":  # probe / helper tools: capture their output
        p = subprocess.run(step.args, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        return StepResult(p.returncode, p.stdout, p.stderr)
    parser = ProgressParser()
    aborted = False
    with tempfile.TemporaryFile(mode="w+") as err:
        proc = subprocess.Popen(step.args, stdout=subprocess.PIPE, stderr=err,
                                stdin=subprocess.DEVNULL, text=True)
        assert proc.stdout is not None
        for line in proc.stdout:
            if parser.feed(line):
                if on_progress:
                    on_progress(step, parser)
                if step.watch_size and session.should_abort(parser.total_size, parser.out_time):
                    aborted = True
                    proc.kill()
                    break
        proc.wait()
        err.seek(0)
        stderr = err.read()
    return StepResult(proc.returncode, "", stderr, aborted=aborted)


def format_duration(seconds: float) -> str:
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
