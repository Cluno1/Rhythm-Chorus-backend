# lyrics_provider（issue 127）

Rhythm-Chorus-backend 的内网歌词来源，封装 [metowolf/Meting](https://github.com/metowolf/Meting)（`@meting/core`，MIT）。

- 只提供搜索和取歌词，不提供音频地址，也不接受 Cookie。
- 只监听 `127.0.0.1:8787`。JDC 上后端容器使用主机网络，通过 `RHYTHM_EXTERNAL_LYRICS_PROVIDER_URL=http://127.0.0.1:8787` 访问。
- 平台接口变化时，优先升级 `@meting/core` 的版本号（`package.json` 和锁文件同时更新）。

| 接口 | 说明 |
| --- | --- |
| `GET /health` | 存活检查 |
| `GET /search?server=netease\|tencent\|kugou&q=&type=title\|lyrics&limit=1..50` | 返回 `items[]`：`source`、`id`、`title`、`artists`、`album`、`duration_ms`、`snippet`。只有网易云支持按歌词全文搜索（`type=lyrics`），并会返回命中的那一行 |
| `GET /lyric?server=&id=` | 返回 `{lyric, tlyric}`，均为 LRC 原文 |

上游失败时返回 502，后端会跳过这个来源。

```bash
npm ci && npm test          # 离线单元测试
node server.mjs             # 本地运行
```
