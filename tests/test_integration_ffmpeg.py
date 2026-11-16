"""End-to-end export tests with the real ffmpeg (synthetic source videos)."""
import json
import os
import subprocess

import pytest

from conftest import make_video, needs_ffmpeg
from videocrop import encoding as enc
from videocrop import geometry as g
from videocrop.geometry import Crop
from videocrop.probe import probe

pytestmark = [needs_ffmpeg, pytest.mark.ffmpeg]


def ffprobe_json(path):
    p = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
                        "-show_streams", path], capture_output=True, text=True, check=True)
    return json.loads(p.stdout)


def mean_rgb(path, crop=None, frame_t=0.5):
    """Mean RGB of one (auto-rotated) frame, optionally of a sub-rectangle."""
    vf = f"crop={crop.w}:{crop.h}:{crop.x}:{crop.y}," if crop else ""
    vf += "scale=32:20:flags=area,format=rgb24"
    p = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(frame_t), "-i", path, "-frames:v", "1",
                        "-vf", vf, "-f", "rawvideo", "-"], capture_output=True, check=True)
    data = p.stdout
    n = len(data) // 3
    return tuple(sum(data[i::3]) / n for i in range(3))


def export(caps, tmp_path, src, crop=None, limits=enc.Limits()):
    info = probe(src)
    crop = crop or g.initial_crop(info.display_w, info.display_h)
    plan = enc.make_plan(info, crop, caps, limits)
    tmp = tmp_path / "work"
    tmp.mkdir(exist_ok=True)
    final = tmp_path / "out" / (os.path.splitext(os.path.basename(src))[0] + ".mp4")
    session = enc.EncodeSession(plan, caps, str(tmp), str(final))
    outcome = enc.run_session_blocking(session)
    assert outcome.success, outcome.message
    assert not os.path.exists(session.tmp_out), "temp file must be moved away"
    return info, plan, outcome, session


def check_output(path, plan, n_audio):
    data = ffprobe_json(path)
    v = [s for s in data["streams"] if s["codec_type"] == "video"]
    a = [s for s in data["streams"] if s["codec_type"] == "audio"]
    assert "mp4" in data["format"]["format_name"]
    assert len(v) == 1 and v[0]["codec_name"] == "h264"
    w, h = v[0]["width"], v[0]["height"]
    assert (w, h) == (plan.out_w, plan.out_h)
    assert w * 10 == h * 16 and w <= 1728 and h <= 1080
    assert len(a) == n_audio and all(s["codec_name"] == "aac" for s in a)
    assert os.path.getsize(path) < enc.HARD_LIMIT_BYTES
    return data


def test_landscape_crop_content_and_constraints(caps, tmp_path):
    src = make_video(tmp_path / "land scape #1.mp4", 1920, 1080, duration=2)
    info = probe(src)
    crop = Crop(1920 - 800, 1080 - 500, 50)  # bottom-right quadrant only (white)
    _, plan, out, session = export(caps, tmp_path, src, crop)
    assert not plan.scaled and out.method.startswith("CRF")
    data = check_output(out.final_path, plan, 1)
    assert abs(float(data["format"]["duration"]) - info.duration) < 0.2
    r, gr, b = mean_rgb(out.final_path)
    assert min(r, gr, b) > 200  # white => the selected region was exported
    # original untouched
    assert os.path.getsize(src) == info.size_bytes


def test_frame_rate_is_preserved(caps, tmp_path):
    src = make_video(tmp_path / "ntsc.mp4", 640, 360, duration=2, fps="30000/1001")
    _, plan, out, _ = export(caps, tmp_path, src)
    v = [s for s in ffprobe_json(out.final_path)["streams"] if s["codec_type"] == "video"][0]
    assert v["avg_frame_rate"] in ("30000/1001", "2997/100") or \
        abs(eval(v["avg_frame_rate"]) - 29.97) < 0.05


def test_4k_downscale(caps, tmp_path):
    src = make_video(tmp_path / "uhd.mp4", 3840, 2160, duration=1)
    _, plan, out, _ = export(caps, tmp_path, src)
    assert plan.scaled
    check_output(out.final_path, plan, 1)
    assert (plan.out_w, plan.out_h) == (1728, 1080)


