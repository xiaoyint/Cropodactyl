"""Qt tests (offscreen): crop overlay interaction, export queue, main window flow."""
import os
import time

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QEventLoop, QPoint, QPointF, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QWheelEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from conftest import make_video, needs_ffmpeg  # noqa: E402
from videocrop import encoding as enc  # noqa: E402
from videocrop import files  # noqa: E402
from videocrop import geometry as g  # noqa: E402
from videocrop.probe import probe  # noqa: E402

pytestmark = pytest.mark.gui


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def wait_until(cond, timeout=120.0):
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise TimeoutError
        QApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
        time.sleep(0.01)


# ----------------------------------------------------------------- crop overlay

@pytest.fixture
def view(app):
    from videocrop.view import VideoView
    v = VideoView()
    v.resize(1000, 600)
    v.show()
    QApplication.processEvents()
    v.set_video(1920, 1080, g.initial_crop(1920, 1080))
    QApplication.processEvents()
    yield v
    v.close()


def widget_pt(view, x, y):
    wx, wy = view._xf.to_widget(x, y)
    return QPoint(round(wx), round(wy))


def drag(widget, a: QPoint, b: QPoint):
    QTest.mousePress(widget, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, a)
    for i in range(1, 6):
        p = a + (b - a) * (i / 5)
        QTest.mouseMove(widget, p)
    QTest.mouseRelease(widget, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, b)


def test_initial_overlay_and_video_item_geometry(view):
    c = view.crop
    assert (c.w, c.h) == (1728, 1080)
    it = view.video_item
    assert it.isVisible()
    assert it.width() / it.height() == pytest.approx(1920 / 1080, rel=1e-3)


def test_drag_moves_crop_and_stays_inside(view):
    ov = view._overlay
    c0 = view.crop
    start = widget_pt(view, c0.cx, c0.cy)
    drag(ov, start, start + QPoint(-400, 0))
    c1 = view.crop
    assert c1.x == 0 and c1.k == c0.k  # clamped to left edge, size unchanged


def test_corner_resize_keeps_aspect_and_anchor(view):
    ov = view._overlay
    c0 = view.crop
    br = widget_pt(view, c0.right, c0.bottom)
    drag(ov, br, br + QPoint(-300, -100))
    c1 = view.crop
    assert c1.k < c0.k
    assert (c1.x, c1.y) == (c0.x, c0.y)  # top-left anchor fixed
    assert c1.w * 10 == c1.h * 16


def test_escape_restores_previous_crop(view):
    ov = view._overlay
    c0 = view.crop
    tl = widget_pt(view, c0.x, c0.y)
    drag(ov, tl, tl + QPoint(200, 200))
    assert view.crop != c0
    assert view.cancel_adjustment()
    assert view.crop == c0


