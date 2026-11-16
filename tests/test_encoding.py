import json
import os

import pytest

from videocrop import encoding as enc
from videocrop import files
from videocrop import geometry as g
from videocrop.geometry import Crop
from videocrop.probe import AudioStream, ProbeError, VideoInfo, parse_probe

CAPS = enc.FFmpegCaps(ffmpeg="ffmpeg", ffprobe="ffprobe", version="test", fps_mode_flag="-fps_mode",
                      has_zscale=True, has_tonemap=True, nice=None)


def make_info(w=1920, h=1080, duration=60.0, audio=(("aac", 2, 192_000),), fps=30.0,
              hdr=None, sar=1.0, rotation=0, path="/videos/clip one.mp4") -> VideoInfo:
    fw, fh = (h, w) if rotation in (90, 270) else (w, h)
    dw, dh = g.display_size(fw, fh, sar)
    return VideoInfo(
        path=path, video_index=0, codec="h264", pix_fmt="yuv420p", raw_w=w, raw_h=h,
        rotation=rotation, frame_w=fw, frame_h=fh, sar=sar, sar_text="1:1",
        display_w=dw, display_h=dh, duration=duration, fps=fps, fps_text=str(fps),
        variable_fps=False, color_transfer="", hdr=hdr, bit_depth=8,
        audio=tuple(AudioStream(i + 1, c, ch, br, 48000) for i, (c, ch, br) in enumerate(audio)),
        size_bytes=10_000_000)


# ----------------------------------------------------------------- probe parsing

def _probe_json(**over):
    v = {"index": 0, "codec_type": "video", "codec_name": "hevc", "width": 1920, "height": 1080,
         "sample_aspect_ratio": "1:1", "r_frame_rate": "30/1", "avg_frame_rate": "30/1",
         "pix_fmt": "yuv420p"}
    v.update(over.pop("video", {}))
    data = {"streams": [v, {"index": 1, "codec_type": "audio", "codec_name": "aac",
                            "channels": 2, "bit_rate": "192000", "sample_rate": "48000"}],
            "format": {"duration": "12.5", "format_name": "mov,mp4,m4a,3gp,3g2,mj2"}}
    data.update(over)
    return data


def test_parse_rotation_side_data_portrait():
    d = _probe_json(video={"side_data_list": [{"side_data_type": "Display Matrix", "rotation": -90}]})
    info = parse_probe("/nonexistent.mp4", d)
    assert info.rotation == 270
    assert (info.frame_w, info.frame_h) == (1080, 1920)
    assert info.is_portrait
    assert info.duration == 12.5
    assert info.audio[0].codec == "aac" and info.audio[0].bit_rate == 192000


def test_parse_legacy_rotate_tag_and_sar_swap():
    d = _probe_json(video={"tags": {"rotate": "90"}, "width": 720, "height": 480,
                           "sample_aspect_ratio": "32:27"})
    info = parse_probe("/x.mp4", d)
    assert info.rotation == 90
    assert (info.frame_w, info.frame_h) == (480, 720)
    assert info.sar == pytest.approx(27 / 32)  # SAR is transposed with the frame
    assert (info.display_w, info.display_h) == (480, round(720 * 32 / 27))


def test_parse_hdr_and_vfr():
    d = _probe_json(video={"color_transfer": "arib-std-b67", "pix_fmt": "yuv420p10le",
                           "avg_frame_rate": "4050000/135013"})
    info = parse_probe("/x.mp4", d)
    assert info.hdr == "hlg" and info.bit_depth == 10
    assert info.fps == pytest.approx(29.997, abs=0.01)


def test_parse_errors():
    with pytest.raises(ProbeError):
        parse_probe("/x", {"streams": [], "format": {}})
    with pytest.raises(ProbeError):
        parse_probe("/x", _probe_json(format={}))  # no duration
    with pytest.raises(ProbeError):
        parse_probe("/x", _probe_json(video={"width": 8, "height": 8}))


def test_attached_picture_is_not_the_video():
    d = _probe_json()
    d["streams"].insert(0, {"index": 5, "codec_type": "video", "codec_name": "mjpeg",
                            "width": 300, "height": 300, "disposition": {"attached_pic": 1}})
    assert parse_probe("/x", d).video_index == 0


