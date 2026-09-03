# Rhythm Metadata API

Rhythm 私有作品库后端。v2 以 `Work → Arrangement → Score/Rendition → Asset` 为主链路，服务端同时负责受保护的音频 Range 传输；客户端不再扫描或播放任意本地媒体，也不依赖 Navidrome/Jellyfin。

## 当前能力

- Work、alias、Contributor/Credit、Arrangement 与 Part。
- 不可变 ScoreRevision；每个修订恰好一个主 MusicXML，可附 MIDI/扫描件/PDF。
- Rendition 与 master/stream/mix/stem/midi 文件关系；只有 Rendition 是可播放业务身份。
- Asset 内容、来源与存储位置分离；SHA-256 去重、两阶段流式上传、MusicXML/MXL/MIDI/图片/音频格式检查。
- Bearer 鉴权、`Idempotency-Key`、`If-Match`/ETag、RFC 7807 风格错误、Bundle 条件缓存和 changes 游标。
- 本机内容寻址对象存储及支持 `Range` 的受保护 Asset 下载。
- SQLAlchemy 2、Alembic、SQLite WAL/外键/事务。

`/v1/tracks/*` 暂时保留，只用于旧客户端迁移，不再扩展。

## 本地运行

要求 Python 3.12–3.14。

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
uvicorn rhythm_metadata_api.main:app --reload
```

默认入口：

- Swagger UI：`http://127.0.0.1:8000/docs`
- OpenAPI：`http://127.0.0.1:8000/openapi.json`
- 健康检查：`GET /healthz`

除健康检查外，请求必须携带：

```http
Authorization: Bearer <RHYTHM_BOOTSTRAP_TOKEN>
```

创建类请求还要带唯一 `Idempotency-Key`；修改聚合或新增不可变修订时带服务端返回的 `If-Match: "rev-N"`。

## v2 核心接口

```text
POST  /v2/works/resolve
POST  /v2/works
GET   /v2/works
GET   /v2/works/{id}
PATCH /v2/works/{id}
GET   /v2/works/{id}/bundle

POST  /v2/works/{id}/arrangements
GET   /v2/arrangements/{id}
PATCH /v2/arrangements/{id}
POST  /v2/arrangements/{id}/parts

POST  /v2/uploads
PUT   /v2/uploads/{id}/content
POST  /v2/uploads/{id}/complete
GET   /v2/assets/{id}
GET   /v2/assets/{id}/content

POST  /v2/arrangements/{id}/scores
PATCH /v2/scores/{id}
POST  /v2/scores/{id}/revisions

POST  /v2/arrangements/{id}/renditions
PATCH /v2/renditions/{id}
POST  /v2/renditions/{id}/assets
GET   /v2/renditions/{id}/playback

GET   /v2/sync/changes?after=<sequence>
```

典型文件流程：客户端先计算 hash 和大小，`POST /v2/uploads`；若不是 `reused`，流式 `PUT` 字节并 `POST complete`；最后把返回的 Asset ID 关联到 ScoreRevision 或 Rendition。

## Docker

`compose.yaml` 默认只绑定 WireGuard 中心机 `10.88.0.1:8010`，数据库和对象目录持久化在 `./data`：

```bash
cp .env.example .env
# 设置随机的 RHYTHM_BOOTSTRAP_TOKEN
docker compose up -d --build
curl http://10.88.0.1:8010/healthz
```

启动时自动执行 Alembic upgrade。运维与备份见 `docs/deployment.md`。

## 验证

```bash
pytest -q
ruff check src tests
```

当前自动化覆盖 v1 回归，以及 v2 鉴权、幂等重放/冲突、精确解析、文件校验与复用、不可变谱面修订、过期 ETag、Rendition 播放选择、Range、Bundle 304 和增量事件。

## 尚未实现

- Release、Lyrics、Artwork 与 metadata suggestions。
- 删除墓碑、对象 GC、转码/预览 worker、COS adapter。
- 旧 v1 Demo 数据一次性导入与线上 v2 部署。
