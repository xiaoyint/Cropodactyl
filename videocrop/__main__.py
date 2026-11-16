"""Entry point:  python -m videocrop [INPUT_DIR] [-o OUTPUT_DIR]"""
from __future__ import annotations

import argparse
import os
import signal
import sys


def _dark_palette(app) -> None:
    from PySide6.QtGui import QColor, QPalette
    app.setStyle("Fusion")
    p = QPalette()
    base, alt, text = QColor("#232323"), QColor("#2c2c2c"), QColor("#e6e6e6")
    for role, col in [
        (QPalette.ColorRole.Window, QColor("#2a2a2a")), (QPalette.ColorRole.WindowText, text),
        (QPalette.ColorRole.Base, base), (QPalette.ColorRole.AlternateBase, alt),
        (QPalette.ColorRole.Text, text), (QPalette.ColorRole.Button, QColor("#353535")),
        (QPalette.ColorRole.ButtonText, text), (QPalette.ColorRole.Highlight, QColor("#3d6fb6")),
        (QPalette.ColorRole.HighlightedText, QColor("white")), (QPalette.ColorRole.ToolTipBase, alt),
        (QPalette.ColorRole.ToolTipText, text), (QPalette.ColorRole.PlaceholderText, QColor("#888")),
    ]:
        p.setColor(role, col)
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, QColor("#777"))
    app.setPalette(p)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="videocrop",
                                 description="Crop videos to 16:10 and export H.264 MP4 under 125 MB.")
    ap.add_argument("input_dir", nargs="?", help="folder containing the videos")
    ap.add_argument("-o", "--output", help="output folder (default: INPUT_DIR/cropped)")
    args = ap.parse_args(argv)

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

    from . import encoding, files
    from .mainwindow import APP_NAME, MainWindow, StartDialog

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    _dark_palette(app)

    try:
        caps = encoding.detect_caps()
    except encoding.MissingFFmpeg as e:
        QMessageBox.critical(None, APP_NAME, f"{e}\n\nInstall FFmpeg with:\n  sudo apt install ffmpeg")
        return 1

    in_dir = os.path.abspath(args.input_dir) if args.input_dir else ""
    out_dir = os.path.abspath(args.output) if args.output else ""
    dlg = StartDialog(in_dir, out_dir)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return 0
    in_dir, out_dir = dlg.input_dir, dlg.output_dir
    paths = files.scan_videos(in_dir)

    win = MainWindow(caps, in_dir, out_dir, paths)
    win.show()

    # Ctrl+C / SIGTERM in the terminal: stop FFmpeg and remove temp files.
    def _on_signal(*_):
        QTimer.singleShot(0, win.force_quit)
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    tick = QTimer()
    tick.timeout.connect(lambda: None)  # lets Python run signal handlers
    tick.start(300)
    app.aboutToQuit.connect(win.queue.abort_all)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
