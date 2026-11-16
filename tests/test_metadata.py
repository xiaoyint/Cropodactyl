"""Recording metadata (date, geotag, device tags, file time) survives export."""
import json
import os
import subprocess

import pytest

from conftest import make_video, needs_ffmpeg
from videocrop import encoding as enc
from videocrop import geometry as g
from videocrop import mp4meta
from videocrop.probe import probe

pytestmark = [needs_ffmpeg, pytest.mark.ffmpeg]

LOC = "+35.6738+139.7017/"


def tags(path):
    p = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", path],
                       capture_output=True, text=True, check=True)
    return json.loads(p.stdout)["format"].get("tags", {})


def frame_count(path):
    p = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                        "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", path],
                       capture_output=True, text=True, check=True)
    return int(p.stdout.strip())


def test_normalize_iso6709():
    assert mp4meta.normalize_iso6709("+35.6738+139.7017") == LOC
    assert mp4meta.normalize_iso6709("+48.15+011.58+520.000/") == "+48.15+011.58+520.000/"
    assert mp4meta.normalize_iso6709("somewhere") is None
    assert mp4meta.normalize_iso6709(None) is None


@pytest.mark.parametrize("faststart", [True, False])
def test_set_location_keeps_file_valid(tmp_path, faststart):
    src = tmp_path / "a.mp4"
    flags = ["-movflags", "+faststart"] if faststart else []
    make_video(src, 320, 200, duration=1, extra_out=flags)
    n = frame_count(str(src))
    mp4meta.set_location(str(src), LOC)
    assert mp4meta.read_location(str(src)) == LOC
    mp4meta.set_location(str(src), "+1.0+2.0/")  # replaces, does not duplicate
    assert mp4meta.read_location(str(src)) == "+1.0+2.0/"
    assert frame_count(str(src)) == n  # chunk offsets still correct
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", str(src), "-f", "null", "-"],
                       capture_output=True, text=True)
    assert p.returncode == 0 and not p.stderr.strip()
    assert not list(tmp_path.glob("*.tmp"))


def _export(caps, tmp_path, src):
    info = probe(src)
    plan = enc.make_plan(info, g.initial_crop(info.display_w, info.display_h), caps)
    work = tmp_path / "work"
    work.mkdir()
    out = enc.run_session_blocking(enc.EncodeSession(plan, caps, str(work), str(tmp_path / "out" / "x.mp4")))
    assert out.success, out.message
    return out.final_path


def test_android_style_metadata_survives(caps, tmp_path):
    src = tmp_path / "PXL_1.mp4"
    make_video(src, 640, 360, duration=1, extra_out=[
        "-movflags", "use_metadata_tags",
        "-metadata", "creation_time=2026-09-21T23:47:27.000000Z",
        "-metadata", "com.android.model=Pixel 8 Pro",
        "-metadata", "com.android.manufacturer=Google"])
    mp4meta.set_location(str(src), LOC)  # how Android stores the geotag
    os.utime(src, (1_700_000_000, 1_700_000_000))
    out = _export(caps, tmp_path, str(src))
    t = tags(out)
    assert t["creation_time"].startswith("2026-09-21T23:47:27")
    assert t["com.android.model"] == "Pixel 8 Pro"
    assert t["com.android.manufacturer"] == "Google"
    assert "major_brand" not in t or t["major_brand"] != "qt  "
    assert mp4meta.read_location(out) == LOC
    # file date = recording time (UTC), not the copy date of the original
    assert int(os.stat(out).st_mtime) == int(enc.recording_timestamp("2026-09-21T23:47:27Z"))


def test_file_date_falls_back_to_original_without_recording_time(caps, tmp_path):
    src = tmp_path / "plain.mp4"
    make_video(src, 320, 200, duration=1, extra_out=["-fflags", "+bitexact"])
    os.utime(src, (1_600_000_000, 1_600_000_000))
    info = probe(str(src))
    assert enc.recording_timestamp(info.creation_time) is None
    out = _export(caps, tmp_path, str(src))
    assert int(os.stat(out).st_mtime) == 1_600_000_000


def test_recording_timestamp_parsing():
    assert enc.recording_timestamp("2026-09-17T11:02:26.000000Z") == 1789642946.0
    assert enc.recording_timestamp("2026-09-17 11:02:26") == 1789642946.0
    assert enc.recording_timestamp("1904-01-01T00:00:00Z") is None
    assert enc.recording_timestamp("garbage") is None
    assert enc.recording_timestamp(None) is None


def test_iphone_style_metadata_survives(caps, tmp_path):
    src = tmp_path / "IMG_0001.mov"
    make_video(src, 640, 360, duration=1, extra_out=[
        "-movflags", "use_metadata_tags",
        "-metadata", "creation_time=2024-05-01T08:11:12Z",
        "-metadata", "com.apple.quicktime.creationdate=2024-05-01T10:11:12+0200",
        "-metadata", "com.apple.quicktime.location.ISO6709=+48.1500+011.5800+520.000/",
        "-metadata", "com.apple.quicktime.make=Apple",
        "-metadata", "com.apple.quicktime.model=iPhone 15"])
    out = _export(caps, tmp_path, str(src))
    t = tags(out)
    assert t["creation_time"].startswith("2024-05-01T08:11:12")
    assert t["com.apple.quicktime.creationdate"] == "2024-05-01T10:11:12+0200"
    assert t["com.apple.quicktime.location.ISO6709"] == "+48.1500+011.5800+520.000/"
    assert t["com.apple.quicktime.model"] == "iPhone 15"
    assert mp4meta.read_location(out) == "+48.1500+011.5800+520.000/"  # also as ©xyz


def test_validation_catches_lost_metadata(caps, tmp_path):
    src = tmp_path / "s.mp4"
    make_video(src, 320, 200, duration=1, extra_out=["-metadata", "creation_time=2020-01-02T03:04:05Z"])
    mp4meta.set_location(str(src), LOC)
    info = probe(str(src))
    bare = tmp_path / "bare.mp4"
    make_video(bare, 320, 200, duration=1)
    data = {"format": {"tags": tags(str(bare))}}
    errs = enc.metadata_errors(data, info, str(bare))
    assert any("recording date" in e for e in errs) and any("geotag" in e for e in errs)