# ----------------------------------------------------------------- plan / budget

def test_short_video_uses_crf_and_keeps_aac():
    plan = enc.make_plan(make_info(duration=30), g.initial_crop(1920, 1080), CAPS)
    assert plan.try_crf_first
    assert (plan.out_w, plan.out_h) == (1728, 1080)
    assert not plan.scaled
    assert plan.audio[0].copy
    assert "crop=1728:1080:96:0" in plan.filter
    assert not plan.warnings


@pytest.mark.parametrize("duration", [1, 60, 600, 1800, 3600, 7200])
def test_budget_fits_target(duration):
    info = make_info(duration=duration)
    plan = enc.make_plan(info, g.initial_crop(1920, 1080), CAPS)
    total = (plan.budget_video_bps + plan.audio_bps) * duration / 8 + plan.limits.overhead
    assert total <= enc.TARGET_BYTES + 1
    assert enc.TARGET_BYTES < enc.HARD_LIMIT_BYTES


def test_long_video_goes_two_pass_with_warning_and_cheaper_audio():
    plan = enc.make_plan(make_info(duration=5400, audio=(("mp3", 2, 320_000),)),
                         g.initial_crop(1920, 1080), CAPS)
    assert not plan.try_crf_first
    assert any("quality" in w for w in plan.warnings)
    assert not plan.audio[0].copy and 48_000 <= plan.audio[0].bitrate < 192_000
    assert plan.audio[0].bitrate == 48_000  # floor for stereo AAC


def test_impossibly_long_video_raises():
    with pytest.raises(enc.PlanError):
        enc.make_plan(make_info(duration=10 * 3600), g.initial_crop(1920, 1080), CAPS)


def test_no_audio_and_multiple_audio():
    p0 = enc.make_plan(make_info(audio=()), g.initial_crop(1920, 1080), CAPS)
    assert p0.audio == []
    p2 = enc.make_plan(make_info(audio=(("aac", 2, 128_000), ("ac3", 6, 448_000))),
                       g.initial_crop(1920, 1080), CAPS)
    assert [a.copy for a in p2.audio] == [True, False]
    assert p2.audio[1].bitrate == 384_000


def test_4k_is_downscaled_with_lanczos():
    plan = enc.make_plan(make_info(3840, 2160), g.initial_crop(3840, 2160), CAPS)
    assert (plan.out_w, plan.out_h) == (1728, 1080)
    assert plan.scaled and "scale=1728:1080:flags=lanczos" in plan.filter


def test_hdr_is_tonemapped():
    plan = enc.make_plan(make_info(3840, 2160, hdr="hlg"), g.initial_crop(3840, 2160), CAPS)
    assert "tonemap=" in plan.filter and "zscale=tin=arib-std-b67" in plan.filter
    assert plan.filter.endswith("format=yuv420p,setsar=1")
    assert "bt709" in plan.color_args


def test_small_crop_warns():
    plan = enc.make_plan(make_info(), Crop(0, 0, 20), CAPS)
    assert (plan.out_w, plan.out_h) == (320, 200)
    assert any("small crop" in w for w in plan.warnings)


def test_invalid_crop_is_clamped_in_plan():
    plan = enc.make_plan(make_info(), Crop(5000, 5000, 500), CAPS)
    s = plan.src_crop
    assert s.x + s.w <= 1920 and s.y + s.h <= 1080


# ----------------------------------------------------------------- commands

def test_crf_command_contents():
    info = make_info(path="/vids/my clip (1) 'x'.mp4")
    plan = enc.make_plan(info, g.initial_crop(1920, 1080), CAPS)
    cmd = enc.crf_command(CAPS, plan, "/out/.tmp/output.mp4")
    assert cmd[cmd.index("-i") + 1] == info.path  # one argv element, no shell quoting
    for a, b in [("-c:v", "libx264"), ("-pix_fmt", "yuv420p"), ("-crf", str(enc.CRF)),
                 ("-fps_mode", "passthrough"), ("-movflags", "+faststart+use_metadata_tags"), ("-f", "mp4"),
                 ("-c:a:0", "copy"), ("-map", "0:0")]:
        i = cmd.index(a)
        assert cmd[i + 1] == b or b in cmd[i + 1:i + 30]
    assert "0:1" in cmd  # audio mapped by stream index; data streams dropped
    assert cmd[-1] == "/out/.tmp/output.mp4"


