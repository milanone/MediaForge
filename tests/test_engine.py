"""Unit tests for the pure functions of MediaForge.pyw (no Tk window, no real media files).

Run: py -m unittest discover -s tests -v
The web sibling (../mediaforge-web) has tests/test_parity.py that compares the shared rules.
"""
import importlib.util
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "MediaForge.pyw"
spec = importlib.util.spec_from_file_location("mediaforge", SRC)
mf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mf)


def video(codec="hevc", pix="yuv420p10le", w=3840, h=2160):
    return {"codec_type": "video", "codec_name": codec, "pix_fmt": pix, "width": w, "height": h}


class DefaultCrf(unittest.TestCase):
    def test_qsv_reference_points(self):
        for (w, h), crf in {(854, 480): 16, (1280, 720): 18, (1920, 1080): 20, (3840, 2160): 22}.items():
            self.assertEqual(mf.crf_suggerito(w, h, "hevc_qsv"), crf)

    def test_qsv_flat_outside_the_range_and_portrait(self):
        self.assertEqual(mf.crf_suggerito(320, 240, "hevc_qsv"), 16)
        self.assertEqual(mf.crf_suggerito(7680, 4320, "hevc_qsv"), 22)
        self.assertEqual(mf.crf_suggerito(2160, 3840, "hevc_qsv"), 22)

    def test_av1_family_gets_plus_one(self):
        self.assertEqual(mf.crf_suggerito(1920, 1080, "av1_qsv"), 21)

    def test_other_encoders_keep_the_old_curve(self):
        self.assertEqual(mf.crf_suggerito(1920, 1080, "libx265"), 25)
        self.assertEqual(mf.crf_suggerito(3840, 2160, "libx265"), 28)


class GpuDecodeGating(unittest.TestCase):
    def test_supported_and_unsupported_sources(self):
        self.assertTrue(mf.sorgente_decodificabile_hw(video("hevc", "yuv420p10le")))
        self.assertTrue(mf.sorgente_decodificabile_hw(video("h264", "yuv420p")))
        self.assertFalse(mf.sorgente_decodificabile_hw(video("h264", "yuv422p10le")))
        self.assertFalse(mf.sorgente_decodificabile_hw(video("prores", "yuv422p10le")))
        self.assertFalse(mf.sorgente_decodificabile_hw(video("mjpeg", "yuvj420p")))

    def test_unknown_source_is_not_blocked(self):
        self.assertTrue(mf.sorgente_decodificabile_hw(None))


class EncoderArgs(unittest.TestCase):
    def test_qsv_quality_with_preset_and_look_ahead(self):
        a = mf.encoder_video_args("hevc_qsv", 20, True, "veryslow")
        self.assertEqual(a, ["-c:v", "hevc_qsv", "-global_quality", "20", "-preset", "veryslow",
                             "-look_ahead", "1"])

    def test_unknown_preset_is_ignored(self):
        self.assertNotIn("-preset", mf.encoder_video_args("hevc_qsv", 20, False, "slower"))

    def test_preset_only_for_hevc_qsv(self):
        self.assertNotIn("-preset", mf.encoder_video_args("av1_qsv", 20, False, "veryfast"))

    def test_bitrate_mode_replaces_quality(self):
        a = mf.encoder_video_args("hevc_qsv", 20, True, "medium", 2500)
        self.assertEqual(a[:4], ["-c:v", "hevc_qsv", "-b:v", "2500k"])
        self.assertNotIn("-global_quality", a)

    def test_bitrate_mode_per_encoder(self):
        self.assertIn("vbr_peak", mf.encoder_video_args("hevc_amf", 20, False, None, 2000))
        self.assertIn("vbr", mf.encoder_video_args("hevc_nvenc", 20, False, None, 2000))
        self.assertIn("-preset", mf.encoder_video_args("libx265", 20, False, None, 2000))