def test_rotated_portrait_crop_uses_display_orientation(caps, tmp_path):
    src = make_video(tmp_path / "rot.mp4", 1280, 720, duration=1, rotation=90)
    info = probe(src)
    if info.rotation == 0:
        pytest.skip("this ffmpeg cannot write rotation metadata")
    assert info.is_portrait and (info.display_w, info.display_h) == (720, 1280)
    crop = Crop(0, 0, 20)  # 320x200 at the top-left of the *displayed* frame
    _, plan, out, _ = export(caps, tmp_path, src, crop)
    data = check_output(out.final_path, plan, 1)
    v = [s for s in data["streams"] if s["codec_type"] == "video"][0]
    assert not any("rotation" in sd for sd in v.get("side_data_list", []))
    expected = mean_rgb(src, plan.src_crop)  # ffmpeg auto-rotates the source frame too
    got = mean_rgb(out.final_path)
    assert all(abs(e - x) < 20 for e, x in zip(expected, got)), (expected, got)


def test_non_square_pixels(caps, tmp_path):
    src = make_video(tmp_path / "anamorphic.mp4", 720, 480, duration=1, sar="32/27")
    info = probe(src)
    assert (info.display_w, info.display_h) == (853, 480)
    _, plan, out, _ = export(caps, tmp_path, src)
    data = check_output(out.final_path, plan, 1)
    v = [s for s in data["streams"] if s["codec_type"] == "video"][0]
    assert v.get("sample_aspect_ratio") in (None, "1:1")
    assert (plan.out_w, plan.out_h) == (768, 480)


def test_no_audio(caps, tmp_path):
    src = make_video(tmp_path / "silent.mp4", 640, 400, duration=1, audio=0)
    _, plan, out, _ = export(caps, tmp_path, src)
    check_output(out.final_path, plan, 0)


def test_multiple_audio_streams(caps, tmp_path):
    src = make_video(tmp_path / "dual.mp4", 640, 400, duration=1, audio=2)
    _, plan, out, _ = export(caps, tmp_path, src)
    assert len(plan.audio) == 2
    check_output(out.final_path, plan, 2)


def test_hdr_hlg_source_is_tonemapped(caps, tmp_path):
    if not caps.can_tonemap:
        pytest.skip("ffmpeg lacks zscale/tonemap")
    src = make_video(tmp_path / "hlg.mp4", 1280, 720, duration=1, hdr=True)
    info = probe(src)
    assert info.hdr == "hlg"
    _, plan, out, _ = export(caps, tmp_path, src)
    data = check_output(out.final_path, plan, 1)
    v = [s for s in data["streams"] if s["codec_type"] == "video"][0]
    assert v["pix_fmt"] == "yuv420p" and v.get("color_transfer") == "bt709"


def test_size_limit_forces_two_pass_and_is_respected(caps, tmp_path):
    """Use a tiny size limit so the CRF attempt is too big and 2-pass kicks in."""
    src = make_video(tmp_path / "noisy.mp4", 1280, 800, duration=6, noisy=True)
    limits = enc.Limits(hard_limit=1_500_000, target=1_400_000)
    _, plan, out, session = export(caps, tmp_path, src, limits=limits)
    assert out.method.startswith("2-pass"), (out.method, session.notes)
    assert out.size < limits.hard_limit
    assert os.path.getsize(out.final_path) == out.size


def test_corrupt_file_is_reported(tmp_path):
    from videocrop.probe import ProbeError
    bad = tmp_path / "broken.mp4"
    bad.write_bytes(b"\x00\x01garbage" * 100)
    with pytest.raises(ProbeError):
        probe(str(bad))


def test_square_export_from_4k_is_1080_square(caps, tmp_path):
    src = make_video(tmp_path / "sq.mp4", 2560, 1440, duration=1)
    info = probe(str(src))
    crop = g.initial_crop(info.display_w, info.display_h, g.SQUARE)
    _, plan, out, _ = export(caps, tmp_path, str(src), crop)
    v = [s for s in ffprobe_json(out.final_path)["streams"] if s["codec_type"] == "video"][0]
    assert (v["width"], v["height"]) == (plan.out_w, plan.out_h) == (1080, 1080)
    assert plan.scaled
