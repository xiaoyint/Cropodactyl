import random

import pytest

from videocrop import geometry as g
from videocrop.geometry import Crop


def assert_valid(c: Crop, dw: int, dh: int):
    assert c.w * 10 == c.h * 16, "aspect must be exactly 16:10"
    assert c.x >= 0 and c.y >= 0
    assert c.right <= dw and c.bottom <= dh
    assert c.k >= 1


# ----------------------------------------------------------------- initial crop

def test_initial_crop_1080p_example_from_spec():
    c = g.initial_crop(1920, 1080)
    assert (c.w, c.h) == (1728, 1080)
    assert (c.x, c.y) == (96, 0)  # centred horizontally
    assert g.output_size(c) == (1728, 1080)


def test_initial_crop_4k_is_downscaled_to_max():
    c = g.initial_crop(3840, 2160)
    assert (c.w, c.h) == (3456, 2160)
    assert (c.x, c.y) == (192, 0)
    assert g.output_size(c) == (1728, 1080)


def test_initial_crop_portrait_stays_landscape():
    c = g.initial_crop(1080, 1920)
    assert c.w > c.h  # landscape 16:10 inside a portrait frame
    assert (c.w, c.h) == (1072, 670)
    assert (c.x, c.y) == (4, 625)
    assert g.output_size(c) == (1072, 670)  # never upscaled


def test_initial_crop_portrait_4k():
    c = g.initial_crop(2160, 3840)
    assert (c.w, c.h) == (2160, 1350)
    assert g.output_size(c) == (1728, 1080)


def test_initial_crop_exact_16_10_source_uses_whole_frame():
    c = g.initial_crop(1680, 1050)
    assert (c.x, c.y, c.w, c.h) == (0, 0, 1680, 1050)
    assert g.output_size(c) == (1680, 1050)


def test_too_small_video_raises():
    with pytest.raises(ValueError):
        g.initial_crop(10, 8)


@pytest.mark.parametrize("dw,dh", [(1920, 1080), (1280, 720), (3840, 2160), (1080, 1920),
                                   (720, 480), (640, 360), (853, 480), (4096, 2160),
                                   (2560, 1600), (100, 100), (17, 11), (1440, 1080)])
def test_initial_crop_is_largest_and_valid(dw, dh):
    c = g.initial_crop(dw, dh)
    assert_valid(c, dw, dh)
    # one step larger would not fit
    assert 16 * (c.k + 1) > dw or 10 * (c.k + 1) > dh
    # centred (within rounding)
    assert abs((dw - c.right) - c.x) <= 1
    assert abs((dh - c.bottom) - c.y) <= 1


# ----------------------------------------------------------------- moving / clamping

def test_move_is_clamped_to_frame():
    c = g.initial_crop(1920, 1080)
    assert g.moved(c, 10_000, 0, 1920, 1080).x == 1920 - 1728
    assert g.moved(c, -10_000, 0, 1920, 1080).x == 0
    assert g.moved(c, 0, 50, 1920, 1080).y == 0  # full height: cannot move vertically


def test_clamp_limits_size():
    c = g.clamp_crop(Crop(-50, -50, 10_000), 1920, 1080)
    assert c == Crop(0, 0, 108)
    c = g.clamp_crop(Crop(500, 500, 1), 1920, 1080)
    assert c.k == g.MIN_K


def test_centered_at_and_zoom_keep_centre():
    c = g.centered_at(960, 540, 50, 1920, 1080)
    assert (c.cx, c.cy) == (960, 540)
    bigger = g.zoomed(c, +1, 1920, 1080)
    smaller = g.zoomed(c, -1, 1920, 1080)
    assert bigger.k > c.k > smaller.k
    assert abs(bigger.cx - 960) <= 1 and abs(smaller.cy - 540) <= 1


def test_zoom_never_exceeds_frame():
    c = g.initial_crop(1920, 1080)
    assert g.zoomed(c, 50, 1920, 1080) == c
    tiny = g.zoomed(c, -500, 1920, 1080)
    assert tiny.k == g.MIN_K
    assert_valid(tiny, 1920, 1080)


# ----------------------------------------------------------------- corner resize

def test_resize_bottom_right_keeps_top_left_anchor():
    c = g.initial_crop(1920, 1080)
    r = g.resized_from_corner(c.x, c.y, +1, +1, c.x + 800, c.y + 500, 1920, 1080)
    assert (r.x, r.y) == (c.x, c.y)
    assert (r.w, r.h) == (800, 500)


