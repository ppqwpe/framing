import ast,json,re,threading,unittest,urllib.request,urllib.error
from pathlib import Path
from typing import Optional,Callable
s=(Path(__file__).resolve().parents[1]/'framing'/'core.py').read_text(encoding='utf-8')
t=ast.parse(s); ns=globals()
classes={'ShotkitError','LocalRequestPolicy','ApiDispatcher','HttpByteRange','LocalServer','EventBus','UiDocument','RetimeEngineFactory'}
for n in t.body:
 if isinstance(n,ast.ClassDef) and n.name in classes:
  exec(compile(ast.Module(body=[n],type_ignores=[]),'<isolated>', 'exec'),ns)
html=next(ast.literal_eval(n.value) for n in t.body if isinstance(n,ast.Assign) and any(isinstance(a,ast.Name) and a.id=='UI_HTML' for a in n.targets))
class FakeApi:
 def bind_emitter(self,*args): pass
 def bind_server(self,*args): pass
 def describe(self): return {'test':True}
class SecurityTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.server=LocalServer(FakeApi(),UiDocument(html));cls.url=cls.server.start()
  with urllib.request.urlopen(cls.url) as r: cls.document=r.read().decode()
  cls.token=re.search(r'window.__shotkitToken=("[^"]+")',cls.document).group(1)
  cls.token=json.loads(cls.token)
 @classmethod
 def tearDownClass(cls):
  cls.server._httpd.shutdown(); cls.server._httpd.server_close()
 def call(self,payload,headers=None):
  request=urllib.request.Request(self.url+'call',json.dumps(payload).encode(),headers=headers or {},method='POST')
  try:
   with urllib.request.urlopen(request) as r:return r.status,json.load(r)
  except urllib.error.HTTPError as e:
   with e:return e.code,json.load(e)
 def headers(self):return {'Content-Type':'application/json','X-Shotkit-Token':self.token}
 def test_authorized(self):self.assertEqual(self.call({'method':'describe','args':[]},self.headers()),(200,{'ok':True,'value':{'test':True}}))
 def test_missing_token(self):self.assertEqual(self.call({'method':'describe'}, {'Content-Type':'application/json'})[0],403)
 def test_cross_origin(self):
  h=self.headers();h['Origin']='https://example.com';self.assertEqual(self.call({'method':'describe'},h)[0],403)
 def test_wrong_host(self):
  h=self.headers();h['Host']='evil.example';self.assertEqual(self.call({'method':'describe'},h)[0],403)
 def test_internal_method(self):self.assertEqual(self.call({'method':'bind_server'},self.headers())[0],400)
 def test_invalid_args(self):self.assertEqual(self.call({'method':'describe','args':{}},self.headers())[0],400)
 def test_wrong_arity(self):self.assertEqual(self.call({'method':'describe','args':[1]},self.headers())[0],400)
 def test_non_object(self):self.assertEqual(self.call([],self.headers())[0],400)
 def test_wrong_content_type(self):
  h=self.headers();h['Content-Type']='text/plain';self.assertEqual(self.call({'method':'describe'},h)[0],415)
 def test_events(self):
  for token,expected in [('',403),(self.token,200)]:
   req=urllib.request.Request(self.url+'events?after=0',headers={'X-Shotkit-Token':token})
   try:
    with urllib.request.urlopen(req) as r:code=r.status
   except urllib.error.HTTPError as e:code=e.code;e.close()
   self.assertEqual(code,expected)
 def test_cursor(self):
  req=urllib.request.Request(self.url+'events?after=bad',headers={'X-Shotkit-Token':self.token})
  with self.assertRaises(urllib.error.HTTPError) as ctx:urllib.request.urlopen(req)
  self.assertEqual(ctx.exception.code,400);ctx.exception.close()
 def test_document(self):
  self.assertIn('X-Shotkit-Token',self.document);self.assertEqual(self.document.count('window.__shotkitToken='),1)
 def test_api_parity(self):
  methods=ast.literal_eval(re.search(r'const API_METHODS = (\[.*?\]);',s,re.S).group(1))
  self.assertEqual(set(methods),set(ApiDispatcher.METHODS))
 def test_body_limits(self):
  p=LocalRequestPolicy()
  for value in ['-1','0','bad',str(p.MAX_BODY+1)]:
   with self.assertRaises(ShotkitError):p.body_length({'Content-Length':value})
 def test_ranges(self):
  for value,expected in [('',(0,99,False)),('bytes=2-5',(2,5,True)),('bytes=90-',(90,99,True)),('bytes=-10',(90,99,True)),('bytes=-200',(0,99,True))]:self.assertEqual(HttpByteRange.parse(value,100),expected)
  for value in ['bytes=100-','bytes=5-2','bytes=-0','bytes=-','bytes=1-2,4-5']:
   with self.assertRaises(ValueError):HttpByteRange.parse(value,100)
class EngineTests(unittest.TestCase):
 def test_factory(self):
  from types import SimpleNamespace
  class Engine:
   NEEDS_FFMPEG=False
   def __init__(self,*args,**kwargs):pass
  class Dup(Engine):KEY='dup'
  class Hold(Engine):KEY='hold'
  class Blend(Engine):KEY='blend'
  class Motion(Engine):
   KEY='motion';NEEDS_FFMPEG=True
   @staticmethod
   def supports(sample):return sample=='8bit'
  class Quiet:
   info=warn=staticmethod(lambda *args:None)
  globals().update(DuplicateEngine=Dup,TailHoldEngine=Hold,BlendEngine=Blend,MotionEngine=Motion,Console=Quiet)
  slow=SimpleNamespace(slowing=True,unchanged=False)
  fast=SimpleNamespace(slowing=False,unchanged=False)
  for ff,mode,plan,matte,sample,expected in [(True,'auto',slow,False,'8bit',Motion),(True,'auto',slow,True,'8bit',Dup),(True,'auto',fast,False,'8bit',Dup),(False,'motion',slow,False,'8bit',Blend),(True,'motion',slow,False,'16bit',Blend),(True,'hold',fast,False,'8bit',Dup)]:
   factory=RetimeEngineFactory((Dup,Hold,Blend,Motion),SimpleNamespace(find=lambda name:ff))
   self.assertIsInstance(factory.create(mode,plan,matte,None,None,None,sample),expected)
 def test_unknown_engine(self):
  from types import SimpleNamespace
  with self.assertRaises(ShotkitError):RetimeEngineFactory((),None).create('bogus',SimpleNamespace(),False,None,None,None)
 def test_shortcut_quoting(self):
  node=next(n for n in t.body if isinstance(n,ast.ClassDef) and n.name=='DesktopShortcut')
  namespace={'Path':Path,'Optional':Optional,'SELF':"C:/O'Neil/SHOTKIT.bat"}
  exec(compile(ast.Module(body=[node],type_ignores=[]),'<shortcut>','exec'),namespace)
  import inspect
  method=next(n for n in node.body if isinstance(n,ast.FunctionDef) and n.name=='_windows')
  expression=next(n.value for n in method.body if isinstance(n,ast.Assign) and any(isinstance(a,ast.Name) and a.id=='script' for a in n.targets))
  env={'link':Path("C:/O'Neil/SHOTKIT.lnk"),'launcher':Path("C:/O'Neil/SHOTKIT.bat"),'icon':Path("C:/O'Neil/icon.ico")}
  result=eval(compile(ast.Expression(expression),'<quoting>','eval'),env)
  self.assertIn("O''Neil",result);self.assertNotIn("O'Neil",result)
if __name__=='__main__':unittest.main(verbosity=2)
