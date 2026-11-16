"""Video preview with an interactive, aspect-locked (16:10 or 1:1) crop overlay.

The video is rendered by Qt Quick's ``VideoOutput`` (GPU colour conversion,
rotation handled by Qt) inside a ``QQuickWidget``.  We size and position the
video item ourselves using the ffprobe display size, which also corrects
non-square pixels.  A transparent QWidget on top draws the crop rectangle and
handles all mouse interaction; it only repaints when the crop changes, so the
overlay costs nothing during playback.
"""
from __future__ import annotations

import os
import time

from PySide6.QtCore import QPointF, QRectF, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtQuick import QQuickItem
from PySide6.QtQuickWidgets import QQuickWidget
from PySide6.QtWidgets import QWidget

from . import geometry as g
from .geometry import Crop

HANDLE_RADIUS = 14  # px: grab distance for corner handles
HANDLE_SIZE = 10
BG = QColor("#151515")
ACCENT = QColor("#ffcc33")
WARN = QColor("#ff9f43")


class _Overlay(QWidget):
    def __init__(self, view: "VideoView"):
        super().__init__(view)
        self._view = view
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._view._paint(p)

    def mousePressEvent(self, e):
        self._view._press(e)

    def mouseMoveEvent(self, e):
        self._view._move(e)

    def mouseReleaseEvent(self, e):
        self._view._release(e)

    def wheelEvent(self, e):
        self._view._wheel(e)


