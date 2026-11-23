// Supply ordinary byte-range HTTP semantics from the local files selected in this tab.
self.addEventListener('install', event => event.waitUntil(self.skipWaiting()));
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()));

function getFile(client, name) {
  return new Promise(resolve => {
    const channel = new MessageChannel();
    function finish(file) {
      clearTimeout(timeout);
      channel.port1.close();
      resolve(file);
    }
    const timeout = setTimeout(() => finish(null), 5000);
    channel.port1.onmessage = event => finish(event.data);
    client.postMessage({ type: 'read-archive', name }, [channel.port2]);
  });
}

async function serve(event, name) {
  const client = await self.clients.get(event.clientId);
  const file = client && await getFile(client, name);
  if (!file) {
    if (['127.0.0.1', 'localhost', '[::1]'].includes(location.hostname)) return fetch(event.request);
    return new Response('Local archive unavailable', { status: 404 });
  }
  const headers = { 'Accept-Ranges': 'bytes', 'Content-Type': 'application/octet-stream', 'Cache-Control': 'no-store' };
  const range = event.request.headers.get('Range');
  let start = 0, end = file.size - 1;
  if (range) {
    const match = /^bytes=(\d*)-(\d*)$/.exec(range);
    if (match && (match[1] || match[2])) {
      start = match[1] ? Number(match[1]) : Math.max(0, file.size - Number(match[2]));
      end = match[1] && match[2] ? Math.min(Number(match[2]), end) : end;
    } else start = file.size;
    if (start > end || start >= file.size) {
      return new Response(null, { status: 416, headers: { ...headers, 'Content-Range': 'bytes */' + file.size } });
    }
    headers['Content-Range'] = 'bytes ' + start + '-' + end + '/' + file.size;
  }
  headers['Content-Length'] = String(end - start + 1);
  const body = event.request.method === 'HEAD' ? null : file.slice(start, end + 1);
  return new Response(body, { status: range ? 206 : 200, headers });
}

self.addEventListener('fetch', event => {
  if (!['GET', 'HEAD'].includes(event.request.method)) return;
  const url = new URL(event.request.url);
  if (!url.href.startsWith(self.registration.scope + 'generated/repack/')) return;
  const name = url.href.slice((self.registration.scope + 'generated/repack/').length);
  if (/^koprulu-[a-z0-9-]+\.mpq$/.test(name)) event.respondWith(serve(event, name));
});
