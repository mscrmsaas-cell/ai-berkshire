-- ============================================================
-- SaaS 后端 PostgreSQL 扩展 — 设备管理 7 张新表
--
-- 外键引用现有表（01-init.sql 创建）：
--   tenants, users, projects, inspections, inspection_photos
--
-- 新建表：
--   1. glasses_devices        — 智能眼镜设备
--   2. bag_terminals          — 挎包终端
--   3. glasses_bag_binding    — 眼镜-挎包动态绑定
--   4. camera_module_events   — 摄像头连接事件
--   5. ai_model_versions      — AI 模型版本管理
--   6. ota_upgrade_records    — OTA 升级记录
--   7. device_telemetry       — 设备遥测时序数据
-- ============================================================

-- ── 扩展（确保已存在）──
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS pg_trgm.gin_trgm_ops; -- 确保 trigram 索引支持

-- ============================================================
-- 1. glasses_devices — 智能眼镜设备
-- ============================================================
CREATE TABLE IF NOT EXISTS glasses_devices (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    tuya_device_id  VARCHAR(100) UNIQUE,          -- 涂鸦分配的子设备 ID
    ble_address     VARCHAR(20) UNIQUE NOT NULL,   -- BLE MAC 地址
    serial_number   VARCHAR(50) UNIQUE,            -- 设备序列号
    pid             VARCHAR(50),                    -- 涂鸦产品 ID
    name            VARCHAR(200),                   -- 设备名称
    firmware_version VARCHAR(50) DEFAULT '1.0.0',  -- 当前固件版本
    target_firmware_version VARCHAR(50),           -- 目标固件版本
    assigned_worker_id UUID REFERENCES users(id),  -- 分配的巡检员
    assigned_project_id UUID REFERENCES projects(id), -- 关联项目
    status          VARCHAR(20) DEFAULT 'registered', -- registered / online / offline / maintenance / retired
    mode            VARCHAR(20) DEFAULT 'standby',  -- standby / inspection / alert / charging / sleeping
    battery_level   SMALLINT DEFAULT 0,             -- 电量 0-100
    is_charging     BOOLEAN DEFAULT FALSE,
    camera_attached BOOLEAN DEFAULT FALSE,
    ble_rssi        SMALLINT DEFAULT 0,             -- BLE 信号 dBm
    last_seen_at    TIMESTAMPTZ,                    -- 最后在线时间
    metadata        JSONB DEFAULT '{}',            -- 扩展元数据
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- 2. bag_terminals — 挎包终端
-- ============================================================
CREATE TABLE IF NOT EXISTS bag_terminals (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    device_id       VARCHAR(100) UNIQUE NOT NULL,   -- 终端设备标识
    serial_number   VARCHAR(50) UNIQUE,            -- 序列号
    tuya_gateway_id VARCHAR(100) UNIQUE,            -- 涂鸦网关设备 ID
    sim_iccid       VARCHAR(20),                    -- SIM 卡 ICCID
    sim_imei        VARCHAR(15),                    -- 模组 IMEI
    hostname        VARCHAR(100),                   -- 主机名
    location        TEXT,                           -- 部署位置描述
    longitude       DECIMAL(10, 7),
    latitude        DECIMAL(10, 7),
    max_glasses     SMALLINT DEFAULT 4,             -- 最多连接眼镜数
    connected_glasses_count SMALLINT DEFAULT 0,     -- 当前连接数
    ai_models_loaded JSONB DEFAULT '[]',            -- 已加载的 AI 模型
    status          VARCHAR(20) DEFAULT 'registered', -- registered / online / offline / maintenance
    cpu_usage       REAL DEFAULT 0,
    memory_usage    REAL DEFAULT 0,
    temperature_c   REAL DEFAULT 0,
    last_heartbeat  TIMESTAMPTZ,                    -- 最后心跳
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- 3. glasses_bag_binding — 眼镜-挎包动态绑定
-- ============================================================
CREATE TABLE IF NOT EXISTS glasses_bag_binding (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    glasses_device_id UUID NOT NULL REFERENCES glasses_devices(id) ON DELETE CASCADE,
    bag_terminal_id UUID NOT NULL REFERENCES bag_terminals(id) ON DELETE CASCADE,
    binding_type    VARCHAR(20) DEFAULT 'auto',     -- auto / manual
    status          VARCHAR(20) DEFAULT 'active',   -- active / released / expired
    bound_at        TIMESTAMPTZ DEFAULT NOW(),
    released_at     TIMESTAMPTZ,
    session_inspection_id UUID REFERENCES inspections(id), -- 关联巡检会话
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(glasses_device_id, bag_terminal_id, status)
);

-- ============================================================
-- 4. camera_module_events — 摄像头连接事件
-- ============================================================
CREATE TABLE IF NOT EXISTS camera_module_events (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    glasses_device_id UUID REFERENCES glasses_devices(id) ON DELETE SET NULL,
    bag_terminal_id UUID REFERENCES bag_terminals(id) ON DELETE SET NULL,
    inspection_id   UUID REFERENCES inspections(id) ON DELETE SET NULL,
    event_type      VARCHAR(20) NOT NULL,           -- attached / detached
    connected_at    TIMESTAMPTZ,
    disconnected_at TIMESTAMPTZ,
    duration_seconds INTEGER,                        -- 连接时长
    frame_count     INTEGER DEFAULT 0,              -- 采集帧数
    metadata        JSONB DEFAULT '{}',             -- 额外信息（分辨率/质量等）
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- 5. ai_model_versions — AI 模型版本管理
-- ============================================================
CREATE TABLE IF NOT EXISTS ai_model_versions (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    model_name      VARCHAR(100) NOT NULL,          -- yolov8n_railway / bge-small-zh / phi-3-mini
    model_type      VARCHAR(50) NOT NULL,            -- detection / embedding / llm
    version         VARCHAR(50) NOT NULL,            -- 1.0.0
    minio_bucket    VARCHAR(100) NOT NULL,
    minio_object_name VARCHAR(500) NOT NULL,         -- MinIO 对象路径
    file_size_bytes BIGINT,
    sha256          VARCHAR(64),                     -- SHA-256 校验值
    architecture    VARCHAR(50),                    -- 模型架构描述
    input_shape     JSONB,                           -- 输入维度 {"input": [1,3,320,320]}
    output_classes  JSONB,                           -- 类别列表 ["crack","rust",...]
    accuracy_metrics JSONB,                          -- 性能指标 {"mAP50": 0.85, "mAP50-95": 0.62}
    status          VARCHAR(20) DEFAULT 'uploaded',  -- uploaded / verified / active / deprecated / retired
    is_active       BOOLEAN DEFAULT FALSE,           -- 是否为当前激活版本
    activated_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(model_name, version, tenant_id)
);

-- ============================================================
-- 6. ota_upgrade_records — OTA 升级记录
-- ============================================================
CREATE TABLE IF NOT EXISTS ota_upgrade_records (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    device_id       UUID NOT NULL,                   -- 关联 glasses_devices 或 bag_terminals
    device_type     VARCHAR(20) NOT NULL,            -- glasses / bag_terminal
    target_version  VARCHAR(50) NOT NULL,            -- 目标固件版本
    from_version    VARCHAR(50),                     -- 原固件版本
    firmware_url    TEXT NOT NULL,                   -- 固件下载 URL
    firmware_sha256 VARCHAR(64),                     -- 固件 SHA-256
    status          VARCHAR(20) DEFAULT 'pending',  -- pending / downloading / transferring / verifying / success / failed / cancelled
    progress        SMALLINT DEFAULT 0,              -- 升级进度 0-100
    chunks_transferred INTEGER DEFAULT 0,            -- 已传输分块数
    total_chunks    INTEGER DEFAULT 0,              -- 总分块数
    triggered_by    UUID REFERENCES users(id),       -- 触发升级的用户
    started_at      TIMESTAMPTZ DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    error_message   TEXT,                            -- 失败原因
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- 7. device_telemetry — 设备遥测时序数据
-- ============================================================
CREATE TABLE IF NOT EXISTS device_telemetry (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    device_id       UUID NOT NULL,                   -- 关联 glasses_devices 或 bag_terminals
    device_type     VARCHAR(20) NOT NULL,            -- glasses / bag_terminal
    -- 眼镜遥测
    battery_level   SMALLINT,
    battery_voltage_mv INTEGER,
    is_charging     BOOLEAN,
    temperature_c   REAL,
    ble_rssi        SMALLINT,
    -- 终端遥测
    cpu_usage       REAL,
    memory_usage    REAL,
    disk_usage      REAL,
    -- AI 遥测
    ai_inference_time_ms INTEGER,
    ai_detections_count INTEGER,
    rag_query_count INTEGER,
    -- 网络遥测
    network_type    VARCHAR(10),                     -- 5g / wifi / none
    signal_rsrp     SMALLINT,
    signal_rsrq     SMALLINT,
    signal_sinr     SMALLINT,
    -- 扩展
    raw_data        JSONB DEFAULT '{}',              -- 原始遥测 JSON
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ============================================================
-- 索引
-- ============================================================

-- glasses_devices
CREATE INDEX IF NOT EXISTS idx_glasses_tenant ON glasses_devices(tenant_id);
CREATE INDEX IF NOT EXISTS idx_glasses_status ON glasses_devices(status);
CREATE INDEX IF NOT EXISTS idx_glasses_worker ON glasses_devices(assigned_worker_id);
CREATE INDEX IF NOT EXISTS idx_glasses_project ON glasses_devices(assigned_project_id);
CREATE INDEX IF NOT EXISTS idx_glasses_last_seen ON glasses_devices(last_seen_at);
CREATE INDEX IF NOT EXISTS idx_glasses_tuya_device ON glasses_devices(tuya_device_id);

-- bag_terminals
CREATE INDEX IF NOT EXISTS idx_bag_tenant ON bag_terminals(tenant_id);
CREATE INDEX IF NOT EXISTS idx_bag_status ON bag_terminals(status);
CREATE INDEX IF NOT EXISTS idx_bag_tuya_gateway ON bag_terminals(tuya_gateway_id);
CREATE INDEX IF NOT EXISTS idx_bag_last_heartbeat ON bag_terminals(last_heartbeat);

-- glasses_bag_binding
CREATE INDEX IF NOT EXISTS idx_binding_glasses ON glasses_bag_binding(glasses_device_id);
CREATE INDEX IF NOT EXISTS idx_binding_bag ON glasses_bag_binding(bag_terminal_id);
CREATE INDEX IF NOT EXISTS idx_binding_status ON glasses_bag_binding(status);
CREATE INDEX IF NOT EXISTS idx_binding_tenant ON glasses_bag_binding(tenant_id);
CREATE INDEX IF NOT EXISTS idx_binding_active ON glasses_bag_binding(glasses_device_id, bag_terminal_id)
    WHERE status = 'active';

-- camera_module_events
CREATE INDEX IF NOT EXISTS idx_cam_events_glasses ON camera_module_events(glasses_device_id);
CREATE INDEX IF NOT EXISTS idx_cam_events_bag ON camera_module_events(bag_terminal_id);
CREATE INDEX IF NOT EXISTS idx_cam_events_inspection ON camera_module_events(inspection_id);
CREATE INDEX IF NOT EXISTS idx_cam_events_type ON camera_module_events(event_type);
CREATE INDEX IF NOT EXISTS idx_cam_events_created ON camera_module_events(created_at);

-- ai_model_versions
CREATE INDEX IF NOT EXISTS idx_ai_models_tenant ON ai_model_versions(tenant_id);
CREATE INDEX IF NOT EXISTS idx_ai_models_name_version ON ai_model_versions(model_name, version);
CREATE INDEX IF NOT EXISTS idx_ai_models_status ON ai_model_versions(status);
CREATE INDEX IF NOT EXISTS idx_ai_models_active ON ai_model_versions(tenant_id, model_name)
    WHERE is_active = TRUE;

-- ota_upgrade_records
CREATE INDEX IF NOT EXISTS idx_ota_device ON ota_upgrade_records(device_id, device_type);
CREATE INDEX IF NOT EXISTS idx_ota_status ON ota_upgrade_records(status);
CREATE INDEX IF NOT EXISTS idx_ota_tenant ON ota_upgrade_records(tenant_id);
CREATE INDEX IF NOT EXISTS idx_ota_created ON ota_upgrade_records(created_at);

-- device_telemetry
CREATE INDEX IF NOT EXISTS idx_telemetry_device ON device_telemetry(device_id, device_type);
CREATE INDEX IF NOT EXISTS idx_telemetry_tenant ON device_telemetry(tenant_id);
CREATE INDEX IF NOT EXISTS idx_telemetry_recorded ON device_telemetry(recorded_at);
CREATE INDEX IF NOT EXISTS idx_telemetry_device_time ON device_telemetry(device_id, device_type, recorded_at);
-- 时序数据按天分区索引（Brin 索引更适合大表）
CREATE INDEX IF NOT EXISTS idx_telemetry_brin ON device_telemetry USING BRIN (recorded_at)
    WITH (pages_per_range = 32);

-- ============================================================
-- 触发器：自动更新 updated_at
-- ============================================================

-- glasses_devices
CREATE TRIGGER IF NOT EXISTS trg_glasses_devices_updated
    BEFORE UPDATE ON glasses_devices
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- bag_terminals
CREATE TRIGGER IF NOT EXISTS trg_bag_terminals_updated
    BEFORE UPDATE ON bag_terminals
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- glasses_bag_binding
CREATE TRIGGER IF NOT EXISTS trg_binding_updated
    BEFORE UPDATE ON glasses_bag_binding
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ai_model_versions
CREATE TRIGGER IF NOT EXISTS trg_ai_models_updated
    BEFORE UPDATE ON ai_model_versions
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ota_upgrade_records
CREATE TRIGGER IF NOT EXISTS trg_ota_updated
    BEFORE UPDATE ON ota_upgrade_records
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ============================================================
-- 触发器：AI 模型版本激活互斥（同一 tenant+model_name 只有一个 active）
-- ============================================================
CREATE OR REPLACE FUNCTION ensure_single_active_model()
RETURNS TRIGGER AS $$
BEGIN
    -- 如果设置为 active，先将同 tenant+model_name 的其他记录设为非 active
    IF NEW.is_active = TRUE THEN
        UPDATE ai_model_versions
        SET is_active = FALSE,
            status = 'deprecated',
            updated_at = NOW()
        WHERE tenant_id = NEW.tenant_id
          AND model_name = NEW.model_name
          AND id != NEW.id;
        NEW.status = 'active';
        NEW.activated_at = NOW();
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER IF NOT EXISTS trg_ai_model_activate
    BEFORE INSERT OR UPDATE ON ai_model_versions
    FOR EACH ROW EXECUTE FUNCTION ensure_single_active_model();

-- ============================================================
-- 触发器：眼镜-挎包绑定状态变更（同步更新设备 connected_glasses_count）
-- ============================================================
CREATE OR REPLACE FUNCTION sync_binding_count()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'INSERT' AND NEW.status = 'active' THEN
        UPDATE bag_terminals
        SET connected_glasses_count = connected_glasses_count + 1
        WHERE id = NEW.bag_terminal_id;
    ELSIF TG_OP = 'UPDATE' AND OLD.status = 'active' AND NEW.status != 'active' THEN
        UPDATE bag_terminals
        SET connected_glasses_count = GREATEST(connected_glasses_count - 1, 0)
        WHERE id = NEW.bag_terminal_id;
    ELSIF TG_OP = 'DELETE' AND OLD.status = 'active' THEN
        UPDATE bag_terminals
        SET connected_glasses_count = GREATEST(connected_glasses_count - 1, 0)
        WHERE id = OLD.bag_terminal_id;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER IF NOT EXISTS trg_binding_count_sync
    AFTER INSERT OR UPDATE OR DELETE ON glasses_bag_binding
    FOR EACH ROW EXECUTE FUNCTION sync_binding_count();

-- ============================================================
-- 设备状态触发器：在线/离线状态自动同步
-- ============================================================
CREATE OR REPLACE FUNCTION update_device_offline()
RETURNS TRIGGER AS $$
BEGIN
    -- 当设备超过 5 分钟未心跳，自动设为 offline
    IF NEW.last_heartbeat IS NOT NULL
       AND NEW.last_heartbeat < NOW() - INTERVAL '5 minutes'
       AND NEW.status = 'online' THEN
        NEW.status := 'offline';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER IF NOT EXISTS trg_bag_terminal_auto_offline
    BEFORE UPDATE ON bag_terminals
    FOR EACH ROW EXECUTE FUNCTION update_device_offline();

-- ============================================================
-- 视图：设备在线状态汇总
-- ============================================================
CREATE OR REPLACE VIEW v_device_online_summary AS
SELECT
    bt.tenant_id,
    bt.id AS bag_terminal_id,
    bt.device_id AS bag_terminal_device_id,
    bt.status AS bag_terminal_status,
    bt.connected_glasses_count,
    bt.max_glasses,
    bt.cpu_usage,
    bt.memory_usage,
    bt.temperature_c,
    bt.last_heartbeat,
    bt.sim_iccid,
    COUNT(gd.id) AS total_glasses_registered,
    COUNT(gd.id) FILTER (WHERE gd.status = 'online') AS glasses_online,
    COUNT(gd.id) FILTER (WHERE gd.battery_level < 20) AS glasses_low_battery,
    AVG(gd.battery_level) FILTER (WHERE gd.status = 'online') AS avg_battery_level
FROM bag_terminals bt
LEFT JOIN glasses_devices gd ON gd.assigned_project_id IS NOT NULL
    AND gd.tenant_id = bt.tenant_id
GROUP BY bt.tenant_id, bt.id, bt.device_id, bt.status, bt.connected_glasses_count,
         bt.max_glasses, bt.cpu_usage, bt.memory_usage, bt.temperature_c,
         bt.last_heartbeat, bt.sim_iccid;

-- ============================================================
-- 注释
-- ============================================================
COMMENT ON TABLE glasses_devices IS '智能眼镜设备注册表 — 外键关联 tenants/users/projects';
COMMENT ON TABLE bag_terminals IS '挎包终端设备表 — 外键关联 tenants';
COMMENT ON TABLE glasses_bag_binding IS '眼镜-挎包终端动态绑定关系 — 外键关联 glasses_devices/bag_terminals/inspections';
COMMENT ON TABLE camera_module_events IS '摄像头模块连接事件记录 — 外键关联 glasses_devices/bag_terminals/inspections';
COMMENT ON TABLE ai_model_versions IS 'AI 模型版本管理 — ONNX/GGUF 模型版本追踪';
COMMENT ON TABLE ota_upgrade_records IS '设备 OTA 升级记录 — 固件版本从到/状态/进度';
COMMENT ON TABLE device_telemetry IS '设备遥测时序数据 — 电量/CPU/内存/温度/信号';
COMMENT ON COLUMN device_telemetry.recorded_at IS '遥测数据采集时间（带时区）';
