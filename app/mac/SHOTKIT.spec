# Build only on macOS, using build_mac.py. Native architecture, not cross-compiled.
import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files
root=Path(SPECPATH)
ffmpeg=Path(os.environ['SHOTKIT_BUILD_FFMPEG'])
ffprobe=Path(os.environ['SHOTKIT_BUILD_FFPROBE'])
binaries=[(str(ffmpeg),'_ffmpeg'),(str(ffprobe),'_ffmpeg')]
datas=collect_data_files('webview')+[(str(root/'build-metadata'),'build-metadata')]
identity=os.environ.get('SHOTKIT_CODESIGN_IDENTITY') or None
a=Analysis([str(root/'src'/'mac_app.py')],pathex=[str(root/'src')],binaries=binaries,datas=datas,
    hiddenimports=['webview.platforms.cocoa','AppKit','Foundation','WebKit','Quartz','objc','cv2','numpy','PIL','mac_diagnostics'],
    excludes=['tkinter','PyQt5','PyQt6','PySide2','PySide6','webview.platforms.winforms','clr','pythonnet','matplotlib','scipy','pandas'],
    noarchive=False)
pyz=PYZ(a.pure)
exe=EXE(pyz,a.scripts,[],exclude_binaries=True,name='SHOTKIT',console=False,
    strip=False,upx=False,argv_emulation=False,target_arch=os.environ['SHOTKIT_BUILD_ARCH'],
    codesign_identity=identity,entitlements_file=None)
coll=COLLECT(exe,a.binaries,a.datas,name='SHOTKIT',strip=False,upx=False)
app=BUNDLE(coll,name='SHOTKIT.app',icon=str(root/'assets'/'SHOTKIT.icns'),
    bundle_identifier='studio.shotkit.desktop',version='1.0.0',
    info_plist={'CFBundleDisplayName':'SHOTKIT','CFBundleShortVersionString':'1.0.0','CFBundleVersion':'20261012.1',
                'NSHighResolutionCapable':True,'NSRequiresAquaSystemAppearance':False,
                'NSLocalNetworkUsageDescription':'SHOTKIT 내부 화면과 영상 처리 기능을 연결합니다.',
                'NSAppTransportSecurity':{'NSAllowsLocalNetworking':True}})
