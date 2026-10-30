"""Portable engine regression only. This does not prove Cocoa or Mac binaries work."""
import json, shutil, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'mac')); sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from mac_runtime import MacPaths
from mac_diagnostics import run

class HostTestPaths(MacPaths):
    def ffmpeg(self,name):return shutil.which(name)

@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'FFmpeg is needed for media regression')
class MediaRegression(unittest.TestCase):
    def test_core_operations(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);report=root/'report.json'
            status=run(HostTestPaths(root,root),report,check_cocoa=False)
            result=json.loads(report.read_text(encoding='utf-8'))
            self.assertEqual(status,0,result)
            self.assertFalse(result['checks']['cocoa_import'])

if __name__=='__main__':unittest.main()
