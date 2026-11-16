"""Minimal MP4 box editing: add the QuickTime ``©xyz`` geotag atom.

FFmpeg's MP4 muxer stores a ``location`` tag as a 3GPP ``loci`` box (or as a
raw ``mdta`` key), but phones write – and photo apps such as Google Photos,
Apple Photos, digiKam or exiftool read – the ``moov/udta/©xyz`` atom holding
an ISO 6709 string like ``+35.6738+139.7017/``.  This module adds that atom.

Only the (small) ``moov`` box is rewritten in memory; media data is copied in
chunks.  If ``moov`` precedes ``mdat`` (faststart), every chunk offset in
``stco``/``co64`` is shifted by the size change so the file stays valid.
"""
from __future__ import annotations

import os
import re
import struct

ISO6709_RE = re.compile(r"^[+-]\d{1,2}(\.\d+)?[+-]\d{1,3}(\.\d+)?([+-]\d+(\.\d+)?)?(CRS[^/]*)?/?$")
_LANG_ENG = 0x15C7  # packed ISO-639-2 "eng", as written by Android and iOS
# boxes whose children may contain chunk-offset tables
_OFFSET_CONTAINERS = {b"trak", b"mdia", b"minf", b"stbl"}


class Mp4Error(Exception):
    pass


def normalize_iso6709(value: str | None) -> str | None:
    """Return a clean ISO 6709 string (with trailing '/') or None."""
    if not value:
        return None
    v = value.strip()
    if not ISO6709_RE.match(v):
        return None
    return v if v.endswith("/") else v + "/"


def _boxes(buf: bytes, start: int = 0, end: int | None = None):
    """Yield (type, box_start, header_size, box_end) for boxes in buf[start:end]."""
    end = len(buf) if end is None else end
    off = start
    while off + 8 <= end:
        size, typ = struct.unpack(">I4s", buf[off:off + 8])
        hdr = 8
        if size == 1:
            size = struct.unpack(">Q", buf[off + 8:off + 16])[0]
            hdr = 16
        elif size == 0:
            size = end - off
        if size < hdr or off + size > end:
            raise Mp4Error(f"corrupt box {typ!r} at {off}")
        yield typ, off, hdr, off + size
        off += size


def _box(typ: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), typ) + payload


def _xyz_atom(iso6709: str) -> bytes:
    text = iso6709.encode("ascii")
    return _box(b"\xa9xyz", struct.pack(">HH", len(text), _LANG_ENG) + text)


def _shift_offsets(buf: bytes, delta: int) -> bytes:
    """Return a copy of a container's content with stco/co64 entries shifted."""
    out = bytearray()
    for typ, s, hdr, e in _boxes(buf):
        if typ in _OFFSET_CONTAINERS:
            out += _box(typ, _shift_offsets(buf[s + hdr:e], delta)) if hdr == 8 else buf[s:e]
        elif typ == b"stco":
            body = bytearray(buf[s + hdr:e])
            (n,) = struct.unpack(">I", body[4:8])
            for i in range(n):
                p = 8 + 4 * i
                (v,) = struct.unpack(">I", body[p:p + 4])
                if v + delta >= 2**32:
                    raise Mp4Error("chunk offset overflow")
                body[p:p + 4] = struct.pack(">I", v + delta)
            out += buf[s:s + hdr] + body
        elif typ == b"co64":
            body = bytearray(buf[s + hdr:e])
            (n,) = struct.unpack(">I", body[4:8])
            for i in range(n):
                p = 8 + 8 * i
                (v,) = struct.unpack(">Q", body[p:p + 8])
                body[p:p + 8] = struct.pack(">Q", v + delta)
            out += buf[s:s + hdr] + body
        else:
            out += buf[s:e]
    return bytes(out)


def _new_moov(moov: bytes, iso6709: str) -> bytes:
    """moov box with ``udta/©xyz`` set (replacing any existing one)."""
    content = moov[8:]
    children = list(_boxes(content))
    out = bytearray()
    have_udta = False
    for typ, s, hdr, e in children:
        if typ == b"udta" and hdr == 8:
            have_udta = True
            udta = bytearray()
            for t2, s2, _h2, e2 in _boxes(content, s + hdr, e):
                if t2 != b"\xa9xyz":
                    udta += content[s2:e2]
            udta += _xyz_atom(iso6709)
            out += _box(b"udta", bytes(udta))
        else:
            out += content[s:e]
    if not have_udta:
        out += _box(b"udta", _xyz_atom(iso6709))
    return _box(b"moov", bytes(out))


def _top_level(f) -> list[tuple[bytes, int, int, int]]:
    """Top-level boxes of an open file, reading only their headers."""
    f.seek(0, os.SEEK_END)
    file_size = f.tell()
    top = []
    off = 0
    while off + 8 <= file_size:
        f.seek(off)
        head = f.read(16)
        size, typ = struct.unpack(">I4s", head[:8])
        hdr = 8
        if size == 1:
            size = struct.unpack(">Q", head[8:16])[0]
            hdr = 16
        elif size == 0:
            size = file_size - off
        if size < hdr or off + size > file_size:
            raise Mp4Error(f"corrupt top-level box {typ!r}")
        top.append((typ, off, hdr, off + size))
        off += size
    return top


def _read_moov(f, top) -> tuple[int, bytes]:
    moovs = [b for b in top if b[0] == b"moov"]
    if len(moovs) != 1 or moovs[0][2] != 8:
        raise Mp4Error("expected exactly one moov box")
    _, start, _, end = moovs[0]
    f.seek(start)
    return start, f.read(end - start)


def set_location(path: str, iso6709: str) -> None:
    """Add/replace the ``©xyz`` geotag in an MP4 file (rewrites the file)."""
    loc = normalize_iso6709(iso6709)
    if loc is None:
        raise Mp4Error(f"not an ISO 6709 location: {iso6709!r}")
    with open(path, "rb") as f:
        top = _top_level(f)
        m_start, old_moov = _read_moov(f, top)
        new_moov = _new_moov(old_moov, loc)
        delta = len(new_moov) - len(old_moov)
        media_after = any(t == b"mdat" and s > m_start for t, s, _, _ in top)
        if delta and media_after:
            new_moov = _box(b"moov", _shift_offsets(new_moov[8:], delta))
        tmp = path + ".meta.tmp"
        try:
            with open(tmp, "wb") as out:
                for typ, s, _hdr, e in top:
                    if s == m_start:
                        out.write(new_moov)
                        continue
                    f.seek(s)
                    remaining = e - s
                    while remaining:
                        chunk = f.read(min(remaining, 4 * 1024 * 1024))
                        if not chunk:
                            raise Mp4Error("unexpected end of file")
                        out.write(chunk)
                        remaining -= len(chunk)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise


def read_location(path: str) -> str | None:
    """The ``moov/udta/©xyz`` string of a file, or None."""
    with open(path, "rb") as f:
        _, moov = _read_moov(f, _top_level(f))
    for t2, s2, h2, e2 in _boxes(moov, 8):
        if t2 != b"udta":
            continue
        for t3, s3, h3, e3 in _boxes(moov, s2 + h2, e2):
            if t3 == b"\xa9xyz":
                (n,) = struct.unpack(">H", moov[s3 + h3:s3 + h3 + 2])
                return moov[s3 + h3 + 4:s3 + h3 + 4 + n].decode("ascii", "replace")
    return None
