import os
import shutil
import subprocess

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe not installed")


def run(args):
    p = subprocess.run(args, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p


def quadrant_filter(w, h, fps=30):
    """Four solid colour quadrants: red | green / blue | white."""
    hw, hh = w // 2, h // 2
    return (f"color=c=red:s={hw}x{hh}:r={fps}[a];color=c=lime:s={hw}x{hh}:r={fps}[b];"
            f"color=c=blue:s={hw}x{hh}:r={fps}[c];color=c=white:s={hw}x{hh}:r={fps}[d];"
            "[a][b]hstack[top];[c][d]hstack[bot];[top][bot]vstack,format=yuv420p")


def make_video(path, w=640, h=360, duration=2.0, fps=30, audio=1, noisy=False, noise=12,
               extra_out=(), sar=None, rotation=None, hdr=False):
    """Create a synthetic test video with ffmpeg's lavfi sources."""
    vf = quadrant_filter(w, h, fps)
    if noisy:
        vf = (f"testsrc2=s={w}x{h}:r={fps},noise=alls={noise}:allf=t,format=yuv420p")
    if sar:
        vf += f",setsar={sar}"
    args = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", vf]
    for i in range(audio):
        args += ["-f", "lavfi", "-i", f"sine=frequency={440 * (i + 1)}:sample_rate=48000"]
    args += ["-t", str(duration), "-map", "0:v"]
    for i in range(audio):
        args += ["-map", f"{i + 1}:a"]
    if hdr:
        args += ["-c:v", "libx264", "-profile:v", "high10", "-pix_fmt", "yuv420p10le",
                 "-color_trc", "arib-std-b67", "-color_primaries", "bt2020", "-colorspace", "bt2020nc"]
    else:
        args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac", "-b:a", "128k", "-ac", "2"]
        if audio > 1:
            args += ["-c:a:1", "libmp3lame" if _has_encoder("libmp3lame") else "ac3"]
    args += [*extra_out, str(path)]
    run(args)
    if rotation:
        rotated = str(path) + ".rot.mp4"
        p = subprocess.run(["ffmpeg", "-v", "error", "-y", "-display_rotation", str(rotation),
                            "-i", str(path), "-c", "copy", rotated], capture_output=True, text=True)
        if p.returncode != 0:  # ffmpeg < 6: legacy rotate tag
            run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-c", "copy",
                 "-metadata:s:v:0", f"rotate={rotation}", rotated])
        os.replace(rotated, path)
    return str(path)


def _has_encoder(name):
    p = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True)
    return name in p.stdout


@pytest.fixture(scope="session")
def caps():
    if not HAVE_FFMPEG:
        pytest.skip("ffmpeg not installed")
    from videocrop import encoding
    return encoding.detect_caps()
