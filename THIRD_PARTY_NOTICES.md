# Third-party notices

The release zips bundle the components below next to Framing's own code. They are not in this Git repository. License texts are in [app/licenses](app/licenses).

| Component | Version | License | Source |
|---|---|---|---|
| Python, Windows embeddable package | 3.12.10 | PSF License | https://www.python.org/ |
| NumPy | 2.5.4 | BSD-3-Clause | https://numpy.org/ |
| OpenCV (opencv-python-headless) | 5.0.0.93 | Apache-2.0 | https://github.com/opencv/opencv-python |
| Pillow | 13.0.0 | MIT-CMU (HPND) | https://python-pillow.org/ |
| FFmpeg, 64-bit static full build by gyan.dev | 9.0 | GPL-3.0 | https://www.gyan.dev/ffmpeg/builds/ · https://ffmpeg.org/releases/ffmpeg-9.0.tar.xz |
| RIFE ncnn Vulkan and the v4.6 model | 20221029 | MIT | https://github.com/nihui/rife-ncnn-vulkan/releases/tag/20221029 |
| Electron (Windows and Mac zips: `app/window`) | 38.8.6 | MIT, with Chromium's licenses in `app/window/LICENSES.chromium.html` | https://www.electronjs.org/ |

Python, NumPy, OpenCV, Pillow, FFmpeg and RIFE are in the Windows zip only. On a Mac, `Framing.command` installs NumPy, OpenCV and Pillow from PyPI and uses Homebrew's FFmpeg; RIFE is Windows-only.

FFmpeg and RIFE are separate, unmodified command-line programs that Framing runs as child processes. See `app/licenses/FFmpeg-GPL.txt`, `app/licenses/FFmpeg-build.txt` and `app/licenses/RIFE-LICENSE.txt`.

The web service (`app/web`) adds FastAPI (MIT) and Uvicorn (BSD-3-Clause), and uses the Debian FFmpeg package inside its container.

The mascot is the ALIENMEOW character (https://alienmeow.tv/), the same one Griding uses.
