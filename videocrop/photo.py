"""JPEG photos: probing, orientation-aware 16:10 crop, lossless export.

Export strategy
---------------
* **Lossless** (default): ``jpegtran -crop`` cuts the compressed data without
  re-encoding, so quality is bit-identical and *all* metadata (EXIF date/GPS/
  camera, ICC profile, XMP) is copied unchanged.  The crop's top-left corner
  is snapped to the JPEG block grid (8 or 16 px), which jpegtran requires.
* **Re-encode** only when unavoidable: the crop is larger than 8K (7680x4800)
  and must be downscaled, the result would be >= 125 MB, or jpegtran is not
  installed.  The photo's own quantization tables and chroma subsampling are
  reused, so quality and file size stay close to the original.

The EXIF Orientation tag is kept; the crop chosen on the (rotated) screen
image is mapped to stored pixel coordinates.  Afterwards the EXIF pixel
dimensions are updated and the embedded thumbnail (which would still show the
uncropped photo) is detached.  Everything is patched in place, so maker notes
and GPS data are never re-serialised.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import struct
import sys
from dataclasses import dataclass, field

from . import geometry as g
from .geometry import Crop, SourceCrop
from .probe import ProbeError

IMAGE_EXTENSIONS = {".jpg", ".jpeg"}
TAG_ORIENTATION = 0x0112
TAG_EXIF_IFD = 0x8769
TAG_GPS_IFD = 0x8825
TAG_DATETIME = 0x0132
TAG_DATETIME_ORIGINAL = 0x9003
TAG_OFFSET_ORIGINAL = 0x9011
TAG_PIXEL_X = 0xA002
TAG_PIXEL_Y = 0xA003
REENCODE_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "photo_reencode.py")


def is_photo(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in IMAGE_EXTENSIONS


# --------------------------------------------------------------------------
# Info
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class PhotoInfo:
    path: str
    stored_w: int
    stored_h: int
    orientation: int  # EXIF 1..8
    display_w: int
    display_h: int
    mcu_w: int
    mcu_h: int
    subsampling: str
    progressive: bool
    taken: str | None  # EXIF DateTimeOriginal "YYYY:MM:DD HH:MM:SS"
    taken_offset: str | None  # EXIF OffsetTimeOriginal "+09:00"
    gps: tuple[float, float] | None
    camera: str | None
    size_bytes: int = 0
    ultra_hdr: bool = False  # has an HDR gain map (Pixel/Android "Ultra HDR")
    depth_data: bool = False  # Portrait depth map / relighting data in XMP
    kind: str = "photo"

    @property
    def duration(self) -> float:  # queue progress code expects it
        return 0.0

    @property
    def is_portrait(self) -> bool:
        return self.display_h > self.display_w


def _ratio(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError, ZeroDivisionError):
        try:
            return v[0] / v[1]
        except Exception:
            return 0.0


def _gps_decimal(gps: dict) -> tuple[float, float] | None:
    try:
        lat = sum(_ratio(x) / 60 ** i for i, x in enumerate(gps[2]))
        lon = sum(_ratio(x) / 60 ** i for i, x in enumerate(gps[4]))
    except (KeyError, TypeError, IndexError):
        return None
    if str(gps.get(1, "N")).upper().startswith("S"):
        lat = -lat
    if str(gps.get(3, "E")).upper().startswith("W"):
        lon = -lon
    return (lat, lon)


def _clean(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, bytes):
        v = v.decode("ascii", "replace")
    v = str(v).strip("\x00 ").strip()
    return v or None


def read_exif_summary(path: str) -> dict:
    """Date, offset, GPS, orientation – used for probing and validation."""
    from PIL import Image
    with Image.open(path) as im:
        exif = im.getexif()
        ex = exif.get_ifd(TAG_EXIF_IFD)
        gps = exif.get_ifd(TAG_GPS_IFD)
        return {
            "format": im.format,
            "size": im.size,
            "orientation": int(exif.get(TAG_ORIENTATION, 1) or 1),
            "taken": _clean(ex.get(TAG_DATETIME_ORIGINAL)) or _clean(exif.get(TAG_DATETIME)),
            "offset": _clean(ex.get(TAG_OFFSET_ORIGINAL)),
            "gps_raw": {k: gps[k] for k in (1, 2, 3, 4) if k in gps},
            "gps": _gps_decimal(gps) if gps else None,
            "camera": " ".join(x for x in (_clean(exif.get(0x010F)), _clean(exif.get(0x0110))) if x) or None,
        }


def probe_photo(path: str) -> PhotoInfo:
    try:
        from PIL import Image
    except ImportError as e:
        raise ProbeError("Pillow is not installed (pip install Pillow)") from e
    try:
        with Image.open(path) as im:
            if im.format not in ("JPEG", "MPO"):  # MPO: JPEG + extra images (gain map)
                raise ProbeError(f"not a JPEG file ({im.format})")
            w, h = im.size
            layers = getattr(im, "layer", None) or []
            hs = max((l[1] for l in layers), default=1)
            vs = max((l[2] for l in layers), default=1)
            if len(layers) == 1:  # greyscale: MCU is one 8x8 block
                hs = vs = 1
            sub = {(2, 2): "4:2:0", (2, 1): "4:2:2", (1, 1): "4:4:4"}.get((hs, vs), f"{hs}x{vs}")
            progressive = bool(im.info.get("progressive") or im.info.get("progression"))
        meta = read_exif_summary(path)
    except ProbeError:
        raise
    except Exception as e:
        raise ProbeError(f"cannot read image: {e}") from e
    with open(path, "rb") as f:
        head = f.read(512 * 1024)
    ultra_hdr = b"hdrgm:" in head or b"MPF\x00" in head
    depth = b"DepthMap" in head or b"GDepth" in head or b"RelitInputImage" in head
    o = meta["orientation"] if meta["orientation"] in range(1, 9) else 1
    dw, dh = (h, w) if o in (5, 6, 7, 8) else (w, h)
    if g.max_k(dw, dh) < 1:
        raise ProbeError(f"image too small for a 16:10 crop ({dw}x{dh})")
    return PhotoInfo(
        path=path, stored_w=w, stored_h=h, orientation=o, display_w=dw, display_h=dh,
        mcu_w=8 * hs, mcu_h=8 * vs, subsampling=sub, progressive=progressive,
        taken=meta["taken"], taken_offset=meta["offset"], gps=meta["gps"],
        camera=meta["camera"], size_bytes=os.path.getsize(path),
        ultra_hdr=ultra_hdr, depth_data=depth)


# --------------------------------------------------------------------------
# Orientation mapping
# --------------------------------------------------------------------------

def display_to_stored_rect(x: int, y: int, w: int, h: int, sw: int, sh: int,
                           orientation: int) -> tuple[int, int, int, int]:
    """Map a rectangle on the displayed (EXIF-rotated) image to stored pixels.

    *sw*, *sh* are the stored image dimensions.
    """
    o = orientation
    if o == 2:
        return sw - x - w, y, w, h
    if o == 3:
        return sw - x - w, sh - y - h, w, h
    if o == 4:
        return x, sh - y - h, w, h
    if o == 5:
        return y, x, h, w
    if o == 6:
        return y, sh - x - w, h, w
    if o == 7:
        return sw - y - h, sh - x - w, h, w
    if o == 8:
        return sw - y - h, x, h, w
    return x, y, w, h


def stored_to_display_rect(x: int, y: int, w: int, h: int, sw: int, sh: int,
                           orientation: int) -> tuple[int, int, int, int]:
    inverse = {6: 8, 8: 6}.get(orientation, orientation)
    dw, dh = (sh, sw) if orientation in (5, 6, 7, 8) else (sw, sh)
    return display_to_stored_rect(x, y, w, h, dw, dh, inverse)


# --------------------------------------------------------------------------
# Plan
# --------------------------------------------------------------------------

@dataclass
class PhotoPlan:
    source: str
    info: PhotoInfo
    crop: Crop | None  # None: whole photo, no crop (skipped in the GUI)
    src_crop: SourceCrop  # stored pixels, top-left on the block grid
    out_w: int  # displayed output size (exact 16:10 or 1:1 when cropped)
    out_h: int
    stored_out_w: int
    stored_out_h: int
    scaled: bool
    lossless: bool
    limits: object
    warnings: list[str] = field(default_factory=list)

    @property
    def output_ext(self) -> str:
        return os.path.splitext(self.source)[1] or ".jpg"

    @property
    def method_text(self) -> str:
        if self.lossless:
            return "lossless crop"
        if self.scaled:
            return "downscaled to 8K, re-encoded"
        return "re-encoded (original quality)"

    def make_session(self, caps, tmp_dir: str, final_path: str, may_overwrite: bool = False):
        return PhotoSession(self, caps, tmp_dir, final_path, may_overwrite)


def make_photo_plan(info: PhotoInfo, crop: Crop, caps, limits=None) -> PhotoPlan:
    from .encoding import Limits
    limits = limits or Limits()
    crop = g.clamp_crop(crop, info.display_w, info.display_h)
    sx, sy, sw, sh = display_to_stored_rect(crop.x, crop.y, crop.w, crop.h,
                                            info.stored_w, info.stored_h, info.orientation)
    sx -= sx % info.mcu_w  # jpegtran needs the top-left on the block grid
    sy -= sy % info.mcu_h
    out_w, out_h = g.output_size(crop, g.PHOTO_BOX)
    scaled = g.is_downscaled(crop, g.PHOTO_BOX)
    stored_out = (out_h, out_w) if info.orientation in (5, 6, 7, 8) else (out_w, out_h)
    jpegtran = getattr(caps, "jpegtran", None) if caps is not None else None
    lossless = not scaled and bool(jpegtran)
    warnings = []
    if scaled:
        warnings.append(f"crop larger than 8K: downscaled to {out_w}x{out_h} and re-encoded")
    elif not jpegtran:
        warnings.append("jpegtran not installed: photo is re-encoded instead of cropped losslessly "
                        "(install libjpeg-turbo-progs)")
    if out_h < g.SMALL_OUTPUT_HEIGHT:
        warnings.append(f"small crop: output is only {out_w}x{out_h}")
    if info.ultra_hdr:
        warnings.append("Ultra HDR gain map is not kept: the crop is a standard (SDR) JPEG")
    if info.depth_data:
        warnings.append("Portrait depth/relighting data is not kept (it would not match the crop)")
    return PhotoPlan(source=info.path, info=info, crop=crop,
                     src_crop=SourceCrop(sx, sy, sw, sh), out_w=out_w, out_h=out_h,
                     stored_out_w=stored_out[0], stored_out_h=stored_out[1],
                     scaled=scaled, lossless=lossless, limits=limits, warnings=warnings)


def photo_conversion_reasons(info: PhotoInfo, limits=None) -> list[str]:
    """Why this photo cannot go out unchanged (empty: copy it as is)."""
    from .encoding import Limits
    limits = limits or Limits()
    reasons = []
    if not g.fits_box(info.display_w, info.display_h, g.PHOTO_BOX):
        reasons.append(f"larger than 8K ({info.display_w}x{info.display_h})")
    if info.size_bytes >= limits.hard_limit:
        reasons.append(f"{info.size_bytes / 1e6:.0f} MB")
    return reasons


def make_photo_full_plan(info: PhotoInfo, caps, limits=None) -> PhotoPlan:
    """Re-encode the whole photo (no crop): downscaled only if it is larger
    than 8K, and further only if the result would be >= 125 MB."""
    from .encoding import Limits
    limits = limits or Limits()
    out_w, out_h = g.fit_size(info.display_w, info.display_h, g.PHOTO_BOX, even=False)
    scaled = (out_w, out_h) != (info.display_w, info.display_h)
    stored_out = (out_h, out_w) if info.orientation in (5, 6, 7, 8) else (out_w, out_h)
    warnings = []
    if scaled:
        warnings.append(f"larger than 8K: downscaled to {out_w}x{out_h} and re-encoded")
    if info.ultra_hdr:
        warnings.append("Ultra HDR gain map is not kept: the result is a standard (SDR) JPEG")
    if info.depth_data:
        warnings.append("Portrait depth/relighting data is not kept")
    return PhotoPlan(source=info.path, info=info, crop=None,
                     src_crop=SourceCrop(0, 0, info.stored_w, info.stored_h),
                     out_w=out_w, out_h=out_h,
                     stored_out_w=stored_out[0], stored_out_h=stored_out[1],
                     scaled=scaled, lossless=False, limits=limits, warnings=warnings)


# --------------------------------------------------------------------------
# Header cleanup: drop data that describes the *uncropped* photo
# --------------------------------------------------------------------------

_XMP_MAIN = b"http://ns.adobe.com/xap/1.0/\x00"
_XMP_EXT = b"http://ns.adobe.com/xmp/extension/\x00"


def _clean_xmp(packet: bytes) -> bytes:
    text = packet.decode("utf-8", "replace")
    text = re.sub(r"<Container:Directory>.*?</Container:Directory>", "", text, flags=re.S)
    text = re.sub(r"\s+(hdrgm:\w+|xmpNote:HasExtendedXMP)=\"[^\"]*\"", "", text)
    text = re.sub(r"<hdrgm:\w+>.*?</hdrgm:\w+>", "", text, flags=re.S)
    return text.encode("utf-8")


def clean_jpeg(path: str) -> list[str]:
    """Remove metadata that no longer matches a cropped photo.

    Dropped: MPF index of extra images (Ultra HDR gain map, which jpegtran /
    re-encoding does not carry over anyway), extended XMP (Portrait depth map,
    relighting data, HDR+ maker note), the Ultra HDR / container description in
    the main XMP, and APP11 JUMBF (C2PA content credentials, whose signature is
    invalid after editing).  EXIF, ICC profile and the rest of XMP are kept.
    Returns the names of what was removed.
    """
    with open(path, "rb") as f:
        data = f.read()
    if data[:2] != b"\xff\xd8":
        return []
    out = bytearray(b"\xff\xd8")
    removed: list[str] = []
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            return []  # unexpected layout: leave the file untouched
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker == 0xDA:  # start of scan: copy the rest unchanged
            out += data[i:]
            break
        (length,) = struct.unpack(">H", data[i + 2:i + 4])
        seg = data[i + 4:i + 2 + length]
        keep = True
        if marker == 0xE2 and seg.startswith(b"MPF\x00"):
            keep, why = False, "MPF index (Ultra HDR gain map)"
        elif marker == 0xEB:
            keep, why = False, "C2PA content credentials"
        elif marker == 0xE1 and seg.startswith(_XMP_EXT):
            keep, why = False, "extended XMP (depth map / relighting / HDR+ data)"
        elif marker == 0xE1 and seg.startswith(_XMP_MAIN):
            packet = _clean_xmp(seg[len(_XMP_MAIN):])
            seg = _XMP_MAIN + packet
            if len(seg) + 2 <= 0xFFFF:
                out += b"\xff\xe1" + struct.pack(">H", len(seg) + 2) + seg
                i += 2 + length
                continue
        if keep:
            out += data[i:i + 2 + length]
        elif why not in removed:
            removed.append(why)
        i += 2 + length
    if removed or len(out) != len(data):
        tmp = path + ".clean.tmp"
        with open(tmp, "wb") as f:
            f.write(out)
        os.replace(tmp, path)
    return removed


# --------------------------------------------------------------------------
# EXIF patching (in place, no re-serialisation)
# --------------------------------------------------------------------------

def _find_exif_segment(f) -> tuple[int, bytes] | None:
    """(offset of TIFF header in file, APP1 payload after 'Exif\\0\\0')."""
    f.seek(0)
    if f.read(2) != b"\xff\xd8":
        return None
    while True:
        b = f.read(1)
        while b == b"\xff":
            b = f.read(1)
        if not b:
            return None
        marker = b[0]
        if marker in (0xD9, 0xDA):  # EOI / start of scan: no more headers
            return None
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            continue
        (length,) = struct.unpack(">H", f.read(2))
        start = f.tell()
        data = f.read(length - 2)
        if marker == 0xE1 and data.startswith(b"Exif\x00\x00"):
            return start + 6, data[6:]
        f.seek(start + length - 2)


def patch_exif(path: str, width: int, height: int) -> bool:
    """Update EXIF pixel dimensions and detach the thumbnail, in place."""
    with open(path, "r+b") as f:
        found = _find_exif_segment(f)
        if found is None:
            return False
        base, tiff = found
        tiff = bytearray(tiff)
        if tiff[:2] == b"II":
            e = "<"
        elif tiff[:2] == b"MM":
            e = ">"
        else:
            return False

        def entries(off):
            if off + 2 > len(tiff):
                return off, []
            (n,) = struct.unpack(e + "H", tiff[off:off + 2])
            out = []
            for i in range(n):
                p = off + 2 + 12 * i
                if p + 12 > len(tiff):
                    break
                tag, typ, cnt = struct.unpack(e + "HHI", tiff[p:p + 8])
                out.append((tag, typ, cnt, p))
            return off + 2 + 12 * n, out

        def set_dim(p, typ, cnt, value):
            if cnt != 1:
                return
            if typ == 3 and value < 65536:  # SHORT
                tiff[p + 8:p + 12] = struct.pack(e + "HH", value, 0)
            elif typ == 4:  # LONG
                tiff[p + 8:p + 12] = struct.pack(e + "I", value)

        (ifd0,) = struct.unpack(e + "I", tiff[4:8])
        next_ptr, ifd0_entries = entries(ifd0)
        for tag, typ, cnt, p in ifd0_entries:
            if tag == 0x0100:
                set_dim(p, typ, cnt, width)
            elif tag == 0x0101:
                set_dim(p, typ, cnt, height)
            elif tag == TAG_EXIF_IFD:
                (exif_off,) = struct.unpack(e + "I", tiff[p + 8:p + 12])
                for t2, ty2, c2, p2 in entries(exif_off)[1]:
                    if t2 == TAG_PIXEL_X:
                        set_dim(p2, ty2, c2, width)
                    elif t2 == TAG_PIXEL_Y:
                        set_dim(p2, ty2, c2, height)
        if next_ptr + 4 <= len(tiff):
            tiff[next_ptr:next_ptr + 4] = b"\x00\x00\x00\x00"  # drop IFD1 (thumbnail)
        f.seek(base)
        f.write(tiff)
    return True


# --------------------------------------------------------------------------
# Validation & file date
# --------------------------------------------------------------------------

def taken_timestamp(taken: str | None, offset: str | None) -> float | None:
    """Unix time of EXIF DateTimeOriginal; None without a time-zone offset."""
    if not taken or not offset:
        return None
    try:
        t = dt.datetime.strptime(taken[:19], "%Y:%m:%d %H:%M:%S")
        sign = -1 if offset.strip().startswith("-") else 1
        hh, mm = offset.strip().lstrip("+-").split(":")[:2]
        tz = dt.timezone(sign * dt.timedelta(hours=int(hh), minutes=int(mm)))
    except (ValueError, IndexError):
        return None
    if t.year < 1980:
        return None
    return t.replace(tzinfo=tz).timestamp()


def validate_photo(path: str, plan: PhotoPlan, file_size: int) -> list[str]:
    errors: list[str] = []
    if file_size >= plan.limits.hard_limit:
        errors.append(f"file is {file_size / 1e6:.1f} MB (limit {plan.limits.hard_limit / 1e6:.0f} MB)")
    try:
        from PIL import Image
        meta = read_exif_summary(path)
        with Image.open(path) as im:  # fast reduced-size decode checks the data
            im.draft("RGB", (max(1, im.size[0] // 8), max(1, im.size[1] // 8)))
            im.load()
        src = read_exif_summary(plan.source)
    except Exception as e:
        return errors + [f"output is not a readable JPEG: {e}"]
    if meta["format"] not in ("JPEG", "MPO"):
        errors.append(f"format is {meta['format']}, expected JPEG")
    if tuple(meta["size"]) != (plan.stored_out_w, plan.stored_out_h):
        errors.append(f"size {meta['size']}, expected {(plan.stored_out_w, plan.stored_out_h)}")
    o = meta["orientation"]
    dw, dh = (meta["size"][1], meta["size"][0]) if o in (5, 6, 7, 8) else tuple(meta["size"])
    if plan.crop is not None:
        aw, ah = plan.crop.aspect
        if dw * ah != dh * aw:
            errors.append(f"displayed size {dw}x{dh} is not exactly {plan.crop.label}")
    elif not g.same_aspect(dw, dh, plan.info.display_w, plan.info.display_h):
        errors.append(f"displayed size {dw}x{dh} changed the aspect ratio")
    if dw > g.PHOTO_BOX[0] or dh > g.PHOTO_BOX[1]:
        errors.append(f"wider than 8K ({dw} px)")
    if o != src["orientation"]:
        errors.append("EXIF orientation changed")
    if src["taken"] and meta["taken"] != src["taken"]:
        errors.append(f"date taken lost (source {src['taken']}, output {meta['taken']})")
    if src["offset"] and meta["offset"] != src["offset"]:
        errors.append("time-zone offset lost")
    if src["gps_raw"] and meta["gps_raw"] != src["gps_raw"]:
        errors.append("GPS geotag lost or changed")
    return errors


def set_photo_file_time(plan: PhotoPlan, dst: str) -> None:
    """File date = time the photo was taken (or the original's file date)."""
    try:
        ts = taken_timestamp(plan.info.taken, plan.info.taken_offset)
        if ts is not None:
            os.utime(dst, (ts, ts))
        else:
            st = os.stat(plan.source)
            os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
    except OSError:
        pass


# --------------------------------------------------------------------------
# Session (same interface as encoding.EncodeSession)
# --------------------------------------------------------------------------

class PhotoSession:
    def __init__(self, plan: PhotoPlan, caps, tmp_dir: str, final_path: str,
                 may_overwrite: bool = False):
        self.plan = plan
        self.caps = caps
        self.tmp_dir = tmp_dir
        self.tmp_out = os.path.join(tmp_dir, "output" + plan.output_ext.lower())
        self.final_path = final_path
        self.may_overwrite = may_overwrite
        self.notes: list[str] = []
        self._state = "init"
        self._method = ""

    def preflight(self) -> str | None:
        from .encoding import disk_free
        if not os.path.isfile(self.plan.source):
            return "source file no longer exists"
        need = 3 * self.plan.info.size_bytes + 16 * 1024 * 1024
        if disk_free(self.tmp_dir) < need:
            return "Insufficient disk space in output folder"
        return None

    def should_abort(self, total_size: int, out_time: float) -> bool:
        return False

    def _lossless_step(self):
        from .encoding import Step
        c = self.plan.src_crop
        self._state = "lossless"
        return Step("tool", [self.caps.jpegtran, "-copy", "all",
                             "-crop", f"{c.w}x{c.h}+{c.x}+{c.y}",
                             "-outfile", self.tmp_out, self.plan.source],
                    "Lossless crop", 0.0, 1.0)

    def _reencode_step(self, force_downscale: bool = False):
        from .encoding import Step
        c = self.plan.src_crop
        p = self.plan
        args = {"src": p.source, "out": self.tmp_out, "crop": [c.x, c.y, c.w, c.h],
                "swap": p.info.orientation in (5, 6, 7, 8),
                "hard_limit": p.limits.hard_limit, "target": p.limits.target,
                "force_downscale": force_downscale}
        if p.crop is None:
            args["out_size"] = [p.out_w, p.out_h]
        else:
            args["out_k"] = g.output_k(p.crop, g.PHOTO_BOX)
            args["aspect"] = list(p.crop.aspect)
        self._state = "reencode"
        return Step("tool", [sys.executable, REENCODE_SCRIPT, json.dumps(args)],
                    "Re-encoding", 0.0, 1.0)

    def first_step(self):
        from .encoding import Outcome
        err = self.preflight()
        if err:
            return Outcome(False, err)
        return self._lossless_step() if self.plan.lossless else self._reencode_step()

    def next_step(self, r):
        from .encoding import Outcome, _tail
        if r.exit_code != 0:
            what = "jpegtran" if self._state == "lossless" else "re-encode"
            return Outcome(False, f"{what} failed: {_tail(r.stderr)}")
        size = self._size()
        if self._state == "lossless":
            if size >= self.plan.limits.hard_limit:
                self.notes.append("lossless crop too large: downscaled")
                return self._reencode_step(force_downscale=True)
            self._method = "lossless"
        else:
            try:
                res = json.loads((r.stdout or "").strip().splitlines()[-1])
                self.plan.stored_out_w, self.plan.stored_out_h = res["w"], res["h"]
                if self.plan.info.orientation in (5, 6, 7, 8):
                    self.plan.out_w, self.plan.out_h = res["h"], res["w"]
                else:
                    self.plan.out_w, self.plan.out_h = res["w"], res["h"]
            except (ValueError, KeyError, IndexError):
                return Outcome(False, "re-encode returned no result")
            self._method = "re-encoded" + (", downscaled" if self.plan.scaled or res.get("downscaled") else "")
        try:
            removed = clean_jpeg(self.tmp_out)
            patch_exif(self.tmp_out, self.plan.stored_out_w, self.plan.stored_out_h)
        except OSError as e:
            return Outcome(False, f"could not update metadata: {e}")
        if removed:
            self.notes.append("removed: " + ", ".join(removed))
        errors = validate_photo(self.tmp_out, self.plan, self._size())
        if errors:
            return Outcome(False, "validation failed: " + "; ".join(errors))
        return self._finalize(self._size())

    def _size(self) -> int:
        try:
            return os.path.getsize(self.tmp_out)
        except OSError:
            return 0

    def _finalize(self, size: int):
        from .encoding import Outcome
        from .files import unique_output_path
        final = self.final_path
        if os.path.lexists(final) and not self.may_overwrite:
            final = unique_output_path(os.path.dirname(final), self.plan.source, set(),
                                       ext=self.plan.output_ext)
        try:
            os.makedirs(os.path.dirname(final), exist_ok=True)
            os.replace(self.tmp_out, final)
        except OSError as e:
            return Outcome(False, f"could not move output into place: {e}")
        set_photo_file_time(self.plan, final)
        self._state = "done"
        return Outcome(True, "ok", final_path=final, size=size, method=self._method)
