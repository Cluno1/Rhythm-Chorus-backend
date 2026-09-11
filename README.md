# Rhythm Metadata API

Rhythm 私有作品库后端。v2 以 `Work → Arrangement → Score/Rendition → Asset` 为主链路，服务端同时负责受保护的音频 Range 传输；客户端不再扫描或播放任意本地媒体，也不依赖 Navidrome/Jellyfin。

## 当前能力

- Work、alias、Contributor/Credit、Arrangement 与 Part。
- 不可变 ScoreRevision；每个修订恰好一个主 MusicXML，可附 MIDI/扫描件/PDF。
- Rendition 与 master/stream/mix/stem/midi 文件关系；只有 Rendition 是可播放业务身份。
- Work、Score、Rendition 的多语言歌词；默认语言正文兼容旧客户端，其他语言以结构化数组保存。
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
GET   /v2/assets/{id}/delivery
GET   /v2/assets/{id}/content

POST  /v2/lyric-source-documents
GET   /v2/lyric-source-documents/{id}
POST  /v2/lyric-source-documents/{id}/pages
POST  /v2/works/{id}/lyric-source-pages
POST  /v2/scores/{id}/lyric-source-pages
POST  /v2/renditions/{id}/lyric-source-pages

POST  /v2/arrangements/{id}/scores
PATCH /v2/scores/{id}
POST  /v2/scores/{id}/revisions

POST  /v2/arrangements/{id}/renditions
PATCH /v2/renditions/{id}
PUT   /v2/renditions/{id}/lyrics/{language}
POST  /v2/renditions/{id}/assets
GET   /v2/renditions/{id}/playback
GET   /v2/renditions/{id}/effective-lyric-sources

GET   /v2/sync/changes?after=<sequence>
```

典型文件流程：客户端先计算 hash 和大小，`POST /v2/uploads`；若不是 `reused`，流式 `PUT` 字节并 `POST complete`；最后把返回的 Asset ID 关联到 ScoreRevision 或 Rendition。

### 多语言歌词

Work、Score 和 Rendition 共用以下向后兼容的字段结构：

```json
{
  "lyrics": "Amazing grace...",
  "lyrics_language": "en",
  "lyrics_translations": [
    {
      "language": "zh-Hans",
      "lyrics": "奇异恩典……"
    }
  ]
}
```

- `lyrics` 是默认主语言歌词，旧客户端可继续只读取此字符串。
- `lyrics_language` 是默认歌词的 BCP 47 风格语言标签；支持 `en`、`en-US`、`zh-Hans`、`zh-Hant` 等形式。
- `lyrics_translations` 是其他语言数组；语言不可重复，也不可再次出现默认语言。
- 新建 Score/Rendition 时如省略 `lyrics_language`，默认继承所属 Work 的 `language`；仍无法确定时使用 `und`。
- `/v2/library/songs` 按语言执行 Rendition → preferred Score → Work 回退：高优先级来源只覆盖它实际提供的语言，其他语言继续从下级来源补齐。
- Android 只通过 `PUT /v2/renditions/{id}/lyrics/{language}` 覆盖某个 Rendition 的单一语言；请求只接受 `lyrics` 与 `format`，必须携带设备签名、实际正文 SHA-256、`If-Match` 和 `Idempotency-Key`。其他语言以及 Score/Work 底稿保持不变。
- Rendition 用 `lyrics_formats` 记录设备写入版本的 `plain`、`lrc`、`enhanced_lrc`、`ttml` 或 `word_by_word_json`；Library 响应同时返回 `rendition_revision` 和有效语言的 `lyrics_formats`。

### 歌词来源整页图

Issue 52 使用 `v2_lyric_source_documents`、`v2_lyric_source_pages` 和
`v2_lyric_source_links` 保存来源文档、完整物理页与 Work/Score/Rendition 关联。
同一 PDF 页只有一个图片 Asset；页面含多首歌时多个 Work 共享它，不裁剪也不复制
COS 对象。业务响应通过 `lyrics_source_images` 返回稳定 Asset ID，客户端显示时再调用
Asset delivery，不能持久化短期 COS 签名 URL。

IHOP Songbook 导入分为可审计的两步，均默认不修改生产环境：

```bash
python scripts/build_lyric_source_plan.py \
  --pdf 2024-IHOP-Songbook.pdf \
  --manifest extracted-songs/manifest.jsonl \
  --work-matches Work歌词导入匹配结果.tsv