class VideoView(QWidget):
    cropChanged = Signal(object)  # Crop | None

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(320, 200)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._quick = QQuickWidget(self)
        self._quick.setResizeMode(QQuickWidget.ResizeMode.SizeRootObjectToView)
        self._quick.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._quick.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._quick.setSource(QUrl.fromLocalFile(os.path.join(os.path.dirname(__file__), "view.qml")))
        if self._quick.status() != QQuickWidget.Status.Ready:
            errs = "; ".join(e.toString() for e in self._quick.errors())
            raise RuntimeError(f"cannot load QML video view: {errs}")
        root = self._quick.rootObject()
        self.video_item: QQuickItem = root.findChild(QQuickItem, "video")
        self.image_item: QQuickItem = root.findChild(QQuickItem, "image")
        self._image_mode = False
        self._out_box = g.VIDEO_BOX
        self._overlay = _Overlay(self)
        self._overlay.raise_()

        self._dw = 0
        self._dh = 0
        self._crop: Crop | None = None
        self._preview = False
        self._notice = ""
        self._xf = g.ViewTransform(1.0, 0.0, 0.0)
        # interaction state
        self._drag: str | None = None  # "move" | "corner"
        self._drag_start: Crop | None = None
        self._drag_start_moved: Crop | None = None
        self._press_w = QPointF()
        self._anchor = (0, 0)
        self._signs = (1, 1)
        self._undo: Crop | None = None
        self._wheel_acc = 0
        self._last_wheel = 0.0

    # ------------------------------------------------------------ public API
    @property
    def crop(self) -> Crop | None:
        return self._crop

    @property
    def has_video(self) -> bool:
        return self._crop is not None

    @property
    def preview(self) -> bool:
        return self._preview

    def set_video(self, dw: int, dh: int, crop: Crop, image: str | None = None,
                  out_box: tuple[int, int] = g.VIDEO_BOX) -> None:
        """Show a video (rendered by the media player) or, with *image*, a photo."""
        self._image_mode = image is not None
        self._out_box = out_box
        self.image_item.setProperty("source", QUrl.fromLocalFile(image) if image else QUrl())
        self._dw, self._dh = dw, dh
        self._crop = g.clamp_crop(crop, dw, dh)
        self._undo = None
        self._drag = None
        self._notice = ""
        self._relayout()
        self.cropChanged.emit(self._crop)

    def clear(self, message: str = "") -> None:
        self._crop = None
        self._dw = self._dh = 0
        self._drag = None
        self._notice = message
        self.video_item.setVisible(False)
        self.image_item.setVisible(False)
        self.image_item.setProperty("source", QUrl())
        self._overlay.update()
        self.cropChanged.emit(None)

    def set_notice(self, text: str) -> None:
        self._notice = text
        self._overlay.update()

    def set_crop(self, c: Crop, record_undo: bool = True) -> None:
        if self._crop is None:
            return
        c = g.clamp_crop(c, self._dw, self._dh)
        if c == self._crop:
            return
        if record_undo:
            self._undo = self._crop
        self._crop = c
        if self._preview:
            self._relayout()
        else:
            self._overlay.update()
        self.cropChanged.emit(c)

    def reset_crop(self) -> None:
        if self._crop is not None:
            self.set_crop(g.initial_crop(self._dw, self._dh, self._crop.aspect))

    def set_aspect(self, aspect: tuple[int, int]) -> None:
        """Switch between 16:10 and 1:1 around the current centre (Esc undoes)."""
        if self._crop is not None and self._crop.aspect != tuple(aspect):
            self._drag = None
            self.set_crop(g.with_aspect(self._crop, aspect, self._dw, self._dh))

    def cancel_adjustment(self) -> bool:
        """Esc: abort the current drag, or undo the last adjustment."""
        if self._crop is None:
            return False
        if self._drag is not None and self._drag_start is not None:
            start = self._drag_start
            self._drag = None
            self._drag_start = None
            self.set_crop(start, record_undo=False)
            return True
        if self._undo is not None:
            prev, self._undo = self._undo, None
            self.set_crop(prev, record_undo=False)
            return True
        return False

    def nudge(self, fx: float, fy: float, fine: bool = False) -> None:
        """Move by a fraction of the frame size (or by 2 px if *fine*)."""
        if self._crop is None:
            return
        if fine:
            dx, dy = 2 * (fx > 0) - 2 * (fx < 0), 2 * (fy > 0) - 2 * (fy < 0)
        else:
            dx = round(fx * max(2, self._dw * 0.01))
            dy = round(fy * max(2, self._dh * 0.01))
        self.set_crop(g.moved(self._crop, dx, dy, self._dw, self._dh))

    def zoom(self, steps: int) -> None:
        """steps > 0 makes the crop larger."""
        if self._crop is not None:
            self.set_crop(g.zoomed(self._crop, steps, self._dw, self._dh))

    def set_preview(self, on: bool) -> None:
        if self._crop is None:
            on = False
        if on != self._preview:
            self._preview = on
            self._drag = None
            self._relayout()

    # ------------------------------------------------------------ layout
    def resizeEvent(self, e):
        r = self.rect()
        self._quick.setGeometry(r)
        self._overlay.setGeometry(r)
        self._relayout()

    def _relayout(self) -> None:
        if self._crop is None or not self._dw:
            self.video_item.setVisible(False)
            self.image_item.setVisible(False)
            self._overlay.update()
            return
        vw, vh = self.width(), self.height()
        if self._preview:
            self._xf = g.fit_crop(self._crop, vw, vh)
        else:
            self._xf = g.fit_frame(self._dw, self._dh, vw, vh)
        s = self._xf.scale
        it, other = ((self.image_item, self.video_item) if self._image_mode
                     else (self.video_item, self.image_item))
        other.setVisible(False)
        it.setX(self._xf.ox)
        it.setY(self._xf.oy)
        it.setWidth(self._dw * s)
        it.setHeight(self._dh * s)
        it.setVisible(True)
        self._overlay.update()

    # ------------------------------------------------------------ painting
    def _crop_rect_w(self) -> QRectF:
        c = self._crop
        x, y = self._xf.to_widget(c.x, c.y)
        return QRectF(x, y, c.w * self._xf.scale, c.h * self._xf.scale)

    def _paint(self, p: QPainter) -> None:
        if self._crop is None:
            if self._notice:
                self._draw_notice(p, self._notice, center=True)
            return
        c = self._crop
        cr = self._crop_rect_w()
        full = QRectF(self._overlay.rect())
        if self._preview:
            path = QPainterPath()
            path.addRect(full)
            path.addRect(cr)
            p.fillPath(path, BG)  # odd-even fill: everything except the crop
            ow, oh = g.output_size(c, self._out_box)
            self._draw_tag(p, QPointF(cr.left() + 8, cr.top() + 8),
                           f"PREVIEW  {ow}×{oh}   (P to edit)", ACCENT)
        else:
            x0, y0 = self._xf.to_widget(0, 0)
            frame = QRectF(x0, y0, self._dw * self._xf.scale, self._dh * self._xf.scale)
            path = QPainterPath()
            path.addRect(frame)
            path.addRect(cr)
            p.fillPath(path, QColor(0, 0, 0, 160))
            # rule-of-thirds guides
            p.setPen(QPen(QColor(255, 255, 255, 60), 1))
            for i in (1, 2):
                xx = cr.left() + cr.width() * i / 3
                yy = cr.top() + cr.height() * i / 3
                p.drawLine(QPointF(xx, cr.top()), QPointF(xx, cr.bottom()))
                p.drawLine(QPointF(cr.left(), yy), QPointF(cr.right(), yy))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(0, 0, 0, 200), 3))
            p.drawRect(cr)
            p.setPen(QPen(ACCENT, 1.5))
            p.drawRect(cr)
            hs = HANDLE_SIZE
            p.setPen(QPen(QColor(0, 0, 0, 220), 1))
            p.setBrush(ACCENT)
            for pt in (cr.topLeft(), cr.topRight(), cr.bottomLeft(), cr.bottomRight()):
                p.drawRect(QRectF(pt.x() - hs / 2, pt.y() - hs / 2, hs, hs))
            ow, oh = g.output_size(c, self._out_box)
            small = oh < g.SMALL_OUTPUT_HEIGHT
            text = f"{ow}×{oh}"
            if g.is_downscaled(c, self._out_box):
                text += f"  (from {c.w}×{c.h})"
            if small:
                text += "  low resolution!"
            self._draw_tag(p, QPointF(cr.left() + 6, cr.top() + 6), text,
                           WARN if small else ACCENT)
        if self._notice:
            self._draw_notice(p, self._notice, center=False)

    def _draw_tag(self, p: QPainter, pos: QPointF, text: str, color: QColor) -> None:
        f = QFont(self.font())
        f.setPointSizeF(max(8.0, f.pointSizeF() * 0.95))
        f.setBold(True)
        p.setFont(f)
        fm = p.fontMetrics()
        r = QRectF(pos.x(), pos.y(), fm.horizontalAdvance(text) + 12, fm.height() + 6)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 170))
        p.drawRoundedRect(r, 4, 4)
        p.setPen(color)
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, text)

    def _draw_notice(self, p: QPainter, text: str, center: bool) -> None:
        f = QFont(self.font())
        f.setPointSizeF(f.pointSizeF() * (1.4 if center else 1.0))
        p.setFont(f)
        r = QRectF(self._overlay.rect()).adjusted(30, 30, -30, -30)
        if center:
            p.setPen(QColor("#dddddd"))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, text)
            return
        fm = p.fontMetrics()
        box = fm.boundingRect(r.toRect(), Qt.AlignmentFlag.AlignHCenter | Qt.TextFlag.TextWordWrap, text)
        box = QRectF(box).adjusted(-10, -6, 10, 6)
        box.moveBottom(self._overlay.height() - 16)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(120, 30, 30, 220))
        p.drawRoundedRect(box, 6, 6)
        p.setPen(QColor("white"))
        p.drawText(box, Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, text)

    # ------------------------------------------------------------ mouse
    def _hit(self, pos: QPointF) -> str | None:
        cr = self._crop_rect_w()
        corners = {"tl": cr.topLeft(), "tr": cr.topRight(),
                   "bl": cr.bottomLeft(), "br": cr.bottomRight()}
        best, best_d = None, HANDLE_RADIUS
        for name, pt in corners.items():
            d = max(abs(pt.x() - pos.x()), abs(pt.y() - pos.y()))
            if d <= best_d:
                best, best_d = name, d
        if best:
            return best
        return "move" if cr.contains(pos) else None

    def _press(self, e) -> None:
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        if self._crop is None or self._preview or e.button() != Qt.MouseButton.LeftButton:
            return
        pos = e.position()
        hit = self._hit(pos)
        self._drag_start = self._crop
        self._undo = self._crop
        if hit in ("tl", "tr", "bl", "br"):
            c = self._crop
            sx = 1 if hit[1] == "r" else -1
            sy = 1 if hit[0] == "b" else -1
            self._anchor = (c.x if sx > 0 else c.right, c.y if sy > 0 else c.bottom)
            self._signs = (sx, sy)
            self._drag = "corner"
        else:
            if hit is None:  # click outside: jump the crop there, then drag it
                px, py = self._xf.to_display(pos.x(), pos.y())
                self.set_crop(g.centered_at(px, py, self._crop.k, self._dw, self._dh,
                                            self._crop.aspect),
                              record_undo=False)
                self._drag_start_moved = self._crop
            else:
                self._drag_start_moved = self._crop
            self._press_w = pos
            self._drag = "move"
            self._overlay.setCursor(Qt.CursorShape.ClosedHandCursor)

    def _move(self, e) -> None:
        if self._crop is None or self._preview:
            self._overlay.unsetCursor()
            return
        pos = e.position()
        if self._drag == "move":
            s = self._xf.scale
            base = self._drag_start_moved
            self.set_crop(g.moved(base, (pos.x() - self._press_w.x()) / s,
                                  (pos.y() - self._press_w.y()) / s, self._dw, self._dh),
                          record_undo=False)
        elif self._drag == "corner":
            px, py = self._xf.to_display(pos.x(), pos.y())
            ax, ay = self._anchor
            sx, sy = self._signs
            self.set_crop(g.resized_from_corner(ax, ay, sx, sy, px, py, self._dw, self._dh,
                                                self._crop.aspect),
                          record_undo=False)
        else:
            hit = self._hit(pos)
            shape = {"tl": Qt.CursorShape.SizeFDiagCursor, "br": Qt.CursorShape.SizeFDiagCursor,
                     "tr": Qt.CursorShape.SizeBDiagCursor, "bl": Qt.CursorShape.SizeBDiagCursor,
                     "move": Qt.CursorShape.OpenHandCursor}.get(hit, Qt.CursorShape.CrossCursor)
            self._overlay.setCursor(shape)

    def _release(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton and self._drag:
            self._drag = None
            self._drag_start = None
            if self._undo == self._crop:
                self._undo = None
            self._move(e)

    def _wheel(self, e) -> None:
        if self._crop is None or self._preview:
            return
        now = time.monotonic()
        if now - self._last_wheel > 0.8:  # group a wheel gesture for Esc-undo
            self._undo = self._crop
            self._wheel_acc = 0
        self._last_wheel = now
        self._wheel_acc += e.angleDelta().y()
        steps = int(self._wheel_acc / 120)
        if steps:
            self._wheel_acc -= steps * 120
            undo = self._undo
            # wheel up = zoom in = smaller crop
            self.set_crop(g.zoomed(self._crop, -steps, self._dw, self._dh), record_undo=False)
            self._undo = undo
        e.accept()
