"""Input scanning and output naming."""
from __future__ import annotations

import os
import re

VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg"}
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS
TEMP_DIR_NAME = ".videocrop-tmp"


def natural_key(name: str):
    """Sort 'clip2' before 'clip10' (like file managers do)."""
    return [int(t) if t.isdigit() else t.casefold() for t in re.split(r"(\d+)", name)]


def scan_videos(directory: str) -> list[str]:
    """Video and JPEG files directly inside *directory*, in natural filename order."""
    out = []
    with os.scandir(directory) as it:
        for entry in it:
            if entry.name.startswith("."):
                continue
            if os.path.splitext(entry.name)[1].lower() not in MEDIA_EXTENSIONS:
                continue
            try:
                if not entry.is_file():
                    continue
            except OSError:
                continue
            out.append(os.path.abspath(entry.path))
    out.sort(key=lambda p: natural_key(os.path.basename(p)))
    return out


def output_name_candidates(stem: str, ext: str = ".mp4"):
    yield f"{stem}{ext}"
    n = 1
    while True:
        yield f"{stem} ({n}){ext}"
        n += 1


def unique_output_path(out_dir: str, source: str, taken: set[str], ext: str = ".mp4") -> str:
    """First free ``<stem><ext>`` / ``<stem> (n)<ext>`` in *out_dir*.

    A path is free if no file exists there and it is not in *taken*
    (outputs reserved by queued jobs).  Existing files – including the
    originals when the output directory equals the input directory – are
    therefore never overwritten.
    """
    stem = os.path.splitext(os.path.basename(source))[0] or "video"
    for name in output_name_candidates(stem, ext):
        cand = os.path.join(out_dir, name)
        if cand not in taken and not os.path.lexists(cand):
            return cand
    raise AssertionError("unreachable")
