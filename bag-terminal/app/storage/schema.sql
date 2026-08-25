-- ============================================================
-- 挎包终端本地 SQLite 数据库 Schema
-- 8 张表 + WAL 模式 + 外键约束 + 索引 + 触发器
-- ============================================================

-- ── PRAGMA 设置 ──
PRAGMA journal_mode = WAL;           -- WAL 模式提升并发读写性能
PRAGMA foreign_keys = ON;            -- 启用外键约束
PRAGMA synchronous = NORMAL;         -- WAL 下 NORMAL 即可保证数据安全
PRAGMA cache_size = -20000;          -- 20MB 页缓存
PRAGMA temp_store = MEMORY;         -- 临时表存内存
PRAGMA busy_timeout = 5000;         -- 5 秒锁等待

-- ============================================================
-- 1. glasses_devices — 眼镜设备注册
-- ============================================================
CREATE TABLE IF NOT EXISTS glasses_devices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tuya_device_id  TEXT NOT NULL UNIQUE,        -- 涂鸦分配的子设备 ID
    ble_address     TEXT NOT NULL UNIQUE,        -- BLE MAC 地址
    serial_number   TEXT,                        -- 眼镜序列号
    firmware_version TEXT DEFAULT '1.0.0',       -- 当前固件版本
    assigned_worker TEXT,                        -- 分配的工人姓名
    worker_id       TEXT,                        -- 工人 UUID
    project_id      TEXT,                        -- 关联项目 ID
    status          TEXT DEFAULT 'offline',      -- offline / online / charging / sleeping
    battery_level   INTEGER DEFAULT 0,           -- 电量百分比 0-100
    is_charging     INTEGER DEFAULT 0,           -- 是否充电中 0/1
    camera_attached INTEGER DEFAULT 0,           -- 摄像头是否接入 0/1
    mode            TEXT DEFAULT 'standby',      -- standby / inspection / alert / charging
    ble_rssi        INTEGER DEFAULT 0,           -- BLE 信号强度 dBm
    last_seen_at    TEXT,                        -- 最后通信时间 ISO8601
    registered_at   TEXT DEFAULT (datetime('now')),
    updated_at     TEXT DEFAULT (datetime('now'))
);

-- ============================================================
-- 2. camera_module_events — 摄像头连接记录
-- ============================================================
CREATE TABLE IF NOT EXISTS camera_module_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id      INTEGER NOT NULL,             -- 关联 glasses_devices.id
    ble_address     TEXT NOT NULL,                -- 眼镜 BLE 地址
    event_type     TEXT NOT NULL,                -- attached / detached
    connected_at    TEXT,                         -- 连接时间
    disconnected_at TEXT,                         -- 断开时间
    frame_count    INTEGER DEFAULT 0,             -- 采集帧数
    inspection_id  TEXT,                          -- 关联巡检 ID
    notes          TEXT,
    created_at     TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (device_id) REFERENCES glasses_devices(id) ON DELETE CASCADE
);

-- ============================================================
-- 3. inspection_tasks — 巡检任务缓存
-- ============================================================
CREATE TABLE IF NOT EXISTS inspection_tasks (
    id              TEXT PRIMARY KEY,             -- SaaS UUID
    project_id     TEXT NOT NULL,
    inspector_id   TEXT,                          -- 巡检员 UUID
    title          TEXT NOT NULL,
    type           TEXT DEFAULT 'routine',        -- routine / emergency / follow_up
    status         TEXT DEFAULT 'pending',        -- pending / in_progress / completed / failed
    location       TEXT,
    longitude      REAL,
    latitude       REAL,
    notes          TEXT,
    ai_findings    TEXT,                           -- JSON: AI 检测结果
    assigned_glasses_id TEXT,                     -- 分配的眼镜设备
    synced_to_cloud INTEGER DEFAULT 0,            -- 0=未同步 1=已同步
    cloud_synced_at TEXT,
    created_at     TEXT DEFAULT (datetime('now')),
    completed_at   TEXT,
    updated_at     TEXT DEFAULT (datetime('now'))
);

