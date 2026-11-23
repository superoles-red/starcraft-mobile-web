// The release exporter supplies SC_RELEASE, SC_SHELL and SC_SHELL_HASHES here.
'use strict';

const scope = self.registration.scope;
const release = self.SC_RELEASE || '';
const prefix = 'starcraft-web:' + encodeURIComponent(scope) + ':';
const shellPrefix = prefix + 'shell:';
const shellName = shellPrefix + release;
const partsName = prefix + 'parts';
const metadataName = prefix + 'metadata';
const offlineRequested = new URL('_offline/requested', scope).href;
const partsRoot = new URL('_offline/sha256/', scope).href;
const packsRoot = new URL('generated/repack/', scope).href;
const manifestUrl = new URL('library/index.json', scope).href;
const indexUrl = new URL('index.html', scope).href;
const shellUrls = new Map((self.SC_SHELL || []).map(path => [new URL(path, scope).href, path]));
const filePattern = /^koprulu-[a-z0-9-]+\.mpq$/;
const shaPattern = /^[a-f0-9]{64}$/;
const isLocal = ['localhost', '127.0.0.1', '[::1]'].includes(self.location.hostname);
const transfers = new Map();
let catalogPromise, installation, warming, progress;

function shellRequest(url) {
  const path = shellUrls.get(url);
  const hash = self.SC_SHELL_HASHES?.[path];
  if (!shaPattern.test(hash || '')) throw new Error('Нет контрольной суммы файла оболочки ' + path);
  const binary = hash.match(/../g).map(pair => String.fromCharCode(parseInt(pair, 16))).join('');
  return new Request(url, {cache: 'reload', integrity: 'sha256-' + btoa(binary)});
}

function parseCatalog(json) {
  if (json.version !== 1 || !Array.isArray(json.archives) || !json.archives.length) {
    throw new Error('Не удалось прочитать список игровых архивов.');
  }
  const archives = new Map(), unique = new Map();
  let totalBytes = 0;
  for (const {name, size, parts} of json.archives) {
    if (!filePattern.test(name) || archives.has(name) || !Number.isSafeInteger(size) || size <= 0 || !Array.isArray(parts)) {
      throw new Error('Ошибка описания архива ' + name);
    }
    let offset = 0;
    const segments = [];
    for (const p of parts) {
      const url = new URL(p.url, scope).href;
      if (!shaPattern.test(p.hash) || !Number.isSafeInteger(p.size) || p.size <= 0 ||
          !url.startsWith(new URL('library/', scope).href) || ![undefined, 'identity', 'gzip'].includes(p.encoding)) {
        throw new Error('Некорректная часть архива ' + name);
      }
      let part = unique.get(p.hash);
      if (!part) {
        part = {hash: p.hash, size: p.size, encoding: p.encoding, url};
        unique.set(p.hash, part);
        totalBytes += part.size;
      } else if (part.size !== p.size) {
        throw new Error('Несовместимые размеры одной части архива.');
      }
      segments.push({offset, part});
      offset += part.size;
    }
    if (offset !== size) throw new Error('Не совпал полный размер ' + name);
    archives.set(name, {size, segments});
  }
  return {archives, unique, totalBytes};
}

function catalog() {
  if (!catalogPromise) {
    catalogPromise = (async () => {
      const shell = await caches.open(shellName);
      const response = await shell.match(manifestUrl);
      if (!response) throw new Error('Список архивов недоступен. Откройте игру с подключением к сети.');
      return parseCatalog(await response.json());
    })().catch(error => { catalogPromise = null; throw error; });
  }
  return catalogPromise;
}

// The decoded size bounds memory, including when a compressed response is incomplete.
async function download(cache, part) {
  const response = await fetch(part.url, {cache: 'no-store', credentials: 'same-origin'});
  if (!response.ok || !response.body) throw new Error('Не удалось скачать часть игровых данных (' + response.status + ').');
  let body = response.body;
  if (part.encoding === 'gzip') body = body.pipeThrough(new DecompressionStream('gzip'));
  const reader = body.getReader();
  const bytes = new Uint8Array(part.size);
  let offset = 0;
  try {
    while (true) {
      const {value, done} = await reader.read();
      if (done) break;
      if (offset + value.length > bytes.length) throw new Error('Размер части превышает ожидаемый.');
      bytes.set(value, offset);
      offset += value.length;
    }
  } catch (error) {
    await reader.cancel().catch(() => {});
    throw error;
  } finally {
    reader.releaseLock();
  }
  if (offset !== bytes.length) throw new Error('Часть игровых данных загружена не полностью.');
  const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', bytes));
  const hash = Array.from(digest, byte => byte.toString(16).padStart(2, '0')).join('');
  if (hash !== part.hash) throw new Error('Контрольная сумма игровых данных не совпала. Повторите загрузку.');
  await cache.put(partsRoot + part.hash, new Response(bytes, {headers: {
    'Content-Type': 'application/octet-stream', 'Content-Length': String(bytes.length)
  }}));
}

