#!/usr/bin/env bash
# ============================================================
# 5G 模块配置脚本 — Quectel RM500Q-GL
# 配置串口映射、AT 命令初始化、拨号脚本、udev 规则
# ============================================================
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC}  $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# 检查 root
if [ "$(id -u)" -ne 0 ]; then
    error "Please run as root: sudo bash setup_modem.sh"
fi

info "Setting up Quectel RM500Q-GL 5G modem..."

# ── 1. 安装依赖 ──
info "Installing modem dependencies..."
apt-get update -qq
apt-get install -y -qq \
    usbutils \
    picocom \
    ppp \
    wvdial \
    modemmanager \
    network-manager \
    || error "Failed to install modem packages"

# ── 2. udev 规则：固定串口设备名 ──
info "Creating udev rules for Quectel RM500Q-GL..."

UDEV_FILE="/etc/udev/rules.d/99-quectel-rm500q.rules"
cat > "$UDEV_FILE" << 'EOF'
# Quectel RM500Q-GL USB 5G module — fixed serial port mapping
# Vendor ID: 2c7c, Product ID: 0800 (RM500Q-GL)
#
# ttyUSB0 → AT command interface (primary)
# ttyUSB1 → AT command interface (secondary)
# ttyUSB2 → Modem data (PPP/ECM)
# ttyUSB3 → GPS NMEA / diagnostics
# ttyUSB4 → Log port

SUBSYSTEMS=="usb", ATTRS{idVendor}=="2c7c", ATTRS{idProduct}=="0800", \
    KERNEL=="ttyUSB0", SYMLINK+="quectel_at_primary", MODE="0666"

SUBSYSTEMS=="usb", ATTRS{idVendor}=="2c7c", ATTRS{idProduct}=="0800", \
    KERNEL=="ttyUSB1", SYMLINK+="quectel_at_secondary", MODE="0666"

SUBSYSTEMS=="usb", ATTRS{idVendor}=="2c7c", ATTRS{idProduct}=="0800", \
    KERNEL=="ttyUSB2", SYMLINK+="quectel_modem_data", MODE="0666"

SUBSYSTEMS=="usb", ATTRS{idVendor}=="2c7c", ATTRS{idProduct}=="0800", \
    KERNEL=="ttyUSB3", SYMLINK+="quectel_gps", MODE="0666"

# Also handle RM500Q-GL with different PID variants
SUBSYSTEMS=="usb", ATTRS{idVendor}=="2c7c", ATTRS{idProduct}=="0900", \
    KERNEL=="ttyUSB*", MODE="0666"

# ECM network interface naming
SUBSYSTEM=="net", ACTION=="add", ATTRS{idVendor}=="2c7c", \
    NAME="usb0"
EOF

info "udev rules written to ${UDEV_FILE}"

# ── 3. 加载内核模块 ──
info "Loading USB serial kernel modules..."
for mod in usbserial option qcserial; do
    modprobe "$mod" 2>/dev/null || true
done

# usbserial fallback: 绑定 Quectel VID:PID
if ! lsmod | grep -q "usb_wwan\|option"; then
    modprobe usbserial 2>/dev/null || true
    # 手动绑定 VID:PID（如果 option 驱动未自动识别）
    echo "2c7c 0800" > /sys/bus/usb-serial/drivers/usbserial/new_id 2>/dev/null || true
    echo "2c7c 0900" > /sys/bus/usb-serial/drivers/usbserial/new_id 2>/dev/null || true
fi

# ── 4. 触发 udev 重载 ──
info "Reloading udev rules..."
udevadm control --reload-rules
udevadm trigger --action=add --subsystem-match=usb
udevadm trigger --action=add --subsystem-match=tty
sleep 2

# ── 5. 检查设备 ──
info "Checking modem devices..."
if [ -e /dev/quectel_at_primary ]; then
    info "AT command port: /dev/quectel_at_primary (→ ttyUSB0)"
elif [ -e /dev/ttyUSB2 ]; then
    warn "Symlink not created, using raw /dev/ttyUSB2"
    ln -sf /dev/ttyUSB2 /dev/quectel_at_primary 2>/dev/null || true
else
    warn "No Quectel serial port found!"
    warn "Check USB connection: lsusb | grep 2c7c"
fi

# ── 6. AT 命令初始化 ──
info "Running AT command initialization..."

AT_PORT="${AT_PORT:-/dev/quectel_at_primary}"
if [ ! -e "$AT_PORT" ]; then
    AT_PORT="/dev/ttyUSB2"
fi

# 等待串口设备就绪
for i in $(seq 1 10); do
    if [ -e "$AT_PORT" ]; then
        break
    fi
    sleep 1
done

# AT 命令初始化函数
send_at() {
    local cmd="$1"
    local port="$2"
    echo -e "AT${cmd}\r" > "$port" 2>/dev/null
    sleep 0.5
}

