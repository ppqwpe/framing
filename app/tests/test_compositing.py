"""합성(누크) 작업자 기준 점검: 색 표기 · EXR 선형 곡선 · 시퀀스 이름 · 타임코드 · 소리 비트 깊이"""
from pathlib import Path
import shutil, subprocess, sys, tempfile, unittest

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from framing import core  # noqa: E402  (FFMPEG_HOME 등 런처 설정을 먼저 태운다)
from framing.cuts import StartTimecode  # noqa: E402
from framing.env import cv2, np  # noqa: E402
from framing.frames import TransferCurve  # noqa: E402
from framing.jobs_convert import ConvertTarget, ImageTarget  # noqa: E402
from framing.media import FFmpegTool, OutputColorTags, ProbeInfo  # noqa: E402
from framing.sequence import SequenceNaming  # noqa: E402
from framing.util import ImageIO  # noqa: E402

FF = FFmpegTool()
HAVE_FF = bool(FF.find("ffmpeg") and FF.find("ffprobe"))


class OutputColorTagsTests(unittest.TestCase):
    def test_flags_after_input_become_setparams_on_existing_chain(self):
        out = OutputColorTags.fold(["-y", "-i", "a.%04d.png", "-vf", "scale=out_range=tv", "-c:v", "prores_ks",
                                    "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
                                    "-color_range", "tv", "o.mov"])
        chain = out[out.index("-vf") + 1]
        self.assertTrue(chain.startswith("scale=out_range=tv,setparams="))
        for part in ("color_primaries=bt709", "color_trc=bt709", "colorspace=bt709", "range=tv"):
            self.assertIn(part, chain)

    def test_no_chain_inserts_one_before_output(self):
        out = OutputColorTags.fold(["-i", "x.png", "-color_trc", "arib-std-b67", "o.mov"])
        self.assertEqual(out[-3:], ["-vf", "setparams=color_trc=arib-std-b67", "o.mov"])

    def test_stream_copy_and_input_side_flags_are_left_alone(self):
        copy = ["-i", "a.mp4", "-c", "copy", "-color_trc", "bt709", "b.mp4"]
        self.assertEqual(OutputColorTags.fold(copy), copy)
        before = ["-color_range", "pc", "-i", "a.mp4", "-c:v", "libx264", "b.mp4"]
        self.assertEqual(OutputColorTags.fold(before), before)

    def test_rgb_matrix_is_not_stamped_on_yuv(self):
        out = OutputColorTags.fold(["-i", "a.png", "-colorspace", "rgb", "-color_trc", "bt709", "o.mov"])
        self.assertNotIn("colorspace=", out[-2])


class TransferCurveTests(unittest.TestCase):
    def test_rec709_matches_nuke_rec709_not_gamma_2_4(self):
        self.assertAlmostEqual(float(TransferCurve._rec709(np.array(0.18))), 0.4090, places=3)
        x = np.linspace(0, 1, 101)
        self.assertLess(float(np.abs(TransferCurve._rec709_inverse(TransferCurve._rec709(x)) - x).max()), 1e-9)
        self.assertLess(float(np.abs(TransferCurve._srgb_inverse(TransferCurve._srgb(x)) - x).max()), 1e-9)

    def test_filter_escapes_windows_drive_colon(self):
        chain = TransferCurve.filter("to_srgb")
        self.assertIn("lut1d=file='", chain)
        self.assertNotRegex(chain.split("file='")[1].split("'")[0], r"(?<!\\):")

    def test_which_curve_to_remove_for_exr(self):
        self.assertEqual(TransferCurve.linearize_from("bt709"), "from_rec709")
        self.assertEqual(TransferCurve.linearize_from(""), "from_rec709")
        self.assertEqual(TransferCurve.linearize_from("iec61966-2-1"), "from_srgb")
        self.assertIsNone(TransferCurve.linearize_from("arib-std-b67"))
        self.assertIsNone(TransferCurve.linearize_from("smpte2084"))

    def test_image_conversion_picks_the_curve(self):
        self.assertEqual(ImageTarget.curve(".exr", "png")[0], "to_srgb")
        self.assertEqual(ImageTarget.curve(".exr", "dpx")[0], "to_rec709")
        self.assertEqual(ImageTarget.curve(".png", "exr")[0], "from_srgb")
        self.assertEqual(ImageTarget.curve(".dpx", "exr")[0], "from_rec709")
        self.assertEqual(ImageTarget.curve(".png", "jpg")[0], "")