class TwoPass(unittest.TestCase):
    base = {"two_pass": True, "bitrate_kbps": 2000, "vcodec": "libx265"}

    def test_only_software_encoders_with_a_target_bitrate(self):
        self.assertTrue(mf.usa_2_passate(self.base))
        self.assertFalse(mf.usa_2_passate(dict(self.base, vcodec="hevc_qsv")))
        self.assertFalse(mf.usa_2_passate(dict(self.base, bitrate_kbps=None)))
        self.assertFalse(mf.usa_2_passate(dict(self.base, two_pass=False)))

    def test_pass_one_writes_nothing(self):
        data = {"streams": [video("h264", "yuv420p", 1920, 1080)]}
        opts = dict(self.base, mode="video", output_ext="mkv", quality=22, look_ahead=False, audio="copy",
                    subs="copy", timestamp="", pass_num=1, passlog="x/pass")
        cmd = mf.build_ffmpeg_cmd(Path("in.mkv"), Path("out.mkv"), opts, data, hwaccel_decode=False)
        self.assertEqual(cmd[-5:], ["-an", "-sn", "-f", "null", mf.os.devnull])
        self.assertEqual(cmd[cmd.index("-pass") + 1], "1")


class FpsRules(unittest.TestCase):
    def test_fraction_parsing(self):
        self.assertAlmostEqual(mf.fps_da_frazione("30000/1001"), 29.97, places=2)
        self.assertEqual(mf.fps_da_frazione("0/0"), 0.0)
        self.assertEqual(mf.fps_da_frazione("N/A"), 0.0)

    def test_standard_snapping_prefers_the_declared_family(self):
        self.assertEqual(mf.fps_standard_vicino(30.1, 59.94)[0], "30000/1001")
        self.assertEqual(mf.fps_standard_vicino(30.1, 60.0)[0], "30/1")
        self.assertEqual(mf.fps_standard_vicino(24.0, 30.0)[0], "24/1")

    def test_exact_fraction_goes_into_the_fps_filter(self):
        opts = {"limit_fps": True, "fps_value": 30, "fps_exact": "30000/1001"}
        self.assertIn("fps=30000/1001", mf.costruisci_filtri_video(opts, video("h264", "yuv420p", 1920, 1080)))


class GpuDecodeSafety(unittest.TestCase):
    """The GPU path dropped 31% of the frames of a 1080p AV1 film while ffmpeg exited with success."""

    def _cmd(self, mode, **extra):
        data = {"streams": [video("av1", "yuv420p", 1920, 1080)]}
        opts = dict({"mode": "video", "output_ext": "mp4", "vcodec": "hevc_qsv", "quality": 22, "look_ahead": True,
                     "audio": "copy", "subs": "copy", "timestamp": "", "limite_res": None}, **extra)
        return mf.build_ffmpeg_cmd(Path("in.mp4"), Path("out.mp4"), opts, data, hwaccel_decode=mode)

    @unittest.skipUnless(sys.platform == "win32", "the GPU paths are Windows only")
    def test_default_gpu_path_is_direct_qsv(self):
        cmd = self._cmd(True)
        self.assertEqual(cmd[cmd.index("-hwaccel") + 1], "qsv")
        self.assertNotIn("hwmap=derive_device=qsv", " ".join(cmd))   # frames are already QSV surfaces
        self.assertNotIn("-vf", cmd)                                  # nothing to filter: no filter chain at all

    @unittest.skipUnless(sys.platform == "win32", "the GPU paths are Windows only")
    def test_direct_qsv_scales_on_the_gpu_and_downloads_only_for_other_filters(self):
        self.assertEqual(self._cmd("qsv", limite_res=720)[self._cmd("qsv", limite_res=720).index("-vf") + 1],
                         "scale_qsv=w=1280:h=720")
        vf = self._cmd("qsv", limite_res=720, hflip=True)
        self.assertEqual(vf[vf.index("-vf") + 1], "scale_qsv=w=1280:h=720,hwdownload,format=nv12,hflip")

    @unittest.skipUnless(sys.platform == "win32", "the GPU paths are Windows only")
    def test_d3d11va_path_enlarges_the_surface_pool(self):
        cmd = self._cmd("d3d11va")
        self.assertEqual(cmd[cmd.index("-hwaccel") + 1], "d3d11va")
        self.assertEqual(cmd[cmd.index("-extra_hw_frames") + 1], "24")
        self.assertLess(cmd.index("-extra_hw_frames"), cmd.index("-i"))  # an input option
        self.assertIn("hwmap=derive_device=qsv,format=qsv", cmd[cmd.index("-vf") + 1])

    def test_software_decoding_has_no_hwaccel(self):
        self.assertNotIn("-hwaccel", self._cmd(False))

    def test_fallback_order(self):
        self.assertEqual(mf.HW_DECODE_MODES, ("qsv", "d3d11va"))

    def test_watchdog_stops_on_a_decoder_error_line(self):
        import queue, threading, time
        script = ("import sys,time\nprint('[AVHWFramesContext] Static surface pool size exceeded.', flush=True)\n"
                  "time.sleep(30)")
        t0 = time.time()
        esito, _ = mf.esegui_ffmpeg_con_watchdog([sys.executable, "-c", script], queue.Queue(), threading.Event(),
                                                  timeout_stallo=60, fatal_patterns=mf.GPU_DECODE_ERRORS)
        self.assertEqual(esito, "errore")
        self.assertLess(time.time() - t0, 15)

    def test_watchdog_ignores_those_lines_without_patterns(self):
        import queue, threading
        script = "print('Static surface pool size exceeded', flush=True)"
        esito, _ = mf.esegui_ffmpeg_con_watchdog([sys.executable, "-c", script], queue.Queue(), threading.Event(),
                                                  timeout_stallo=60)
        self.assertEqual(esito, "ok")


