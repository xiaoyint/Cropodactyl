# VideoCrop 16:10

A fast "crop, save & next" tool for videos **and JPEG photos** on Ubuntu (and
Windows). Open a folder and frame each file with a locked **16:10** crop
rectangle, or press **S** for a **square 1:1** one. Press **Enter**, and it is exported in the background. Videos become
**H.264/AAC MP4, at most 1728×1080 (square: 1080×1080), under 125 MB**. Photos stay **JPEG at full
resolution** (cropped losslessly, at most 8K, under 125 MB). Date and geotag are
preserved for both. The next file appears immediately. Press **→** to skip the
crop: the whole file is then copied unchanged if the frame already accepts it,
or converted (H.264 MP4 ≤ 1080p, photos ≤ 8K, all under 125 MB) if not.

You never type pixel sizes, bitrates or encoder settings.

---

## 1. Installation

### Ubuntu 22.04

```bash
# System packages: FFmpeg (encoder), Python venv support, Qt's X11 dependency
sudo apt update
sudo apt install ffmpeg python3-venv python3-pip libxcb-cursor0

# Recommended for photos: lossless JPEG cropping (jpegtran)
sudo apt install libjpeg-turbo-progs

# Optional: hardware-accelerated preview decoding (VA-API)
sudo apt install va-driver-all          # Intel / AMD drivers
```

Check FFmpeg:

```bash
ffmpeg -version      # Ubuntu 22.04 ships 4.4.x, which works
ffprobe -version
```

Get the app (copy this `video-cropper` folder anywhere, e.g. `~/video-cropper`).
The launcher creates a virtual environment and installs PySide6 automatically on
first start:

```bash
cd ~/video-cropper
./run.sh
```

Manual install, if you prefer:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m videocrop
```

### Windows 10 / 11

> Windows support is new and has not been tested on a real Windows machine yet.
> The code is the same as on Linux; only the launcher and process handling differ.

1. **Install Python 3.10 or newer.** Use [python.org](https://www.python.org/downloads/)
   and tick **"Add python.exe to PATH"** in the installer, or open *PowerShell*
   and run:

   ```powershell
   winget install Python.Python.3.12
   ```

2. **Install FFmpeg.** The app needs `ffmpeg.exe` and `ffprobe.exe` on the PATH.
   The easiest way is winget, which installs a full build (with libx264, and
   zscale/tonemap for HDR) and adds it to the PATH:

   ```powershell
   winget install Gyan.FFmpeg
   ```

   Without winget: download the "full" build from
   [gyan.dev](https://www.gyan.dev/ffmpeg/builds/), unzip it, for example to
   `C:\ffmpeg`, and add `C:\ffmpeg\bin` to the PATH (Start → "Edit the system
   environment variables" → Environment Variables → Path → New).

   *Recommended for photos:* lossless JPEG cropping needs `jpegtran.exe`.
   Download the Windows installer (`libjpeg-turbo-…-vc-x64.exe`) from the
   [libjpeg-turbo releases](https://github.com/libjpeg-turbo/libjpeg-turbo/releases),
   install it, and add its `bin` folder (e.g. `C:\libjpeg-turbo64\bin`) to the
   PATH. Without it, photos are re-encoded at their original quality instead.

3. **Close and reopen the terminal** so it sees the new PATH, then check:

   ```powershell
   python --version
   ffmpeg -version
   ffprobe -version
   jpegtran -version    # optional, see above
   ```

4. **Copy the `video-cropper` folder** anywhere, for example to
   `C:\Tools\video-cropper`, and start the app. Either **double-click `run.bat`**,
   or from a terminal:

   ```powershell
   cd C:\Tools\video-cropper
   .\run.bat
   ```

   The first start creates a virtual environment (`.venv`) and downloads
   PySide6 (~200 MB). This takes a minute; later starts are immediate.

Manual install, if you prefer:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m videocrop
```

Windows notes:

* Hardware-accelerated preview decoding uses Windows' own video APIs
  automatically; no driver setup is needed.
* There is no `nice` on Windows, so FFmpeg runs at normal priority. If the
  preview stutters while exporting, set `ffmpeg.exe` to "Below normal" in Task
  Manager → Details → right-click → Set priority.
