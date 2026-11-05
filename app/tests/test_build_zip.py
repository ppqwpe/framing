"""배포 zip: Mac 판이 Electron 원본에서 Framing.app 을 제대로 옮겨 담는지 (심볼릭 링크 · 실행 권한 · 이름 · 앱 코드)"""
from pathlib import Path
import io, plistlib, stat, sys, tempfile, unittest, zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import build_zip as bz


def fake_electron(path: Path) -> None:
    """Electron 원본 zip 흉내: 실행 파일 · 심볼릭 링크 · 언어 폴더 · 기본 앱."""
    plist = plistlib.dumps({'CFBundleName': 'Electron', 'CFBundleExecutable': 'Electron',
                            'CFBundleIconFile': 'electron.icns', 'ElectronAsarIntegrity': {},
                            'NSCameraUsageDescription': 'x'})
    entries = [('Electron.app/Contents/Info.plist', plist, stat.S_IFREG | 0o644),
               ('Electron.app/Contents/MacOS/Electron', b'bin', stat.S_IFREG | 0o755),
               ('Electron.app/Contents/Frameworks/E.framework/Versions/Current', b'A', stat.S_IFLNK | 0o755),
               ('Electron.app/Contents/Frameworks/E.framework/Resources/fr.lproj/x.pak', b'fr', stat.S_IFREG | 0o644),
               ('Electron.app/Contents/Frameworks/E.framework/Resources/ko.lproj/x.pak', b'ko', stat.S_IFREG | 0o644),
               ('Electron.app/Contents/Resources/default_app.asar', b'asar', stat.S_IFREG | 0o644),
               ('LICENSE', b'mit', stat.S_IFREG | 0o644)]
    with zipfile.ZipFile(path, 'w') as z:
        for name, data, mode in entries:
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = mode << 16
            z.writestr(info, data)


class MacZipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mac = bz.MacZip(Path(self.tmp.name) / 'mac.zip', 'arm64')
        self.mac.runtime = Path(self.tmp.name) / 'electron.zip'
        fake_electron(self.mac.runtime)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as z:
            self.mac.extra(z)
        self.out = zipfile.ZipFile(io.BytesIO(buffer.getvalue()))
        self.names = self.out.namelist()

    def tearDown(self):
        self.tmp.cleanup()

    def mode(self, name):
        return self.out.getinfo(name).external_attr >> 16

    def test_bundle_is_renamed_and_keeps_links_and_exec_bits(self):
        app = bz.MacZip.BUNDLE
        self.assertTrue(stat.S_ISLNK(self.mode(f'{app}/Contents/Frameworks/E.framework/Versions/Current')))
        self.assertEqual(self.out.read(f'{app}/Contents/Frameworks/E.framework/Versions/Current'), b'A')
        self.assertTrue(self.mode(f'{app}/Contents/MacOS/Electron') & 0o111)
        self.assertFalse(any(n.startswith('Electron.app') or n == 'LICENSE' for n in self.names))

    def test_plist_names_framing_and_drops_unused_keys(self):
        info = plistlib.loads(self.out.read(f'{bz.MacZip.BUNDLE}/Contents/Info.plist'))
        self.assertEqual((info['CFBundleName'], info['CFBundleIconFile'], info['CFBundleExecutable']),
                         ('Framing', 'framing.icns', 'Electron'))
        self.assertNotIn('ElectronAsarIntegrity', info)
        self.assertNotIn('NSCameraUsageDescription', info)

    def test_app_code_icon_and_only_korean_english(self):
        res = f'{bz.MacZip.BUNDLE}/Contents/Resources'
        for name in ('app/main.js', 'app/preload.js', 'app/package.json', 'framing.icns'):
            self.assertIn(f'{res}/{name}', self.names)
        self.assertNotIn(f'{res}/default_app.asar', self.names)
        self.assertFalse(any('fr.lproj' in n for n in self.names))
        self.assertTrue(any('ko.lproj' in n for n in self.names))

    def test_mac_zip_leaves_out_windows_runtime(self):
        skipped = self.mac.skipped()
        for path in (bz.APP / 'python', bz.APP / 'ffmpeg', bz.APP / 'window', bz.ROOT / 'Framing.bat'):
            self.assertIn(path, skipped)


if __name__ == '__main__':
    unittest.main(verbosity=2)
