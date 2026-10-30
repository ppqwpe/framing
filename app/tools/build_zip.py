"""배포 zip 만들기 — 받은 사람이 풀어서 Framing.bat 만 누르면 되게 (Windows)

  app\\python\\python.exe app\\tools\\build_zip.py            → 이 폴더 옆에 Framing_1.0_windows.zip
  app\\python\\python.exe app\\tools\\build_zip.py D:\\x.zip  → 원하는 곳에

zip 안의 맨 위 폴더 이름은 Framing. 쓰다가 생긴 것(app/work, 결과, __pycache__)과 깃 폴더,
Mac 소스 실행용 가상환경은 넣지 않는다.
파이썬 · 라이브러리 · ffmpeg · RIFE(app/python, app/lib, app/ffmpeg, app/rife)는 깃에는 없지만 zip 에는 꼭 들어가야
하므로, 이 폴더에 있어야 만든다.
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SKIP = {ROOT / "results", ROOT / "app" / "work", ROOT / ".git", ROOT / "app" / ".venv-mac",
        ROOT / "app" / "web" / "data", ROOT / "app" / "mac" / "build", ROOT / "app" / "mac" / "dist"}
SKIP_NAMES = {"__pycache__", ".gitignore", ".gitattributes", ".env"}
NEEDED = (ROOT / "app" / "python" / "python.exe", ROOT / "app" / "lib",
          ROOT / "app" / "ffmpeg" / "ffmpeg.exe", ROOT / "app" / "ffmpeg" / "ffprobe.exe",
          ROOT / "app" / "rife" / "rife-ncnn-vulkan.exe")


def files() -> list[Path]:
    out = []
    for p in sorted(ROOT.rglob("*")):
        if any(p == s or s in p.parents for s in SKIP) or any(part in SKIP_NAMES for part in p.parts):
            continue
        if p.is_file() and not (p.parent == ROOT and p.suffix == ".zip"):   # 맨 위의 옛 배포 zip 만 뺀다 (python312.zip 은 꼭 필요)
            out.append(p)
    return out


def build(dest: Path) -> tuple[int, float]:
    missing = [str(p) for p in NEEDED if not p.exists()]
    if missing:
        raise SystemExit("런타임이 없어 zip 을 만들 수 없습니다 (배포 zip 에서 app\\python, app\\lib, app\\ffmpeg, app\\rife 를 복사해 오세요): "
                         + ", ".join(missing))
    tmp = dest.with_suffix(".tmp")
    items = files()
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in items:
            z.write(p, Path("Framing") / p.relative_to(ROOT))
    tmp.replace(dest)
    return len(items), dest.stat().st_size / 1e6


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT.parent / "Framing_1.0_windows.zip"
    n, mb = build(target)
    print(f"{target} · 파일 {n}개 · {mb:.0f}MB")
