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

```bash
# 构建并启动
docker-compose up -d

# 查看日志
docker-compose logs -f

# 重启
docker-compose restart

# 停止
docker-compose down
```

纯 Docker 命令：

```bash
docker build -t 189video .
docker run -d --name 189video \
  -v $(pwd)/data:/app/data \
  -e TZ=Asia/Shanghai \
  189video
```

---

## 配置说明

配置分为两类，优先级：**环境变量 > config.json > 内置默认值**。

- **敏感密钥** (Bot Token / TMDB / Emby 密钥) → 放在 `.env` 文件，不进版本库、不进镜像。
- **非敏感参数** → 放在 `config.json`。

### 1. `.env` 敏感密钥

首次部署时复制示例文件并填入真实密钥：

```bash
cp .env.example .env
# 然后编辑 .env，填入 BOT_TOKEN / TMDB_API_KEY / EMBY_API_KEY
```

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

---

## 项目迁移

### 需要迁移的文件

| 优先级 | 文件 | 说明 |
|--------|------|------|
| ⭐⭐⭐ | `data/msg_db.json` | 频道帖子数据库，**最重要** |
| ⭐⭐⭐ | `data/admins.json` | 管理员白名单 |
| ⭐⭐ | `data/emby_processed.json` | Emby 已处理记录，丢了会自动重建 |
| ⭐ | `config.json` | 配置文件，新服务器可能要改参数 |

不需要迁移的：`video.py`、`Dockerfile` 等代码文件直接从仓库拉取即可。

### 迁移步骤

**旧服务器上打包：**

```bash
cd /home/telegram/189video

# 停掉容器
docker compose down

# 打包 data 目录 (核心数据)
tar -czf backup.tar.gz data/

# 下载到本地 (在本地电脑执行)
scp root@旧服务器IP:/home/telegram/189video/backup.tar.gz .
```

**新服务器上恢复：**

```bash
# 1. 克隆代码或将项目文件上传到新服务器
#    需要这些文件: video.py, config.json, requirements.txt,
#                 Dockerfile, docker-compose.yml, .dockerignore

# 2. 上传备份包
scp backup.tar.gz root@新服务器IP:/home/telegram/189video/

# 3. 在新服务器上恢复
cd /home/telegram/189video
tar -xzf backup.tar.gz    # 解压出 data/ 目录

# 4. 启动
docker compose up -d
docker compose logs -f    # 确认运行正常
```

### 定时备份 (crontab)

在服务器上设置每天凌晨自动备份：

```bash
# 编辑定时任务
crontab -e

# 添加这一行 (每天凌晨 3 点备份，保留最近 7 份)
0 3 * * * tar -czf /home/telegram/189video/backups/backup_$(date +\%Y\%m\%d).tar.gz -C /home/telegram/189video data/ && find /home/telegram/189video/backups -mtime +7 -delete

# 先创建备份目录
mkdir -p /home/telegram/189video/backups
```

---

## 目录结构

```
天翼影视/
├── video.py                  # 主程序
├── config.json               # 非敏感配置
├── .env                      # 敏感密钥 (不提交)
├── .env.example              # 密钥模板
├── requirements.txt          # Python 依赖
├── Dockerfile                # Docker 镜像
├── docker-compose.yml        # Docker 编排
├── .dockerignore             # 构建排除
├── README.md                 # 本文件
├── data/                     # 持久化数据 (运行时)
└── 历史版本/
    └── video_v1.0_original.py
```