# 审核 metadata-only 结果后，再传 --render-dir 和 --output 生成完整原页及锁定 hash 的计划。
# 正式执行还要求已迁移数据库、COS 凭据和双重显式确认：
python scripts/import_lyric_source_plan.py \
  --plan reviewed-plan.json --database rhythm-v2.sqlite3 \
  --bucket '<bucket-appid>' --apply --confirm APPLY_LYRIC_SOURCES
```

执行器先校验所有本地字节/hash 和 Work ID，再上传并用 COS 对象大小与
`x-cos-meta-sha256` 回验，最后以确定性 ID 在单个数据库事务内登记 Asset、页面、关联和
change event；重复运行不会新增重复记录。

## Docker

`compose.yaml` 默认只绑定 WireGuard 中心机 `10.88.0.1:8010`，数据库和对象目录持久化在 `./data`：

```bash
cp .env.example .env
# 设置随机的 RHYTHM_BOOTSTRAP_TOKEN
docker compose up -d --build
curl http://10.88.0.1:8010/healthz
```

启动时自动执行 Alembic upgrade。运维与备份见 `docs/deployment.md`。

当前 `0.3.0` 已于 `2026-09-03` 部署到该中心机，v1 数据保留在 `rhythm.sqlite3`，v2 独立使用 `rhythm-v2.sqlite3`。旧 v1 业务数据尚未迁移；v2 已导入下述 COS 典型测试样本。

### 公网受限 Catalog 网关

Issue 14 增加独立进程 `public-api`。它与内网管理 API 共用 Catalog 数据，默认只允许 Android 当前需要的 GET/HEAD 路由；Issue 58 仅额外开放上面的单语言 Rendition 歌词 PUT。上传、通用修改和发布等 handler 在进入路由前统一返回 404。公网进程关闭 OpenAPI 与文档页面。

启动前在 `.env` 设置至少 32 字节的 `RHYTHM_PUBLIC_TOKEN_SECRET`，并设置 scrypt 格式的管理员密码哈希：

```bash
python -c 'from rhythm_metadata_api.application.device_auth import hash_admin_password; print(hash_admin_password(input("Admin password: ")))'
docker compose up -d api
docker compose --profile public up -d --build public-api
```

管理员密码只在获取 5 分钟管理令牌时提交，不保存到 Android。管理员签发一次性邀请码后，客户端用 Android Keystore 内不可导出的 P-256 私钥登记；后续每个 Catalog 请求都需要短期 token、服务端一次性 nonce、时间戳和请求签名。歌词 PUT 还会对收到的实际 JSON 字节重新计算 SHA-256，并要求 token 具有 `catalog:lyrics:write` scope。设备登记绑定 Sonorus applicationId 与 APK 签名证书；一个用户可为 Debug 和 Release 各保留一台 active 设备，但同一 applicationId 仍只能有一台。

`issue15updateidentity` 迁移会把旧登记标为 legacy 身份；升级网关后，既有 Android 客户端需要由管理员重新签发邀请码并登记一次。新的 enrollment V2 签名同时覆盖 applicationId 和证书指纹，避免这两个字段在 HTTP 传输中被替换。

### Sonorus 自托管更新

`public-api` 只读挂载 `./updates:/updates:ro`，开放设备认证的 `GET /v2/app-updates/latest` 以及 `GET/HEAD /v2/app-updates/files/{versionCode}/{fileName}`。服务器根据登记的 applicationId 与冻结证书映射 Debug/Stable，不信任客户端单独提交的 channel。更新目录之外的路径、未列入版本 manifest 的 APK 和公网写请求全部拒绝。

普通浏览器无需设备登记即可通过下列固定地址下载各渠道最新 APK：

- `GET/HEAD http://175.178.242.232:8010/v2/app-updates/debug/latest.apk`
- `GET/HEAD http://175.178.242.232:8010/v2/app-updates/stable/latest.apk`