class SequenceNamingTests(unittest.TestCase):
    def test_keeps_name_start_padding_and_extension(self):
        naming = SequenceNaming.of([Path("plate.1001.exr"), Path("plate.1002.exr")], Path("shot"))
        self.assertEqual(naming.name(0, "_reversed"), "plate_reversed.1001.exr")
        self.assertEqual(naming.name(47, "_retimed"), "plate_retimed.1048.exr")
        self.assertEqual(naming.pattern("_reversed"), "plate_reversed.%04d.exr")

    def test_underscore_and_bare_numbers(self):
        self.assertEqual(SequenceNaming.of([Path("A_0012.dpx")], Path("x")).name(0, "_r"), "A_r_0012.dpx")
        self.assertEqual(SequenceNaming.of([Path("000001.png")], Path("shot")).name(1, "_r"), "shot_r.000002.png")


class TimecodeAndAudioTests(unittest.TestCase):
    def test_start_timecode_plus_frames(self):
        self.assertEqual(StartTimecode("10:20:30:12", 23.976).plus(24), "10:20:31:12")
        self.assertEqual(StartTimecode("00:59:59:23", 24).plus(1), "01:00:00:00")
        self.assertFalse(StartTimecode("01:00:00;00", 29.97).valid)      # 드롭프레임은 건드리지 않는다

    def test_mov_audio_keeps_24bit(self):
        self.assertEqual(ConvertTarget.pcm(ProbeInfo(acodec="pcm_s24le", audio_bits=24)), ["-c:a", "pcm_s24le"])
        self.assertEqual(ConvertTarget.pcm(ProbeInfo(acodec="aac")), ["-c:a", "pcm_s24le"])
        self.assertEqual(ConvertTarget.pcm(ProbeInfo(acodec="pcm_s16le", audio_bits=16)), ["-c:a", "pcm_s16le"])
        self.assertEqual(ConvertTarget.pcm(ProbeInfo()), ["-an"])

    def test_timecode_args(self):
        self.assertEqual(ProbeInfo(timecode="01:00:00:00").timecode_args(), ["-timecode", "01:00:00:00"])
        self.assertEqual(ProbeInfo().timecode_args(), [])


class FpsTargetTests(unittest.TestCase):
    def test_ntsc_rates_are_exact_fractions(self):
        from framing.jobs_convert import FpsTarget
        self.assertEqual(FpsTarget.rate(23.976)[0], "24000/1001")
        self.assertEqual(FpsTarget.rate("29.97")[0], "30000/1001")
        self.assertEqual(FpsTarget.rate(59.94)[0], "60000/1001")
        self.assertEqual(FpsTarget.rate(25)[0], "25/1")
        with self.assertRaises(core.FramingError):
            FpsTarget.rate(0)

    def test_tempo_splits_outside_atempo_range(self):
        from framing.jobs_convert import FpsTarget
        self.assertEqual(FpsTarget.tempo(0.96), "atempo=0.960000")
        self.assertEqual(FpsTarget.tempo(0.4), "atempo=0.5,atempo=0.800000")
        self.assertEqual(FpsTarget.tempo(5.0), "atempo=2.0,atempo=2.0,atempo=1.250000")