def test_resize_top_left_keeps_bottom_right_anchor():
    c = Crop(400, 300, 40)  # 640x400
    r = g.resized_from_corner(c.right, c.bottom, -1, -1, c.right - 320, c.bottom - 10, 1920, 1080)
    assert (r.right, r.bottom) == (c.right, c.bottom)
    assert r.w == 320 and r.h == 200  # width drove the size


def test_resize_stops_at_frame_edge():
    c = Crop(1000, 200, 30)
    r = g.resized_from_corner(c.x, c.y, +1, +1, 5000, 5000, 1920, 1080)
    assert r.right <= 1920 and r.bottom <= 1080
    assert (r.x, r.y) == (c.x, c.y)
    assert_valid(r, 1920, 1080)


def test_resize_past_anchor_gives_minimum_not_flip():
    c = Crop(500, 300, 40)
    r = g.resized_from_corner(c.x, c.y, +1, +1, c.x - 300, c.y - 300, 1920, 1080)
    assert r.k == g.MIN_K and (r.x, r.y) == (c.x, c.y)


def test_random_interactions_always_valid():
    rnd = random.Random(1234)
    for _ in range(300):
        dw, dh = rnd.randint(40, 5000), rnd.randint(40, 5000)
        if g.max_k(dw, dh) < 1:
            continue
        c = g.initial_crop(dw, dh)
        for _ in range(30):
            op = rnd.choice(["move", "zoom", "corner", "center"])
            if op == "move":
                c = g.moved(c, rnd.uniform(-dw, dw), rnd.uniform(-dh, dh), dw, dh)
            elif op == "zoom":
                c = g.zoomed(c, rnd.randint(-5, 5), dw, dh)
            elif op == "center":
                c = g.centered_at(rnd.uniform(-100, dw + 100), rnd.uniform(-100, dh + 100), c.k, dw, dh)
            else:
                sx, sy = rnd.choice([1, -1]), rnd.choice([1, -1])
                ax = c.x if sx > 0 else c.right
                ay = c.y if sy > 0 else c.bottom
                c = g.resized_from_corner(ax, ay, sx, sy, rnd.uniform(-200, dw + 200),
                                          rnd.uniform(-200, dh + 200), dw, dh)
            assert_valid(c, dw, dh)
            ow, oh = g.output_size(c)
            assert ow * 10 == oh * 16 and ow <= 1728 and oh <= 1080
            assert ow <= c.w and oh <= c.h  # never upscaled


# ----------------------------------------------------------------- output size

@pytest.mark.parametrize("k,expected", [(108, (1728, 1080)), (200, (1728, 1080)),
                                        (50, (800, 500)), (8, (128, 80)), (109, (1728, 1080))])
def test_output_size(k, expected):
    ow, oh = g.output_size(Crop(0, 0, k))
    assert (ow, oh) == expected
    assert ow % 2 == 0 and oh % 2 == 0  # H.264 / yuv420p needs even dimensions
    assert ow * 10 == oh * 16


# ----------------------------------------------------------------- source mapping

def test_source_crop_square_pixels_is_identity_but_even():
    c = Crop(97, 33, 50)  # 800x500
    s = g.to_source_crop(c, 1920, 1080, 1920, 1080)
    assert (s.w, s.h) == (800, 500)
    assert (s.x, s.y) == (96, 32)  # rounded down to even for 4:2:0 chroma
    assert not g.needs_scaling(s, *g.output_size(c))


def test_source_crop_right_edge_stays_inside():
    c = Crop(1920 - 1728, 0, 108)
    s = g.to_source_crop(c, 1920, 1080, 1920, 1080)
    assert s.x + s.w <= 1920 and s.y + s.h <= 1080


def test_display_size_with_sar():
    # anamorphic DVD 720x480 with SAR 32:27 -> displayed 853x480
    assert g.display_size(720, 480, 32 / 27) == (853, 480)
    # SAR < 1 stretches vertically instead (never discards resolution)
    assert g.display_size(608, 1080, 1215 / 1216) == (608, 1081)
    assert g.display_size(1920, 1080, 1.0) == (1920, 1080)
    assert g.display_size(1920, 1080, None) == (1920, 1080)


