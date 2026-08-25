# SaaS API 对接契约

> 本文档定义挎包终端 (Bag Terminal) 与 SaaS 后端之间的 REST API 对接契约。
> 涵盖设备管理、OTA、遥测 (新增) 以及巡检、RAG、文件上传 (现有) 六大模块。

## 1. 基础约定

### 1.1 基础 URL

```
{SAAS_API_URL}/api/v1
```

`SAAS_API_URL` 由环境变量或 `config.yaml` 配置，例如 `https://saas.railway.example.com`。

### 1.2 认证

- **JWT Bearer Token**: 所有需认证接口在 Header 携带 `Authorization: Bearer <token>`。
- Token 获取: `POST /api/v1/auth/login` (用户名+密码 → access_token + refresh_token)。
- Token 刷新: `POST /api/v1/auth/refresh` (refresh_token → 新 access_token)。
- Token 有效期: access_token 30 分钟, refresh_token 7 天。

### 1.3 通用响应格式

```json
{
  "code": 0,
  "message": "success",
  "data": { ... },
  "trace_id": "uuid-v4"
}
```

- `code = 0` 表示成功，非零为业务错误码。
- HTTP 状态码: 2xx 成功, 4xx 客户端错误, 5xx 服务端错误。

### 1.4 分页参数

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| page | int | 1 | 页码 (从 1 开始) |
| page_size | int | 20 | 每页条数 (最大 100) |
| sort | string | created_at:desc | 排序字段:方向 |

### 1.5 幂等性

- 写入接口支持 `Idempotency-Key` Header (UUID)，相同 Key 在 24 小时内返回相同结果。
- 用于离线缓存重试场景，避免重复写入。

---

## 2. 设备管理 (新增)

### 2.1 注册眼镜设备

```
POST /api/v1/devices/glasses
```

**请求 Body:**
```json
{
  "tuya_device_id": "uuid-of-tuya-device",
  "ble_address": "AA:BB:CC:DD:EE:FF",
  "serial_number": "GLS-2024-00001",
  "firmware_version": "1.2.0",
  "tenant_id": "tenant-uuid",
  "project_id": "project-uuid"
}
```

**响应 (201):**
```json
{
  "code": 0,
  "data": {
    "device_id": "uuid-v4",
    "tuya_device_id": "uuid-of-tuya-device",
    "ble_address": "AA:BB:CC:DD:EE:FF",
    "serial_number": "GLS-2024-00001",
    "firmware_version": "1.2.0",
    "status": "offline",
    "assigned_worker_id": null,
    "created_at": "2024-01-01T00:00:00Z"
  }
}
```

### 2.2 注册挎包终端

```
POST /api/v1/devices/bag-terminals
```

**请求 Body:**
```json
{
  "serial_number": "BAG-2024-00001",
  "tuya_gateway_id": "tuya-gateway-uuid",
  "sim_iccid": "89860491XXXXXXXXXXXXF",
  "location": {
    "lat": 31.2304,
    "lng": 121.4737
  },
  "tenant_id": "tenant-uuid"
}
```

### 2.3 眼镜-挎包绑定

```
POST /api/v1/devices/bindings
```

**请求 Body:**
```json
{
  "glasses_device_id": "glasses-uuid",
  "bag_terminal_id": "bag-uuid",
  "worker_id": "user-uuid",
  "inspection_id": "inspection-uuid (可选)"
}
```

**响应:**
```json
{
  "code": 0,
  "data": {
    "binding_id": "uuid-v4",
    "status": "active",
    "bound_at": "2024-01-01T00:00:00Z"
  }
}
```

### 2.4 解绑

```
PATCH /api/v1/devices/bindings/{binding_id}
```
**请求 Body:** `{"status": "released"}`

### 2.5 查询设备列表

```
GET /api/v1/devices/glasses?tenant_id={}&status={}&page=1&page_size=20
```

---

## 3. OTA 管理 (新增)

### 3.1 查询固件版本

```
GET /api/v1/ota/firmwares?device_type=glasses&model=esp32s3
```

**响应:**
```json
{
  "code": 0,
  "data": [
    {
      "firmware_id": "uuid-v4",
      "version": "1.3.0",
      "device_type": "glasses",
      "model": "esp32s3",
      "min_version": "1.0.0",
      "file_size": 1048576,
      "sha256": "abcdef0123456789...",
      "download_url": "https://minio.../firmware-1.3.0.bin",
      "release_notes": "修复 BLE 重连问题",
      "status": "published",
      "created_at": "2024-01-01T00:00:00Z"
    }
  ]
}
```

### 3.2 触发升级

```
POST /api/v1/ota/trigger
```

**请求 Body:**
```json
{
  "device_id": "glasses-uuid",
  "firmware_id": "firmware-uuid",
  "force": false
}
```

### 3.3 查询升级状态

```
GET /api/v1/ota/records?device_id={}&status={}
```

---

## 4. 遥测数据 (新增)

### 4.1 批量写入

```
POST /api/v1/telemetry/batch
```

