import { showProgress, hideProgress } from './ui/progress.js';

const picker = document.getElementById('library');
const canvas = document.getElementById('canvas');
const coreName = 'koprulu-core.mpq';
const packPath = 'generated/repack/';
const archiveName = /^koprulu-[a-z0-9-]+\.mpq$/;
const localHost = ['127.0.0.1', 'localhost', '[::1]'].includes(location.hostname);
const files = new Map();
let engineLoaded = false;
const params = new URLSearchParams(location.search);
const progress = document.getElementById('install-progress');
let retry = null;
let registration;

function retryInstall() {
  if (!retry) return;
  const action = retry;
  retry = null;
  progress.removeAttribute('tabindex');
  progress.setAttribute('role', 'status');
  action().catch(showFailure);
}
progress.addEventListener('click', retryInstall);
progress.addEventListener('keydown', event => {
  if (event.key === 'Enter' || event.key === ' ') {
    event.preventDefault();
    retryInstall();
  }
});
window.addEventListener('online', retryInstall);

// Only the requesting tab supplies files to its service worker. No upload or persistent copy.
navigator.serviceWorker?.addEventListener('message', event => {
  if (event.data?.type === 'read-archive') {
    // A missing file in an explicitly selected folder must never fall back to a different library.
    event.ports[0]?.postMessage(files.size ? (files.get(event.data.name) ?? false) : null);
  }
});

async function enableWorker() {
  const workers = navigator.serviceWorker;
  if (!workers) throw new Error('Service workers unavailable');
  // Reopening an installed copy must work immediately even with the network down.
  registration ??= await workers.getRegistration('./');
  if (!registration?.active || !workers.controller) {
    registration = await workers.register('./local-files.js', { scope: './', updateViaCache: 'none' });
    if (!workers.controller) {
      await new Promise((resolve, reject) => {
        const timer = setTimeout(() => finish(new Error('Installation timed out')), 120000);
        const worker = registration.installing || registration.waiting;
        function finish(error) {
          clearTimeout(timer);
          workers.removeEventListener('controllerchange', controlled);
          worker?.removeEventListener('statechange', changed);
          if (error) reject(error);
          else resolve();
        }
        function controlled() { if (workers.controller) finish(); }
        function changed() {
          if (worker?.state === 'redundant' && !workers.controller) finish(new Error('Shell download interrupted'));
        }
        workers.addEventListener('controllerchange', controlled);
        worker?.addEventListener('statechange', changed);
        controlled();
        changed();
      });
    }
  }
  if (navigator.onLine) registration.update().catch(() => {});
}

// Safari may suspend a Home Screen app mid-download. Repeating the message resumes verified parts.
function installLibrary() {
  const workers = navigator.serviceWorker;
  return new Promise((resolve, reject) => {
    let watchdog;
    let lastFraction = 0;
    function finish(error) {
      clearTimeout(watchdog);
      workers.removeEventListener('message', message);
      workers.removeEventListener('controllerchange', begin);
      document.removeEventListener('visibilitychange', foreground);
      if (error) reject(error);
      else resolve();
    }
    function arm() {
      clearTimeout(watchdog);
      watchdog = setTimeout(() => finish(new Error('Download paused')), 120000);
    }
    function begin() {
      arm();
      workers.controller?.postMessage({ type: 'install-library' });
    }
    function foreground() { if (!document.hidden) begin(); }
    function message(event) {
      const data = event.data;
      if (event.source !== workers.controller || data?.type !== 'library-progress') return;
      arm();
      if (data.state === 'ready') return finish();
      if (data.state === 'error') return finish(new Error(data.error || 'Download interrupted'));
      if (data.totalBytes > 0) lastFraction = data.bytes / data.totalBytes;
      showProgress(lastFraction, 'Installing');
    }
    workers.addEventListener('message', message);
    workers.addEventListener('controllerchange', begin);
    document.addEventListener('visibilitychange', foreground);
    begin();
  });
}

function showPicker(message) {
  hideProgress();
  document.title = 'StarCraft · Выберите папку repack';
  picker.hidden = false;
  picker.disabled = false;
  canvas.hidden = true;
  if (message) {
    console.error(message);
    picker.setCustomValidity(message);
    picker.reportValidity();
  }
}

function showFailure(error) {
  const message = error instanceof Error ? error.message : String(error);
  if (engineLoaded) {
    console.error(error);
    window.alert('StarCraft: ' + message + '\nОбновите страницу, чтобы повторить запуск.');
    return;
  }
  if (params.has('files') || params.has('folder')) return showPicker(message);
  console.error(error);
  canvas.hidden = true;
  picker.hidden = true;
  const label = /quota|storage.*(exceed|full)|not enough space/i.test(message)
    ? 'Storage full - tap to retry' : 'Tap to retry';
  showProgress(null, label);
  progress.setAttribute('role', 'button');
  progress.setAttribute('tabindex', '0');
  progress.setAttribute('aria-label', message + '. Повторить запуск');
  retry = openLibrary;
}