-- ============================================================
-- 4. inspection_photos_cache — 照片缓存
-- ============================================================
CREATE TABLE IF NOT EXISTS inspection_photos_cache (
    id              TEXT PRIMARY KEY,             -- 本地生成 UUID
    inspection_id  TEXT NOT NULL,                 -- 关联巡检任务
    local_path     TEXT NOT NULL,                 -- SD 卡本地路径
    minio_bucket   TEXT,                          -- MinIO 目标桶
    minio_object_name TEXT,                       -- MinIO 对象名
    original_filename TEXT,
    file_size      INTEGER,
    mime_type      TEXT DEFAULT 'image/jpeg',
    ai_detection   TEXT,                           -- JSON: YOLO 检测结果
    taken_at      TEXT,
    upload_status  TEXT DEFAULT 'pending',        -- pending / uploading / uploaded / failed
    retry_count    INTEGER DEFAULT 0,
    last_error     TEXT,
    created_at     TEXT DEFAULT (datetime('now')),
    uploaded_at    TEXT,
    FOREIGN KEY (inspection_id) REFERENCES inspection_tasks(id) ON DELETE CASCADE
);

-- ============================================================
-- 5. ai_detection_cache — AI 检测结果缓存
-- ============================================================
CREATE TABLE IF NOT EXISTS ai_detection_cache (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    photo_id       TEXT,                           -- 关联照片 ID
    inspection_id  TEXT,
    device_id      INTEGER,
    model_name     TEXT NOT NULL,                  -- yolov8n_railway
    model_version  TEXT NOT NULL,                  -- 1.0.0
    detection_json TEXT NOT NULL,                 -- JSON: 检测框+类别+置信度
    class_counts   TEXT,                           -- JSON: 各类别数量
    inference_time_ms INTEGER DEFAULT 0,           -- 推理耗时
    confidence_avg REAL DEFAULT 0.0,              -- 平均置信度
    synced_to_cloud INTEGER DEFAULT 0,
    created_at     TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (photo_id) REFERENCES inspection_photos_cache(id) ON DELETE SET NULL,
    FOREIGN KEY (inspection_id) REFERENCES inspection_tasks(id) ON DELETE CASCADE,
    FOREIGN KEY (device_id) REFERENCES glasses_devices(id) ON DELETE SET NULL
);

-- ============================================================
-- 6. rag_query_cache — RAG 问答缓存
-- ============================================================
CREATE TABLE IF NOT EXISTS rag_query_cache (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    question       TEXT NOT NULL,
    answer         TEXT,
    sources        TEXT,                           -- JSON: 检索来源
    confidence     REAL DEFAULT 0.0,
    project_id     TEXT,
    conversation_id TEXT,
    response_time_ms INTEGER DEFAULT 0,
    from_local     INTEGER DEFAULT 1,              -- 1=本地RAG 0=远程回退
    synced_to_cloud INTEGER DEFAULT 0,
    created_at     TEXT DEFAULT (datetime('now'))
);

-- ============================================================
-- 7. alerts_cache — 告警缓存（sync_priority = 最高）
-- ============================================================
CREATE TABLE IF NOT EXISTS alerts_cache (
    id              TEXT PRIMARY KEY,             -- 本地生成 UUID
    device_id      INTEGER,
    inspection_id  TEXT,
    project_id     TEXT,
    alert_type     TEXT NOT NULL,                -- no_helmet / intrusion / equipment_fault
    severity       TEXT DEFAULT 'warning',        -- info / warning / danger
    description    TEXT,
    screenshot_path TEXT,                          -- 告警截图本地路径
    cloud_alert_id TEXT,                           -- 同步后 SaaS 分配的 ID
    sync_priority  INTEGER DEFAULT 0,            -- 0=最高(告警)
    sync_status    TEXT DEFAULT 'pending',        -- pending / syncing / synced / failed
    retry_count    INTEGER DEFAULT 0,
    last_error     TEXT,
    created_at     TEXT DEFAULT (datetime('now')),
    synced_at      TEXT,
    FOREIGN KEY (device_id) REFERENCES glasses_devices(id) ON DELETE SET NULL,
    FOREIGN KEY (inspection_id) REFERENCES inspection_tasks(id) ON DELETE CASCADE
);