* The hidden temp folder `.videocrop-tmp` is visible in Explorer if you have
  "show hidden items" enabled. It is removed automatically when exports finish.

## 2. Launch

```bash
./run.sh                               # asks for input/output folders
./run.sh ~/Videos/trip                 # input folder pre-filled
./run.sh ~/Videos/trip -o ~/Videos/out # input and output pre-filled
```

On Windows, use `run.bat` the same way:

```powershell
.\run.bat "D:\Videos\Trip" -o "D:\Videos\Trip out"
```

The output folder defaults to `<input folder>/cropped/`.

## 3. Workflow

1. Choose the input folder (and optionally the output folder), then click **Start**.
   Videos and JPEG photos in it are reviewed in one list.
2. The first video plays (looped). The yellow rectangle is the crop. It starts
   as the largest centred 16:10 area. For example, a 1920×1080 video gets a
   1728×1080 crop.
3. Drag inside the rectangle to move it. Drag a corner to resize it (the aspect
   ratio stays locked). Click outside the rectangle to jump it there.
   Press **S** (or the **Square 1:1** button) to switch between 16:10 and a
   square crop. The switch keeps the centre and the height, and Esc undoes it.
   The chosen shape stays on for the following files until you press S again.
   Files you go back to keep the shape you saved them with.
