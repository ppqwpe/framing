import os, sys, tempfile, unittest, time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
os.environ['SHOTKIT_ACCESS_PASSWORD']='test-password-only'
os.environ['SHOTKIT_DATA']=tempfile.mkdtemp(prefix='shotkit-web-test-')
from fastapi.testclient import TestClient
from server import app
from domain import options, filename

class ServiceTests(unittest.TestCase):
    def test_access_and_isolation(self):
        with TestClient(app) as a, TestClient(app) as b:
            self.assertEqual(a.get('/api/session').status_code,401)
            self.assertEqual(a.post('/api/login',json={'password':'wrong'}).status_code,401)
            for c in [a,b]:self.assertEqual(c.post('/api/login',json={'password':'test-password-only'}).status_code,200)
            token=a.get('/api/session').json()['csrf'];h={'x-csrf-token':token}
            self.assertEqual(a.post('/api/jobs',headers=h,content=b'not-json').status_code,400)
            self.assertEqual(a.post('/api/files?name=a.txt',content=b'memo').status_code,403)
            self.assertEqual(a.post('/api/files?name=a.txt',headers={**h,'Origin':'https://wrong.example'},content=b'memo').status_code,403)
            f=a.post('/api/files?name=../../note.txt',headers=h,content=b'memo').json()
            self.assertEqual(f['name'],'note.txt')
            self.assertEqual(b.get('/api/files/'+f['id']).status_code,404)
            self.assertEqual(a.post('/api/jobs',headers=h,json={'files':[f['id']],'tool':'rename','options':{'out_dir':'/tmp'}}).status_code,400)
            j=a.post('/api/jobs',headers=h,json={'files':[f['id']],'tool':'rename','options':{}}).json()
            self.assertEqual(b.get('/api/jobs/'+j['id']).status_code,404)
            for _ in range(40):
                state=a.get('/api/jobs/'+j['id']).json()
                if state['status'] not in ['running','queued']:break
                time.sleep(.5)
            self.assertEqual(state['status'],'done',state)
            self.assertTrue(any('SHOT_001.txt' in f['name'] for f in state['results']))
            url='/api/jobs/'+j['id']+'/results/'+state['results'][-1]['id']
            self.assertEqual(a.get(url).status_code,200)
            self.assertEqual(b.get(url).status_code,404)
            self.assertEqual(a.delete('/api/files/'+f['id'],headers=h).status_code,200)
            self.assertEqual(a.get('/api/files/'+f['id']).status_code,404)
            fake=a.post('/api/files?name=fake.mp4',headers=h,content=b'#EXTM3U\nhttp://example.com/private').json()
            j=a.post('/api/jobs',headers=h,json={'files':[fake['id']],'tool':'extract','options':{}}).json()
            for _ in range(40):
                state=a.get('/api/jobs/'+j['id']).json()
                if state['status'] not in ['running','queued']:break
                time.sleep(.2)
            self.assertEqual(state['status'],'failed')
    def test_validation(self):
        for raw in [{'target_frames':float('nan')},{'target_frames':0},{'retime_mode':'rife'}]:
            with self.assertRaises(ValueError):options('retime',raw)
        with self.assertRaises(ValueError):options('rename',{'prefix':'../'})
        with self.assertRaises(ValueError):filename('evil.html')
if __name__=='__main__':unittest.main()
