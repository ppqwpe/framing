"""Electron 창(app/electron)이 엔진을 띄워 화면을 열고, 닫으면 엔진도 같이 끝나는지"""
from pathlib import Path
import json, os, subprocess, sys, tempfile, unittest

APP = Path(__file__).resolve().parents[1]
ELECTRON = APP / 'electron' / 'node_modules' / 'electron' / 'dist' / ('electron.exe' if os.name == 'nt' else 'electron')
PYTHON = APP / 'python' / 'python.exe'


class ElectronEngineTests(unittest.TestCase):
    def test_engine_prints_address_and_stops_when_stdin_closes(self):
        env = dict(os.environ, FRAMING_MODE='electron')
        child = subprocess.Popen([sys.executable, '-u', '-X', 'utf8', str(APP / 'main.py')], cwd=APP, env=env,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            line = child.stdout.readline().decode('utf-8')
            self.assertRegex(line, r'^FRAMING_URL=http://127\.0\.0\.1:\d+/')
            child.stdin.close()
            self.assertEqual(child.wait(15), 0)
        finally:
            if child.poll() is None:
                child.kill()

    @unittest.skipUnless(ELECTRON.exists() and PYTHON.exists(), 'Electron or the Windows runtime is not installed')
    def test_window_opens_with_native_bridge(self):
        with tempfile.TemporaryDirectory() as name:
            shot = Path(name) / 'window.png'
            env = dict(os.environ, FRAMING_SMOKE=str(shot))
            done = subprocess.run([str(ELECTRON), str(APP / 'electron')], env=env, capture_output=True, timeout=90)
            lines = [l for l in done.stdout.decode('utf-8', 'replace').splitlines() if l.startswith('FRAMING_SMOKE=')]
            self.assertTrue(lines, done.stdout.decode('utf-8', 'replace') + done.stderr.decode('utf-8', 'replace'))
            state = json.loads(lines[0].split('=', 1)[1])
            self.assertEqual(state['title'], 'Framing')
            self.assertTrue(state['native'] and state['api'])
            self.assertGreater(shot.stat().st_size, 10000)


if __name__ == '__main__':
    unittest.main(verbosity=2)
