const { app, BrowserWindow, shell, Menu } = require('electron');
const { spawn, spawnSync } = require('child_process');
const path = require('path');
const fs = require('fs');
const net = require('net');
const os = require('os');

const PORT = parseInt(process.env.FOCUSTRACKER_PORT || '5050', 10);
const HOST = '127.0.0.1';
const URL = `http://${HOST}:${PORT}`;

const isPackaged = app.isPackaged;
const backendRoot = isPackaged
  ? path.join(process.resourcesPath, 'backend')
  : path.resolve(__dirname, '..');

const VENV = path.join(os.homedir(), '.focustracker-venv');
const VENV_PY = path.join(VENV, 'bin', 'python');

let backendProc = null;
let mainWindow = null;
let backendStartedByUs = false;

function portInUse(port) {
  return new Promise((resolve) => {
    const tester = net.createServer()
      .once('error', () => resolve(true))
      .once('listening', () => tester.once('close', () => resolve(false)).close())
      .listen(port, HOST);
  });
}

function waitForPort(port, timeoutMs = 15000) {
  const start = Date.now();
  return new Promise((resolve, reject) => {
    const tryOnce = () => {
      const sock = net.createConnection(port, HOST);
      sock.once('connect', () => { sock.end(); resolve(); });
      sock.once('error', () => {
        sock.destroy();
        if (Date.now() - start > timeoutMs) return reject(new Error('Backend timeout'));
        setTimeout(tryOnce, 250);
      });
    };
    tryOnce();
  });
}

function ensureVenv() {
  if (fs.existsSync(VENV_PY)) return;
  console.log('Creating venv at', VENV);
  const create = spawnSync('python3', ['-m', 'venv', VENV], { stdio: 'inherit' });
  if (create.status !== 0) throw new Error('venv creation failed');
  const pip = path.join(VENV, 'bin', 'pip');
  const req = path.join(backendRoot, 'requirements.txt');
  const install = spawnSync(pip, ['install', '-q', '-r', req], { stdio: 'inherit' });
  if (install.status !== 0) throw new Error('pip install failed');
}

async function startBackend() {
  const running = await portInUse(PORT);
  if (running) {
    console.log(`Backend already on port ${PORT} — reusing`);
    return;
  }
  ensureVenv();
  const logDir = path.join(os.homedir(), '.focustracker', 'logs');
  fs.mkdirSync(logDir, { recursive: true });
  const logFd = fs.openSync(path.join(logDir, 'app.log'), 'a');

  backendProc = spawn(VENV_PY, ['app.py'], {
    cwd: backendRoot,
    env: {
      ...process.env,
      FOCUSTRACKER_DATA_DIR: process.env.FOCUSTRACKER_DATA_DIR || '/Users/Shared/FocusTracker',
      FOCUSTRACKER_PORT: String(PORT),
    },
    stdio: ['ignore', logFd, logFd],
    detached: false,
  });
  backendStartedByUs = true;
  backendProc.on('exit', (code) => {
    console.log('Backend exited with', code);
    backendProc = null;
  });
  await waitForPort(PORT, 20000);
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1200,
    height: 820,
    minWidth: 900,
    minHeight: 600,
    title: 'FocusTracker',
    backgroundColor: '#0f1115',
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  mainWindow.loadURL(URL);
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });
  mainWindow.on('closed', () => { mainWindow = null; });
}

function buildMenu() {
  const template = [
    {
      label: 'FocusTracker',
      submenu: [
        { role: 'about' },
        { type: 'separator' },
        { role: 'hide' }, { role: 'hideOthers' }, { role: 'unhide' },
        { type: 'separator' },
        { role: 'quit' },
      ],
    },
    { label: 'Edit', submenu: [
      { role: 'undo' }, { role: 'redo' }, { type: 'separator' },
      { role: 'cut' }, { role: 'copy' }, { role: 'paste' }, { role: 'selectAll' },
    ]},
    { label: 'View', submenu: [
      { role: 'reload' }, { role: 'forceReload' }, { role: 'toggleDevTools' },
      { type: 'separator' },
      { role: 'resetZoom' }, { role: 'zoomIn' }, { role: 'zoomOut' },
      { type: 'separator' },
      { role: 'togglefullscreen' },
    ]},
    { role: 'windowMenu' },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

app.whenReady().then(async () => {
  buildMenu();
  try {
    await startBackend();
  } catch (err) {
    console.error('Backend start failed:', err);
  }
  createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => { app.quit(); });

app.on('before-quit', () => {
  if (backendProc && backendStartedByUs) {
    try { backendProc.kill('SIGTERM'); } catch (e) { /* noop */ }
  }
});