-- ============================================================
-- 8. sync_state — 同步状态追踪
-- ============================================================
CREATE TABLE IF NOT EXISTS sync_state (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name     TEXT NOT NULL,                 -- 关联表名
    record_id      TEXT NOT NULL,                 -- 记录 ID
    sync_direction TEXT DEFAULT 'upload',         -- upload / download
    sync_priority  INTEGER DEFAULT 5,            -- 0=最高 5=最低
    status         TEXT DEFAULT 'pending',        -- pending / in_progress / success / failed
    payload_json   TEXT,                           -- 待同步数据 JSON 快照
    retry_count    INTEGER DEFAULT 0,
    max_retries    INTEGER DEFAULT 5,
    last_error     TEXT,
    next_retry_at  TEXT,                           -- 下次重试时间
    created_at     TEXT DEFAULT (datetime('now')),
    updated_at     TEXT DEFAULT (datetime('now')),
    UNIQUE(table_name, record_id, sync_direction)
);

-- ============================================================
-- 索引
-- ============================================================

-- glasses_devices
CREATE INDEX IF NOT EXISTS idx_glasses_devices_ble ON glasses_devices(ble_address);
CREATE INDEX IF NOT EXISTS idx_glasses_devices_status ON glasses_devices(status);
CREATE INDEX IF NOT EXISTS idx_glasses_devices_worker ON glasses_devices(worker_id);

-- camera_module_events
CREATE INDEX IF NOT EXISTS idx_cam_events_device ON camera_module_events(device_id);
CREATE INDEX IF NOT EXISTS idx_cam_events_type ON camera_module_events(event_type);
CREATE INDEX IF NOT EXISTS idx_cam_events_created ON camera_module_events(created_at);

-- inspection_tasks
CREATE INDEX IF NOT EXISTS idx_tasks_project ON inspection_tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON inspection_tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_synced ON inspection_tasks(synced_to_cloud);
CREATE INDEX IF NOT EXISTS idx_tasks_glasses ON inspection_tasks(assigned_glasses_id);

-- inspection_photos_cache
CREATE INDEX IF NOT EXISTS idx_photos_inspection ON inspection_photos_cache(inspection_id);
CREATE INDEX IF NOT EXISTS idx_photos_upload_status ON inspection_photos_cache(upload_status);
CREATE INDEX IF NOT EXISTS idx_photos_retry ON inspection_photos_cache(retry_count);

-- ai_detection_cache
CREATE INDEX IF NOT EXISTS idx_detection_photo ON ai_detection_cache(photo_id);
CREATE INDEX IF NOT EXISTS idx_detection_inspection ON ai_detection_cache(inspection_id);
CREATE INDEX IF NOT EXISTS idx_detection_model ON ai_detection_cache(model_name, model_version);

-- rag_query_cache
CREATE INDEX IF NOT EXISTS idx_rag_project ON rag_query_cache(project_id);
CREATE INDEX IF NOT EXISTS idx_rag_synced ON rag_query_cache(synced_to_cloud);
CREATE INDEX IF NOT EXISTS idx_rag_question ON rag_query_cache(question);

-- alerts_cache
CREATE INDEX IF NOT EXISTS idx_alerts_severity ON alerts_cache(severity);
CREATE INDEX IF NOT EXISTS idx_alerts_sync_status ON alerts_cache(sync_status);
CREATE INDEX IF NOT EXISTS idx_alerts_sync_priority ON alerts_cache(sync_priority);
CREATE INDEX IF NOT EXISTS idx_alerts_device ON alerts_cache(device_id);
CREATE INDEX IF NOT EXISTS idx_alerts_inspection ON alerts_cache(inspection_id);

