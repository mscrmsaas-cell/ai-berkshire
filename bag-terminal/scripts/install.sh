#!/usr/bin/env bash
# ============================================================
# 挎包终端安装脚本
# 在 Raspberry Pi CM4 / Pi 4B 上安装系统依赖、Python 虚拟环境、
# BLE/I2C 权限、Docker 镜像构建
# ============================================================
set -euo pipefail

# ── 颜色输出 ──
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC}  $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# ── 变量 ──
APP_DIR="/opt/bag-terminal"
DATA_DIR="/data/bag-terminal"
SDCARD_DIR="/mnt/sdcard/bag-terminal"
VENV_DIR="${APP_DIR}/venv"
PYTHON_VERSION="3.11"
COMPOSE_FILE="${APP_DIR}/docker-compose.yml"
ENV_FILE="${APP_DIR}/.env"

info "Starting bag-terminal installation..."

# ── 1. 检查 root ──
if [ "$(id -u)" -ne 0 ]; then
    error "Please run as root: sudo bash install.sh"
fi

# ── 2. 系统更新 + 依赖安装 ──
info "Installing system dependencies..."

apt-get update -qq
apt-get install -y -qq \
    python3 python3-pip python3-venv \
    bluez bluez-firmware pi-bluetooth \
    libglib2.0-dev libdbus-1-dev libsystemd-dev \
    libusb-1.0-0-dev \
    i2c-tools \
    socat picocom \
    curl wget \
    htop \
    docker.io docker-compose-plugin \
    qrencode \
    jq \
    || error "Failed to install system packages"

# ── 3. 创建目录结构 ──
info "Creating directory structure..."
mkdir -p "${APP_DIR}/app"
mkdir -p "${APP_DIR}/config"
mkdir -p "${APP_DIR}/scripts"
mkdir -p "${APP_DIR}/systemd"
mkdir -p "${DATA_DIR}"
mkdir -p "${SDCARD_DIR}/photos"
mkdir -p "${SDCARD_DIR}/videos"
mkdir -p "${SDCARD_DIR}/alerts"
mkdir -p "${SDCARD_DIR}/logs"
mkdir -p "${SDCARD_DIR}/models"

# ── 4. Python 虚拟环境 ──
info "Setting up Python ${PYTHON_VERSION} virtual environment..."

if ! command -v python3.11 &>/dev/null; then
    warn "Python 3.11 not found, installing from deadsnakes PPA..."
    apt-get install -y -qq software-properties-common
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update -qq
    apt-get install -y -qq python3.11 python3.11-venv python3.11-dev
fi

python3.11 -m venv "${VENV_DIR}"
info "Installing Python dependencies..."
"${VENV_DIR}/bin/pip" install --upgrade pip setuptools wheel
"${VENV_DIR}/bin/pip" install -r "${APP_DIR}/requirements.txt" \
    || error "Failed to install Python dependencies"

# ── 5. BLE 权限 ──
info "Setting up BLE permissions..."
bash "${APP_DIR}/scripts/setup_ble.sh"

# ── 6. I2C 权限 ──
info "Setting up I2C permissions..."
if ! grep -q "i2c-dev" /etc/modules; then
    echo "i2c-dev" >> /etc/modules
fi
# 使能 I2C
if ! raspi-config nonint get_i2c 2>/dev/null; then
    raspi-config nonint do_i2c 0 2>/dev/null || warn "raspi-config not available"
fi
# 用户组权限
usermod -aG i2c pi 2>/dev/null || true
chmod 666 /dev/i2c-* 2>/dev/null || true

# ── 7. 5G 模块配置 ──
info "Setting up 5G modem..."
if [ -f "${APP_DIR}/scripts/setup_modem.sh" ]; then
    bash "${APP_DIR}/scripts/setup_modem.sh"
fi

# ── 8. 环境变量 ──
info "Creating .env file..."
if [ ! -f "${ENV_FILE}" ]; then
    cp "${APP_DIR}/.env.example" "${ENV_FILE}"
    # 生成随机密钥
    SECRET_KEY=$(openssl rand -hex 32)
    sed -i "s/CHANGE_ME/${SECRET_KEY}/" "${ENV_FILE}"
    warn ".env file created at ${ENV_FILE} — please edit with your settings!"
else
    info ".env file already exists, skipping..."
fi

# ── 9. SD 卡自动挂载 ──
info "Configuring SD card automount..."
FSTAB_LINE="/dev/mmcblk0p1 ${SDCARD_DIR} ext4 defaults,noatime,nofail 0 2"
if ! grep -q "${SDCARD_DIR}" /etc/fstab; then
    echo "${FSTAB_LINE}" >> /etc/fstab
    info "SD card mount entry added to /etc/fstab"
fi
mount -a 2>/dev/null || warn "Some mounts failed, check /etc/fstab"

# ── 10. Docker 镜像构建（可选） ──
if [ -f "${COMPOSE_FILE}" ]; then
    info "Building Docker image..."
    cd "${APP_DIR}"
    docker compose build || warn "Docker build failed (may need --build)"
    info "Docker image built successfully!"
fi

# ── 11. systemd 服务安装 ──
info "Installing systemd service..."
SERVICE_FILE="${APP_DIR}/systemd/bag-terminal.service"
if [ -f "${SERVICE_FILE}" ]; then
    cp "${SERVICE_FILE}" /etc/systemd/system/bag-terminal.service
    # 修正路径（确保使用正确的 Python 路径）
    if [ -f "${VENV_DIR}/bin/uvicorn" ]; then
        sed -i "s|ExecStart=.*|ExecStart=${VENV_DIR}/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000|" \
            /etc/systemd/system/bag-terminal.service
    fi
    systemctl daemon-reload
    systemctl enable bag-terminal
    info "Service enabled. Start with: sudo systemctl start bag-terminal"
fi

# ── 12. 下载 AI 模型 ──
info "Downloading AI models..."
if [ -f "${APP_DIR}/scripts/download_models.sh" ]; then
    bash "${APP_DIR}/scripts/download_models.sh" || warn "Model download skipped"
fi

# ── 完成 ──
info "=============================================="
info " bag-terminal installation complete!"
info "=============================================="
info ""
info " Next steps:"
info "   1. Edit ${ENV_FILE} with your settings"
info "   2. Start service: sudo systemctl start bag-terminal"
info "   3. Check health:  curl http://localhost:8000/health"
info "   4. View logs:      journalctl -u bag-terminal -f"
info ""
info " Data directory:  ${DATA_DIR}"
info " SD card storage: ${SDCARD_DIR}"
info " App directory:   ${APP_DIR}"
