# WireGuard 中心机部署说明

## 目标与凭据边界

- 服务器：腾讯云 `VM-0-16-ubuntu`
- WireGuard 地址：`10.88.0.1`
- 公网地址：`175.178.242.232`
- 部署目录：`/home/ubuntu/rhythm-metadata-api`
- API 地址：`http://10.88.0.1:8010`

SSH 使用 PEM 私钥，不使用 Rhythm API token：

```bash
ssh -i ~/.ssh/zld_TecentCloud.pem -o IdentitiesOnly=yes ubuntu@10.88.0.1
```

WireGuard 未连接时，可将地址换成公网 `175.178.242.232`。Rhythm API 的 bearer token 是另一套凭据：服务器保存在部署目录 `.env`，Mac 备份在 `~/.config/rhythm/server.env`，两处均应保持 `0600`，不得提交到 Git 或写进归档正文。

## 私网调用

Mac 终端如果设置了 HTTP 代理，应对 WireGuard 地址使用 no-proxy：

```bash
set -a
source ~/.config/rhythm/server.env
set +a

curl --noproxy 10.88.0.1 \
  -H "Authorization: Bearer $RHYTHM_BOOTSTRAP_TOKEN" \
  http://10.88.0.1:8010/v2/works
```

健康检查不要求 token：

```bash
curl --noproxy 10.88.0.1 http://10.88.0.1:8010/healthz
```

## 运维

```bash
ssh -i ~/.ssh/zld_TecentCloud.pem -o IdentitiesOnly=yes ubuntu@10.88.0.1
cd /home/ubuntu/rhythm-metadata-api
sudo docker compose ps
sudo docker compose logs --tail 100 api
sudo docker compose up -d --build
```

服务仅绑定 `10.88.0.1:8010`，不会监听公网网卡。运行容器不使用 mihomo；只有 Docker 构建阶段经中心机 `127.0.0.1:7890` 下载依赖。

在线合唱上线前必须设置 `RHYTHM_CHORUS_COS_BUCKET`、`RHYTHM_COS_REGION`、
`RHYTHM_COS_SECRET_ID` 和 `RHYTHM_COS_SECRET_KEY`。桶保持私有，设备只取得限定对象键与短时
有效期的 HTTPS PUT/GET 签名；不得把 COS 密钥放入客户端。`api` 与 `public-api` 必须使用同一
组配置。未设置合唱桶时的本地上传路由仅供私网开发和自动测试，不能作为公网部署方式。

镜像包含 FFmpeg，用于把投稿标准化为 `48 kHz / mono / AAC-LC` 并生成内容寻址混音。
`RHYTHM_CHORUS_MIX_TIMEOUT_SECONDS` 默认 300 秒；上线时应同时限制容器 CPU/内存，并监控
`chorus track processing failed` 与 `chorus mix rendering failed` 日志。数据库迁移到
`issue78chorus` 后才可开放客户端入口。

持久数据位于部署目录 `data/`，包含旧 v1 SQLite、`rhythm-v2.sqlite3`、WAL 和内容寻址对象。备份时应同时备份整个 `data/`；SQLite 在线备份应优先使用 SQLite backup API，避免只复制主数据库而遗漏 WAL。

当前 `0.3.0` 已于 `2026-09-03` 部署，运行镜像对应源码提交 `98ea388`，镜像标签为 `rhythm-metadata-api-api:v0.3.0-98ea388`。线上保留 v1 SQLite 及其 WAL，v2 使用独立数据库并已执行 Alembic `25ff14940d0d`；尚未进行 v1 -> v2 业务数据导入。

本次切换前的回滚资源位于服务器 `backups/`：

- `rhythm-v1-before-v2-20260903-091235.sqlite3`：经 SQLite backup API 创建且完整性检查通过。
- `source-before-v2-20260903-091235.tar.gz`：升级前源码。
- `env-before-v2-20260903-091235`：升级前环境配置，权限 `0600`。
- `rhythm-metadata-api-api:pre-v2-20260903-091235`：升级前镜像标签。

部署后已验证容器重启持久化、两个 SQLite `integrity_check=ok`、v1 旧 API 抽样读取、v2 Bearer 认证与作品列表；服务只监听 `10.88.0.1:8010`。