-- sync_state
CREATE INDEX IF NOT EXISTS idx_sync_state_table ON sync_state(table_name, record_id);
CREATE INDEX IF NOT EXISTS idx_sync_state_status ON sync_state(status);
CREATE INDEX IF NOT EXISTS idx_sync_state_priority ON sync_state(sync_priority, next_retry_at);
CREATE INDEX IF NOT EXISTS idx_sync_state_retry ON sync_state(next_retry_at) WHERE status = 'failed';

-- ============================================================
-- 触发器：自动更新 updated_at
-- ============================================================

CREATE TRIGGER IF NOT EXISTS trg_glasses_devices_updated
    AFTER UPDATE ON glasses_devices
    FOR EACH ROW
    BEGIN
        UPDATE glasses_devices SET updated_at = datetime('now') WHERE id = NEW.id;
    END;

CREATE TRIGGER IF NOT EXISTS trg_inspection_tasks_updated
    AFTER UPDATE ON inspection_tasks
    FOR EACH ROW
    BEGIN
        UPDATE inspection_tasks SET updated_at = datetime('now') WHERE id = NEW.id;
    END;

CREATE TRIGGER IF NOT EXISTS trg_sync_state_updated
    AFTER UPDATE ON sync_state
    FOR EACH ROW
    BEGIN
        UPDATE sync_state SET updated_at = datetime('now') WHERE id = NEW.id;
    END;

-- ============================================================
-- 触发器：告警自动入同步队列（优先级最高）
-- ============================================================

CREATE TRIGGER IF NOT EXISTS trg_alerts_enqueue_sync
    AFTER INSERT ON alerts_cache
    FOR EACH ROW
    WHEN NEW.sync_status = 'pending'
    BEGIN
        INSERT OR IGNORE INTO sync_state (table_name, record_id, sync_direction, sync_priority, status, payload_json, next_retry_at)
        VALUES ('alerts_cache', NEW.id, 'upload', 0, 'pending',
                json_object('alert_id', NEW.id, 'type', NEW.alert_type,
                           'severity', NEW.severity, 'project_id', NEW.project_id),
                datetime('now'));
    END;

-- ============================================================
-- 触发器：照片自动入同步队列（优先级 1）
-- ============================================================

CREATE TRIGGER IF NOT EXISTS trg_photos_enqueue_sync
    AFTER INSERT ON inspection_photos_cache
    FOR EACH ROW
    WHEN NEW.upload_status = 'pending'
    BEGIN
        INSERT OR IGNORE INTO sync_state (table_name, record_id, sync_direction, sync_priority, status, payload_json, next_retry_at)
        VALUES ('inspection_photos_cache', NEW.id, 'upload', 1, 'pending',
                json_object('photo_id', NEW.id, 'inspection_id', NEW.inspection_id,
                           'local_path', NEW.local_path),
                datetime('now'));
    END;

-- ============================================================
-- 触发器：AI 检测结果自动入同步队列（优先级 2）
-- ============================================================

CREATE TRIGGER IF NOT EXISTS trg_detection_enqueue_sync
    AFTER INSERT ON ai_detection_cache
    FOR EACH ROW
    WHEN NEW.synced_to_cloud = 0
    BEGIN
        INSERT OR IGNORE INTO sync_state (table_name, record_id, sync_direction, sync_priority, status, payload_json, next_retry_at)
        VALUES ('ai_detection_cache', CAST(NEW.id AS TEXT), 'upload', 2, 'pending',
                json_object('detection_id', NEW.id, 'photo_id', NEW.photo_id,
                           'model_version', NEW.model_version),
                datetime('now'));
    END;
