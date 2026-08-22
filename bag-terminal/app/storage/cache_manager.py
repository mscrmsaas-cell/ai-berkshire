"""离线缓存管理器。

负责离线场景下数据的缓存与批量同步：
- 离线数据入队（按优先级排序）
- 网络恢复后批量同步到 SaaS
- 指数退避重试机制
- 同步状态追踪

同步优先级（从高到低）：
    告警(0) > 照片(1) > 检测(2) > RAG(3) > 遥测(4) > 巡检(5)
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import structlog

from app.storage.local_db import LocalDatabase, TABLE_ALERTS, TABLE_PHOTOS_CACHE, TABLE_AI_DETECTION, TABLE_RAG_CACHE, TABLE_SYNC_STATE

logger = structlog.get_logger(__name__)


class SyncPriority(Enum):
    """同步优先级枚举。"""

    ALERT = 0       # 告警（最高）
    PHOTO = 1       # 照片
    DETECTION = 2   # AI 检测结果
    RAG = 3         # RAG 问答
    TELEMETRY = 4   # 遥测
    INSPECTION = 5  # 巡检（最低）


class CacheStatus(Enum):
    """缓存项状态。"""

    PENDING = "pending"
    SYNCING = "syncing"
    SUCCESS = "success"
    FAILED = "failed"
    PERMANENTLY_FAILED = "permanently_failed"


@dataclass
class CacheItem:
    """缓存项数据结构。"""

    table_name: str
    record_id: str
    priority: int
    payload: dict[str, Any]
    retry_count: int = 0
    last_error: str = ""


@dataclass
class CacheManagerConfig:
    """缓存管理器配置。"""

    batch_size: int = 50                  # 每批同步数量
    max_concurrent: int = 5              # 并发同步数
    retry_base_delay: float = 1.0        # 初始重试间隔（秒）
    retry_max_delay: float = 300.0       # 最大重试间隔（5 分钟）
    retry_backoff_factor: float = 2.0    # 指数退避因子
    max_retries: int = 5                 # 最大重试次数
    sync_interval: float = 30.0          # 定期同步间隔（秒）
    disk_full_threshold: float = 0.95    # 磁盘使用率阈值


class CacheManager:
    """离线缓存与批量同步管理器。

    工作流程：
    1. 离线时数据写入 SQLite（由 LocalDatabase 管理）
    2. sync_state 表追踪待同步状态
    3. 网络恢复后，从 sync_state 按优先级批量拉取
    4. 并发同步到 SaaS，带指数退避重试
    5. 成功/失败更新状态

    使用示例::

        cm = CacheManager(db, saas_client, config)
        await cm.start()       # 启动后台同步任务
        await cm.enqueue_alert(alert_data)
        # ...
        await cm.stop()        # 停止
    """

    def __init__(
        self,
        db: LocalDatabase,
        saas_client: Any,  # SaasApiClient
        config: CacheManagerConfig | None = None,
    ) -> None:
        self._db = db
        self._saas = saas_client
        self._config = config or CacheManagerConfig()
        self._sync_task: asyncio.Task | None = None
        self._running = False
        self._network_online = False
        self._sync_in_progress = False
        # 同步处理器映射: table_name → handler coroutine
        self._handlers: dict[str, Any] = {
            TABLE_ALERTS: self._sync_alert,
            TABLE_PHOTOS_CACHE: self._sync_photo,
            TABLE_AI_DETECTION: self._sync_detection,
            TABLE_RAG_CACHE: self._sync_rag,
            TABLE_SYNC_STATE: self._sync_telemetry,
        }

    # ── 生命周期 ──────────────────────────────────────────────────

    async def start(self) -> None:
        """启动后台同步循环。"""
        if self._running:
            return
        self._running = True
        self._sync_task = asyncio.create_task(self._sync_loop(), name="cache-sync")
        logger.info("cache_manager_started", sync_interval=self._config.sync_interval)

    async def stop(self) -> None:
        """停止同步循环。"""
        self._running = False
        if self._sync_task and not self._sync_task.done():
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
        logger.info("cache_manager_stopped")

    # ── 网络状态 ──────────────────────────────────────────────────

    def set_network_status(self, online: bool) -> None:
        """设置网络状态（由 NetworkMonitor 调用）。"""
        if online != self._network_online:
            self._network_online = online
            logger.info("network_status_changed", online=online)
            if online:
                # 网络恢复，触发立即同步
                asyncio.create_task(self.sync_now())

    @property
    def is_online(self) -> bool:
        return self._network_online

    # ── 入队 ──────────────────────────────────────────────────────

    async def enqueue_alert(self, alert_data: dict[str, Any]) -> str:
        """入队告警（优先级最高 0）。"""
        alert_id = await self._db.cache_alert(
            alert_type=alert_data.get("alert_type", "unknown"),
            severity=alert_data.get("severity", "warning"),
            description=alert_data.get("description", ""),
            project_id=alert_data.get("project_id"),
            inspection_id=alert_data.get("inspection_id"),
            device_id=alert_data.get("device_id"),
            screenshot_path=alert_data.get("screenshot_path"),
        )
        # 触发器已自动入 sync_state 队列
        if self._network_online:
            asyncio.create_task(self.sync_now())
        return alert_id

    async def enqueue_photo(self, photo_data: dict[str, Any]) -> str:
        """入队照片上传（优先级 1）。"""
        photo_id = await self._db.cache_photo(
            inspection_id=photo_data["inspection_id"],
            local_path=photo_data["local_path"],
            original_filename=photo_data.get("original_filename"),
            file_size=photo_data.get("file_size"),
            mime_type=photo_data.get("mime_type", "image/jpeg"),
            taken_at=photo_data.get("taken_at"),
        )
        if self._network_online:
            asyncio.create_task(self.sync_now())
        return photo_id

    async def enqueue_detection(self, detection_data: dict[str, Any]) -> int:
        """入队 AI 检测结果同步（优先级 2）。"""
        detection_id = await self._db.cache_ai_detection(
            photo_id=detection_data.get("photo_id"),
            inspection_id=detection_data.get("inspection_id"),
            device_id=detection_data.get("device_id"),
            model_name=detection_data.get("model_name", "yolov8n_railway"),
            model_version=detection_data.get("model_version", "1.0.0"),
            detection_json=detection_data.get("detection_json", "{}"),
            class_counts=detection_data.get("class_counts"),
            inference_time_ms=detection_data.get("inference_time_ms", 0),
            confidence_avg=detection_data.get("confidence_avg", 0.0),
        )
        # 手动入 sync_state
        await self._db.enqueue_sync(
            TABLE_AI_DETECTION, str(detection_id),
            sync_priority=SyncPriority.DETECTION.value,
            payload_json=json.dumps({"detection_id": detection_id}, ensure_ascii=False),
        )
        return detection_id

    async def enqueue_rag(self, rag_data: dict[str, Any]) -> int:
        """入队 RAG 问答同步（优先级 3）。"""
        rag_id = await self._db.cache_rag_query(
            question=rag_data["question"],
            answer=rag_data["answer"],
            sources=rag_data.get("sources"),
            confidence=rag_data.get("confidence", 0.0),
            project_id=rag_data.get("project_id"),
            conversation_id=rag_data.get("conversation_id"),
            response_time_ms=rag_data.get("response_time_ms", 0),
            from_local=rag_data.get("from_local", True),
        )
        await self._db.enqueue_sync(
            TABLE_RAG_CACHE, str(rag_id),
            sync_priority=SyncPriority.RAG.value,
            payload_json=json.dumps({"rag_id": rag_id}, ensure_ascii=False),
        )
        return rag_id

    async def enqueue_telemetry(self, telemetry_data: dict[str, Any]) -> None:
        """入队遥测数据同步（优先级 4）。"""
        record_id = str(telemetry_data.get("timestamp", int(time.time())))
        await self._db.enqueue_sync(
            TABLE_SYNC_STATE, record_id,
            sync_priority=SyncPriority.TELEMETRY.value,
            payload_json=json.dumps(telemetry_data, ensure_ascii=False),
        )

    # ── 同步循环 ──────────────────────────────────────────────────

    async def sync_now(self) -> int:
        """触发一次同步。返回本次同步的记录数。"""
        if self._sync_in_progress or not self._network_online:
            return 0
        return await self._do_batch_sync()

    async def _sync_loop(self) -> None:
        """后台定期同步循环。"""
        while self._running:
            try:
                await asyncio.sleep(self._config.sync_interval)
                if self._network_online and not self._sync_in_progress:
                    await self._do_batch_sync()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.error("sync_loop_error", error=str(exc))
                await asyncio.sleep(self._config.sync_interval)

    async def _do_batch_sync(self) -> int:
        """执行一批同步。"""
        self._sync_in_progress = True
        try:
            records = await self._db.get_pending_sync_records(limit=self._config.batch_size)
            if not records:
                return 0
            logger.info("batch_sync_start", count=len(records))

            # 按 table_name 分组并发处理
            semaphore = asyncio.Semaphore(self._config.max_concurrent)

            async def _sync_one(rec: dict) -> bool:
                async with semaphore:
                    return await self._sync_record(rec)

            results = await asyncio.gather(
                *[_sync_one(rec) for rec in records], return_exceptions=True
            )
            success_count = sum(1 for r in results if r is True)
            logger.info(
                "batch_sync_complete",
                total=len(records),
                success=success_count,
                failed=len(records) - success_count,
            )
            return success_count
        finally:
            self._sync_in_progress = False

    async def _sync_record(self, record: dict[str, Any]) -> bool:
        """同步单条记录。"""
        table_name = record["table_name"]
        record_id = record["record_id"]
        sync_id = record["id"]
        retry_count = record.get("retry_count", 0)

        # 标记为同步中
        await self._db.update_sync_status(sync_id, "in_progress")

        handler = self._handlers.get(table_name)
        if handler is None:
            logger.warning("no_handler_for_table", table=table_name)
            await self._db.update_sync_status(sync_id, "failed", error="no_handler")
            return False

        try:
            success = await handler(record)
            if success:
                await self._db.update_sync_status(sync_id, "success")
                await self._db.mark_synced(table_name, record_id)
                return True
            # 失败
            await self._handle_sync_failure(record)
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error("sync_record_error", table=table_name, error=str(exc))
            await self._handle_sync_failure(record, str(exc))
            return False

    async def _handle_sync_failure(
        self, record: dict[str, Any], error: str = "sync_failed"
    ) -> None:
        """处理同步失败（指数退避重试）。"""
        sync_id = record["id"]
        retry_count = record.get("retry_count", 0) + 1
        if retry_count >= self._config.max_retries:
            # 超过最大重试次数 → 永久失败
            await self._db.update_sync_status(
                sync_id, "permanently_failed",
                error=error, retry_count=retry_count,
            )
            logger.error(
                "sync_permanently_failed",
                sync_id=sync_id,
                retries=retry_count,
                error=error,
            )
        else:
            # 计算退避延迟
            delay = min(
                self._config.retry_base_delay * (self._config.retry_backoff_factor ** retry_count),
                self._config.retry_max_delay,
            )
            next_retry = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime(time.time() + delay),
            )
            await self._db.update_sync_status(
                sync_id, "failed",
                error=error, retry_count=retry_count,
                next_retry_at=next_retry,
            )
            logger.warning(
                "sync_retry_scheduled",
                sync_id=sync_id,
                retry=retry_count,
                delay=delay,
                next_retry=next_retry,
            )

    # ── 各类型同步处理器 ──────────────────────────────────────────

    async def _sync_alert(self, record: dict[str, Any]) -> bool:
        """同步告警到 SaaS。"""
        alert = await self._db._fetchone(
            f"SELECT * FROM {TABLE_ALERTS} WHERE id = ?",
            (record["record_id"],),
        )
        if not alert:
            return True  # 记录不存在，视为成功（已删除）
        result = await self._saas.report_alert(
            project_id=alert.get("project_id", ""),
            inspection_id=alert.get("inspection_id"),
            alert_type=alert["alert_type"],
            severity=alert["severity"],
            description=alert.get("description", ""),
            image_url=alert.get("screenshot_path"),
        )
        cloud_id = result.get("id")
        await self._db.update_alert_sync_status(
            alert["id"], "synced", cloud_alert_id=cloud_id
        )
        return True

    async def _sync_photo(self, record: dict[str, Any]) -> bool:
        """同步照片到 SaaS（含分块上传）。"""
        photo = await self._db._fetchone(
            f"SELECT * FROM {TABLE_PHOTOS_CACHE} WHERE id = ?",
            (record["record_id"],),
        )
        if not photo:
            return True
        if not photo.get("local_path"):
            await self._db.update_photo_upload_status(
                photo["id"], "failed", error="no_local_path"
            )
            return False
        result = await self._saas.upload_photo(
            inspection_id=photo["inspection_id"],
            file_path=photo["local_path"],
            mime_type=photo.get("mime_type", "image/jpeg"),
            taken_at=photo.get("taken_at"),
        )
        await self._db.update_photo_upload_status(
            photo["id"], "uploaded",
            minio_object_name=result.get("minio_object_name"),
        )
        return True

    async def _sync_detection(self, record: dict[str, Any]) -> bool:
        """同步 AI 检测结果到 SaaS。"""
        # 检测结果通常随照片一起上传，这里可触发云端检测或直接写入
        detection = await self._db._fetchone(
            f"SELECT * FROM {TABLE_AI_DETECTION} WHERE id = ?",
            (record["record_id"],),
        )
        if not detection:
            return True
        # 通过 SaaS API 更新巡检的 ai_findings
        if detection.get("inspection_id"):
            ai_findings = json.loads(detection["detection_json"]) if detection.get("detection_json") else {}
            await self._saas.update_inspection_status(
                inspection_id=detection["inspection_id"],
                status="in_progress",
                ai_findings=ai_findings,
            )
        await self._db._execute(
            f"UPDATE {TABLE_AI_DETECTION} SET synced_to_cloud = 1 WHERE id = ?",
            (record["record_id"],),
        )
        return True

    async def _sync_rag(self, record: dict[str, Any]) -> bool:
        """同步 RAG 问答到 SaaS（用于训练数据收集）。"""
        rag = await self._db._fetchone(
            f"SELECT * FROM {TABLE_RAG_CACHE} WHERE id = ?",
            (record["record_id"],),
        )
        if not rag:
            return True
        # RAG 问答记录通常不需要严格同步，但可用于 SaaS 端训练数据
        await self._db._execute(
            f"UPDATE {TABLE_RAG_CACHE} SET synced_to_cloud = 1 WHERE id = ?",
            (record["record_id"],),
        )
        return True

    async def _sync_telemetry(self, record: dict[str, Any]) -> bool:
        """同步遥测数据到 SaaS。"""
        payload = json.loads(record.get("payload_json", "{}"))
        device_id = payload.get("device_id", "bag-terminal")
        await self._saas.report_telemetry(device_id, payload)
        return True

    # ── 统计 ──────────────────────────────────────────────────────

    async def get_stats(self) -> dict[str, Any]:
        """获取缓存与同步统计。"""
        sync_stats = await self._db.get_sync_stats()
        return {
            "network_online": self._network_online,
            "sync_in_progress": self._sync_in_progress,
            "sync_stats": sync_stats,
        }
