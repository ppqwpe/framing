// Framing 을 자기 창으로 띄운다 (Windows · Mac 같은 코드).
//
// 엔진은 그대로 파이썬(app/main.py)이다. 여기서는 그 프로세스를 FRAMING_MODE=electron 으로
// 띄우고, 파이썬이 알려 주는 로컬 주소(FRAMING_URL=...)를 창에 연다. 창을 닫으면 stdin 을
// 닫아 파이썬도 끝나게 하고, 화면의 '종료' 로 파이썬이 먼저 끝나면 창도 닫는다.
const { app, BrowserWindow, dialog, ipcMain, shell } = require('electron');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

const IS_MAC = process.platform === 'darwin';
const IS_WIN = process.platform === 'win32';

/**
 * Framing 폴더 안의 자리들. 소스로 돌 때는 이 파일 한 칸 위가 app 이고,
 * 구운 판(app/window/Framing.exe · Framing.app)은 실행 파일 위쪽에서 app 을 찾는다.
 * app.isPackaged 는 쓰지 않는다 — Mac 판은 도우미 앱 이름을 지키려고 실행 파일 이름이 Electron 그대로라 늘 false 다.
 */
class Layout {
  static get app() {
    const source = path.resolve(__dirname, '..');
    if (fs.existsSync(path.join(source, 'main.py'))) return source;
    // Windows: app/window/Framing.exe · Mac: app/window/Framing.app/Contents/MacOS/<실행 파일>
    return IS_MAC ? path.resolve(process.execPath, '..', '..', '..', '..', '..')
                  : path.resolve(path.dirname(process.execPath), '..');
  }

  static get icon() {
    return path.join(this.app, 'icon', IS_WIN ? 'framing.ico' : 'framing.png');
  }

  static get background() { return '#0b0d0f'; }       // ui.html 의 --bg
}

/** 실행 결과를 모아 두는 작은 자식 프로세스 도우미. */
class Command {
  static run(file, args, options = {}) {
    return new Promise((resolve, reject) => {
      const child = spawn(file, args, { windowsHide: true, ...options });
      let out = '';
      child.stdout && child.stdout.on('data', (c) => { out += c; });
      child.stderr && child.stderr.on('data', (c) => { out += c; });
      child.on('error', reject);
      child.on('exit', (code) => (code === 0 ? resolve(out)
        : reject(new Error(`${path.basename(file)} ${args.join(' ')} (코드 ${code})\n${out.slice(-2000)}`))));
    });
  }
}

/**
 * 어느 파이썬으로 엔진을 돌릴지.
 * Windows 는 내장 파이썬(app/python). Mac 은 app/.venv-mac — 없으면 처음 한 번 만든다.
 */
class PythonRuntime {
  constructor(root) {
    this._root = root;
  }

  get venv() { return path.join(this._root, '.venv-mac'); }

  /** Finder 에서 연 앱은 셸 PATH 를 못 받는다. Homebrew 자리를 앞에 붙여 ffmpeg · python3 을 찾게 한다. */
  static searchPath() {
    const extra = IS_MAC ? ['/opt/homebrew/bin', '/usr/local/bin'] : [];
    return [...extra, process.env.PATH || ''].filter(Boolean).join(path.delimiter);
  }

  existing() {
    const candidates = IS_WIN
      ? [path.join(this._root, 'python', 'python.exe')]
      : [path.join(this.venv, 'bin', 'python3'), path.join(this.venv, 'bin', 'python')];
    return candidates.find((p) => fs.existsSync(p)) || process.env.FRAMING_PYTHON || null;
  }

  async ensure(onSetup) {
    const found = this.existing();
    if (found) return found;
    if (!IS_MAC) {
      throw new Error('app\\python 이 없습니다. 릴리스 zip 에서 app\\python · app\\lib · app\\ffmpeg · app\\rife 를 복사해 주세요.');
    }
    onSetup();
    await new MacSetup(this._root, this.venv).run();
    return this.existing();
  }
}

/** Mac 첫 실행: Python 3.10+ 로 app/.venv-mac 을 만들고 requirements.txt 를 설치한다 (Framing.command 와 같은 일). */
class MacSetup {
  constructor(root, venv) {
    this._root = root;
    this._venv = venv;
  }

