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

## 快速开始

### 1. 本地运行

```bash
# 安装依赖
pip install -r requirements.txt

# 直接运行
python video.py
```

### 2. Docker 运行 (推荐)

镜像由 GitHub Actions 自动构建并推送到 Docker Hub 私有仓库 `ylgy007/189video`，部署时直接拉取即可，**无需本地构建**。

**部署步骤：**

```bash
# 1. 准备配置文件 (放在项目目录下)
#    - .env          填入真实密钥 (BOT_TOKEN / TMDB_API_KEY / EMBY_API_KEY)
#    - config.json   填入真实配置 (频道 ID / 管理员 ID / Emby 地址等)

# 2. 登录 Docker Hub (私有仓库必须先登录; 密码用 Read & Write 权限的 Access Token)
docker login -u ylgy007

# 3. 拉取镜像并启动
docker compose pull
docker compose up -d

# 4. 查看日志
docker compose logs -f

# 常用命令
docker compose restart    # 重启
docker compose down       # 停止
```

> ⚠️ **重要**：`config.json` 和 `.env` 必须先**创建好**再执行 `docker compose up`。
> 尤其 `config.json` 是**文件挂载**，如果文件不存在，Docker 会误把它创建成目录，导致 `not a directory` 报错。两个文件的模板见下方「配置说明」。

`docker-compose.yml` 已配好：

| 配置项 | 作用 |
|--------|------|
| `image: ylgy007/189video:latest` | 直接拉取远程镜像，不本地构建 |
| `./data:/app/data` | 数据持久化 |
| `./config.json:/app/config.json` | 用服务器上的真实配置覆盖镜像内的占位配置 |

纯 Docker 命令 (不用 compose 时)：

```bash
docker login -u ylgy007
docker pull ylgy007/189video:latest
docker run -d --name 189video \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/config.json:/app/config.json \
  --env-file .env \
  -e TZ=Asia/Shanghai \
  ylgy007/189video:latest
```

---

## 配置说明

配置分为两类，优先级：**环境变量 > config.json > 内置默认值**。

- **敏感密钥** (Bot Token / TMDB / Emby 密钥) → 放在 `.env` 文件，不进版本库、不进镜像。
- **非敏感参数** → 放在 `config.json`。

### 1. `.env` 敏感密钥

在项目目录下创建 `.env`，填入你的真实密钥（下面是模板，均为占位符）：

```bash
# .env
BOT_TOKEN=你的Telegram_Bot_Token
TMDB_API_KEY=你的TMDB_API_Key
EMBY_API_KEY=你的Emby_API_Key
```

> 也可以直接 `cp .env.example .env` 再编辑。

`docker-compose up -d` 会自动读取同目录下的 `.env` 完成变量替换；本地 `python video.py` 也会通过 `python-dotenv` 自动加载。

### 2. config.json 字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `super_admin` | int | 超级管理员 Telegram ID |
| `channel_id` | string | 频道 ID (负数) |
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
| `data_dir` | string | 数据目录 (Docker 用 `/app/data/`) |

完整模板（复制后把空字符串/0 改成真实值即可）：

```json
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
  "data_dir": "/app/data/"
}
```

### 生产环境安全建议

密钥只存在于服务器上的 `.env` 文件，请勿提交到公开仓库或分享给他人。`.dockerignore` 已排除 `.env`，不会被构建进镜像。

---

## 数据持久化

| 文件 | 用途 |
|------|------|
| `data/msg_db.json` | 频道帖子数据库 |
| `data/admins.json` | 管理员白名单 |
| `data/emby_processed.json` | Emby 已处理记录 |

Docker 运行时通过 `-v ./data:/app/data` 挂载，容器重启数据不丢失。
