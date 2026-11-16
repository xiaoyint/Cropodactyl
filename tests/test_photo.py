"""JPEG photo cropping: orientation mapping, lossless/re-encode export, metadata."""
import dataclasses
import os
import shutil

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image, ImageChops, ImageOps, ImageStat  # noqa: E402
from PIL.TiffImagePlugin import IFDRational as R  # noqa: E402

from videocrop import encoding as enc  # noqa: E402
from videocrop import files  # noqa: E402
from videocrop import geometry as g  # noqa: E402
from videocrop import photo  # noqa: E402
from videocrop.geometry import Crop  # noqa: E402

HAVE_JPEGTRAN = bool(shutil.which("jpegtran"))
TAKEN, OFFSET = "2026:09:25 09:01:46", "+09:00"
GPS = {1: "N", 2: (R(35), R(55), R(5252, 100)), 3: "E", 4: (R(137), R(47), R(689, 100))}


def caps(jpegtran=True):
    return enc.FFmpegCaps(ffmpeg="ffmpeg", ffprobe="ffprobe", version="t", fps_mode_flag="-fps_mode",
                          has_zscale=True, has_tonemap=True, nice=None,
                          jpegtran=shutil.which("jpegtran") if jpegtran else None)


def make_photo(path, w=960, h=600, orientation=1, subsampling=2, quality=92, gps=True, taken=True):
    """Detailed test photo (gradient + coloured quadrants + noise) with EXIF."""
    base = Image.radial_gradient("L").resize((w, h)).convert("RGB")
    quad = Image.new("RGB", (w, h))
    for i, col in enumerate([(220, 40, 40), (40, 200, 40), (40, 40, 220), (230, 230, 230)]):
        quad.paste(col, ((i % 2) * (w // 2), (i // 2) * (h // 2), (i % 2 + 1) * (w // 2), (i // 2 + 1) * (h // 2)))
    noise = Image.effect_noise((w, h), 40).convert("RGB")
    im = Image.blend(Image.blend(base, quad, 0.6), noise, 0.15)
    exif = Image.Exif()
    exif[0x0112] = orientation
    exif[0x010F] = "Google"
    exif[0x0110] = "Pixel 8 Pro"
    ex = exif.get_ifd(0x8769)
    if taken:
        ex[0x9003] = TAKEN
        ex[0x9011] = OFFSET
    ex[0xA002] = w
    ex[0xA003] = h
    if gps:
        exif.get_ifd(0x8825).update(GPS)
    im.save(path, "JPEG", quality=quality, subsampling=subsampling, exif=exif)
    if (taken or gps) and not photo.read_exif_summary(str(path))["taken" if taken else "gps"]:
        pytest.skip("this Pillow version cannot write EXIF sub-IFDs for test photos")
    return str(path)


def export(tmp_path, src, crop=None, jpegtran=True, limits=None):
    info = photo.probe_photo(src)
    crop = crop or g.initial_crop(info.display_w, info.display_h)
    plan = photo.make_photo_plan(info, crop, caps(jpegtran), limits or enc.Limits())
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    final = tmp_path / "out" / os.path.basename(src)
    out = enc.run_session_blocking(plan.make_session(caps(jpegtran), str(work), str(final)))
    assert out.success, out.message
    return plan, out


def displayed(path):
    with Image.open(path) as im:
        return ImageOps.exif_transpose(im).convert("RGB")


def mean_diff(a, b):
    return sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3


# ----------------------------------------------------------------- orientation mapping

@pytest.mark.parametrize("orientation", range(1, 9))
def test_display_rect_maps_to_same_pixels(tmp_path, orientation):
    """Cropping the stored image at the mapped rect and then applying EXIF
    orientation must equal cropping what the viewer sees."""
    src = make_photo(tmp_path / "o.jpg", 320, 200, orientation=orientation, subsampling=0, quality=100)
    with Image.open(src) as raw:
        raw = raw.convert("RGB")
        raw.info["exif"] = None
        shown = displayed(src)
        x, y, w, h = 30, 20, 96, 60
        if shown.size[0] < shown.size[1]:
            x, y = 10, 40
        sx, sy, sw, sh = photo.display_to_stored_rect(x, y, w, h, raw.size[0], raw.size[1], orientation)
        part = raw.crop((sx, sy, sx + sw, sy + sh))
        method = {2: Image.FLIP_LEFT_RIGHT, 3: Image.ROTATE_180, 4: Image.FLIP_TOP_BOTTOM,
                  5: Image.TRANSPOSE, 6: Image.ROTATE_270, 7: Image.TRANSVERSE, 8: Image.ROTATE_90}
        if orientation in method:
            part = part.transpose(method[orientation])
        assert part.size == (w, h)
        assert mean_diff(part, shown.crop((x, y, x + w, y + h))) < 0.01
        back = photo.stored_to_display_rect(sx, sy, sw, sh, raw.size[0], raw.size[1], orientation)
        assert back == (x, y, w, h)


# ----------------------------------------------------------------- planning

def test_plan_pixel_size_photo_is_native_and_aligned(tmp_path):
    info = dataclasses.replace(photo.probe_photo(make_photo(tmp_path / "p.jpg")),
                               stored_w=8160, stored_h=4590, display_w=8160, display_h=4590)
    plan = photo.make_photo_plan(info, g.initial_crop(8160, 4590), caps())
    assert (plan.out_w, plan.out_h) == (7344, 4590)  # largest 16:10, not downscaled
    assert not plan.scaled
    assert plan.src_crop.x % 16 == 0 and plan.src_crop.y % 16 == 0
    assert plan.output_ext == ".jpg"


def test_plan_over_8k_is_downscaled(tmp_path):
    info = dataclasses.replace(photo.probe_photo(make_photo(tmp_path / "p.jpg")),
                               stored_w=12000, stored_h=8000, display_w=12000, display_h=8000)
    plan = photo.make_photo_plan(info, g.initial_crop(12000, 8000), caps())
    assert (plan.out_w, plan.out_h) == (7680, 4800)
    assert plan.scaled and not plan.lossless
    assert any("8K" in w for w in plan.warnings)


def test_plan_444_uses_8px_grid(tmp_path):
    info = photo.probe_photo(make_photo(tmp_path / "p.jpg", subsampling=0))
    assert (info.mcu_w, info.mcu_h) == (8, 8) and info.subsampling == "4:4:4"
    plan = photo.make_photo_plan(info, Crop(13, 9, 30), caps())
    assert plan.src_crop.x % 8 == 0 and plan.src_crop.y % 8 == 0


# ----------------------------------------------------------------- export (re-encode path)

def check_metadata(src, out_path, orientation=1):
    s, o = photo.read_exif_summary(src), photo.read_exif_summary(out_path)
    assert o["taken"] == TAKEN and o["offset"] == OFFSET
    assert o["gps_raw"] == s["gps_raw"] and o["gps"] == pytest.approx(s["gps"])
    assert o["orientation"] == orientation
    assert o["camera"] == "Google Pixel 8 Pro"
    with Image.open(out_path) as im:
        ex = im.getexif().get_ifd(0x8769)
        assert (ex[0xA002], ex[0xA003]) == im.size  # EXIF dimensions updated


def test_reencode_export_keeps_metadata_quality_and_date(tmp_path):
    src = make_photo(tmp_path / "PXL_1.jpg", 960, 720)
    plan, out = export(tmp_path, src, Crop(64, 100, 50), jpegtran=False)
    assert out.method.startswith("re-encoded")
    assert out.final_path.endswith("PXL_1.jpg")
    with Image.open(out.final_path) as im:
        assert im.format == "JPEG" and im.size == (800, 500)
    check_metadata(src, out.final_path)
    # quality close to the original crop (same quantization tables)
    c = plan.src_crop
    ref = displayed(src).crop((c.x, c.y, c.x + c.w, c.y + c.h))
    assert mean_diff(ref, displayed(out.final_path)) < 2.0
    # file date = time taken (09:01:46 at +09:00 = 00:01:46 UTC)
    assert int(os.stat(out.final_path).st_mtime) == int(photo.taken_timestamp(TAKEN, OFFSET))
    assert photo.taken_timestamp(TAKEN, OFFSET) == 1790294506.0


def test_rotated_photo_export_is_landscape_16_10(tmp_path):
    src = make_photo(tmp_path / "rot.jpg", 960, 600, orientation=6)  # shown as 600x960 portrait
    info = photo.probe_photo(src)
    assert (info.display_w, info.display_h) == (600, 960)
    plan, out = export(tmp_path, src, jpegtran=False)
    shown = displayed(out.final_path)
    assert shown.size[0] * 10 == shown.size[1] * 16 and shown.size[0] > shown.size[1]
    check_metadata(src, out.final_path, orientation=6)


def test_size_limit_forces_downscale(tmp_path):
    src = make_photo(tmp_path / "big.jpg", 1600, 1000, quality=100, subsampling=0)
    limits = enc.Limits(hard_limit=120_000, target=110_000)
    plan, out = export(tmp_path, src, jpegtran=False, limits=limits)
    assert out.size < limits.hard_limit
    with Image.open(out.final_path) as im:
        w, h = im.size
    assert w * 10 == h * 16 and w < 1600
    check_metadata(src, out.final_path)


def test_photo_without_date_keeps_original_file_time(tmp_path):
    src = make_photo(tmp_path / "nodate.jpg", taken=False, gps=False)
    os.utime(src, (1_600_000_000, 1_600_000_000))
    _, out = export(tmp_path, src, jpegtran=False)
    assert int(os.stat(out.final_path).st_mtime) == 1_600_000_000


# ----------------------------------------------------------------- export (lossless path)

@pytest.mark.skipif(not HAVE_JPEGTRAN, reason="jpegtran not installed (libjpeg-turbo-progs)")
@pytest.mark.parametrize("orientation", [1, 6])
def test_lossless_export_is_pixel_identical(tmp_path, orientation):
    src = make_photo(tmp_path / "ll.jpg", 960, 640, orientation=orientation)
    info = photo.probe_photo(src)
    plan, out = export(tmp_path, src, g.zoomed(g.initial_crop(info.display_w, info.display_h),
                                               -3, info.display_w, info.display_h))
    assert out.method == "lossless"
    c = plan.src_crop
    with Image.open(src) as a, Image.open(out.final_path) as b:
        ref = a.convert("RGB").crop((c.x, c.y, c.x + c.w, c.y + c.h))
        got = b.convert("RGB")
        assert got.size == ref.size
        # identical DCT data: only chroma upsampling at the new edges may differ
        inner = (16, 16, ref.size[0] - 16, ref.size[1] - 16)
        assert mean_diff(ref.crop(inner), got.crop(inner)) < 0.05
    check_metadata(src, out.final_path, orientation)
    shown = displayed(out.final_path)
    assert shown.size[0] * 10 == shown.size[1] * 16


# ----------------------------------------------------------------- scanning / naming

def test_scan_includes_jpegs_and_keeps_extension(tmp_path):
    for n in ["b.JPG", "a.mp4", "c.jpeg", "d.png"]:
        (tmp_path / n).write_text("x")
    names = [os.path.basename(p) for p in files.scan_videos(str(tmp_path))]
    assert names == ["a.mp4", "b.JPG", "c.jpeg"]
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "b.JPG").write_text("x")
    assert files.unique_output_path(str(tmp_path / "out"), "/in/b.JPG", set(), ext=".JPG") \
        == str(tmp_path / "out" / "b (1).JPG")


# ----------------------------------------------------------------- square (1:1)

@pytest.mark.parametrize("jpegtran", [False, pytest.param(True, marks=pytest.mark.skipif(
    not HAVE_JPEGTRAN, reason="jpegtran not installed"))])
@pytest.mark.parametrize("orientation", [1, 6])
def test_square_photo_export(tmp_path, jpegtran, orientation):
    src = make_photo(tmp_path / "sq.jpg", 960, 640, orientation=orientation)
    info = photo.probe_photo(src)
    crop = g.initial_crop(info.display_w, info.display_h, g.SQUARE)
    plan, out = export(tmp_path, src, crop, jpegtran=jpegtran)
    assert out.method == "lossless" if jpegtran else out.method.startswith("re-encoded")
    shown = displayed(out.final_path)
    assert shown.size[0] == shown.size[1] == min(info.display_w, info.display_h)
    check_metadata(src, out.final_path, orientation)


def test_square_photo_over_8k_downscaled(tmp_path):
    info = dataclasses.replace(photo.probe_photo(make_photo(tmp_path / "p.jpg")),
                               stored_w=12000, stored_h=9000, display_w=12000, display_h=9000)
    plan = photo.make_photo_plan(info, g.initial_crop(12000, 9000, g.SQUARE), caps())
    assert (plan.out_w, plan.out_h) == (7680, 7680) and plan.scaled
    info = dataclasses.replace(info, stored_w=8160, stored_h=6120, display_w=8160, display_h=6120)
    plan = photo.make_photo_plan(info, g.initial_crop(8160, 6120, g.SQUARE), caps())
    assert (plan.out_w, plan.out_h) == (6120, 6120) and not plan.scaled  # native
