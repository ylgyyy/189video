# ============================================
# 天翼影视 Telegram Bot - Docker 镜像
# ============================================

FROM python:3.13-slim

LABEL maintainer="xizivideo"
LABEL description="天翼影视 Telegram Bot with Emby auto-polling"

# --- 系统依赖 ---
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# --- 工作目录 ---
WORKDIR /app

# --- Python 依赖 (分层缓存) ---
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- 应用代码 ---
COPY video.py .
COPY config.json .

# --- 数据卷 (持久化) ---
RUN mkdir -p /app/data
VOLUME ["/app/data"]

# --- 运行时环境变量 ---
ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/app/data/

HEALTHCHECK --interval=60s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import sys; sys.exit(0)"

CMD ["python", "video.py"]
