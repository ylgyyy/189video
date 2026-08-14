# -*- coding: utf-8 -*-
"""
天翼影视 Telegram Bot - 优化版 v2.1
=====================================
优化点:
  1. 敏感配置抽离至 config.json (环境变量可覆盖)
  2. 线程安全: msg_db / processed_ids 加锁
  3. HTTP 连接复用: requests.Session
  4. TMDB 内存缓存 (TTL 600s)
  5. msg_db 写入防抖 (2s 批量落盘)
  6. msg_db 索引字典 (O(1) 按 msg_id 查找)
  7. 分页 / 频道顶贴 等重复逻辑抽取为公共函数
  8. 静默 except:pass 替换为 logging 日志
  9. 常量集中 & 类型注解
"""

import html
import telebot
import requests
import urllib.parse
import json
import os
import random
import re
import traceback
import time
import threading
import logging
import hashlib
import uuid as uuid_lib
from datetime import datetime, timedelta, date
from typing import Optional, List, Dict, Any, Tuple
from xml.etree import ElementTree as ET
from curl_cffi import requests as curl_requests  # 天翼云盘反爬 TLS 指纹模拟

# 加载 .env (敏感密钥), 未安装 python-dotenv 或文件不存在时静默跳过
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ===================== 日志 =====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("video_bot")

# ===================== 配置加载 (config.json → 环境变量覆盖) =====================
CONFIG_FILE = "config.json"

def _load_config() -> dict:
    """加载 config.json，不存在则用空字典兜底"""
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    log.warning("⚠️ config.json 未找到，将依赖环境变量或内置默认值")
    return {}

_cfg = _load_config()

# 敏感字段: 环境变量优先 (可覆盖 config.json 中的值)
BOT_TOKEN    = os.environ.get("BOT_TOKEN",    _cfg.get("bot_token",    ""))
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", _cfg.get("tmdb_api_key", ""))
EMBY_API_KEY = os.environ.get("EMBY_API_KEY", _cfg.get("emby_api_key", ""))

# 非敏感字段: config.json 为主, 环境变量为辅
SUPER_ADMIN     = int(os.environ.get("SUPER_ADMIN", _cfg.get("super_admin", 0)))
CHANNEL_ID      = os.environ.get("CHANNEL_ID",      _cfg.get("channel_id",      ""))
CHANNEL_USERNAME = os.environ.get("CHANNEL_USERNAME", _cfg.get("channel_username", ""))
GROUP_ID        = os.environ.get("GROUP_ID",        _cfg.get("group_id",        ""))

# 私密频道链接: https://t.me/c/{数字ID}/{msg_id}
_CHANNEL_NUMERIC = CHANNEL_ID.replace("-100", "") if CHANNEL_ID.startswith("-100") else CHANNEL_ID
CHANNEL_LINK     = f"https://t.me/c/{_CHANNEL_NUMERIC}"

# 运行参数 (带默认值兜底)
DEFAULT_IMAGE    = _cfg.get("default_image",    "https://picsum.photos/1280/720")
EMBY_API_URL     = _cfg.get("emby_api_url",     "https://emby.trrr.top/emby/Items")
POLL_INTERVAL    = _cfg.get("poll_interval",    180)
PAGE_SIZE        = _cfg.get("page_size",        10)
SEARCH_PAGE_SIZE = _cfg.get("search_page_size", 5)
TMDB_CACHE_TTL   = _cfg.get("tmdb_cache_ttl",   600)
SAVE_DEBOUNCE_S  = _cfg.get("save_debounce_s",  2.0)
TIMEOUT_MINUTES  = _cfg.get("timeout_minutes",  1)

# 数据目录 (Docker: /app/data/ | 本地: 当前目录)
DATA_DIR    = os.environ.get("DATA_DIR", _cfg.get("data_dir", ""))
ADMIN_FILE  = os.path.join(DATA_DIR, "admins.json")

# ===================== 线程安全锁 =====================
db_lock        = threading.Lock()   # 保护 msg_db / msg_db_index
processed_lock = threading.Lock()   # 保护 processed_ids
cache_lock     = threading.Lock()   # 保护 tmdb_cache / user_data / 分页缓存

# ===================== 持久化数据 =====================
def _json_load(path: str, default: Any) -> Any:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default

def _json_save(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

# --- 管理员 ---
admins_raw = _json_load(ADMIN_FILE, [])
admins: List[Dict] = []
for a in admins_raw:
    if isinstance(a, dict):
        admins.append(a)
    else:
        admins.append({"id": a, "name": ""})
def save_admins() -> None:
    _json_save(ADMIN_FILE, admins)

# --- 消息数据库 ---
DB_FILE     = os.path.join(DATA_DIR, "msg_db.json")
msg_db: List[Dict[str, Any]] = _json_load(DB_FILE, [])
msg_db_index: Dict[int, Dict[str, Any]] = {}  # msg_id(int) -> item

def _rebuild_index() -> None:
    """依据 msg_db 全量重建 msg_db_index"""
    global msg_db_index
    msg_db_index.clear()
    for item in msg_db:
        mid = item.get("msg_id")
        if mid is not None:
            msg_db_index[int(mid)] = item

_rebuild_index()

_save_timer: Optional[threading.Timer] = None

def save_msg() -> None:
    """防抖写入: 2 秒内连续调用只落盘一次"""
    global _save_timer
    if _save_timer is not None:
        _save_timer.cancel()
    _save_timer = threading.Timer(SAVE_DEBOUNCE_S, _do_save_msg)
    _save_timer.start()

def _do_save_msg() -> None:
    """实际写盘"""
    with db_lock:
        _json_save(DB_FILE, msg_db)

def save_msg_sync() -> None:
    """立即同步写盘 (极少数场景用)"""
    global _save_timer
    if _save_timer is not None:
        _save_timer.cancel()
        _save_timer = None
    with db_lock:
        _json_save(DB_FILE, msg_db)

# --- Emby 已处理记录 ---
PROCESSED_FILE = os.path.join(DATA_DIR, "emby_processed.json")
processed_ids: List[str] = _json_load(PROCESSED_FILE, [])
def save_processed() -> None:
    with processed_lock:
        _json_save(PROCESSED_FILE, processed_ids)

# ===================== HTTP 会话复用 =====================
http = requests.Session()
http.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
})

# ===================== TMDB 内存缓存 =====================
tmdb_cache: Dict[str, Tuple[float, Any]] = {}  # key -> (timestamp, data)

def tmdb_cached_get(url: str, timeout: int = 10) -> Optional[dict]:
    """带缓存的 TMDB GET 请求"""
    now = time.time()
    with cache_lock:
        if url in tmdb_cache:
            ts, data = tmdb_cache[url]
            if now - ts < TMDB_CACHE_TTL:
                return data
    try:
        resp = http.get(url, timeout=timeout)
        if resp.status_code == 200:
            data = resp.json()
            with cache_lock:
                tmdb_cache[url] = (now, data)
                # 缓存上限 200 条
                if len(tmdb_cache) > 200:
                    oldest = min(tmdb_cache, key=lambda k: tmdb_cache[k][0])
                    del tmdb_cache[oldest]
            return data
    except Exception as e:
        log.warning("TMDB 请求失败: %s", e)
    return None

# ===================== 机器人实例 =====================
bot = telebot.TeleBot(BOT_TOKEN, threaded=False)

# ===================== 用户会话 & 缓存 =====================
user_data: Dict[int, Dict] = {}
search_cache: Dict[int, Dict] = {}
today_cache: Dict[int, Dict] = {}
yesterday_cache: Dict[int, Dict] = {}
popular_cache: Dict[int, Dict] = {}
hot_cache: Dict[int, Dict] = {}
retry_cache: Dict[int, list] = {}
record_mgmt_cache: Dict[int, Dict] = {}
user_timers: Dict[int, threading.Timer] = {}

# ===================== 辅助工具函数 =====================

def is_admin(user_id: int) -> bool:
    if user_id == SUPER_ADMIN:
        return True
    for a in admins:
        aid = a.get("id", a) if isinstance(a, dict) else a
        if aid == user_id:
            return True
    return False

def admin_only(func):
    """装饰器: 仅管理员可调用"""
    def wrapper(message):
        if not is_admin(message.from_user.id):
            bot.reply_to(message, "❌ 仅管理员可使用！")
            return
        return func(message)
    return wrapper

def make_channel_markup(link: str, access_code: str = "") -> telebot.types.InlineKeyboardMarkup:
    """构造频道帖子的标准按钮: 链接直达(含访问码) + 订阅频道"""
    mk = telebot.types.InlineKeyboardMarkup(row_width=2)
    mk.add(
        telebot.types.InlineKeyboardButton("☁️ 链接直达", url=link),
        telebot.types.InlineKeyboardButton("🔍 资源搜索", url=f"https://t.me/xizivideo_bot"),
    )
    return mk

def is_group(chat) -> bool:
    """判断是否为群聊"""
    return str(chat.type) in ("group", "supergroup")

def make_main_keyboard(uid: int, chat=None) -> telebot.types.InlineKeyboardMarkup:
    """构造主菜单内联键盘 (群组简化版，私聊完整版)"""
    mk = telebot.types.InlineKeyboardMarkup(row_width=2)
    mk.add(
        telebot.types.InlineKeyboardButton("🔍 搜索资源", callback_data="menu_search"),
        telebot.types.InlineKeyboardButton("🔥 热门影视", callback_data="menu_popular"),
    )
    mk.add(
        telebot.types.InlineKeyboardButton("☀️ 今日汇总", callback_data="menu_today"),
        telebot.types.InlineKeyboardButton("🌙 昨日汇总", callback_data="menu_yesterday"),
    )
    # 私聊显示完整菜单
    if chat is None or not is_group(chat):
        mk.add(
            telebot.types.InlineKeyboardButton("📅 追更日历", callback_data="menu_calendar"),
            telebot.types.InlineKeyboardButton("📤 我要投稿", callback_data="menu_submit"),
        )
        mk.add(telebot.types.InlineKeyboardButton("ℹ️ 使用帮助", callback_data="menu_help"))
        if is_admin(uid):
            mk.add(telebot.types.InlineKeyboardButton("📋 记录管理", callback_data="menu_record"))
        if uid == SUPER_ADMIN:
            mk.add(telebot.types.InlineKeyboardButton("⚙️ 管理面板", callback_data="menu_admin"))
    return mk

def reset_user_state(uid: int) -> None:
    """清除用户所有会话状态"""
    cancel_submit_timer(uid)
    user_data.pop(uid, None)
    search_cache.pop(uid, None)
    today_cache.pop(uid, None)
    yesterday_cache.pop(uid, None)
    popular_cache.pop(uid, None)
    hot_cache.pop(uid, None)
    retry_cache.pop(uid, None)
    record_mgmt_cache.pop(uid, None)

def cancel_submit_timer(uid: int) -> None:
    """取消用户的投稿超时定时器"""
    if uid in user_timers:
        user_timers[uid].cancel()
        del user_timers[uid]

def on_submit_timeout(uid: int) -> None:
    """投稿超时回调"""
    data = user_data.get(uid)
    if data and data.get("step") == "link":
        user_data.pop(uid, None)
        try:
            bot.send_message(
                uid,
                f"⏳ 您已超过 {TIMEOUT_MINUTES} 分钟未操作，系统已自动回收投稿流。",
            )
        except Exception as e:
            log.warning("超时通知发送失败 uid=%s: %s", uid, e)

def start_submit_timer(uid: int) -> None:
    """启动投稿超时倒计时"""
    cancel_submit_timer(uid)
    t = threading.Timer(TIMEOUT_MINUTES * 60.0, on_submit_timeout, args=[uid])
    t.start()
    user_timers[uid] = t

# ---------- 频道帖子更新 (复制 + 删除旧帖 + 更新数据库) ----------
def repost_channel_update(item: Dict, new_caption: str, new_version: str) -> bool:
    """
    复制频道帖子并更新标题, 然后删除旧帖, 更新内存数据库。
    返回是否成功。
    """
    old_msg_id = int(item.get("msg_id", 0))
    if not old_msg_id:
        return False
    try:
        copied = bot.copy_message(
            chat_id=CHANNEL_ID,
            from_chat_id=CHANNEL_ID,
            message_id=old_msg_id,
            caption=new_caption,
            parse_mode="HTML",
            reply_markup=make_channel_markup(item.get("link", ""), item.get("access_code", "")),
        )
        new_msg_id = copied.message_id

        # 尝试删除旧帖 (失败不阻塞)
        try:
            bot.delete_message(chat_id=CHANNEL_ID, message_id=old_msg_id)
        except Exception:
            log.debug("删除旧帖失败 msg_id=%s", old_msg_id)

        # 更新数据库
        with db_lock:
            item["msg_id"]     = new_msg_id
            item["version_desc"] = new_version
            item["caption"]    = new_caption
            item["date"]       = date.today().strftime("%Y-%m-%d")

            # 更新索引
            msg_db_index.pop(old_msg_id, None)
            msg_db_index[new_msg_id] = item
        save_msg()
        log.info("频道顶贴成功: %s -> %s (新 msg_id=%s)", item.get("title"), new_version, new_msg_id)
        return True
    except Exception as e:
        log.error("频道顶贴失败: %s", e)
        return False

