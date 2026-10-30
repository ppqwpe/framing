# Framing Mac

Mac에서 직접 실행하는 데스크톱 앱의 소스와 빌드 도구입니다. **이 폴더는 아직 빌드된 .app이 아닙니다.** 현재 작업 환경은 Windows이므로 macOS 실행 검증은 하지 않았습니다.

앱을 빌드하면 사용자는 ZIP을 풀고 Framing.app을 응용 프로그램 폴더로 옮겨 실행합니다. Python·Homebrew·FFmpeg는 빌드하는 Mac에만 필요하며, 배포받는 사용자에게 별도 설치를 요구하지 않도록 묶습니다. 실제 배포 전에는 개발 도구가 없는 Mac에서 확인해야 합니다.

## 앱 동작

- Cocoa와 WKWebView를 사용하는 Mac 앱 창
- macOS 파일·폴더 선택 창, 작업 파일 저장 창
- 기존 Framing의 프레임 추출, 컷 분할, 리타임, 메모 복원, 역재생, 대조, 검사, 콘택트시트, 변환, 이름 변경
- 결과 폴더와 파일을 Finder로 열기
- 앱 실행과 함께 내부 처리 시작, 종료할 때 처리 프로세스 정리
- 개인 파일은 Mac에서 처리하며 외부 서버에 업로드하지 않음

화면과 영상 재생을 연결하는 내부 통신은 앱이 자동으로 관리합니다. 고정 포트나 접속 비밀번호를 입력할 필요가 없습니다. 창을 띄우지 못하면 오류를 표시하며 외부 브라우저로 전환하지 않습니다.

기본 보간은 FFmpeg입니다. Windows용 RIFE exe는 Mac에서 실행하지 않으며, 기본 빌드에서는 RIFE 선택지를 숨깁니다. Mac GPU 보간은 이 패키지의 검증 범위에 포함되지 않습니다. 메모 복원은 원본 프레임을 보존한 결과에 한해 프레임 수와 순서를 되돌리며, 원본 음성이나 압축 전 화질을 복구하지 않습니다.

## Mac을 사용할 수 있게 됐을 때

빌드용 Mac에 Homebrew와 Xcode Command Line Tools를 준비합니다. 터미널에서 다음을 실행하세요.

```sh
brew install python@3.12 ffmpeg
cd "FRAMING_Mac_Source 폴더의 경로"
bash Mac에서_빌드.command
```

빌드 도구는 Python 가상환경을 만들고 정해진 의존성을 설치합니다. FFmpeg·ffprobe와 필요한 동적 라이브러리를 앱에 넣고, 외부 Homebrew 경로가 남았는지 검사합니다. 이어서 앱 내부 실행 파일로 실제 영상 생성·대조 소리·보간·메모 복원·변환 등을 검사합니다. 이 단계 중 하나라도 실패하면 최종 ZIP을 만들지 않습니다.

완료 파일:

```text
dist/arm64/Framing.app
dist/arm64/FRAMING_Mac_AppleSilicon.zip
```

Intel Mac에서 빌드하면 `dist/x86_64/FRAMING_Mac_Intel.zip`이 만들어집니다. M 시리즈는 arm64, Intel은 x86_64로 **각각 빌드**합니다. Universal 앱이나 이전 macOS 호환성을 검증했다고 간주하면 안 됩니다. 빌드한 macOS 버전은 `build-status.json`에 기록됩니다.

## 공개 배포용 서명·공증

일반 개발 빌드에는 Developer ID 공증이 없습니다. Apple Developer ID Application 인증서와 키체인에 등록된 공증 프로필을 준비한 뒤 다음을 실행하세요. 인증서나 비밀번호를 소스에 넣지 마세요.

```sh
bash Mac에서_빌드.command \
  --identity "Developer ID Application: 실제 이름 (TEAMID)" \
  --notary-profile "키체인에 등록한 프로필 이름"
```

이 경우 서명 확인, Apple 공증, 티켓 첨부와 실행 정책 검사를 통과한 뒤 ZIP을 다시 만듭니다. 일반 빌드와 공증된 빌드의 상태는 `build-status.json`으로 구분됩니다. 빌드 시 FFmpeg 라이선스·빌드 설정과 Python 패키지 목록도 `build-metadata`에 기록합니다. 공개 배포에는 사용하는 FFmpeg 빌드와 의존성의 라이선스에 맞는 고지·소스 제공을 별도로 준비해야 합니다.

## Mac에서 마지막으로 확인할 것

1. Python·Homebrew가 없는 별도 Mac에서 앱을 실행합니다.
2. 클릭해서 영상 두 개 넣기, 선택 취소, 한글·공백 파일명, 폴더 선택을 확인합니다.
3. 두 영상 대조 시 화면과 A/B 음성을 확인하고 결과를 저장합니다.
4. 보간과 메모 복원, 작업 저장·열기, 이름 변경·되돌리기를 확인합니다.
5. 실행 중 취소, 창 닫기, 다시 실행, Finder에서 결과 열기를 확인합니다.
6. 드래그 입력은 실제 창에서 확인합니다. 연결이 실패하면 클릭 선택을 사용합니다.

자동 검사만으로 위 UI 검사를 통과했다고 표시하지 않습니다. `build-status.json`의 `manual_gui_test`는 기본값이 false입니다.

설정: `~/Library/Application Support/Framing`

로그: `~/Library/Logs/Framing`

화면 캐시: `~/Library/Caches/Framing`

## 코드 구조

`src/mac_app.py`는 앱 시작·Mac용 API·화면을, `src/mac_runtime.py`는 경로·선택 창·로그·프로세스 종료를 맡습니다. `src/framing_core.py`는 기존 처리 엔진이며 Mac의 RIFE 실행 파일 구분만 추가했습니다. Windows 앱과 웹앱 파일은 수정하지 않았습니다.

회귀 검사:

```sh
.venv-mac/bin/python -m unittest discover -s tests -v
```

구현 참고: [pywebview macOS 의존성](https://pywebview.flowrl.com/guide/installation), [PyInstaller macOS 아키텍처·서명](https://pyinstaller.org/en/stable/feature-notes.html).
