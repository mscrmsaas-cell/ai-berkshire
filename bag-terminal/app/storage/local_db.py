"""挎包终端本地 SQLite 数据库管理。

基于 aiosqlite 异步驱动，管理 8 张表的 CRUD 操作：
1. glasses_devices        — 眼镜设备注册
2. camera_module_events   — 摄像头连接记录
3. inspection_tasks       — 巡检任务缓存
4. inspection_photos_cache— 照片缓存
5. ai_detection_cache     — AI 检测结果
6. rag_query_cache        — RAG 问答缓存
7. alerts_cache           — 告警缓存
8. sync_state             — 同步状态追踪

特性：
- WAL 模式 + 外键约束
- 按 sync_priority 优先级查询待同步记录
- 异步上下文管理器
- 表级 CRUD 封装
- 同步状态状态机管理
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import aiosqlite
import structlog

logger = structlog.get_logger(__name__)

# schema.sql 路径
SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")

# 8 张表名常量
TABLE_GLASSES_DEVICES = "glasses_devices"
TABLE_CAMERA_EVENTS = "camera_module_events"
TABLE_INSPECTION_TASKS = "inspection_tasks"
TABLE_PHOTOS_CACHE = "inspection_photos_cache"
TABLE_AI_DETECTION = "ai_detection_cache"
TABLE_RAG_CACHE = "rag_query_cache"
TABLE_ALERTS = "alerts_cache"
TABLE_SYNC_STATE = "sync_state"

ALL_TABLES = [
    TABLE_GLASSES_DEVICES,
    TABLE_CAMERA_EVENTS,
    TABLE_INSPECTION_TASKS,
    TABLE_PHOTOS_CACHE,
    TABLE_AI_DETECTION,
    TABLE_RAG_CACHE,
    TABLE_ALERTS,
    TABLE_SYNC_STATE,
]


class LocalDatabase:
    """挎包终端异步 SQLite 数据库管理器。

    使用示例::

        async with LocalDatabase("/data/bag_terminal.db") as db:
            await db.upsert_glasses_device(
                tuya_device_id="tuya123",
                ble_address="AA:BB:CC:DD:EE:FF",
                firmware_version="1.0.0",
            )
            pending = await db.get_pending_sync_records(limit=50)
    """

    def __init__(self, db_path: str = "/data/bag-terminal/local.db") -> None:
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    # ── 生命周期 ──────────────────────────────────────────────────

    async def __aenter__(self) -> LocalDatabase:
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()

    async def connect(self) -> None:
        """连接数据库并初始化 Schema。"""
        os.makedirs(os.path.dirname(self._db_path) or ".", exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        # PRAGMA 设置
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA synchronous = NORMAL")
        await self._conn.execute("PRAGMA busy_timeout = 5000")
        await self._init_schema()
        await self._conn.commit()
        logger.info("local_db_connected", path=self._db_path)

    async def close(self) -> None:
        """关闭数据库连接。"""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            logger.info("local_db_closed")

    async def _init_schema(self) -> None:
        """从 schema.sql 初始化表结构。"""
        if not os.path.exists(SCHEMA_PATH):
            logger.warning("schema_sql_not_found", path=SCHEMA_PATH)
            return
        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            schema_sql = f.read()
        # 逐条执行 SQL 语句
        await self._conn.executescript(schema_sql)
        logger.info("schema_initialized", tables=ALL_TABLES)

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._conn

    # ── 通用辅助方法 ──────────────────────────────────────────────

    @staticmethod
    def _now_iso() -> str:
        """返回当前 UTC 时间 ISO 字符串。"""
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _generate_uuid() -> str:
        return str(uuid.uuid4())

    async def _execute(
        self, sql: str, params: tuple | None = None
    ) -> aiosqlite.Cursor:
        """执行单条 SQL（自动 commit）。"""
        cursor = await self.conn.execute(sql, params or ())
        await self.conn.commit()
        return cursor

    async def _fetchone(
        self, sql: str, params: tuple | None = None
    ) -> dict[str, Any] | None:
        """查询单行，返回字典。"""
        cursor = await self.conn.execute(sql, params or ())
        row = await cursor.fetchone()
        if row is None:
            return None
        columns = [desc[0] for desc in cursor.description]
        return dict(zip(columns, row, strict=True))

    async def _fetchall(
        self, sql: str, params: tuple | None = None
    ) -> list[dict[str, Any]]:
        """查询多行，返回字典列表。"""
        cursor = await self.conn.execute(sql, params or ())
        rows = await cursor.fetchall()
        columns = [desc[0] for desc in cursor.description]
        return [dict(zip(columns, row, strict=True)) for row in rows]

    # ── 1. glasses_devices ────────────────────────────────────────

    async def upsert_glasses_device(
        self,
        tuya_device_id: str,
        ble_address: str,
        firmware_version: str = "1.0.0",
        assigned_worker: str | None = None,
        worker_id: str | None = None,
        project_id: str | None = None,
        serial_number: str | None = None,
    ) -> int:
        """插入或更新眼镜设备记录。"""
        await self._execute(
            f"""
            INSERT INTO {TABLE_GLASSES_DEVICES}
                (tuya_device_id, ble_address, firmware_version,
                 assigned_worker, worker_id, project_id, serial_number,
                 status, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'online', ?)
            ON CONFLICT(tuya_device_id) DO UPDATE SET
                ble_address = excluded.ble_address,
                firmware_version = excluded.firmware_version,
                assigned_worker = COALESCE(excluded.assigned_worker, glasses_devices.assigned_worker),
                worker_id = COALESCE(excluded.worker_id, glasses_devices.worker_id),
                project_id = COALESCE(excluded.project_id, glasses_devices.project_id),
                status = 'online',
                last_seen_at = excluded.last_seen_at
            """,
            (
                tuya_device_id, ble_address, firmware_version,
                assigned_worker, worker_id, project_id, serial_number,
                self._now_iso(),
            ),
        )
        cursor = await self.conn.execute(
            f"SELECT id FROM {TABLE_GLASSES_DEVICES} WHERE tuya_device_id = ?",
            (tuya_device_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row else -1

    async def get_glasses_device(
        self, tuya_device_id: str | None = None, ble_address: str | None = None
    ) -> dict[str, Any] | None:
        """查询眼镜设备。"""
        if tuya_device_id:
            return await self._fetchone(
                f"SELECT * FROM {TABLE_GLASSES_DEVICES} WHERE tuya_device_id = ?",
                (tuya_device_id,),
            )
        if ble_address:
            return await self._fetchone(
                f"SELECT * FROM {TABLE_GLASSES_DEVICES} WHERE ble_address = ?",
                (ble_address,),
            )
        return None

    async def update_glasses_status(
        self,
        tuya_device_id: str,
        status: str | None = None,
        battery_level: int | None = None,
        is_charging: bool | None = None,
        camera_attached: bool | None = None,
        mode: str | None = None,
        ble_rssi: int | None = None,
    ) -> None:
        """更新眼镜设备状态。"""
        sets: list[str] = []
        params: list[Any] = []
        if status is not None:
            sets.append("status = ?")
            params.append(status)
        if battery_level is not None:
            sets.append("battery_level = ?")
            params.append(battery_level)
        if is_charging is not None:
            sets.append("is_charging = ?")
            params.append(1 if is_charging else 0)
        if camera_attached is not None:
            sets.append("camera_attached = ?")
            params.append(1 if camera_attached else 0)
        if mode is not None:
            sets.append("mode = ?")
            params.append(mode)
        if ble_rssi is not None:
            sets.append("ble_rssi = ?")
            params.append(ble_rssi)
        sets.append("last_seen_at = ?")
        params.append(self._now_iso())
        params.append(tuya_device_id)
        await self._execute(
            f"UPDATE {TABLE_GLASSES_DEVICES} SET {', '.join(sets)} WHERE tuya_device_id = ?",
            tuple(params),
        )

    async def list_glasses_devices(self, status: str | None = None) -> list[dict[str, Any]]:
        """列出所有/按状态的眼镜设备。"""
        if status:
            return await self._fetchall(
                f"SELECT * FROM {TABLE_GLASSES_DEVICES} WHERE status = ? ORDER BY last_seen_at DESC",
                (status,),
            )
        return await self._fetchall(
            f"SELECT * FROM {TABLE_GLASSES_DEVICES} ORDER BY last_seen_at DESC"
        )

    async def delete_glasses_device(self, tuya_device_id: str) -> None:
        await self._execute(
            f"DELETE FROM {TABLE_GLASSES_DEVICES} WHERE tuya_device_id = ?",
            (tuya_device_id,),
        )

    # ── 2. camera_module_events ───────────────────────────────────

    async def log_camera_event(
        self,
        device_id: int,
        ble_address: str,
        event_type: str,
        inspection_id: str | None = None,
        connected_at: str | None = None,
    ) -> int:
        """记录摄像头连接/断开事件。"""
        cursor = await self._execute(
            f"""
            INSERT INTO {TABLE_CAMERA_EVENTS}
                (device_id, ble_address, event_type, connected_at, inspection_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (device_id, ble_address, event_type, connected_at or self._now_iso(), inspection_id),
        )
        return cursor.lastrowid or -1

    async def update_camera_event_disconnect(
        self, event_id: int, disconnected_at: str | None = None, frame_count: int = 0
    ) -> None:
        """更新摄像头断开事件。"""
        await self._execute(
            f"""
            UPDATE {TABLE_CAMERA_EVENTS}
            SET disconnected_at = ?, frame_count = ?
            WHERE id = ?
            """,
            (disconnected_at or self._now_iso(), frame_count, event_id),
        )

    # ── 3. inspection_tasks ───────────────────────────────────────

    async def upsert_inspection_task(self, task: dict[str, Any]) -> str:
        """插入或更新巡检任务缓存（从 SaaS 同步）。"""
        task_id = task.get("id") or self._generate_uuid()
        ai_findings = task.get("ai_findings")
        if isinstance(ai_findings, (dict, list)):
            ai_findings = json.dumps(ai_findings, ensure_ascii=False)
        await self._execute(
            f"""
            INSERT INTO {TABLE_INSPECTION_TASKS}
                (id, project_id, inspector_id, title, type, status,
                 location, longitude, latitude, notes, ai_findings,
                 assigned_glasses_id, synced_to_cloud, cloud_synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status = excluded.status,
                notes = COALESCE(excluded.notes, inspection_tasks.notes),
                ai_findings = COALESCE(excluded.ai_findings, inspection_tasks.ai_findings),
                assigned_glasses_id = COALESCE(excluded.assigned_glasses_id, inspection_tasks.assigned_glasses_id),
                completed_at = COALESCE(excluded.completed_at, inspection_tasks.completed_at)
            """,
            (
                task_id, task.get("project_id", ""), task.get("inspector_id"),
                task.get("title", ""), task.get("type", "routine"),
                task.get("status", "pending"), task.get("location"),
                task.get("longitude"), task.get("latitude"),
                task.get("notes"), ai_findings, task.get("assigned_glasses_id"),
                1 if task.get("synced_to_cloud") else 0,
                self._now_iso(),
            ),
        )
        return task_id

    async def get_inspection_task(self, task_id: str) -> dict[str, Any] | None:
        return await self._fetchone(
            f"SELECT * FROM {TABLE_INSPECTION_TASKS} WHERE id = ?", (task_id,)
        )

    async def list_pending_inspection_tasks(
        self, glasses_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """查询待执行的巡检任务。"""
        if glasses_id:
            return await self._fetchall(
                f"""SELECT * FROM {TABLE_INSPECTION_TASKS}
                    WHERE status = 'pending' AND assigned_glasses_id = ?
                    ORDER BY created_at ASC LIMIT ?""",
                (glasses_id, limit),
            )
        return await self._fetchall(
            f"""SELECT * FROM {TABLE_INSPECTION_TASKS}
                WHERE status = 'pending'
                ORDER BY created_at ASC LIMIT ?""",
            (limit,),
        )

    async def update_inspection_task_status(
        self, task_id: str, status: str, notes: str | None = None,
        ai_findings: dict | None = None,
    ) -> None:
        findings_json = json.dumps(ai_findings, ensure_ascii=False) if ai_findings else None
        await self._execute(
            f"""UPDATE {TABLE_INSPECTION_TASKS}
            SET status = ?,
                notes = COALESCE(?, notes),
                ai_findings = COALESCE(?, ai_findings),
                completed_at = CASE WHEN ? = 'completed' THEN ? ELSE completed_at END
            WHERE id = ?""",
            (status, notes, findings_json, status, self._now_iso(), task_id),
        )

    # ── 4. inspection_photos_cache ────────────────────────────────

    async def cache_photo(
        self,
        inspection_id: str,
        local_path: str,
        original_filename: str | None = None,
        file_size: int | None = None,
        mime_type: str = "image/jpeg",
        taken_at: str | None = None,
        minio_bucket: str | None = None,
        minio_object_name: str | None = None,
    ) -> str:
        """缓存照片记录。"""
        photo_id = self._generate_uuid()
        await self._execute(
            f"""
            INSERT INTO {TABLE_PHOTOS_CACHE}
                (id, inspection_id, local_path, original_filename,
                 file_size, mime_type, taken_at, minio_bucket, minio_object_name,
                 upload_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            (photo_id, inspection_id, local_path, original_filename,
             file_size, mime_type, taken_at, minio_bucket, minio_object_name),
        )
        return photo_id

    async def update_photo_upload_status(
        self, photo_id: str, status: str,
        minio_object_name: str | None = None,
        error: str | None = None,
    ) -> None:
        await self._execute(
            f"""UPDATE {TABLE_PHOTOS_CACHE}
            SET upload_status = ?,
                minio_object_name = COALESCE(?, minio_object_name),
                last_error = ?,
                retry_count = retry_count + 1,
                uploaded_at = CASE WHEN ? = 'uploaded' THEN ? ELSE uploaded_at END
            WHERE id = ?""",
            (status, minio_object_name, error, status, self._now_iso(), photo_id),
        )

    async def get_pending_photos(self, limit: int = 20) -> list[dict[str, Any]]:
        """获取待上传的照片。"""
        return await self._fetchall(
            f"""SELECT * FROM {TABLE_PHOTOS_CACHE}
                WHERE upload_status IN ('pending', 'failed')
                ORDER BY created_at ASC LIMIT ?""",
            (limit,),
        )

    # ── 5. ai_detection_cache ─────────────────────────────────────

    async def cache_ai_detection(
        self,
        photo_id: str | None,
        inspection_id: str | None,
        device_id: int | None,
        model_name: str,
        model_version: str,
        detection_json: str,
        class_counts: dict | None = None,
        inference_time_ms: int = 0,
        confidence_avg: float = 0.0,
    ) -> int:
        """缓存 AI 检测结果。"""
        counts_json = json.dumps(class_counts, ensure_ascii=False) if class_counts else None
        cursor = await self._execute(
            f"""
            INSERT INTO {TABLE_AI_DETECTION}
                (photo_id, inspection_id, device_id, model_name, model_version,
                 detection_json, class_counts, inference_time_ms, confidence_avg)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (photo_id, inspection_id, device_id, model_name, model_version,
             detection_json, counts_json, inference_time_ms, confidence_avg),
        )
        return cursor.lastrowid or -1

    async def get_detections_by_photo(self, photo_id: str) -> list[dict[str, Any]]:
        return await self._fetchall(
            f"SELECT * FROM {TABLE_AI_DETECTION} WHERE photo_id = ?",
            (photo_id,),
        )

    # ── 6. rag_query_cache ────────────────────────────────────────

    async def cache_rag_query(
        self,
        question: str,
        answer: str,
        sources: list | None = None,
        confidence: float = 0.0,
        project_id: str | None = None,
        conversation_id: str | None = None,
        response_time_ms: int = 0,
        from_local: bool = True,
    ) -> int:
        sources_json = json.dumps(sources, ensure_ascii=False) if sources else None
        cursor = await self._execute(
            f"""
            INSERT INTO {TABLE_RAG_CACHE}
                (question, answer, sources, confidence, project_id,
                 conversation_id, response_time_ms, from_local)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (question, answer, sources_json, confidence, project_id,
             conversation_id, response_time_ms, 1 if from_local else 0),
        )
        return cursor.lastrowid or -1

    async def search_rag_cache(self, question: str, project_id: str | None = None) -> dict[str, Any] | None:
        """精确匹配 RAG 缓存。"""
        if project_id:
            return await self._fetchone(
                f"SELECT * FROM {TABLE_RAG_CACHE} WHERE question = ? AND project_id = ? ORDER BY created_at DESC LIMIT 1",
                (question, project_id),
            )
        return await self._fetchone(
            f"SELECT * FROM {TABLE_RAG_CACHE} WHERE question = ? ORDER BY created_at DESC LIMIT 1",
            (question,),
        )

    # ── 7. alerts_cache ───────────────────────────────────────────

    async def cache_alert(
        self,
        alert_type: str,
        severity: str,
        description: str,
        project_id: str | None = None,
        inspection_id: str | None = None,
        device_id: int | None = None,
        screenshot_path: str | None = None,
    ) -> str:
        """缓存告警（自动入同步队列，优先级最高）。"""
        alert_id = self._generate_uuid()
        await self._execute(
            f"""
            INSERT INTO {TABLE_ALERTS}
                (id, device_id, inspection_id, project_id,
                 alert_type, severity, description, screenshot_path,
                 sync_priority, sync_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'pending')
            """,
            (alert_id, device_id, inspection_id, project_id,
             alert_type, severity, description, screenshot_path),
        )
        return alert_id

    async def update_alert_sync_status(
        self, alert_id: str, status: str,
        cloud_alert_id: str | None = None, error: str | None = None,
    ) -> None:
        await self._execute(
            f"""UPDATE {TABLE_ALERTS}
            SET sync_status = ?,
                cloud_alert_id = COALESCE(?, cloud_alert_id),
                last_error = ?,
                retry_count = retry_count + 1,
                synced_at = CASE WHEN ? = 'synced' THEN ? ELSE synced_at END
            WHERE id = ?""",
            (status, cloud_alert_id, error, status, self._now_iso(), alert_id),
        )

    async def get_pending_alerts(self, limit: int = 50) -> list[dict[str, Any]]:
        """获取待同步告警（按优先级+时间排序）。"""
        return await self._fetchall(
            f"""SELECT * FROM {TABLE_ALERTS}
                WHERE sync_status IN ('pending', 'failed')
                ORDER BY sync_priority ASC, created_at ASC LIMIT ?""",
            (limit,),
        )

    # ── 8. sync_state ─────────────────────────────────────────────

    async def enqueue_sync(
        self,
        table_name: str,
        record_id: str,
        sync_direction: str = "upload",
        sync_priority: int = 5,
        payload_json: str | None = None,
    ) -> None:
        """将一条记录加入同步队列。"""
        await self._execute(
            f"""
            INSERT OR IGNORE INTO {TABLE_SYNC_STATE}
                (table_name, record_id, sync_direction, sync_priority,
                 status, payload_json, next_retry_at)
            VALUES (?, ?, ?, ?, 'pending', ?, ?)
            """,
            (table_name, record_id, sync_direction, sync_priority,
             payload_json, self._now_iso()),
        )

    async def get_pending_sync_records(self, limit: int = 50) -> list[dict[str, Any]]:
        """按优先级获取待同步记录（核心查询）。

        优先级排序：sync_priority ASC → next_retry_at ASC → created_at ASC
        """
        return await self._fetchall(
            f"""
            SELECT * FROM {TABLE_SYNC_STATE}
            WHERE status IN ('pending', 'failed')
              AND (next_retry_at IS NULL OR next_retry_at <= ?)
            ORDER BY sync_priority ASC, next_retry_at ASC, created_at ASC
            LIMIT ?
            """,
            (self._now_iso(), limit),
        )

    async def update_sync_status(
        self,
        sync_id: int,
        status: str,
        error: str | None = None,
        retry_count: int | None = None,
        next_retry_at: str | None = None,
    ) -> None:
        """更新同步状态。"""
        await self._execute(
            f"""UPDATE {TABLE_SYNC_STATE}
            SET status = ?,
                last_error = ?,
                retry_count = COALESCE(?, retry_count),
                next_retry_at = COALESCE(?, next_retry_at)
            WHERE id = ?""",
            (status, error, retry_count, next_retry_at, sync_id),
        )

    async def mark_synced(self, table_name: str, record_id: str, direction: str = "upload") -> None:
        """标记记录为已同步。"""
        await self._execute(
            f"""UPDATE {TABLE_SYNC_STATE}
            SET status = 'success', next_retry_at = NULL
            WHERE table_name = ? AND record_id = ? AND sync_direction = ?""",
            (table_name, record_id, direction),
        )

    async def get_sync_stats(self) -> dict[str, int]:
        """获取同步统计。"""
        stats: dict[str, int] = {}
        for status in ("pending", "in_progress", "success", "failed"):
            cursor = await self.conn.execute(
                f"SELECT COUNT(*) FROM {TABLE_SYNC_STATE} WHERE status = ?", (status,)
            )
            row = await cursor.fetchone()
            stats[status] = row[0] if row else 0
        return stats

    # ── 维护方法 ──────────────────────────────────────────────────

    async def cleanup_old_records(self, days: int = 30) -> int:
        """清理超过 N 天的已同步记录。"""
        cutoff = (datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0))
        cutoff_str = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
        # 删除已同步的 sync_state 记录
        cursor = await self._execute(
            f"""DELETE FROM {TABLE_SYNC_STATE}
            WHERE status = 'success' AND updated_at < datetime(?, '-{days} days')""",
            (cutoff_str,),
        )
        deleted = cursor.rowcount or 0
        logger.info("cleanup_old_records", deleted=deleted, days=days)
        return deleted

    async def vacuum(self) -> None:
        """执行 VACUUM 压缩数据库。"""
        await self.conn.execute("VACUUM")
        logger.info("db_vacuumed")