  static async findPython() {
    const env = { ...process.env, PATH: PythonRuntime.searchPath() };
    const names = ['/opt/homebrew/bin/python3', '/usr/local/bin/python3', 'python3.13', 'python3.12', 'python3.11', 'python3.10', 'python3'];
    for (const name of names) {
      try {
        await Command.run(name, ['-c', 'import sys; sys.exit(sys.version_info < (3, 10))'], { env });
        return name;
      } catch (_) { /* 다음 후보 */ }
    }
    throw new Error('Python 3.10 이상이 필요합니다. 터미널에서 brew install python@3.12 를 실행한 뒤 다시 열어 주세요.');
  }

  async run() {
    const env = { ...process.env, PATH: PythonRuntime.searchPath() };
    const python = await MacSetup.findPython();
    await Command.run(python, ['-m', 'venv', this._venv], { env });
    const pip = path.join(this._venv, 'bin', 'python');
    await Command.run(pip, ['-m', 'pip', 'install', '--quiet', '--upgrade', 'pip'], { env });
    await Command.run(pip, ['-m', 'pip', 'install', '--quiet', '-r', path.join(this._root, 'requirements.txt')], { env });
  }
}

/** 파이썬 엔진 프로세스 하나. 주소를 받을 때까지 기다리고, 끝나면 알려 준다. */
class Engine {
  constructor(root, onExit) {
    this._root = root;
    this._onExit = onExit;
    this._log = [];
    this._child = null;
  }

  start(python) {
    const env = { ...process.env, PATH: PythonRuntime.searchPath(), FRAMING_MODE: 'electron',
                  FRAMING_DETACHED: '1', FRAMING_PORTABLE: '1', PYTHONUNBUFFERED: '1' };
    this._child = spawn(python, ['-u', '-X', 'utf8', path.join(this._root, 'main.py')],
      { cwd: this._root, env, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
    return new Promise((resolve, reject) => {
      let pending = '';
      let settled = false;
      const take = (chunk) => {
        const text = chunk.toString('utf8');
        this._remember(text);
        if (settled) return;
        pending += text;
        const match = pending.match(/^FRAMING_URL=(\S+)\s*$/m);
        if (match) { settled = true; resolve(match[1]); }
      };
      this._child.stdout.on('data', take);
      this._child.stderr.on('data', (chunk) => this._remember(chunk.toString('utf8')));
      this._child.on('error', (err) => { if (!settled) { settled = true; reject(err); } });
      this._child.on('exit', (code) => {
        if (!settled) { settled = true; reject(new Error(`엔진이 시작하지 못했습니다 (코드 ${code})\n\n${this.log()}`)); return; }
        this._onExit(code);
      });
    });
  }

  /** stdin 을 닫으면 엔진이 하던 일을 멈추고 스스로 끝난다. 그래도 남아 있으면 끝낸다. */
  stop() {
    const child = this._child;
    if (!child || child.exitCode !== null || child.signalCode !== null) return Promise.resolve();
    return new Promise((resolve) => {
      const timer = setTimeout(() => { child.kill(); resolve(); }, 4000);
      child.once('exit', () => { clearTimeout(timer); resolve(); });
      child.stdin.end();
    });
  }

  log() { return this._log.join('').slice(-4000); }

  _remember(text) {
    this._log.push(text);
    if (this._log.length > 400) this._log.splice(0, 200);
  }
}

/** 창 하나. 같은 주소 안에서만 돌아다니고, 바깥 링크는 기본 브라우저로 보낸다. */
class MainWindow {
  constructor() {
    this._origin = '';
    this.win = new BrowserWindow({
      width: 1440, height: 900, minWidth: 960, minHeight: 600,
      title: 'Framing', icon: Layout.icon, backgroundColor: Layout.background, show: false,
      autoHideMenuBar: true,
      webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, sandbox: true },
    });
    if (!IS_MAC) this.win.removeMenu();                  // Mac 은 기본 메뉴가 있어야 복사 · 붙여넣기 단축키가 된다
    this.win.once('ready-to-show', () => this.win.show());
    this.win.webContents.setWindowOpenHandler(({ url }) => this._popup(url));
    this.win.webContents.on('will-navigate', (event, target) => {
      if (!this._inside(target)) { event.preventDefault(); this._external(target); }
    });
  }

  /** 엔진을 준비하는 동안 보여 줄 한 줄. */
  waiting(text) {
    const page = `<body style="margin:0;background:${Layout.background};color:#9aa3ad;font:14px system-ui;`
      + `display:flex;align-items:center;justify-content:center;height:100vh">${text}</body>`;
    this.win.loadURL('data:text/html;charset=utf-8,' + encodeURIComponent(page));
  }

  open(url) {
    this._origin = new URL(url).origin;
    return this.win.loadURL(url);
  }

  focus() {
    if (this.win.isMinimized()) this.win.restore();
    this.win.focus();
  }

  _inside(target) {
    try { return new URL(target).origin === this._origin; } catch (_) { return false; }
  }

  _external(target) {
    if (/^https?:/i.test(target)) shell.openExternal(target);
  }

  /** 대조 화면처럼 엔진이 내주는 페이지는 새 창으로, 나머지는 브라우저로. */
  _popup(target) {
    if (this._inside(target)) {
      return { action: 'allow', overrideBrowserWindowOptions: {
        width: 1280, height: 800, icon: Layout.icon, backgroundColor: Layout.background, autoHideMenuBar: true } };
    }
    this._external(target);
    return { action: 'deny' };
  }
}

/** 화면의 '찾아보기' 를 OS 대화상자로. preload.js 가 이 둘을 부른다. */
class NativePickers {
  static wire() {
    ipcMain.handle('framing:pick-files', (event) => NativePickers._pick(event, ['openFile', 'multiSelections']));
    ipcMain.handle('framing:pick-folder', (event) => NativePickers._pick(event, ['openDirectory']));
  }