async function ensurePart(cache, part) {
  const key = partsRoot + part.hash;
  const cached = await cache.match(key);
  if (cached) return cached;
  let pending = transfers.get(part.hash);
  if (!pending) {
    pending = download(cache, part).finally(() => transfers.delete(part.hash));
    transfers.set(part.hash, pending);
  }
  await pending;
  const response = await cache.match(key);
  if (!response) throw new Error('Браузер освободил место, занятое игровой библиотекой.');
  return response;
}

function report(state, data, bytes = 0, count = 0, error) {
  return {type: 'library-progress', release, state, bytes, parts: count,
    totalBytes: data?.totalBytes || 0, totalParts: data?.unique.size || 0, ...(error ? {error} : {})};
}

async function inventory(cache, data) {
  const held = new Set();
  let bytes = 0;
  for (const request of await cache.keys()) {
    if (!request.url.startsWith(partsRoot)) continue;
    const hash = request.url.slice(partsRoot.length);
    const part = data.unique.get(hash);
    if (part) { held.add(hash); bytes += part.size; }
  }
  return {held, bytes};
}

async function status() {
  if (!release) throw new Error('Эта копия запускается с локальными файлами.');
  if (installation && progress) return progress;
  const data = await catalog();
  const {held, bytes} = await inventory(await caches.open(partsName), data);
  return report(held.size === data.unique.size ? 'ready' : 'idle', data, bytes, held.size);
}

async function broadcast(message) {
  // Capture clients before picking the latest value. Concurrent downloads cannot move the progress bar backwards.
  const clients = await self.clients.matchAll({type: 'window'});
  const latest = progress || message;
  for (const client of clients) client.postMessage(latest);
}

async function fillLibrary(announce) {
  const data = await catalog();
  const cache = await caches.open(partsName);
  const inventoryState = await inventory(cache, data);
  let bytes = inventoryState.bytes, count = inventoryState.held.size, next = 0, failure;
  const pending = [...data.unique.values()].filter(p => !inventoryState.held.has(p.hash));
  async function update(state, error) {
    if (announce) {
      progress = report(state, data, bytes, count, error);
      await broadcast(progress);
    }
  }
  await update('installing');
  async function lane() {
    while (!failure && next < pending.length) {
      const part = pending[next++];
      try {
        await ensurePart(cache, part);
        bytes += part.size;
        count++;
        await update('installing');
      } catch (error) {
        failure = error;
      }
    }
  }
  await Promise.all(Array.from({length: Math.min(3, pending.length)}, lane));
  if (failure) throw failure;
  if (announce) await update('ready');
}

function warmUpdate() {
  warming ??= fillLibrary(false).finally(() => { warming = null; });
  return warming;
}

function notifyWaiting() {
  const waiting = self.registration.waiting;
  if (waiting?.state === 'installed') waiting.postMessage({type: 'warm-update'});
}

async function installOffline() {
  if (!release) throw new Error('Эта копия запускается с локальными файлами.');
  if (!installation) {
    progress = null;
    installation = (async () => {
      const meta = await caches.open(metadataName);
      await meta.put(offlineRequested, new Response('1'));
      // An update can already be waiting when the user first chooses offline play.
      notifyWaiting();
      await fillLibrary(true);
      notifyWaiting();
    })().catch(async error => {
      const current = progress || await status().catch(() => report('idle'));
      progress = {...current, state: 'error', error: error.message || String(error)};
      await broadcast(progress);
    }).finally(() => { installation = null; });
  }
  return installation;
}

self.addEventListener('install', event => {
  if (!release) return;
  event.waitUntil((async () => {
    const older = (await caches.keys()).some(name => name.startsWith(shellPrefix) && name !== shellName);
    const shell = await caches.open(shellName);
    try {
      await shell.addAll([...shellUrls.keys()].map(shellRequest));
      await catalog();
      const meta = await caches.open(metadataName);
      if (await meta.match(offlineRequested)) await warmUpdate();
      // A legacy local-file worker predates release caches and cannot coordinate an installer.
      if (!older) await self.skipWaiting();
    } catch (error) {
      // A failed first migration must not look like a previous installed release on retry.
      await caches.delete(shellName);
      throw error;
    }
  })());
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    if (release) {
      // The browser has naturally retired the previous release after its last controlled tab closed.
      const retired = (await caches.keys()).filter(name => name.startsWith(shellPrefix) && name !== shellName);
      const current = await catalog();
      const parts = await caches.open(partsName);
      for (const name of retired) {
        const old = await (await caches.open(name)).match(manifestUrl);
        if (old) {
          const previous = parseCatalog(await old.json());
          for (const hash of previous.unique.keys()) {
            if (!current.unique.has(hash)) await parts.delete(partsRoot + hash);
          }
        }
        await caches.delete(name);
      }
    }
    await self.clients.claim();
  })());
});

