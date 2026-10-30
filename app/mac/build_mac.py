"""Build, inspect and self-test a Mac .app before creating a distributable ZIP."""
import argparse, hashlib, json, os, platform, shutil, subprocess, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent

def command(args,**kwargs):
    return subprocess.run([str(x) for x in args],check=True,**kwargs)

def text(args):return command(args,capture_output=True,text=True).stdout.strip()

def native_tool(name,arch):
    found=shutil.which(name)
    if not found:raise RuntimeError(f'{name}가 없습니다. 빌드용 Mac에서 brew install ffmpeg를 실행하세요.')
    path=Path(found).resolve()
    if arch not in text(['lipo','-archs',path]).split():raise RuntimeError(f'{name}와 Python의 아키텍처가 다릅니다: {arch}')
    return path

def audit_bundle(app):
    """Fail on unbundled Homebrew/user-path libraries (the common clean-Mac failure)."""
    bad=[];seen=set()
    for p in app.rglob('*'):
        if not p.is_file() or p.resolve() in seen:continue
        seen.add(p.resolve())
        with p.open('rb') as f:magic=f.read(4)
        if magic not in (b'\xcf\xfa\xed\xfe',b'\xce\xfa\xed\xfe',b'\xfe\xed\xfa\xcf',b'\xca\xfe\xba\xbe',b'\xbe\xba\xfe\xca'):continue
        for line in text(['otool','-L',p]).splitlines()[1:]:
            dependency=line.strip().split(' (',1)[0]
            if dependency.startswith('/') and not dependency.startswith(('/usr/lib/','/System/Library/')):
                bad.append(f'{p.name}: {dependency}')
        commands=text(['otool','-l',p]).splitlines()
        for i,line in enumerate(commands):
            if line.strip()=='cmd LC_RPATH':
                for detail in commands[i+1:i+4]:
                    detail=detail.strip()
                    if detail.startswith('path '):
                        search_path=detail[5:].split(' (offset',1)[0]
                        if search_path.startswith('/') and not search_path.startswith(('/usr/lib/','/System/Library/')):
                            bad.append(f'{p.name}: RPATH {search_path}')
    if bad:raise RuntimeError('앱 외부 라이브러리 의존성이 남았습니다:\n'+'\n'.join(bad))

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--identity');parser.add_argument('--notary-profile');args=parser.parse_args()
    if sys.platform!='darwin':raise RuntimeError('이 빌드는 macOS에서만 실행합니다. Windows에서 .app을 만들었다고 표시하지 않습니다.')
    if sys.version_info[:2]!=(3,12):raise RuntimeError('빌드에는 Python 3.12를 사용하세요.')
    arch=platform.machine()
    if arch not in ('arm64','x86_64'):raise RuntimeError('지원하지 않는 아키텍처: '+arch)
    if args.notary_profile and not args.identity:raise RuntimeError('공증에는 Developer ID 서명 인증서가 필요합니다.')
    ffmpeg=native_tool('ffmpeg',arch);ffprobe=native_tool('ffprobe',arch)
    from PIL import Image
    Image.open(ROOT/'assets'/'icon.ico').convert('RGBA').resize((1024,1024)).save(ROOT/'assets'/'SHOTKIT.icns')
    metadata=ROOT/'build-metadata';metadata.mkdir(exist_ok=True)
    (metadata/'ffmpeg-license.txt').write_text(text([ffmpeg,'-L']),encoding='utf-8')
    (metadata/'ffmpeg-build.txt').write_text(text([ffmpeg,'-version']),encoding='utf-8')
    (metadata/'python-packages.json').write_text(text([sys.executable,'-m','pip','list','--format=json']),encoding='utf-8')
    env=dict(os.environ,SHOTKIT_BUILD_ARCH=arch,SHOTKIT_BUILD_FFMPEG=str(ffmpeg),SHOTKIT_BUILD_FFPROBE=str(ffprobe))
    if args.identity:env['SHOTKIT_CODESIGN_IDENTITY']=args.identity
    dist=ROOT/'dist'/arch
    command([sys.executable,'-m','PyInstaller','--noconfirm','--clean','--distpath',dist,
             '--workpath',ROOT/'build'/arch,ROOT/'SHOTKIT.spec'],cwd=ROOT,env=env)
    app=dist/'SHOTKIT.app';audit_bundle(app)
    command(['codesign','--verify','--deep','--strict','--verbose=2',app])
    # No Homebrew PATH: checks must use the binaries and dependencies inside the .app.
    runenv=dict(os.environ,PATH='/usr/bin:/bin:/usr/sbin:/sbin')
    for key in list(runenv):
        if key.startswith(('PYTHON','DYLD_')):runenv.pop(key,None)
    report=dist/'mac-self-test.json'
    command([app/'Contents'/'MacOS'/'SHOTKIT','--self-test',report],cwd='/tmp',env=runenv,timeout=300)
    if not json.loads(report.read_text())['ok']:raise RuntimeError('앱 내부 검사 실패: '+str(report))
    suffix='AppleSilicon' if arch=='arm64' else 'Intel'
    archive=dist/f'SHOTKIT_Mac_{suffix}.zip'
    def pack():
        archive.unlink(missing_ok=True)
        command(['ditto','-c','-k','--sequesterRsrc','--keepParent',app,archive])
    pack()
    if args.notary_profile:
        command(['xcrun','notarytool','submit',archive,'--keychain-profile',args.notary_profile,'--wait'])
        command(['xcrun','stapler','staple',app]);command(['xcrun','stapler','validate',app])
        command(['spctl','--assess','--type','execute','--verbose=2',app]);pack()
    (dist/'build-status.json').write_text(json.dumps({'architecture':arch,'macOS':platform.mac_ver()[0],
        'signed':bool(args.identity),'notarized':bool(args.notary_profile),'self_test':True,
        'manual_gui_test':False,'sha256':hashlib.sha256(archive.read_bytes()).hexdigest()},indent=2))
    print('완료:',archive)
    print('배포 전 앱을 직접 열어 파일 선택, 영상 소리, 드래그, 저장을 확인하세요.')

if __name__=='__main__':
    try:main()
    except Exception as e:print('빌드 중단:',e,file=sys.stderr);sys.exit(1)
