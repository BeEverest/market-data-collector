# 投研大盘数据采集系统 —— 生产镜像
# 构建：docker build -t market-data-collector:local .
# 运行：见 docker-compose.yml / README.md「Docker 部署」章节
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# PyPI 源可用构建参数覆盖：--build-arg PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_INDEX_URL=https://mirrors.tencent.com/pypi/simple/
ENV PIP_INDEX_URL=${PIP_INDEX_URL}

# 基础镜像已内置 Asia/Shanghai 时区数据，无需 apt 安装。
# 调度器依赖本地 HH:MM；健康检查用 Python 标准库，无需 curl。
RUN ln -snf /usr/share/zoneinfo/${TZ} /etc/localtime \
    && echo ${TZ} > /etc/timezone

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 数据目录由 docker-compose 挂载 ./data 持久化；此处仅保证目录存在
RUN mkdir -p /app/output/monitor /app/output/exports \
    && chmod +x /app/docker/entrypoint.sh

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python3.11 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)" || exit 1

ENTRYPOINT ["/app/docker/entrypoint.sh"]