  static async _pick(event, properties) {
    const picked = await dialog.showOpenDialog(BrowserWindow.fromWebContents(event.sender), { properties });
    return picked.canceled ? [] : picked.filePaths;
  }
}

/** 테스트용(tests/test_electron.py): 화면이 뜨면 상태를 한 줄 JSON 으로 찍고, 창을 PNG 로 남기고 끝낸다. */
class SmokeCheck {
  constructor(shotPath) {
    this._shot = shotPath;
  }

  async run(win) {
    await new Promise((resolve) => setTimeout(resolve, 2000));
    const state = await win.webContents.executeJavaScript(`({
      native: !!window.framingNative && typeof window.framingNative.pickFiles === 'function',
      api: typeof api === 'function' && !!api(),
      title: document.title,
      text: document.body.innerText.slice(0, 120) })`);
    fs.writeFileSync(this._shot, (await win.webContents.capturePage()).toPNG());
    process.stdout.write(`FRAMING_SMOKE=${JSON.stringify(state)}\n`);
    app.quit();
  }
}

/** 앱 전체: 엔진을 띄우고, 창을 열고, 끝날 때 엔진을 정리한다. */
class FramingDesktop {
  constructor() {
    this._quitting = false;
    this._root = Layout.app;
    this._engine = new Engine(this._root, () => this.quit());
    this._window = null;
  }

  quit() {
    if (!this._quitting) app.quit();
  }

  async boot() {
    app.on('before-quit', (event) => this._beforeQuit(event));
    app.on('window-all-closed', () => app.quit());   // Mac 에서도 창을 닫으면 끝 (엔진을 남겨 두지 않는다)
    app.on('second-instance', () => this._window && this._window.focus());
    NativePickers.wire();

    this._window = new MainWindow();
    try {
      const python = await new PythonRuntime(this._root).ensure(() => {
        this._window.waiting('처음 한 번 Framing 을 준비하고 있습니다 (1분쯤)…');
        this._window.win.show();
      });
      const url = await this._engine.start(python);
      await this._window.open(url);
    } catch (err) {
      dialog.showErrorBox('Framing', String((err && err.message) || err));
      this._quitting = true;
      app.quit();
      return;
    }
    if (process.env.FRAMING_SMOKE) new SmokeCheck(process.env.FRAMING_SMOKE).run(this._window.win);
  }

  _beforeQuit(event) {
    if (this._quitting) return;
    this._quitting = true;
    event.preventDefault();
    this._engine.stop().finally(() => app.quit());
  }
}

// 테스트 실행은 자기 데이터 폴더를 쓴다 — 쓰고 있는 Framing 창과 '하나만 열기' 잠금을 다투지 않게.
if (process.env.FRAMING_SMOKE) app.setPath('userData', fs.mkdtempSync(path.join(require('os').tmpdir(), 'framing-smoke-')));

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.setAppUserModelId('com.alienmeow.framing');
  app.whenReady().then(() => new FramingDesktop().boot());
}
