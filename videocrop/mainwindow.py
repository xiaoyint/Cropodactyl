"""Main window: review videos one by one, crop, queue exports."""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QKeySequence, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QProgressBar, QPushButton, QSlider, QStyle, QToolButton, QVBoxLayout, QWidget,
)

from . import encoding, files, passthrough, photo
from . import geometry as g
from .encoding import FFmpegCaps, PlanError, format_duration
from .photo import PhotoInfo
from .probe import ProbeCache, ProbeError, VideoInfo
from .exportqueue import CANCELLED, DONE, FAILED, PENDING, RUNNING, EncodeQueue, Job
from .view import VideoView

APP_NAME = "VideoCrop 16:10"


# ---------------------------------------------------------------- helpers

def _fmt_time(ms: int) -> str:
    s = max(0, ms) / 1000.0
    m, sec = divmod(s, 60)
    h, m = divmod(int(m), 60)
    return f"{h}:{m:02d}:{sec:04.1f}" if h else f"{m}:{sec:04.1f}"


def _no_focus(*widgets):
    for w in widgets:
        w.setFocusPolicy(Qt.FocusPolicy.NoFocus)


class JumpSlider(QSlider):
    """Timeline slider that jumps directly to the clicked position."""

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            val = QStyle.sliderValueFromPosition(self.minimum(), self.maximum(),
                                                 int(e.position().x()), self.width())
            self.setValue(val)
            self.sliderMoved.emit(val)
        super().mousePressEvent(e)


# ---------------------------------------------------------------- start dialog

class StartDialog(QDialog):
    def __init__(self, input_dir: str = "", output_dir: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME} – choose folders")
        self.setMinimumWidth(620)
        self._out_edited = bool(output_dir)
        self.in_edit = QLineEdit(input_dir)
        self.out_edit = QLineEdit(output_dir)
        self.info = QLabel()
        in_btn = QPushButton("Browse…")
        out_btn = QPushButton("Browse…")
        in_btn.clicked.connect(self._browse_in)
        out_btn.clicked.connect(self._browse_out)
        self.in_edit.textChanged.connect(self._in_changed)
        self.out_edit.textEdited.connect(lambda _: setattr(self, "_out_edited", True))

        form = QFormLayout()
        row = QHBoxLayout(); row.addWidget(self.in_edit); row.addWidget(in_btn)
        form.addRow("Input folder:", row)
        row = QHBoxLayout(); row.addWidget(self.out_edit); row.addWidget(out_btn)
        form.addRow("Output folder:", row)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                        | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Start")
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(self.info)
        lay.addWidget(self.buttons)
        self._in_changed(input_dir)

    def _browse_in(self):
        d = QFileDialog.getExistingDirectory(self, "Input folder", self.in_edit.text() or os.path.expanduser("~"))
        if d:
            self.in_edit.setText(d)

    def _browse_out(self):
        start = self.out_edit.text() or self.in_edit.text() or os.path.expanduser("~")
        d = QFileDialog.getExistingDirectory(self, "Output folder", start)
        if d:
            self._out_edited = True
            self.out_edit.setText(d)

    def _in_changed(self, text: str):
        text = text.strip()
        if not self._out_edited:
            self.out_edit.setText(os.path.join(text, "cropped") if text else "")
        ok = False
        if text and os.path.isdir(text):
            try:
                n = len(files.scan_videos(text))
            except OSError as e:
                self.info.setText(f"Cannot read folder: {e}")
            else:
                found = files.scan_videos(text)
                n_photo = sum(1 for p in found if photo.is_photo(p))
                self.info.setText(f"{n - n_photo} video(s) and {n_photo} photo(s) found."
                                  if n else "No videos or JPEG photos in this folder.")
                ok = n > 0
        else:
            self.info.setText("Select the folder that contains your videos.")
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(ok)

    def _accept(self):
        out = self.output_dir
        if os.path.realpath(out) == os.path.realpath(self.input_dir):
            r = QMessageBox.question(self, APP_NAME,
                                     "The output folder is the same as the input folder.\n"
                                     "Originals are never overwritten (outputs get a numbered "
                                     "name), but a separate folder is recommended. Continue?")
            if r != QMessageBox.StandardButton.Yes:
                return
        try:
            os.makedirs(out, exist_ok=True)
        except OSError as e:
            QMessageBox.critical(self, APP_NAME, f"Cannot create output folder:\n{e}")
            return
        if not os.access(out, os.W_OK):
            QMessageBox.critical(self, APP_NAME, "The output folder is not writable.")
            return
        self.accept()

    @property
    def input_dir(self) -> str:
        return os.path.abspath(os.path.expanduser(self.in_edit.text().strip()))

    @property
    def output_dir(self) -> str:
        return os.path.abspath(os.path.expanduser(self.out_edit.text().strip()))


