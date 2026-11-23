const picker = document.getElementById('library');
const canvas = document.getElementById('canvas');
const coreName = 'koprulu-core.mpq';
const packPath = 'generated/repack/';
const archiveName = /^koprulu-[a-z0-9-]+\.mpq$/;
const localHost = ['127.0.0.1', 'localhost', '[::1]'].includes(location.hostname);
const files = new Map();
let engineLoaded = false;

// Only the requesting tab supplies files to its service worker. No upload or persistent copy.
navigator.serviceWorker?.addEventListener('message', event => {
  if (event.data?.type === 'read-archive') {
    // A missing file in an explicitly selected folder must never fall back to a different library.
    event.ports[0]?.postMessage(files.size ? (files.get(event.data.name) ?? false) : null);
  }
});

async function enableLocalFiles() {
  if (!navigator.serviceWorker) throw new Error('Откройте версию на GitHub Pages или на localhost.');
  await navigator.serviceWorker.register('./local-files.js', { scope: './' });
  await navigator.serviceWorker.ready;
  if (!navigator.serviceWorker.controller) {
    await new Promise((resolve, reject) => {
      const timeout = setTimeout(() => reject(new Error('Обновите страницу, чтобы браузер подключил локальные файлы.')), 8000);
      navigator.serviceWorker.addEventListener('controllerchange', () => {
        clearTimeout(timeout);
        resolve();
      }, { once: true });
    });
  }
}

function showPicker(message) {
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
  showPicker(message);
}

async function launch(core, entries) {
  if (!WebAssembly.validate(new Uint8Array([0, 97, 115, 109, 1, 0, 0, 0]))) {
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
  const mission = new URLSearchParams(location.search).get('mission');
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
    onAbort: reason => showFailure('Движок остановился: ' + reason),
    print: console.log,
    printErr: console.error,
  };
  document.title = 'StarCraft · Brood War';
  picker.hidden = true;
  canvas.hidden = false;
  canvas.focus();
  const script = document.createElement('script');
  script.src = './engine/koprulu.js';
  script.onerror = () => {
    engineLoaded = false;
    showPicker('Не удалось загрузить движок. Проверьте подключение и выберите папку ещё раз.');
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
    await enableLocalFiles();
    await launch(files.get(coreName), [...files].map(([name, file]) => ({ name, size: file.size })));
  } catch (error) {
    showFailure(error);
  }
};
if ('webkitdirectory' in picker) picker.webkitdirectory = true;

canvas.addEventListener('contextmenu', e => e.preventDefault());
window.addEventListener('keydown', e => {
  if (canvas.hidden || e.metaKey || e.getModifierState('AltGraph')) return;
  if (/^F([1-9]|10)$/.test(e.key) || ((e.ctrlKey || e.altKey) && /^[a-z\d]$/i.test(e.key))) e.preventDefault();
}, true);

async function openLibrary() {
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
  showPicker();
}
openLibrary().catch(showFailure);