# ---------- 通用分页渲染 ----------
def render_paginated(
    uid: int,
    cache: Dict,
    cache_key: str,
    title_prefix: str,
    item_formatter,          # callable(item, index) -> str
    page_cb_prefix: str,     # 翻页 callback_data 前缀, 如 "hotp_"
    init_msg=None,
    page_size: int = PAGE_SIZE,
    extra_markup: Optional[telebot.types.InlineKeyboardMarkup] = None,
) -> None:
    """
    通用分页渲染:
      cache[uid] = {"list": [...], "page": N, "cid": 群聊/私聊ID, "msg_id": M}
    """
    d = cache.get(uid)
    if not d:
        return
    cid = d.get("cid", uid)  # 群聊 ID 优先，私聊 fallback
    lst   = d["list"]
    total = len(lst)
    pages = max(1, (total + page_size - 1) // page_size)
    p     = max(1, min(d.get("page", 1), pages))
    start = (p - 1) * page_size
    chunk = lst[start:start + page_size]

    text = f"{title_prefix} 共{total}条 第{p}/{pages}页\n\n"
    for i, item in enumerate(chunk, 1):
        text += item_formatter(item, start + i)

    mk = extra_markup if extra_markup else telebot.types.InlineKeyboardMarkup(row_width=2)
    # 翻页按钮
    page_btns = []
    if p > 1:
        page_btns.append(telebot.types.InlineKeyboardButton("⬅️上一页", callback_data=f"{page_cb_prefix}{p-1}"))
    if p < pages:
        page_btns.append(telebot.types.InlineKeyboardButton("➡️下一页", callback_data=f"{page_cb_prefix}{p+1}"))
    if page_btns:
        # 插入到最前面
        mk.keyboard.insert(0, page_btns)

    d["page"] = p
    try:
        if init_msg:
            sent = bot.send_message(cid, text, parse_mode="HTML", reply_markup=mk, disable_web_page_preview=True)
            d["msg_id"] = sent.message_id
        else:
            bot.edit_message_text(
                text, chat_id=cid, message_id=d["msg_id"],
                parse_mode="HTML", reply_markup=mk, disable_web_page_preview=True,
            )
    except Exception:
        # 编辑失败则重新发送
        try:
            sent = bot.send_message(cid, text, parse_mode="HTML", reply_markup=mk, disable_web_page_preview=True)
            d["msg_id"] = sent.message_id
        except Exception as e:
            log.error("分页渲染失败 uid=%s: %s", uid, e)

# ---------- 通用 item 格式化 ----------
def _format_item_link(item: Dict, idx: int) -> str:
    """生成频道帖子的可点击链接行"""
    title   = item.get("title", "未知")
    version = item.get("version_desc", "").replace("更新中", "").strip().replace("  ", " ")
    msg_id  = item.get("msg_id", "")
    line = f"{idx}. <a href='{CHANNEL_LINK}/{msg_id}'>{title}</a> <code>{msg_id}</code>"
    if version and version != "默认版本":
        line += f"\n<code>{version}</code>"
    return line + "\n\n"

# ===================== 过滤其他 Bot 消息 =====================
# 拦截 process_new_messages, 静默丢弃来自其他 bot 的消息 (避免 USER_BOT_TO_BOT_DISABLED)
# ===================== 作者预设 =====================
AUTHOR_FILE = os.path.join(DATA_DIR, "authors.json")

def _load_authors() -> list:
    default = [
        {"name": "下雨了", "link": "https://t.me/Fengdeyuyan_bot"},
        {"name": "FreeYuA", "link": "https://t.me/FreeYuA"},
    ]
    data = _json_load(AUTHOR_FILE, None)
    return data if data else default

def _save_authors(authors: list) -> None:
    _json_save(AUTHOR_FILE, authors)

# ===================== 通用发送辅助 =====================
def _chat_id(msg_or_call) -> int:
    """从 message 或 callback 提取 chat_id，群聊/私聊通用"""
    if hasattr(msg_or_call, 'message'):  # callback
        return msg_or_call.message.chat.id
    return msg_or_call.chat.id

def _safe_delete(chat_id, msg_id):
    try:
        bot.delete_message(chat_id, msg_id)
    except Exception:
        pass

# 群组消息自动删除 (3分钟): 机器人回复 + 用户命令都删
_NO_AUTO_DEL = {int(CHANNEL_ID)} if CHANNEL_ID else set()

_original_send = bot.send_message
def _auto_del_send(chat_id, text, **kwargs):
    msg = _original_send(chat_id, text, **kwargs)
    if str(chat_id).startswith('-') and int(chat_id) not in _NO_AUTO_DEL:
        threading.Timer(180, lambda: _safe_delete(chat_id, msg.message_id)).start()
    return msg
bot.send_message = _auto_del_send

_original_process = bot.process_new_messages
def _auto_del_input(messages):
    for m in messages:
        cid = m.chat.id
        # 跳过频道自动转发的帖子 (is_automatic_forward)
        if getattr(m, 'is_automatic_forward', False):
            continue
        if str(cid).startswith('-') and int(cid) not in _NO_AUTO_DEL:
            threading.Timer(180, lambda mid=m.message_id, cid=cid: _safe_delete(cid, mid)).start()
    return _original_process(messages)
bot.process_new_messages = _auto_del_input

# ===================== 命令菜单 =====================
try:
    # 私聊：完整命令列表
    private_cmds = [
        telebot.types.BotCommand("start",     "启动机器人/返回主菜单"),
        telebot.types.BotCommand("search",    "搜索资源"),
        telebot.types.BotCommand("popular",   "热门影视 (近三月)"),
        telebot.types.BotCommand("today",     "今日汇总"),
        telebot.types.BotCommand("yesterday", "昨日汇总"),
        telebot.types.BotCommand("calendar",  "追更日历"),
        telebot.types.BotCommand("submit",    "我要投稿"),
        telebot.types.BotCommand("up",        "快捷更新连载"),
        telebot.types.BotCommand("help",      "使用帮助"),
        telebot.types.BotCommand("cancel",    "取消当前操作"),
        telebot.types.BotCommand("apply",    "申请投稿权限"),
        telebot.types.BotCommand("del",      "快捷删除记录 /del ID"),
    ]
    bot.set_my_commands(private_cmds)

    # 群组：精简命令
    group_cmds = [
        telebot.types.BotCommand("start",     "主菜单"),
        telebot.types.BotCommand("search",    "搜索资源"),
        telebot.types.BotCommand("popular",   "热门影视"),
        telebot.types.BotCommand("today",     "今日汇总"),
        telebot.types.BotCommand("yesterday", "昨日汇总"),
        telebot.types.BotCommand("del",      "快捷删除 /del ID"),
    ]
    bot.set_my_commands(group_cmds, scope=telebot.types.BotCommandScopeAllGroupChats())
except Exception as e:
    log.warning("设置命令菜单失败 (scope 可能不支持): %s", e)
    # 兜底：统一命令列表
    try:
        bot.set_my_commands(private_cmds)
    except Exception:
        pass

# ===================== 主菜单 =====================
def _fake_msg_from_call(call):
    """从 callback 构造一个兼容 message 接口的对象，方便复用现有 handler"""
    class _FakeMsg:
        pass
    m = _FakeMsg()
    m.chat = call.message.chat
    m.from_user = call.from_user
    m.message_id = call.message.message_id
    m.text = ""
    return m

@bot.message_handler(commands=['start'])
def start(message):
    uid = message.from_user.id
    cid = message.chat.id
    reset_user_state(uid)

    if is_group(message.chat):
        # 群组：简化菜单，3分钟后自动删除
        bot.send_message(
            cid,
            "🤖 杨🐑的天翼小助手\n\n👇 请点击下方按钮选择功能：",
            reply_markup=make_main_keyboard(uid, message.chat),
        )
    else:
        # 私聊：底部键盘 (ReplyKeyboardMarkup)
        kb = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=3)
        kb.add("🔍 搜索资源", "🔥 热门影视", "☀️ 今日汇总")
        kb.add("🌙 昨日汇总", "📅 追更日历", "📤 我要投稿")
        bottom_row = ["❓ 使用帮助"]
        if uid == SUPER_ADMIN:
            bottom_row.append("📋 记录管理")
            bottom_row.append("⚙️ 管理面板")
        kb.add(*bottom_row)

        username = message.from_user.username
        user_display = f"@{username}" if username else str(uid)
        role = "🔴 超管" if uid == SUPER_ADMIN else ("🟢 管理员" if is_admin(uid) else "⚪ 用户")
        cl = _cfg.get("channel_link", "https://t.me/+WQ_ZoAoAHSRmZTEx")
        gl = _cfg.get("group_link", "https://t.me/+IS2hgphdu8Y4MTUx")
        bot.send_message(
            cid,
            f"🤖 欢迎使用杨🐑的天翼小助手\n\n"
            f"📢 <a href='{cl}'>频道</a> | 💬 <a href='{gl}'>群组</a>\n"
            f"👤 用户: {user_display}\n"
            f"🏷️ 身份：{role}\n"
            f"📤 投稿模式支持智能流控机制\n\n"
            f"⌨️ 请使用下方键盘选择功能：",
            reply_markup=kb,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

# "🔙 返回主菜单" — 改成 callback 触发
@bot.callback_query_handler(func=lambda c: c.data == "menu_start")
def cb_start(call):
    bot.answer_callback_query(call.id)
    start(_fake_msg_from_call(call))

@bot.callback_query_handler(func=lambda c: c.data.startswith("menu_") and c.data != "menu_start")
def menu_router(call):
    """内联菜单按钮路由"""
    action = call.data[5:]
    msg = _fake_msg_from_call(call)
    bot.answer_callback_query(call.id)

    if action == "search":
        search_start(msg)
    elif action == "popular":
        popular_summary(msg)
    elif action == "today":
        today_summary(msg)
    elif action == "yesterday":
        yesterday_summary(msg)
    elif action == "calendar":
        emb_calendar_menu(msg)
    elif action == "submit":
        submit_start(msg)
    elif action == "help":
        show_help(msg)
    elif action == "record":
        record_manager(msg)
    elif action == "admin":
        super_admin_panel(msg)

# ===================== 使用帮助 =====================
@bot.message_handler(commands=['help'])
@bot.message_handler(func=lambda m: m.text == "❓ 使用帮助")
def show_help(message):
    help_text = """💡 <b>使用帮助</b>

🔍 <b>/search</b> - 搜索资源
🔥 <b>/popular</b> - 热门影视 (近三月TMDB上映)
☀️ <b>/today</b> - 今日汇总 / 🌙 <b>/yesterday</b> - 昨日汇总
📅 <b>/calendar</b> - 追更日历 (入库进度 + 连载列表)
📤 <b>/submit</b> - 投稿 (或直接发天翼链接)
🚀 <b>/up</b> <code>ID 集数</code> - 快捷更新
📝 <b>/apply</b> - 申请投稿权限
📋 <b>/start</b> - 主菜单 / <b>/cancel</b> - 取消

💡 <b>投稿技巧</b>
• 发 <code>cloud.189.cn/t/xxx</code> 直接投稿
• 文件名写 GB/MB 自动提取大小
• 预览可改版本、大小、TMDB识别、加作者
• 重复链接自动覆盖旧帖
• 云盘文件数 = 追更集数 (每10分钟自动刷新)

⚙️ <b>管理功能</b>
• 管理面板添加/移除管理员
• 记录管理搜索/删除帖子
• 非管理员可申请投稿权限"""
    bot.reply_to(message, help_text, parse_mode="HTML")

# ===================== 📝 申请投稿 =====================
APPLY_FILE = os.path.join(DATA_DIR, "apply_list.json")

@bot.message_handler(func=lambda m: m.text == "📝 申请投稿")
def apply_btn(message):
    apply_submit(message)

@bot.message_handler(commands=['apply'])
def apply_submit(message):
    """普通用户申请投稿权限 — 第一步：输入理由"""
    uid = message.from_user.id
    if is_admin(uid):
        bot.reply_to(message, "✅ 你已是管理员，无需申请")
        return
    applies = _json_load(APPLY_FILE, [])
    for ap in applies:
        if ap.get("id", ap) == uid:
            bot.reply_to(message, "⏳ 你已申请过，请耐心等待审核")
            return
    user_data[uid] = {"step": "apply_reason"}
    bot.reply_to(message, "📝 请输入申请理由：\n(你为什么想投稿？有哪些资源可以分享？)")

@bot.message_handler(func=lambda m: user_data.get(m.from_user.id, {}).get("step") == "apply_reason")
def do_apply_reason(message):
    """用户输入理由 → 提交申请"""
    uid = message.from_user.id
    reason = message.text.strip()
    if not reason:
        return
    username = message.from_user.username or ""
    name = message.from_user.full_name or str(uid)
    applies = _json_load(APPLY_FILE, [])
    applies.append({"id": uid, "name": name, "username": username})
    _json_save(APPLY_FILE, applies)
    user_data.pop(uid, None)

    mk = telebot.types.InlineKeyboardMarkup()
    mk.add(
        telebot.types.InlineKeyboardButton("✅ 通过", callback_data=f"approve_{uid}"),
        telebot.types.InlineKeyboardButton("❌ 拒绝", callback_data=f"reject_{uid}"),
    )
    bot.send_message(SUPER_ADMIN,
        f"📩 新的投稿申请\n👤 {html.escape(name)} (@{username})\n🆔 <code>{uid}</code>\n📝 理由：{html.escape(reason)}",
        reply_markup=mk, parse_mode="HTML")
    bot.reply_to(message, "📩 申请已提交，请等待审核")

@bot.callback_query_handler(func=lambda c: c.data.startswith(("dead_del_", "dead_skip_")))
def cb_dead_link(call):
    """失效链接处理"""
    if call.from_user.id != SUPER_ADMIN:
        bot.answer_callback_query(call.id, "无权限")
        return
    action, msg_id = call.data.split("_", 2)[1:]
    if action == "del":
        try:
            bot.delete_message(chat_id=CHANNEL_ID, message_id=int(msg_id))
            log.info("已删除频道帖子 msg_id=%s", msg_id)
        except Exception as e:
            log.warning("删除频道帖子失败 msg_id=%s: %s", msg_id, e)
        with db_lock:
            idx = next((i for i, x in enumerate(msg_db) if str(x.get("msg_id","")) == msg_id), -1)
            if idx >= 0:
                del msg_db[idx]
            msg_db_index.pop(int(msg_id), None)
        save_msg()
        bot.edit_message_text("🗑️ 已删除失效帖子", call.message.chat.id, call.message.id)
    else:
        bot.edit_message_text("⏭️ 已跳过", call.message.chat.id, call.message.id)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith(("approve_", "reject_")))
def cb_apply_review(call):
    """超管审核申请"""
    if call.from_user.id != SUPER_ADMIN:
        bot.answer_callback_query(call.id, "无权限")
        return
    action, uid_str = call.data.split("_")
    uid = int(uid_str)
    applies = _json_load(APPLY_FILE, [])
    found_ap = None
    for ap in applies:
        if ap.get("id", ap) == uid:
            found_ap = ap
            break
    if not found_ap:
        bot.answer_callback_query(call.id, "已处理")
        return
    applies.remove(found_ap)
    _json_save(APPLY_FILE, applies)

    if action == "approve":
        if not is_admin(uid):
            name = found_ap.get("name", "") if isinstance(found_ap, dict) else ""
            admins.append({"id": uid, "name": name})
            save_admins()
        bot.edit_message_text(f"✅ 已通过 <code>{uid}</code> 的投稿申请", call.message.chat.id, call.message.id, parse_mode="HTML")
        try:
            bot.send_message(uid, "🎉 你的投稿申请已通过！现在可以使用 /submit 投稿了。")
        except Exception:
            pass
    else:
        bot.edit_message_text(f"❌ 已拒绝 <code>{uid}</code> 的投稿申请", call.message.chat.id, call.message.id, parse_mode="HTML")
        try:
            bot.send_message(uid, "❌ 你的投稿申请未通过")
        except Exception:
            pass
    bot.answer_callback_query(call.id)

# ===================== ⚙️ 管理面板 =====================
@bot.message_handler(func=lambda m: m.text == "⚙️ 管理面板")
def super_admin_panel(message):
    uid = message.from_user.id
    if uid != SUPER_ADMIN:
        bot.reply_to(message, "❌ 权限不足，此面板仅最高超级管理员可用。")
        return
    user_data[uid] = {"action": "super_admin_menu"}
    mk = telebot.types.InlineKeyboardMarkup(row_width=2)
    mk.add(
        telebot.types.InlineKeyboardButton("👥 查看管理员", callback_data="adm_view"),
        telebot.types.InlineKeyboardButton("➕ 添加管理员", callback_data="adm_add"),
    )
    mk.add(
        telebot.types.InlineKeyboardButton("➖ 移除管理员", callback_data="adm_remove"),
        telebot.types.InlineKeyboardButton("🔙 返回主菜单", callback_data="menu_start"),
    )
    bot.reply_to(message, "⚙️ **最高控制台已开启**\n您可以在此管理其他发帖助手的权限：", reply_markup=mk, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda c: c.data == "adm_view")
def cb_view_admins(call):
    bot.answer_callback_query(call.id)
    view_admins(_fake_msg_from_call(call))

@bot.callback_query_handler(func=lambda c: c.data == "adm_add")
def cb_add_admin(call):
    bot.answer_callback_query(call.id)
    add_admin_start(_fake_msg_from_call(call))

@bot.callback_query_handler(func=lambda c: c.data == "adm_remove")
def cb_remove_admin(call):
    bot.answer_callback_query(call.id)
    remove_admin_start(_fake_msg_from_call(call))

@bot.message_handler(func=lambda m: m.text == "👥 查看管理员")
def view_admins(message):
    if message.from_user.id != SUPER_ADMIN:
        return
    if not admins:
        bot.reply_to(message, "💡 当前没有任何子管理员。\n\n(除了您这位超级管理员以外，无人可使用发帖与记录管理功能)")
        return
    text = "👥 <b>当前子管理员列表：</b>\n\n"
    for idx, adm in enumerate(admins, 1):
        name = adm.get("name", "") if isinstance(adm, dict) else ""
        aid = adm.get("id", adm) if isinstance(adm, dict) else adm
        try:
            if not name:
                chat = bot.get_chat(aid)
                name = chat.full_name or chat.first_name or ""
        except Exception:
            pass
        if name:
            text += f"{idx}. <a href='tg://user?id={aid}'>{html.escape(str(name))}</a>  <code>{aid}</code>\n"
        else:
            text += f"{idx}. 🆔 <code>{aid}</code>\n"
    bot.reply_to(message, text, parse_mode="HTML")

@bot.message_handler(func=lambda m: m.text == "➕ 添加管理员")
def add_admin_start(message):
    if message.from_user.id != SUPER_ADMIN:
        return
    user_data[message.from_user.id] = {"action": "add_admin"}
    bot.reply_to(message, "➕ 请发送要添加的用户 ID 或 @用户名：\n发送 0 返回", parse_mode="HTML")

@bot.message_handler(func=lambda m: user_data.get(m.from_user.id, {}).get("action") == "add_admin")
def do_add_admin(message):
    uid, text = message.from_user.id, message.text.strip()
    if text in ["0", "退出", "quit"]:
        super_admin_panel(message)
        return
    if text.startswith("@"):
        text = text[1:]
    if not text.isdigit():
        bot.reply_to(message, "❌ 请输入纯数字 ID")
        return
    new_admin = int(text)
    if new_admin == SUPER_ADMIN:
        bot.reply_to(message, "⚠️ 已是超管")
        return
    for a in admins:
        aid = a.get("id", a) if isinstance(a, dict) else a
        if aid == new_admin:
            bot.reply_to(message, "⚠️ 该用户已经是管理员了")
            return
    user_data[uid] = {"action": "add_admin_name", "new_id": new_admin}
    bot.reply_to(message, "✅ ID 已确认，请发送显示名称：")