class DefaultCrfForDownscaled4K(unittest.TestCase):
    def test_4k_source_scaled_down_gets_16_with_qsv(self):
        self.assertEqual(mf.crf_suggerito(1920, 1080, "hevc_qsv", source_side=3840), 16)
        self.assertEqual(mf.crf_suggerito(1280, 720, "hevc_qsv", source_side=3840), 16)
        self.assertEqual(mf.crf_suggerito(1920, 1080, "av1_qsv", source_side=3840), 17)

    def test_4k_source_kept_at_4k_and_other_sources_keep_the_curve(self):
        self.assertEqual(mf.crf_suggerito(3840, 2160, "hevc_qsv", source_side=3840), 22)
        self.assertEqual(mf.crf_suggerito(1920, 1080, "hevc_qsv", source_side=1920), 20)
        self.assertEqual(mf.crf_suggerito(1280, 720, "hevc_qsv", source_side=2560), 18)

    def test_other_encoders_are_not_affected(self):
        self.assertEqual(mf.crf_suggerito(1920, 1080, "libx265", source_side=3840), 25)


class PredictBitrate(unittest.TestCase):
    def test_random_starts_are_spread_one_per_bin_and_inside_the_film(self):
        import random
        d, clip = 6000.0, 60.0
        for seed in range(50):
            starts = mf.predict_start_times(d, 3, clip, random.Random(seed))
            self.assertEqual(len(starts), 3)
            self.assertGreaterEqual(starts[0], d * 0.05)
            self.assertLessEqual(starts[-1] + clip, d * 0.95 + 1e-6)
            self.assertEqual(starts, sorted(starts))
            self.assertGreater(starts[1] - starts[0], 0)

    def test_short_file_gives_one_clip(self):
        self.assertEqual(mf.predict_start_times(30, 3, 60), [0.0])
        self.assertEqual(len(mf.predict_start_times(100, 3, 90)), 1)

    def test_typed_times_in_minutes_and_clock_format(self):
        self.assertEqual(mf.parse_start_times("13, 20.5", 6000, 60), [780.0, 1230.0])
        self.assertEqual(mf.parse_start_times("1:02:03; 5:30", 6000, 60), [3723.0, 330.0])

    def test_typed_times_reject_bad_input(self):
        for bad in ("abc", "", "100", "-1"):
            with self.assertRaises(ValueError):
                mf.parse_start_times(bad, 6000, 60)

    def test_clock_format_round_trips_through_the_parser(self):
        self.assertEqual(mf.format_clock(824.7), "0:13:44")
        self.assertEqual(mf.format_clock(3723), "1:02:03")
        self.assertEqual(mf.parse_start_times(mf.format_clock(824.7), 6000, 60), [824.0])

    def test_new_points_text_can_be_fed_back_as_selected_times(self):
        import random
        starts = mf.predict_start_times(6000, 4, 60, random.Random(1))
        text = ", ".join(mf.format_clock(s) for s in starts)
        parsed = mf.parse_start_times(text, 6000, 60)
        self.assertEqual(parsed, [float(int(s)) for s in starts])

    def test_summary_mean_min_max_and_size(self):
        s = mf.summarize_bitrates([483, 732, 505], 3000)
        self.assertAlmostEqual(s["mean"], 573.33, places=1)
        self.assertEqual((s["min"], s["max"]), (483, 732))
        self.assertAlmostEqual(s["size_mb"], 573.33 * 1000 * 3000 / 8 / 1_048_576, places=1)


