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
