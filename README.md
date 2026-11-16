# VideoCrop 16:10

Get a folder of videos and photos ready for a digital photo frame, one file at
a time. For each file you either crop it (16:10, or square) or keep the whole
picture, and the app converts everything the frame can't play. Exports run in
the background, so the next file appears immediately.

You never type pixel sizes or encoder settings. Your originals are never changed.

**What comes out:**

* **Videos:** MP4 (H.264), at most 1080p, under 125 MB. That is what most photo
  frames accept.
* **Photos:** JPEG at full resolution, at most 8K, under 125 MB. Crops are
  lossless when `jpegtran` is installed.
* Date, time and GPS location are kept for both, so the frame and photo apps
  sort and place them correctly.

---

## 1. Installation

### Windows 10 / 11

> Windows support is new and has not been tested much yet. Please report
> anything that doesn't work.

1. **Install Python 3.10 or newer** from [python.org](https://www.python.org/downloads/).
   Tick **"Add python.exe to PATH"** in the installer. Or, in *PowerShell*:

   ```powershell
   winget install Python.Python.3.12
   ```

2. **Install FFmpeg** (it does the video conversion):

   ```powershell
   winget install Gyan.FFmpeg
   ```

   Without winget: download the "full" build from
   [gyan.dev](https://www.gyan.dev/ffmpeg/builds/), unzip it to e.g. `C:\ffmpeg`,
   and add `C:\ffmpeg\bin` to the PATH (Start → "Edit the system environment
   variables" → Environment Variables → Path → New).

3. *Optional, for lossless photo crops:* install `jpegtran`. Download
   `libjpeg-turbo-…-vc-x64.exe` from the
   [libjpeg-turbo releases](https://github.com/libjpeg-turbo/libjpeg-turbo/releases),
   install it and add its `bin` folder (e.g. `C:\libjpeg-turbo64\bin`) to the PATH.
   Without it, photo crops are re-encoded at the original quality instead.

4. **Close and reopen the terminal**, then check that everything is found:

   ```powershell
   python --version
   ffmpeg -version
   ```

5. **Download the app:** on GitHub click **Code → Download ZIP** and unzip it,
   or run `git clone https://github.com/xiaoyint/Cropodactyl.git`.

### Ubuntu 22.04

```bash
sudo apt update
sudo apt install ffmpeg python3-venv python3-pip libxcb-cursor0
sudo apt install libjpeg-turbo-progs   # optional: lossless photo crops
git clone https://github.com/xiaoyint/Cropodactyl.git
```

## 2. Start

**Windows:** double-click **`run.bat`** in the app folder.
**Ubuntu:** run `./run.sh` in the app folder.

The first start downloads the app's components (about 200 MB) and takes a
minute. Later starts are immediate.

The app asks for an **input folder** (your videos and photos) and an **output
folder** (default: a `cropped` folder inside the input folder). You can also
pass them directly:

```bash
./run.sh ~/Videos/trip -o ~/Videos/frame        # Ubuntu
.\run.bat "D:\Videos\Trip" -o "D:\Videos\Frame"  # Windows
```

## 3. How to use it

1. The first file is shown (videos play in a loop). The yellow rectangle is the
   crop. It starts as the largest 16:10 area that fits.
2. Drag the rectangle to move it and drag a corner to resize it. Press **S** for
   a square crop instead (it stays on for the next files until you press S
   again). Press **P** to preview the result.
3. Choose what happens to the file:
   * **Enter**: export the crop and show the next file.
   * **→**: don't crop. The whole file is exported instead (see
     [Skipping the crop](#skipping-the-crop)).
4. Repeat until every file is reviewed. A summary appears when all exports are
   done.

The sidebar shows information about the current file and the export queue.
Double-click a job for details, or cancel jobs from there.

### Keyboard shortcuts

| Key | Action |
|---|---|
| Enter | Export the crop, next file |
| → | Skip the crop (export the whole file), next file |
| ← | Previous file (its crop is remembered) |
| S | Switch between 16:10 and square |
| P | Preview the crop |
| R | Reset the crop |
| Esc | Undo the last crop change |
| Mouse wheel, `+` / `-` | Make the crop bigger / smaller |
| Shift + arrows | Nudge the crop (Ctrl + arrows: tiny steps) |
| Space | Play / pause |
| M | Mute |
| Ctrl+Q | Quit |

### Skipping the crop

Pressing **→** keeps the whole picture, but the file still ends up in the
output folder in a form the frame accepts:

* Files the frame can already show are **copied unchanged**.
* Others are **converted**: videos to MP4 (H.264, at most 1080p, under 125 MB),
  photos larger than 8K are scaled down. The status bar says why.

Phone videos from Pixel phones (and many others) use the HEVC format, so they
are always converted. 50 MP photos are just over 8K and are scaled down slightly.

Changed your mind? Go back with **←** and press **Enter** to crop the file
after all; the crop replaces the uncropped export. Going back and pressing →
again just moves on and never replaces an export.

### Good to know

* **Output names** stay the same as the original (`clip.mov` → `clip.mp4`,
  photos keep their name). If a name is taken, you get `clip (1).mp4`.
* **Supported files:** videos `.mp4 .m4v .mov .mkv .webm .avi` and photos
  `.jpg .jpeg`, reviewed together in filename order.
* **Lossless photo crops** can shift the crop's corner by a few pixels; the
  sidebar shows the exact area.
* **Not kept in cropped or converted photos:** Ultra HDR brightness data and
  Portrait-mode depth data. The picture itself looks the same on a normal screen.
* **HDR videos** (e.g. from newer phones) are converted to normal colours so
  they don't look washed out.
* **Very long videos** (around 2 hours) can't fit under 125 MB and are refused,
  with an explanation.
* **Quitting while exports run:** the app asks whether to finish them first,
  cancel them, or keep working.

## 4. Troubleshooting

* **"ffmpeg/ffprobe not found"**: install FFmpeg (see above). On Windows, open a
  *new* terminal afterwards so the PATH is updated.
* **Windows: `python` opens the Microsoft Store**: install Python from python.org
  or with winget, or turn off the "App execution aliases" for python in Settings.
* **Ubuntu: `Could not load the Qt platform plugin "xcb"`**: run
  `sudo apt install libxcb-cursor0`.
* **"jpegtran not installed: photo is re-encoded"**: install jpegtran (see
  above) and restart the app.
* **"Preview unavailable"**: the preview can't show that file, but cropping and
  exporting still work.
* **Slow 4K preview on Ubuntu**: install video drivers with
  `sudo apt install va-driver-all`.
* **Preview stutters while exporting (Windows)**: in Task Manager → Details,
  right-click `ffmpeg.exe` → Set priority → Below normal.
* **An export failed**: double-click the job in the sidebar to see why.

---

Developer notes (export details, design, code layout, tests):
[DEVELOPMENT.md](DEVELOPMENT.md).
