# =============================================================================
# 挎包终端 Docker 镜像 — 铁路巡检智能眼镜系统
# 基础镜像: python:3.11-slim-bookworm (ARM64/aarch64 兼容)
# 依赖: BlueZ + libglib2.0 + OpenCV 运行时 + ONNX Runtime ARM
# =============================================================================
FROM python:3.11-slim-bookworm AS base

# 元数据
LABEL maintainer="railway-inspection-team"
LABEL description="铁路巡检智能眼镜 - 挎包终端服务"
LABEL version="1.0.0"

# 环境变量
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Shanghai \
    OMP_NUM_THREADS=4

# 安装系统依赖:
# - BlueZ: BLE 协议栈 (Bleak 后端)
# - libglib2.0: BlueZ D-Bus 依赖
# - libglib2.0-dev / pkg-config: 构建期需要
# - libgl1 / libglib2.0-0: OpenCV 运行时
# - libusb-1.0-0: 5G 模块 USB 串口
# - bluez-hcidump / bluetooth: 蓝牙调试工具
# - ffmpeg: 图像/视频处理
# - curl / wget / ca-certificates: 模型下载
RUN apt-get update && apt-get install -y --no-install-recommends \
    bluez \
    bluez-hcidump \
    bluetooth \
    libglib2.0-0 \
    libglib2.0-dev \
    pkg-config \
    libgl1 \
    libglib2.0-0 \
    libusb-1.0-0 \
    libusb-1.0-0-dev \
    ffmpeg \
    curl \
    wget \
    ca-certificates \
    tzdata \
    && ln -sf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 创建非 root 用户 (BLE 需要通过 dbus, 但容器以 privileged 运行)
RUN groupadd -r bagterm && useradd -r -g bagterm -m -d /home/bagterm -s /bin/bash bagterm

# 创建工作目录与挂载点
WORKDIR /app
RUN mkdir -p /app/config /app/models /app/data /app/logs /app/storage/photos \
    && chown -R bagterm:bagterm /app

# 复制 requirements 并安装 Python 依赖
COPY --chown=bagterm:bagterm requirements.txt /app/requirements.txt

USER root
RUN pip install --no-cache-dir -r /app/requirements.txt

# 复制应用代码
COPY --chown=bagterm:bagterm app /app/app
COPY --chown=bagterm:bagterm config /app/config

USER bagterm

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# 暴露 API 端口
EXPOSE 8000

# 启动命令
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
