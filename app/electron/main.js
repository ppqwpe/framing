// Framing 을 자기 창으로 띄운다.
//
// 엔진은 그대로 파이썬(app/main.py)이다. 여기서는 그 프로세스를 FRAMING_MODE=electron 으로
// 띄우고, 파이썬이 알려 주는 로컬 주소(FRAMING_URL=...)를 창에 연다. 창을 닫으면 stdin 을
// 닫아 파이썬도 끝나게 하고, 화면의 '종료' 로 파이썬이 먼저 끝나면 창도 닫는다.
const { app, BrowserWindow, dialog, ipcMain, shell } = require('electron');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');

// Framing/app. 구운 판(app/window/Framing.exe · Framing.app)은 실행 파일 폴더의 한 칸 위.
const APP = !app.isPackaged ? path.resolve(__dirname, '..')
  : process.platform === 'darwin' ? path.resolve(process.execPath, '..', '..', '..', '..', '..')
  : path.resolve(path.dirname(process.execPath), '..');
const ICON = path.join(APP, 'icon', process.platform === 'win32' ? 'framing.ico' : 'framing.png');
const BACKGROUND = '#0b0d0f';                          // ui.html 의 --bg

/** 어느 파이썬으로 엔진을 돌릴지. Windows 는 내장 파이썬, Mac 은 Framing.command 가 만든 venv. */
class PythonRuntime {
  static find() {
    const candidates = process.platform === 'win32'
      ? [path.join(APP, 'python', 'python.exe')]
      : [path.join(APP, '.venv-mac', 'bin', 'python3'), path.join(APP, '.venv-mac', 'bin', 'python')];
    const found = candidates.find((p) => fs.existsSync(p));
    if (found) return found;
    if (process.env.FRAMING_PYTHON) return process.env.FRAMING_PYTHON;
    throw new Error(process.platform === 'win32'
      ? 'app\\python 이 없습니다. 릴리스 zip 에서 app\\python · app\\lib · app\\ffmpeg · app\\rife 를 복사해 주세요.'
      : 'app/.venv-mac 이 없습니다. Framing.command 를 한 번 실행해 준비해 주세요.');
  }
}

/** 파이썬 엔진 프로세스 하나. 주소를 받을 때까지 기다리고, 끝나면 알려 준다. */
class Engine {
  constructor(onExit) {
    this._onExit = onExit;
    this._log = [];
    this._child = null;
  }

  start() {
    const python = PythonRuntime.find();
    const env = { ...process.env, FRAMING_MODE: 'electron', FRAMING_DETACHED: '1', PYTHONUNBUFFERED: '1' };
    this._child = spawn(python, ['-u', '-X', 'utf8', path.join(APP, 'main.py')],
      { cwd: APP, env, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
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
    if (!child || child.exitCode !== null) return Promise.resolve();
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
  constructor(url) {
    this._origin = new URL(url).origin;
    this.win = new BrowserWindow({
      width: 1440, height: 900, minWidth: 960, minHeight: 600,
      title: 'Framing', icon: ICON, backgroundColor: BACKGROUND, show: false,
      autoHideMenuBar: true,
      webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, sandbox: true },
    });
    this.win.removeMenu();
    this.win.once('ready-to-show', () => this.win.show());
    this.win.webContents.setWindowOpenHandler(({ url: target }) => this._popup(target));
    this.win.webContents.on('will-navigate', (event, target) => {
      if (!this._inside(target)) { event.preventDefault(); shell.openExternal(target); }
    });
    this.win.loadURL(url);
  }

  _inside(target) {
    try { return new URL(target).origin === this._origin; } catch (_) { return false; }
  }

  /** 대조 화면처럼 엔진이 내주는 페이지는 새 창으로, 나머지는 브라우저로. */
  _popup(target) {
    if (this._inside(target)) {
      return { action: 'allow', overrideBrowserWindowOptions: {
        width: 1280, height: 800, icon: ICON, backgroundColor: BACKGROUND, autoHideMenuBar: true } };
    }
    if (/^https?:/i.test(target)) shell.openExternal(target);
    return { action: 'deny' };
  }
}

/** 화면의 '찾아보기' 를 OS 대화상자로. preload.js 가 이 둘을 부른다. */
function wirePickers() {
  const owner = (event) => BrowserWindow.fromWebContents(event.sender);
  ipcMain.handle('framing:pick-files', async (event) => {
    const picked = await dialog.showOpenDialog(owner(event), { properties: ['openFile', 'multiSelections'] });
    return picked.canceled ? [] : picked.filePaths;
  });
  ipcMain.handle('framing:pick-folder', async (event) => {
    const picked = await dialog.showOpenDialog(owner(event), { properties: ['openDirectory'] });
    return picked.canceled ? [] : picked.filePaths;
  });
}

/** 테스트용: 화면이 뜨면 상태를 한 줄 JSON 으로 찍고, 창을 PNG 로 남기고 끝낸다 (tests/test_electron.py). */
function smokeCheck(win, shotPath) {
  win.webContents.once('did-finish-load', () => setTimeout(async () => {
    const state = await win.webContents.executeJavaScript(`({
      native: !!window.framingNative && typeof window.framingNative.pickFiles === 'function',
      api: typeof api === 'function' && !!api(),
      title: document.title,
      text: document.body.innerText.slice(0, 120) })`);
    fs.writeFileSync(shotPath, (await win.webContents.capturePage()).toPNG());
    process.stdout.write(`FRAMING_SMOKE=${JSON.stringify(state)}\n`);
    app.quit();
  }, 2000));
}

async function boot() {
  let quitting = false;
  const engine = new Engine(() => { if (!quitting) app.quit(); });
  app.on('before-quit', (event) => {
    if (quitting) return;
    quitting = true;
    event.preventDefault();
    engine.stop().finally(() => app.quit());
  });
  app.on('window-all-closed', () => app.quit());     // Mac 에서도 창을 닫으면 끝 (엔진을 남겨 두지 않는다)

  wirePickers();
  let url;
  try {
    url = await engine.start();
  } catch (err) {
    dialog.showErrorBox('Framing', String(err && err.message || err));
    quitting = true;
    app.quit();
    return;
  }
  const main = new MainWindow(url);
  if (process.env.FRAMING_SMOKE) smokeCheck(main.win, process.env.FRAMING_SMOKE);
  app.on('second-instance', () => {
    if (main.win.isMinimized()) main.win.restore();
    main.win.focus();
  });
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.setAppUserModelId('com.alienmeow.framing');
  app.whenReady().then(boot);
}