def test_two_pass_commands():
    plan = enc.make_plan(make_info(duration=7200), g.initial_crop(1920, 1080), CAPS)
    p1 = enc.pass1_command(CAPS, plan, 5_000_000, "/t/log")
    p2 = enc.pass2_command(CAPS, plan, 5_000_000, "/t/log", "/t/out.mp4")
    assert p1[p1.index("-pass") + 1] == "1" and "-an" in p1 and p1[-1] == os.devnull
    assert p2[p2.index("-pass") + 1] == "2" and p2[p2.index("-b:v") + 1] == "5000000"
    assert p1[p1.index("-passlogfile") + 1] == p2[p2.index("-passlogfile") + 1]


def test_vsync_fallback_for_old_ffmpeg():
    old = enc.FFmpegCaps(**{**CAPS.__dict__, "fps_mode_flag": "-vsync"})
    plan = enc.make_plan(make_info(), g.initial_crop(1920, 1080), old)
    cmd = enc.crf_command(old, plan, "/o.mp4")
    assert cmd[cmd.index("-vsync") + 1] == "passthrough" and "-fps_mode" not in cmd


# ----------------------------------------------------------------- progress

def test_progress_parser():
    p = enc.ProgressParser()
    assert not p.feed("frame=10\nout_time_us=2500000\ntotal_si")
    assert p.feed("ze=1048576\nspeed=1.5x\nprogress=continue\n")
    assert p.out_time == 2.5 and p.total_size == 1048576 and p.speed == "1.5x"
    p.feed("out_time_us=N/A\nprogress=end\n")
    assert p.ended and p.out_time == 2.5


# ----------------------------------------------------------------- validation

