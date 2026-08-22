#!/usr/bin/env bash
# ============================================================
# BLE 权限配置脚本
# 配置 BlueZ、dbus、hciconfig、非 root 用户 BLE 访问权限
# 确保 Python Bleak 可在不提权的情况下操作蓝牙
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
    error "Please run as root: sudo bash setup_ble.sh"
fi

info "Setting up BLE permissions for bag-terminal..."

# ── 1. 安装 BlueZ ──
info "Installing BlueZ and Bluetooth stack..."
apt-get update -qq
apt-get install -y -qq \
    bluez bluez-firmware pi-bluetooth \
    bluetooth bluez-tools \
    libglib2.0-dev libdbus-1-dev \
    || error "Failed to install BlueZ"

# ── 2. 启用蓝牙服务 ──
info "Enabling bluetooth service..."
systemctl enable bluetooth
systemctl start bluetooth

# ── 3. 检查 hci 接口 ──
info "Checking HCI interfaces..."
if ! command -v hciconfig &>/dev/null; then
    error "hciconfig not found. BlueZ installation may have failed."
fi

HCI_DEVICE=$(hciconfig 2>/dev/null | grep -oP '^hci\d+' | head -1 || echo "")
if [ -z "$HCI_DEVICE" ]; then
    warn "No HCI device found. Checking for USB Bluetooth dongle..."
    # 尝试重置 USB 总线
    for dev in /sys/bus/usb/devices/*/driver; do
        if echo "$(dirname "$dev")" | grep -qi "bluetooth\|bt"; then
            info "Found Bluetooth USB device, binding driver..."
            bind_path=$(dirname "$(dirname "$dev")")
            echo "$bind_path" > /sys/bus/usb/drivers/usb/unbind 2>/dev/null || true
            sleep 1
            echo "$bind_path" > /sys/bus/usb/drivers/usb/bind 2>/dev/null || true
            sleep 2
        fi
    done
    HCI_DEVICE=$(hciconfig 2>/dev/null | grep -oP '^hci\d+' | head -1 || echo "")
fi

if [ -z "$HCI_DEVICE" ]; then
    warn "Still no HCI device. BLE functionality may be unavailable."
    warn "Ensure a Bluetooth module is connected."
else
    info "Found HCI device: ${HCI_DEVICE}"
    # 启用接口
    hciconfig "$HCI_DEVICE" up
    hciconfig "$HCI_DEVICE" piscan  # 可被发现 + 可查询
    info "HCI device configured and enabled"
fi

# ── 4. 非 root BLE 访问权限 ──
info "Configuring non-root BLE access..."

# 添加 pi 用户到 bluetooth 组（如果存在）
for user in pi root; do
    if id "$user" &>/dev/null; then
        usermod -aG bluetooth "$user" 2>/dev/null || true
        usermod -aG plugdev "$user" 2>/dev/null || true
        info "Added ${user} to bluetooth and plugdev groups"
    fi
done

# ── 5. D-Bus 策略配置 ──
info "Configuring D-Bus policies for BlueZ..."

DBUS_CONF="/etc/dbus-1/system.d/bluetooth-bag-terminal.conf"
cat > "$DBUS_CONF" << 'EOF'
<!DOCTYPE busconfig PUBLIC "-//freedesktop//DTD D-BUS Bus Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/dbus/1.0/busconfig.dtd">
<busconfig>
  <!-- Allow bag-terminal service to access BlueZ D-Bus interface -->
  <policy user="pi">
    <allow send_destination="org.bluez"/>
    <allow send_destination="org.bluez"
           send_interface="org.bluez.Adapter1"/>
    <allow send_destination="org.bluez"
           send_interface="org.bluez.Device1"/>
    <allow send_destination="org.bluez"
           send_interface="org.bluez.GattManager1"/>
    <allow send_destination="org.bluez"
           send_interface="org.bluez.Device1.Set"/>
    <allow send_destination="org.bluez"
           send_interface="org.freedesktop.DBus.ObjectManager"/>
    <allow send_destination="org.bluez"
           send_interface="org.freedesktop.DBus.Properties"/>
  </policy>
  <policy user="root">
    <allow send_destination="org.bluez"/>
  </policy>
</busconfig>
EOF

info "D-Bus policy written to ${DBUS_CONF}"

# ── 6. setcap 给 Python 二进制 ──
info "Setting CAP_NET_RAW for Python (BLE socket access)..."
PYTHON_BIN=$(which python3.11 || which python3 || echo "")
if [ -n "$PYTHON_BIN" ]; then
    setcap cap_net_raw,cap_net_admin+eip "$PYTHON_BIN" 2>/dev/null || {
        warn "setcap failed for ${PYTHON_BIN}"
        warn "Running as root or in bluetooth group is required"
    }
    info "Capabilities set on ${PYTHON_BIN}"
fi

# ── 7. rfkill 解除蓝牙阻止 ──
info "Unblocking Bluetooth via rfkill..."
if command -v rfkill &>/dev/null; then
    rfkill unblock bluetooth 2>/dev/null || true
fi

# ── 8. 内核模块 ──
info "Ensuring Bluetooth kernel modules are loaded..."
for mod in bluetooth btusb hci_uart btintel btrtl; do
    modprobe "$mod" 2>/dev/null || true
done
# 持久加载
for mod in bluetooth btusb hci_uart; do
    if ! grep -q "^$mod" /etc/modules-load.d/bt-modules.conf 2>/dev/null; then
        echo "$mod" >> /etc/modules-load.d/bt-modules.conf 2>/dev/null || true
    fi
done
mkdir -p /etc/modules-load.d
echo -e "bluetooth\nbtusb\nhci_uart" > /etc/modules-load.d/bt-modules.conf

# ── 9. btmon 调试工具 ──
info "Installing Bluetooth debug tools..."
apt-get install -y -qq btmon 2>/dev/null || true

# ── 10. 重启蓝牙服务 ──
info "Restarting bluetooth service..."
systemctl restart bluetooth
sleep 2

# ── 验证 ──
info "Verifying BLE setup..."
if hciconfig "$HCI_DEVICE" 2>/dev/null | grep -q "UP"; then
    info "BLE is UP and running on ${HCI_DEVICE}"
    info "BLE setup complete!"
else
    warn "BLE interface not UP. Check: systemctl status bluetooth"
    warn "Manual fix: sudo hciconfig ${HCI_DEVICE:-hci0} up"
fi

echo ""
info "BLE setup complete. Reboot recommended: sudo reboot"
