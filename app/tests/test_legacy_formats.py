"""SHOTKIT 시절에 만든 리타임 메모 · 영상 안 늘림 메모 · 작업 파일을 Framing 이 그대로 읽는지"""
from pathlib import Path
import shutil, subprocess, sys, tempfile, unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from framing import core as m


class LegacyFormatTests(unittest.TestCase):
    def test_shotkit_retime_note_text_still_reverts(self):
        plan = m.RetimePlan(153, 264, 24); report = m.RetimeReport(plan, 'motion')
        text = m.RetimeNote(report, Path('source.mp4')).text()
        self.assertIn('FRAMING_TIMELINE', text)
        with tempfile.TemporaryDirectory() as name:
            note = Path(name) / 'old.txt'
            note.write_text(text.replace('FRAMING_TIMELINE', 'SHOTKIT_TIMELINE'), encoding='utf-8')
            parsed = m.RetimeNoteText(note).note(m.PlateSpec(source='source', frames=264, fps=24), 264)
            self.assertEqual(parsed.kept, m.RetimeTimeline(153, 264).anchors)

    def test_shotkit_project_file_still_opens(self):
        data = {'format': 'shotkit-project', 'version': 1, 'paths': [], 'steps': [{'key': 'audit', 'options': {}}]}
        self.assertEqual(m.ProjectDocument.validate(dict(data))['steps'][0]['key'], 'audit')
        data['format'] = 'something-else'
        with self.assertRaises(m.FramingError):
            m.ProjectDocument.validate(data)

    def test_shotkit_stretch_tag_inside_movie_is_found(self):
        ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
        if not (ffmpeg and ffprobe):
            self.skipTest('FFmpeg required')
        with tempfile.TemporaryDirectory() as name:
            for tag in ('SHOTKIT_STRETCH=', 'FRAMING_STRETCH='):
                movie = Path(name) / f'{tag[:7]}.mp4'
                subprocess.run([ffmpeg, '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=black:s=64x64:d=0.2',
                                '-metadata', f'comment={tag}PAYLOAD', str(movie)], check=True)
                self.assertEqual(m.StretchNote._embedded(movie, ffprobe), 'PAYLOAD')


if __name__ == '__main__':
    unittest.main(verbosity=2)
