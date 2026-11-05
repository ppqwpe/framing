"""Framing 로고 만들기 — 앱 아이콘(ico · png), 따로 쓸 로고 PNG 묶음, 화면 속 픽셀 마스코트

  app/icon/framing.ico · framing.png · framing.icns  앱 · 바로가기 · 창 아이콘 (icns 는 Mac)
  logo/framing_icon_{1024,512,256,128,64,32}.png  아이콘만 (투명 바탕)
  logo/framing_logo_dark.png · _light.png         아이콘 + 픽셀 글자 (어두운 · 밝은 바탕용, 투명)
  logo/framing_text_dark.png · _light.png         픽셀 글자만 — i 의 점은 뷰파인더와 같은 빨강
  app/framing/ui.html  의 // <alien> ... // </alien> 사이   화면 왼쪽 위 · 첫 화면의 외계인 픽셀 지도

재료: logo/source/alien_head.png (ALIENMEOW 캐릭터 머리 · 초록 바탕, Griding 과 같은 그림)
  python app/tools/make_logo.py
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

APP = Path(__file__).resolve().parent.parent
ROOT = APP.parent
HEAD = ROOT / "logo" / "source" / "alien_head.png"
UI = APP / "framing" / "ui.html"
NAME = "Framing"
TILE, RED = (21, 24, 29, 255), (220, 40, 30, 255)
S = 1024
HEAD_SCALE = 0.50          # 아이콘 한 변 대비 머리(더듬이 포함) 크기
CORNER_MARGIN = 175        # 뷰파인더 모서리 네 개의 바깥 여백 (상하좌우 같게)
CORNER_LEN, CORNER_W = 150, int(S * 0.032)

# 픽셀 글자 — 한 줄에 한 행, '#' = 글자색 · 'o' = 빨강(i 의 점) · '.' = 빈칸. 높이 9칸 (7칸 + 아래로 내려가는 2칸)
GLYPHS = {
    "F": ["#####", "#....", "#....", "####.", "#....", "#....", "#....", ".....", "....."],
    "r": [".....", ".....", "#.##.", "##..#", "#....", "#....", "#....", ".....", "....."],
    "a": [".....", ".....", ".###.", "....#", ".####", "#...#", ".####", ".....", "....."],
    "m": [".....", ".....", "####.", "#.#.#", "#.#.#", "#.#.#", "#.#.#", ".....", "....."],
    "i": ["o", ".", "#", "#", "#", "#", "#", ".", "."],
    "n": [".....", ".....", "####.", "#...#", "#...#", "#...#", "#...#", ".....", "....."],
    "g": [".....", ".....", ".####", "#...#", "#...#", "#...#", ".####", "....#", "####."],
}


class PixelText:
    """GLYPHS 로 쓴 각진 글자 — 한 칸 = cell px 정사각형"""

    def __init__(self, text: str = NAME, gap: int = 1):
        self.text, self.gap = text, gap

    @property
    def grid(self) -> list[str]:
        rows = [""] * 9
        for i, ch in enumerate(self.text):
            for r in range(9):
                rows[r] += GLYPHS[ch][r] + ("." * self.gap if i < len(self.text) - 1 else "")
        return rows

    def render(self, cell: int, color, accent=RED) -> Image.Image:
        rows = self.grid
        img = Image.new("RGBA", (len(rows[0]) * cell, len(rows) * cell), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        for y, row in enumerate(rows):
            for x, c in enumerate(row):
                if c != ".":
                    d.rectangle((x * cell, y * cell, (x + 1) * cell - 1, (y + 1) * cell - 1), fill=accent if c == "o" else color)
        return img


class Logo:
    def __init__(self, head_path: Path = HEAD):
        self.head = self._cut_out(Image.open(head_path).convert("RGB"))

    @staticmethod
    def _cut_out(img: Image.Image) -> Image.Image:
        """바탕 초록을 투명으로 · 목 아래(폭이 머리의 35% 아래로 좁아지는 곳)를 자른다"""
        a = np.asarray(img).astype(int)
        bg = np.median(a.reshape(-1, 3), 0)
        alpha = np.clip((np.abs(a - bg).sum(2) - 40) * 6, 0, 255).astype(np.uint8)
        im = Image.fromarray(np.dstack([a.astype(np.uint8), alpha]), "RGBA")
        im = im.crop(im.getbbox())
        w = (np.asarray(im.getchannel("A")) > 128).sum(1)
        cut = next((y for y in range(int(w.argmax()), len(w)) if w[y] < w.max() * 0.35), len(w))
        im = im.crop((0, 0, im.size[0], cut))
        return im.crop(im.getbbox())

    def _mark(self, size: int, tile: bool) -> Image.Image:
        """뷰파인더 모서리 네 개 + 정중앙의 머리. tile=False 면 바탕 없이 (화면 속 마스코트용)"""
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        k = size / S
        if tile:
            ImageDraw.Draw(img).rounded_rectangle((0, 0, size - 1, size - 1), radius=int(size * 0.22), fill=TILE)
        h = size * HEAD_SCALE / max(self.head.size)
        head = self.head.resize((max(1, int(self.head.size[0] * h)), max(1, int(self.head.size[1] * h))), Image.NEAREST)
        img.alpha_composite(head, ((size - head.size[0]) // 2, (size - head.size[1]) // 2))   # 더듬이까지 포함한 상자를 정중앙에
        g = ImageDraw.Draw(img)
        m, L, w = CORNER_MARGIN * k, CORNER_LEN * k, max(1, CORNER_W * k)
        x0, y0, x1, y1 = m, m, size - m, size - m
        for x, y, dx, dy in ((x0, y0, 1, 1), (x1, y0, -1, 1), (x0, y1, 1, -1), (x1, y1, -1, -1)):
            g.rectangle((min(x, x + dx * L), min(y, y + dy * w), max(x, x + dx * L), max(y, y + dy * w)), fill=RED)
            g.rectangle((min(x, x + dx * w), min(y, y + dy * L), max(x, x + dx * w), max(y, y + dy * L)), fill=RED)
        return img

    def icon(self) -> Image.Image:
        return self._mark(S, tile=True)

    @staticmethod
    def wordmark(icon: Image.Image, text_color) -> Image.Image:
        """아이콘 + 픽셀 글자 가로형 (투명 바탕) — 글자 7칸 높이를 아이콘 높이의 절반쯤으로"""
        h = 512
        cell = h // 14
        text = PixelText().render(cell, text_color)
        gap = int(h * 0.14)
        out = Image.new("RGBA", (h + gap + text.size[0] + cell, h), (0, 0, 0, 0))
        out.alpha_composite(icon.resize((h, h), Image.LANCZOS), (0, 0))
        out.alpha_composite(text, (h + gap, (h - 7 * cell) // 2))
        return out

    def pixel_map(self, cells: int = 48, colors: int = 12) -> tuple[dict[str, str], list[str]]:
        """화면용 픽셀 지도 — 바탕 없는 마크를 cells×cells 칸으로 줄이고 색을 colors 개로 묶는다.
        글자는 대문자(A~) — ui.html 의 다른 아이콘 색 이름(소문자)과 겹치지 않게"""
        mark = self._mark(cells * 16, tile=False).resize((cells, cells), Image.BOX)
        a = np.asarray(mark)
        solid = a[..., 3] > 110
        rgb = Image.fromarray(a[..., :3]).quantize(colors=colors, method=Image.Quantize.MEDIANCUT)
        idx = np.asarray(rgb)
        pal = rgb.getpalette()[: colors * 3]
        used = sorted({int(i) for i in idx[solid]})
        letter = {i: chr(ord("A") + n) for n, i in enumerate(used)}
        palette = {letter[i]: "#%02x%02x%02x" % tuple(pal[i * 3:i * 3 + 3]) for i in used}
        rows = ["".join(letter[int(idx[y, x])] if solid[y, x] else "." for x in range(cells)) for y in range(cells)]
        ys, xs = np.nonzero(solid)                      # 바깥 빈 여백은 잘라 낸다 — 화면에서 그림이 지정 크기를 꽉 채우게
        return palette, [r[xs.min():xs.max() + 1] for r in rows[ys.min():ys.max() + 1]]

    def write_ui_map(self, ui: Path = UI) -> None:
        palette, rows = self.pixel_map()
        body = ("  // <alien> make_logo.py 가 채운다 — 손으로 고치지 말 것\n"
                f"  const ALIEN_PAL = {{ {', '.join(f'{k}: {v!r}' for k, v in palette.items())} }};\n"
                "  const ALIEN = [\n" + "".join(f"    '{r}',\n" for r in rows) + "  ];\n"
                "  // </alien>\n")
        text = ui.read_text(encoding="utf-8")
        new, n = re.subn(r"  // <alien>.*?  // </alien>\n", lambda _: body, text, flags=re.S)
        if n != 1:
            raise SystemExit("ui.html 에서 // <alien> ... // </alien> 자리를 찾지 못했습니다")
        ui.write_text(new, encoding="utf-8", newline="\n")

    def save_all(self) -> list[Path]:
        icon = self.icon()
        out = []
        app_icon = APP / "icon"
        app_icon.mkdir(exist_ok=True)
        icon.resize((256, 256), Image.LANCZOS).save(app_icon / "framing.png")
        icon.save(app_icon / "framing.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
        icon.save(app_icon / "framing.icns")      # Mac 창(Framing.app) 아이콘
        out += [app_icon / "framing.png", app_icon / "framing.ico", app_icon / "framing.icns"]
        folder = ROOT / "logo"
        for size in (1024, 512, 256, 128, 64, 32):
            p = folder / f"framing_icon_{size}.png"
            (icon if size == S else icon.resize((size, size), Image.LANCZOS)).save(p)
            out.append(p)
        for name, color in (("dark", (237, 237, 237, 255)), ("light", (29, 29, 31, 255))):
            for kind, img in (("logo", self.wordmark(icon, color)), ("text", PixelText().render(24, color))):
                p = folder / f"framing_{kind}_{name}.png"
                img.save(p)
                out.append(p)
        self.write_ui_map()
        out.append(UI)
        return out


if __name__ == "__main__":
    for p in Logo().save_all():
        print(p)
