"""Uncropped export for skipped files.

A skipped file is not cropped, but it still ends up in the output folder in a
form the photo frame accepts:

* Videos that already meet the frame's rules (H.264 .mp4, at most 1080p,
  < 125 MB, AAC or no audio, no rotation metadata) and photos that are at most
  8K and < 125 MB are **copied unchanged** (bit-identical, all metadata).
* Anything else is re-encoded as a whole frame: videos with the usual
  size-limited H.264 pipeline, scaled down to fit 1080p; photos downscaled to
  fit 8K (and further only if still >= 125 MB), date and GPS kept.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

from . import encoding, photo
from .encoding import Limits, Outcome, Step
from .geometry import SourceCrop

_COPY = "import shutil, sys; shutil.copy2(sys.argv[1], sys.argv[2])"


@dataclass
class CopyPlan:
    """Copy the source unchanged (same interface as the encode plans)."""

    source: str
    info: object  # VideoInfo or PhotoInfo
    limits: Limits = field(default_factory=Limits)
    warnings: list[str] = field(default_factory=list)
    crop: None = None
    scaled: bool = False

    @property
    def src_crop(self) -> SourceCrop:
        return SourceCrop(0, 0, self.info.display_w, self.info.display_h)

    @property
    def out_w(self) -> int:
        return self.info.display_w

    @property
    def out_h(self) -> int:
        return self.info.display_h

    @property
    def output_ext(self) -> str:
        return os.path.splitext(self.source)[1] or ".mp4"

    def make_session(self, caps, tmp_dir: str, final_path: str, may_overwrite: bool = False):
        return CopySession(self, tmp_dir, final_path, may_overwrite)


class CopySession:
    def __init__(self, plan: CopyPlan, tmp_dir: str, final_path: str, may_overwrite: bool = False):
        self.plan = plan
        self.tmp_dir = tmp_dir
        self.tmp_out = os.path.join(tmp_dir, "output" + plan.output_ext.lower())
        self.final_path = final_path
        self.may_overwrite = may_overwrite
        self.notes: list[str] = []

    def should_abort(self, total_size: int, out_time: float) -> bool:
        return False

    def first_step(self) -> Step | Outcome:
        if not os.path.isfile(self.plan.source):
            return Outcome(False, "source file no longer exists")
        size = os.path.getsize(self.plan.source)
        if encoding.disk_free(self.tmp_dir) < size + encoding.DISK_RESERVE_BYTES:
            return Outcome(False, "Insufficient disk space in output folder")
        # A separate process, so copying a large video never blocks the GUI.
        return Step("tool", [sys.executable, "-c", _COPY, self.plan.source, self.tmp_out],
                    "Copying (already compatible)", 0.0, 1.0, nice=False)

    def next_step(self, r) -> Step | Outcome:
        if r.exit_code != 0:
            return Outcome(False, f"copy failed: {encoding._tail(r.stderr)}")
        try:
            size = os.path.getsize(self.tmp_out)
            src_size = os.path.getsize(self.plan.source)
        except OSError as e:
            return Outcome(False, f"copy failed: {e}")
        if size != src_size:
            return Outcome(False, f"copy is incomplete ({size} of {src_size} bytes)")
        return self._finalize(size)

    def _finalize(self, size: int) -> Outcome:
        from .files import unique_output_path
        final = self.final_path
        if os.path.lexists(final) and not self.may_overwrite:
            final = unique_output_path(os.path.dirname(final), self.plan.source, set(),
                                       ext=self.plan.output_ext)
        try:
            os.makedirs(os.path.dirname(final), exist_ok=True)
            os.replace(self.tmp_out, final)
        except OSError as e:
            return Outcome(False, f"could not move output into place: {e}")
        if isinstance(self.plan.info, photo.PhotoInfo):
            photo.set_photo_file_time(self.plan, final)
        else:
            encoding.set_file_times(self.plan.source, final, self.plan.info.creation_time)
        return Outcome(True, "ok", final_path=final, size=size, method="copied unchanged")


def make_uncropped_plan(info, caps, limits: Limits = Limits()):
    """Plan for a skipped file: copy if compatible, else re-encode uncropped.

    Returns ``(plan, reasons)``; *reasons* says why a re-encode is needed and
    is empty for a plain copy.  Raises :class:`encoding.PlanError` if a video
    cannot fit the size limit.
    """
    if isinstance(info, photo.PhotoInfo):
        reasons = photo.photo_conversion_reasons(info, limits)
        if not reasons:
            return CopyPlan(info.path, info, limits), []
        return photo.make_photo_full_plan(info, caps, limits), reasons
    reasons = encoding.video_conversion_reasons(info, limits)
    if not reasons:
        return CopyPlan(info.path, info, limits), []
    return encoding.make_full_plan(info, caps, limits), reasons