class FpsReviewTests(unittest.TestCase):
    """합성 작업자 검토에서 나온 것: 타임코드 칸 · VFR 평균 · 알파 분리 그래프."""

    def test_timecode_frame_field_is_moved_not_overflowed(self):
        from framing.jobs_convert import FpsTarget
        info = ProbeInfo(timecode="10:00:00:24")
        self.assertEqual(FpsTarget._timecode(info, 25, 24), ["-timecode", "10:00:00:23"])
        self.assertEqual(FpsTarget._timecode(ProbeInfo(timecode="01:00:00:23"), 24, 25), ["-timecode", "01:00:00:24"])
        self.assertEqual(FpsTarget._timecode(ProbeInfo(timecode="01:00:00;00"), 29.97, 24), [])
        self.assertEqual(FpsTarget._timecode(ProbeInfo(), 25, 24), [])

    def test_vfr_uses_actual_average(self):
        from framing.jobs_convert import FpsTarget
        vfr = ProbeInfo(fps=30, reported_frames=111, video_duration=4.6)
        self.assertAlmostEqual(FpsTarget.source_rate(vfr), 111 / 4.6, places=3)
        cfr = ProbeInfo(fps=25, reported_frames=100, video_duration=4.0)
        self.assertEqual(FpsTarget.source_rate(cfr), 25)

    def test_alpha_is_carried_in_its_own_plane(self):
        from framing.jobs_convert import FpsTarget
        from framing.frames import ColorProfile
        chain = FpsTarget._keep_alpha("framerate=fps=24", ColorProfile(pix_fmt="yuva444p12le"))
        self.assertIn("shuffleplanes=map0=3", chain)
        self.assertIn("mergeplanes=", chain)
        self.assertNotIn("alphamerge", chain)            # alphamerge 는 8bit 로 합친다
        self.assertEqual(FpsTarget._keep_alpha("fps=24", ColorProfile(pix_fmt="yuv422p10le")), "fps=24")


