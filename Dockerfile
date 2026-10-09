FROM python:3.10-slim-bookworm

# 设置工作目录
WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y \
    gcc \
    default-libmysqlclient-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

# 复制requirements.txt并安装Python依赖
COPY backend/requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY backend/ ./
COPY frontend/ ./frontend/
COPY config/ ./config/

# 安全加固（P1-4）：以非 root 用户运行，遵循最小权限原则，降低容器逃逸风险。
# 创建 appuser(uid 1000) 并授权应用/数据/日志目录。挂载的 ./data、./logs 属主由
# deploy.sh 的 `chown -R 1000:1000 data/ logs/` 对齐到 uid 1000，
# 从根本上解决此前非 root 启动期无法写挂载目录（Worker failed to boot）的问题。
RUN groupadd -r appuser -g 1000 \
    && useradd -r -u 1000 -g appuser -d /app appuser \
    && mkdir -p /app/data/uploads /app/backups /var/log/ams \
    && chown -R appuser:appuser /app /var/log/ams

# 暴露端口
EXPOSE 8088

# 健康检查
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8088/api/auth/check')" || exit 1

# 切换为非 root 用户运行
USER appuser

# 启动命令 - 输出到stdout避免权限问题
CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:8088", "--access-logfile", "-", "--error-logfile", "-", "--timeout", "120", "wsgi:app"]
