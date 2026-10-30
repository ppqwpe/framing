"""Framing 테스트 전부 돌리기 — 파일마다 따로 띄워서(서로 모듈 상태를 나누지 않게) 결과를 모은다

  python app/tests/run_tests.py            전부
  python app/tests/run_tests.py compare    이름에 compare 가 들어간 것만

UI 문법 검사 몇 개는 Node.js 가 있어야 돈다. PATH 에 없으면 그 검사만 건너뛰고 끝에 알려 준다.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")     # 콘솔이 CP949 여도 한글이 깨지지 않게
    wanted = argv[0] if argv else ""
    files = sorted(p for p in HERE.glob("test_*.py") if wanted in p.stem)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    total = failed = skipped = 0
    for p in files:
        start = time.time()
        r = subprocess.run([sys.executable, "-X", "utf8", str(p)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, cwd=HERE)
        out = r.stdout + r.stderr
        ran = re.search(r"^Ran (\d+) test", out, re.M)
        skip = re.search(r"skipped=(\d+)", out)
        n = int(ran.group(1)) if ran else 0
        total += n
        skipped += int(skip.group(1)) if skip else 0
        ok = r.returncode == 0
        failed += 0 if ok else 1
        print(f"{'OK  ' if ok else 'FAIL'} {p.name:<28} {n:>3}개 {time.time() - start:5.1f}초" + (f"  (건너뜀 {skip.group(1)})" if skip else ""))
        if not ok:
            print("\n".join("     " + line for line in out.strip().splitlines()[-30:]))
    print(f"\n{len(files)}개 파일 · 테스트 {total}개 · 실패한 파일 {failed}개 · 건너뜀 {skipped}개")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
