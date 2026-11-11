"""역재생 작업"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Optional, Sequence

from .errors import FramingError
from .options import JobResult
from .util import Console, FrameRate, Progress, hms
from .media import ProbeInfo, collect_images
from .frames import ColorProfile, PixelPlan, TransferCurve
from .job import Job
from .jobs_extract import Delivery
from .sequence import SequenceNaming


class ReverseJob(Job):
    """영상이나 시퀀스를 거꾸로. 픽셀은 건드리지 않는다."""

    KEY = "reverse"
    ALIASES = ("rev",)
    TITLE = "Reverse"
    HINT = "영상 · 시퀀스를 거꾸로"
    NOTE = """
    무엇으로 받을지 고를 수 있습니다  (--result)
      auto   알아서 (기본). 8bit 는 mp4, 10bit·알파는 ProRes mov
      mp4    어디서나 열리는 h264 하나
      mov    누크용 ProRes 마스터 — 계조와 알파를 지킵니다
      seq    PNG 시퀀스만. 영상은 만들지 않습니다
      both   시퀀스 + 확인용 영상 둘 다

    auto 가 소스를 보고 정하는 기준
      8bit 4:2:0        h264 mp4 (소리도 같이 뒤집습니다)
      10bit · 4:2:2     ProRes 422 HQ mov — 계조를 안 깎으려고
      알파가 있으면     ProRes 4444 mov — 투명도를 지키려고
    무엇이 들어오든 h264 로 내보내면 순서 바꾸려다 원본 계조와 알파를 죽이게 됩니다.

    시퀀스 폴더를 넣으면 (auto · seq · both) 파일을 역순으로 복사만 합니다 —
    픽셀도 형식도 그대로. 이름 · 시작 번호 · 자릿수도 원본을 따릅니다
    (plate.1001.exr → plate_reversed.1001.exr). 영상 하나만 원하면 mp4 · mov 로 고르세요.

    소스의 색 규격(bt709 / bt601 · 제한범위 / 전체범위)은 결과에 그대로 찍어 둡니다.
    안 찍어 두면 다음 도구가 추측하고, 그 추측이 어긋나는 게 색이 밀리는 진짜 원인입니다.


    30초가 넘는 영상은 ffmpeg 의 reverse 필터가 전체를 램에 올려서 위험하므로,
    프레임을 경유하는 방식으로 자동 전환합니다. 이때 소리는 빠집니다."""
    ACCEPTS_FOLDER = True
    RAM_SAFE_SECONDS = 30.0     # 이보다 길면 ffmpeg reverse 필터 대신 프레임 경유

    def run(self, target: Path) -> JobResult:
        if self._opt.delivery == 'bundle':
            raise FramingError('영상 + 프레임 폴더는 리타임용입니다. 역재생은 시퀀스 + MP4를 선택하세요.')
        if target.is_dir():
            return self._reverse_sequence(target)
        return self._reverse_video(target)

    # --- 시퀀스 -----------------------------------------------------------

    def _reverse_sequence(self, target: Path) -> JobResult:
        """
        시퀀스는 파일 순서만 뒤집는다 — 픽셀은 손대지 않는다.

        받는 모습이 시퀀스가 아니면(mp4 · mov 하나만 원하면) 뒤집은 프레임을
        임시 자리에 두었다가 영상만 남긴다. 안 쓸 폴더를 원본 옆에 두지 않는다.
        """
        images = collect_images(target)
        if not images:
            raise FramingError("이미지가 없습니다.")
        # 시퀀스를 넣었으면 시퀀스로 돌려준다. 알아서(auto)가 EXR 을 mp4 로 바꾸면
        # 선형 float 이 8bit 로 깎이고 형식까지 달라진다 — 합성에서 쓸 수 없는 결과다.
        key = (self._opt.delivery or "auto").lower()
        how = Delivery.pick("seq" if key == "auto" else key)
        if how.keeps_frames:
            return self._reverse_into(target, images, how,
                                      self._output_dir(target, "_reversed"), keep=True)
        stage = Path(tempfile.mkdtemp(prefix="framing_revseq_"))
        try:
            return self._reverse_into(target, images, how, stage, keep=False)
        finally:
            shutil.rmtree(stage, ignore_errors=True)

    def _reverse_into(self, target: Path, images: Sequence[Path], how: Delivery,
                      folder: Path, keep: bool) -> JobResult:
        result = JobResult(target=target)
        naming = SequenceNaming.of(images, target)
        total = len(images)
        written = []
        for i, source in enumerate(reversed(images)):
            self._abort_if_asked()
            destination = folder / naming.name(i, "_reversed")
            shutil.copyfile(source, destination)
            written.append(destination)
            Progress.tick(i + 1, total)
        last = naming.start + total - 1
        if keep:
            result.outputs = written
            result.output_dir = folder
            result.message = (f"{total}프레임 역순 복사 (픽셀 그대로) · "
                              f"{naming.name(0, '_reversed')} … {last}")
        else:
            result.output_dir = target.parent
            result.message = f"{total}프레임 역순"
        made = self._mux(folder, naming.pattern("_reversed"), total, how, target,
                         start=naming.start, linear=SequenceNaming.linear(naming.ext))
        if made:
            result.outputs.append(made)
            result.message += f"  ·  {made.name}"
        elif not keep:
            raise FramingError("영상을 만들지 못했습니다. ffmpeg 을 확인해 주세요.")
        result.ok = True
        return result

    def _mux(self, folder: Path, pattern: str, total: int,
             how: Delivery, target: Path,
             profile: Optional[ColorProfile] = None,
             start: int = 1, linear: bool = False) -> Optional[Path]:
        """
        뒤집힌 프레임들을 영상 하나로 묶는다.

        묶은 것을 어디에 둘지는 프레임을 남기느냐가 정한다. 남기면 그 폴더 안에
        확인용으로, 안 남기면 원본 옆에 결과물로.

        profile 은 **이 프레임들이 어디서 왔는지**다. 영상을 펴서 나온 프레임이면
        그 소스의 규격을 넘겨야 한다 — 안 넘기면 편 기준과 접는 기준이 달라져
        주황·하늘색이 밀린다. 진짜 그림 파일이면 되돌릴 원본이 없으니 빈 프로필.
        """
        if not how.makes_video or not self._ff.find("ffmpeg"):
            return None
        if how.keeps_frames and not self._opt.make_mp4:
            return None
        profile = profile if profile is not None else ColorProfile()
        encoder, ext, why = how.encode(profile)
        if not ext:
            return None
        Console.info(f"묶기  {why}")
        made = (folder / f"reversed_{total}f{ext}" if how.keeps_frames
                else self._output_file(target, "_reversed", ext))
        yuv = Delivery.needs_yuv(encoder)
        # EXR 은 선형이다. 곡선 없이 영상에 담으면 어둡게 나온다 — Rec.709 곡선을 씌운다
        curve = TransferCurve.filter("to_rec709", alpha=not yuv) if linear else ""
        shape = ColorProfile.join(curve, profile.to_yuv_filter() if yuv else "")
        code = self._ff.run(["-y", "-framerate", str(self._opt.fps), "-start_number", str(start),
                             "-i", str(folder / pattern), *shape, *encoder,
                             *profile.stamp_args(), "-an", str(made)], quiet=True)
        return made if code == 0 and made.exists() else None

    # --- 영상 -------------------------------------------------------------

    def _encoder_for(self, profile: ColorProfile) -> tuple[list[str], str, str]:
        """
        (인코더 인자, 확장자, 설명)

        Reverse 는 순서만 바꾸는 작업이다. 그런데 늘 8bit 4:2:0 h264 로 뱉으면
        10bit 도 4:2:2 도 알파도 여기서 조용히 죽는다. 순서 바꾸려다 원본을 깎는 셈이다.
        무엇으로 받을지는 Delivery 가 안다 — 기본(알아서)이면 소스를 보고 정한다.
        """
        how = Delivery.pick(self._opt.delivery)
        args, ext, why = how.encode(profile)
        if ext == ".mov":
            args = list(args) + ["-an"]    # ProRes 에는 소리를 싣지 않는다
        return list(args), ext, why

    def _reverse_video(self, target: Path) -> JobResult:
        if Delivery.pick(self._opt.delivery).keeps_frames:
            return self._reverse_video_to_frames(target)
        result = JobResult(target=target, size_in=target.stat().st_size)
        self._ff.ensure()
        info = self._ff.probe(target)
        profile = self._color(target, info)
        self._announce_color(profile)
        duration = info.duration if info else 0.0
        encoder, ext, why = self._encoder_for(profile)
        Console.info(f"할 일  거꾸로 · {why}")
        destination = self._output_file(target, "_reversed", ext)
        keeps_audio = ext == ".mp4"

        if duration and duration > self.RAM_SAFE_SECONDS:
            Console.step(f"{hms(duration)} 짜리라 프레임을 경유합니다 (메모리 안전, 소리는 빠집니다)")
            self._reverse_via_frames(target, destination, info, profile, encoder)
        else:
            audio = ["-af", "areverse"] if keeps_audio else ["-an"]
            code = self._ff.run(
                ["-y", "-i", str(target), "-vf", "reverse",
                 *audio, *encoder, *profile.tag_args(),
                 *profile.range_bsf(encoder), str(destination)])
            if code != 0 or not destination.exists():
                Console.step("한 번에 뒤집기 실패 — 프레임을 경유해 다시 시도합니다")
                self._reverse_via_frames(target, destination, info, profile, encoder)

        if not destination.exists():
            raise FramingError("역재생 파일이 만들어지지 않았습니다.")
        result.outputs = [destination]
        result.output_dir = destination.parent
        result.size_out = destination.stat().st_size
        result.ok = True
        result.message = f"{hms(duration)} → {destination.name}"
        return result

    def _reverse_video_to_frames(self, target: Path) -> JobResult:
        """
        영상을 거꾸로 된 PNG 시퀀스로. 누크로 바로 들고 갈 때 쓴다.

        중간 그림은 소스 계조에 맞춰 받는다 — 10bit 을 8bit 로 거치면
        여기서 계조가 죽고 되돌릴 방법이 없다.
        """
        result = JobResult(target=target, size_in=target.stat().st_size)
        self._ff.ensure()
        info = self._ff.probe(target)
        profile = self._color(target, info)
        self._announce_color(profile)
        folder = self._output_dir(target, "_reversed")
        workspace = Path(tempfile.mkdtemp(prefix="framing_revf_"))
        try:
            code = self._ff.run(["-y", "-i", str(target),
                                 *ColorProfile.join(profile.read_filter()),
                                 "-c:v", "png", *PixelPlan.still_args(profile), "-an",
                                 str(workspace / "f.%06d.png")])
            frames = sorted(workspace.glob("f.*.png"))
            if code != 0 and not frames:
                raise FramingError("프레임 분해에 실패했습니다.")
            total = len(frames)
            for index, source in enumerate(reversed(frames), 1):
                self._abort_if_asked()
                shutil.move(str(source), str(folder / f"rev.{index:04d}.png"))
                Progress.tick(index, total)
            result.outputs = sorted(folder.glob("rev.*.png"))
            result.output_dir = folder
            result.ok = True
            result.message = f"{total}프레임 역순 시퀀스"
            made = self._mux(folder, "rev.%04d.png", total,
                             Delivery.pick(self._opt.delivery), target, profile)
            if made:
                result.outputs.append(made)
                result.message += f"  ·  {made.name}"
            return result
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    def _reverse_via_frames(self, target: Path, destination: Path,
                            info: Optional[ProbeInfo], profile: ColorProfile,
                            encoder: Sequence[str]) -> None:
        """
        긴 영상용. ffmpeg 의 reverse 필터는 전체를 메모리에 올려서 위험하다.

        중간 그림은 소스에 맞춰 받는다 — 10bit 소스를 8bit PNG 로 거치면
        여기서 계조가 죽고, 되돌릴 방법이 없다.
        """
        fps = (info.fps if info and info.fps else float(self._opt.fps)) or 24.0
        still = PixelPlan.still_args(profile)
        workspace = Path(tempfile.mkdtemp(prefix="framing_rev_"))
        try:
            code = self._ff.run(["-y", "-i", str(target),
                                 *ColorProfile.join(profile.read_filter()),
                                 "-c:v", "png", *still, "-an",
                                 str(workspace / "f.%06d.png")])
            frames = sorted(workspace.glob("f.*.png"))
            if code != 0 and not frames:
                raise FramingError("프레임 분해에 실패했습니다.")
            for index, source in enumerate(reversed(frames), 1):
                source.rename(workspace / f"r.{index:06d}.png")
            self._ff.run(["-y", "-framerate", FrameRate.from_value(fps).text,
                          "-i", str(workspace / "r.%06d.png"),
                          "-vf", profile.to_yuv_filter(), *encoder,
                          *profile.tag_args(), *profile.range_bsf(encoder),
                          "-an", str(destination)])
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
