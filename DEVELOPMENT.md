# Development notes

Technical details for people working on the code. For using the app, see
[README.md](README.md).

## Manual setup

The launchers (`run.sh`, `run.bat`) create `.venv` and install the requirements
automatically. To do it by hand:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m videocrop
```

Windows:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m videocrop
```

FFmpeg 4.4 (Ubuntu 22.04's version) and newer work. On Windows the Gyan "full"
build has libx264 and zscale/tonemap (needed for HDR sources).

## Tests

```bash
.venv/bin/python -m pytest                  # all tests (about 25 s)
.venv/bin/python -m pytest tests/test_geometry.py tests/test_encoding.py   # fast unit tests only
```

On Windows: `.venv\Scripts\python -m pytest`.

The integration tests generate small synthetic videos with FFmpeg: rotated,
anamorphic, HDR, no audio, two audio streams, and special characters in names.
They check that the exported pixels really come from the selected region and that
the size limit forces two-pass. The GUI tests run with `QT_QPA_PLATFORM=offscreen`
and need no display.

## Code layout

```
├── run.sh / run.bat        launchers for Linux / Windows (create .venv on first run)
├── requirements.txt
├── videocrop/
│   ├── __main__.py         entry point, start dialog, signal handling
│   ├── mainwindow.py       main window, sidebar, shortcuts, summary
│   ├── view.py / view.qml  video preview + interactive crop overlay
│   ├── geometry.py         pure 16:10 / 1:1 crop maths (no Qt)
│   ├── probe.py            ffprobe parsing (rotation, SAR, HDR, audio) + cache
│   ├── encoding.py         size budget, FFmpeg commands, encode state machine, validation
│   ├── exportqueue.py      QProcess-based background queue
│   ├── mp4meta.py          writes the ©xyz geotag atom into the exported MP4
│   ├── photo.py            JPEG probing, EXIF-orientation mapping, lossless crop, metadata
│   ├── photo_reencode.py   helper process for photos that must be re-encoded
│   ├── passthrough.py      skipped files: copy unchanged if compatible, else convert uncropped
│   └── files.py            folder scan, natural sort, collision-free names
└── tests/
    ├── test_geometry.py            crop / aspect / resolution rules
    ├── test_encoding.py            budget, commands, retries, validation, naming
    ├── test_integration_ffmpeg.py  real FFmpeg exports of synthetic videos
    ├── test_metadata.py            date, geotag, device tags, file date survive export
    ├── test_photo.py               photo crop mapping (all 8 orientations), lossless/re-encode, EXIF
    ├── test_uncropped.py           skip: copy-if-compatible, whole-frame conversion, GUI flow
    └── test_gui.py                 overlay mouse interaction, queue, main window (offscreen)
```

## Design

**Playback backend.** Preview uses **Qt Multimedia** (its bundled FFmpeg
backend) rendered through a Qt Quick `VideoOutput` inside a `QQuickWidget`. The
crop overlay is a normal transparent widget on top. Two simpler options were
tried and rejected:

* `QVideoWidget` is a native window, so widgets cannot be drawn reliably on top of it.
* Converting each frame to a `QImage` cost ~68 ms per 4K 10-bit frame on the GUI
  thread (measured), which makes the UI stutter.

The Quick path converts colours on the GPU, applies rotation itself, and uses
VA-API hardware decoding when drivers are installed. During 4K HDR playback,
GUI-thread latency stayed under 20 ms. The overlay only repaints when the crop
changes. To check that hardware decoding works:
`QT_LOGGING_RULES="qt.multimedia.ffmpeg*=true" ./run.sh`.

**Background work.** Exports run one at a time through `QProcess`, which is
asynchronous, so the GUI never blocks. FFmpeg runs under `nice -n 10` (not
available on Windows, where it runs at normal priority), so the preview stays
smooth while encoding. Metadata (`ffprobe`) is prefetched for all files in a
background thread, so "Save & Next" is instant.

**Temp files.** Encoding happens in a hidden `.videocrop-tmp/` folder inside the
output folder. Failed or cancelled jobs are deleted; leftovers from a crash are
removed on the next start. Before each video job the app checks that ~320 MB of
disk space is free.

## Video export details

Every export is validated with `ffprobe` before it is moved to the output folder:

* MP4 container, H.264 (libx264, High profile, yuv420p) video, AAC audio.
* Cropped exports are exactly 16:10 or 1:1: the output size is always
  `16k × 10k` (square `10k × 10k`), so it is even and H.264-safe, at most
  1728×1080 (square 1080×1080). Uncropped exports keep the source aspect ratio
  (even dimensions, rounded down) and fit 1920×1080 or 1080×1920.
* Never upscaled; downscaling uses Lanczos.
* Strictly smaller than 125,000,000 bytes (below both 125 MB and 125 MiB).
* Frame rate and timestamps kept (`-fps_mode passthrough`, or `-vsync
  passthrough` on FFmpeg 4.x), so A/V sync is preserved.
* All audio streams kept. AAC up to 320 kb/s is copied; other formats become AAC.
* Rotation metadata is applied to the pixels; the output has no rotation flag.
  Non-square pixels (SAR) are corrected.
* HDR (HLG / PQ) is tone-mapped to SDR BT.709 with zscale + tonemap.
* Metadata kept, and validation fails if it goes missing: `creation_time`,
  `com.apple.quicktime.creationdate`, the GPS geotag (written as the QuickTime
  `©xyz` atom that Google Photos, Apple Photos, digiKam and exiftool read; the
  iPhone ISO 6709 key is kept too), and device tags (`com.android.*`,
  `com.apple.quicktime.make` / `model`).
* The file's modification date is set to the recording time (or to the
  original's file date if there is none).

### File size control

1. Budget = 120 MB minus audio and minus MP4 overhead (1 % + 100 KB), divided by
   the duration.
2. If the budget is generous, encode once with CRF 18. Progress is watched; if
   the file is clearly heading over 125 MB, the encode is stopped early.
3. Otherwise, or if the CRF file is too big, run a two-pass encode at the budget
   bitrate.
4. If the file is still ≥ 125 MB, pass 2 is repeated with a proportionally lower
   bitrate (up to 3 times), reusing the pass-1 statistics.
5. Validate, then move the file into place.

For long videos audio gets a smaller share of the budget. When the bitrate gets
very low the UI warns; videos that cannot reasonably fit (about 2 hours with
audio) are refused.

### Skipped (uncropped) videos

Copied unchanged when: `.mp4`, H.264, 8-bit yuv420p, not HDR, fits 1080p in
either orientation, < 125 MB, all audio AAC, no rotation flag, square pixels.
Otherwise re-encoded with the pipeline above (`encoding.make_full_plan`).

## Photo export details

* **Lossless crop** with `jpegtran -crop -copy all`: the compressed data is cut,
  not re-encoded. The crop's top-left corner is snapped to the JPEG block grid
  (8 or 16 px). A 37 MP photo takes about 0.2 s.
* **Re-encode** only when the crop is larger than 8K (7680×4800, square
  7680×7680), the result would be ≥ 125 MB, or jpegtran is missing. It reuses
  the original's quantization tables and chroma subsampling
  (`photo_reencode.py`, run as a separate process).
* **Kept:** EXIF (date taken with time zone, GPS, camera, orientation), ICC
  profile, the rest of the XMP. Validation fails if date or GPS goes missing.
* **Updated:** EXIF pixel dimensions are patched in place; the embedded
  thumbnail (IFD1) is detached. The file date is set to the time taken.
* **Removed** (it describes the uncropped photo): the MPF index and Ultra HDR
  gain map, extended XMP (Portrait depth map, relighting, HDR+ data), the
  hdrgm/Container description in the main XMP, and APP11 JUMBF (C2PA Content
  Credentials, invalid after editing).
* **Skipped (uncropped) photos** are copied byte-for-byte when they fit 8K and
  are < 125 MB; otherwise they are downscaled to fit 8K and re-encoded as above.