4. Press **P** to preview exactly what will be exported. Press P or Esc to go back.
5. Press **Enter** (Save & Next). The crop is queued for export and the next video
   is shown at once. Or press **→** (Skip crop) to keep the whole frame. The
   file is still exported uncropped, converted only where the frame needs it
   (see [Skipped files](#skipped-files-no-crop)).
   Going back with ← and pressing → again only moves on. It never replaces
   an export you already made.
6. When all videos are reviewed and the queue has finished, a summary appears.
7. To quit, close the window or press **Ctrl+Q**. Anything not yet exported
   is handled as described in
   [Closing while exports are running](#closing-while-exports-are-running).

The sidebar shows file info, the selected crop (in source pixels), the output
resolution, the size strategy and warnings, plus the export queue with progress.
Double-click a job for details. You can cancel a selected job or all pending jobs.

### Keyboard shortcuts

| Key | Action |
|---|---|
| Space | Play / Pause |
| Enter | Save & Next |
| → | Skip crop: export the whole frame (copy or convert), next file |
| ← | Previous video (its crop is remembered) |
| R | Reset crop (largest centred 16:10 or square) |
| S | Toggle square 1:1 / 16:10 crop |
| Ctrl+Q | Quit (asks first if exports are still running) |
| Esc | Cancel the current drag, or undo the last adjustment (leaves preview) |
| P | Toggle crop preview |
| M | Mute |
| Mouse wheel, `+` / `-` | Zoom the crop in / out (around its centre) |
| Shift + arrows | Nudge the crop by 1 % of the frame |
| Ctrl + arrows | Nudge the crop by 2 px |

Navigation keys do not auto-repeat, so holding a key cannot skip many videos by
accident.

## 4. What the export guarantees

Every successful export is checked with `ffprobe` before it is moved to the
output folder:

* MP4 container, H.264 (libx264, High profile, yuv420p) video, AAC audio.
* Cropped exports are exactly 16:10 or exactly 1:1. The output size is always
  `16k × 10k` (square: `10k × 10k`), so it is even and H.264-safe, at most
  **1728×1080** (square: **1080×1080**), which keeps it within 1080p.
  Uncropped (skipped) videos keep their own aspect ratio and fit 1920×1080, or
  1080×1920 in portrait (see [Skipped files](#skipped-files-no-crop)).
* **Never upscaled.** A crop smaller than the limit is exported at its own size,
  with no scaling at all for square-pixel sources. Larger crops (e.g. from 4K)
  are downscaled with Lanczos.
* **Strictly smaller than 125,000,000 bytes.** This is below both 125 MB and
  125 MiB.
* Original frame rate and timestamps are kept (`-fps_mode passthrough`, or
  `-vsync passthrough` on FFmpeg 4.x), so audio/video sync is preserved.
* All audio streams are kept. AAC up to 320 kb/s is copied untouched; other
  formats become AAC.
* Rotation metadata (phone videos) is applied. The crop is placed on the frame
  exactly as you see it, and the output has no rotation flag.
* Non-square pixels (anamorphic video) are corrected. The output has square pixels.
* HDR (HLG / PQ, e.g. Pixel/iPhone 10-bit) is tone-mapped to SDR BT.709, so
  colours don't look washed out.
* **Recording metadata is preserved**, and validation fails the export if it
  goes missing:
  * the recording date/time (`creation_time`, and iPhone's local-time
    `com.apple.quicktime.creationdate`)
  * the GPS geotag. It is written as the standard QuickTime `©xyz` atom that
    Google Photos, Apple Photos, digiKam and exiftool read; iPhone's ISO 6709
    key is kept as well.
  * device and camera tags such as `com.android.model` / `manufacturer` and
    `com.apple.quicktime.make` / `model`
* The export's **file date** is set to the recording time, so it sorts correctly
  in file managers and photo apps. Videos without a recording time get the
  original file's date instead.

### How the file size is controlled (automatic)

1. Budget = 120 MB minus audio and minus MP4 overhead (1 % + 100 KB), divided by
   the duration.
2. If the budget is generous, encode once with **CRF 18** (quality mode). Progress
   is watched; if the file is clearly heading over 125 MB, the encode is stopped
   early instead of wasting time.
3. Otherwise, or if the CRF file is too big, run a **two-pass** encode at the
   budget bitrate.
4. If the file is still ≥ 125 MB, pass 2 is repeated with a proportionally lower
   bitrate (up to 3 times), reusing the pass-1 statistics.
5. Validate, then move the file into place.

Short videos therefore keep full quality and are never compressed more than they
need to be. For long videos, audio gets a smaller share of the budget. When the
remaining bitrate gets very low, the UI and the job list show a warning. Videos
that cannot reasonably fit at all (about 2 hours with audio) are refused with an
explanation.

### Files and folders

* Originals are never modified, moved or overwritten.
* Output names keep the original name (`clip.mov` → `clip.mp4`). If the name is
  taken, you get `clip (1).mp4`, `clip (2).mp4`, …
* Saving the same video again in one session replaces that session's own output.
* Skipped files are exported uncropped (see below). Only unreadable files, and
  videos too long to fit 125 MB, produce no file.
* Encoding happens in a hidden `.videocrop-tmp/` folder inside the output folder.
  Failed or cancelled jobs are deleted. Leftovers from a crash are removed on the
  next start.
* Before each job the app checks that ~320 MB of disk space is free. "No space
  left on device" errors are reported clearly.
* Supported inputs: videos `.mp4 .m4v .mov .mkv .webm .avi` and photos
  `.jpg .jpeg`, mixed in one list in natural filename order (`clip2` before
  `clip10`). Photos keep their name and extension (`IMG_1.JPG` → `IMG_1.JPG`).

### Skipped files (no crop)

Pressing → exports the file **without cropping**, so everything you review ends
up in the output folder in a form the frame accepts:

* **Copied unchanged** (byte-identical, all metadata) when the file already
  qualifies:
  * Videos: `.mp4`, H.264 8-bit, at most 1080p (1920×1080, or 1080×1920 in
    portrait), smaller than 125 MB, AAC or no audio, and no rotation flag.
  * Photos: at most 8K (7680 px on each side) and smaller than 125 MB.
* **Converted** otherwise. The status bar tells you why (e.g. "codec is hevc"):
  * Videos are re-encoded with the same pipeline and guarantees as crops, but
    as the whole frame scaled down to fit 1080p in their own orientation. A 4K
    portrait clip becomes 1080×1920, and rotation is applied to the pixels.
  * Photos are downscaled to fit 8K, and further only if the file would still be
    ≥ 125 MB. They are re-encoded with their original quality settings, and
    the date taken, GPS and orientation are kept.

Pixel phones record HEVC (H.265), so their videos are always converted. 50 MP
Pixel photos (8160×6144) are just over 8K and are downscaled to 7680×5782.

The job list marks these exports "(uncropped)", and the summary counts copied
and converted files separately. To crop a skipped file after all, go back with ←
and press Enter. The crop replaces the uncropped export.

### Photos (JPEG)

Photos use the same workflow: drag the 16:10 (or square, S) rectangle and press Enter. There is
no playback, and the sidebar shows when and where the photo was taken.

* **Lossless crop.** If `jpegtran` is installed, the compressed image data is cut,
  not re-encoded, so quality is bit-identical to the original and the file
  shrinks in proportion to the area removed. A 37 MP Pixel photo takes about
  0.2 s. JPEG can only be cut on its 8×8 or 16×16 block grid, so the crop's
  top-left corner may shift by up to 15 px; the sidebar shows the exact area.
* **No downscaling**, except when the crop is wider than 8K (7680×4800, square
  7680×7680) or the
  file would reach 125 MB. Only then, or when `jpegtran` is missing, is the photo
  re-encoded. Re-encoding reuses the original's quantization tables and chroma
  subsampling, so quality and file size stay very close to the original.
* **Kept:** EXIF date taken *with time zone*, GPS geotag, camera make/model and
  settings, EXIF orientation (portrait photos stay portrait-tagged; the crop is
  landscape 16:10 or square as displayed), ICC colour profile (e.g. Display P3) and the
  rest of the XMP. Validation fails the export if the date or GPS goes missing.
* **Updated:** the EXIF pixel dimensions. The embedded EXIF thumbnail is
  detached, because it would still show the uncropped picture. The file date is
  set to when the photo was taken (or to the original's file date if the EXIF
  has no time zone).
* **Removed, because it describes the uncropped photo:**
  * the Ultra HDR gain map (Pixel/Android). The crop becomes a standard-range
    JPEG, which is what non-HDR screens show anyway.
  * Portrait depth map and relighting data, which would no longer line up with
    the photo
  * C2PA "Content Credentials", whose signature is invalid after any edit

### Closing while exports are running

Close the window or press Ctrl+Q. If nothing is exporting, the app exits at
once. Otherwise you can choose **Finish exports, then quit**, **Cancel exports and quit** (FFmpeg
is stopped and temp files are removed) or **Keep working**. Ctrl+C in the
terminal also stops FFmpeg and cleans up.

## 5. Design notes

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
changes.

**Background work.** Exports run one at a time through `QProcess`, which is
asynchronous, so the GUI never blocks. FFmpeg runs under `nice -n 10`, so the
preview stays smooth while encoding. Metadata (`ffprobe`) is prefetched for all
files in a background thread, so "Save & Next" is instant.

**Code layout**

```
video-cropper/
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

## 6. Tests

```bash
.venv/bin/pip install -r requirements.txt   # includes pytest
.venv/bin/python -m pytest                  # all tests (about 25 s)
.venv/bin/python -m pytest tests/test_geometry.py tests/test_encoding.py   # fast unit tests only
```

On Windows: `.venv\Scripts\python -m pytest`.

The integration tests generate small synthetic videos with FFmpeg: rotated,
anamorphic, HDR, no audio, two audio streams, and special characters in names.
They check that the exported pixels really come from the selected region and that
the size limit forces two-pass. The GUI tests run with `QT_QPA_PLATFORM=offscreen`
and need no display.

## 7. Troubleshooting

* **`Could not load the Qt platform plugin "xcb"`**: run `sudo apt install libxcb-cursor0`.
* **Photo shows "jpegtran not installed: photo is re-encoded"**: run
  `sudo apt install libjpeg-turbo-progs` (Windows: see installation) and restart the app.
* **"ffmpeg/ffprobe not found"**: run `sudo apt install ffmpeg`. On Windows, install
  FFmpeg (see above), then open a *new* terminal so the PATH is updated.
* **Windows: `python` opens the Microsoft Store**: install Python from python.org
  or with winget, or turn off the "App execution aliases" for python in Settings.
* **"Preview unavailable"**: Qt could not decode that file. Cropping and
  exporting still work, because export uses the system FFmpeg.
* **Slow 4K preview**: install VA-API drivers (see above). You can check that
  hardware decoding works with
  `QT_LOGGING_RULES="qt.multimedia.ffmpeg*=true" ./run.sh`.
* **Export failed**: double-click the job to see FFmpeg's error message.