@bot.message_handler(func=lambda m: user_data.get(m.from_user.id, {}).get("action") == "add_admin_name")
def do_add_admin_name(message):
    uid = message.from_user.id
    name = message.text.strip()
    new_id = user_data.get(uid, {}).get("new_id")
    if not new_id:
        return
    admins.append({"id": new_id, "name": name})
    save_admins()
    user_data.pop(uid, None)
    bot.reply_to(message, f"✅ 添加成功！\n{name}  <code>{new_id}</code>", parse_mode="HTML")
    super_admin_panel(message)

@bot.message_handler(func=lambda m: m.text == "➖ 移除管理员")
def remove_admin_start(message):
    if message.from_user.id != SUPER_ADMIN:
        return
    if not admins:
        bot.reply_to(message, "💡 当前没有可供移除的子管理员。")
        return
    mk = telebot.types.InlineKeyboardMarkup(row_width=1)
    for adm in admins:
        name = adm.get("name", "") if isinstance(adm, dict) else ""
        aid = adm.get("id", adm) if isinstance(adm, dict) else adm
        label = f"🗑️ {name} ({aid})" if name else f"🗑️ 移除: {aid}"
        mk.add(telebot.types.InlineKeyboardButton(label, callback_data=f"deladm_{aid}"))
    mk.add(telebot.types.InlineKeyboardButton("🔙 取消操作", callback_data="adm_cancel"))
    bot.reply_to(message, "➖ 选择要移除的管理员：", reply_markup=mk)

@bot.callback_query_handler(func=lambda c: c.data == "adm_cancel")
def cb_adm_cancel(call):
    bot.answer_callback_query(call.id)
    bot.edit_message_text("✅ 已取消", call.message.chat.id, call.message.id)

@bot.callback_query_handler(func=lambda c: c.data.startswith("deladm_"))
def do_remove_admin(call):
    if call.from_user.id != SUPER_ADMIN:
        return
    target_id = int(call.data.split("_")[1])
    found = None
    for a in admins:
        aid = a.get("id", a) if isinstance(a, dict) else a
        if aid == target_id:
            found = a
            break
    if found:
        admins.remove(found)
        save_admins()
        bot.edit_message_text(f"✅ 移除成功！\n<code>{target_id}</code> 已失去管理员权限", call.message.chat.id, call.message.id, parse_mode="HTML")
    else:
        bot.answer_callback_query(call.id, "❌ 该管理员不存在或已被移除。")

# ===================== 🔥 热门影视 (近三个月 TMDB 发布) =====================
def _get_tmdb_release_date(item: dict) -> Optional[str]:
    """从 TMDB 获取影视发布日期（优先缓存）"""
    title = item.get("title", "")
    # 读缓存
    cache_key = f"{title}"
    with _tmdb_date_lock:
        if cache_key in _tmdb_date_cache:
            return _tmdb_date_cache[cache_key]

    tmdb_id = item.get("tmdb_id")
    try:
        # 推测类型 (从 title 判断)
        search_type = "tv" if re.search(r'[SE]\d+', title) or "更新中" in item.get("version_desc", "") else "movie"

        if tmdb_id:
            data = get_tmdb_data_by_id(search_type, tmdb_id)
        else:
            clean = re.sub(r'\(\d{4}\)', '', title).strip()
            url = f"https://api.themoviedb.org/3/search/{search_type}?api_key={TMDB_API_KEY}&query={urllib.parse.quote(clean)}&language=zh-CN"
            res = tmdb_cached_get(url, timeout=5)
            results = res.get("results", []) if res else []
            if not results:
                # 换另一种类型重试
                alt_type = "movie" if search_type == "tv" else "tv"
                url = f"https://api.themoviedb.org/3/search/{alt_type}?api_key={TMDB_API_KEY}&query={urllib.parse.quote(clean)}&language=zh-CN"
                res = tmdb_cached_get(url, timeout=5)
                results = res.get("results", []) if res else []
            if not results:
                return None
            data = results[0]

        result = ""
        if search_type == "movie":
            result = data.get("release_date") or ""
        else:
            result = data.get("first_air_date") or ""
        if result:
            with _tmdb_date_lock:
                _tmdb_date_cache[cache_key] = result
        return result
    except Exception:
        return None

@bot.message_handler(commands=['popular'])
@bot.message_handler(func=lambda m: m.text == "🔥 热门影视")
def popular_summary(message):
    uid = message.from_user.id
    wait = bot.reply_to(message, "⏳ 正在从 TMDB 获取影视发布日期...")
    three_months_ago = (date.today() - timedelta(days=90)).strftime("%Y-%m-%d")

    with db_lock:
        all_items = list(msg_db)

    # 先用年份过滤：只看今年和去年的
    this_year = str(date.today().year)
    last_year = str(date.today().year - 1)
    candidates = []
    for item in all_items:
        title = item.get("title", "")
        yr_match = re.search(r'\((\d{4})\)', title)
        yr = yr_match.group(1) if yr_match else ""
        if yr and yr < last_year:
            continue  # 两年前的跳过
        candidates.append(item)

    popular_items = []
    for item in candidates:
        release = _get_tmdb_release_date(item)
        if release and release >= three_months_ago:
            item = dict(item)
            item["_release_date"] = release
            popular_items.append(item)

    try:
        bot.delete_message(uid, wait.message_id)
    except Exception:
        pass

    if not popular_items:
        bot.reply_to(message, "🔥 近三个月暂无新上映影视")
        return

    popular_items.sort(key=lambda x: x.get("_release_date", ""), reverse=True)
    popular_cache[uid] = {"list": popular_items, "page": 1, "msg_id": None, "cid": message.chat.id}
    render_paginated(
        uid, popular_cache, uid,
        "🔥 热门影视 (近三月上映)",
        _format_item_link,
        "pp_",
        init_msg=message,
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("pp_"))
def cb_popular_page(c):
    uid = c.from_user.id
    p = int(c.data.split("_")[1])
    popular_cache[uid]["page"] = p
    render_paginated(uid, popular_cache, uid, "🔥 热门影视 (近三月上映)", _format_item_link, "pp_")
    bot.answer_callback_query(c.id)

# ===================== 🚀 快捷更新引擎 =====================
@bot.message_handler(commands=['up'])
def quick_update_cmd(message):
    if message.from_user.id != SUPER_ADMIN:
        bot.reply_to(message, "❌ 仅超级管理员可使用！")
        return
    parts = message.text.split()
    if len(parts) != 3:
        bot.reply_to(message, "⚠️ 格式错误！\n请使用命令：<code>/up 消息ID 最新集数</code> 或 <code>/up 消息ID 已完结</code>", parse_mode="HTML")
        return

    msg_id_str, new_ep = parts[1], parts[2]
    if not (new_ep.isdigit() or new_ep == "已完结"):
        bot.reply_to(message, "❌ 参数错误！第三个参数必须是「纯数字」或「已完结」。")
        return

    with db_lock:
        target_item = msg_db_index.get(int(msg_id_str))
    if not target_item:
        bot.reply_to(message, f"❌ 未在数据库中找到 ID 为 {msg_id_str} 的帖子记录。")
        return

    old_version = target_item.get("version_desc", "")
    old_caption = target_item.get("caption", "")

    if "更新中" not in old_version:
        bot.reply_to(message, "⚠️ 该剧集当前不是【更新中】状态。请使用常规的 /submit 重新投稿覆盖。")
        return

    if new_ep == "已完结":
        new_version = _simplify_completed(old_version)
    else:
        ep_padded = f"{int(new_ep):02d}"
        new_version = re.sub(r'E\d+(?![\d-])', f'E{ep_padded}', old_version)
        if new_version == old_version:
            new_version = re.sub(r'E\d+-\d+', f'E01-E{ep_padded}', old_version)

    if new_version == old_version and new_ep != "已完结":
        # 版本号已是最新, 允许刷新 caption (处理轮询更新了 version 但 caption 未更新的情况)
        new_caption = _replace_ep_in_text(old_caption, ep_padded, old_version)
        fresh = _refresh_file_size(target_item.get("link", ""), target_item.get("access_code", ""))
        if fresh:
            new_caption = _update_caption_size(new_caption, fresh)
        if not repost_channel_update(target_item, new_caption, new_version):
            bot.reply_to(message, "❌ 刷新失败，请稍后重试。")
            return
        bot.reply_to(message,
            f"✅ 帖子已刷新 (版本号无变化)！\n\n🎬 {target_item['title']}\n🆔 新消息ID: <code>{target_item['msg_id']}</code>",
            parse_mode="HTML")
        return

    if new_version == old_version:
        bot.reply_to(message, "❌ 无法自动识别原帖中的集数格式，请手动 /submit 覆盖更新。")
        return

    # 用去"更新中"的版本号匹配 caption (caption 已不显示更新中)
    old_disp = old_version.replace("更新中", "").strip().replace("  ", " ")
    new_disp = new_version.replace("更新中", "").strip().replace("  ", " ")
    new_caption = old_caption.replace(old_disp, new_disp) if old_disp in old_caption else old_caption
    if new_caption == old_caption:
        # 旧版号在 caption 中找不到 (可能caption格式不同), 用 regex 直接替换集数
        new_caption = _replace_ep_in_text(old_caption, ep_padded, old_version)
    fresh = _refresh_file_size(target_item.get("link", ""), target_item.get("access_code", ""))
    if fresh:
        new_caption = _update_caption_size(new_caption, fresh)

    if repost_channel_update(target_item, new_caption, new_version):
        bot.reply_to(
            message,
            f"✅ 手动更新并顶贴成功！\n\n🎬 {target_item['title']}\n🆕 新版本：{new_version}\n🆔 新消息ID：<code>{target_item['msg_id']}</code>",
            parse_mode="HTML",
        )
    else:
        bot.reply_to(message, f"❌ 快捷更新失败，请确认该帖子未被手动删除。")

# ===================== 今日汇总 =====================
@bot.message_handler(commands=['today'])
@bot.message_handler(func=lambda m: m.text == "☀️ 今日汇总")
def today_summary(message):
    uid = message.from_user.id
    today_str = date.today().strftime("%Y-%m-%d")
    with db_lock:
        items = [x for x in msg_db if x.get("date") == today_str]
    if not items:
        bot.reply_to(message, "☀️ 今日暂无更新")
        return
    today_cache[uid] = {"list": items, "page": 1, "date": today_str, "msg_id": None, "cid": message.chat.id}
    render_paginated(
        uid, today_cache, uid,
        f"☀️ 今日 {today_str}",
        _format_item_link,
        "tp_",
        init_msg=message,
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("tp_"))
def cb_today(c):
    uid = c.from_user.id
    p = int(c.data.split("_")[1])
    d = today_cache.get(uid)
    if d:
        d["page"] = p
    render_paginated(uid, today_cache, uid, f"☀️ 今日 {d.get('date', '')}" if d else "☀️ 今日", _format_item_link, "tp_")
    bot.answer_callback_query(c.id)

# ===================== 昨日汇总 =====================
@bot.message_handler(commands=['yesterday'])
@bot.message_handler(func=lambda m: m.text == "🌙 昨日汇总")
def yesterday_summary(message):
    uid = message.from_user.id
    yd = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    with db_lock:
        items = [x for x in msg_db if x.get("date") == yd]
    if not items:
        bot.reply_to(message, "🌙 昨日暂无更新")
        return
    yesterday_cache[uid] = {"list": items, "page": 1, "date": yd, "msg_id": None, "cid": message.chat.id}
    render_paginated(
        uid, yesterday_cache, uid,
        f"🌙 昨日 {yd}",
        _format_item_link,
        "yp_",
        init_msg=message,
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("yp_"))
def cb_yesterday(c):
    uid = c.from_user.id
    p = int(c.data.split("_")[1])
    d = yesterday_cache.get(uid)
    if d:
        d["page"] = p
    render_paginated(uid, yesterday_cache, uid, f"🌙 昨日 {d.get('date', '')}" if d else "🌙 昨日", _format_item_link, "yp_")
    bot.answer_callback_query(c.id)

# ===================== 搜索 =====================
@bot.message_handler(func=lambda m: m.text == "🔍 搜索资源")
def search_start(message):
    user_data[message.from_user.id] = {"step": "search"}
    bot.reply_to(message, "🔍 请输入要搜索的影视名称：")

@bot.message_handler(commands=['search'])
def search_cmd(message):
    """/search 命令：带参数直接搜，不带参数进入交互"""
    # 提取 /search@botname 后面的关键词
    parts = message.text.split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        # 有搜索词 → 直接搜
        message.text = parts[1].strip()
        do_search(message)
    else:
        # 无搜索词 → 进入交互模式
        search_start(message)

