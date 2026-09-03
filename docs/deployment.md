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

持久数据位于部署目录 `data/`，包含旧 v1 SQLite、`rhythm-v2.sqlite3`、WAL 和内容寻址对象。备份时应同时备份整个 `data/`；SQLite 在线备份应优先使用 SQLite backup API，避免只复制主数据库而遗漏 WAL。

当前 `0.3.0` 已于 `2026-09-03` 部署，运行镜像对应源码提交 `98ea388`，镜像标签为 `rhythm-metadata-api-api:v0.3.0-98ea388`。线上保留 v1 SQLite 及其 WAL，v2 使用独立数据库并已执行 Alembic `25ff14940d0d`；尚未进行 v1 -> v2 业务数据导入。

本次切换前的回滚资源位于服务器 `backups/`：

- `rhythm-v1-before-v2-20260903-091235.sqlite3`：经 SQLite backup API 创建且完整性检查通过。
- `source-before-v2-20260903-091235.tar.gz`：升级前源码。
- `env-before-v2-20260903-091235`：升级前环境配置，权限 `0600`。
- `rhythm-metadata-api-api:pre-v2-20260903-091235`：升级前镜像标签。

部署后已验证容器重启持久化、两个 SQLite `integrity_check=ok`、v1 旧 API 抽样读取、v2 Bearer 认证与作品列表；服务只监听 `10.88.0.1:8010`。
