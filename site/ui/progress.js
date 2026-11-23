import font from './font-metrics.js';

const image = name => {
  const resource = new Image();
  resource.src = new URL(name, import.meta.url).href;
  resource.addEventListener('load', paint);
  resource.addEventListener('error', paint);
  return resource;
};

const title = image('loading.png');
const sprites = image('sprites.png');
let canvas;
let host;
let percentage = null;
let caption = '';

function drawCaption(context, text) {
  const codes = Array.from(text.replaceAll('…', '...'), c => {
    const code = c.charCodeAt(0);
    return (code >= 32 && code <= 126 ? code : 63) - 32;
  });
  const width = codes.reduce((total, code) => total + font.advances[code], 0);
  const scale = width * 2 <= 600 ? 2 : 1;
  let x = Math.floor((640 - width * scale) / 2);
  for (const code of codes) {
    const sx = code % 16 * font.width;
    const sy = Math.floor(code / 16) * font.height;
    context.drawImage(sprites, sx, sy, font.width, font.height, x, 394, font.width * scale, font.height * scale);
    x += font.advances[code] * scale;
  }
}

function paint() {
  if (!canvas || host.hidden) return;
  const context = canvas.getContext('2d');
  const text = caption + (percentage === null ? '' : ' ' + percentage + '%');
  if (!context) return;
  context.fillStyle = '#000';
  context.fillRect(0, 0, 640, 480);
  context.imageSmoothingEnabled = false;
  if (title.complete && title.naturalWidth) context.drawImage(title, 0, 0);
  if (!sprites.complete || !sprites.naturalWidth) return;
  drawCaption(context, text);
  if (percentage === null) return;
  const x = 320 - font.barW;
  const y = 437;
  context.drawImage(sprites, 0, font.barY, font.barW, font.barH, x, y, font.barW * 2, font.barH * 2);
  const w = Math.round(font.barW * percentage / 100);
  if (w) context.drawImage(sprites, 0, font.barY + font.barH, w, font.barH, x, y, w * 2, font.barH * 2);
}

export function showProgress(fraction, label) {
  if (!host) {
    host = document.getElementById('install-progress');
    canvas = document.createElement('canvas');
    canvas.width = 640;
    canvas.height = 480;
    canvas.setAttribute('aria-hidden', 'true');
    canvas.style.cssText = 'width:min(100%,133.333vh);width:min(100%,133.333dvh);height:auto;display:block;image-rendering:pixelated';
    host.appendChild(canvas);
    host.style.cssText += ';position:fixed;inset:0;z-index:10;background:#000;place-items:center;touch-action:manipulation;';
  }
  const next = Number.isFinite(fraction) ? Math.max(0, Math.min(100, Math.floor(fraction * 100))) : null;
  const text = String(label || '');
  if (!host.hidden && text === caption && next === percentage) return;
  caption = text;
  percentage = next;
  host.hidden = false;
  host.style.display = 'grid';
  host.setAttribute('aria-label', caption + (next === null ? '' : ' ' + next + '%'));
  paint();
}

export function hideProgress() {
  if (host) {
    host.hidden = true;
    host.style.display = 'none';
  }
}
