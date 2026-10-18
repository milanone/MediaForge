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

    @unittest.skipUnless(sys.platform == "win32", "the d3d11va path is Windows only")
    def test_gpu_command_enlarges_the_surface_pool(self):
        data = {"streams": [video("av1", "yuv420p", 1920, 1080)]}
        opts = {"mode": "video", "output_ext": "mp4", "vcodec": "hevc_qsv", "quality": 22, "look_ahead": True,
                "audio": "copy", "subs": "copy", "timestamp": "", "limite_res": None}
        cmd = mf.build_ffmpeg_cmd(Path("in.mp4"), Path("out.mp4"), opts, data)
        self.assertIn("-hwaccel", cmd)
        self.assertEqual(cmd[cmd.index("-extra_hw_frames") + 1], "24")
        self.assertLess(cmd.index("-extra_hw_frames"), cmd.index("-i"))  # an input option

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


if __name__ == "__main__":
    unittest.main()
