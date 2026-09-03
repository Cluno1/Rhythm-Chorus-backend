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

当前本机 v2 代码尚未部署到该中心机。正式升级前必须先备份线上 v1 数据和对象目录，再构建镜像；容器启动会自动执行 Alembic upgrade。不要把“本机测试通过”写成“线上已切换”。