@bot.message_handler(func=lambda m: user_data.get(m.from_user.id, {}).get("step") == "search")
def do_search(message):
    uid = message.from_user.id
    cid = message.chat.id
    kw = message.text.strip().lower()
    with db_lock:
        res = [x for x in msg_db if kw in str(x.get("title", "")).lower() or kw in str(x.get("version_desc", "")).lower()]
    if not res:
        user_data[uid] = {"step": "search"}
        bot.send_message(cid, "❌ 无结果，可直接输入新关键词搜索：")
        return
    search_cache[uid] = {"kw": kw, "list": res, "page": 1, "cid": cid, "msg_id": None}
    user_data.pop(uid, None)

    # 搜索用独立分页大小
    d = search_cache[uid]
    lst, kw2 = d["list"], d["kw"]
    total = len(lst)
    pages = max(1, (total + SEARCH_PAGE_SIZE - 1) // SEARCH_PAGE_SIZE)
    chunk = lst[:SEARCH_PAGE_SIZE]
    text = f"🔍 {kw2}\n📊 共 {total} 条 第1/{pages}页\n\n"
    for i, item in enumerate(chunk, 1):
        text += _format_item_link(item, i)

    mk = telebot.types.InlineKeyboardMarkup(row_width=2)
    if pages > 1:
        mk.add(telebot.types.InlineKeyboardButton("➡️下一页", callback_data="gopage_2"))
    sent = bot.send_message(cid, text, parse_mode="HTML", reply_markup=mk if pages > 1 else None, disable_web_page_preview=True)
    d["msg_id"] = sent.message_id

def show_search_page(uid: int, p: int, cid: int = None) -> None:
    d = search_cache.get(uid)
    if not d:
        return
    if cid is None:
        cid = d.get("cid", uid)
    lst, kw = d["list"], d["kw"]
    total = len(lst)
    pages = max(1, (total + SEARCH_PAGE_SIZE - 1) // SEARCH_PAGE_SIZE)
    p = max(1, min(p, pages))
    start = (p - 1) * SEARCH_PAGE_SIZE
    chunk = lst[start:start + SEARCH_PAGE_SIZE]

    text = f"🔍 {kw}\n📊 共 {total} 条 第{p}/{pages}页\n\n"
    for i, item in enumerate(chunk, 1):
        text += _format_item_link(item, start + i)

    mk = telebot.types.InlineKeyboardMarkup(row_width=2)
    page_btns = []
    if p > 1:
        page_btns.append(telebot.types.InlineKeyboardButton("⬅️上一页", callback_data=f"gopage_{p-1}"))
    if p < pages:
        page_btns.append(telebot.types.InlineKeyboardButton("➡️下一页", callback_data=f"gopage_{p+1}"))
    if page_btns:
        mk.add(*page_btns)
    d["page"] = p
    try:
        bot.edit_message_text(
            text, chat_id=cid, message_id=d["msg_id"],
            parse_mode="HTML", reply_markup=mk if page_btns else None, disable_web_page_preview=True,
        )
    except Exception:
        sent = bot.send_message(
            cid, text,
            parse_mode="HTML", reply_markup=mk if page_btns else None, disable_web_page_preview=True,
        )
        d["msg_id"] = sent.message_id

@bot.callback_query_handler(func=lambda c: c.data.startswith("gopage_"))
def cb_search(c):
    uid = c.from_user.id
    p = int(c.data.split("_")[1])
    show_search_page(uid, p, c.message.chat.id)
    bot.answer_callback_query(c.id)

# ===================== 📅 追更日历 子菜单 =====================
# 云盘缓存 (每10分钟由 poll_file_size 刷新)
_cloud_cache: Dict[str, dict] = {}
_cloud_cache_lock = threading.Lock()

# TMDB 发布日期缓存 (每10分钟刷新)
_tmdb_date_cache: Dict[str, str] = {}
_tmdb_date_lock = threading.Lock()

def _get_cloud_file_count(link: str, access_code: str = "") -> int:
    """从云盘缓存获取文件数（集数）"""
    with _cloud_cache_lock:
        entry = _cloud_cache.get(link)
        if entry:
            return entry.get("count", 0)
    # 缓存未命中，实时查一次
    try:
        result = parse_189_share_details(link, access_code or None)
        if result:
            with _cloud_cache_lock:
                _cloud_cache[link] = {"size": result.get("size", 0), "count": result.get("count", 0)}
            return result.get("count", 0)
    except Exception:
        pass
    return 0

@bot.message_handler(func=lambda m: m.text == "📅 追更日历")
def emb_calendar_menu(message):
    """追更日历入口 — 显示子菜单"""
    mk = telebot.types.InlineKeyboardMarkup(row_width=2)
    mk.add(
        telebot.types.InlineKeyboardButton("📅 入库进度", callback_data="sub_calendar"),
        telebot.types.InlineKeyboardButton("🔄 热更影视", callback_data="sub_hot"),
    )
    mk.add(telebot.types.InlineKeyboardButton("🔙 返回主菜单", callback_data="menu_start"))
    bot.reply_to(message, "📅 <b>追更日历</b>\n\n请选择功能：", parse_mode="HTML", reply_markup=mk)

@bot.callback_query_handler(func=lambda c: c.data == "sub_calendar")
def cb_sub_calendar(call):
    bot.answer_callback_query(call.id)
    tracking_calendar(_fake_msg_from_call(call))

@bot.callback_query_handler(func=lambda c: c.data == "sub_hot")
def cb_sub_hot(call):
    bot.answer_callback_query(call.id)
    hot_summary(_fake_msg_from_call(call))

# ===================== 🔄 热更影视 =====================
def hot_summary(message):
    uid = message.from_user.id
    with db_lock:
        hot_items = [x for x in msg_db if "更新中" in x.get("version_desc", "")]
    if not hot_items:
        bot.reply_to(message, "🔄 暂无正在更新的影视")
        return
    hot_cache[uid] = {"list": hot_items, "page": 1, "msg_id": None, "cid": message.chat.id}
    render_paginated(
        uid, hot_cache, uid,
        "🔄 热更影视列表",
        _format_item_link,
        "hotp_",
        init_msg=message,
    )

@bot.callback_query_handler(func=lambda c: c.data.startswith("hotp_"))
def cb_hot_page(c):
    uid = c.from_user.id
    p = int(c.data.split("_")[1])
    hot_cache[uid]["page"] = p
    render_paginated(uid, hot_cache, uid, "🔄 热更影视列表", _format_item_link, "hotp_")
    bot.answer_callback_query(c.id)

# ===================== 📅 追更日历 =====================
# Emby 剧集缓存 (追更日历时一次性加载)
_emby_cache = None
_emby_cache_time = 0
_EMBY_STATE_FILE = os.path.join(DATA_DIR, "emby_state.json")

def _load_emby_series() -> List[dict]:
    """一次性加载 Emby 所有剧集，缓存5分钟"""
    global _emby_cache, _emby_cache_time
    now = time.time()
    if _emby_cache and now - _emby_cache_time < 300:
        log.info("Emby 使用缓存 (%d部)", len(_emby_cache))
        return _emby_cache

    all_series = []
    try:
        for limit, start in [(500, 0), (500, 500), (500, 1000)]:
            params = {"api_key": EMBY_API_KEY, "IncludeItemTypes": "Series",
                      "Recursive": "true", "Limit": limit, "StartIndex": start}
            res = http.get(EMBY_API_URL, params=params, timeout=15)
            if res.status_code != 200:
                break
            items = res.json().get("Items", [])
            all_series.extend(items)
            if len(items) < limit:
                break
        log.info("Emby 加载 %d 部剧集", len(all_series))
        _emby_cache = all_series
        _emby_cache_time = now
    except Exception as e:
        log.info("Emby 加载失败: %s", e)
    return _emby_cache or []

def _get_emby_max_ep(series_name: str, season_num: int) -> int:
    """从 Emby 缓存查询剧集最大集数"""
    try:
        all_series = _load_emby_series()
        series_id = None
        for s in all_series:
            if series_name.lower() in s.get("Name", "").lower():
                series_id = s.get("Id")
                break
        if not series_id:
            return 0

        # 用 ParentId 查分集
        ep_params = {"api_key": EMBY_API_KEY, "ParentId": series_id,
                     "IncludeItemTypes": "Episode", "Recursive": "true", "Limit": 200}
        ep_res = http.get(EMBY_API_URL, params=ep_params, timeout=10)
        if ep_res.status_code != 200:
            return 0
        max_ep = 0
        for ep in ep_res.json().get("Items", []):
            if ep.get("ParentIndexNumber") == season_num:
                ep_num = ep.get("IndexNumber", 0) or 0
                if ep_num > max_ep:
                    max_ep = ep_num
        log.info("☁️: %s S%d → %d集", series_name, season_num, max_ep)
        return max_ep
    except Exception as e:
        log.info("Emby查询失败 %s: %s", series_name, e)
        return 0

@bot.message_handler(commands=['calendar'])
def tracking_calendar(message):
    uid = message.from_user.id
    wait_msg = bot.reply_to(message, "⏳ 正在计算全网缺更与 Emby 入库进度，请稍候...")

    with db_lock:
        hot_items = [x for x in msg_db if "更新中" in x.get("version_desc", "")]
    today_str = date.today().strftime("%Y-%m-%d")
    emby_yesterday = _json_load(_EMBY_STATE_FILE, {})
    emby_today_state = {}
    today_completed_ids = emby_yesterday.pop("_completed", {}).get(today_str, [])

    updated_today = []
    pending_today = []
    auto_complete_list = []

    for item in hot_items:
        title = re.sub(r'\s*\(\d{4}\)', '', item.get("title", ""))  # 去掉年份
        saved_tmdb_id = item.get("tmdb_id")
        version_desc = item.get("version_desc", "")
        is_updated_today = (item.get("date") == today_str)

        local_season = 1
        local_max_ep = 0
        s_match = re.search(r'[Ss](\d+)', version_desc)
        e_matches = re.findall(r'[Ee](\d+)', version_desc)
        if s_match:
            local_season = int(s_match.group(1))
        if e_matches:
            local_max_ep = int(e_matches[-1])

        aired_eps = []
        tmdb_total = 0
        season_data = None
        try:
            tv_id = saved_tmdb_id
            if not tv_id:
                clean_title = re.sub(r'\(\d{4}\)', '', title).strip()
                search_url = f"https://api.themoviedb.org/3/search/tv?api_key={TMDB_API_KEY}&query={urllib.parse.quote(clean_title)}&language=zh-CN"
                res_data = tmdb_cached_get(search_url, timeout=5)
                if res_data and res_data.get("results"):
                    tv_id = res_data["results"][0]["id"]

            if tv_id:
                season_url = f"https://api.themoviedb.org/3/tv/{tv_id}/season/{local_season}?api_key={TMDB_API_KEY}&language=zh-CN"
                season_data = tmdb_cached_get(season_url, timeout=5)
                if season_data:
                    tmdb_total = len(season_data.get("episodes", []))
                    for ep in season_data.get("episodes", []):
                        air_date = ep.get("air_date")
                        if air_date and air_date <= today_str:
                            aired_eps.append(ep.get("episode_number"))
        except Exception as e:
            log.warning("获取排期失败 %s: %s", title, e)

        # 查云盘文件数（集数）、对比昨天算范围
        emby_max_ep = _get_cloud_file_count(item.get("link", ""), item.get("access_code", ""))
        emby_today_state[f"{title}|{local_season}"] = emby_max_ep

        # 今日入库范围
        yesterday_ep = emby_yesterday.get(f"{title}|{local_season}", 0)
        today_range = ""
        if emby_max_ep > yesterday_ep:
            start = yesterday_ep + 1
            end = emby_max_ep
            today_range = f"S{local_season:02d}E{start:02d}" if start == end else f"S{local_season:02d}E{start:02d}-E{end:02d}"

        # 今天 TMDB 播出的集数 + 下一集播出时间
        today_aired = [ep for ep in season_data.get("episodes", []) if ep.get("air_date") == today_str] if season_data else []
        today_aired_nums = [ep.get("episode_number") for ep in today_aired]
        next_ep = None
        for ep in season_data.get("episodes", []) if season_data else []:
            air = ep.get("air_date", "")
            if air and air > today_str and ep.get("episode_number", 0) > emby_max_ep:
                next_ep = ep
                break

        s_str = f"S{local_season:02d}"
        # 跟进进度: 今天应入但未入的集数，没播出则显示全部缺集
        if today_aired_nums:
            pending_list = [ep for ep in today_aired_nums if ep > emby_max_ep]
        else:
            pending_list = [ep for ep in aired_eps if ep > emby_max_ep]
        pend_info = ""
        if today_aired_nums:
            if pending_list:
                if len(pending_list) == 1:
                    pend_info = f"缺 {s_str}E{pending_list[0]:02d}"
                else:
                    pend_info = f"缺 {s_str}E{min(pending_list):02d}-E{max(pending_list):02d}"
            else:
                pend_info = "✅ 已同步"
        elif emby_max_ep > 0 and emby_max_ep >= tmdb_total:
            pend_info = "✅ 已同步"
        else:
            pend_info = "今日无更"
        next_info = ""
        if next_ep:
            next_info = f" | 下次更新:{next_ep.get('air_date', '')}"
        pending_today.append({"db_item": item, "ep_info": pend_info, "emby_ep": emby_max_ep, "tmdb_ep": tmdb_total, "next": next_info, "updated_today": is_updated_today or item.get("date") == today_str})

        # Emby 超前但帖子未更新 → 自动更新帖子
        if emby_max_ep > local_max_ep and "更新中" in item.get("version_desc", ""):
            old_v = item.get("version_desc", "")
            old_c = item.get("caption", "")
            new_v = _replace_ep_in_text(old_v, f"{emby_max_ep:02d}")
            if new_v != old_v:
                old_v_disp = old_v.replace("更新中", "").strip().replace("  ", " ")
                new_v_disp = new_v.replace("更新中", "").strip().replace("  ", " ")
                new_c = old_c.replace(old_v_disp, new_v_disp) if old_v_disp in old_c else _replace_ep_in_text(old_c, f"{emby_max_ep:02d}")
                fresh = _refresh_file_size(item.get("link", ""), item.get("access_code", ""))
                if fresh:
                    new_c = _update_caption_size(new_c, fresh)
                if repost_channel_update(item, new_c, new_v):
                    item["date"] = today_str  # 标记今日更新

        if is_updated_today or item.get("date") == today_str:
            ep_info = today_range or (f"{s_str}E{emby_max_ep:02d}" if emby_max_ep else version_desc)
            updated_today.append({"db_item": item, "ep_info": ep_info})

        # 自动完结:
        # 条件1: TMDB 全季已播完 (已播=总集 或 最后一集已过7天)
        # 条件2: 频道帖子7天未更新
        # 条件3: 本地已跟进到最新已播集
        all_aired = tmdb_total > 0 and len(aired_eps) >= tmdb_total
        if all_aired and local_max_ep >= max(aired_eps) and "更新中" in item.get("version_desc", ""):
            final_ep = emby_max_ep if emby_max_ep > 0 else local_max_ep
            ep_info = f"{s_str}E01-E{final_ep:02d}" if final_ep > 1 else f"{s_str}E{final_ep:02d}"
            auto_complete_list.append((item, final_ep, ep_info))

    # 执行自动完结
    completed_list = []
    for item, emby_ep, ep_info in auto_complete_list:
        try:
            old_v = item.get("version_desc", "")
            old_c = item.get("caption", "")
            new_v = _simplify_completed(old_v)
            # caption 已不显示"更新中", 用去"更新中"的版本号匹配
            old_v_disp = old_v.replace("更新中", "").strip().replace("  ", " ")
            new_v_disp = new_v.replace("更新中", "").strip().replace("  ", " ")
            new_c = old_c.replace(old_v_disp, new_v_disp) if old_v_disp in old_c else old_c
            if repost_channel_update(item, new_c, new_v):
                completed_list.append({"db_item": item, "ep_info": ep_info})
        except Exception as e:
            log.warning("自动完结失败 %s: %s", item.get("title"), e)

    # 保存今日完结 ID，后续查询也能显示
    all_completed_ids = today_completed_ids + [c["db_item"].get("msg_id") for c in completed_list]
    if all_completed_ids:
        emby_today_state.setdefault("_completed", {})[today_str] = list(set(all_completed_ids))
    _json_save(_EMBY_STATE_FILE, emby_today_state)
    auto_complete_list = []

    reply_text = f"📅 <b>追更日历 (截至 {today_str})</b>\n"

    if updated_today:
        reply_text += "🟢 <b>已入库</b>\n"
        for idx, p in enumerate(updated_today, 1):
            item = p["db_item"]
            msg_id = item.get("msg_id", "")
            reply_text += f"  {idx}. {item['title']} ID:<code>{msg_id}</code> → {p['ep_info']}\n"

    if pending_today:
        reply_text += "\n🔄 <b>跟进进度</b>\n"
        for idx, p in enumerate(pending_today, 1):
            item = p["db_item"]
            msg_id = item.get("msg_id", "")
            emby_ep = p.get("emby_ep", 0)
            tmdb_ep = p.get("tmdb_ep", 0)
            reply_text += f"  {idx}. {item['title']} ID:<code>{msg_id}</code>\n"
            status = p['ep_info']
            updated = p.get('updated_today', False)
            dead_hint = ""
            if emby_ep == 0:
                dead_hint = " ⚠️链接可能失效"
            if "已同步" in status or updated:
                reply_text += f"     ✅ ☁️:{emby_ep} | TMDB:{tmdb_ep}{dead_hint}\n"
            elif status == "今日无更":
                next_str = p.get("next", "")
                reply_text += f"     🚫 ☁️:{emby_ep} | TMDB:{tmdb_ep}{next_str}{dead_hint}\n"
            else:
                reply_text += f"     ⏳ ☁️:{emby_ep} | TMDB:{tmdb_ep} | {status}{dead_hint}\n"

    # 追加之前已完结的（今天第二次查询时）
    with db_lock:
        for mid in today_completed_ids:
            if not any(c["db_item"].get("msg_id") == mid for c in completed_list):
                item = msg_db_index.get(mid)
                if item:
                    v = item.get("version_desc", "")
                    # 提取 S01E01-E24 格式
                    m = re.search(r'(S\d+E\d+(?:-E?\d+)?)', v)
                    completed_list.append({"db_item": item, "ep_info": m.group(1) if m else v})

    if completed_list:
        reply_text += "\n✅ <b>今日完结</b>\n"
        for idx, p in enumerate(completed_list, 1):
            item = p["db_item"]
            msg_id = item.get("msg_id", "")
            reply_text += f"  {idx}. {item['title']} <code>{msg_id}</code> → {p['ep_info']}\n"

    # 插入标题行
    status_parts = []
    if updated_today: status_parts.append(f"🟢 已入库 {len(updated_today)}")
    if pending_today: status_parts.append(f"🔄 跟进 {len(pending_today)}")
    if completed_list: status_parts.append(f"✅ 今日完结 {len(completed_list)}")
    status_line = " | ".join(status_parts) if status_parts else "暂无更新"
    idx = reply_text.index("\n") + 1
    reply_text = reply_text[:idx] + status_line + "\n\n" + reply_text[idx:]

    try:
        bot.delete_message(uid, wait_msg.message_id)
    except Exception:
        pass
    bot.send_message(uid, reply_text, parse_mode="HTML", disable_web_page_preview=True)

# ===================== 📋 记录管理 =====================
@bot.message_handler(func=lambda m: m.text == "📋 记录管理")
@admin_only
def record_manager(message):
    uid = message.from_user.id
    user_data[uid] = {"action": "record_menu"}
    mk = telebot.types.InlineKeyboardMarkup(row_width=2)
    mk.add(
        telebot.types.InlineKeyboardButton("🔍 搜索影视记录", callback_data="rec_search"),
        telebot.types.InlineKeyboardButton("🧹 清空所有影视", callback_data="rec_clear"),
    )
    mk.add(telebot.types.InlineKeyboardButton("🔙 返回主菜单", callback_data="menu_start"))
    bot.reply_to(message, "📋 记录管理面板已开启。\n请在下方选择您要进行的操作：", reply_markup=mk)

@bot.callback_query_handler(func=lambda c: c.data == "rec_search")
@admin_only
def cb_rec_search(call):
    bot.answer_callback_query(call.id)
    search_record_start(_fake_msg_from_call(call))

@bot.callback_query_handler(func=lambda c: c.data == "rec_clear")
@admin_only
def cb_rec_clear(call):
    bot.answer_callback_query(call.id)
    clear_all_records(_fake_msg_from_call(call))

@bot.message_handler(func=lambda m: m.text == "🧹 清空所有影视")
@admin_only
def clear_all_records(message):
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(
        telebot.types.InlineKeyboardButton("⚠️ 确认清空", callback_data="confirm_clear_all"),
        telebot.types.InlineKeyboardButton("🚫 取消操作", callback_data="cancel_action"),
    )
    bot.reply_to(message, "🚨 **高危操作警告** 🚨\n您确定要清空数据库中的**所有**记录并销毁频道帖子吗？", reply_markup=markup, parse_mode="Markdown")

@bot.message_handler(func=lambda m: m.text == "🔍 搜索影视记录")
@admin_only
def search_record_start(message):
    uid = message.from_user.id
    user_data[uid] = {"action": "search_delete"}
    bot.reply_to(message, "🔍 请输入要搜索的影视名称：\nℹ️ 支持模糊搜索，发送 0 返回")

@bot.message_handler(commands=['del'])
@admin_only
def quick_delete(message):
    """快捷删除: /del <msg_id>"""
    uid = message.from_user.id
    parts = message.text.strip().split()
    if len(parts) < 2:
        bot.reply_to(message, "❌ 用法: <code>/del 消息ID</code>\n例如: <code>/del 12345</code>", parse_mode="HTML")
        return
    try:
        msg_id_to_del = int(parts[1])
    except ValueError:
        bot.reply_to(message, "❌ 消息ID必须是纯数字")
        return

    with db_lock:
        found_item = msg_db_index.pop(msg_id_to_del, None)
        if found_item:
            msg_db[:] = [x for x in msg_db if x.get("msg_id") != msg_id_to_del]

    if not found_item:
        bot.reply_to(message, f"⚠️ 未找到 ID 为 <code>{msg_id_to_del}</code> 的记录", parse_mode="HTML")
        return

    save_msg()

    channel_status = ""
    try:
        bot.delete_message(chat_id=CHANNEL_ID, message_id=msg_id_to_del)
        channel_status = "📢 频道帖子已同步删除"
    except Exception as e:
        channel_status = "⚠️ 频道帖子删除失败（可能已被手动删除）"
        log.warning("快捷删除频道帖子失败 %s: %s", msg_id_to_del, e)

    bot.reply_to(message,
        f"✅ 已删除：\n🎬 {found_item.get('title')}\n🏷️ {found_item.get('version_desc', '无')}\n🆔 <code>{msg_id_to_del}</code>\n\n{channel_status}",
        parse_mode="HTML")

@bot.message_handler(func=lambda m: user_data.get(m.from_user.id, {}).get("action") == "search_delete")
@admin_only
def do_search_delete(message):
    uid, kw = message.from_user.id, message.text.strip().lower()
    if kw in ["0", "退出", "quit"]:
        record_manager(message)
        return

    with db_lock:
        results = [x for x in msg_db if kw in str(x.get("title", "")).lower() or kw in str(x.get("version_desc", "")).lower()]
    if not results:
        bot.reply_to(message, f"❌ 未找到包含「{kw}」的影视记录。")
        return

    display_results = results[:10]
    text = f"🔍 找到了 {len(results)} 条相关记录 (显示前 {len(display_results)} 条)：\n\n"
    markup = telebot.types.InlineKeyboardMarkup(row_width=1)
    for item in display_results:
        msg_id = item.get("msg_id")
        title = item.get("title", "未知标题")
        version = item.get("version_desc", "未知版本")
        text += f"🎬 <b>{html.escape(str(title))}</b>\n🏷️ 版本: {html.escape(str(version))}\n🆔 ID: <code>{msg_id}</code>\n〰️〰️〰️〰️〰️〰️〰️〰️\n"
        markup.add(telebot.types.InlineKeyboardButton(f"🗑️ 删除: {title} ({version})", callback_data=f"delrec_{msg_id}"))
    markup.add(telebot.types.InlineKeyboardButton("🔙 取消并退出搜索", callback_data="cancel_action"))
    bot.send_message(uid, text, parse_mode="HTML", reply_markup=markup)

@bot.callback_query_handler(func=lambda c: c.data.startswith("delrec_") or c.data in ["confirm_clear_all", "cancel_action"])
@admin_only
def record_mgmt_callback(call):
    uid = call.from_user.id
    action = call.data

    if action == "cancel_action":
        bot.edit_message_text("✅ 操作已取消", uid, call.message.id)
        user_data.pop(uid, None)
        return

    if action == "confirm_clear_all":
        bot.edit_message_text("⏳ 正在批量清理频道消息和本地数据，请稍候...", uid, call.message.id)
        deleted_count = 0
        failed_count = 0
        with db_lock:
            items_to_delete = list(msg_db)  # 副本
        for item in items_to_delete:
            msg_id = item.get("msg_id")
            if msg_id:
                try:
                    bot.delete_message(chat_id=CHANNEL_ID, message_id=msg_id)
                    deleted_count += 1
                except Exception:
                    failed_count += 1

        with db_lock:
            msg_db.clear()
            msg_db_index.clear()
        save_msg_sync()

        bot.edit_message_text(
            f"🧹 终极清理完成！\n\n✅ 成功删除频道帖子: {deleted_count} 条\n⚠️ 跳过/失效帖子: {failed_count} 条\n📂 本地数据已彻底清空。",
            uid, call.message.id,
        )
        user_data.pop(uid, None)
        start(call.message)
        return

    if action.startswith("delrec_"):
        try:
            msg_id_to_del = int(action.split("_")[1])
        except ValueError:
            return

        with db_lock:
            found_item = msg_db_index.pop(msg_id_to_del, None)
            if found_item:
                msg_db[:] = [x for x in msg_db if x.get("msg_id") != msg_id_to_del]
        if not found_item:
            bot.answer_callback_query(call.id, "⚠️ 此记录已被删除。")
            bot.edit_message_text("⚠️ 此记录似乎已被删除，请重新搜索。", uid, call.message.id)
            return

        save_msg()

        channel_del_status = ""
        try:
            bot.delete_message(chat_id=CHANNEL_ID, message_id=msg_id_to_del)
            channel_del_status = "📢 频道帖子也已同步销毁。"
        except Exception as e:
            channel_del_status = "⚠️ 频道帖子销毁失败"
            log.warning("删除频道帖子失败 %s: %s", msg_id_to_del, e)

        bot.edit_message_text(
            f"✅ 已成功删除：\n🎬 {found_item.get('title')}\n🏷️ {found_item.get('version_desc')}\n\n{channel_del_status}",
            uid, call.message.id,
        )
        bot.answer_callback_query(call.id, "删除成功！")

# ===================== TMDB 工具函数 =====================
def parse_tmdb_id(input_str: str) -> Optional[dict]:
    pattern = r'(?:https?://www\.themoviedb\.org/)?(tv|movie)/(\d+)'
    match = re.search(pattern, input_str)
    if match:
        return {"type": match.group(1), "id": match.group(2)}
    if input_str.strip().isdigit():
        return {"type": None, "id": input_str.strip()}
    return None

def get_tmdb_data_by_id(tmdb_type: str, tmdb_id: str) -> Optional[dict]:
    url = f"https://api.themoviedb.org/3/{tmdb_type}/{tmdb_id}?api_key={TMDB_API_KEY}&language=zh-CN"
    return tmdb_cached_get(url, timeout=15)

# ===================== 天翼云盘解析 =====================
def build_189_link_with_pwd(short_url: str, access_code: str) -> str:
    """将短链转为带访问码的长链，打开时自动填入密码"""
    if not access_code:
        return short_url
    try:
        # 先重定向拿到 share code
        resp = http.get(short_url, allow_redirects=True, timeout=10)
        actual_url = resp.url
        match = re.search(r'code=(\w+)', actual_url)
        if match:
            return f"https://cloud.189.cn/web/share?code={match.group(1)}&accessCode={access_code}"
    except Exception:
        pass
    return short_url

def format_file_size(size_bytes: int) -> str:
    """字节转人类可读格式: 1.23GB / 456MB / 789KB"""
    if size_bytes >= 1024**3:
        return f"{size_bytes / 1024**3:.2f}GB"
    elif size_bytes >= 1024**2:
        return f"{size_bytes / 1024**2:.2f}MB"
    elif size_bytes >= 1024:
        return f"{size_bytes / 1024:.2f}KB"
    else:
        return f"{size_bytes}B"

def _rand_hex(n: int = 32) -> str:
    """生成随机 hex 字符串"""
    return hashlib.md5(str(random.random()).encode()).hexdigest()

def _build_189_headers(share_code: str) -> Tuple[Dict, Dict]:
    """构建天翼云盘 checkAccessCode 所需的 Cookie 和 headers（反爬）"""
    cookies = {
        "apm_key": "9B076F2BC3DF34EAD61392ABC7B33279",
        "apm_uid": _rand_hex(),
        "apm_ct": time.strftime("%Y%m%d%H%M%S000"),
        "apm_sid": _rand_hex(),
        "apm_ua": _rand_hex(),
    }
    headers = {
        "browser-id": _rand_hex(),
        "sign-type": "1",
        "Accept": "application/json;charset=UTF-8",
        "Referer": f"https://cloud.189.cn/web/share?code={share_code}",
        "Origin": "https://cloud.189.cn",
        "X-Requested-With": "XMLHttpRequest",
    }
    return cookies, headers

def _list_dir_recursive(file_id: str, share_dir_file_id: str, share_id: str,
                        share_mode: str, share_code: str, access_code: str = "",
                        visited: Optional[set] = None) -> Tuple[int, int]:
    """递归列出共享目录下的所有文件，返回 (总大小Bytes, 文件数)"""
    if visited is None:
        visited = set()
    dir_key = str(share_dir_file_id)
    if dir_key in visited:
        return 0, 0, 0
    visited.add(dir_key)

    total = 0
    file_count = 0
    max_ep = 0
    sub_dirs = []  # (folder_id, folder_name) 子目录列表
    page = 1
    while True:
        params = {
            "pageNum": page, "pageSize": 60,
            "fileId": share_dir_file_id,        # 子文件夹时与 shareDirFileId 相同
            "shareDirFileId": share_dir_file_id,
            "isFolder": "true",
            "shareId": share_id, "shareMode": share_mode or "1",
            "iconOption": 5,
            "orderBy": "lastOpTime", "descending": "true",
        }
        if access_code:
            params["accessCode"] = access_code
        dir_resp = curl_requests.get(
            "https://cloud.189.cn/api/open/share/listShareDir.action",
            params=params,
            impersonate="chrome131",
            headers={"Referer": f"https://cloud.189.cn/web/share?code={share_code}"},
            timeout=10,
        )
        dir_root = ET.fromstring(dir_resp.text)

        # XML 中文件夹用 <folder> 标签，文件用 <file> 标签
        for folder in dir_root.findall(".//folder"):
            fid = folder.findtext("id") or ""
            fname = folder.findtext("name") or ""
            if fid and fid != dir_key:
                sub_dirs.append((fid, fname))

        for f in dir_root.findall(".//file"):
            total += int(f.findtext("size") or 0)
            file_count += 1
            fname = f.findtext("name") or ""
            m = re.search(r'[Ee](\d+)', fname)
            if m:
                ep = int(m.group(1))
                if ep > max_ep:
                    max_ep = ep

        entries = dir_root.findall(".//file") + dir_root.findall(".//folder")
        if len(entries) < 60:
            break
        page += 1

    # 递归进入子目录
    for fid, fname in sub_dirs:
        sub_size, sub_count, sub_max = _list_dir_recursive(
            file_id, fid, share_id, share_mode,
            share_code, access_code, visited,
        )
        total += sub_size
        file_count += sub_count
        max_ep = max(max_ep, sub_max)
        log.info("子目录 %s → %s (%d文件, 最大E%d)", fname, format_file_size(sub_size), sub_count, sub_max)

    return total, file_count, max_ep

def parse_189_share_details(url_str: str, access_code: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """解析天翼云盘分享链接，返回文件名和文件大小（含子目录递归）"""
    try:
        # 短链重定向
        if "/t/" in url_str:
            resp = http.get(url_str, allow_redirects=True, timeout=10)
            url_str = resp.url

        match = re.search(r'code=(\w+)', url_str)
        if not match:
            return None
        share_code = match.group(1)

        # Step 1: getShareInfoByCodeV2 (XML) — 域名 api.cloud.189.cn
        resp = curl_requests.get(
            "https://api.cloud.189.cn/open/share/getShareInfoByCodeV2.action",
            params={"shareCode": share_code},
            impersonate="chrome131",
            timeout=10,
        )
        root = ET.fromstring(resp.text)
        share_name = root.findtext("fileName") or ""
        is_folder = root.findtext("isFolder") == "true"
        file_id = root.findtext("fileId") or ""
        share_id = root.findtext("shareId") or ""          # shareMode=1 时不返回
        share_mode = root.findtext("shareMode") or ""

        if not share_name:
            return None

        # 生成反爬 Cookie + headers（整个 session 共用）
        cookies, extra_headers = _build_189_headers(share_code)

        # Step 2 (仅 shareMode=1): 验证访问码获取 shareId
        if not share_id and access_code and share_mode == "1":
            r = curl_requests.get(
                "https://cloud.189.cn/api/open/share/checkAccessCode.action",
                params={
                    "shareCode": share_code,
                    "accessCode": access_code,
                    "uuid": str(uuid_lib.uuid4()),
                },
                cookies=cookies,
                headers=extra_headers,
                impersonate="chrome131",
                timeout=10,
            )
            data = r.json()
            if data.get("res_code") == 0:
                share_id = str(data.get("shareId", ""))
                log.info("checkAccessCode 获取 shareId=%s", share_id)

        file_size = 0
        file_count = 0
        # Step 3: 文件夹则递归列出所有文件计算总大小
        if is_folder and file_id and share_id:
            file_size, file_count, max_ep = _list_dir_recursive(
                file_id, file_id, share_id, share_mode,
                share_code, access_code or "",
            )

        log.info("天翼解析: name=%s size=%s", share_name, format_file_size(file_size))
        return {"name": share_name, "size": file_size, "count": max_ep or file_count} if share_name else None

    except Exception as e:
        log.warning("解析天翼云盘链接失败: %s", e)
        return None

def _normalize_episode(episode: str) -> str:
    """归一化集数: E6→E06, S1→S01, E01-E6→E01-E06"""
    episode = re.sub(r'([Ss])(\d)(?!\d)', r'\g<1>0\g<2>', episode)
    episode = re.sub(r'([Ee])(\d)(?!\d)', r'\g<1>0\g<2>', episode)
    episode = re.sub(r'([Ee]\d+)-(\d)(?!\d)', r'\g<1>-0\g<2>', episode)
    return episode

def _simplify_completed(version: str) -> str:
    """已完结时简化版本: S01E01-E46 已完结 2160P → S01 2160P"""
    s_match = re.search(r'[Ss]\d{2}', version)
    if not s_match:
        return version
    season = s_match.group(0)
    rest = version
    rest = re.sub(r'[Ss]\d{2}', '', rest)                    # 去季数
    rest = re.sub(r'[Ee]\d+[-–]?[Ee]?\d*', '', rest)        # 去集数范围
    rest = re.sub(r'更新中|已完结', '', rest)                 # 去状态
    rest = re.sub(r'\s{2,}', ' ', rest).strip()
    return f"{season} {rest}".strip()

def _replace_ep_in_text(text: str, ep_padded: str, _version: str = "") -> str:
    """替换文本中的集数: E05→E12 或 E01-E05→E01-E12, 只替换第一处"""
    result = re.sub(r'E\d+-\d+', f'E01-E{ep_padded}', text, count=1)
    if result == text:
        result = re.sub(r'E\d+(?![\d-])', f'E{ep_padded}', text, count=1)
    return result

def extract_meta_from_title(title: Optional[str], file_size: int = 0) -> Dict[str, Any]:
    """从文件名提取元数据。大小规则: 文件名有标记→API大小|文件名大小; 无标记→API大小"""
    if not title:
        return {"name": "未知影片", "year": "", "episode": "默认版本", "share_type": "0KB", "tmdb_id": None}

    title = re.sub(r'\.(mp4|mkv|mov|avi|zip|rar|tar|cas)$', '', str(title), flags=re.IGNORECASE).strip()
    name = title
    year, episode, share_type, tmdb_id = "", "默认版本", "0KB", None

    # API 返回的文件大小 (.cas 索引文件通常很小)
    api_size_str = format_file_size(file_size) if file_size > 0 else ""

    # 文件名中的大小标记 — 只取 MB/GB/TB (忽略 KB/B)
    filename_size_str = ""
    size_pattern = re.compile(r'[-_\s]?\[?((?:\d+(?:\.\d+)?\s*[MmGgTt][Bb]?(?:[|/]\d+(?:\.\d+)?\s*[MmGgTt][Bb]?)*)(?:/?集)?)\]?$')
    size_match = size_pattern.search(name)
    if size_match:
        raw = size_match.group(1)
        if "集" in raw:
            raw = raw.replace("集", "").replace("/", "").strip() + "/集"
        filename_size_str = raw
        name = name[:size_match.start()].strip()
        name = re.sub(r'[-_\s]+$', '', name)

    # 拼接最终大小
    if filename_size_str and api_size_str:
        share_type = f"{api_size_str} | {filename_size_str}"
    elif filename_size_str:
        share_type = filename_size_str
    elif api_size_str:
        share_type = api_size_str
    # else: 保持 "0KB"

    # TMDB ID
    tmdb_match = re.search(r'[\{\[]tmdb[-_](\d+)[\}\]]', name, re.IGNORECASE)
    if tmdb_match:
        tmdb_id = tmdb_match.group(1)
        name = f"{name[:tmdb_match.start()].strip()} {name[tmdb_match.end():].strip()}".strip()

    # 年份 + 集数
    year_match = re.search(r'[\(\[（【\s.\-](\d{4})[\)\]）】\s.\-]?', name)
    if year_match:
        year = year_match.group(1)
        episode_raw = name[year_match.end():].strip()
        # 名字末尾的 S01/E01 移到集数前面 (如: Show.S01.2026.xxx → Show.2026.S01.xxx)
        season_prefix = re.search(r'\.?([Ss]\d{1,2}|[Ee]\d{1,3})$', name[:year_match.start()])
        if season_prefix:
            episode_raw = season_prefix.group(1) + '.' + episode_raw if episode_raw else season_prefix.group(1)
        if episode_raw:
            episode = re.sub(r'^[-_.\s]+|[-_.\s]+$', '', episode_raw)
        name = re.sub(r'\.?[Ss]\d{1,2}$|\.?[Ee]\d{1,3}$', '', name[:year_match.start()]).strip().rstrip('.')
    else:
        spec_match = re.search(r'(S\d+|E\d+|已完结|更新中|内嵌|1080P|2160P|4K|蓝光|BD|原盘)', name, re.IGNORECASE)
        if spec_match:
            episode = name[spec_match.start():].strip()
            name = re.sub(r'^[-_\s]+|[-_\s]+$', '', name[:spec_match.start()].strip())

    if not episode:
        episode = "默认版本"
    else:
        episode = _normalize_episode(episode)
    return {"name": name, "year": year, "episode": episode, "share_type": share_type, "tmdb_id": tmdb_id}

# ===================== 投稿系统 =====================
@bot.message_handler(commands=['cancel'])
def cmd_cancel(message):
    uid = message.from_user.id
    reset_user_state(uid)
    bot.reply_to(message, "✅ 已取消")

@bot.message_handler(func=lambda m: m.text and "cloud.189.cn" in m.text)
def quick_submit(message):
    """对话框直接发链接一键投稿"""
    uid = message.from_user.id
    if not is_admin(uid):
        mk = telebot.types.InlineKeyboardMarkup()
        mk.add(
            telebot.types.InlineKeyboardButton("📝 申请权限", callback_data="apply_req"),
            telebot.types.InlineKeyboardButton("❌ 取消", callback_data="apply_cancel"),
        )
        bot.reply_to(message, "🔒 你还不是管理员，无法投稿\n是否需要申请管理员权限？", reply_markup=mk)
        return
    reset_user_state(uid)
    display_name = message.from_user.full_name or message.from_user.first_name or str(uid)
    display_username = message.from_user.username or ""
    user_data[uid] = {"step": "link", "display_name": display_name, "display_username": display_username}
    cancel_submit_timer(uid)
    submit_flow(message)  # 直接解析链接

@bot.message_handler(commands=['submit'])
@bot.message_handler(func=lambda m: m.text == "📤 我要投稿")
def submit_start(message):
    uid = message.from_user.id
    if not is_admin(uid):
        mk = telebot.types.InlineKeyboardMarkup()
        mk.add(
            telebot.types.InlineKeyboardButton("📝 申请权限", callback_data="apply_req"),
            telebot.types.InlineKeyboardButton("❌ 取消", callback_data="apply_cancel"),
        )
        bot.reply_to(message, "🔒 你还不是管理员，无法投稿\n是否需要申请管理员权限？", reply_markup=mk)
        return
    reset_user_state(uid)
    display_name = message.from_user.full_name or message.from_user.first_name or str(uid)
    display_username = message.from_user.username or ""
    user_data[uid] = {"step": "link", "display_name": display_name, "display_username": display_username}
    bot.reply_to(message, "📤 智能发布流已开启\n请发送带有链接（及访问码）的文本：")

@bot.callback_query_handler(func=lambda c: c.data == "apply_req")
def cb_apply_req(call):
    """申请权限 → 进入理由输入"""
    bot.answer_callback_query(call.id)
    apply_submit(_fake_msg_from_call(call))

@bot.callback_query_handler(func=lambda c: c.data == "apply_cancel")
def cb_apply_cancel(call):
    """取消申请"""
    bot.answer_callback_query(call.id)
    bot.edit_message_text("✅ 已取消", call.message.chat.id, call.message.id)

def generate_auto_preview(uid: int) -> None:
    """生成发帖预览"""
    try:
        data = user_data.get(uid, {})
        data["step"] = "preview"
        name = data.get("name") or ""
        year = data.get("year") or ""
        search_type = data.get("search_type") or "tv"
        share_type = data.get("share_type") or "0KB"
        episode = data.get("episode") or "默认版本"

        img = DEFAULT_IMAGE
        intro, genres = "暂无简介", []

        # 查询 TMDB
        try:
            if data.get("tmdb_id"):
                res_data = get_tmdb_data_by_id(search_type, data["tmdb_id"])
                results = [res_data] if res_data and "status_code" not in res_data else []
            else:
                q = urllib.parse.quote(name)
                url = f"https://api.themoviedb.org/3/search/{search_type}?api_key={TMDB_API_KEY}&query={q}&language=zh-CN"
                res_data = tmdb_cached_get(url, timeout=10)
                results = res_data.get("results", []) if res_data else []

            if results:
                m = results[0]
                media_id = m.get("id")
                if m.get("backdrop_path"):
                    img = "https://image.tmdb.org/t/p/original" + m.get("backdrop_path")
                elif m.get("poster_path"):
                    img = "https://image.tmdb.org/t/p/original" + m.get("poster_path")

                fetched_intro = m.get("overview")
                if fetched_intro:
                    intro = str(fetched_intro)

                if not year:
                    year = str(m.get("release_date", ""))[:4] if search_type == "movie" else str(m.get("first_air_date", ""))[:4]
                if media_id:
                    detail_url = f"https://api.themoviedb.org/3/{search_type}/{media_id}?api_key={TMDB_API_KEY}&language=zh-CN"
                    detail = tmdb_cached_get(detail_url, timeout=5)
                    if detail:
                        genres = [g.get("name") for g in detail.get("genres", []) if g.get("name")]
            # 用 TMDB 中文名替换英文名
            if results:
                tmdb_name = results[0].get("title") or results[0].get("name")
                if tmdb_name and not re.search(r'[一-鿿]', name):
                    name = str(tmdb_name)
        except Exception as e:
            log.warning("生成预览 TMDB 查询失败: %s", e)

        full_name = f"{name} ({year})" if year else name
        data["full_name"] = full_name

        if len(intro) > 500:
            intro = intro[:500] + "..."

        caption = f"🎬 <b>{html.escape(str(full_name))}</b>\n"
        display_ep = str(episode).replace('更新中', '').strip().replace('  ', ' ')
        if display_ep and display_ep != "默认版本":
            caption += f"<blockquote>{html.escape(display_ep)}</blockquote>\n"
        caption += "🎯 简介：\n"
        caption += f"<blockquote expandable>{html.escape(str(intro))}</blockquote>\n"

        # 其他版本
        with db_lock:
            other_versions = [
                x for x in msg_db
                if x.get("title") == full_name and x.get("link") != data.get("link") and "转发自" not in (x.get("caption") or "")
            ]
        if other_versions and "更新中" not in episode \
                and not any("更新中" in v.get("version_desc", "") for v in other_versions):
            caption += "其他版本：\n"
            for v in other_versions:
                vd = str(v.get('version_desc', '')).replace('更新中', '').strip().replace('  ', ' ')
                vd = vd if vd and vd != "默认版本" else "📎 查看"
                caption += f"<blockquote><a href=\"{CHANNEL_LINK}/{v.get('msg_id', '')}\">{html.escape(vd)}</a></blockquote>\n"

        tag_name = f"#{str(name).replace(' ','').replace('(','').replace(')','').replace('-','')}"
        tag_genres = " ".join([f"#{str(g).replace(' ','').replace('-','')}" for g in genres[:3]]) if genres else ""
        caption += f"🏷️ {tag_name} {tag_genres}\n" if tag_genres else f"🏷️ {tag_name}\n"
        if share_type:
            caption += f"📦 <code>{html.escape(str(share_type))}</code>"

        data["same_name_same_link"] = next(
            (x for x in msg_db if x.get("title") == full_name and x.get("link") == data.get("link")), None
        )

        # 作者信息 (可点击跳转)
        author_name = data.get("author_name", "")
        author_link = data.get("author_link", "")
        if uid == SUPER_ADMIN:
            if author_name:
                if author_link:
                    caption += f"\n✍️ 作者：<a href='{html.escape(str(author_link))}'>{html.escape(str(author_name))}</a>"
                else:
                    caption += f"\n✍️ 作者：{html.escape(str(author_name))}"
        else:
            dname = data.get("display_name", "")
            duser = data.get("display_username", "")
            if duser:
                caption += f"\n✍️ 作者：<a href='https://t.me/{html.escape(str(duser))}'>{html.escape(str(dname))}</a>"
            else:
                caption += f"\n✍️ 作者：{html.escape(str(dname))}"

        data["preview_img"] = img
        data["preview_caption"] = caption

        markup = telebot.types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            telebot.types.InlineKeyboardButton("🟢 确认发布", callback_data="publish"),
            telebot.types.InlineKeyboardButton("✏️ 修改版本", callback_data="manual_input"),
        )
        row2 = [
            telebot.types.InlineKeyboardButton("🔄 重新识别", callback_data="retry_tmdb"),
            telebot.types.InlineKeyboardButton("📦 修改大小", callback_data="edit_size"),
        ]
        if uid == SUPER_ADMIN:
            row2.append(telebot.types.InlineKeyboardButton("👤 添加作者" if not author_name else "✍️ 修改作者", callback_data="set_author"))
        row2.append(telebot.types.InlineKeyboardButton("🔴 取消退出", callback_data="cancel"))
        markup.add(*row2)

        try:
            bot.send_photo(uid, img, caption=caption, reply_markup=markup, parse_mode="HTML")
        except Exception as e:
            log.warning("海报发送失败, 降级纯文本: %s", e)
            bot.send_message(uid, f"⚠️ 海报渲染失败，已安全降级为纯文本预览：\n\n{caption}", reply_markup=markup, parse_mode="HTML")

    except Exception as outer_e:
        err_str = traceback.format_exc()
        try:
            bot.send_message(uid, f"❌ 机器人内部严重错误！\n无法渲染卡片，请截图以下报错信息排查：\n<pre>{html.escape(err_str)}</pre>", parse_mode="HTML")
        except Exception:
            log.error("预览生成崩溃: %s", outer_e)

# 投稿流程状态机
@bot.message_handler(func=lambda m: user_data.get(m.from_user.id, {}).get("step") in [
    "link", "manual_name", "manual_type", "manual_episode_input", "manual_size", "waiting_episode_num",
])
@admin_only
def submit_flow(message):
    uid, text = message.from_user.id, message.text.strip()
    cancel_submit_timer(uid)

    data = user_data.get(uid)
    if not data:
        return
    step = data.get("step")

    if text.lower() in ["退出", "quit", "0", "exit"]:
        user_data.pop(uid, None)
        bot.reply_to(message, "✅ 已退出投稿模式")
        start(message)
        return

    # --- Step: link ---
    if step == "link":
        if "189.cn" in text:
            # 提取纯链接 (遇到中文/全角括号/空格即停)
            link_match = re.search(r'(https?://cloud\.189\.cn/[^\s（）\(\)一-鿿]+)', text)
            # 提取访问码 (支持: 访问码：xxxx / 密码: xxxx / (访问码：xxxx) 等)
            pwd_match = re.search(r'(?:密码|访问码|提取码)[:：\s]*([a-zA-Z0-9]{4})', text)
            if not link_match:
                bot.reply_to(message, "❌ 无法提取有效链接，请检查格式。")
                return

            clean_link = link_match.group(1).rstrip('.,;，。；）)')
            access_code = pwd_match.group(1) if pwd_match else None
            data["link"] = clean_link
            if access_code:
                data["access_code"] = access_code
                # 把访问码直接拼在链接后面作为按钮 URL
                data["link"] = f"{clean_link}（访问码：{access_code}）"

            bot.send_message(uid, f"⚡ 正在智能读取并分析天翼网盘数据...{' (已启用密码解析)' if access_code else ''}")
            result = parse_189_share_details(clean_link, access_code)

            if result:
                fetched_title = result["name"]
                fetched_size = result.get("size", 0)
                meta = extract_meta_from_title(fetched_title, file_size=fetched_size)
                data.update({
                    "name": meta["name"], "year": meta["year"],
                    "episode": meta["episode"], "share_type": meta.get("share_type", "0KB"),
                    "tmdb_id": meta["tmdb_id"],
                    "search_type": "tv" if any(x in fetched_title for x in ["剧", "季"]) or re.search(r'[SE]\d+', fetched_title) else "movie",
                    "raw_fetched_title": fetched_title,
                    "step": "choose_status",
                })
                markup = telebot.types.InlineKeyboardMarkup(row_width=2)
                markup.add(
                    telebot.types.InlineKeyboardButton("🟢 已完结", callback_data="status_completed"),
                    telebot.types.InlineKeyboardButton("🔄 更新中", callback_data="status_updating"),
                )
                size_hint = f"  📦 {meta['share_type']}" if fetched_size > 0 or meta.get('share_type', '0KB') != '0KB' else ""
                bot.reply_to(message, f"🎉 链接解析成功！\n🎬 识别名称：{data['name']}{size_hint}\n\n请选择该影视的当前状态：", reply_markup=markup)
            else:
                data["step"] = "manual_name"
                bot.reply_to(message, "⚠️ 自动解析受限。已为您降级切换至【手动投稿模式】。\n请输入影视名称或 TMDB ID：")
        else:
            bot.reply_to(message, "❌ 识别失败，请发送合规的天翼网盘链接。")
        return

    # --- Step: manual_name ---
    if step == "manual_name":
        tmdb_info = parse_tmdb_id(text)
        if tmdb_info:
            if not tmdb_info["type"]:
                data["tmdb_id"] = tmdb_info["id"]
                # 尝试两个类型
                for t in ["tv", "movie"]:
                    td = get_tmdb_data_by_id(t, tmdb_info["id"])
                    if td:
                        data["search_type"] = t
                        data["name"] = td.get("name") or td.get("title", "")
                        data["year"] = str(td.get("first_air_date", ""))[:4] if t == "tv" else str(td.get("release_date", ""))[:4]
                        bot.send_message(uid, "✅ 生成预览中...")
                        generate_auto_preview(uid)
                        return
                bot.reply_to(message, "❌ 无法获取该 TMDB 信息，请检查 ID")
            else:
                tmdb_data = get_tmdb_data_by_id(tmdb_info["type"], tmdb_info["id"])
                if not tmdb_data:
                    bot.reply_to(message, "❌ 无法获取该 TMDB 信息，请重新输入：")
                    return
                data["name"] = tmdb_data.get("name") or tmdb_data.get("title")
                data["year"] = str(tmdb_data.get("first_air_date", ""))[:4] if tmdb_info["type"] == "tv" else str(tmdb_data.get("release_date", ""))[:4]
                data["search_type"] = tmdb_info["type"]
                bot.send_message(uid, "✅ 生成预览中...")
                generate_auto_preview(uid)
                return
        else:
            year_match = re.search(r'(\d{4})$', text)
            if year_match:
                data["year"] = year_match.group(1)
                data["name"] = text[:-4].strip()
            else:
                data["name"], data["year"] = text, ""
            # 搜 TMDB 列出选项
            all_retry = []
            for stype in ["tv", "movie"]:
                try:
                    url = f"https://api.themoviedb.org/3/search/{stype}?api_key={TMDB_API_KEY}&query={urllib.parse.quote(text)}&language=zh-CN"
                    res = tmdb_cached_get(url, timeout=10)
                    for r in (res.get("results", []) if res else [])[:3]:
                        yr = str(r.get("first_air_date" if stype == "tv" else "release_date", ""))[:4]
                        all_retry.append({"type": stype, "id": r["id"], "name": r.get("name") or r.get("title", ""), "year": yr})
                except Exception:
                    pass
            if all_retry:
                retry_cache[uid] = all_retry
                mk = telebot.types.InlineKeyboardMarkup(row_width=1)
                for r in all_retry[:6]:
                    label = f"{'📺' if r['type']=='tv' else '🎬'} {r['name']} ({r['year']})"
                    mk.add(telebot.types.InlineKeyboardButton(label, callback_data=f"rtry_{all_retry.index(r)}"))
                bot.reply_to(message, "🔍 TMDB 搜索结果，请选择：", reply_markup=mk)
            else:
                data["search_type"] = "tv" if any(x in text for x in ["剧", "季"]) or re.search(r'[SE]\d+', text) else "movie"
                bot.send_message(uid, "✅ 生成预览中...")
                generate_auto_preview(uid)
            return

    # --- Step: manual_type ---
    if step == "manual_type":
        if text not in ["🎬 电影", "📺 电视剧"]:
            bot.reply_to(message, "❌ 请点击键盘上的按钮选择类型！")
            return
        data["search_type"] = "movie" if text == "🎬 电影" else "tv"

        if data.get("tmdb_id"):
            tmdb_data = get_tmdb_data_by_id(data["search_type"], data["tmdb_id"])
            if not tmdb_data:
                data["step"] = "manual_name"
                bot.reply_to(message, "❌ 抓取失败，请重新输入名称：")
                return
            data["name"] = tmdb_data.get("name") or tmdb_data.get("title")
            data["year"] = str(tmdb_data.get("first_air_date", ""))[:4] if data["search_type"] == "tv" else str(tmdb_data.get("release_date", ""))[:4]

        bot.send_message(uid, "✅ 生成预览中...")
        generate_auto_preview(uid)
        return

    # --- Step: manual_episode_input ---
    if step == "manual_episode_input":
        data["episode"] = _normalize_episode(text) if text else "默认版本"
        if not data.pop("_from_preview", False):
            bot.send_message(uid, f"✅ 版本信息：{data['episode']}")
        else:
            bot.send_message(uid, f"✅ 版本已修改: {data['episode']}")
        generate_auto_preview(uid)
        return

    # --- Step: manual_size ---
    if step == "manual_size":
        data["share_type"] = text if text else "0KB"
        bot.send_message(uid, f"📦 大小已设为: {text}")
        generate_auto_preview(uid)
        return

    # --- Step: waiting_episode_num ---
    if step == "waiting_episode_num":
        if not text.isdigit():
            bot.reply_to(message, "⚠️ 格式错误，请只输入纯数字集数（例如：16 ）：")
            return
        raw_title = data.get("raw_fetched_title", "")
        old_ep = data.get("episode", "")
        season_match = re.search(r'([Ss]\d+)', raw_title + old_ep)
        season_str = season_match.group(1).upper() if season_match else "S01"
        if len(season_str) == 2:
            season_str = f"S0{season_str[1]}"
        # 保留所有原有版本信息
        rest = old_ep
        rest = re.sub(r'\b[Ee]\d+[-–]?[Ee]?\d*\b', '', rest)  # 去旧集数
        rest = re.sub(r'\b[Ss]\d+\b', '', rest)                # 去旧季数
        rest = re.sub(r'更新中|已完结', '', rest)               # 去旧状态
        rest = re.sub(r'\s{2,}', ' ', rest).strip()
        data["episode"] = f"{season_str}E01-E{int(text):02d} 更新中{' ' + rest if rest else ''}"
        bot.send_message(uid, f"✅ 自动集数拼装成功！最新版本：{data['episode']}\n\n🔍 正在生成发帖预览...")
        generate_auto_preview(uid)
        return

# ===================== 投稿回调 =====================
@bot.callback_query_handler(func=lambda c: c.data in ["type_movie", "type_tv"])
@admin_only
def cb_manual_type(call):
    """手动投稿中的类型选择 (电影/电视剧)"""
    bot.answer_callback_query(call.id)
    # 构造假消息模拟文本输入
    msg = _fake_msg_from_call(call)
    msg.text = "🎬 电影" if call.data == "type_movie" else "📺 电视剧"
    submit_flow(msg)

@bot.callback_query_handler(func=lambda c: c.data in ["status_completed", "status_updating"])
@admin_only
def callback_status_selection(call):
    uid = call.from_user.id
    data = user_data.get(uid)
    if not data or data.get("step") != "choose_status":
        bot.answer_callback_query(call.id, "操作已过期，请重新使用 /submit 投稿。")
        return

    if call.data == "status_completed":
        bot.edit_message_text("🎉 您选择了【已完结】！直接生成发帖预览...", uid, call.message.id)
        generate_auto_preview(uid)
    elif call.data == "status_updating":
        data["step"] = "waiting_episode_num"
        bot.edit_message_text("📥 您选择了【更新中】。\n请直接回复输入当前更新到了第几集（纯数字，如：16）：", uid, call.message.id)

@bot.callback_query_handler(func=lambda c: c.data == "retry_tmdb")
@admin_only
def cb_retry_tmdb(call):
    """重新识别 — 允许输入新名称查询 TMDB"""
    uid = call.from_user.id
    data = user_data.get(uid)
    if not data:
        bot.answer_callback_query(call.id, "操作已过期")
        return
    data["step"] = "retry_name"
    bot.answer_callback_query(call.id)
    bot.send_message(uid, "🔄 请输入正确的影视名称 (中文/英文/TMDB ID)：")
    try:
        bot.delete_message(uid, call.message.message_id)
    except Exception:
        pass

@bot.message_handler(func=lambda m: user_data.get(m.from_user.id, {}).get("step") == "retry_name")
@admin_only
def do_retry_tmdb(message):
    uid, text = message.from_user.id, message.text.strip()

    # TMDB ID 直接识别
    tmdb_info = parse_tmdb_id(text)
    if tmdb_info and tmdb_info["type"]:
        data = user_data[uid]
        data["search_type"] = tmdb_info["type"]
        data["tmdb_id"] = tmdb_info["id"]
        tmdb_data = get_tmdb_data_by_id(tmdb_info["type"], tmdb_info["id"])
        if tmdb_data:
            data["name"] = tmdb_data.get("name") or tmdb_data.get("title") or data.get("name", "")
        generate_auto_preview(uid)
        return

    # 搜 TV + Movie，列出选项
    data = user_data[uid]
    all_results = []
    for stype in ["tv", "movie"]:
        try:
            url = f"https://api.themoviedb.org/3/search/{stype}?api_key={TMDB_API_KEY}&query={urllib.parse.quote(text)}&language=zh-CN"
            res = tmdb_cached_get(url, timeout=10)
            for r in (res.get("results", []) if res else [])[:3]:
                yr = str(r.get("first_air_date" if stype == "tv" else "release_date", ""))[:4]
                all_results.append({
                    "type": stype, "id": r["id"],
                    "name": r.get("name") or r.get("title", ""),
                    "year": yr,
                })
        except Exception:
            pass

    if not all_results:
        data["name"] = text
        data["year"] = ""
        generate_auto_preview(uid)
        return

    # 存到缓存等用户选择
    retry_cache[uid] = all_results
    mk = telebot.types.InlineKeyboardMarkup(row_width=1)
    for r in all_results[:6]:
        label = f"{'📺' if r['type']=='tv' else '🎬'} {r['name']} ({r['year']})"
        mk.add(telebot.types.InlineKeyboardButton(label, callback_data=f"rtry_{all_results.index(r)}"))
    mk.add(telebot.types.InlineKeyboardButton("↩️ 重新搜索", callback_data="retry_tmdb"))
    bot.reply_to(message, "🔍 TMDB 搜索结果，请选择：", reply_markup=mk)

@bot.callback_query_handler(func=lambda c: c.data.startswith("rtry_"))
def cb_retry_select(call):
    """重新识别 — 用户选择搜索结果"""
    uid = call.from_user.id
    idx = int(call.data[5:])
    results = retry_cache.get(uid, [])
    bot.answer_callback_query(call.id)
    if idx >= len(results):
        return
    r = results[idx]
    data = user_data.get(uid)
    if data:
        data["search_type"] = r["type"]
        data["tmdb_id"] = str(r["id"])
        data["name"] = r["name"]
        data["year"] = r["year"]
        generate_auto_preview(uid)

@bot.callback_query_handler(func=lambda c: c.data == "set_author")
def cb_set_author(call):
    """超管选择作者"""
    uid = call.from_user.id
    if uid != SUPER_ADMIN:
        bot.answer_callback_query(call.id, "无权限")
        return
    bot.answer_callback_query(call.id)
    authors = _load_authors()
    mk = telebot.types.InlineKeyboardMarkup(row_width=2)
    # 预设作者按钮
    for a in authors:
        mk.add(telebot.types.InlineKeyboardButton(
            f"👤 {a['name']}", callback_data=f"author_{a['name']}|{a['link']}"))
    mk.add(
        telebot.types.InlineKeyboardButton("✍️ 手动输入", callback_data="author_custom"),
        telebot.types.InlineKeyboardButton("➕ 添加作者", callback_data="author_add"),
    )
    mk.add(
        telebot.types.InlineKeyboardButton("🗑️ 删除作者", callback_data="author_del"),
        telebot.types.InlineKeyboardButton("❌ 移除本文作者", callback_data="author_清空"),
    )
    bot.send_message(uid, "👤 选择作者：", reply_markup=mk)

@bot.callback_query_handler(func=lambda c: c.data.startswith("author_"))
def cb_author_select(call):
    """作者选择回调"""
    uid = call.from_user.id
    val = call.data[7:]  # strip "author_"
    data = user_data.get(uid)

    # 删除特定作者
    if val.startswith("del_"):
        name = val[4:]
        authors = _load_authors()
        authors = [a for a in authors if a["name"] != name]
        _save_authors(authors)
        bot.answer_callback_query(call.id, f"已删除 {name}")
        # 重新显示作者菜单
        call.data = "set_author"
        return cb_set_author(call)

    if not data:
        bot.answer_callback_query(call.id, "操作已过期")
        return
    if val == "custom":
        user_data[uid]["step"] = "waiting_author"
        bot.answer_callback_query(call.id)
        bot.send_message(uid, "👤 请输入作者名称和链接：\n格式: <code>名称 @username</code> 或 <code>名称 https://t.me/xxx</code>", parse_mode="HTML")
    elif val == "del":
        # 显示删除子菜单
        authors = _load_authors()
        mk = telebot.types.InlineKeyboardMarkup(row_width=1)
        for a in authors:
            mk.add(telebot.types.InlineKeyboardButton(
                f"🗑️ {a['name']}", callback_data=f"author_del_{a['name']}"))
        mk.add(telebot.types.InlineKeyboardButton("🔙 返回", callback_data="set_author"))
        bot.answer_callback_query(call.id)
        bot.send_message(uid, "🗑️ 选择要删除的作者：", reply_markup=mk)
        return
    elif val == "add":
        user_data[uid]["step"] = "waiting_add_author"
        bot.answer_callback_query(call.id)
        bot.send_message(uid, "➕ 请输入新作者：\n格式: <code>名称 链接</code>\n例: <code>FreeYuA https://t.me/FreeYuA</code>\n保存后会自动出现在作者列表中", parse_mode="HTML")
    elif val == "清空":
        data["author_name"] = ""
        data["author_link"] = ""
        bot.answer_callback_query(call.id, "已清空作者")
        generate_auto_preview(uid)
    else:
        # 格式: "下雨了|https://t.me/xxx"
        parts = val.split("|", 1)
        data["author_name"] = parts[0]
        data["author_link"] = parts[1] if len(parts) > 1 else ""
        bot.answer_callback_query(call.id, f"已设为: {parts[0]}")
        generate_auto_preview(uid)

@bot.message_handler(func=lambda m: user_data.get(m.from_user.id, {}).get("step") == "waiting_add_author")
@admin_only
def do_add_author(message):
    """添加新作者到预设列表"""
    uid, text = message.from_user.id, message.text.strip()
    name, link = text, ""
    m = re.search(r'(https?://t\.me/\S+|@\w+)', text)
    if m:
        link = m.group(1)
        if link.startswith("@"):
            link = f"https://t.me/{link[1:]}"
        name = text[:m.start()].strip()
    if not name:
        bot.reply_to(message, "❌ 请输入有效名称")
        return
    authors = _load_authors()
    for a in authors:
        if a["name"] == name:
            a["link"] = link
            break
    else:
        authors.append({"name": name, "link": link})
    _save_authors(authors)
    bot.send_message(uid, f"✅ 作者已保存: {name}" + (f" ({link})" if link else ""))
    user_data[uid]["step"] = "preview"
    # 重新显示作者菜单
    mk = telebot.types.InlineKeyboardMarkup(row_width=2)
    for a in authors:
        mk.add(telebot.types.InlineKeyboardButton(
            f"👤 {a['name']}", callback_data=f"author_{a['name']}|{a['link']}"))
    mk.add(
        telebot.types.InlineKeyboardButton("✍️ 手动输入", callback_data="author_custom"),
        telebot.types.InlineKeyboardButton("➕ 添加作者", callback_data="author_add"),
    )
    mk.add(
        telebot.types.InlineKeyboardButton("🗑️ 删除作者", callback_data="author_del"),
        telebot.types.InlineKeyboardButton("❌ 移除本文作者", callback_data="author_清空"),
    )
    bot.send_message(uid, "👤 作者已更新，继续选择：", reply_markup=mk)

@bot.message_handler(func=lambda m: user_data.get(m.from_user.id, {}).get("step") == "waiting_author")
def do_set_author(message):
    uid, text = message.from_user.id, message.text.strip()
    data = user_data.get(uid)
    if not data:
        return
    if text in ("清空", ""):
        data["author_name"] = ""
        data["author_link"] = ""
        bot.send_message(uid, "✅ 作者已清空")
    else:
        # 支持格式: "名称 @username" 或 "名称 https://t.me/xxx" 或 "名称"
        link = ""
        name = text
        m = re.search(r'(https?://t\.me/\S+|@\w+)', text)
        if m:
            link = m.group(1)
            if link.startswith("@"):
                link = f"https://t.me/{link[1:]}"
            name = text[:m.start()].strip()
        data["author_name"] = name
        data["author_link"] = link
        bot.send_message(uid, f"✅ 作者已设为: {name}" + (f" ({link})" if link else ""))
    generate_auto_preview(uid)

@bot.callback_query_handler(func=lambda c: c.data.startswith("sz_"))
@admin_only
def cb_size_unit(call):
    """选择大小单位 → 应用数字"""
    uid = call.from_user.id
    unit = call.data[3:]
    label = {"gb": "GB", "mb": "MB", "gbp": "GB/集", "mbp": "MB/集"}[unit]
    data = user_data.get(uid, {})
    num = data.pop("_size_num", "0")
    new_val = f"{num}{label}"
    old = data.get("share_type", "")
    if "|" in old:
        data["share_type"] = old.split("|")[0].strip() + " | " + new_val
    elif old and old != "0KB":
        data["share_type"] = old + " | " + new_val
    else:
        data["share_type"] = new_val
    data["step"] = "preview"
    bot.answer_callback_query(call.id)
    bot.send_message(uid, f"📦 大小已设为: {data['share_type']}")
    generate_auto_preview(uid)

@bot.callback_query_handler(func=lambda c: c.data == "edit_size")
@admin_only
def cb_edit_size(call):
    """快捷修改文件大小 — 先输入数字"""
    uid = call.from_user.id
    data = user_data.get(uid)
    if not data:
        bot.answer_callback_query(call.id, "操作已过期")
        return
    data["step"] = "edit_size_wait"
    data["_size_num"] = ""
    bot.answer_callback_query(call.id)
    bot.send_message(uid, "📦 请输入大小数字：")

@bot.message_handler(func=lambda m: user_data.get(m.from_user.id, {}).get("step") == "edit_size_wait")
@admin_only
def do_edit_size(message):
    uid, text = message.from_user.id, message.text.strip()
    data = user_data.get(uid)
    if not data:
        return
    # 直接输入带单位的 → 原样生效
    if not text.replace(".", "").replace("GB","").replace("MB","").replace("/集","").isdigit():
        old = data.get("share_type", "")
        if "|" in old:
            data["share_type"] = old.split("|")[0].strip() + " | " + text
        elif old and old != "0KB":
            data["share_type"] = old + " | " + text
        else:
            data["share_type"] = text
        data["step"] = "preview"
        bot.send_message(uid, f"📦 大小已设为: {data['share_type']}")
        generate_auto_preview(uid)
        return
    if not text.replace(".", "").isdigit():
        bot.reply_to(message, "❌ 请输入有效数字")
        return
    data["_size_num"] = text
    mk = telebot.types.InlineKeyboardMarkup(row_width=2)
    mk.add(
        telebot.types.InlineKeyboardButton("📦 GB", callback_data="sz_gb"),
        telebot.types.InlineKeyboardButton("📦 MB", callback_data="sz_mb"),
    )
    mk.add(
        telebot.types.InlineKeyboardButton("📦 GB/集", callback_data="sz_gbp"),
        telebot.types.InlineKeyboardButton("📦 MB/集", callback_data="sz_mbp"),
    )
    bot.send_message(uid, f"📦 {text} 选择单位：", reply_markup=mk)

@bot.callback_query_handler(func=lambda c: c.data in ["publish", "cancel", "manual_input"])
@admin_only
def callback_action(call):
    uid = call.from_user.id
    data = user_data.get(uid)
    if not data or data.get("step") != "preview":
        bot.answer_callback_query(call.id, "操作已过期，请重新使用 /submit 投稿。")
        return

    if call.data == "manual_input":
        data["step"] = "manual_episode_input"
        data["_from_preview"] = True
        try:
            bot.delete_message(uid, call.message.id)
        except Exception:
            pass
        bot.send_message(uid, "✏️ 可直接发送修改后的版本信息：\n(例如: S01E01-E16 更新中 2160P)")
        return

    if call.data == "cancel":
        bot.edit_message_caption("❌ 已取消发布，点击 /cancel 退出。", uid, call.message.id)
        reset_user_state(uid)
        return

    # --- 确认发布 ---
    if call.data == "publish":
        try:
            markup = make_channel_markup(data["link"], data.get("access_code", ""))

            # 覆盖同名同链接旧帖
            if data.get("same_name_same_link"):
                old_msg_id = data["same_name_same_link"].get("msg_id")
                if old_msg_id:
                    try:
                        bot.delete_message(chat_id=CHANNEL_ID, message_id=old_msg_id)
                    except Exception:
                        pass
                    with db_lock:
                        msg_db_index.pop(old_msg_id, None)
                        for idx, item in enumerate(msg_db):
                            if item.get("msg_id") == old_msg_id:
                                del msg_db[idx]
                                break

            sent = bot.send_photo(CHANNEL_ID, photo=data["preview_img"], caption=data["preview_caption"], reply_markup=markup, parse_mode="HTML")
            new_item = {
                "msg_id": sent.message_id,
                "title": data["full_name"],
                "caption": data["preview_caption"],
                "link": data["link"],
                "access_code": data.get("access_code", ""),
                "version_desc": data.get("episode", "默认版本"),
                "date": date.today().strftime("%Y-%m-%d"),
                "tmdb_id": data.get("tmdb_id"),
                "author_name": data.get("author_name") or data.get("display_name", ""),
                "author_link": data.get("author_link", ""),
            }
            with db_lock:
                msg_db.append(new_item)
                msg_db_index[sent.message_id] = new_item
            save_msg()

            try:
                bot.delete_message(uid, call.message.id)
            except Exception:
                pass
            user_data[uid] = {"step": "link"}
            link = f"{CHANNEL_LINK}/{sent.message_id}"
            bot.send_message(uid,
                f"🎉 投稿已完成～！连载旧贴已被覆盖替换。\n"
                f"<a href='{link}'>📎 查看帖子</a>\n"
                "📥 可继续发送链接投稿～\n💨 或短暂的离开我～ /cancel",
                parse_mode="HTML", disable_web_page_preview=True)
            start_submit_timer(uid)

        except Exception as e:
            log.error("发布失败: %s", e)
            try:
                bot.edit_message_caption(f"❌ 发布失败：{str(e)}", uid, call.message.id)
            except Exception:
                bot.send_message(uid, f"❌ 发布失败：{str(e)}")

# ===================== 频道同步 =====================
@bot.channel_post_handler(chat_id=CHANNEL_ID)
def sync_channel(message):
    if message.photo and message.caption:
        title = message.caption.split("\n")[0].strip()
        msg_id = message.message_id
        with db_lock:
            if msg_id not in msg_db_index:
                new_item = {
                    "msg_id": msg_id,
                    "title": title,
                    "caption": message.caption,
                    "link": "",
                    "version_desc": "同步版本",
                    "date": date.today().strftime("%Y-%m-%d"),
                }
                msg_db.append(new_item)
                msg_db_index[msg_id] = new_item
                save_msg()

# ===================== 🔄 Emby API 主动拉取引擎 =====================
def _refresh_file_size(link: str, access_code: str = "") -> str:
    """重新获取天翼云盘文件大小，返回格式化字符串如 '8.26KB'，失败返回空"""
    if not link or "189.cn" not in link:
        return ""
    try:
        result = parse_189_share_details(link, access_code or None)
        if result and result.get("size", 0) > 0:
            return format_file_size(result["size"])
    except Exception:
        pass
    return ""

def _update_caption_size(caption: str, new_api_size: str) -> str:
    """更新 caption 中的 API 大小，保留 | 后面的文件名大小"""
    m = re.search(r'📦 <code>([^<]*)</code>', caption)
    if m:
        old = m.group(1)
        if "|" in old:
            new_size = f"{new_api_size}|{old.split('|', 1)[1]}"
        else:
            new_size = new_api_size
        return caption[:m.start()] + f'📦 <code>{new_size}</code>' + caption[m.end():]
    return caption

def update_db_from_emby(series_name: str, season_num: int, episode_num: int) -> None:
    """匹配 Emby 新入库剧集并自动更新频道帖子"""
    with db_lock:
        items_snapshot = list(msg_db)
    for item in items_snapshot:
        if series_name.lower() not in item.get("title", "").lower():
            continue
        if "更新中" not in item.get("version_desc", ""):
            continue
        s_match = re.search(r'[Ss](\d+)', item.get("version_desc", ""))
        db_season = int(s_match.group(1)) if s_match else 1
        if db_season != season_num:
            continue

        old_version = item.get("version_desc", "")
        old_caption = item.get("caption", "")
        # 只更新到更高集数 (防止 E31 覆盖 E32)
        e_matches = re.findall(r'[Ee](\d+)', old_version)
        current_max = int(e_matches[-1]) if e_matches else 0
        if episode_num <= current_max:
            break

        new_version = _replace_ep_in_text(old_version, f"{episode_num:02d}")

        if new_version != old_version:
            old_disp = old_version.replace("更新中", "").strip().replace("  ", " ")
            new_disp = new_version.replace("更新中", "").strip().replace("  ", " ")
            new_caption = old_caption.replace(old_disp, new_disp) if old_disp in old_caption else _replace_ep_in_text(old_caption, f"{episode_num:02d}")
            # 刷新文件大小
            fresh = _refresh_file_size(item.get("link", ""), item.get("access_code", ""))
            if fresh:
                new_caption = _update_caption_size(new_caption, fresh)
            repost_channel_update(item, new_caption, new_version)
        break

def poll_file_size() -> None:
    """每10分钟刷新云盘缓存 + 更新帖子文件大小"""
    log.info("📦 文件轮询线程已启动")
    while True:
        try:
            _do_poll_file_size()
        except Exception as e:
            log.info("📦 轮询异常: %s", e)
        time.sleep(600)

def _check_link_alive(share_code: str) -> bool:
    """访问分享页检测链接是否有效"""
    try:
        url = f"https://cloud.189.cn/web/share?code={share_code}"
        r = requests.get(url, timeout=10, allow_redirects=True)
        text = r.text
        dead_keywords = ["审核不通过", "分享已失效", "内容不存在", "该分享不存在",
                        "抱歉，该内容", "已失效", "已被删除"]
        for kw in dead_keywords:
            if kw in text:
                return False
        return True
    except Exception:
        return True  # 网络问题不算失效

def _notify_dead_link(item: dict) -> None:
    """通知超管链接失效"""
    msg_id = item.get("msg_id", "")
    if not msg_id or item.get("_dead_notified"):
        return
    item["_dead_notified"] = True
    title = item.get("title", "未知")
    mk = telebot.types.InlineKeyboardMarkup()
    mk.add(
        telebot.types.InlineKeyboardButton("🗑️ 删除", callback_data=f"dead_del_{msg_id}"),
        telebot.types.InlineKeyboardButton("⏭️ 跳过", callback_data=f"dead_skip_{msg_id}"),
    )
    try:
        bot.send_message(SUPER_ADMIN,
            f"⚠️ 云盘链接可能失效\n🎬 {title}\n"
            f"📎 <a href='{CHANNEL_LINK}/{msg_id}'>查看帖子</a>",
            reply_markup=mk, parse_mode="HTML")
    except Exception:
        pass

def _check_auto_complete(item: dict) -> bool:
    """检查单个剧集是否满足 TMDB 自动完结条件，满足则自动完结并返回 True"""
    try:
        title = re.sub(r'\s*\(\d{4}\)', '', item.get("title", ""))
        version_desc = item.get("version_desc", "")
        if "更新中" not in version_desc:
            return False
        s_match = re.search(r'[Ss](\d+)', version_desc)
        season_num = int(s_match.group(1)) if s_match else 1
        e_matches = re.findall(r'[Ee](\d+)', version_desc)
        local_max = int(e_matches[-1]) if e_matches else 0
        if not local_max:
            return False

        tmdb_id = item.get("tmdb_id")
        if not tmdb_id:
            clean = re.sub(r'\(\d{4}\)', '', title).strip()
            url = f"https://api.themoviedb.org/3/search/tv?api_key={TMDB_API_KEY}&query={urllib.parse.quote(clean)}&language=zh-CN"
            data = tmdb_cached_get(url, timeout=5)
            results = data.get("results", []) if data else []
            if results:
                tmdb_id = results[0]["id"]

        if not tmdb_id:
            return False

        url = f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season_num}?api_key={TMDB_API_KEY}&language=zh-CN"
        data = tmdb_cached_get(url, timeout=5)
        if not data:
            return False

        episodes = data.get("episodes", [])
        all_eps = [ep.get("episode_number") for ep in episodes if ep.get("episode_number")]
        aired = [ep.get("episode_number") for ep in episodes if ep.get("air_date") and ep.get("air_date") <= date.today().strftime("%Y-%m-%d")]
        if not aired or not all_eps:
            return False

        if len(aired) >= len(all_eps) and local_max >= max(aired):
            old_v = version_desc
            old_c = item.get("caption", "")
            new_v = _simplify_completed(old_v)
            old_v_disp = old_v.replace("更新中", "").strip().replace("  ", " ")
            new_v_disp = new_v.replace("更新中", "").strip().replace("  ", " ")
            new_c = old_c.replace(old_v_disp, new_v_disp) if old_v_disp in old_c else old_c
            if repost_channel_update(item, new_c, new_v):
                log.info("📦 自动完结: %s → %s", title, new_v)
                return True
    except Exception as e:
        log.debug("自动完结检查失败 %s: %s", item.get("title"), e)
    return False

def _do_poll_file_size() -> None:
    try:
        with db_lock:
            items = [x for x in msg_db if "更新中" in x.get("version_desc", "")]
        updated = 0
        for item in items:
            link = item.get("link", "")
            if "189.cn" not in link:
                continue
            time.sleep(3)
            try:
                result = parse_189_share_details(link, item.get("access_code") or None)
                if result:
                    cloud_count = result.get("count", 0)
                    cloud_size = result.get("size", 0)
                    with _cloud_cache_lock:
                        _cloud_cache[link] = {"size": cloud_size, "count": cloud_count}
                    # 大小
                    new_size = format_file_size(cloud_size)
                    old_ver = item.get("version_desc", "")
                    old_cap = item.get("caption", "")
                    new_ver = old_ver
                    # 集数 (不依赖"更新中"位置, 已筛选过更新中的条目)
                    if cloud_count > 0:
                        new_ver = re.sub(r'E\d+(?![\d-])', f'E{cloud_count:02d}', old_ver)
                        if new_ver == old_ver:
                            new_ver = re.sub(r'E\d+-\d+', f'E01-E{cloud_count:02d}', old_ver)
                    new_cap = old_cap
                    if new_ver != old_ver:
                        # 用去"更新中"的版本号匹配 caption (caption 已不显示更新中)
                        old_disp = old_ver.replace("更新中", "").strip().replace("  ", " ")
                        new_disp = new_ver.replace("更新中", "").strip().replace("  ", " ")
                        if old_disp in old_cap:
                            new_cap = old_cap.replace(old_disp, new_disp)
                        else:
                            new_cap = _replace_ep_in_text(old_cap, f"{cloud_count:02d}")
                    new_cap = _update_caption_size(new_cap, new_size)
                    if new_ver != old_ver or new_cap != old_cap:
                        repost_channel_update(item, new_cap, new_ver)
                        updated += 1
                # 检查链接是否失效
                share_code = re.search(r'code=(\w+)', link) or re.search(r'/t/(\w+)', link)
                if share_code and not _check_link_alive(share_code.group(1)):
                    _notify_dead_link(item)
            except Exception as e:
                log.warning("轮询跳过 %s: %s", item.get("title","?"), e)
                _notify_dead_link(item)
        if updated:
            log.info("📦 轮询更新了 %d 个帖子", updated)
        else:
            log.info("📦 轮询完成，无需更新")

        # TMDB 自动完结检查
        for item in items:
            _check_auto_complete(item)

        # 预热 TMDB 缓存 (扫全部帖子，跳过已缓存)
        with db_lock:
            all_items = list(msg_db)
        for item in all_items:
            cache_key = f"{item.get('title', '')}"
            with _tmdb_date_lock:
                if cache_key in _tmdb_date_cache:
                    continue
            try:
                _get_tmdb_release_date(item)
            except Exception:
                pass
            time.sleep(0.5)
    except Exception as e:
        log.debug("轮询内部异常: %s", e)

def poll_emby_api() -> None:
    """Emby 新入库轮询线程"""
    while True:
        try:
            params = {
                "api_key": EMBY_API_KEY,
                "IncludeItemTypes": "Episode",
                "Recursive": "true",
                "SortBy": "DateCreated",
                "SortOrder": "Descending",
                "Limit": 5,
            }
            resp = http.get(EMBY_API_URL, params=params, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("Items", [])
                for item in reversed(items):  # 从旧到新处理
                    item_id = item.get("Id")
                    with processed_lock:
                        if item_id in processed_ids:
                            continue
                    series_name = item.get("SeriesName")
                    season_num = item.get("ParentIndexNumber", 1)
                    ep_num = item.get("IndexNumber")
                    if series_name and ep_num is not None:
                        log.info("🔍 [新入库发现] %s S%02dE%02d", series_name, season_num, ep_num)
                        update_db_from_emby(series_name, season_num, ep_num)
                        with processed_lock:
                            processed_ids.append(item_id)
                            if len(processed_ids) > 200:
                                processed_ids.pop(0)
                        save_processed()
        except Exception as e:
            log.debug("Emby 轮询异常 (静默重试): %s", e)

        time.sleep(POLL_INTERVAL)

# ===================== 启动入口 =====================
if __name__ == "__main__":
    # 启动前强制同步写盘一次
    save_msg_sync()

    # Token 有效性校验
    print(f"🔑 当前 Bot Token: {BOT_TOKEN[:12]}...{BOT_TOKEN[-4:]}")
    try:
        me = bot.get_me()
        print(f"🤖 Bot 身份验证通过: @{me.username} (ID: {me.id})")
    except Exception as e:
        print(f"❌ Token 验证失败 (401 Unauthorized)！请检查 BOT_TOKEN 是否正确。")
        print(f"   错误详情: {e}")
        exit(1)

    # Emby 轮询已禁用
    # threading.Thread(target=poll_emby_api, daemon=True).start()
    print("📦 启动文件大小轮询线程 (每10分钟)...")
    threading.Thread(target=poll_file_size, daemon=True).start()

    print("✅ 天翼影视机器人已启动")
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=30)
        except requests.exceptions.ConnectionError:
            time.sleep(5)
        except Exception as e:
            log.warning("Bot轮询异常: %s", e)
            time.sleep(5)
