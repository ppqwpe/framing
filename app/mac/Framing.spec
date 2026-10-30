# Build only on macOS, using build_mac.py. Native architecture, not cross-compiled.
import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files
root=Path(SPECPATH)
ffmpeg=Path(os.environ['FRAMING_BUILD_FFMPEG'])
ffprobe=Path(os.environ['FRAMING_BUILD_FFPROBE'])
binaries=[(str(ffmpeg),'_ffmpeg'),(str(ffprobe),'_ffmpeg')]
datas=collect_data_files('webview')+[(str(root/'build-metadata'),'build-metadata')]
identity=os.environ.get('FRAMING_CODESIGN_IDENTITY') or None
a=Analysis([str(root/'src'/'mac_app.py')],pathex=[str(root/'src')],binaries=binaries,datas=datas,
    hiddenimports=['webview.platforms.cocoa','AppKit','Foundation','WebKit','Quartz','objc','cv2','numpy','PIL','mac_diagnostics'],
    excludes=['tkinter','PyQt5','PyQt6','PySide2','PySide6','webview.platforms.winforms','clr','pythonnet','matplotlib','scipy','pandas'],
    noarchive=False)
pyz=PYZ(a.pure)
exe=EXE(pyz,a.scripts,[],exclude_binaries=True,name='Framing',console=False,
    strip=False,upx=False,argv_emulation=False,target_arch=os.environ['FRAMING_BUILD_ARCH'],
    codesign_identity=identity,entitlements_file=None)
coll=COLLECT(exe,a.binaries,a.datas,name='Framing',strip=False,upx=False)
app=BUNDLE(coll,name='Framing.app',icon=str(root/'assets'/'Framing.icns'),
    bundle_identifier='studio.framing.desktop',version='1.0.0',
    info_plist={'CFBundleDisplayName':'Framing','CFBundleShortVersionString':'1.0.0','CFBundleVersion':'20261012.1',
                'NSHighResolutionCapable':True,'NSRequiresAquaSystemAppearance':False,
                'NSLocalNetworkUsageDescription':'Framing 내부 화면과 영상 처리 기능을 연결합니다.',
                'NSAppTransportSecurity':{'NSAllowsLocalNetworking':True}})
