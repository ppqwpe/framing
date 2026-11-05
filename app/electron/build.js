// Framing 창을 실행 파일로 굽는다.
//
//   cd app/electron && pnpm install
//   pnpm run build                      → 이 컴퓨터용: app/window/Framing.exe (Mac 에서는 app/window/Framing.app)
//   pnpm run build:mac                  → Mac 용 Electron 원본 zip 두 개(Apple Silicon · Intel)를 app/window-mac 에 받아 둔다
//
// 구운 창은 엔진(app/python · app/.venv-mac · app/main.py)을 자기 위쪽의 app 폴더에서 찾는다.
// 배포 zip(tools/build_zip.py)이 이 폴더를 app/window 자리에 넣는다.
//
// Mac 판을 Windows 에서 굽지 않는 이유: Framing.app 안에는 심볼릭 링크가 있어야 하는데 Windows 는
// 관리자 권한 없이는 만들 수 없다. 그래서 Electron 원본 zip 만 받아 두고, build_zip.py --mac 이
// 그 zip 의 항목(링크 포함)을 그대로 옮겨 담으며 Framing.app 으로 이름 · 아이콘 · 앱 코드를 바꾼다.
const { packager } = require('@electron/packager');
const { downloadArtifact } = require('@electron/get');
const fs = require('fs');
const path = require('path');

const APP = path.resolve(__dirname, '..');
const KEEP_LOCALES = ['en-US.pak', 'ko.pak', 'en.lproj', 'ko.lproj'];   // 화면은 한국어 · 영어뿐

/** 이 컴퓨터용 창을 app/window 에 굽는다 (FRAMING_WINDOW_OUT 으로 다른 곳에). */
class WindowBuild {
  constructor(platform, arch) {
    this.platform = platform;
    this.arch = arch;
  }

  get out() {
    return process.env.FRAMING_WINDOW_OUT ? path.resolve(process.env.FRAMING_WINDOW_OUT) : path.join(APP, 'window');
  }

  get icon() {
    return path.join(APP, 'icon', this.platform === 'darwin' ? 'framing.icns' : 'framing.ico');
  }

  async run() {
    const out = this.out;
    const stage = `${out}.stage`;
    fs.rmSync(out, { recursive: true, force: true });
    fs.rmSync(stage, { recursive: true, force: true });
    const [made] = await packager({
      dir: __dirname,
      platform: this.platform,
      arch: this.arch,
      name: 'Framing',
      executableName: 'Framing',
      appBundleId: 'com.alienmeow.framing',
      appCategoryType: 'public.app-category.video',
      appCopyright: 'ALIENMEOW',
      icon: this.icon,
      out: stage,
      overwrite: true,
      asar: true,
      prune: true,
      ignore: [/^\/build\.js$/, /^\/pnpm-.*\.yaml$/],
      win32metadata: { CompanyName: 'ALIENMEOW', FileDescription: 'Framing', ProductName: 'Framing' },
    });
    if (!made) throw new Error(`${this.platform}-${this.arch} 판을 이 컴퓨터에서 구울 수 없습니다`);
    fs.renameSync(made, out);                 // Framing-<os>-<arch> → app/window...
    fs.rmSync(stage, { recursive: true, force: true });
    this._trimLocales(out);
    return out;
  }

  /** Chromium 언어 팩은 한국어 · 영어만 남긴다 (40MB 쯤 덜어짐). */
  _trimLocales(out) {
    const folders = this.platform === 'darwin'
      ? [path.join(out, 'Framing.app', 'Contents', 'Frameworks', 'Electron Framework.framework', 'Versions', 'A', 'Resources'),
         path.join(out, 'Framing.app', 'Contents', 'Resources')]
      : [path.join(out, 'locales')];
    for (const folder of folders.filter((f) => fs.existsSync(f))) {
      for (const name of fs.readdirSync(folder)) {
        const isLocale = name.endsWith('.pak') ? this.platform !== 'darwin' : name.endsWith('.lproj');
        if (isLocale && !KEEP_LOCALES.includes(name)) fs.rmSync(path.join(folder, name), { recursive: true, force: true });
      }
    }
  }
}

/** Mac 용 Electron 원본 zip 을 받아 app/window-mac 에 둔다. build_zip.py --mac 이 쓴다. */
class MacRuntime {
  constructor(arch) {
    this.arch = arch;
  }

  static get folder() { return path.join(APP, 'window-mac'); }

  async run() {
    const version = require('electron/package.json').version;
    const zip = await downloadArtifact({ version, platform: 'darwin', arch: this.arch, artifactName: 'electron' });
    fs.mkdirSync(MacRuntime.folder, { recursive: true });
    const target = path.join(MacRuntime.folder, `electron-darwin-${this.arch}.zip`);
    fs.copyFileSync(zip, target);
    return target;
  }
}

/** 명령줄: 없으면 이 컴퓨터용 창을 굽고, --mac 이면 Mac 판 재료를 받는다 (어느 컴퓨터에서든 같은 방식). */
function jobs(argv) {
  if (argv.includes('--mac')) return [new MacRuntime('arm64'), new MacRuntime('x64')];
  return [new WindowBuild(process.platform, process.arch)];
}

(async () => {
  for (const job of jobs(process.argv.slice(2))) {
    console.log(`built ${await job.run()}`);
  }
})().catch((err) => { console.error(err); process.exit(1); });