这两个公开路由只接受信任的 Debug/Stable 固定值，每次都从对应的 `latest.json` 选择 universal（优先）或 arm64-v8a APK，并复用 manifest 身份、不可变版本副本、文件大小和 SHA-256 校验。固定地址必须重新验证缓存，所以后续发布不需要更换对外链接。

配置 `RHYTHM_SONORUS_UPDATES_COS_BUCKET` 后，声明支持 COS 交付的新客户端在认证 APK
GET 上会收到短时签名地址的 `307 Temporary Redirect`，APK 字节由私有 COS 直接交付；
旧客户端仍由网关返回本地文件。HEAD 始终由网关返回本地清单核验后的大小和摘要。
COS 对象使用无扩展名的内容寻址键
`{channel}/releases/{versionCode}/{sha256}`，以适配腾讯云新桶默认域名禁止 APK 类型公网下载
的限制；客户端最终仍以 manifest 中的 `.apk` 文件名落盘并校验。公开浏览器固定地址暂时
继续由网关直出 `.apk`，待配置 COS 自定义域名后再迁移，因为新桶默认域名会拒绝浏览器
下载 APK。所有 COS 对象必须在切换 `latest.json` 前上传并完整回读校验。

部署前必须配置 `RHYTHM_SONORUS_DEBUG_CERTIFICATE_SHA256` 与 `RHYTHM_SONORUS_STABLE_CERTIFICATE_SHA256`。发布流程通过管理通道把不可变版本目录写入主机 `updates/`，最后原子替换对应的 `latest.json`；公网容器对该目录没有写权限。

`public-api` 固定监听腾讯云内网地址 `10.1.0.16:8010`（公网映射为 `175.178.242.232:8010`），而原有管理 API 继续只监听 WireGuard 地址 `10.88.0.1:8010`。确认容器健康并完成签名联调之前，不要开放安全组 8010。

## COS 典型样本导入

`scripts/import_cos_samples.py` 从 GMUSIC Mongo 索引和 COS 导入一组固定的小样本，用来验证多谱、修订、扫描附件、MIDI Rendition、Asset 去重和 Range 播放。当前样本为 `321`、`348`、`528`、`test1`、`110`。

脚本要求环境中提供 `RHYTHM_BOOTSTRAP_TOKEN`、`COS_*` 与 `MONGO_*` 配置，并使用已安装 `pymongo`、`cos-python-sdk-v5` 的 ingestion Python：

```bash
python scripts/import_cos_samples.py --dry-run
python scripts/import_cos_samples.py
```

导入以 `gmusic` alias、Score label、修订链和 SHA-256 判重，可以重复运行。GMUSIC MusicXML 的标准 Recordare 外部 DTD 会在上传前移除，以满足后端的 XXE 防护；Asset source ref 会同时记录原 COS key、原始 SHA-256、原始大小和转换版本，COS 原文件不会被修改。

## 验证

```bash
pytest -q
ruff check src tests scripts/build_lyric_source_plan.py scripts/import_lyric_source_plan.py
```

当前自动化覆盖 v1 回归，以及 v2 鉴权、幂等重放/冲突、精确解析、多语言歌词与旧数据迁移、文件校验与复用、不可变谱面修订、过期 ETag、Rendition 播放选择、Range、Bundle 304 和增量事件。

## 尚未实现

- Metadata suggestions。
- 删除墓碑、对象 GC、转码/预览 worker、COS adapter。
- 旧 v1 Demo 数据一次性导入与 Android v2 端到端联调。