def test_wheel_zoom_and_reset(view):
    ov = view._overlay
    c0 = view.crop
    pos = QPointF(widget_pt(view, c0.cx, c0.cy))
    ev = QWheelEvent(pos, ov.mapToGlobal(pos), QPoint(0, 0), QPoint(0, 120),
                     Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                     Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(ov, ev)
    assert view.crop.k < c0.k  # wheel up = zoom in = smaller crop
    view.reset_crop()
    assert view.crop == g.initial_crop(1920, 1080)


def test_preview_mode_maps_crop_to_view(view):
    view.set_crop(g.Crop(100, 100, 40))
    view.set_preview(True)
    it = view.video_item
    # the crop region must fill the view (minus margin) and be centred
    x0 = it.x() + 100 * view._xf.scale
    x1 = it.x() + (100 + 640) * view._xf.scale
    assert x0 == pytest.approx(view.width() - x1, abs=1)
    view.set_preview(False)
    assert it.width() < view.width()


# ----------------------------------------------------------------- export queue

@needs_ffmpeg
def test_queue_exports_in_background_and_cleans_up(app, tmp_path):
    from videocrop.exportqueue import DONE, EncodeQueue
    caps = enc.detect_caps()
    out = tmp_path / "out"
    out.mkdir()
    srcs = [make_video(tmp_path / f"clip {i}.mp4", 800, 600, duration=1) for i in range(2)]
    q = EncodeQueue(caps, str(out))
    for s in srcs:
        info = probe(s)
        q.enqueue(enc.make_plan(info, g.initial_crop(info.display_w, info.display_h), caps))
    assert q.is_busy()  # enqueue returned immediately
    wait_until(lambda: not q.is_busy())
    assert [j.state for j in q.jobs] == [DONE, DONE], [j.message for j in q.jobs]
    assert sorted(os.listdir(out)) == ["clip 0.mp4", "clip 1.mp4"]  # no temp leftovers
    # re-saving the same source overwrites only its own earlier output
    info = probe(srcs[0])
    q.enqueue(enc.make_plan(info, g.Crop(0, 0, 20), caps))
    wait_until(lambda: not q.is_busy())
    assert q.jobs[-1].final_path == str(out / "clip 0.mp4")
    assert sorted(os.listdir(out)) == ["clip 0.mp4", "clip 1.mp4"]


@needs_ffmpeg
def test_queue_name_collision_and_cancel(app, tmp_path):
    from videocrop.exportqueue import CANCELLED, DONE, EncodeQueue
    caps = enc.detect_caps()
    out = tmp_path / "out"
    out.mkdir()
    (out / "a.mp4").write_text("pre-existing file")
    src = make_video(tmp_path / "a.mp4", 1920, 1080, duration=8, noisy=True)
    src2 = make_video(tmp_path / "b.mp4", 640, 400, duration=1)
    q = EncodeQueue(caps, str(out))
    info, info2 = probe(src), probe(src2)
    j1 = q.enqueue(enc.make_plan(info, g.initial_crop(info.display_w, info.display_h), caps))
    j2 = q.enqueue(enc.make_plan(info2, g.initial_crop(info2.display_w, info2.display_h), caps))
    assert j1.out_path == str(out / "a (1).mp4")
    wait_until(lambda: j1.state == "running" and j1.progress > 0.0, 60)
    q.cancel(j1.id)
    wait_until(lambda: not q.is_busy())
    assert j1.state == CANCELLED and j2.state == DONE
    assert (out / "a.mp4").read_text() == "pre-existing file"
    assert sorted(os.listdir(out)) == ["a.mp4", "b.mp4"]  # cancelled job left nothing


@needs_ffmpeg
def test_abort_all_on_shutdown_removes_temp(app, tmp_path):
    from videocrop.exportqueue import EncodeQueue
    caps = enc.detect_caps()
    out = tmp_path / "out"
    out.mkdir()
    src = make_video(tmp_path / "long.mp4", 1920, 1080, duration=10, noisy=True)
    q = EncodeQueue(caps, str(out))
    info = probe(src)
    q.enqueue(enc.make_plan(info, g.initial_crop(info.display_w, info.display_h), caps))
    wait_until(lambda: q.jobs[0].progress > 0.0, 60)
    q.abort_all()
    assert os.listdir(out) == []


# ----------------------------------------------------------------- main window

@needs_ffmpeg
def test_main_window_review_flow(app, tmp_path, monkeypatch):
    from videocrop.mainwindow import MainWindow
    caps = enc.detect_caps()
    inp = tmp_path / "in"
    inp.mkdir()
    for n in ["v1.mp4", "v2.mp4", "v3.mp4"]:
        make_video(inp / n, 960, 540, duration=1)
    (inp / "broken.mp4").write_bytes(b"not a video")
    out = tmp_path / "out"
    out.mkdir()
    summaries = []
    monkeypatch.setattr(MainWindow, "_show_summary", lambda self: summaries.append(True))
    w = MainWindow(caps, str(inp), str(out), files.scan_videos(str(inp)))
    w.show()
    wait_until(lambda: "broken.mp4" in w.windowTitle(), 10)
    assert os.path.basename(w.files[w.index]) == "broken.mp4"
    assert not w.save_btn.isEnabled()  # unreadable file: only skip allowed
    w.skip()
    assert os.path.basename(w.files[w.index]) == "v1.mp4"
    w.view.set_crop(g.Crop(0, 0, 30))
    w.save_and_next()  # returns immediately
    assert w.index == 2
    w.skip()             # v2 skipped
    w.prev_video()       # back to v2, crop remembered
    assert w.index == 2
    w.save_and_next()    # v2 saved after all
    w.save_and_next()    # v3
    assert w.index == 4  # all reviewed
    wait_until(lambda: not w.queue.is_busy())
    assert sorted(os.listdir(out)) == ["v1.mp4", "v2.mp4", "v3.mp4"]
    v1 = probe(str(out / "v1.mp4"))
    assert (v1.display_w, v1.display_h) == (480, 300)
    assert summaries  # summary shown once everything finished
    w._force_close = True
    w.close()


def test_pid_alive_is_harmless():
    import os
    from videocrop.exportqueue import pid_alive
    assert pid_alive(os.getpid())
    assert not pid_alive(2**22 + 12345)  # far above any real PID


@needs_ffmpeg
def test_main_window_mixed_videos_and_photos(app, tmp_path, monkeypatch):
    pytest.importorskip("PIL")
    from test_photo import make_photo
    from videocrop.mainwindow import MainWindow
    from videocrop import photo
    caps = enc.detect_caps()
    inp = tmp_path / "in"
    inp.mkdir()
    make_video(inp / "a.mp4", 960, 540, duration=1)
    make_photo(inp / "b.jpg", 1200, 800, orientation=6)
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(MainWindow, "_show_summary", lambda self: None)
    w = MainWindow(caps, str(inp), str(out), files.scan_videos(str(inp)))
    w.show()
    wait_until(lambda: w.view.has_video, 10)
    w.save_and_next()                       # video
    assert isinstance(w.info, photo.PhotoInfo)
    assert w.view.image_item.isVisible() and not w.view.video_item.isVisible()
    assert not w.play_btn.isEnabled()       # nothing to play for a photo
    assert w.lbl["dur"].text().startswith("2026:09:25")  # "Taken" row
    w.save_and_next()                       # photo
    wait_until(lambda: not w.queue.is_busy())
    assert sorted(os.listdir(out)) == ["a.mp4", "b.jpg"], [j.message for j in w.queue.jobs]
    shown = photo.read_exif_summary(str(out / "b.jpg"))
    assert shown["orientation"] == 6 and shown["taken"] == "2026:09:25 09:01:46"
    w._force_close = True
    w.close()


def test_square_toggle_and_quit_shortcut(app, tmp_path, monkeypatch):
    pytest.importorskip("PIL")
    from test_photo import make_photo
    from videocrop.mainwindow import MainWindow
    from videocrop import photo
    inp = tmp_path / "in"
    inp.mkdir()
    for n in ("a.jpg", "b.jpg"):
        make_photo(inp / n, 960, 600)
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(MainWindow, "_show_summary", lambda self: None)
    w = MainWindow(enc.detect_caps(), str(inp), str(out), files.scan_videos(str(inp)))
    w.show()
    wait_until(lambda: w.view.has_video, 10)
    assert w.view.crop.aspect == g.WIDE and not w.square_btn.isChecked()
    QTest.keyClick(w, Qt.Key.Key_S)
    assert w.square_btn.isChecked() and (w.view.crop.w, w.view.crop.h) == (600, 600)
    assert "1:1" in w.lbl["out"].text()
    QTest.keyClick(w, Qt.Key.Key_Escape)       # undo the switch -> button follows
    assert w.view.crop.aspect == g.WIDE and not w.square_btn.isChecked()
    w.square_btn.click()
    w.save_and_next()
    assert w.view.crop.aspect == g.SQUARE      # mode sticks for the next file
    w.save_and_next()
    wait_until(lambda: not w.queue.is_busy())
    for n in ("a.jpg", "b.jpg"):
        assert photo.read_exif_summary(str(out / n))["size"] == (600, 600)
    QTest.keyClick(w, Qt.Key.Key_Q, Qt.KeyboardModifier.ControlModifier)
    wait_until(lambda: not w.isVisible(), 5)
