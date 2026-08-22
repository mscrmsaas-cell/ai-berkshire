#!/usr/bin/env bash
# =============================================================================
# flash_firmware.sh — Build & flash the railway AR glasses firmware to ESP32-S3
#
# This script wraps the ESP-IDF build system (idf.py) with sensible defaults
# for the railway AR glasses project. It handles:
#   * ESP-IDF environment activation (export.sh)
#   * Setting the target chip (esp32s3)
#   * Full build (idf.py build)
#   * Flash + monitor (idf.py flash monitor)
#   * Erasing flash before flashing (optional --erase)
#   * Specifying the serial port (optional --port)
#   * Specifying the baud rate (optional --baud)
#
# Usage:
#   ./flash_firmware.sh [OPTIONS]
#
# Options:
#   --port PORT      Serial port (default: auto-detect or $ESPPORT)
#   --baud BAUD      Flash baud rate (default: 921600)
#   --erase          Erase entire flash before flashing
#   --build-only     Build but do not flash
#   --monitor        Flash then monitor (default if not --build-only)
#   --no-monitor     Flash without starting serial monitor
#   --clean          Run idf.py fullclean before building
#   --idf-path PATH  Path to ESP-IDF installation (default: $IDF_PATH or /opt/esp-idf)
#   --help           Show this help message
#
# Examples:
#   ./flash_firmware.sh                         # Build, flash, monitor
#   ./flash_firmware.sh --port /dev/ttyUSB0     # Specify port
#   ./flash_firmware.sh --build-only            # Build without flashing
#   ./flash_firmware.sh --erase --no-monitor    # Erase & flash, no monitor
#   ./flash_firmware.sh --clean                 # Full clean rebuild
#
# Exit codes:
#   0  success
#   1  ESP-IDF not found / environment setup failed
#   2  build failed
#   3  flash failed
#
# Copyright (c) 2024 Railway Inspection AR Glasses Project
# License: Apache-2.0
# =============================================================================

set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIRMWARE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PROJECT_NAME="glasses-firmware"
TARGET_CHIP="esp32s3"
DEFAULT_BAUD="921600"

# User-configurable via flags.
PORT=""
BAUD="${DEFAULT_BAUD}"
ERASE=false
BUILD_ONLY=false
MONITOR=true
CLEAN=false
IDF_PATH="${IDF_PATH:-/opt/esp-idf}"

# ── Color helpers (only if terminal supports colors) ─────────────────────────

if [[ -t 1 ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    BLUE='\033[0;34m'
    NC='\033[0m'  # No Color
else
    RED=''
    GREEN=''
    YELLOW=''
    BLUE=''
    NC=''
fi

log_info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }
log_step()  { echo -e "${BLUE}[STEP]${NC}  $*"; }

# ── Argument parsing ─────────────────────────────────────────────────────────

show_help() {
    sed -n '2,/^# =====/p' "${BASH_SOURCE[0]}" | sed 's/^# //; s/^#//'
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)
            PORT="$2"; shift 2 ;;
        --baud)
            BAUD="$2"; shift 2 ;;
        --erase)
            ERASE=true; shift ;;
        --build-only)
            BUILD_ONLY=true; MONITOR=false; shift ;;
        --monitor)
            MONITOR=true; shift ;;
        --no-monitor)
            MONITOR=false; shift ;;
        --clean)
            CLEAN=true; shift ;;
        --idf-path)
            IDF_PATH="$2"; shift 2 ;;
        --help|-h)
            show_help ;;
        *)
            log_error "unknown option: $1"
            echo "Use --help for usage."
            exit 1 ;;
    esac
done

# ── Step 1: Locate ESP-IDF ───────────────────────────────────────────────────

log_step "1. Locating ESP-IDF"

if [[ ! -d "${IDF_PATH}" ]]; then
    log_error "ESP-IDF not found at: ${IDF_PATH}"
    log_error "Set IDF_PATH or use --idf-path /path/to/esp-idf"
    exit 1
fi

IDF_EXPORT="${IDF_PATH}/export.sh"
if [[ ! -f "${IDF_EXPORT}" ]]; then
    log_error "export.sh not found in IDF_PATH: ${IDF_PATH}"
    exit 1
fi

log_info "ESP-IDF path: ${IDF_PATH}"
log_info "Activating ESP-IDF environment..."