if [ -e "$AT_PORT" ]; then
    # 配置串口
    stty -F "$AT_PORT" 115200 raw -echo

    # 基础初始化
    send_at "" "$AT_PORT"           # AT (测试)
    send_at "E0" "$AT_PORT"         # 关闭回显
    send_at "+CMEE=2" "$AT_PORT"    # 详细错误报告
    send_at "+CSCS=\"GSM\"" "$AT_PORT"  # 字符集

    info "AT initialization completed on ${AT_PORT}"
else
    warn "AT port not available, skipping AT initialization"
    warn "Connect modem and re-run: sudo bash setup_modem.sh"
fi

# ── 7. 配置 APN ──
info "Configuring APN..."
APN="${APN:-cmnet}"  # 默认移动 APN，可环境变量覆盖

if [ -e "$AT_PORT" ]; then
    # 配置 APN
    echo -e "AT+CGDCONT=1,\"IP\",\"${APN}\"\r" > "$AT_PORT" 2>/dev/null
    sleep 1
    info "APN configured: ${APN}"

    # 设置网络制式偏好 5G
    echo -e 'AT+QNWPREFCFG=\"mode_pref\",\"NR5G:LTE:WCDMA\"\r' > "$AT_PORT" 2>/dev/null
    sleep 1
    info "Network preference set to 5G preferred"

    # 启用 ECM 模式
    echo -e 'AT$QCRMCALL=1,1\r' > "$AT_PORT" 2>/dev/null
    sleep 2
    info "ECM data call initiated"
fi

# ── 8. 网络接口配置 ──
info "Configuring USB network interface..."
NET_IFACE="usb0"

# 等待网络接口出现
for i in $(seq 1 15); do
    if ip link show "$NET_IFACE" &>/dev/null; then
        break
    fi
    sleep 1
done

if ip link show "$NET_IFACE" &>/dev/null; then
    # 启用接口
    ip link set "$NET_IFACE" up
    # 通过 DHCP 获取 IP
    if command -v dhclient &>/dev/null; then
        dhclient "$NET_IFACE" 2>/dev/null || warn "DHCP failed on ${NET_IFACE}"
    elif command -v udhcpc &>/dev/null; then
        udhcpc -i "$NET_IFACE" 2>/dev/null || warn "udhcpc failed on ${NET_IFACE}"
    fi
    info "Network interface ${NET_IFACE} configured"
else
    warn "Network interface ${NET_IFACE} not found yet"
    warn "ECM dial may still be in progress, waiting..."
    sleep 5
    if ip link show "$NET_IFACE" &>/dev/null; then
        ip link set "$NET_IFACE" up
        dhclient "$NET_IFACE" 2>/dev/null || true
        info "Network interface ${NET_IFACE} configured (delayed)"
    fi
fi

# ── 9. 拨号脚本 ──
info "Creating dial script..."
DIAL_SCRIPT="/usr/local/bin/modem_dial.sh"
cat > "$DIAL_SCRIPT" << 'EOF'
#!/usr/bin/env bash
# 5G modem dial/re-dial script
set -e
AT_PORT="${AT_PORT:-/dev/quectel_at_primary}"
[ ! -e "$AT_PORT" ] && AT_PORT="/dev/ttyUSB2"

echo "[INFO] Dialing 5G modem..."
stty -F "$AT_PORT" 115200 raw -echo
echo -e "AT\r" > "$AT_PORT"; sleep 0.5
echo -e "ATE0\r" > "$AT_PORT"; sleep 0.5
echo -e 'AT+CGDCONT=1,"IP","cmnet"\r' > "$AT_PORT"; sleep 1
echo -e 'AT$QCRMCALL=1,1\r' > "$AT_PORT"; sleep 3
ip link set usb0 up 2>/dev/null || true
dhclient usb0 2>/dev/null || true
echo "[INFO] Dial complete. Check: ip addr show usb0"
EOF
chmod +x "$DIAL_SCRIPT"
info "Dial script installed at ${DIAL_SCRIPT}"

# ── 10. ModemManager 禁用（避免干扰 AT 命令） ──
info "Disabling ModemManager (interferes with raw AT access)..."
systemctl stop ModemManager 2>/dev/null || true
systemctl disable ModemManager 2>/dev/null || true

# ── 11. cron 自动重拨 ──
info "Setting up auto-redial cron job..."
CRON_LINE="*/2 * * * * /usr/local/bin/modem_dial.sh >> /var/log/modem_dial.log 2>&1"
(crontab -l 2>/dev/null; echo "$CRON_LINE") | sort -u | crontab -

# ── 验证 ──
info "Verifying modem setup..."
if ip addr show usb0 2>/dev/null | grep -q "inet "; then
    IP_ADDR=$(ip addr show usb0 2>/dev/null | grep -oP 'inet \K[\d.]+' | head -1)
    info "Modem online! IP: ${IP_ADDR}"
else
    warn "Modem interface has no IP yet. Check: modem_dial.sh"
fi

echo ""
info "5G modem setup complete!"
info "  AT port:        ${AT_PORT}"
info "  Network iface:  ${NET_IFACE}"
info "  Dial script:    ${DIAL_SCRIPT}"
info "  Auto-redial:    every 2 minutes via cron"
