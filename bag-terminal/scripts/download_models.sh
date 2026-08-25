#!/usr/bin/env bash
# ============================================================
# AI 模型下载脚本
# 从 MinIO 下载挎包终端所需的 AI 模型文件：
#   - yolov8n_railway.onnx  — YOLOv8n 铁路缺陷检测（8类）
#   - bge-small-zh.onnx      — BGE-small-zh 中文嵌入模型
#   - phi-3-mini-q4.gguf    — Phi-3-mini Q4 量化 LLM
# ============================================================
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC}  $1"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# ── 配置 ──
MODEL_DIR="${MODEL_DIR:-/mnt/sdcard/bag-terminal/models}"
MINIO_ENDPOINT="${MINIO_ENDPOINT:-http://minio:9000}"
MINIO_ACCESS_KEY="${MINIO_ACCESS_KEY:-admin}"
MINIO_SECRET_KEY="${MINIO_SECRET_KEY:-password}"
MINIO_BUCKET="${MINIO_BUCKET:-railway-models}"

# 模型文件清单: filename | sha256 (empty=skip verification)
declare -A MODELS=(
    ["yolov8n_railway.onnx"]="a1b2c3d4e5f6"     # 替换为实际 SHA-256
    ["bge-small-zh.onnx"]="b2c3d4e5f6a1"
    ["phi-3-mini-q4.gguf"]="c3d4e5f6a1b2"
)

# 模型大小估算（用于磁盘空间检查）
declare -A MODEL_SIZES_MB=(
    ["yolov8n_railway.onnx"]=25
    ["bge-small-zh.onnx"]=95
    ["phi-3-mini-q4.gguf"]=2300
)

info "Starting AI model download..."
info "  Target directory: ${MODEL_DIR}"
info "  MinIO endpoint:   ${MINIO_ENDPOINT}"
info "  Bucket:            ${MINIO_BUCKET}"

# ── 1. 创建目录 ──
mkdir -p "${MODEL_DIR}"

# ── 2. 磁盘空间检查 ──
info "Checking disk space..."
TOTAL_SIZE_MB=0
for size in "${MODEL_SIZES_MB[@]}"; do
    TOTAL_SIZE_MB=$((TOTAL_SIZE_MB + size))
done
REQUIRED_GB=$(( (TOTAL_SIZE_MB + 500) / 1024 ))  # 额外 500MB 缓冲

AVAILABLE_KB=$(df -P "${MODEL_DIR}" | awk 'NR==2 {print $4}')
AVAILABLE_GB=$(( AVAILABLE_KB / 1024 / 1024 ))

if [ "$AVAILABLE_GB" -lt "$REQUIRED_GB" ]; then
    error "Insufficient disk space: ${AVAILABLE_GB}GB available, ${REQUIRED_GB}GB required"
fi
info "Disk space OK: ${AVAILABLE_GB}GB available"

# ── 3. 下载函数 ──
download_model() {
    local model_name="$1"
    local expected_sha256="$2"
    local model_path="${MODEL_DIR}/${model_name}"
    local model_url="${MINIO_ENDPOINT}/${MINIO_BUCKET}/${model_name}"

    # 检查已存在且完整
    if [ -f "$model_path" ]; then
        local file_size=$(stat -c%s "$model_path" 2>/dev/null || echo 0)
        if [ "$file_size" -gt 0 ]; then
            if [ -n "$expected_sha256" ] && [ "$expected_sha256" != "a1b2c3d4e5f6" ]; then
                local actual_sha256=$(sha256sum "$model_path" | awk '{print $1}')
                if [ "$actual_sha256" = "$expected_sha256" ]; then
                    info "Model ${model_name} already exists and verified (skip)"
                    return 0
                else
                    warn "Model ${model_name} exists but SHA-256 mismatch, re-downloading..."
                fi
            else
                info "Model ${model_name} already exists (skip, SHA-256 not configured)"
                return 0
            fi
        fi
    fi

    info "Downloading ${model_name}..."
    info "  URL: ${model_url}"

    # 使用 curl 下载
    if curl -sSL --progress-bar \
        -o "${model_path}.tmp" \
        "${model_url}" \
        --connect-timeout 30 \
        --max-time 3600; then

        # 原子写入
        mv "${model_path}.tmp" "$model_path"
        local file_size=$(stat -c%s "$model_path")
        local file_mb=$(( file_size / 1024 / 1024 ))
        info "  Downloaded: ${file_mb}MB"

        # SHA-256 校验
        if [ -n "$expected_sha256" ] && [ "$expected_sha256" != "a1b2c3d4e5f6" ]; then
            local actual_sha256=$(sha256sum "$model_path" | awk '{print $1}')
            if [ "$actual_sha256" = "$expected_sha256" ]; then
                info "  SHA-256 verified: ${actual_sha256:0:16}..."
            else
                warn "  SHA-256 mismatch! Expected: ${expected_sha256:0:16}, Got: ${actual_sha256:0:16}"
                warn "  Model may be corrupted. Re-run to retry."
            fi
        fi
    else
        error "Failed to download ${model_name} from ${model_url}"
    fi
}

# ── 4. 下载所有模型 ──
SUCCESS_COUNT=0
FAIL_COUNT=0

for model_name in "${!MODELS[@]}"; do
    expected_sha="${MODELS[$model_name]}"
    if download_model "$model_name" "$expected_sha"; then
        SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
    else
        FAIL_COUNT=$((FAIL_COUNT + 1))
        warn "Failed to download ${model_name}"
    fi
done

# ── 5. 验证下载结果 ──
info "Verifying downloaded models..."
for model_name in "${!MODELS[@]}"; do
    model_path="${MODEL_DIR}/${model_name}"
    if [ -f "$model_path" ]; then
        file_size=$(stat -c%s "$model_path")
        info "  ✓ ${model_name}: $(( file_size / 1024 / 1024 ))MB"
    else
        warn "  ✗ ${model_name}: NOT FOUND"
    fi
done

# ── 6. 创建模型配置文件 ──
info "Creating model config..."
cat > "${MODEL_DIR}/models.json" << EOF
{
    "yolo": {
        "path": "${MODEL_DIR}/yolov8n_railway.onnx",
        "model_name": "yolov8n_railway",
        "version": "1.0.0",
        "input_size": [1, 3, 320, 320],
        "classes": [
            "crack", "rust", "loose_bolt", "missing_bolt",
            "vegetation", "foreign_object", "water_damage", "track_misalignment"
        ]
    },
    "embedding": {
        "path": "${MODEL_DIR}/bge-small-zh.onnx",
        "model_name": "bge-small-zh",
        "version": "1.0.0",
        "dimension": 512,
        "max_length": 512
    },
    "llm": {
        "path": "${MODEL_DIR}/phi-3-mini-q4.gguf",
        "model_name": "phi-3-mini",
        "version": "1.0.0",
        "quantization": "Q4_K_M",
        "context_size": 4096,
        "max_tokens": 512
    }
}
EOF
info "Model config written to ${MODEL_DIR}/models.json"

# ── 完成 ──
echo ""
info "=============================================="
info " Model download complete!"
info "=============================================="
info "  Success: ${SUCCESS_COUNT}/${#MODELS[@]}"
if [ "$FAIL_COUNT" -gt 0 ]; then
    warn "  Failed:  ${FAIL_COUNT}"
    warn "  Re-run this script to retry failed downloads"
fi
info "  Directory: ${MODEL_DIR}"