async function launch(core, entries) {
  if (typeof WebAssembly === 'undefined' || !WebAssembly.validate(new Uint8Array([0, 97, 115, 109, 1, 0, 0, 0]))) {
    throw new Error('Для игры нужен браузер с WebAssembly.');
  }
  const probe = document.createElement('canvas').getContext('webgl2');
  if (!probe) throw new Error('Браузер не смог открыть WebGL 2. Проверьте аппаратное ускорение.');
  probe.getExtension('WEBGL_lose_context')?.loseContext();

  const bytes = new Uint8Array(await core.arrayBuffer());
  if (bytes.length < 32 || bytes[0] !== 0x4d || bytes[1] !== 0x50 || bytes[2] !== 0x51 || bytes[3] !== 0x1a) {
    throw new Error('Файл koprulu-core.mpq повреждён.');
  }
  // Streaming groups have an index in the core. Only their lengths belong in web_sizes.
  const sizes = entries.filter(e => /^koprulu-(cinematics|hd2?|ui-|portraits)/.test(e.name))
    .map(e => packPath + e.name + ' ' + e.size + '\n').join('');
  const args = ['--graphics', 'original', '--font', 'original'];
  const mission = params.get('mission');
  if (window.matchMedia('(pointer: coarse)').matches) args.push('--frame-cap', '30');
  if (mission && /^(?:exp)?(terran|zerg|protoss)\/\1[0-9]{2}[a-z]?$/.test(mission)) {
    args.push('--mission', mission, '--play');
  }
  window.Module = {
    canvas,
    arguments: args,
    locateFile: name => new URL('./engine/' + name, location.href).href,
    preRun: [module => {
      const fs = (module || window.Module).FS;
      fs.mkdirTree('/data/' + packPath);
      fs.writeFile('/data/' + packPath + coreName, bytes, { canOwn: true });
      fs.writeFile('/data/generated/web_sizes.txt', sizes);
    }],
    onRuntimeInitialized: () => {
      hideProgress();
      canvas.hidden = false;
      canvas.focus();
    },
    onAbort: reason => showFailure('Движок остановился: ' + reason),
    print: console.log,
    printErr: console.error,
  };
  document.title = 'StarCraft · Brood War';
  picker.hidden = true;
  showProgress(null, 'Starting game');
  const script = document.createElement('script');
  script.src = './engine/koprulu.js';
  script.onerror = () => {
    engineLoaded = false;
    showFailure(new Error('Не удалось загрузить движок.'));
  };
  engineLoaded = true;
  document.head.append(script);
}

picker.onchange = async () => {
  picker.disabled = true;
  picker.setCustomValidity('');
  try {
    files.clear();
    for (const file of picker.files) {
      const name = file.name.toLowerCase();
      if (!archiveName.test(name)) continue;
      if (files.has(name)) throw new Error('Выберите одну папку repack. Найдены повторяющиеся архивы.');
      files.set(name, file);
    }
    picker.value = '';
    if (!files.has(coreName) || !files.has('koprulu-music.mpq')) {
      throw new Error('Выберите подготовленную папку repack целиком, включая koprulu-core.mpq и koprulu-music.mpq.');
    }
    await enableWorker();
    await launch(files.get(coreName), [...files].map(([name, file]) => ({ name, size: file.size })));
  } catch (error) {
    showFailure(error);
  }
};
if ('webkitdirectory' in picker && !params.has('files')) {
  picker.webkitdirectory = true;
}

canvas.addEventListener('contextmenu', e => e.preventDefault());
window.addEventListener('keydown', e => {
  if (canvas.hidden || e.metaKey || e.getModifierState('AltGraph')) return;
  if (/^F([1-9]|10)$/.test(e.key) || ((e.ctrlKey || e.altKey) && /^[a-z\d]$/i.test(e.key))) e.preventDefault();
}, true);

async function openLibrary() {
  if (params.has('files') || params.has('folder')) return showPicker();
  // Safari and the Home Screen app can have separate storage. The installation link
  // loads just the splash in Safari; the icon downloads the campaigns into its own storage.
  const standalone = navigator.standalone || matchMedia('(display-mode: standalone)').matches;
  if (params.get('install') === 'iphone' && !standalone && 'standalone' in navigator) {
    showProgress(null, 'Share > Add to Home Screen');
    return;
  }
  showProgress(null, 'Loading...');
  if (localHost) {
    const manifest = await fetch('./private-library.json', {cache: 'no-store'}).catch(() => null);
    if (manifest?.ok && manifest.headers.get('content-type')?.includes('application/json')) {
      const entries = await manifest.json();
      if (entries.some(e => e.name === coreName)) {
        const response = await fetch(packPath + coreName);
        if (!response.ok) throw new Error('Не удалось прочитать локальный архив koprulu-core.mpq.');
        await launch(await response.blob(), entries);
        return;
      }
    }
  }
  await enableWorker();
  await installLibrary();
  showProgress(null, 'Starting game');
  if (navigator.storage?.persist) navigator.storage.persist().catch(() => {});
  const [index, core] = await Promise.all([fetch('./library/index.json'), fetch(packPath + coreName)]);
  if (!index.ok || !core.ok) throw new Error('Installed game data unavailable');
  await launch(await core.blob(), (await index.json()).archives);
}
let lastUpdateCheck = Date.now();
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && registration && navigator.onLine && Date.now() - lastUpdateCheck > 60000) {
    lastUpdateCheck = Date.now();
    registration.update().catch(() => {});
  }
});
openLibrary().catch(showFailure);
