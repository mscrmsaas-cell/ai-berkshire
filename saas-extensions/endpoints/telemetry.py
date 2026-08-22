"""设备遥测数据 API — 批量写入与按设备/时间范围查询。

FastAPI Router，对应 PostgreSQL 表：
  - device_telemetry（时序数据表）

注册方式::

    from app.api.v1.endpoints.telemetry import router as telemetry_router
    api_router.include_router(telemetry_router, prefix="/telemetry", tags=["设备遥测"])
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, text, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session

router = APIRouter()


# ============================================================
# Pydantic 模型
# ============================================================

class TelemetryItem(BaseModel):
    """单个遥测数据点。"""

    device_id: UUID
    device_type: str = Field(..., pattern=r"^(glasses|bag_terminal)$")
    # 眼镜遥测
    battery_level: int | None = Field(None, ge=0, le=100)
    battery_voltage_mv: int | None = None
    is_charging: bool | None = None
    temperature_c: float | None = None
    ble_rssi: int | None = None
    # 终端遥测
    cpu_usage: float | None = Field(None, ge=0, le=100)
    memory_usage: float | None = Field(None, ge=0, le=100)
    disk_usage: float | None = Field(None, ge=0, le=100)
    # AI 遥测
    ai_inference_time_ms: int | None = None
    ai_detections_count: int | None = None
    rag_query_count: int | None = None
    # 网络遥测
    network_type: str | None = Field(None, pattern=r"^(5g|wifi|none)$")
    signal_rsrp: int | None = None
    signal_rsrq: int | None = None
    signal_sinr: int | None = None
    # 扩展
    raw_data: dict | None = None
    recorded_at: datetime | None = None


class TelemetryBatch(BaseModel):
    """批量遥测数据写入请求。"""

    tenant_id: UUID
    items: list[TelemetryItem] = Field(..., min_length=1, max_length=1000)


class TelemetryBatchResponse(BaseModel):
    """批量写入响应。"""

    inserted: int
    failed: int = 0
    errors: list[str] = []


class TelemetryQueryResponse(BaseModel):
    """遥测查询响应。"""

    device_id: UUID
    device_type: str
    items: list[dict[str, Any]]
    count: int
    time_range_start: datetime
    time_range_end: datetime


class TelemetrySummary(BaseModel):
    """遥测数据汇总统计。"""

    device_id: UUID
    device_type: str
    period_start: datetime
    period_end: datetime
    data_points: int
    avg_battery_level: float | None
    avg_cpu_usage: float | None
    avg_memory_usage: float | None
    avg_temperature: float | None
    avg_ai_inference_time_ms: float | None
    total_ai_detections: int | None
    total_rag_queries: int | None
    min_signal_rsrp: int | None
    max_signal_rsrp: int | None


# ============================================================
# 数据库依赖
# ============================================================

async def get_db():
    async with async_session() as session:
        try:
            yield session
        finally:
            await session.close()


# ============================================================
# 批量写入
# ============================================================

@router.post("/batch", response_model=TelemetryBatchResponse, status_code=201)
async def batch_insert_telemetry(
    batch: TelemetryBatch,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """批量写入设备遥测数据。

    :param batch: 批量数据（最多 1000 条/次）
    :return: 插入成功/失败数量
    """
    inserted = 0
    failed = 0
    errors: list[str] = []

    for idx, item in enumerate(batch.items):
        try:
            recorded_at = item.recorded_at or datetime.now(timezone.utc)
            params: dict[str, Any] = {
                "tenant_id": batch.tenant_id,
                "device_id": item.device_id,
                "device_type": item.device_type,
                "battery_level": item.battery_level,
                "battery_voltage_mv": item.battery_voltage_mv,
                "is_charging": item.is_charging,
                "temperature_c": item.temperature_c,
                "ble_rssi": item.ble_rssi,
                "cpu_usage": item.cpu_usage,
                "memory_usage": item.memory_usage,
                "disk_usage": item.disk_usage,
                "ai_inference_time_ms": item.ai_inference_time_ms,
                "ai_detections_count": item.ai_detections_count,
                "rag_query_count": item.rag_query_count,
                "network_type": item.network_type,
                "signal_rsrp": item.signal_rsrp,
                "signal_rsrq": item.signal_rsrq,
                "signal_sinr": item.signal_sinr,
                "raw_data": json.dumps(item.raw_data) if item.raw_data else None,
                "recorded_at": recorded_at,
            }

            await db.execute(
                text("""
                    INSERT INTO device_telemetry
                        (tenant_id, device_id, device_type,
                         battery_level, battery_voltage_mv, is_charging,
                         temperature_c, ble_rssi,
                         cpu_usage, memory_usage, disk_usage,
                         ai_inference_time_ms, ai_detections_count, rag_query_count,
                         network_type, signal_rsrp, signal_rsrq, signal_sinr,
                         raw_data, recorded_at)
                    VALUES (:tenant_id, :device_id, :device_type,
                            :battery_level, :battery_voltage_mv, :is_charging,
                            :temperature_c, :ble_rssi,
                            :cpu_usage, :memory_usage, :disk_usage,
                            :ai_inference_time_ms, :ai_detections_count, :rag_query_count,
                            :network_type, :signal_rsrp, :signal_rsrq, :signal_sinr,
                            :raw_data, :recorded_at)
                """),
                params,
            )
            inserted += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            errors.append(f"Item {idx}: {str(exc)[:200]}")
            await db.rollback()

    await db.commit()
    return {
        "inserted": inserted,
        "failed": failed,
        "errors": errors[:20],  # 限制错误列表长度
    }


# ============================================================
# 查询
# ============================================================

@router.get("/{device_id}", response_model=TelemetryQueryResponse)
async def query_telemetry(
    device_id: UUID,
    device_type: str = Query(..., pattern=r"^(glasses|bag_terminal)$"),
    start_time: datetime | None = Query(default=None),
    end_time: datetime | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=10000),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """按设备 + 时间范围查询遥测数据。

    :param device_id: 设备 UUID
    :param device_type: 设备类型
    :param start_time: 查询起始时间（默认最近 24 小时）
    :param end_time: 查询结束时间（默认当前时间）
    :param limit: 返回数据条数上限
    """
    now = datetime.now(timezone.utc)
    start = start_time or (now - timedelta(hours=24))
    end = end_time or now

    result = await db.execute(
        select(text("* FROM device_telemetry "
                    "WHERE device_id = :did AND device_type = :dt "
                    "AND recorded_at BETWEEN :start AND :end "
                    "ORDER BY recorded_at DESC LIMIT :limit")),
        {"did": device_id, "dt": device_type, "start": start, "end": end, "limit": limit},
    )
    rows = result.fetchall()
    items = [dict(row._mapping) for row in rows]

    return {
        "device_id": device_id,
        "device_type": device_type,
        "items": items,
        "count": len(items),
        "time_range_start": start,
        "time_range_end": end,
    }


@router.get("/{device_id}/summary", response_model=TelemetrySummary)
async def get_telemetry_summary(
    device_id: UUID,
    device_type: str = Query(..., pattern=r"^(glasses|bag_terminal)$"),
    period_hours: int = Query(default=24, ge=1, le=720),  # 最大 30 天
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """获取遥测数据汇总统计。

    :param period_hours: 统计时间范围（小时）
    """
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=period_hours)

    result = await db.execute(
        text("""
            SELECT
                COUNT(*) AS data_points,
                AVG(battery_level) AS avg_battery_level,
                AVG(cpu_usage) AS avg_cpu_usage,
                AVG(memory_usage) AS avg_memory_usage,
                AVG(temperature_c) AS avg_temperature,
                AVG(ai_inference_time_ms) AS avg_ai_inference_time_ms,
                SUM(COALESCE(ai_detections_count, 0)) AS total_ai_detections,
                SUM(COALESCE(rag_query_count, 0)) AS total_rag_queries,
                MIN(signal_rsrp) AS min_signal_rsrp,
                MAX(signal_rsrp) AS max_signal_rsrp
            FROM device_telemetry
            WHERE device_id = :did AND device_type = :dt
              AND recorded_at >= :start
        """),
        {"did": device_id, "dt": device_type, "start": start},
    )
    row = result.fetchone()

    return {
        "device_id": device_id,
        "device_type": device_type,
        "period_start": start,
        "period_end": now,
        "data_points": row[0] or 0,
        "avg_battery_level": round(row[1], 1) if row[1] else None,
        "avg_cpu_usage": round(row[2], 1) if row[2] else None,
        "avg_memory_usage": round(row[3], 1) if row[3] else None,
        "avg_temperature": round(row[4], 1) if row[4] else None,
        "avg_ai_inference_time_ms": round(row[5], 1) if row[5] else None,
        "total_ai_detections": row[6] or 0,
        "total_rag_queries": row[7] or 0,
        "min_signal_rsrp": row[8],
        "max_signal_rsrp": row[9],
    }


# ============================================================
# 数据清理
# ============================================================

@router.delete("/cleanup")
async def cleanup_old_telemetry(
    retention_days: int = Query(default=90, ge=1, le=365),
    tenant_id: UUID | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """清理超过保留期的遥测数据。

    :param retention_days: 数据保留天数
    :param tenant_id: 可选，限定租户
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

    if tenant_id:
        result = await db.execute(
            text("DELETE FROM device_telemetry WHERE tenant_id = :tid AND recorded_at < :cutoff"),
            {"tid": tenant_id, "cutoff": cutoff},
        )
    else:
        result = await db.execute(
            text("DELETE FROM device_telemetry WHERE recorded_at < :cutoff"),
            {"cutoff": cutoff},
        )

    deleted = result.rowcount
    await db.commit()

    return {
        "deleted": deleted,
        "retention_days": retention_days,
        "cutoff": cutoff.isoformat(),
    }


# ============================================================
# 最新遥测
# ============================================================

@router.get("/{device_id}/latest")
async def get_latest_telemetry(
    device_id: UUID,
    device_type: str = Query(..., pattern=r"^(glasses|bag_terminal)$"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """获取设备最新一条遥测数据。"""
    result = await db.execute(
        select(text("* FROM device_telemetry "
                    "WHERE device_id = :did AND device_type = :dt "
                    "ORDER BY recorded_at DESC LIMIT 1")),
        {"did": device_id, "dt": device_type},
    )
    row = result.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="No telemetry data found")
    return dict(row._mapping)