def _out_json(w=1728, h=1080, codec="h264", audio=1, dur=60.0, extra_v=None):
    v = {"codec_type": "video", "codec_name": codec, "width": w, "height": h,
         "sample_aspect_ratio": "1:1", "pix_fmt": "yuv420p"}
    v.update(extra_v or {})
    streams = [v] + [{"codec_type": "audio", "codec_name": "aac"}] * audio
    return {"streams": streams, "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                                           "duration": str(dur)}}


def test_validation_accepts_good_output():
    plan = enc.make_plan(make_info(), g.initial_crop(1920, 1080), CAPS)
    assert enc.validate_output(_out_json(), plan, 50_000_000) == []


@pytest.mark.parametrize("kwargs,size,needle", [
    ({}, 125_000_000, "MB"),
    ({"codec": "hevc"}, 1, "codec"),
    ({"w": 1920, "h": 1080}, 1, "resolution"),
    ({"audio": 0}, 1, "audio"),
    ({"dur": 30.0}, 1, "duration"),
    ({"extra_v": {"side_data_list": [{"rotation": 90}]}}, 1, "rotation"),
    ({"extra_v": {"pix_fmt": "yuv444p"}}, 1, "pixel format"),
])
def test_validation_rejects(kwargs, size, needle):
    plan = enc.make_plan(make_info(), g.initial_crop(1920, 1080), CAPS)
    errors = enc.validate_output(_out_json(**kwargs), plan, size)
    assert errors and any(needle in e for e in errors)


# ----------------------------------------------------------------- session logic

def _session(tmp_path, duration=60.0, limits=enc.Limits()):
    info = make_info(duration=duration, path=str(tmp_path / "src.mp4"))
    (tmp_path / "src.mp4").write_bytes(b"x")
    plan = enc.make_plan(info, g.initial_crop(1920, 1080), CAPS, limits)
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    return enc.EncodeSession(plan, CAPS, str(tmp), str(tmp_path / "out" / "src.mp4"))


def _write(path, n):
    with open(path, "wb") as f:
        f.truncate(n)


def test_session_crf_too_big_falls_back_to_two_pass_then_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(enc, "disk_free", lambda p: 10**12)
    s = _session(tmp_path)
    step = s.first_step()
    assert step.watch_size and "-crf" in step.args
    _write(s.tmp_out, 130_000_000)
    step = s.next_step(enc.StepResult(0))
    assert step.label == "Pass 1/2"
    step = s.next_step(enc.StepResult(0))
    assert step.label == "Pass 2/2"
    first_rate = int(step.args[step.args.index("-b:v") + 1])
    _write(s.tmp_out, 126_000_000)  # 2-pass overshoot -> retry with less bitrate
    step = s.next_step(enc.StepResult(0))
    assert "retry 1" in step.label
    assert int(step.args[step.args.index("-b:v") + 1]) < first_rate
    _write(s.tmp_out, 119_000_000)
    step = s.next_step(enc.StepResult(0))
    assert step.kind == "probe"
    out = s.next_step(enc.StepResult(0, json.dumps(_out_json(dur=60.0))))
    assert out.success and os.path.exists(out.final_path)
    assert not os.path.exists(s.tmp_out)


def test_session_early_abort_logic(tmp_path, monkeypatch):
    monkeypatch.setattr(enc, "disk_free", lambda p: 10**12)
    s = _session(tmp_path, duration=100.0)
    s.first_step()
    assert not s.should_abort(10_000_000, 2.0)       # too early to judge
    assert not s.should_abort(10_000_000, 20.0)      # projects to 50 MB: fine
    assert s.should_abort(40_000_000, 20.0)          # projects to 200 MB: abort
    assert s.should_abort(125_000_000, 90.0)         # already over the limit
    nxt = s.next_step(enc.StepResult(-1, aborted=True))
    assert nxt.label == "Pass 1/2"


def test_session_gives_up_after_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(enc, "disk_free", lambda p: 10**12)
    s = _session(tmp_path, duration=1800)
    step = s.first_step()
    assert step.label == "Pass 1/2"
    step = s.next_step(enc.StepResult(0))
    for _ in range(enc.MAX_RETRIES):
        _write(s.tmp_out, 200_000_000)
        step = s.next_step(enc.StepResult(0))
        assert isinstance(step, enc.Step)
    _write(s.tmp_out, 200_000_000)
    out = s.next_step(enc.StepResult(0))
    assert isinstance(out, enc.Outcome) and not out.success


def test_session_reports_ffmpeg_failure_and_disk_space(tmp_path, monkeypatch):
    monkeypatch.setattr(enc, "disk_free", lambda p: 10**12)
    s = _session(tmp_path)
    s.first_step()
    out = s.next_step(enc.StepResult(1, stderr="foo\nbar: No space left on device\n"))
    assert not out.success and "disk space" in out.message
    monkeypatch.setattr(enc, "disk_free", lambda p: 1000)
    s2 = _session(tmp_path / "b") if (tmp_path / "b").mkdir() is None else None
    out = s2.first_step()
    assert isinstance(out, enc.Outcome) and "disk space" in out.message


def test_finalize_never_overwrites_foreign_file(tmp_path, monkeypatch):
    monkeypatch.setattr(enc, "disk_free", lambda p: 10**12)
    s = _session(tmp_path)
    os.makedirs(os.path.dirname(s.final_path))
    with open(s.final_path, "w") as f:
        f.write("someone else's file")
    _write(s.tmp_out, 1000)
    s._state = "probe"
    out = s.next_step(enc.StepResult(0, json.dumps(_out_json())))
    assert out.success and out.final_path != s.final_path
    assert open(s.final_path).read() == "someone else's file"


# ----------------------------------------------------------------- files

def test_unique_output_path(tmp_path):
    (tmp_path / "a.mp4").write_text("x")
    taken = {str(tmp_path / "a (1).mp4")}
    assert files.unique_output_path(str(tmp_path), "/in/a.mov", taken) == str(tmp_path / "a (2).mp4")
    assert files.unique_output_path(str(tmp_path), "/in/b c ä.mp4", set()) == str(tmp_path / "b c ä.mp4")


def test_scan_videos_natural_order(tmp_path):
    for n in ["clip10.mp4", "clip2.MP4", "clip1.mov", ".hidden.mp4", "notes.txt"]:
        (tmp_path / n).write_text("x")
    (tmp_path / "cropped").mkdir()
    names = [os.path.basename(p) for p in files.scan_videos(str(tmp_path))]
    assert names == ["clip1.mov", "clip2.MP4", "clip10.mp4"]