# Source the export script (suppress the noisy output).
source "${IDF_EXPORT}" > /dev/null 2>&1

# Verify idf.py is available.
if ! command -v idf.py &> /dev/null; then
    log_error "idf.py not found after sourcing ${IDF_EXPORT}"
    exit 1
fi

log_info "ESP-IDF version: $(idf.py --version 2>/dev/null || echo 'unknown')"

# ── Step 2: Set target chip ──────────────────────────────────────────────────

log_step "2. Setting target chip: ${TARGET_CHIP}"

cd "${FIRMWARE_DIR}"

idf.py set-target "${TARGET_CHIP}" 2>&1 | tail -5

# ── Step 3: (Optional) Clean ─────────────────────────────────────────────────

if [[ "${CLEAN}" == true ]]; then
    log_step "3. Cleaning previous build (fullclean)"
    idf.py fullclean
    log_info "Clean complete."
else
    log_step "3. Skipping clean (use --clean to force)"
fi

# ── Step 4: Build ─────────────────────────────────────────────────────────────

log_step "4. Building firmware"

BUILD_LOG="/tmp/glasses-firmware-build-$$.log"
idf.py build 2>&1 | tee "${BUILD_LOG}" | grep -E '(Building|CMake|error:|warning:|Project build complete)' || true

if ! grep -q "Project build complete" "${BUILD_LOG}" 2>/dev/null; then
    log_error "Build failed. See full log: ${BUILD_LOG}"
    # Show the last 30 lines of the build log for diagnostics.
    tail -30 "${BUILD_LOG}" >&2
    exit 2
fi

log_info "Build successful."

# Show the firmware binary size.
BIN_FILE="${FIRMWARE_DIR}/build/${PROJECT_NAME}.bin"
if [[ -f "${BIN_FILE}" ]]; then
    BIN_SIZE=$(stat -c%s "${BIN_FILE}" 2>/dev/null || stat -f%z "${BIN_FILE}" 2>/dev/null || echo "?")
    log_info "Firmware binary: ${BIN_FILE} (${BIN_SIZE} bytes)"
fi

# Clean up the build log.
rm -f "${BUILD_LOG}"

# ── Step 5: Flash (if not build-only) ─────────────────────────────────────────

if [[ "${BUILD_ONLY}" == true ]]; then
    log_step "5. Skipping flash (--build-only)"
    log_info "Build complete. Run without --build-only to flash."
    exit 0
fi

log_step "5. Flashing firmware"

# Auto-detect serial port if not specified.
if [[ -z "${PORT}" ]]; then
    # Try the ESPPORT environment variable.
    if [[ -n "${ESPPORT:-}" ]]; then
        PORT="${ESPPORT}"
    else
        # Auto-detect: look for ttyUSB* or cu.SLAB_USB* (macOS).
        PORT=$(ls /dev/ttyUSB* 2>/dev/null | head -1 || true)
        if [[ -z "${PORT}" ]]; then
            PORT=$(ls /dev/cu.SLAB_USB* 2>/dev/null | head -1 || true)
        fi
        if [[ -z "${PORT}" ]]; then
            PORT=$(ls /dev/ttyACM* 2>/dev/null | head -1 || true)
        fi
    fi
fi

if [[ -z "${PORT}" ]]; then
    log_error "No serial port found. Specify with --port /dev/ttyUSB0"
    exit 3
fi

log_info "Serial port: ${PORT}"
log_info "Baud rate:   ${BAUD}"

FLASH_ARGS="--port ${PORT} --baud ${BAUD}"

if [[ "${ERASE}" == true ]]; then
    log_warn "Erasing entire flash before writing..."
    idf.py ${FLASH_ARGS} erase-flash
fi

if [[ "${MONITOR}" == true ]]; then
    log_info "Flashing and starting monitor (Ctrl+] to exit)..."
    idf.py ${FLASH_ARGS} flash monitor
else
    log_info "Flashing (no monitor)..."
    idf.py ${FLASH_ARGS} flash
fi

FLASH_EXIT=$?

if [[ ${FLASH_EXIT} -ne 0 ]]; then
    log_error "Flash failed (exit code ${FLASH_EXIT})"
    exit 3
fi

log_info "Flash complete!"
log_info "  Port:   ${PORT}"
log_info "  Baud:   ${BAUD}"
log_info "  Erased: ${ERASE}"
log_info "  Binary: ${BIN_FILE}"

exit 0
