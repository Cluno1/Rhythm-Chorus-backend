// Offline tests: fixtures are trimmed copies of real 2026-10-28 platform responses.
import test from 'node:test';
import assert from 'node:assert/strict';
import { once } from 'node:events';
import Meting from '@meting/core';
import { createServer, normalizeSearch, snippetOf } from './server.mjs';

const formatOf = (server) => {
  const meting = new Meting(server);
  return (item) => meting.provider.format(item);
};

test('网易云歌词搜索保留命中行并去掉加粗标记', () => {
  const raw = { result: { songs: [{
    id: 3339809937, name: '旅人 王铮亮', dt: 280785,
    ar: [{ name: '楓葉落幕' }], al: { name: '凡人修仙传动画 韩立结婴纪念曲', pic: 1 },
    lyrics: ['<b>你掌心的温度</b>催开花万丛', '你胸腔里还有不熄的火种'],
  }] } };
  const [item] = normalizeSearch('netease', raw, formatOf('netease'), 10);
  assert.deepEqual(item, {
    source: 'netease', id: '3339809937', title: '旅人 王铮亮', artists: ['楓葉落幕'],
    album: '凡人修仙传动画 韩立结婴纪念曲', duration_ms: 280785, snippet: '你掌心的温度催开花万丛',
  });
});

test('QQ 音乐时长从秒换算为毫秒且兼容新版字段', () => {
  const raw = { data: { song: { list: [{
    mid: '004BzmAS3gqK1T', name: '旅人', interval: 277,
    singer: [{ name: '王铮亮' }], album: { title: '凡人修仙传 韩立结婴纪念专辑', mid: 'x' },
  }] } } };
  const [item] = normalizeSearch('tencent', raw, formatOf('tencent'), 10);
  assert.equal(item.id, '004BzmAS3gqK1T');
  assert.equal(item.duration_ms, 277000);
  assert.equal(item.snippet, null);
});

test('单条坏数据不影响其余结果且遵守条数上限', () => {
  const good = (id) => ({ id, name: `歌${id}`, dt: 1000, ar: [{ name: 'A' }], al: { name: 'B', pic: 1 } });
  const raw = { result: { songs: [{ id: 1, name: '坏', ar: null, al: null }, good(2), good(3), good(4)] } };
  const items = normalizeSearch('netease', raw, formatOf('netease'), 2);
  assert.deepEqual(items.map((item) => item.id), ['2', '3']);
});

test('平台返回结构不对时返回空列表', () => {
  assert.deepEqual(normalizeSearch('kugou', { data: null }, formatOf('kugou'), 10), []);
});

test('非网易云来源没有片段', () => {
  assert.equal(snippetOf('tencent', { lyrics: ['<b>x</b>'] }), null);
});

test('HTTP 层拒绝不支持的来源与非法参数', async () => {
  const server = createServer().listen(0, '127.0.0.1');
  await once(server, 'listening');
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    assert.equal((await fetch(`${base}/health`)).status, 200);
    assert.equal((await fetch(`${base}/search?server=kuwo&q=a`)).status, 400);
    assert.equal((await fetch(`${base}/search?server=netease&q=`)).status, 400);
    assert.equal((await fetch(`${base}/search?server=netease&q=a&limit=99`)).status, 400);
    assert.equal((await fetch(`${base}/lyric?server=netease&id=../x`)).status, 400);
    assert.equal((await fetch(`${base}/url?server=netease&id=1`)).status, 404);
  } finally {
    server.close();
  }
});
