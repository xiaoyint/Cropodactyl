"""Re-encode a JPEG crop (run as a separate process so the GUI never blocks).

Usage: python photo_reencode.py '<json>'
JSON keys: src, out, crop [x, y, w, h] (stored pixels), aspect [aw, ah]
(default 16:10), out_k (multiplier of the wanted displayed size aw*k x ah*k),
or instead out_size [w, h] (wanted displayed size, any aspect; used for
uncropped photos), swap (EXIF orientation rotates by 90 degrees), hard_limit,
target (bytes), force_downscale.

Prints one JSON line: {"w": stored_width, "h": stored_height, "downscaled": bool}

Standalone on purpose: only needs Pillow, no package imports.
"""
import json
import math
import os
import sys

from PIL import Image, JpegImagePlugin

LANCZOS = getattr(getattr(Image, "Resampling", Image), "LANCZOS")


def main() -> int:
    a = json.loads(sys.argv[1])
    x, y, w, h = a["crop"]
    with Image.open(a["src"]) as src:
        info = dict(src.info)
        qtables = getattr(src, "quantization", None)
        try:
            sampling = JpegImagePlugin.get_sampling(src)
        except Exception:
            sampling = -1
        img = src.crop((x, y, x + w, y + h))
        img.load()

    save_kw = {"optimize": True}
    for key in ("exif", "icc_profile", "xmp", "dpi", "comment"):
        if info.get(key):
            save_kw[key] = info[key]
    if info.get("progressive") or info.get("progression"):
        save_kw["progressive"] = True
    if qtables:
        save_kw["qtables"] = qtables  # same quality as the original
    else:
        save_kw["quality"] = 95
    if sampling in (0, 1, 2):
        save_kw["subsampling"] = sampling

    if a.get("out_size"):
        return _fit_whole(a, img, save_kw, w, h)

    k = int(a["out_k"])
    aw, ah = a.get("aspect") or (16, 10)
    cur_k = (w if not a["swap"] else h) // aw  # displayed width / aw
    downscaled = False
    if a.get("force_downscale"):
        k = max(1, int(min(k, cur_k) * 0.9))
    for _attempt in range(8):
        if k < cur_k:
            dw, dh = aw * k, ah * k
            size = (dh, dw) if a["swap"] else (dw, dh)
            out = img.resize(size, LANCZOS)
            downscaled = True
        else:
            out = img
        out.save(a["out"], "JPEG", **save_kw)
        n = os.path.getsize(a["out"])
        if n < a["hard_limit"]:
            print(json.dumps({"w": out.size[0], "h": out.size[1], "downscaled": downscaled}))
            return 0
        # too big: shrink the pixel count proportionally and retry
        k = max(1, int(min(k, cur_k) * math.sqrt(a["target"] / n) * 0.97))
    sys.stderr.write("could not get below the size limit\n")
    return 1


def _fit_whole(a, img, save_kw, w, h) -> int:
    """Uncropped photo: displayed size out_size (any aspect), shrunk further
    only while the file is >= hard_limit."""
    swap = a["swap"]
    cw, ch = (h, w) if swap else (w, h)  # displayed size of the source
    tw, th = min(int(a["out_size"][0]), cw), min(int(a["out_size"][1]), ch)
    if a.get("force_downscale"):
        tw, th = max(1, int(tw * 0.9)), max(1, int(th * 0.9))
    for _attempt in range(8):
        if (tw, th) != (cw, ch):
            out = img.resize((th, tw) if swap else (tw, th), LANCZOS)
        else:
            out = img
        out.save(a["out"], "JPEG", **save_kw)
        n = os.path.getsize(a["out"])
        if n < a["hard_limit"]:
            print(json.dumps({"w": out.size[0], "h": out.size[1],
                              "downscaled": (tw, th) != (cw, ch)}))
            return 0
        f = math.sqrt(a["target"] / n) * 0.97
        tw, th = max(1, int(tw * f)), max(1, int(th * f))
    sys.stderr.write("could not get below the size limit\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