self.addEventListener('message', event => {
  const type = event.data?.type;
  if (type === 'warm-update' && release) {
    event.waitUntil(warmUpdate());
  } else if (type === 'install-library') {
    event.waitUntil(installOffline().then(async () => {
      // A tab can join after the final broadcast, before the shared installation resolves.
      event.source?.postMessage(progress || await status());
    }).catch(error => event.source?.postMessage(report('error', null, 0, 0, error.message))));
  } else if (type === 'library-status') {
    event.waitUntil(status().then(message => event.source?.postMessage(message))
      .catch(error => event.source?.postMessage(report('error', null, 0, 0, error.message))));
  }
});

function rangeFor(header, size) {
  if (header === null) return [0, size - 1];
  const match = /^bytes=(\d*)-(\d*)$/i.exec(header.trim());
  if (!match || (!match[1] && !match[2])) return null;
  const start = match[1] ? Number(match[1]) : Math.max(0, size - Number(match[2]));
  const end = match[1] && match[2] ? Math.min(Number(match[2]), size - 1) : size - 1;
  return start <= end && start < size ? [start, end] : null;
}

function bytesResponse(request, size, makeBody) {
  const range = request.headers.get('Range');
  const interval = rangeFor(range, size);
  const headers = {'Accept-Ranges': 'bytes', 'Content-Type': 'application/octet-stream', 'Cache-Control': 'no-store'};
  if (!interval) return new Response(null, {status: 416, headers: {...headers, 'Content-Range': 'bytes */' + size}});
  const [start, end] = interval;
  if (range !== null) headers['Content-Range'] = 'bytes ' + start + '-' + end + '/' + size;
  headers['Content-Length'] = String(end - start + 1);
  return new Response(request.method === 'HEAD' ? null : makeBody(start, end), {status: range === null ? 200 : 206, headers});
}

function streamParts(archive, cache, start, end) {
  let index = archive.segments.findIndex(s => s.offset + s.part.size > start);
  return new ReadableStream({
    async pull(controller) {
      const {part, offset} = archive.segments[index++];
      const response = await ensurePart(cache, part);
      const bytes = new Uint8Array(await response.arrayBuffer());
      controller.enqueue(bytes.subarray(Math.max(0, start - offset), Math.min(bytes.length, end - offset + 1)));
      if (offset + part.size > end) controller.close();
    }
  });
}

function selectedFile(client, name) {
  return new Promise(resolve => {
    const channel = new MessageChannel();
    function finish(value) {
      clearTimeout(timer);
      channel.port1.close();
      resolve(value);
    }
    const timer = setTimeout(() => finish(null), 5000);
    channel.port1.onmessage = event => finish(event.data);
    client.postMessage({type: 'read-archive', name}, [channel.port2]);
  });
}

async function archiveResponse(event, name) {
  const client = event.clientId ? await self.clients.get(event.clientId) : null;
  const query = client && new URL(client.url).searchParams;
  const picker = query?.has('files') || query?.has('folder');
  if (!release || picker || (isLocal && query?.has('local'))) {
    if (client && (picker || !query?.has('local'))) {
      const file = await selectedFile(client, name);
      if (file) return bytesResponse(event.request, file.size, (start, end) => file.slice(start, end + 1));
      if (file === false || picker) return new Response('Local archive unavailable', {status: 404});
    }
    if (isLocal) return fetch(event.request);
    return new Response('Local archive unavailable', {status: 404});
  }
  const data = await catalog();
  const archive = data.archives.get(name);
  if (!archive) return new Response('Archive unavailable', {status: 404});
  const cache = await caches.open(partsName);
  return bytesResponse(event.request, archive.size, (start, end) => streamParts(archive, cache, start, end));
}

async function shellResponse(request, url) {
  const shell = await caches.open(shellName);
  let response = await shell.match(url);
  if (!response) {
    response = await fetch(shellRequest(url));
    if (!response.ok) throw new Error('Shell unavailable');
    await shell.put(url, response.clone());
  }
  if (request.method === 'HEAD') return new Response(null, {headers: response.headers, status: response.status});
  return response;
}

self.addEventListener('fetch', event => {
  if (!['GET', 'HEAD'].includes(event.request.method)) return;
  const url = new URL(event.request.url);
  const path = url.origin + url.pathname;
  if (path.startsWith(packsRoot)) {
    const name = path.slice(packsRoot.length);
    if (filePattern.test(name)) event.respondWith(archiveResponse(event, name));
    return;
  }
  if (!release) return;
  const key = path === scope ? indexUrl : path;
  if (shellUrls.has(key)) event.respondWith(shellResponse(event.request, key));
});
