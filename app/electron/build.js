// Framing 창을 실행 파일로 굽는다 — Windows 는 app/window/Framing.exe, Mac 은 app/window/Framing.app.
//
//   cd app/electron && pnpm install && pnpm run build
//
// 구운 창은 엔진(app/python · app/main.py)을 자기 폴더의 한 칸 위(app)에서 찾는다.
// 그래서 app/window 를 통째로 배포 zip 에 넣으면 Framing.exe 하나로 열린다.
const { packager } = require('@electron/packager');
const fs = require('fs');
const path = require('path');

const APP = path.resolve(__dirname, '..');
const OUT = path.join(APP, 'window');
const STAGE = path.join(OUT, '.stage');

async function build() {
  fs.rmSync(OUT, { recursive: true, force: true });
  const icon = path.join(APP, 'icon', process.platform === 'darwin' ? 'framing.icns' : 'framing.ico');
  const [made] = await packager({
    dir: __dirname,
    name: 'Framing',
    executableName: 'Framing',
    appBundleId: 'com.alienmeow.framing',
    appCopyright: 'ALIENMEOW',
    icon: fs.existsSync(icon) ? icon : undefined,
    out: STAGE,
    overwrite: true,
    asar: true,
    prune: true,
    ignore: [/^\/build\.js$/, /^\/pnpm-.*\.yaml$/],
    win32metadata: { CompanyName: 'ALIENMEOW', FileDescription: 'Framing', ProductName: 'Framing' },
  });
  // Framing-win32-x64/* → app/window/* (Mac 은 app/window/Framing.app)
  for (const name of fs.readdirSync(made)) fs.renameSync(path.join(made, name), path.join(OUT, name));
  fs.rmSync(STAGE, { recursive: true, force: true });
  // 화면은 한국어 · 영어뿐이니 Chromium 언어 팩도 그 둘만 남긴다 (약 40MB 덜어짐)
  const locales = path.join(OUT, 'locales');
  if (fs.existsSync(locales)) {
    for (const name of fs.readdirSync(locales)) {
      if (!['en-US.pak', 'ko.pak'].includes(name)) fs.rmSync(path.join(locales, name));
    }
  }
  console.log(`built ${OUT}`);
}

build().catch((err) => { console.error(err); process.exit(1); });
