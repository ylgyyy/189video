# 天翼影视 Telegram Bot

基于 Telegram + Emby + TMDB 的影视资源频道管理机器人。

---

## 功能

| 命令 | 说明 |
|------|------|
| `/start` | 主菜单 |
| `/search` | 搜索频道资源 |
| `/hot` | 热更影视列表 |
| `/today` | 今日汇总 |
| `/yesterday` | 昨日汇总 |
| `/calendar` | 追更日历 (缺更 & Emby 入库进度) |
| `/submit` | 投稿 (智能解析天翼云盘链接) |
| `/up <ID> <集数>` | 快捷更新连载集数 |
| `/help` | 使用帮助 |
| `/cancel` | 取消当前操作 |

---

## 配置准备

部署前，先在 `docker-compose.yml` **同目录**下创建下面两个配置文件。

> ⚠️ **必须先创建好再启动**。尤其 `config.json` 是文件挂载，如果文件不存在，Docker 会把它误创建成目录，导致 `not a directory` 报错。

**1. 创建 `.env`（敏感密钥）**

`.env` 存放密钥，不进版本库、不进镜像，只留在服务器上。把下面三个占位值换成你的真实密钥，然后整段复制粘贴执行：

```bash
cat > .env << 'EOF'
BOT_TOKEN=你的Telegram_Bot_Token
TMDB_API_KEY=你的TMDB_API_Key
EMBY_API_KEY=你的Emby_API_Key
EOF
```

> 也可以 `nano .env` 手动编辑。

**2. 创建 `config.json`（非敏感配置）**

把下面模板里的空字符串 / `0` 换成真实值，然后整段复制粘贴执行：

```bash
cat > config.json << 'EOF'
{
  "super_admin": 0,
  "channel_id": "",
  "channel_username": "",
  "group_id": "",
  "channel_link": "",
  "group_link": "",
  "emby_api_url": "",
  "poll_interval": 180,
  "page_size": 10,
  "search_page_size": 5,
  "tmdb_cache_ttl": 600,
  "save_debounce_s": 2.0,
  "timeout_minutes": 1,
  "default_image": "https://picsum.photos/1280/720",
  "data_dir": ""
}
EOF
```

> 也可以 `nano config.json` 手动编辑。

| 字段 | 类型 | 说明 |
|------|------|------|
| `super_admin` | int | 超级管理员 Telegram ID |
| `channel_id` | string | 频道数字 ID (负数) |
| `channel_username` | string | 频道公开用户名 |
| `group_id` | string | 关联群组 ID |
| `channel_link` | string | 频道邀请链接 |
| `group_link` | string | 群组邀请链接 |
| `emby_api_url` | string | Emby API 地址 |
| `poll_interval` | int | Emby 轮询间隔 (秒) |
| `page_size` | int | 列表每页条数 |
| `search_page_size` | int | 搜索结果每页条数 |
| `tmdb_cache_ttl` | int | TMDB 缓存有效期 (秒) |
| `save_debounce_s` | float | 数据库写入防抖 (秒) |
| `timeout_minutes` | int | 投稿超时 (分钟) |
| `default_image` | string | 默认封面图 URL |
| `data_dir` | string | 数据目录 (本地留空; Docker 已内置 `/app/data/`) |

> 没写到的字段会使用内置默认值，可留空。

---

## Docker 运行

镜像由 GitHub Actions 自动构建并推送到 Docker Hub 公开仓库 `ylgy007/189video`，部署时直接拉取即可，**无需本地构建、无需登录**。

### 方式一：docker-compose（推荐）

创建 `docker-compose.yml`（仓库里已自带这份文件，可直接用）：

```yaml
services:
  video-bot:
    image: ylgy007/189video:latest
    container_name: 189video
    restart: unless-stopped

    environment:
      TZ: "Asia/Shanghai"
    env_file:
      - .env

    volumes:
      - ./data:/app/data
      - ./config.json:/app/config.json

    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "3"
```

然后启动：

```bash
docker compose up -d
```

> 常用命令：`docker compose restart`（重启）、`docker compose down`（停止）。

### 方式二：纯 docker 命令

```bash
docker run -d --name 189video \
  -e TZ=Asia/Shanghai \
  --env-file .env \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/config.json:/app/config.json \
  --restart unless-stopped \
  ylgy007/189video:latest
```

> Windows (cmd / PowerShell) 下把 `$(pwd)` 换成绝对路径，如 `D:/189video/data`。

---

## 数据持久化

| 文件 | 用途 |
|------|------|
| `data/msg_db.json` | 频道帖子数据库 |
| `data/admins.json` | 管理员白名单 |
| `data/emby_processed.json` | Emby 已处理记录 |

Docker 运行时通过 `-v ./data:/app/data` 挂载，容器重启数据不丢失。