# ---------------------------------------------------------------- main window

class MainWindow(QMainWindow):
    def __init__(self, caps: FFmpegCaps, input_dir: str, output_dir: str, paths: list[str]):
        super().__init__()
        self.caps = caps
        self.input_dir = input_dir
        self.output_dir = output_dir
        self.files = paths
        self.index = 0
        self.info: VideoInfo | None = None
        self.crops: dict[str, g.Crop] = {}
        self.aspect = g.WIDE  # aspect for files without a stored crop (S toggles)
        # path -> "saved" (cropped) | "uncropped" (skipped, exported whole)
        #         | "skipped" (nothing exported: unreadable or does not fit)
        self.decisions: dict[str, str] = {}
        self._summary_shown = False
        self._quit_when_idle = False
        self._force_close = False
        self._job_items: dict[int, QListWidgetItem] = {}

        self.cache = ProbeCache(caps.ffprobe)
        self.cache.prefetch(paths)
        self.queue = EncodeQueue(caps, output_dir, self)
        self.queue.changed.connect(self._on_queue_changed)
        self.queue.progress.connect(self._on_job_progress)
        self.queue.idle.connect(self._on_queue_idle)

        self._build_ui()
        self._setup_player()
        self._setup_shortcuts()
        self.setWindowTitle(APP_NAME)
        self.resize(1400, 880)
        QTimer.singleShot(0, lambda: self.show_index(0))

    # ------------------------------------------------------------ UI
    def _build_ui(self):
        self.view = VideoView()
        self.view.cropChanged.connect(self._on_crop_changed)

        st = self.style()
        self.play_btn = QToolButton(); self.play_btn.setIcon(st.standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.play_btn.setToolTip("Play / Pause (Space)")
        self.play_btn.clicked.connect(self.toggle_play)
        self.mute_btn = QToolButton(); self.mute_btn.setCheckable(True)
        self.mute_btn.setIcon(st.standardIcon(QStyle.StandardPixmap.SP_MediaVolume))
        self.mute_btn.setToolTip("Mute (M)")
        self.mute_btn.toggled.connect(self._set_muted)
        self.slider = JumpSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.sliderMoved.connect(self._seek)
        self.time_lbl = QLabel("0:00.0 / 0:00.0")
        self.time_lbl.setMinimumWidth(130)

        self.prev_btn = QPushButton("◀  Previous"); self.prev_btn.setToolTip("Previous video (←)")
        self.reset_btn = QPushButton("Reset crop"); self.reset_btn.setToolTip("Largest centred crop (R)")
        self.square_btn = QPushButton("Square 1:1"); self.square_btn.setCheckable(True)
        self.square_btn.setToolTip("Toggle between 16:10 and square 1:1 crop (S).\n"
                                   "The choice stays on for the following files.")
        self.preview_btn = QPushButton("Preview crop"); self.preview_btn.setCheckable(True)
        self.preview_btn.setToolTip("Show only the cropped result (P)")
        self.skip_btn = QPushButton("Skip crop  ▶")
        self.skip_btn.setToolTip("Don't crop: export the whole frame and show the next file (→).\n"
                                 "Files the frame already accepts are copied unchanged; others are\n"
                                 "converted (video: H.264 MP4, max 1080p, < 125 MB;\n"
                                 "photo: downscaled only if over 8K or 125 MB).")
        self.save_btn = QPushButton("Save && Next  ⏎"); self.save_btn.setToolTip("Queue export and show next video (Enter)")
        self.save_btn.setStyleSheet("QPushButton { font-weight: bold; padding: 6px 18px; "
                                    "background: #2e7d32; color: white; border-radius: 4px; }"
                                    "QPushButton:disabled { background: #555; color: #999; }")
        self.prev_btn.clicked.connect(self.prev_video)
        self.reset_btn.clicked.connect(self.view.reset_crop)
        self.preview_btn.toggled.connect(self.view.set_preview)
        self.square_btn.toggled.connect(self._set_square)
        self.skip_btn.clicked.connect(self.skip)
        self.save_btn.clicked.connect(self.save_and_next)

        timeline = QHBoxLayout()
        timeline.addWidget(self.play_btn); timeline.addWidget(self.slider, 1)
        timeline.addWidget(self.time_lbl); timeline.addWidget(self.mute_btn)
        buttons = QHBoxLayout()
        buttons.addWidget(self.prev_btn); buttons.addWidget(self.reset_btn)
        buttons.addWidget(self.square_btn)
        buttons.addWidget(self.preview_btn); buttons.addStretch(1)
        buttons.addWidget(self.skip_btn); buttons.addWidget(self.save_btn)
        hint = QLabel("Drag to move · drag corners to resize · wheel/+/- zoom · Shift+arrows nudge · "
                      "Esc undo · P preview · S square/16:10 · → skip crop · Ctrl+Q quit")
        hint.setStyleSheet("color: #888;")
        left = QVBoxLayout()
        left.addWidget(self.view, 1)
        left.addLayout(timeline)
        left.addLayout(buttons)
        left.addWidget(hint)

        # sidebar --------------------------------------------------------
        self.lbl = {}
        vid_box = QGroupBox("File")
        form = QFormLayout(vid_box)
        self._info_form = form
        for key, title in [("file", "File"), ("pos", "Position"), ("res", "Resolution"),
                           ("dur", "Duration"), ("fps", "Frame rate"), ("codec", "Format"),
                           ("audio", "Audio"), ("state", "Status")]:
            lab = QLabel("–"); lab.setWordWrap(True)
            lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            _no_focus(lab)
            form.addRow(title + ":", lab)
            self.lbl[key] = lab
        crop_box = QGroupBox("Crop")
        form = QFormLayout(crop_box)
        for key, title in [("crop", "Selection"), ("out", "Output"), ("budget", "Export")]:
            lab = QLabel("–"); lab.setWordWrap(True)
            form.addRow(title + ":", lab)
            self.lbl[key] = lab
        self.warn_lbl = QLabel(); self.warn_lbl.setWordWrap(True)
        self.warn_lbl.setStyleSheet("color: #ff9f43;")
        form.addRow(self.warn_lbl)

        exp_box = QGroupBox("Exports")
        ev = QVBoxLayout(exp_box)
        self.counts_lbl = QLabel()
        self.cur_lbl = QLabel("Idle"); self.cur_lbl.setWordWrap(True)
        self.cur_bar = QProgressBar(); self.cur_bar.setRange(0, 1000); self.cur_bar.setTextVisible(True)
        self.cur_bar.setFormat("%p%")
        self.job_list = QListWidget()
        self.job_list.itemDoubleClicked.connect(self._job_details)
        self.job_list.setToolTip("Double-click for details")
        self.job_list.setWordWrap(True)
        self.job_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        jb = QHBoxLayout()
        self.cancel_sel_btn = QPushButton("Cancel job")
        self.cancel_sel_btn.setToolTip("Cancel the selected export")
        self.cancel_all_btn = QPushButton("Cancel pending")
        self.cancel_all_btn.setToolTip("Cancel all exports that have not started yet")
        self.open_out_btn = QPushButton("Open folder")
        self.cancel_sel_btn.clicked.connect(self._cancel_selected)
        self.cancel_all_btn.clicked.connect(self.queue.cancel_pending)
        self.open_out_btn.clicked.connect(self._open_output)
        jb.addWidget(self.cancel_sel_btn); jb.addWidget(self.cancel_all_btn); jb.addWidget(self.open_out_btn)
        ev.addWidget(self.counts_lbl); ev.addWidget(self.cur_lbl); ev.addWidget(self.cur_bar)
        ev.addWidget(self.job_list, 1); ev.addLayout(jb)

        side = QVBoxLayout()
        side.addWidget(vid_box); side.addWidget(crop_box); side.addWidget(exp_box, 1)
        side_w = QWidget(); side_w.setLayout(side); side_w.setFixedWidth(370)

        central = QWidget()
        root = QHBoxLayout(central)
        root.addLayout(left, 1)
        root.addWidget(side_w)
        self.setCentralWidget(central)
        self.status_lbl = QLabel()
        self.statusBar().addPermanentWidget(self.status_lbl)

        # Keyboard shortcuts must reach the window: no control takes focus.
        _no_focus(self.play_btn, self.mute_btn, self.slider, self.prev_btn, self.reset_btn, self.square_btn,
                  self.preview_btn, self.skip_btn, self.save_btn, self.job_list,
                  self.cancel_sel_btn, self.cancel_all_btn, self.open_out_btn)
        self.view.setFocus()
        self._on_queue_changed()

    def _setup_player(self):
        self.player = QMediaPlayer(self)
        self.audio_out = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_out)
        self.player.setVideoOutput(self.view.video_item)
        self.player.setLoops(QMediaPlayer.Loops.Infinite)
        self.player.positionChanged.connect(self._on_position)
        self.player.durationChanged.connect(self._on_duration)
        self.player.playbackStateChanged.connect(self._on_play_state)
        self.player.errorOccurred.connect(self._on_player_error)

    def _setup_shortcuts(self):
        def sc(keys, fn, repeat=True):
            for k in keys:
                s = QShortcut(QKeySequence(k), self)
                s.setContext(Qt.ShortcutContext.WindowShortcut)
                s.setAutoRepeat(repeat)
                s.activated.connect(fn)
        sc(["Space"], self.toggle_play, repeat=False)
        sc(["Return", "Enter"], self.save_and_next, repeat=False)
        sc(["Right"], self.skip, repeat=False)
        sc(["Left"], self.prev_video, repeat=False)
        sc(["R"], self.view.reset_crop, repeat=False)
        sc(["S"], self.square_btn.toggle, repeat=False)
        sc(["Ctrl+Q"], self.close, repeat=False)
        sc(["Esc"], self._escape, repeat=False)
        sc(["P"], self.preview_btn.toggle, repeat=False)
        sc(["M"], self.mute_btn.toggle, repeat=False)
        sc(["+", "=", "Shift+="], lambda: self.view.zoom(+1))
        sc(["-"], lambda: self.view.zoom(-1))
        for key, dx, dy in [("Left", -1, 0), ("Right", 1, 0), ("Up", 0, -1), ("Down", 0, 1)]:
            sc([f"Shift+{key}"], lambda dx=dx, dy=dy: self.view.nudge(dx, dy))
            sc([f"Ctrl+{key}"], lambda dx=dx, dy=dy: self.view.nudge(dx, dy, fine=True))

    _VIDEO_TITLES = {"dur": "Duration", "fps": "Frame rate", "codec": "Format", "audio": "Audio"}
    _PHOTO_TITLES = {"dur": "Taken", "fps": "GPS", "codec": "Format", "audio": "Camera"}

    def _set_row_titles(self, titles: dict) -> None:
        for key, title in titles.items():
            lab = self._info_form.labelForField(self.lbl[key])
            if lab is not None:
                lab.setText(title + ":")

    def _is_photo(self) -> bool:
        return isinstance(self.info, PhotoInfo)

    def _set_square(self, on: bool) -> None:
        self.aspect = g.SQUARE if on else g.WIDE
        self.view.set_aspect(self.aspect)

    def _sync_square_btn(self, crop) -> None:
        self.aspect = crop.aspect
        self.square_btn.blockSignals(True)
        self.square_btn.setChecked(crop.aspect == g.SQUARE)
        self.square_btn.blockSignals(False)

    def _make_plan(self, crop):
        if self._is_photo():
            return photo.make_photo_plan(self.info, crop, self.caps)
        return encoding.make_plan(self.info, crop, self.caps)

    # ------------------------------------------------------------ navigation
    def show_index(self, i: int):
        self.preview_btn.setChecked(False)
        n = len(self.files)
        self.index = max(0, min(i, n))
        if self.index >= n:
            self.info = None
            self.player.stop()
            self.player.setSource(QUrl())
            self.view.clear(self._finished_text())
            self._update_info()
            self._maybe_summary()
            return
        path = self.files[self.index]
        err = None
        try:
            self.info = self.cache.get(path)
        except ProbeError as e:
            self.info, err = None, str(e)
        if self.info is not None:
            info = self.info
            crop = self.crops.get(path) or g.initial_crop(info.display_w, info.display_h,
                                                          self.aspect)
            self._sync_square_btn(crop)
            if self._is_photo():
                self.player.stop()
                self.player.setSource(QUrl())
                self.view.set_video(info.display_w, info.display_h, crop, image=path,
                                    out_box=g.PHOTO_BOX)
            else:
                self.view.set_video(info.display_w, info.display_h, crop)
                self.player.setSource(QUrl.fromLocalFile(path))
                self.player.play()
        else:
            self.player.stop()
            self.player.setSource(QUrl())
            self.view.clear(f"Cannot use this file:\n{os.path.basename(path)}\n\n{err}\n\n"
                            "Press → to skip it.")
        self._update_info(err)

    def save_and_next(self):
        if self.index >= len(self.files) or self.info is None or self.view.crop is None:
            return
        path = self.files[self.index]
        crop = self.view.crop
        try:
            plan = self._make_plan(crop)
        except PlanError as e:
            QMessageBox.warning(self, APP_NAME, f"Cannot export {os.path.basename(path)}:\n{e}")
            return
        self.crops[path] = crop
        self.decisions[path] = "saved"
        self._summary_shown = False
        self.queue.enqueue(plan)
        self.show_index(self.index + 1)

    def skip(self):
        """Don't crop: queue an uncropped export (copy or convert) and move on.

        On a file that was already reviewed (going forward again after ←)
        this only moves on, so an earlier export is never replaced."""
        if self.index >= len(self.files):
            return
        path = self.files[self.index]
        if self.view.crop is not None:
            self.crops[path] = self.view.crop
        if path not in self.decisions:
            self.decisions[path] = self._queue_uncropped(path)
        self.show_index(self.index + 1)

    def _queue_uncropped(self, path: str) -> str:
        name = os.path.basename(path)
        if self.info is None:
            return "skipped"
        try:
            plan, reasons = passthrough.make_uncropped_plan(self.info, self.caps)
        except PlanError as e:
            self.statusBar().showMessage(f"{name}: not exported – {e}", 10000)
            return "skipped"
        self._summary_shown = False
        self.queue.enqueue(plan)
        self.statusBar().showMessage(
            f"{name}: converting uncropped ({', '.join(reasons)})" if reasons
            else f"{name}: already compatible, copied unchanged", 6000)
        return "uncropped"

    def prev_video(self):
        if self.index <= 0:
            return
        if self.index < len(self.files) and self.view.crop is not None:
            self.crops[self.files[self.index]] = self.view.crop
        self.show_index(self.index - 1)

    def _escape(self):
        if self.view.preview:
            self.preview_btn.setChecked(False)
        else:
            self.view.cancel_adjustment()

    # ------------------------------------------------------------ player
    def toggle_play(self):
        if self.info is None or self._is_photo():
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _set_muted(self, muted: bool):
        self.audio_out.setMuted(muted)
        self.mute_btn.setIcon(self.style().standardIcon(
            QStyle.StandardPixmap.SP_MediaVolumeMuted if muted else QStyle.StandardPixmap.SP_MediaVolume))

    def _seek(self, ms: int):
        self.player.setPosition(ms)

    def _on_position(self, ms: int):
        if not self.slider.isSliderDown():
            self.slider.blockSignals(True)
            self.slider.setValue(ms)
            self.slider.blockSignals(False)
        self.time_lbl.setText(f"{_fmt_time(ms)} / {_fmt_time(self.player.duration())}")

    def _on_duration(self, ms: int):
        self.slider.setRange(0, max(0, ms))

    def _on_play_state(self, state):
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.play_btn.setIcon(self.style().standardIcon(
            QStyle.StandardPixmap.SP_MediaPause if playing else QStyle.StandardPixmap.SP_MediaPlay))

    def _on_player_error(self, error, text):
        if error != QMediaPlayer.Error.NoError and self.view.has_video:
            self.view.set_notice(f"Preview unavailable ({text or error.name}). "
                                 "Cropping and export still work.")

    # ------------------------------------------------------------ sidebar
    def _update_info(self, err: str | None = None):
        n = len(self.files)
        L = self.lbl
        if self.index >= n:
            for k in ("file", "res", "dur", "fps", "codec", "audio", "state"):
                L[k].setText("–")
            L["pos"].setText(f"done ({n} files)")
            self.setWindowTitle(APP_NAME)
        else:
            path = self.files[self.index]
            name = os.path.basename(path)
            L["file"].setText(name); L["file"].setToolTip(path)
            L["pos"].setText(f"{self.index + 1} / {n}")
            self.setWindowTitle(f"{name} – {APP_NAME}")
            info = self.info
            self._set_row_titles(self._PHOTO_TITLES if self._is_photo() else self._VIDEO_TITLES)
            if info is None:
                for k in ("res", "dur", "fps", "codec", "audio"):
                    L[k].setText("–")
                L["state"].setText(f"<span style='color:#e57373'>unreadable: {err}</span>")
            elif self._is_photo():
                notes = []
                if info.orientation != 1:
                    notes.append(f"EXIF orientation {info.orientation}")
                if info.is_portrait:
                    notes.append("portrait")
                L["res"].setText(f"{info.display_w} × {info.display_h}"
                                 + (f"  ({', '.join(notes)})" if notes else ""))
                L["dur"].setText(((info.taken or "–") + (f" {info.taken_offset}" if info.taken_offset else ""))
                                 if info.taken else "no date in EXIF")
                if info.gps:
                    lat, lon = info.gps
                    L["fps"].setText(f"{abs(lat):.5f}° {'N' if lat >= 0 else 'S'}, "
                                     f"{abs(lon):.5f}° {'E' if lon >= 0 else 'W'}")
                else:
                    L["fps"].setText("none")
                L["codec"].setText(f"JPEG {info.subsampling}"
                                   + (", progressive" if info.progressive else "")
                                   + f", {info.size_bytes / 1e6:.1f} MB")
                L["audio"].setText(info.camera or "–")
                L["state"].setText(self._file_state_text(path))
            else:
                res = f"{info.display_w} × {info.display_h}"
                notes = []
                if info.rotation:
                    notes.append(f"rotated {info.rotation}°")
                if abs(info.sar - 1) > 1e-6:
                    notes.append(f"SAR {info.sar_text}")
                if info.is_portrait:
                    notes.append("portrait")
                L["res"].setText(res + (f"  ({', '.join(notes)})" if notes else ""))
                L["dur"].setText(format_duration(info.duration) + f"  ({info.duration:.1f} s)")
                L["fps"].setText(f"{info.fps_text} fps" + ("  (variable)" if info.variable_fps else ""))
                fmt = f"{info.codec} {info.bit_depth}-bit"
                if info.hdr:
                    fmt += f", HDR {info.hdr.upper()} → SDR"
                L["codec"].setText(fmt + f", {info.size_bytes / 1e6:.1f} MB")
                if not info.audio:
                    L["audio"].setText("none")
                else:
                    parts = [f"{a.codec} {a.channels}ch" + (f" {a.bit_rate // 1000} kb/s" if a.bit_rate else "")
                             for a in info.audio]
                    L["audio"].setText("; ".join(parts))
                L["state"].setText(self._file_state_text(path))
        self.save_btn.setEnabled(self.info is not None and self.index < n)
        self.skip_btn.setEnabled(self.index < n)
        self.prev_btn.setEnabled(self.index > 0)
        for w in (self.reset_btn, self.preview_btn, self.square_btn):
            w.setEnabled(self.info is not None)
        for w in (self.play_btn, self.slider, self.mute_btn):
            w.setEnabled(self.info is not None and not self._is_photo())
        self._on_crop_changed(self.view.crop)
        self._update_status()

    def _file_state_text(self, path: str) -> str:
        job = self.queue.latest_job_for(path)
        if job is not None:
            return {PENDING: "queued for export", RUNNING: "exporting…",
                    DONE: f"exported → {os.path.basename(job.final_path or '')}",
                    FAILED: "<span style='color:#e57373'>export failed</span>",
                    CANCELLED: "export cancelled"}[job.state]
        if self.decisions.get(path) == "skipped":
            return "skipped (not exported)"
        return "not reviewed yet"

    def _on_crop_changed(self, crop):
        L = self.lbl
        info = self.info
        if crop is None or info is None:
            for k in ("crop", "out", "budget"):
                L[k].setText("–")
            self.warn_lbl.setText("")
            return
        self._sync_square_btn(crop)  # also follows Esc-undo of an aspect switch
        if self._is_photo():
            plan = photo.make_photo_plan(info, crop, self.caps)
            c = plan.src_crop
            L["crop"].setText(f"{c.w} × {c.h} at ({c.x}, {c.y})")
            L["out"].setText(f"<b>{plan.out_w} × {plan.out_h}</b> {crop.label}  "
                             f"({'downscaled to 8K' if plan.scaled else 'native resolution'})")
            L["budget"].setText(plan.method_text + ", keeps date & GPS")
            self.warn_lbl.setText("\n".join(w[0].upper() + w[1:] + "." for w in plan.warnings))
            return
        src = g.to_source_crop(crop, info.frame_w, info.frame_h, info.display_w, info.display_h)
        ow, oh = g.output_size(crop)
        L["crop"].setText(f"{src.w} × {src.h} at ({src.x}, {src.y})")
        scale = "downscaled" if (src.w, src.h) != (ow, oh) and g.is_downscaled(crop) else (
            "rescaled (non-square pixels)" if (src.w, src.h) != (ow, oh) else "no scaling")
        L["out"].setText(f"<b>{ow} × {oh}</b> {crop.label}  ({scale})")
        warns = []
        if oh < g.SMALL_OUTPUT_HEIGHT:
            warns.append(f"Small crop: output only {ow}×{oh}, quality may suffer.")
        try:
            plan = encoding.make_plan(info, crop, self.caps)
        except PlanError as e:
            L["budget"].setText("does not fit")
            warns.append(str(e))
        else:
            mbps = plan.budget_video_bps / 1e6
            if plan.try_crf_first:
                L["budget"].setText(f"fits under 120 MB (quality mode, up to {mbps:.0f} Mb/s)")
            else:
                L["budget"].setText(f"limited to ≈{mbps:.1f} Mb/s (two-pass) for 120 MB")
            warns += [w[0].upper() + w[1:] + "." for w in plan.warnings
                      if not w.startswith("small crop")]
        self.warn_lbl.setText("\n".join(warns))

    def _update_status(self):
        c = self.queue.counts()
        n = len(self.files)
        reviewed = sum(1 for p in self.files if p in self.decisions)
        self.counts_lbl.setText(
            f"<b>{c[DONE]}</b> done · <b>{c[RUNNING]}</b> running · <b>{c[PENDING]}</b> pending"
            + (f" · <span style='color:#e57373'><b>{c[FAILED]}</b> failed</span>" if c[FAILED] else "")
            + (f" · {c[CANCELLED]} cancelled" if c[CANCELLED] else ""))
        pos = f"File {min(self.index + 1, n)} / {n}" if self.index < n else f"All {n} reviewed"
        self.status_lbl.setText(f"{pos}  ·  reviewed {reviewed}  ·  exports: {c[DONE]} done, "
                                f"{c[PENDING] + c[RUNNING]} pending, {c[FAILED]} failed"
                                f"  ·  output: {self.output_dir}")

    # ------------------------------------------------------------ queue view
    def _job_text(self, j: Job) -> str:
        name = j.name + (" (uncropped)" if j.plan.crop is None else "")
        if j.state == PENDING:
            return f"⏳ {name}"
        if j.state == RUNNING:
            sp = f", {j.speed}" if j.speed and j.speed != "N/A" else ""
            return f"▶ {name} — {j.phase} {j.progress * 100:.0f}%{sp}"
        if j.state == DONE:
            return f"✔ {name} → {os.path.basename(j.final_path or '')} ({j.size / 1e6:.1f} MB)"
        if j.state == FAILED:
            return f"✖ {name} — {j.message}"
        return f"⊘ {name} — cancelled"

    def _on_queue_changed(self):
        self.job_list.clear()
        self._job_items.clear()
        colors = {DONE: "#81c784", FAILED: "#e57373", CANCELLED: "#9e9e9e", RUNNING: "#ffcc33"}
        for j in self.queue.jobs:
            it = QListWidgetItem(self._job_text(j))
            it.setData(Qt.ItemDataRole.UserRole, j.id)
            tip = [j.source, j.message] + j.warnings
            it.setToolTip("\n".join(t for t in tip if t))
            if j.state in colors:
                it.setForeground(QColor(colors[j.state]))
            if j.warnings and j.state != FAILED:
                it.setText(it.text() + "  ⚠")
            self.job_list.addItem(it)
            self._job_items[j.id] = it
        running = next((j for j in self.queue.jobs if j.state == RUNNING), None)
        self._on_job_progress(running)
        self._update_status()
        if self.index < len(self.files):
            self.lbl["state"].setText(self._file_state_text(self.files[self.index]))
        else:
            self.view.clear(self._finished_text())
        self._maybe_summary()

    def _on_job_progress(self, job: Job | None):
        if job is None or job.state != RUNNING:
            self.cur_lbl.setText("Idle")
            self.cur_bar.setValue(0)
            self.cur_bar.setTextVisible(False)
            return
        self.cur_bar.setTextVisible(True)
        sp = f" · {job.speed}" if job.speed and job.speed != "N/A" else ""
        self.cur_lbl.setText(f"{job.name}\n{job.phase}{sp}")
        self.cur_bar.setValue(int(job.progress * 1000))
        it = self._job_items.get(job.id)
        if it is not None:
            it.setText(self._job_text(job))

    def _selected_job_id(self) -> int | None:
        it = self.job_list.currentItem()
        return it.data(Qt.ItemDataRole.UserRole) if it else None

    def _cancel_selected(self):
        jid = self._selected_job_id()
        if jid is not None:
            self.queue.cancel(jid)

    def _job_details(self, item: QListWidgetItem):
        jid = item.data(Qt.ItemDataRole.UserRole)
        j = next((x for x in self.queue.jobs if x.id == jid), None)
        if j is None:
            return
        p = j.plan
        crop = (f"Crop (source pixels): {p.src_crop.w}×{p.src_crop.h} at ({p.src_crop.x}, {p.src_crop.y})"
                if p.crop is not None else "Crop: none (whole frame)")
        lines = [f"Source: {j.source}", f"State: {j.state}", crop,
                 f"Output: {p.out_w}×{p.out_h}", f"Destination: {j.final_path or j.out_path}"]
        if j.method:
            lines.append(f"Encoding: {j.method}")
        if j.message:
            lines.append(f"Result: {j.message}")
        lines += [f"Note: {w}" for w in j.warnings]
        QMessageBox.information(self, APP_NAME, "\n".join(lines))

    def _open_output(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(self.output_dir))

    # ------------------------------------------------------------ finishing
    def _finished_text(self) -> str:
        c = self.queue.counts()
        busy = c[PENDING] + c[RUNNING]
        msg = f"All {len(self.files)} files reviewed."
        if busy:
            msg += f"\n\nWaiting for {busy} export(s) to finish…"
        else:
            msg += f"\n\n{c[DONE]} exported, {c[FAILED]} failed.\nPress ← to go back."
        return msg

    def _on_queue_idle(self):
        if self._quit_when_idle:
            self._force_close = True
            self.close()
            return
        self._maybe_summary()

    def _maybe_summary(self):
        if (self.index >= len(self.files) and not self.queue.is_busy()
                and not self._summary_shown):
            self._summary_shown = True
            QTimer.singleShot(0, self._show_summary)

    def _show_summary(self):
        latest: dict[str, Job] = {}
        for j in self.queue.jobs:
            latest[j.source] = j
        done = [j for j in latest.values() if j.state == DONE]
        failed = [j for j in latest.values() if j.state == FAILED]
        cancelled = [j for j in latest.values() if j.state == CANCELLED]
        skipped = [p for p, d in self.decisions.items() if d == "skipped" and p not in latest]
        total = sum(j.size for j in done)
        warned = [j for j in done if j.warnings]
        whole = [j for j in done if j.plan.crop is None]
        copied = sum(1 for j in whole if j.method == "copied unchanged")
        text = (f"Reviewed {len(self.decisions)} of {len(self.files)} files.\n\n"
                f"Exported: {len(done)}  ({total / 1e6:.1f} MB total)\n"
                f"    cropped: {len(done) - len(whole)}\n"
                f"    uncropped: {len(whole)}  ({copied} copied unchanged, "
                f"{len(whole) - copied} converted)\n"
                f"Not exported: {len(skipped)}\nFailed: {len(failed)}\nCancelled: {len(cancelled)}\n\n"
                f"Output folder:\n{self.output_dir}")
        box = QMessageBox(self)
        box.setWindowTitle(f"{APP_NAME} – summary")
        box.setIcon(QMessageBox.Icon.Warning if failed else QMessageBox.Icon.Information)
        box.setText(text)
        details = [f"FAILED {j.name}: {j.message}" for j in failed]
        details += [f"NOTE {j.name}: {w}" for j in warned for w in j.warnings]
        if details:
            box.setDetailedText("\n".join(details))
        open_btn = box.addButton("Open output folder", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        if box.clickedButton() is open_btn:
            self._open_output()

    def closeEvent(self, e):
        if self.queue.is_busy() and not self._force_close:
            c = self.queue.counts()
            box = QMessageBox(self)
            box.setWindowTitle(APP_NAME)
            box.setIcon(QMessageBox.Icon.Question)
            box.setText(f"{c[RUNNING] + c[PENDING]} export(s) are still running or queued.")
            wait = box.addButton("Finish exports, then quit", QMessageBox.ButtonRole.AcceptRole)
            abort = box.addButton("Cancel exports and quit", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton("Keep working", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            clicked = box.clickedButton()
            if clicked is wait:
                self._quit_when_idle = True
                self.statusBar().showMessage("Will quit when all exports have finished…")
                e.ignore()
                return
            if clicked is not abort:
                e.ignore()
                return
        self.player.stop()
        self.queue.abort_all()
        e.accept()

    def force_quit(self):
        """SIGINT/SIGTERM: abort exports, clean temp files and exit."""
        self._force_close = True
        self.close()