class BitrateOverTime(unittest.TestCase):
    @staticmethod
    def packets(seconds, per_second, size_of):
        """per_second packets every second, size given by size_of(second)."""
        return [(s + i / per_second, size_of(s)) for s in range(seconds) for i in range(per_second)]

    def test_constant_bitrate_gives_the_same_value_in_every_minute(self):
        pk = self.packets(180, 24, lambda s: 1000)            # 24 packets/s x 1000 bytes = 192 kbps
        out = mf.bucket_bitrates(pk, 60, 180)
        self.assertEqual([t for t, _k in out], [0, 60, 120])
        for _t, k in out:
            self.assertAlmostEqual(k, 192.0, places=3)

    def test_each_minute_has_its_own_value(self):
        pk = self.packets(60, 10, lambda s: 3750) + [(60 + t, s) for t, s in self.packets(60, 10, lambda s: 4375)]
        out = mf.bucket_bitrates(pk, 60, 120)                  # 10 x 3750 x 8 = 300 kbps, then 350 kbps
        self.assertAlmostEqual(out[0][1], 300.0, places=3)
        self.assertAlmostEqual(out[1][1], 350.0, places=3)

    def test_last_partial_interval_uses_its_real_length(self):
        out = mf.bucket_bitrates(self.packets(150, 10, lambda s: 1250), 60, 150)   # 100 kbps everywhere
        self.assertEqual(len(out), 3)
        self.assertAlmostEqual(out[2][1], 100.0, places=3)     # 30 s long, not diluted over 60 s

    def test_a_tail_shorter_than_a_second_is_skipped(self):
        out = mf.bucket_bitrates(self.packets(120, 10, lambda s: 1250) + [(120.2, 9999)], 60, 120.5)
        self.assertEqual(len(out), 2)

    def test_empty_and_bad_step(self):
        self.assertEqual(mf.bucket_bitrates([], 60, 100), [])
        self.assertEqual(mf.bucket_bitrates([(0.0, 10)], 0, 100), [])

    def test_the_total_of_the_buckets_matches_the_total_size(self):
        pk = self.packets(300, 24, lambda s: 500 + 40 * (s % 7))
        out = mf.bucket_bitrates(pk, 30, 300)
        total_bits = sum(k * 1000 * 30 for _t, k in out)
        self.assertAlmostEqual(total_bits, sum(sz for _t, sz in pk) * 8, delta=1)

    def test_csv_text(self):
        data = [(0, 1234.56), (60, 800.0)]
        self.assertEqual(mf.bitrate_csv_text(data, 60), "start;end;kbps\n0:00:00;0:01:00;1234.6\n0:01:00;0:02:00;800.0\n")
        with_src = mf.bitrate_csv_text(data, 60, [(0, 2000.0)]).splitlines()
        self.assertEqual(with_src[0], "start;end;kbps;source_kbps")
        self.assertEqual(with_src[1].split(";")[-1], "2000.0")
        self.assertEqual(with_src[2].split(";")[-1], "")      # a source shorter than the output: empty cell

    def test_the_csv_is_saved_next_to_a_real_file(self):
        import queue
        import shutil
        import subprocess
        import tempfile
        if not shutil.which("ffmpeg"):
            self.skipTest("ffmpeg not available")
        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp) / "clip.mkv"
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=24",
                            "-t", "130", "-c:v", "mpeg4", str(dst)], check=True)
            q = queue.Queue()
            mf.save_bitrate_csv(dst, q)
            lines = (Path(tmp) / "clip_bitrate.csv").read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[0], "start;end;kbps")
            self.assertEqual(len(lines), 4)                    # 0-1, 1-2 and the 10 s tail
            self.assertEqual(q.get_nowait()[0], "detail")


if __name__ == "__main__":
    unittest.main()