**请求 Body:**
```json
{
  "device_id": "glasses-uuid",
  "data_points": [
    {
      "timestamp": "2024-01-01T00:00:00Z",
      "battery_level": 85,
      "cpu_usage": 45.2,
      "memory_usage": 62.8,
      "temperature": 42.5,
      "ble_signal": -65,
      "inference_time_ms": 120
    }
  ]
}
```

**响应 (201):**
```json
{
  "code": 0,
  "data": {
    "accepted": 100,
    "rejected": 0,
    "batch_id": "uuid-v4"
  }
}
```

### 4.2 查询遥测

```
GET /api/v1/telemetry?device_id={}&start={}&end={}&metric=battery_level
```

**响应:**
```json
{
  "code": 0,
  "data": {
    "device_id": "glasses-uuid",
    "metric": "battery_level",
    "points": [
      {"timestamp": "2024-01-01T00:00:00Z", "value": 85},
      {"timestamp": "2024-01-01T00:05:00Z", "value": 84}
    ]
  }
}
```

---

## 5. 巡检同步 (现有)

### 5.1 下发巡检任务

```
GET /api/v1/inspections?worker_id={}&status=pending
```

### 5.2 上传巡检结果

```
POST /api/v1/inspections/{inspection_id}/complete
```

**请求 Body:**
```json
{
  "completed_at": "2024-01-01T12:00:00Z",
  "summary": "巡检完成，发现 2 处缺陷",
  "route_points": 128,
  "distance_m": 3500
}
```

### 5.3 上传照片

```
POST /api/v1/uploads/photo
```
**Content-Type:** `multipart/form-data`
- `file`: 图片文件
- `inspection_id`: 巡检 ID
- `metadata`: JSON 字符串 (GPS/角度/检测信息)

**响应:**
```json
{
  "code": 0,
  "data": {
    "photo_id": "uuid-v4",
    "url": "https://minio.../photos/uuid.jpg",
    "thumbnail_url": "https://minio.../photos/uuid_thumb.jpg"
  }
}
```

---

## 6. RAG 接口 (现有)

### 6.1 创建 RAG 会话

```
POST /api/v1/rag/sessions
```

### 6.2 知识库同步 (离线回退)

当终端网络不可用时，RAG 查询在本地执行；网络恢复后通过以下接口同步：

```
POST /api/v1/rag/sync
```

**请求 Body:**
```json
{
  "session_id": "local-session-uuid",
  "queries": [
    {
      "question": "接触网拉出值标准是多少？",
      "answer": "正线区间不大于400mm...",
      "sources": ["规章/接触网检修规程.pdf#page=12"],
      "confidence": 0.92,
      "queried_at": "2024-01-01T11:00:00Z"
    }
  ]
}
```

---

## 7. 离线同步策略

### 7.1 优先级

网络恢复后，挎包终端按以下优先级同步缓存数据：

| 优先级 | 数据类型 | 接口 | 原因 |
|--------|---------|------|------|
| P0 (最高) | 告警 (alerts_cache) | POST /api/v1/inspections/{id}/alerts | 安全相关，需即时上报 |
| P1 | 照片 (inspection_photos_cache) | POST /api/v1/uploads/photo | 证据保全，防止本地丢失 |
| P2 | AI 检测 (ai_detection_cache) | POST /api/v1/inspections/{id}/detections | 结构化数据，体积小 |
| P3 | RAG 查询 (rag_query_cache) | POST /api/v1/rag/sync | 知识更新，非紧急 |
| P4 (最低) | 遥测 (device_telemetry) | POST /api/v1/telemetry/batch | 时序数据，可聚合后批量发送 |

### 7.2 同步状态管理

每条缓存记录通过 `sync_state` 表追踪:

| 字段 | 说明 |
|------|------|
| table_name | 源表名 |
| record_id | 记录主键 |
| direction | upload / download |
| status | pending / syncing / synced / failed |
| retry_count | 重试次数 (最多 5 次) |
| next_retry_at | 下次重试时间 (指数退避: 30s, 60s, 120s, 300s, 900s) |
| last_error | 最后一次错误信息 |

### 7.3 重试策略

- 初始间隔: 30 秒
- 退避: 指数增长 (×2)，上限 15 分钟
- 最大重试: 5 次后标记 `failed`，需人工干预
- 批量写入: 遥测数据每次最多 100 条合并为一个 HTTP 请求

---

## 8. 错误码定义

| HTTP | code | 含义 | 终端处理 |
|------|------|------|---------|
| 401 | 1001 | Token 过期 | 刷新 Token 后重试 |
| 403 | 1002 | 权限不足 | 记录日志，停止同步该资源 |
| 404 | 1003 | 资源不存在 | 标记记录为 skipped |
| 409 | 1004 | 冲突 (重复) | 标记为 synced (幂等) |
| 422 | 1005 | 参数校验失败 | 标记为 failed |
| 429 | 1006 | 限流 | 指数退避后重试 |
| 500 | 2001 | 服务端错误 | 指数退避重试 |
| 502/503 | 2002 | 网关不可用 | 延迟 60 秒后重试整个同步队列 |