@unittest.skipUnless(HAVE_FF, "ffmpeg 이 없습니다")
class FpsAlphaRunTests(unittest.TestCase):
    """4444 알파 소스를 겹치기로 25 → 24: 알파 값이 그대로 남아야 한다 (예전엔 전부 불투명)."""

    def test_blend_keeps_alpha_values(self):
        tmp = Path(tempfile.mkdtemp(prefix="framing_fps_alpha_"))
        try:
            src = tmp / "a.mov"
            subprocess.run([FF.find("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i",
                            "color=black:s=160x90:r=25:d=1,format=yuva444p10le,geq=lum=512:cb=512:cr=512:a='200+X*4'",
                            "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le",
                            "-alpha_bits", "16", str(src)], check=True)
            run = subprocess.run([sys.executable, str(APP / "main.py"), "convert", str(src), "--to", "fps",
                                  "--target-fps", "24", "--fps-mode", "blend", "--out", str(tmp / "o"), "--no-pause"],
                                 stdin=subprocess.DEVNULL, capture_output=True)
            self.assertEqual(run.returncode, 0, run.stdout[-400:])
            made = next((tmp / "o").glob("*.mov"))
            raw = subprocess.run([FF.find("ffmpeg"), "-v", "error", "-i", str(made), "-frames:v", "1", "-f", "rawvideo",
                                  "-pix_fmt", "yuva444p16le", "-"], capture_output=True).stdout
            alpha = np.frombuffer(raw, np.uint16).reshape(4, 90, 160)[3, 45, ::40] // 64
            self.assertEqual([int(v) for v in alpha], [200, 360, 520, 680])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


@unittest.skipUnless(HAVE_FF, "ffmpeg 이 없습니다")
class FpsConvertRunTests(unittest.TestCase):
    """25fps ProRes 를 24 로: 컨폼은 장 수 그대로, 복제는 길이 그대로."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="framing_fps_"))
        cls.src = cls.tmp / "plate_25.mov"
        subprocess.run([FF.find("ffmpeg"), "-v", "error", "-y", "-f", "lavfi", "-i",
                        "testsrc2=size=320x180:rate=25:duration=2", "-f", "lavfi", "-i",
                        "sine=sample_rate=48000:duration=2", "-c:v", "prores_ks", "-profile:v", "3",
                        "-pix_fmt", "yuv422p10le", "-timecode", "01:00:00:00", "-c:a", "pcm_s24le",
                        "-shortest", str(cls.src)], check=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def convert(self, mode):
        out = self.tmp / mode
        run = subprocess.run([sys.executable, str(APP / "main.py"), "convert", str(self.src), "--to", "fps",
                              "--target-fps", "24", "--fps-mode", mode, "--out", str(out), "--no-pause"],
                             stdin=subprocess.DEVNULL, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stdout[-400:])
        made = next(out.glob("*.mov"))
        self.assertEqual(made.name, "plate_25_24fps.mov")
        return FF.probe(made), made

    def frames(self, path):
        out = subprocess.run([FF.find("ffprobe"), "-v", "error", "-count_frames", "-select_streams", "v",
                              "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", str(path)],
                             capture_output=True, text=True)
        return int(out.stdout.split()[0])

    def test_conform_keeps_every_frame(self):
        info, made = self.convert("conform")
        self.assertAlmostEqual(info.fps, 24.0)
        self.assertEqual(self.frames(made), 50)
        self.assertEqual(info.timecode, "01:00:00:00")
        self.assertEqual(info.acodec, "pcm_s24le")

    def test_dup_keeps_length(self):
        info, made = self.convert("dup")
        self.assertEqual(self.frames(made), 48)
        self.assertEqual(info.vcodec, "prores")


@unittest.skipUnless(HAVE_FF, "ffmpeg 이 없습니다")
class ExrDpxIoTests(unittest.TestCase):
    """이 OpenCV 는 EXR · DPX 를 못 연다. ffmpeg 경유로 값을 그대로 읽고 쓰는지."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="framing_exr_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_exr_round_trip_keeps_float_and_over_one(self):
        image = np.zeros((8, 10, 3), np.float32)
        image[..., 2] = 2.5                         # BGR 의 R — 1.0 넘는 하이라이트
        image[..., 1] = 0.18
        path = self.tmp / "a.1001.exr"
        self.assertTrue(ImageIO.write(path, image))
        back = ImageIO.read(path, cv2.IMREAD_UNCHANGED)
        self.assertEqual(back.dtype, np.float32)
        self.assertAlmostEqual(float(back[0, 0, 2]), 2.5, places=2)
        self.assertAlmostEqual(float(back[0, 0, 1]), 0.18, places=3)

    def test_dpx_round_trip_10bit(self):
        image = (np.arange(8 * 10 * 3).reshape(8, 10, 3) * 64).astype(np.uint16)
        path = self.tmp / "a.1001.dpx"
        self.assertTrue(ImageIO.write(path, image, dpx_bits=10))
        back = ImageIO.read(path, cv2.IMREAD_UNCHANGED)
        self.assertEqual(back.dtype, np.uint16)
        self.assertLessEqual(int(np.abs(back.astype(int) - image.astype(int)).max()), 64)

    def test_reverse_exr_sequence_stays_exr_with_same_numbers(self):
        src = self.tmp / "plate"
        src.mkdir()
        for i in range(3):
            ImageIO.write(src / f"plate.{1001 + i}.exr", np.full((4, 4, 3), 0.1 * (i + 1), np.float32))
        out = self.tmp / "out"
        run = subprocess.run([sys.executable, str(APP / "main.py"), "reverse", str(src), "--out", str(out),
                              "--no-pause"], stdin=subprocess.DEVNULL, capture_output=True)
        self.assertEqual(run.returncode, 0, run.stdout[-400:])
        made = sorted(p.name for p in out.rglob("*.exr"))
        self.assertEqual(made, ["plate_reversed.1001.exr", "plate_reversed.1002.exr", "plate_reversed.1003.exr"])
        first = ImageIO.read(next(out.rglob("plate_reversed.1001.exr")), cv2.IMREAD_UNCHANGED)
        self.assertAlmostEqual(float(first[0, 0, 0]), 0.3, places=3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
