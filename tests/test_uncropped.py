"""Skip = no crop: whole-frame export, copied unchanged when already compatible."""
import filecmp
import os

import pytest
from conftest import make_video, needs_ffmpeg

from videocrop import encoding as enc
from videocrop import files, passthrough
from videocrop import geometry as g
from videocrop.probe import probe


# ----------------------------------------------------------------- geometry

@pytest.mark.parametrize("dw, dh, want", [
    (3840, 2160, (1920, 1080)),   # 4K landscape -> 1080p
    (2160, 3840, (1080, 1920)),   # 4K portrait  -> 1080p portrait
    (1920, 1080, (1920, 1080)),   # already 1080p
    (1280, 720, (1280, 720)),     # never upscaled
    (1081, 609, (1080, 608)),     # odd sizes rounded down to even
    (2560, 1920, (1440, 1080)),   # 4:3
])
def test_fit_size_video(dw, dh, want):
    assert g.fit_size(dw, dh) == want
    assert g.fits_box(*want)


def test_fit_size_photo_8k():
    assert g.fit_size(8160, 6144, g.PHOTO_BOX, even=False) == (7680, 5782)
    assert g.fit_size(4000, 3000, g.PHOTO_BOX, even=False) == (4000, 3000)
    assert g.fits_box(1080, 1920) and not g.fits_box(1200, 1920)


# ----------------------------------------------------------------- videos

@needs_ffmpeg
def test_compatible_video_is_copied_unchanged(tmp_path, caps):
    src = make_video(tmp_path / "ok.mp4", 960, 540, duration=1)
    info = probe(src)
    assert enc.video_conversion_reasons(info) == []
    plan, reasons = passthrough.make_uncropped_plan(info, caps)
    assert isinstance(plan, passthrough.CopyPlan) and reasons == []
    work = tmp_path / "work"; work.mkdir()
    final = tmp_path / "out" / "ok.mp4"
    out = enc.run_session_blocking(plan.make_session(caps, str(work), str(final)))
    assert out.success, out.message
    assert out.method == "copied unchanged"
    assert filecmp.cmp(src, final, shallow=False)


@needs_ffmpeg
@pytest.mark.parametrize("name, w, h, rotation, want", [
    ("big.mp4", 2560, 1440, None, (1920, 1080)),       # too large: scaled to 1080p
    ("tall.mp4", 1440, 2560, None, (1080, 1920)),      # portrait stays portrait
    ("rot.mp4", 640, 360, 90, (360, 640)),             # rotation baked into pixels
    ("clip.mkv", 640, 360, None, (640, 360)),          # wrong container
])
def test_incompatible_video_is_converted_uncropped(tmp_path, caps, name, w, h, rotation, want):
    src = make_video(tmp_path / name, w, h, duration=1, rotation=rotation)
    info = probe(src)
    plan, reasons = passthrough.make_uncropped_plan(info, caps)
    assert reasons and plan.crop is None
    assert (plan.out_w, plan.out_h) == want
    work = tmp_path / "work"; work.mkdir()
    final = tmp_path / "out" / (os.path.splitext(name)[0] + ".mp4")
    out = enc.run_session_blocking(plan.make_session(caps, str(work), str(final)))
    assert out.success, out.message
    res = probe(str(final))
    assert (res.display_w, res.display_h) == want
    assert res.codec == "h264" and res.rotation == 0
    assert enc.video_conversion_reasons(res) == []  # now frame-compatible


# ----------------------------------------------------------------- photos

PIL = pytest.importorskip("PIL")
from test_photo import TAKEN, caps as photo_caps, make_photo  # noqa: E402
from videocrop import photo  # noqa: E402


def _run_photo(tmp_path, src, limits=enc.Limits()):
    info = photo.probe_photo(src)
    plan, reasons = passthrough.make_uncropped_plan(info, photo_caps(), limits)
    work = tmp_path / "work"; work.mkdir(exist_ok=True)
    final = tmp_path / "out" / os.path.basename(src)
    out = enc.run_session_blocking(plan.make_session(photo_caps(), str(work), str(final)))
    assert out.success, out.message
    return plan, reasons, out, str(final)


def test_normal_photo_is_copied_unchanged(tmp_path):
    src = make_photo(tmp_path / "p.jpg", 1200, 800)
    plan, reasons, out, final = _run_photo(tmp_path, src)
    assert isinstance(plan, passthrough.CopyPlan) and not reasons
    assert filecmp.cmp(src, final, shallow=False)


def test_photo_over_8k_is_downscaled_whole(tmp_path):
    # stored 4000x7700 with orientation 6 -> displayed 7700x4000 (over 8K)
    src = make_photo(tmp_path / "big.jpg", 4000, 7700, orientation=6)
    plan, reasons, out, final = _run_photo(tmp_path, src)
    assert reasons and plan.crop is None and plan.scaled
    meta = photo.read_exif_summary(final)
    assert meta["size"] == (3989, 7680)          # stored size, displayed 7680x3989
    assert meta["orientation"] == 6
    assert meta["taken"] == TAKEN and meta["gps_raw"] == photo.read_exif_summary(src)["gps_raw"]
    assert meta["gps_raw"]


def test_photo_over_size_limit_is_shrunk(tmp_path):
    src = make_photo(tmp_path / "heavy.jpg", 1600, 1000, quality=98)
    size = os.path.getsize(src)
    limits = enc.Limits(hard_limit=size // 2, target=int(size * 0.4))
    plan, reasons, out, final = _run_photo(tmp_path, src, limits)
    assert reasons and plan.crop is None
    assert os.path.getsize(final) < limits.hard_limit
    w, h = photo.read_exif_summary(final)["size"]
    assert g.same_aspect(w, h, 1600, 1000)


# ----------------------------------------------------------------- GUI

@needs_ffmpeg
def test_skip_exports_uncropped_once(tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication
    from test_gui import wait_until
    from videocrop.mainwindow import MainWindow
    QApplication.instance() or QApplication([])
    caps = enc.detect_caps()
    inp = tmp_path / "in"; inp.mkdir()
    make_video(inp / "a.mp4", 960, 540, duration=1)           # compatible -> copy
    make_video(inp / "b.mkv", 640, 360, duration=1)           # -> converted to mp4
    make_photo(inp / "c.jpg", 1200, 800)                      # -> copy
    out = tmp_path / "out"; out.mkdir()
    monkeypatch.setattr(MainWindow, "_show_summary", lambda self: None)
    w = MainWindow(caps, str(inp), str(out), files.scan_videos(str(inp)))
    w.show()
    wait_until(lambda: w.view.has_video, 10)
    w.skip(); w.skip(); w.skip()
    assert w.index == 3 and len(w.queue.jobs) == 3
    w.prev_video()
    w.skip()                       # already reviewed: only moves on
    assert len(w.queue.jobs) == 3
    wait_until(lambda: not w.queue.is_busy(), 60)
    assert sorted(os.listdir(out)) == ["a.mp4", "b.mp4", "c.jpg"], [j.message for j in w.queue.jobs]
    assert filecmp.cmp(inp / "a.mp4", out / "a.mp4", shallow=False)
    assert filecmp.cmp(inp / "c.jpg", out / "c.jpg", shallow=False)
    assert all(j.plan.crop is None for j in w.queue.jobs)
    assert "(uncropped)" in w.job_list.item(0).text()
    w._force_close = True
    w.close()
