// 화면(ui.html)이 쓸 수 있는 것만 꺼내 준다: 끌어다 놓은 파일의 경로, OS 파일 · 폴더 대화상자.
const { contextBridge, ipcRenderer, webUtils } = require('electron');

contextBridge.exposeInMainWorld('framingNative', {
  pathFor: (file) => webUtils.getPathForFile(file),
  pickFiles: () => ipcRenderer.invoke('framing:pick-files'),
  pickFolder: () => ipcRenderer.invoke('framing:pick-folder'),
});