def test_source_crop_anamorphic_maps_back_to_frame_pixels():
    fw, fh, sar = 720, 480, 32 / 27
    dw, dh = g.display_size(fw, fh, sar)
    c = g.initial_crop(dw, dh)  # 768x480 display
    s = g.to_source_crop(c, fw, fh, dw, dh)
    assert s.h == 480
    assert abs(s.w - c.w * fw / dw) <= 2  # ~648 frame pixels
    assert s.x % 2 == 0 and s.w % 2 == 0
    assert s.x + s.w <= fw
    # the displayed aspect of the source region is 16:10
    assert abs((s.w * sar) / s.h - 1.6) < 0.01
    assert g.needs_scaling(s, *g.output_size(c))


def test_source_crop_random_bounds():
    rnd = random.Random(7)
    for _ in range(500):
        fw, fh = rnd.randint(20, 4000), rnd.randint(20, 4000)
        sar = rnd.choice([1.0, 32 / 27, 8 / 9, 4 / 3, 1215 / 1216])
        dw, dh = g.display_size(fw, fh, sar)
        if g.max_k(dw, dh) < 1:
            continue
        c = g.clamp_crop(Crop(rnd.randint(0, dw), rnd.randint(0, dh), rnd.randint(1, 400)), dw, dh)
        s = g.to_source_crop(c, fw, fh, dw, dh)
        assert 0 <= s.x and 0 <= s.y and s.x + s.w <= fw and s.y + s.h <= fh
        assert s.w % 2 == 0 and s.h % 2 == 0 and s.x % 2 == 0 and s.y % 2 == 0


# ----------------------------------------------------------------- view transform

def test_view_transform_roundtrip_and_fit():
    t = g.fit_frame(1920, 1080, 1000, 800, margin=0)
    assert t.scale == pytest.approx(1000 / 1920)
    x, y = t.to_widget(960, 540)
    assert (x, y) == pytest.approx((500, 400))
    assert t.to_display(x, y) == pytest.approx((960, 540))


def test_fit_crop_fills_view_with_crop():
    c = Crop(100, 50, 50)  # 800x500
    t = g.fit_crop(c, 1600, 1000, margin=0)
    assert t.to_widget(c.x, c.y) == pytest.approx((0, 0))
    assert t.to_widget(c.right, c.bottom) == pytest.approx((1600, 1000))


# ----------------------------------------------------------------- square (1:1) crops

def test_square_initial_and_output_limits():
    c = g.initial_crop(1920, 1080, g.SQUARE)
    assert (c.x, c.y, c.w, c.h) == (420, 0, 1080, 1080) and c.label == "1:1"
    assert g.output_size(c) == (1080, 1080) and not g.is_downscaled(c)
    big = g.initial_crop(3840, 2160, g.SQUARE)
    assert g.output_size(big) == (1080, 1080) and g.is_downscaled(big)
    assert g.output_size(big, g.PHOTO_BOX) == (2160, 2160)
    huge = g.initial_crop(12000, 9000, g.SQUARE)
    assert g.output_size(huge, g.PHOTO_BOX) == (7680, 7680)
    # 16:10 limits unchanged
    assert g.output_size(g.initial_crop(3840, 2160)) == (1728, 1080)
    assert g.output_size(g.initial_crop(12000, 9000), g.PHOTO_BOX) == (7680, 4800)


def test_square_portrait_source_uses_full_width():
    c = g.initial_crop(1080, 1920, g.SQUARE)
    assert (c.w, c.h, c.x, c.y) == (1080, 1080, 0, 420)


def test_aspect_switch_keeps_centre_and_height():
    wide = g.initial_crop(1920, 1080)
    sq = g.with_aspect(wide, g.SQUARE, 1920, 1080)
    assert (sq.w, sq.h) == (1080, 1080) and (sq.cx, sq.cy) == (wide.cx, wide.cy)
    back = g.with_aspect(sq, g.WIDE, 1920, 1080)
    assert back == wide
    # near the edge the switched crop is pushed back inside the frame
    small = g.Crop(1900 - 160, 0, 10)
    s2 = g.with_aspect(g.clamp_crop(small, 1920, 1080), g.SQUARE, 1920, 1080)
    assert s2.right <= 1920 and s2.w == s2.h


def test_square_operations_keep_aspect():
    c = g.initial_crop(1920, 1080, g.SQUARE)
    for op in (lambda c: g.moved(c, 37, -5, 1920, 1080),
               lambda c: g.zoomed(c, -3, 1920, 1080),
               lambda c: g.resized_from_corner(c.x, c.y, 1, 1, c.x + 700, c.y + 300, 1920, 1080,
                                               c.aspect)):
        r = op(c)
        assert r.w == r.h and r.aspect == g.SQUARE
        assert 0 <= r.x and r.right <= 1920 and 0 <= r.y and r.bottom <= 1080
