'use strict';
// VILO desktop — no CMD, no external browser, one desktop instance.
const { app, BrowserWindow, shell, dialog } = require('electron');
const path = require('path');
const fs = require('fs');
const net = require('net');
const http = require('http');
const { spawn } = require('child_process');

let mainWindow = null;
let backendProcess = null;
let isQuitting = false;
let localOrigin = null;

const instanceLock = app.requestSingleInstanceLock();
if (!instanceLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (!mainWindow) return;
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
  });
}

// Ask Windows to group the taskbar window under the VILO application icon.
if (process.platform === 'win32') app.setAppUserModelId('app.vilo.desktop');

function chooseFreePort() {
  return new Promise((resolve, reject) => {
    const socket = net.createServer();
    socket.once('error', reject);
    socket.listen(0, '127.0.0.1', () => {
      const port = socket.address().port;
      socket.close(error => error ? reject(error) : resolve(port));
    });
  });
}

function initLog(userDir) {
  const file = path.join(userDir, 'vilo-desktop.log');
  try {
    if (fs.existsSync(file) && fs.statSync(file).size > 2 * 1024 * 1024) {
      fs.renameSync(file, file + '.old');
    }
  } catch { /* Logs must not block application startup. */ }
  const fd = fs.openSync(file, 'a');
  fs.writeSync(fd, '\n=== VILO desktop start ' + new Date().toISOString() + ' ===\n');
  return fd;
}

function waitForBackend(port, child) {
  return new Promise((resolve, reject) => {
    const begin = Date.now();
    let finished = false;
    const fail = error => { if (!finished) { finished = true; reject(error); } };
    child.once('error', fail);
    child.once('exit', (code, signal) => fail(new Error(
      `Serwer VILO zakończył działanie (kod: ${code}, sygnał: ${signal || '-'}).`
    )));
    const check = () => {
      if (finished) return;
      if (Date.now() - begin > 30000) {
        fail(new Error('Serwer lokalny VILO nie uruchomił się w ciągu 30 sekund.'));
        return;
      }
      const request = http.get({ hostname: '127.0.0.1', port, path: '/api/health', timeout: 1200 }, response => {
        response.resume();
        if (response.statusCode === 200) { finished = true; resolve(); }
        else setTimeout(check, 250);
      });
      request.on('timeout', () => request.destroy());
      request.on('error', () => setTimeout(check, 250));
    };
    check();
  });
}

async function runDesktop() {
  const userDir = path.join(app.getPath('userData'), 'local-data');
  fs.mkdirSync(userDir, { recursive: true });
  const logFd = initLog(app.getPath('userData'));
  const port = await chooseFreePort();

  let executable;
  let args;
  if (app.isPackaged) {
    executable = path.join(process.resourcesPath, 'backend', 'vilo-server.exe');
    args = [];
    if (!fs.existsSync(executable)) {
      throw new Error('Brakuje backendu w instalacji: ' + executable);
    }
  } else {
    executable = process.platform === 'win32' ? 'py' : 'python3';
    const devFile = path.join(__dirname, 'backend', 'backend_entry.py');
    args = process.platform === 'win32' ? ['-3', devFile] : [devFile];
  }

  try {
    backendProcess = spawn(executable, args, {
      cwd: app.isPackaged ? path.join(process.resourcesPath, 'backend') : path.join(__dirname, 'backend'),
      env: { ...process.env, VILO_PORT: String(port), VILO_DATA_DIR: userDir },
      windowsHide: true,
      stdio: ['ignore', logFd, logFd]
    });
  } finally {
    // The child inherited this log handle; close only our copy.
    fs.closeSync(logFd);
  }
  await waitForBackend(port, backendProcess);
  localOrigin = `http://127.0.0.1:${port}`;
  mainWindow = new BrowserWindow({
    width: 1280, height: 830, minWidth: 850, minHeight: 570,
    show: false, autoHideMenuBar: true,
    title: 'VILO', backgroundColor: '#10131e',
    icon: path.join(__dirname, 'backend', 'static', 'vilo-icon.png'),
    webPreferences: {
      nodeIntegration: false, contextIsolation: true,
      sandbox: true, webSecurity: true
    }
  });
  mainWindow.once('ready-to-show', () => mainWindow?.show());
  mainWindow.webContents.on('will-navigate', (event, url) => {
    if (!url.startsWith(localOrigin + '/')) {
      event.preventDefault();
      if (url.startsWith('https://')) shell.openExternal(url);
    }
  });
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith('https://')) shell.openExternal(url);
    return { action: 'deny' };
  });
  mainWindow.on('closed', () => { mainWindow = null; });
  await mainWindow.loadURL(localOrigin + '/');
  mainWindow.show();
}

if (instanceLock) {
  app.whenReady().then(runDesktop).catch(error => {
    const log = path.join(app.getPath('userData'), 'vilo-desktop.log');
    dialog.showErrorBox('VILO — nie można uruchomić',
      `${error.message}\n\nDiagnostyka: ${log}`);
    app.quit();
  });
}
app.on('window-all-closed', () => app.quit());
app.on('before-quit', () => {
  isQuitting = true;
  if (backendProcess && backendProcess.exitCode === null) {
    try { backendProcess.kill(); } catch { /* Already stopped */ }
  }
});
