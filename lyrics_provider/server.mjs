// issue 127: internal lyrics provider for Rhythm-Chorus-backend.
//
// Wraps @meting/core (metowolf/Meting) so the Python backend never re-implements each
// platform's encryption and endpoint churn; upgrading Meting is a version bump.
// Only search and lyric are exposed: no audio URLs (copyright), no cookies.
// Listens on 127.0.0.1 only; the backend containers share the host network on JDC.
import http from 'node:http';
import Meting from '@meting/core';

export const SERVERS = ['netease', 'tencent', 'kugou'];
const LIST_PATH = { netease: 'result.songs', tencent: 'data.song.list', kugou: 'data.info' };
// NetEase cloudsearch: 1 = song title / artist, 1006 = lyric full text.
const NETEASE_TYPE = { title: 1, lyrics: 1006 };
const TIMEOUT_MS = Number(process.env.LYRICS_PROVIDER_TIMEOUT_MS || 6000);

function pick(data, path) {
  return path.split('.').reduce((value, key) => (value == null ? value : value[key]), data);
}

function durationMs(server, raw) {
  const value = server === 'netease' ? raw.dt : server === 'tencent' ? raw.interval : raw.duration;
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return null;
  return server === 'netease' ? Math.round(value) : Math.round(value * 1000);
}

/** NetEase lyric search marks the matched line with <b>…</b>; keep that line only, untagged. */
export function snippetOf(server, raw) {
  if (server !== 'netease' || !Array.isArray(raw.lyrics)) return null;
  const line = raw.lyrics.find((text) => typeof text === 'string' && text.includes('<b>'));
  if (!line) return null;
  return line.replace(/<\/?b>/g, '').replace(/\s+/g, ' ').trim() || null;
}

/** Raw platform search response -> stable items; `format` is the Meting provider's own mapper. */
export function normalizeSearch(server, raw, format, limit) {
  let list = pick(raw, LIST_PATH[server]);
  if (!Array.isArray(list)) return [];
  const items = [];
  for (const entry of list) {
    const item = entry && entry.musicData ? entry.musicData : entry;
    let base;
    try {
      base = format(item);
    } catch {
      continue; // one malformed row must not drop the page
    }
    if (!base || !base.lyric_id || !base.name) continue;
    items.push({
      source: server,
      id: String(base.lyric_id),
      title: String(base.name).trim(),
      artists: (base.artist || []).map((name) => String(name).trim()).filter(Boolean),
      album: base.album ? String(base.album).trim() : null,
      duration_ms: durationMs(server, item),
      snippet: snippetOf(server, item),
    });
    if (items.length >= limit) break;
  }
  return items;
}

function withTimeout(promise) {
  let timer;
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error('upstream timeout')), TIMEOUT_MS);
    }),
  ]).finally(() => clearTimeout(timer));
}

export async function search(server, q, type, limit) {
  const meting = new Meting(server);
  meting.format(false);
  const option = { page: 1, limit };
  if (server === 'netease') option.type = NETEASE_TYPE[type];
  const raw = JSON.parse(await withTimeout(meting.search(q, option)));
  return normalizeSearch(server, raw, (item) => meting.provider.format(item), limit);
}

export async function lyric(server, id) {
  const meting = new Meting(server);
  meting.format(true);
  const data = JSON.parse(await withTimeout(meting.lyric(id)));
  return { lyric: typeof data.lyric === 'string' ? data.lyric : '', tlyric: typeof data.tlyric === 'string' ? data.tlyric : '' };
}

function send(res, status, body) {
  const text = JSON.stringify(body);
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'content-length': Buffer.byteLength(text) });
  res.end(text);
}

export function createServer() {
  return http.createServer(async (req, res) => {
    const url = new URL(req.url, 'http://localhost');
    const server = url.searchParams.get('server');
    try {
      if (req.method !== 'GET') return send(res, 405, { error: 'method_not_allowed' });
      if (url.pathname === '/health') return send(res, 200, { ok: true, servers: SERVERS });
      if (!SERVERS.includes(server)) return send(res, 400, { error: 'unsupported_server' });
      if (url.pathname === '/search') {
        const q = (url.searchParams.get('q') || '').trim();
        const type = url.searchParams.get('type') || 'title';
        const limit = Number(url.searchParams.get('limit') || 20);
        if (!q || q.length > 100 || !['title', 'lyrics'].includes(type) || !(limit >= 1 && limit <= 50)) {
          return send(res, 400, { error: 'invalid_query' });
        }
        // Only NetEase has a dedicated lyric index; the other platforms answer both kinds the same.
        return send(res, 200, { items: await search(server, q, type, Math.floor(limit)) });
      }
      if (url.pathname === '/lyric') {
        const id = url.searchParams.get('id') || '';
        if (!/^[A-Za-z0-9]{1,64}$/.test(id)) return send(res, 400, { error: 'invalid_id' });
        return send(res, 200, await lyric(server, id));
      }
      return send(res, 404, { error: 'not_found' });
    } catch (error) {
      // Upstream failures are expected now and then; the backend skips this source.
      console.warn(`lyrics provider upstream failed server=${server} path=${url.pathname} error=${error.message}`);
      return send(res, 502, { error: 'upstream_failed' });
    }
  });
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const host = process.env.LYRICS_PROVIDER_HOST || '127.0.0.1';
  const port = Number(process.env.LYRICS_PROVIDER_PORT || 8787);
  createServer().listen(port, host, () => console.log(`lyrics provider listening on ${host}:${port}`));
}
